"""Ingenic USB Cloner protocol implementation.

Complete protocol reverse-engineered from USB capture of the Ingenic Cloner tool.

Boot sequence (ROM → SPL → Stage2):
  1. Boot ROM: load ginfo + SPL via SET_ADDR/SET_LEN/bulk/PROGRAM_START1
  2. SPL stays resident, initializes DDR + USB
  3. SPL: load stage2 burner via SET_ADDR/SET_LEN/bulk/FLUSH_CACHES/PROGRAM_START2
  4. Stage2 burner initializes, responds to GET_CPU_INFO with board ID

Flash sequence (Stage2):
  5. UPDATE_CFG × 2 (policy + full config via TLV)
  6. GET_FLASH_INFO → JEDEC ID
  7. VR_INIT → chip erase
  8. SET_DATA_LEN(firmware_size)
  9. VR_WRITE(40b cmd) + bulk(64K chunk) → ACK (repeat for each chunk)
  10. VR_REBOOT
"""

import binascii
import importlib.resources
import logging
import struct
import time
from pathlib import Path

from .chips import ChipInfo
from .usb import USBDevice

log = logging.getLogger(__name__)


def find_firmware_dir(chip_name: str) -> Path:
    """Locate the bundled firmware directory for a chip."""
    base = chip_name.lower()
    pkg = importlib.resources.files("ingenic_flash") / "firmware"
    for name in [base, base.rstrip("x").rstrip("a").rstrip("n").rstrip("l")]:
        candidate = Path(str(pkg / name))
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"No bundled firmware for {chip_name!r}. "
        f"Available: {', '.join(d.name for d in Path(str(pkg)).iterdir() if d.is_dir())}"
    )


def boot_device(
    dev: USBDevice,
    chip: ChipInfo,
    ginfo_path: Path,
    spl_path: Path,
    stage2_path: Path,
    wait_time: float = 3.0,
) -> USBDevice:
    """Execute the full two-stage boot sequence.

    Loads ginfo + SPL via boot ROM, then stage2 via resident SPL.
    Returns the same USBDevice handle with stage2 running.
    """
    ginfo_data = ginfo_path.read_bytes()
    spl_data = spl_path.read_bytes()
    stage2_data = stage2_path.read_bytes()

    # Stage 1: ginfo + SPL → DDR init, SPL stays resident
    info = dev.get_cpu_info()
    log.info("Boot ROM CPU info: %s", info.hex())

    log.info("Loading ginfo (%d bytes) to 0x%08x", len(ginfo_data), chip.ginfo_addr)
    dev.set_data_address(chip.ginfo_addr)
    dev.set_data_length(len(ginfo_data))
    dev.bulk_write(ginfo_data)

    log.info("Loading SPL (%d bytes) to 0x%08x", len(spl_data), chip.spl_addr)
    dev.set_data_address(chip.spl_addr)
    dev.set_data_length(len(spl_data))
    dev.bulk_write(spl_data)

    log.info("Starting SPL: SET_DATA_LEN(0x%x) + START1(0x%08x)", chip.d2i_len, chip.spl_addr)
    dev.set_data_length(chip.d2i_len)
    dev.program_start1(chip.spl_addr)

    log.info("Waiting %.1fs for SPL to init DDR + USB...", wait_time)
    time.sleep(wait_time)
    info = dev.get_cpu_info()
    log.info("SPL running: %s", info.hex())

    # Stage 2: load burner into DRAM via SPL
    log.info("Loading stage2 (%d bytes) to 0x%08x", len(stage2_data), chip.stage2_addr)
    dev.set_data_address(chip.stage2_addr)
    dev.set_data_length(len(stage2_data))
    dev.bulk_write(stage2_data)

    log.info("Executing stage2: FLUSH_CACHES + PROGRAM_START2")
    dev.flush_caches()
    dev.program_start2(chip.stage2_addr)

    log.info("Waiting %.1fs for stage2...", wait_time)
    time.sleep(wait_time)
    info = dev.get_cpu_info()
    log.info("Stage2 running: %s (%s)", info, info.hex())

    return dev


def _patch_sfc_erase_mode(cfg_bulk: bytes, erase_mode: int) -> bytes:
    """Patch the spi_erase field in the SFC TLV entry.

    erase_mode: 0 = SPI_NO_ERASE (sector erase per write)
                1 = SPI_ERASE_PART (full chip erase during init)
    """
    data = bytearray(cfg_bulk)
    off = 0
    while off < len(data) - 8:
        magic = struct.unpack("<I", data[off:off+4])[0]
        size = struct.unpack("<I", data[off+4:off+8])[0]
        if magic == 0x53464300:  # SFC TLV
            # spi_param offset 12 = spi_erase field
            struct.pack_into("<I", data, off + 8 + 12, erase_mode)
            log.debug("Patched SFC spi_erase to %d", erase_mode)
            return bytes(data)
        off += 8 + size
    return bytes(data)  # no SFC TLV found, return unchanged


