# ingenic-flash-tool

Lightweight Python CLI tool for flashing Ingenic T-series SoCs (T20/T21/T23/T30/T31/T40/T41) via USB boot mode. Drop-in replacement for the proprietary Ingenic USB Cloner GUI tool.

## Installation

```bash
pip install ingenic-flash-tool
```

Requires `libusb` on your system:
- **Linux**: `sudo apt install libusb-1.0-0` or `sudo pacman -S libusb`
- **macOS**: `brew install libusb`

### udev rules (Linux)

To use without `sudo`, add a udev rule:

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="a108", MODE="0666"' | sudo tee /etc/udev/rules.d/99-ingenic.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Usage

### Detect device

Check if an Ingenic device is connected in USB boot mode:

```
$ ingenic-flash-tool detect
Device found!
  USB PID:  0xc309 (T-series)
  CPU info: b'T 3 1 V ' (hex: 5420332031205620)
```

### Flash firmware

Flash a firmware image to SPI NOR flash:

```
$ ingenic-flash-tool flash prj008 firmware.bin
INFO: Boot ROM CPU info: 5420332031205620
INFO: Loading ginfo (324 bytes) to 0x80001000
INFO: Loading SPL (32384 bytes) to 0x80001800
INFO: Starting SPL: SET_DATA_LEN(0x7000) + START1(0x80001800)
INFO: SPL running: 5420332031205620
INFO: Loading stage2 (417656 bytes) to 0x80100000
INFO: Executing stage2: FLUSH_CACHES + PROGRAM_START2
INFO: Stage2 running: b'PRJ\x00\x00\x00\x00\x00'
INFO: Flash JEDEC ID: 1870ef
INFO: Initializing flash (chip erase)...
INFO: Flash init complete (14s)
INFO: Writing 232652 bytes (227K) at offset 0x0
  [########################################] 100% (232652/232652)
Flash complete!
```

Options:
- `-v` / `-vv` — increase verbosity
- `--offset 0x40000` — write at a specific flash offset
- `--erase-all` — full chip erase before writing (default: sector erase only)
- `--no-reboot` — don't reboot the device after flashing

### Boot device

Boot the device into the stage2 burner without flashing (useful for debugging):

```
$ ingenic-flash-tool boot prj008
Device booted into stage2: b'PRJ\x00\x00\x00\x00\x00' (50524a0000000000)
```

### Show chip info

```
$ ingenic-flash-tool info
Supported chips:
  jz4775      PID=0x4775  ginfo=0xf4000800
  prj008      PID=0xc309  ginfo=0x80001000 [bundled]
  t20         PID=0xc309  ginfo=0x80001000 [bundled]
  t21         PID=0xc309  ginfo=0x80001000 [bundled]
  ...

$ ingenic-flash-tool info --chip prj008
Chip:        PRJ008 [bundled]
ginfo addr:  0x80001000
SPL addr:    0x80001800
Stage2 addr: 0x80100000
d2i_len:     0x7000
USB PID:     0xc309
```

## Supported Hardware

| Chip/Board | Boot | Flash | Notes |
|------------|------|-------|-------|
| PRJ008     | Yes  | Yes   | Full support (T31/T33 camera board, SPI NOR) |
| T20–T41    | Yes  | No    | Boot only — need board-specific ginfo + config |

Full flash support requires board-specific firmware files (`ginfo.bin`, `spl.bin`, `uboot.bin`, and config blobs) captured from a working Ingenic USB Cloner session. The PRJ008 board files are bundled.

## How It Works

The tool implements the Ingenic USB Cloner protocol, reverse-engineered from USB packet captures:

1. **Boot ROM** — The SoC enters USB boot mode. The tool sends a DDR configuration block (ginfo) and first-stage bootloader (SPL) via vendor USB requests.

2. **SPL** — The SPL initializes DDR memory, then stays resident and re-implements USB. It does **not** return to the boot ROM. The tool sends the stage2 burner firmware (U-Boot) via the SPL's USB interface.

3. **Stage2 Burner** — A specialized U-Boot build that handles flash operations. The tool sends board configuration, triggers chip erase, then writes firmware in 64KB chunks with CRC verification.

### Protocol details

```
Boot ROM requests: GET_CPU_INFO(0x00), SET_DATA_ADDR(0x01), SET_DATA_LEN(0x02),
                   PROGRAM_START1(0x04)
SPL requests:      FLUSH_CACHES(0x03), PROGRAM_START2(0x05)
Stage2 requests:   GET_ACK(0x10), INIT(0x11), WRITE(0x12), UPDATE_CFG(0x14),
                   REBOOT(0x16), GET_FLASH_INFO(0x26)
```

Key discovery: `FLUSH_CACHES` and `PROGRAM_START2` are only supported by the resident SPL, **not** the boot ROM (contrary to the JZ4760 documentation). The boot ROM only supports requests `0x00`–`0x02` and `0x04`.

## Development

```bash
git clone https://github.com/will-tm/ingenic-flash-tool.git
cd ingenic-flash-tool
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## License

MIT
