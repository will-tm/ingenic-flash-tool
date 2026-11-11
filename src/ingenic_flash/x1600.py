"""X1600EN DDR2/64 MiB and 16 MiB SPI NOR burner configuration.

Wire layouts match Ingenic Cloner 2.5.58.2. The SFC header has eight u32
fields; it must not be passed through the T33 cfg2 offset-based patcher.
"""

import struct

from .spiflash_db import ChipParams, _pack_op, _pack_sr


FLASH_SIZE = 16 * 1024 * 1024
DDR_ID = 0x00420042


def _tlv(magic, payload):
    return struct.pack("<II", magic, len(payload)) + payload


def make_ginfo(ddr_record):
    """Pack Cloner's detect flag, board/UART/SFC GPIO settings and DDR TLVs."""
    if len(ddr_record) != 168:
        raise ValueError("X1600 DDR record must be 168 bytes")
    if struct.unpack_from("<III", ddr_record, 32) != (0x42, 4, 350000000):
        raise ValueError("Expected X1600EN DDR2/64 MiB at 350 MHz")
    if struct.unpack_from("<II", ddr_record, 140) != (64 * 1024 * 1024, 0):
        raise ValueError("Expected a single 64 MiB DDR chip")
    # ModuleInfo's 24-byte payload followed by ModuleGPIO's 160-byte payload.
    board = bytearray(184)
    struct.pack_into("<6I", board, 0, 24000000, 800000000, 350000000, 0, 2, 115200)
    struct.pack_into("<7I", board, 24, 1, 1, 1, 1, 2, 0x10, 0x007E0000)
    # SPL clears 27 calibration bytes beyond the 168-byte fallback DDR record.
    # Reserve that space and a zero TLV terminator inside the ginfo upload.
    return (_tlv(0x44455445, struct.pack("<I", 0))
            + _tlv(0x42444946, board) + _tlv(0x44445200, ddr_record) + bytes(32))


def validate_image(data, offset=0):
    """Accept only complete images for this DDR2/64 MiB board profile."""
    if offset != 0 or len(data) != FLASH_SIZE:
        raise ValueError("X1600 native flashing requires a full 16 MiB image at offset 0")
    if struct.unpack_from("<I", data, 128)[0] != DDR_ID:
        raise ValueError("Image DDR ID does not match the bundled X1600EN DDR2/64 MiB profile")


def make_cfg1():
    """Select SFC NOR before querying the live JEDEC ID."""
    policy = bytearray(164)
    struct.pack_into("<I", policy, 20, 1)
    return _tlv(0x504F4C49, policy)


def make_cfg2(chip: ChipParams, erase_all=False):
    """Build a raw-image configuration with per-write read-back checking.

    A single dynamic-MTD partition covers the entire chip. Parameter download
    and DDR-ID injection are disabled so offset-zero writes preserve every byte.
    Standard (single-bit) SPI transfers avoid changing the flash's QE bits.
    """
    if chip.size != FLASH_SIZE:
        raise ValueError("X1600 profile requires a recognized 16 MiB SPI NOR chip")
    if len(chip.opcodes) != 7 or len(chip.sr_ops) != 3:
        raise ValueError("Incomplete SPI NOR command definition")
    if not 0 < chip.erase_size <= 65536 or 65536 % chip.erase_size:
        raise ValueError("SPI NOR erase size must divide the 64 KiB write chunk")
    info = bytearray(140)
    info[:32] = chip.name.encode("ascii")[:31].ljust(32, b"\x00")
    struct.pack_into("<I", info, 32, chip.jedec)
    info[36:78] = b"".join(_pack_op(op) for op in chip.opcodes)
    info[78:102] = b"".join(_pack_sr(op) for op in chip.sr_ops)
    struct.pack_into("<HH2x4I3IB3x", info, 102,
                     chip.quad_ops_mode, chip.address_ops_mode,
                     chip.tCHSH, chip.tSLCH, chip.tSHSL_RD, chip.tSHSL_WR,
                     chip.size, chip.page_size, chip.erase_size, chip.chip_erase_cmd)
    partitions = bytearray(484)
    struct.pack_into("<32s4I", partitions, 0, b"full", chip.size, 0, 0, 2)
    struct.pack_into("<I", partitions, 480, 1)
    full = struct.pack("<II", 0x00726F6E, 2) + info + partitions + struct.pack("<IB3x", 0, 0)
    # The mini structure's two u16 modes end on a 4-byte boundary.
    mini = info[:36] + info[36:48] + info[66:78] + info[78:106] + info[124:136]
    debug = _tlv(0x44424700, struct.pack("<5I", 1, 1, 1, 65536, 200))
    sfc = struct.pack("<8I", 0, 0, chip.erase_size, int(erase_all), 50000000, 0, 0, 0x5800)
    return debug + make_cfg1() + _tlv(0x53464300, sfc + full + mini)


def update_command(payload):
    """The 40-byte UPDATE_CFG control packet contains only the payload size."""
    return struct.pack("<I", len(payload)) + bytes(36)