def flash_firmware(
    dev: USBDevice,
    chip: ChipInfo,
    firmware_path: Path,
    fw_dir: Path,
    offset: int = 0,
    reboot: bool = True,
    erase_all: bool = False,
    progress_cb=None,
) -> None:
    """Flash firmware using the full Ingenic Cloner protocol.

    fw_dir must contain: ginfo.bin, spl.bin, uboot.bin,
    cfg1_ep0.bin, cfg1_bulk.bin, cfg2_ep0.bin, cfg2_bulk.bin
    """
    # Verify all required files exist
    required = ["ginfo.bin", "spl.bin", "uboot.bin",
                "cfg1_ep0.bin", "cfg1_bulk.bin", "cfg2_ep0.bin", "cfg2_bulk.bin"]
    for f in required:
        if not (fw_dir / f).exists():
            raise FileNotFoundError(f"Missing {f} in {fw_dir}")

    # Boot
    dev = boot_device(dev, chip,
        fw_dir / "ginfo.bin", fw_dir / "spl.bin", fw_dir / "uboot.bin")

    # Configure stage2
    log.info("Sending UPDATE_CFG #1")
    dev.stage2_update_cfg(
        (fw_dir / "cfg1_ep0.bin").read_bytes(),
        (fw_dir / "cfg1_bulk.bin").read_bytes())
    ack = struct.unpack("<i", dev.stage2_get_ack())[0]
    if ack != 0:
        raise RuntimeError(f"UPDATE_CFG #1 failed: {ack}")

    jedec = dev.stage2_get_flash_info()
    log.info("Flash JEDEC ID: %s", jedec.hex())

    cfg2_bulk = (fw_dir / "cfg2_bulk.bin").read_bytes()
    if erase_all:
        log.info("Sending UPDATE_CFG #2 (chip erase mode)")
    else:
        cfg2_bulk = _patch_sfc_erase_mode(cfg2_bulk, 0)  # SPI_NO_ERASE
        log.info("Sending UPDATE_CFG #2 (sector erase mode)")
    dev.stage2_update_cfg(
        (fw_dir / "cfg2_ep0.bin").read_bytes(), cfg2_bulk)
    ack = struct.unpack("<i", dev.stage2_get_ack())[0]
    if ack != 0:
        raise RuntimeError(f"UPDATE_CFG #2 failed: {ack}")

    # Init (triggers chip erase)
    log.info("Initializing flash (chip erase)...")
    dev._dev.ctrl_transfer(0x40, 0x11, 0, 0, b"", timeout=5000)

    for attempt in range(60):
        time.sleep(2)
        try:
            ack = struct.unpack("<i", dev.stage2_get_ack())[0]
            if ack == 0:
                log.info("Flash init complete (%ds)", (attempt + 1) * 2)
                break
            elif ack != -16:  # -EBUSY
                raise RuntimeError(f"Flash init failed: {ack}")
        except struct.error:
            continue
        except Exception:
            continue
    else:
        raise RuntimeError("Flash init timed out")

    # Set total firmware size
    fw_data = firmware_path.read_bytes()
    total = len(fw_data)
    dev.set_data_length(total)
    log.info("Writing %d bytes (%dK) at offset 0x%x", total, total // 1024, offset)

    # Write in 64KB chunks
    ops = 0x00060000  # SPISFC | SFC_NOR
    sent = 0
    while sent < total:
        chunk = fw_data[sent:sent + 65536]
        crc = ~binascii.crc32(chunk) & 0xFFFFFFFF

        # 40-byte write command (reverse-engineered from USB capture)
        write_cmd = struct.pack("<QIIIIII", 0, offset + sent, 0, len(chunk), 0, ops, crc)
        write_cmd = write_cmd.ljust(40, b'\x00')

        dev._dev.ctrl_transfer(0x40, 0x12, 0, 0, write_cmd, timeout=5000)
        dev.bulk_write(chunk)
        ack = struct.unpack("<i", dev.stage2_get_ack())[0]

        sent += len(chunk)
        if ack != 0:
            raise RuntimeError(f"Write failed at offset 0x{offset + sent:x}: ack={ack}")
        if progress_cb:
            progress_cb(sent, total)

    log.info("Flash complete: %d bytes written", total)

    if reboot:
        log.info("Rebooting device")
        dev.stage2_reboot()
