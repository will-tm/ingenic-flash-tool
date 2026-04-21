"""SPI NOR chip database and runtime cfg2_bulk.bin patcher.

Parses the vendor's `spiflashinfo.cfg` into a JEDEC→params map, then patches a
board's `cfg2_bulk.bin` with the live chip's parameters before the burner sees
it. This lets one board firmware bundle (e.g. `prj008`) flash any SPI NOR in
the database without per-chip firmware variants.

For chips >16 MB, the patcher substitutes dedicated 4-byte-address opcodes
(0x12/0x13/0x5c, etc.) and disables ENTER_4B, so the chip stays in its
power-on 3-byte mode. Without this, the burner would leave the chip in
4-byte mode and the SoC's boot ROM (which uses 3-byte addressing) would read
garbage on warm-reboot — see prj008_32m investigation history.

The cfg2_bulk.bin TLV layout was reverse-engineered from prj008/cfg2_bulk.bin
and validated against W25Q128JVSM (norkey54). Field offsets are absolute
within the file (the SFC TLV body starts at 0xd0).
"""

from __future__ import annotations

import importlib.resources
import logging
import re
import struct
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# Opcode tuple: (cmd, dummy_bits, addr_bytes, transfer_mode)
Opcode = tuple[int, int, int, int]
# Status-register tuple: (cmd, bit_shift, mask, value, length, dummy)
SROp = tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class ChipParams:
    name: str
    jedec: int           # 24-bit JEDEC ID (mfr<<16 | dev_hi<<8 | dev_lo)
    size: int            # bytes
    page_size: int
    erase_size: int      # block size for the configured erase command
    chip_erase_cmd: int  # full-chip erase opcode (typically 0x60)
    quad_ops_mode: int   # 0 = direct cmd to enter quad, 1 = via SR bit
    address_ops_mode: int  # 0 = direct ENTER_4B, 1 = WREN+ENTER_4B
    block_erase_time_ms: int
    tCHSH: int
    tSLCH: int
    tSHSL_RD: int
    tSHSL_WR: int
    # 7 main opcodes in order: READ, FAST_READ, PAGE_PROG, QUAD_PP, ERASE, WREN, ENTER_4B
    # Disabled opcodes use cmd = -1.
    opcodes: list[Opcode] = field(default_factory=list)
    # 3 SR ops: WRITE_SR1, WRITE_SR2, READ_SR_BUSY
    sr_ops: list[SROp] = field(default_factory=list)


# === Parser =================================================================

_VALUE_RE = re.compile(r'^value\s*=\s*"([^"]+)"\s*$')


def _parse_int(s: str) -> int:
    s = s.strip()
    return int(s, 16) if s.lower().startswith("0x") else int(s, 10)


def parse_spiflashinfo(text: str) -> dict[int, ChipParams]:
    """Parse the vendor INI-style chip database into a JEDEC→ChipParams map."""
    db: dict[int, ChipParams] = {}
    for line in text.splitlines():
        m = _VALUE_RE.match(line.strip())
        if not m:
            continue
        parts = [p.strip() for p in m.group(1).split(",")]
        # Only the SPI NOR section has this exact field count (59).
        # NAND entries have a different layout; skip them quietly.
        if len(parts) < 59:
            continue
        try:
            name = parts[0]
            jedec = _parse_int(parts[1])
            size = _parse_int(parts[2])
            page = _parse_int(parts[3])
            erase = _parse_int(parts[4])
            erase_cmd = _parse_int(parts[5])
            quad_mode = _parse_int(parts[6])
            addr_mode = _parse_int(parts[7])
            be_time = _parse_int(parts[8])
            tCHSH = _parse_int(parts[9])
            tSLCH = _parse_int(parts[10])
            tSHSL_RD = _parse_int(parts[11])
            tSHSL_WR = _parse_int(parts[12])
            # 7 opcodes × 4 fields = 28 numbers, indices 13..40
            ops: list[Opcode] = []
            for i in range(7):
                base = 13 + i * 4
                cmd = _parse_int(parts[base])
                ops.append((
                    cmd,
                    _parse_int(parts[base + 1]),
                    _parse_int(parts[base + 2]),
                    _parse_int(parts[base + 3]),
                ))
            # 3 SR ops × 6 fields = 18 numbers, indices 41..58
            sr_ops: list[SROp] = []
            for i in range(3):
                base = 41 + i * 6
                sr_ops.append(tuple(_parse_int(parts[base + j]) for j in range(6)))  # type: ignore
            db[jedec] = ChipParams(
                name=name, jedec=jedec, size=size, page_size=page,
                erase_size=erase, chip_erase_cmd=erase_cmd,
                quad_ops_mode=quad_mode, address_ops_mode=addr_mode,
                block_erase_time_ms=be_time,
                tCHSH=tCHSH, tSLCH=tSLCH, tSHSL_RD=tSHSL_RD, tSHSL_WR=tSHSL_WR,
                opcodes=ops, sr_ops=sr_ops,
            )
        except (ValueError, IndexError) as e:
            log.debug("Skipping malformed entry %r: %s", parts[0] if parts else "?", e)
    return db


