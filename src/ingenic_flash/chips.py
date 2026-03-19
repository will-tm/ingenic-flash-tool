"""Ingenic SoC chip definitions and memory maps."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChipInfo:
    name: str
    ginfo_addr: int      # Global info / DDR config load address
    spl_addr: int        # SPL code entry address
    stage2_addr: int     # Stage2 (U-Boot/burner) load address in DRAM
    d2i_len: int         # DDR init config region size
    usb_pid: int         # USB product ID in boot mode


# USB vendor ID for all Ingenic SoCs
INGENIC_VID = 0xA108

# Known USB product IDs
USB_PIDS = {
    0x4775: "JZ4775",
    0x4780: "JZ4780",
    0x4785: "JZ4785",
    0x1000: "X1000",
    0xC309: "T-series",
    0xEAEF: "X2000",
}

# Memory map groups (derived from firmware config.cfg files)
_T_SERIES = (0x80001000, 0x80001800, 0x80100000, 0x7000, 0xC309)
_JZ4775   = (0xF4000800, 0xF4001000, 0x80100000, 0x4000, 0x4775)
_M150     = (0xF4000800, 0xF4001000, 0x80100000, 0x4000, 0x4780)
_X1000    = (0xF4001000, 0xF4001800, 0x80100000, 0x7000, 0x1000)
_X2000    = (0xB2401000, 0xB2401800, 0x80100000, 0x7000, 0xEAEF)

CHIPS = {
    # T-series (all share PID 0xC309, same memory map)
    "t20":       ChipInfo("T20", *_T_SERIES),
    "t21":       ChipInfo("T21", *_T_SERIES),
    "t23":       ChipInfo("T23", *_T_SERIES),
    "t30":       ChipInfo("T30", *_T_SERIES),
    "t30a":      ChipInfo("T30A", *_T_SERIES),
    "t30nl":     ChipInfo("T30NL", *_T_SERIES),
    "t30x":      ChipInfo("T30X", *_T_SERIES),
    "t31":       ChipInfo("T31", *_T_SERIES),
    "t31a":      ChipInfo("T31A", *_T_SERIES),
    "t31nl":     ChipInfo("T31NL", *_T_SERIES),
    "t31x":      ChipInfo("T31X", *_T_SERIES),
    "t40":       ChipInfo("T40", *_T_SERIES),
    "t41":       ChipInfo("T41", *_T_SERIES),
    # A-series
    "a1_n_ne_x": ChipInfo("A1_N_NE_X", *_T_SERIES),
    "a1_nt_a":   ChipInfo("A1_NT_A", *_T_SERIES),
    # Board-specific (same memory map as T-series)
    "prj007":     ChipInfo("PRJ007", *_T_SERIES),
    "prj007_sfc1": ChipInfo("PRJ007_SFC1", *_T_SERIES),
    "prj008":     ChipInfo("PRJ008", *_T_SERIES),
    # AD/C series
    "ad100":     ChipInfo("AD100", *_T_SERIES),
    "c100":      ChipInfo("C100", *_T_SERIES),
    # JZ series
    "jz4775":    ChipInfo("JZ4775", *_JZ4775),
    # M series
    "m150":      ChipInfo("M150", *_M150),
    "m200":      ChipInfo("M200", 0x80001000, 0x80001800, 0x80100000, 0x7000, 0x4780),
    "m200s":     ChipInfo("M200S", 0x80001000, 0x80001800, 0x80100000, 0x7000, 0x4785),
    "m210":      ChipInfo("M210", *_X2000),
    "m300":      ChipInfo("M300", *_X2000),
    # X series
    "x1000":     ChipInfo("X1000", *_X1000),
    "x1021":     ChipInfo("X1021", *_T_SERIES),
    "x1500":     ChipInfo("X1500", 0xF4001000, 0xF4001800, 0x80100000, 0x7000, 0x1000),
    "x1520":     ChipInfo("X1520", 0x80001000, 0x80001800, 0x80100000, 0x7000, 0x1000),
    "x1521":     ChipInfo("X1521", *_T_SERIES),
    "x1600":     ChipInfo("X1600", *_T_SERIES),
    "x1630":     ChipInfo("X1630", 0x80001000, 0x80001800, 0x80100000, 0x7000, 0xC309),
    "x1800":     ChipInfo("X1800", 0x80001000, 0x80001800, 0x80100000, 0x7000, 0xC309),
    "x1830":     ChipInfo("X1830", 0x80001000, 0x80001800, 0x80100000, 0x7000, 0xC309),
    "x2000":     ChipInfo("X2000", *_X2000),
    "x2100":     ChipInfo("X2100", *_X2000),
    "x2500":     ChipInfo("X2500", *_T_SERIES),
    "x2580":     ChipInfo("X2580", *_T_SERIES),
    "x2600":     ChipInfo("X2600", *_T_SERIES),
}


def chip_by_name(name: str) -> ChipInfo:
    key = name.lower().replace("-", "").replace("_", "")
    if key in CHIPS:
        return CHIPS[key]
    # Try with underscores preserved (for a1_n_ne_x etc.)
    key2 = name.lower().replace("-", "_")
    if key2 in CHIPS:
        return CHIPS[key2]
    # Prefix match
    for k, v in CHIPS.items():
        if key.startswith(k):
            return v
    raise KeyError(f"Unknown chip: {name!r}. Known chips: {', '.join(sorted(CHIPS))}")
