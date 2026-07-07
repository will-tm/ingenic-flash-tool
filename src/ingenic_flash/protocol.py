"""Ingenic USB boot and flash protocol implementation.

Reverse-engineered protocol for flashing Ingenic SoCs via USB boot mode.

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


# Ingenic GPIO controller, uncached KSEG1 mapping of physical 0x10010000.
# Ports are 0x1000 apart: PA=0x0000, PB=0x1000, PC=0x2000, PD=0x3000.
# Register offsets within a port (write-1-to-act set/clear pairs), matching
# the U-Boot board_early_init_f() sequence in the firmware tree:
#   PXINTC  0x18  - GPIO (vs device) function
#   PXMSKS  0x24  - GPIO active (vs interrupt)
#   PXPAT1C 0x38  - output mode
#   PXPAT0S 0x44 / PXPAT0C 0x48  - drive high / low
#   PXPUENC 0x118 - pull-up disable
#   PXPDENS 0x124 / PXPDENC 0x128 - pull-down enable / disable
GPIO_BASE = 0xB0010000


def _poke32(dev: USBDevice, addr: int, value: int) -> None:
    """Write one 32-bit word to an arbitrary address via the boot ROM.

    Reuses the boot ROM's download primitive (SET_ADDR + SET_LEN + bulk).
    Only valid while the device is still in boot-ROM mode, before SPL load.
    """
    dev.set_data_address(addr)
    dev.set_data_length(4)
    dev.bulk_write(struct.pack("<I", value))


def apply_gpio_writes(dev: USBDevice, gpio_writes) -> None:
    """Drive GPIOs as output high/low via the boot ROM, before any SPL.

    gpio_writes: iterable of (port_offset, pin, on) tuples.
    This is the earliest reachable point in a USB-boot session, so it is
    used e.g. to assert the PMIC power-hold line so the board stays alive
    for the whole flash without the operator holding the power button.
    """
    for port_offset, pin, on in gpio_writes:
        base = GPIO_BASE + port_offset
        bit = 1 << pin
        port_letter = chr(ord("A") + port_offset // 0x1000)
        log.info("GPIO poke: P%s%d -> %s (base=0x%08x bit=0x%08x)",
                 port_letter, pin, "on" if on else "off", base, bit)
        _poke32(dev, base + 0x18, bit)    # PXINTC  - GPIO mode
        _poke32(dev, base + 0x24, bit)    # PXMSKS  - GPIO active
        _poke32(dev, base + 0x38, bit)    # PXPAT1C - output mode
        _poke32(dev, base + 0x118, bit)   # PXPUENC - pull-up off
        if on:
            _poke32(dev, base + 0x44, bit)    # PXPAT0S - drive high
            _poke32(dev, base + 0x128, bit)   # PXPDENC - pull-down off
        else:
            _poke32(dev, base + 0x48, bit)    # PXPAT0C - drive low
            _poke32(dev, base + 0x124, bit)   # PXPDENS - pull-down on


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


def _wait_for_cpu_info(dev: USBDevice, max_wait: float, what: str) -> bytes:
    """Poll GET_CPU_INFO until the device answers or max_wait elapses.

    Used after PROGRAM_START1/2 to detect when the next stage (SPL or
    stage2 burner) has come up and is ready to handle USB requests,
    instead of sleeping a fixed duration.
    """
    import usb.core  # noqa: PLC0415
    deadline = time.monotonic() + max_wait
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return dev.get_cpu_info()
        except (usb.core.USBError, OSError) as e:
            last_err = e
            time.sleep(0.1)
    raise TimeoutError(f"{what} did not respond within {max_wait:.1f}s ({last_err})")


def boot_device(
    dev: USBDevice,
    chip: ChipInfo,
    ginfo_path: Path,
    spl_path: Path,
    stage2_path: Path,
    wait_time: float = 10.0,
    gpio_writes=None,
) -> USBDevice:
    """Execute the full two-stage boot sequence.

    Loads ginfo + SPL via boot ROM, then stage2 via resident SPL.
    Returns the same USBDevice handle with stage2 running.

    gpio_writes, if given, is an iterable of (port_offset, pin, on) tuples
    driven via the boot ROM before SPL load — the earliest reachable point.
    """
    ginfo_data = ginfo_path.read_bytes()
    spl_data = spl_path.read_bytes()
    stage2_data = stage2_path.read_bytes()

    # Stage 1: ginfo + SPL → DDR init, SPL stays resident
    info = dev.get_cpu_info()
    log.info("Boot ROM CPU info: %s", info.hex())

    # Drive requested GPIOs as early as possible, still in boot-ROM mode.
    if gpio_writes:
        log.info("Applying %d GPIO write(s) via boot ROM before SPL load",
                 len(gpio_writes))
        apply_gpio_writes(dev, gpio_writes)

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

    log.info("Waiting up to %.1fs for SPL to init DDR + USB...", wait_time)
    t0 = time.monotonic()
    info = _wait_for_cpu_info(dev, wait_time, "SPL")
    log.info("SPL running: %s (took %.1fs)", info.hex(), time.monotonic() - t0)

    # Stage 2: load burner into DRAM via SPL
    log.info("Loading stage2 (%d bytes) to 0x%08x", len(stage2_data), chip.stage2_addr)
    dev.set_data_address(chip.stage2_addr)
    dev.set_data_length(len(stage2_data))
    dev.bulk_write(stage2_data)

    log.info("Executing stage2: FLUSH_CACHES + PROGRAM_START2")
    dev.flush_caches()
    dev.program_start2(chip.stage2_addr)

    log.info("Waiting up to %.1fs for stage2...", wait_time)
    t0 = time.monotonic()
    info = _wait_for_cpu_info(dev, wait_time, "stage2 burner")
    log.info("Stage2 running: %s (%s) (took %.1fs)", info, info.hex(), time.monotonic() - t0)

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
    gpio_writes=None,
) -> None:
    """Flash firmware via the Ingenic USB boot protocol.

    fw_dir must contain: ginfo.bin, spl.bin, uboot.bin,
    cfg1_ep0.bin, cfg1_bulk.bin, cfg2_ep0.bin, cfg2_bulk.bin

    gpio_writes, if given, is an iterable of (port_offset, pin, on) tuples
    driven via the boot ROM before SPL load (e.g. PMIC power-hold).
    """
    # Verify all required files exist
    required = ["ginfo.bin", "spl.bin", "uboot.bin",
                "cfg1_ep0.bin", "cfg1_bulk.bin", "cfg2_ep0.bin", "cfg2_bulk.bin"]
    for f in required:
        if not (fw_dir / f).exists():
            raise FileNotFoundError(f"Missing {f} in {fw_dir}")

    # Boot
    dev = boot_device(dev, chip,
        fw_dir / "ginfo.bin", fw_dir / "spl.bin", fw_dir / "uboot.bin",
        gpio_writes=gpio_writes)

    # Configure stage2
    log.info("Sending UPDATE_CFG #1")
    dev.stage2_update_cfg(
        (fw_dir / "cfg1_ep0.bin").read_bytes(),
        (fw_dir / "cfg1_bulk.bin").read_bytes())
    ack = struct.unpack("<i", dev.stage2_get_ack())[0]
    if ack != 0:
        raise RuntimeError(f"UPDATE_CFG #1 failed: {ack}")

    jedec_raw = dev.stage2_get_flash_info()
    jedec_id = (jedec_raw[2] << 16) | (jedec_raw[1] << 8) | jedec_raw[0]
    log.info("Flash JEDEC ID: 0x%06x (%s)", jedec_id, jedec_raw.hex())

    cfg2_bulk = (fw_dir / "cfg2_bulk.bin").read_bytes()

    # Auto-patch cfg2 with the live chip's parameters from the bundled DB.
    # Lets one board firmware bundle support any SPI NOR in spiflashinfo.cfg
    # without per-chip variants (e.g. prj008_32m). For >16 MB chips the patcher
    # also substitutes dedicated 4-byte opcodes so the chip stays in 3-byte
    # mode and warm-reboot reads cleanly.
    from .spiflash_db import lookup_chip, patch_cfg2
    chip_def = lookup_chip(jedec_id)
    if chip_def is not None:
        log.info("Recognized %s (%d MB) from chip database",
                 chip_def.name, chip_def.size // (1024 * 1024))
        cfg2_bulk = patch_cfg2(cfg2_bulk, chip_def)
    else:
        log.warning("JEDEC 0x%06x not in chip database — using bundled cfg as-is",
                    jedec_id)

    if erase_all:
        log.info("Sending UPDATE_CFG #2 (chip erase mode)")
    else:
        cfg2_bulk = _patch_sfc_erase_mode(cfg2_bulk, 0)
        log.info("Sending UPDATE_CFG #2 (sector erase mode)")
    dev.stage2_update_cfg(
        (fw_dir / "cfg2_ep0.bin").read_bytes(), cfg2_bulk)
    ack = struct.unpack("<i", dev.stage2_get_ack())[0]
    if ack != 0:
        raise RuntimeError(f"UPDATE_CFG #2 failed: {ack}")

    # Init (triggers chip erase if --erase-all)
    fw_data = firmware_path.read_bytes()
    total = len(fw_data)

    if erase_all:
        log.info("Initializing flash (chip erase)...")
    else:
        log.info("Initializing flash...")
    dev._dev.ctrl_transfer(0x40, 0x11, 0, 0, b"", timeout=5000)

    # Poll ACK — chip erase can take 15-120s depending on flash size
    # Scale max wait: 30s base + 1s per 64KB of firmware
    max_polls = max(30, 15 + total // 65536)
    for attempt in range(max_polls):
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
    dev.set_data_length(total)
    log.info("Writing %d bytes (%dK) at offset 0x%x", total, total // 1024, offset)

    # Detect flash type from firmware directory name
    fw_dir_name = fw_dir.name.lower()
    if "mmc0" in fw_dir_name:
        ops = 0x00020000  # MMC slot 0
    elif "mmc1" in fw_dir_name:
        ops = 0x00020001  # MMC slot 1
    elif "nand" in fw_dir_name:
        ops = 0x00060001  # SPISFC | SFC_NAND
    else:
        ops = 0x00060000  # SPISFC | SFC_NOR (default)
    log.debug("Flash ops: 0x%06x", ops)
    sent = 0
    while sent < total:
        chunk = fw_data[sent:sent + 65536]
        crc = ~binascii.crc32(chunk) & 0xFFFFFFFF

        # 40-byte write command (reverse-engineered from USB capture)
        write_cmd = struct.pack("<QIIIIII", 0, offset + sent, 0, len(chunk), 0, ops, crc)
        write_cmd = write_cmd.ljust(40, b'\x00')

        dev._dev.ctrl_transfer(0x40, 0x12, 0, 0, write_cmd, timeout=5000)
        dev.bulk_write(chunk)
        # ACK timeout scales with chunk size: sector erase + program + verify
        # ~1-2s typical per 64KB, up to 10s on slow flash, 30s safety margin
        ack_timeout = max(10000, len(chunk) // 32 * 1000)  # ~1s per 32KB, min 10s
        ack = struct.unpack("<i", bytes(
            dev._dev.ctrl_transfer(0xC0, 0x10, 0, 0, 4, timeout=ack_timeout)
        ))[0]

        sent += len(chunk)
        if ack != 0:
            raise RuntimeError(f"Write failed at offset 0x{offset + sent:x}: ack={ack}")
        if progress_cb:
            progress_cb(sent, total)

    log.info("Flash complete: %d bytes written", total)

    if reboot:
        log.info("Rebooting device")
        dev.stage2_reboot()