_DB_CACHE: Optional[dict[int, ChipParams]] = None


def load_chip_db() -> dict[int, ChipParams]:
    """Load the bundled chip DB (cached after first call)."""
    global _DB_CACHE
    if _DB_CACHE is None:
        text = (importlib.resources.files("ingenic_flash") / "firmware" / "spiflashinfo.cfg").read_text()
        _DB_CACHE = parse_spiflashinfo(text)
        log.debug("Loaded %d chip entries from spiflashinfo.cfg", len(_DB_CACHE))
    return _DB_CACHE


def lookup_chip(jedec: int) -> Optional[ChipParams]:
    return load_chip_db().get(jedec)


# === Patcher ================================================================

# Field offsets within cfg2_bulk.bin's primary chip entry (validated against
# prj008/cfg2_bulk.bin's W25Q128JVSM @ 0xef7018).
_PRI_NAME = 0x0f4         # 32 bytes, NUL-padded
_PRI_JEDEC = 0x114        # 3 bytes LE + 1 pad
_PRI_OPCODES = 0x118      # 7 × 6 bytes
_PRI_SR_OPS = 0x142       # 3 × 8 bytes
_PRI_QUAD_MODE = 0x15a    # u16 (with 2 bytes pad following)
_PRI_ADDR_MODE = 0x15c    # u16 (with 2 bytes pad following)
_PRI_TIMINGS = 0x160      # 4 × u32 (tCHSH, tSLCH, tSHSL_RD, tSHSL_WR)
_PRI_CHIP_SIZE = 0x170    # u32
_PRI_PAGE_SIZE = 0x174    # u32
_PRI_ERASE_SIZE = 0x178   # u32
_PRI_ERASE_CMD = 0x17c    # u32

# Secondary (fallback) chip entry — partial structure
_SEC_NAME = 0x36c         # 32 bytes
_SEC_JEDEC = 0x38c        # 3 + 1
_SEC_OPCODES = 0x390      # 4 × 6 (READ, FAST_READ, WREN, ENTER_4B)
_SEC_CHIP_SIZE = 0x3c4    # u32
_SEC_PAGE_SIZE = 0x3c8    # u32
_SEC_ERASE_SIZE = 0x3cc   # u32

# Indices into ChipParams.opcodes / cfg-file-order
OP_READ, OP_FAST_READ, OP_PAGE_PROG, OP_QUAD_PP, OP_ERASE, OP_WREN, OP_ENTER_4B = range(7)

# Dedicated 4-byte-address opcodes (per common Winbond/Boya/GigaDevice 256+ Mbit
# datasheets). Used to address >16 MB without entering 4-byte mode.
_DEDICATED_4B = {
    0x03: 0x13,  # READ → READ4B
    0x0b: 0x0c,  # FAST_READ → FAST_READ4B
    0x6b: 0x6c,  # FAST_READ_QUAD → FAST_READ_QUAD4B
    0x02: 0x12,  # PAGE_PROGRAM → PAGE_PROGRAM4B
    0x32: 0x34,  # QUAD_PAGE_PROGRAM → QUAD_PP4B
    0x52: 0x5c,  # BLOCK_ERASE_32K → BLOCK_ERASE_32K_4B
    0xd8: 0xdc,  # BLOCK_ERASE_64K → BLOCK_ERASE_64K_4B
    0x20: 0x21,  # SECTOR_ERASE_4K → SECTOR_ERASE_4K_4B
}


def _pack_op(op: Opcode) -> bytes:
    cmd, dummy, addr_bytes, mode = op
    if cmd < 0:
        return b"\xff\xff\xff\xff\xff\x00"  # disabled marker
    return bytes([cmd & 0xff, 0, dummy & 0xff, addr_bytes & 0xff, mode & 0xff, 0])


