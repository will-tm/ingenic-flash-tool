"""Bundle the X1600EN native loaders and configuration from Ingenic Cloner.

Usage: python tools/bundle_x1600.py CLONER_DIRECTORY OUTPUT_DIRECTORY

Reads vendor files as data; never executes the Cloner launcher or host binaries.
"""

import hashlib
from pathlib import Path
import sys

from ingenic_flash.spiflash_db import lookup_chip
from ingenic_flash.x1600 import make_cfg1, make_cfg2, make_ginfo, update_command


SPL_SHA256 = "3fdeaacfcb4b173c3250bb83c72ddca1d6e71b08ea5303c9f4dc60455c0f9fcb"
UBOOT_SHA256 = "38d78fccd4c6633685c0e7895614e799760628b295b9509c122a5666bb6198d0"


def bundle(source, output):
    firmware = source / "firmwares" / "x1600"
    spl = (firmware / "spl.bin").read_bytes()
    uboot = (firmware / "uboot.bin").read_bytes()
    if hashlib.sha256(spl).hexdigest() != SPL_SHA256:
        raise ValueError("Expected the Ingenic Cloner 2.5.58.2 X1600 SPL")
    if hashlib.sha256(uboot).hexdigest() != UBOOT_SHA256:
        raise ValueError("Expected the Ingenic Cloner 2.5.58.2 X1600 burner")
    # The SPL's built-in table has 196-byte entries. Cloner's DDR TLV carries
    # the first 168 bytes; the SPL initializes the remaining calibration bytes.
    start = spl.index(b"X1600EN-DDR2\x00")
    chip = lookup_chip(0x0B4018)
    if chip is None or chip.name != "XT25F128F-W":
        raise ValueError("Bundled flash database must contain XT25F128F-W (0x0b4018)")
    cfg1 = make_cfg1()
    cfg2 = make_cfg2(chip)
    files = {
        "spl.bin": spl,
        "uboot.bin": uboot,
        "config.cfg": (firmware / "config.cfg").read_bytes(),
        "ginfo.bin": make_ginfo(spl[start:start + 168]),
        "cfg1_bulk.bin": cfg1,
        "cfg1_ep0.bin": update_command(cfg1),
        "cfg2_bulk.bin": cfg2,
        "cfg2_ep0.bin": update_command(cfg2),
    }
    output.mkdir(parents=True, exist_ok=True)
    manifest = ["[source]", "vendor=Ingenic", "package=cloner-2.5.58.2-ubuntu_alpha",
                "profile=X1600EN-DDR2-64MiB-SPI-NOR-16MiB", "", "[sha256]"]
    for name, data in files.items():
        (output / name).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        manifest.append(f"{name}={digest}")
        print(f"{name}: {len(data)} bytes, sha256={digest}")
    (output / "provenance.cfg").write_text("\n".join(manifest) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    bundle(Path(sys.argv[1]), Path(sys.argv[2]))
