"""CLI entry point for ingenic-flash-tool."""

import argparse
import importlib.resources
import logging
import sys
from pathlib import Path

from .chips import CHIPS, INGENIC_VID, USB_PIDS, chip_by_name
from .usb import find_device


def _parse_addr(s: str) -> int:
    return int(s, 0)


def _progress_bar(current: int, total: int) -> None:
    pct = current * 100 // total
    filled = 40 * current // total
    bar = "#" * filled + "-" * (40 - filled)
    sys.stderr.write(f"\r  [{bar}] {pct}% ({current}/{total})")
    if current >= total:
        sys.stderr.write("\n")
    sys.stderr.flush()


def _bundled_chips() -> set[str]:
    """Return set of chip names that have bundled firmware."""
    pkg = importlib.resources.files("ingenic_flash") / "firmware"
    fw_path = Path(str(pkg))
    if fw_path.exists():
        return {d.name for d in fw_path.iterdir() if d.is_dir()}
    return set()


def cmd_detect(args: argparse.Namespace) -> int:
    """Detect an Ingenic device in USB boot mode."""
    dev = find_device()
    if dev is None:
        print("No Ingenic device found in USB boot mode.")
        print(f"  Expected USB VID: 0x{INGENIC_VID:04x}")
        print(f"  Known PIDs: {', '.join(f'0x{p:04x} ({n})' for p, n in USB_PIDS.items())}")
        return 1

    print(f"Device found!")
    print(f"  USB PID:  0x{dev.pid:04x} ({dev.pid_name})")
    try:
        with dev:
            cpu_info = dev.get_cpu_info()
        print(f"  CPU info: {cpu_info} (hex: {cpu_info.hex()})")
    except Exception as e:
        print(f"  CPU info: failed ({e})")
        print(f"  Device may need to be power-cycled.")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    """Show chip information."""
    bundled = _bundled_chips()

    if args.chip:
        try:
            chip = chip_by_name(args.chip)
        except KeyError as e:
            print(str(e), file=sys.stderr)
            return 1
        tag = " [bundled]" if args.chip.lower() in bundled else ""
        print(f"Chip:        {chip.name}{tag}")
        print(f"ginfo addr:  0x{chip.ginfo_addr:08x}")
        print(f"SPL addr:    0x{chip.spl_addr:08x}")
        print(f"Stage2 addr: 0x{chip.stage2_addr:08x}")
        print(f"d2i_len:     0x{chip.d2i_len:04x}")
        print(f"USB PID:     0x{chip.usb_pid:04x}")
    else:
        print("Supported chips:")
        for name, chip in sorted(CHIPS.items()):
            tag = " [bundled]" if name in bundled else ""
            print(f"  {name:10s}  PID=0x{chip.usb_pid:04x}  ginfo=0x{chip.ginfo_addr:08x}{tag}")
    return 0


def cmd_flash(args: argparse.Namespace) -> int:
    """Flash firmware to device."""
    from .protocol import flash_firmware, find_firmware_dir

    try:
        chip = chip_by_name(args.chip)
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 1

    dev = find_device()
    if dev is None:
        print("No Ingenic device found in USB boot mode.", file=sys.stderr)
        return 1
    dev.open()

    try:
        fw_dir = find_firmware_dir(args.chip)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    try:
        flash_firmware(
            dev, chip,
            firmware_path=Path(args.firmware),
            fw_dir=fw_dir,
            offset=_parse_addr(args.offset) if args.offset else 0,
            reboot=not args.no_reboot,
            erase_all=args.erase_all,
            progress_cb=_progress_bar,
        )
        print("Flash complete!")
    except (RuntimeError, FileNotFoundError, TimeoutError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        dev.close()
    return 0


def cmd_boot(args: argparse.Namespace) -> int:
    """Boot device through stage1+stage2 without flashing."""
    from .protocol import boot_device, find_firmware_dir

    try:
        chip = chip_by_name(args.chip)
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 1

    dev = find_device()
    if dev is None:
        print("No Ingenic device found in USB boot mode.", file=sys.stderr)
        return 1
    dev.open()

    try:
        fw_dir = find_firmware_dir(args.chip)
        ginfo = fw_dir / "ginfo.bin"
        spl = fw_dir / "spl.bin"
        stage2 = fw_dir / "uboot.bin"
        for f in [ginfo, spl, stage2]:
            if not f.exists():
                print(f"Missing: {f}", file=sys.stderr)
                return 1

        dev = boot_device(dev, chip, ginfo, spl, stage2)
        info = dev.get_cpu_info()
        print(f"Device booted into stage2: {info} ({info.hex()})")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        dev.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ingenic-flash-tool",
        description="Lightweight tool for flashing Ingenic SoCs via USB boot mode",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Increase verbosity (-v info, -vv debug)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # detect
    sub.add_parser("detect", help="Detect Ingenic device in USB boot mode")

    # info
    p_info = sub.add_parser("info", help="Show chip/config information")
    p_info.add_argument("--chip", help="Chip name (e.g., prj008, t31)")

    # boot
    p_boot = sub.add_parser("boot", help="Boot device (stage1+stage2) without flashing")
    p_boot.add_argument("chip", help="Chip name (e.g., prj008)")

    # flash
    p_flash = sub.add_parser("flash", help="Flash firmware to device")
    p_flash.add_argument("chip", help="Chip name (e.g., prj008)")
    p_flash.add_argument("firmware", help="Path to firmware image to flash")
    p_flash.add_argument("--offset", default="0", help="Flash offset (hex or decimal)")
    p_flash.add_argument("--no-reboot", action="store_true", help="Don't reboot after flashing")
    p_flash.add_argument("--erase-all", action="store_true", help="Full chip erase before writing (default: sector erase)")

    args = parser.parse_args()

    level = logging.WARNING
    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose >= 1:
        level = logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    handlers = {
        "detect": cmd_detect,
        "info": cmd_info,
        "boot": cmd_boot,
        "flash": cmd_flash,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