def _pack_sr(sr: SROp) -> bytes:
    cmd, bit_shift, mask, value, length, dummy = sr
    if cmd < 0:
        return b"\xff\xff\xff\xff\xff\xff\xff\xff"
    return bytes([cmd & 0xff, 0,
                  bit_shift & 0xff, mask & 0xff, value & 0xff, length & 0xff,
                  dummy & 0xff, 0])


def _convert_to_dedicated_4byte(opcodes: list[Opcode]) -> list[Opcode]:
    """Substitute dedicated 4B opcodes and disable ENTER_4B.

    Used for >16 MB chips so the burner can address the upper half via
    explicit 4-byte opcodes without putting the chip into 4-byte address
    mode. Result: chip stays in 3-byte mode, warm-reboot reads cleanly.
    """
    new = []
    for i, (cmd, dummy, addr, mode) in enumerate(opcodes):
        if i == OP_ENTER_4B:
            new.append((-1, 0, 0, 0))  # disabled
            continue
        if cmd in _DEDICATED_4B:
            cmd = _DEDICATED_4B[cmd]
            addr = 4
        new.append((cmd, dummy, addr, mode))
    return new


def patch_cfg2(
    cfg2_bulk: bytes,
    chip: ChipParams,
    force_4byte_dedicated: Optional[bool] = None,
) -> bytes:
    """Patch a cfg2_bulk.bin with a chip's params.

    force_4byte_dedicated: if None (default), auto-enable for chips >16 MB.
    """
    if force_4byte_dedicated is None:
        force_4byte_dedicated = chip.size > (16 * 1024 * 1024)

    opcodes = (_convert_to_dedicated_4byte(chip.opcodes)
               if force_4byte_dedicated else chip.opcodes)

    data = bytearray(cfg2_bulk)

    # Primary chip entry
    name = chip.name.encode("ascii", errors="replace").ljust(32, b"\x00")[:32]
    data[_PRI_NAME:_PRI_NAME + 32] = name
    data[_PRI_JEDEC:_PRI_JEDEC + 4] = bytes([
        chip.jedec & 0xff, (chip.jedec >> 8) & 0xff, (chip.jedec >> 16) & 0xff, 0,
    ])
    op_blob = b"".join(_pack_op(op) for op in opcodes)
    assert len(op_blob) == 7 * 6, "expected 7 opcodes"
    data[_PRI_OPCODES:_PRI_OPCODES + len(op_blob)] = op_blob
    sr_blob = b"".join(_pack_sr(sr) for sr in chip.sr_ops)
    assert len(sr_blob) == 3 * 8, "expected 3 SR opcodes"
    data[_PRI_SR_OPS:_PRI_SR_OPS + len(sr_blob)] = sr_blob
    struct.pack_into("<H", data, _PRI_QUAD_MODE, chip.quad_ops_mode & 0xffff)
    struct.pack_into("<H", data, _PRI_ADDR_MODE, chip.address_ops_mode & 0xffff)
    struct.pack_into("<IIII", data, _PRI_TIMINGS,
                     chip.tCHSH, chip.tSLCH, chip.tSHSL_RD, chip.tSHSL_WR)
    struct.pack_into("<I", data, _PRI_CHIP_SIZE, chip.size)
    struct.pack_into("<I", data, _PRI_PAGE_SIZE, chip.page_size)
    struct.pack_into("<I", data, _PRI_ERASE_SIZE, chip.erase_size)
    struct.pack_into("<I", data, _PRI_ERASE_CMD, chip.chip_erase_cmd)

    # Secondary entry — only the fields that exist there
    data[_SEC_NAME:_SEC_NAME + 32] = name
    data[_SEC_JEDEC:_SEC_JEDEC + 4] = data[_PRI_JEDEC:_PRI_JEDEC + 4]
    # Secondary has 4 opcode slots: READ, FAST_READ, WREN, ENTER_4B
    sec_ops = b"".join(_pack_op(op) for op in [
        opcodes[OP_READ], opcodes[OP_FAST_READ], opcodes[OP_WREN], opcodes[OP_ENTER_4B],
    ])
    data[_SEC_OPCODES:_SEC_OPCODES + len(sec_ops)] = sec_ops
    struct.pack_into("<I", data, _SEC_CHIP_SIZE, chip.size)
    struct.pack_into("<I", data, _SEC_PAGE_SIZE, chip.page_size)
    struct.pack_into("<I", data, _SEC_ERASE_SIZE, chip.erase_size)

    return bytes(data)
