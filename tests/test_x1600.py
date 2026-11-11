"""Offline checks for the bundled native X1600EN SPI NOR profile."""

import configparser
from dataclasses import replace
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

from ingenic_flash import protocol, usb, x1600
from ingenic_flash.chips import chip_by_name
from ingenic_flash.spiflash_db import lookup_chip


FW = protocol.find_firmware_dir("x1600")


def image():
    data = bytearray(x1600.FLASH_SIZE)
    struct.pack_into("<I", data, 128, x1600.DDR_ID)
    return data


def records(data):
    offset = 0
    result = {}
    while offset + 8 <= len(data):
        magic, size = struct.unpack_from("<II", data, offset)
        if magic == 0:
            break
        end = offset + 8 + size
        if end > len(data):
            raise AssertionError("TLV exceeds payload")
        result[magic] = data[offset + 8:end]
        offset = end
    return result


class X1600AssetTests(unittest.TestCase):
    def test_manifest_and_all_required_assets_are_present(self):
        manifest = configparser.ConfigParser()
        manifest.read(FW / "provenance.cfg")
        self.assertEqual(manifest["source"]["package"], "cloner-2.5.58.2-ubuntu_alpha")
        for name, digest in manifest["sha256"].items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((FW / name).read_bytes()).hexdigest(), digest)
        for phase in (1, 2):
            bulk = (FW / f"cfg{phase}_bulk.bin").read_bytes()
            control = (FW / f"cfg{phase}_ep0.bin").read_bytes()
            self.assertEqual(control, x1600.update_command(bulk))
            self.assertEqual(len(control), 40)

    def test_ginfo_uses_the_vendor_ddr_record_and_correct_gpio(self):
        spl = (FW / "spl.bin").read_bytes()
        start = spl.index(b"X1600EN-DDR2\x00")
        ddr = spl[start:start + 168]
        ginfo = (FW / "ginfo.bin").read_bytes()
        self.assertEqual(ginfo, x1600.make_ginfo(ddr))
        self.assertLess(len(ginfo), 0x800)
        values = records(ginfo)
        self.assertEqual(values[0x44455445], bytes(4))
        self.assertEqual(values[0x44445200], ddr)
        board = values[0x42444946]
        self.assertEqual(len(board), 184)
        self.assertEqual(struct.unpack_from("<6I", board),
                         (24000000, 800000000, 350000000, 0, 2, 115200))
        self.assertEqual(struct.unpack_from("<7I", board, 24),
                         (1, 1, 1, 1, 2, 0x10, 0x007E0000))
        self.assertEqual(ginfo[-32:], bytes(32))

    def test_ginfo_rejects_wrong_geometry_or_format(self):
        spl = (FW / "spl.bin").read_bytes()
        start = spl.index(b"X1600EN-DDR2\x00")
        valid = spl[start:start + 168]
        for offset in (32, 36, 40, 140):
            bad = bytearray(valid)
            struct.pack_into("<I", bad, offset, 0)
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                x1600.make_ginfo(bad)
        with self.assertRaises(ValueError):
            x1600.make_ginfo(valid[:-1])

    def test_t33_assets_are_unchanged(self):
        directory = protocol.find_firmware_dir("prj008")
        hashes = {
            "ginfo.bin": "ee7484f1f5f02cce64c2fc7990345dcf0093234fc2eeb47539e051dfd93c8eb1",
            "cfg2_bulk.bin": "880d0afa53223b655d53c32fbd00d647862dd63f83b96993ef2b99879968cb31",
        }
        for name, digest in hashes.items():
            self.assertEqual(hashlib.sha256((directory / name).read_bytes()).hexdigest(), digest)


class X1600ConfigTests(unittest.TestCase):
    def setUp(self):
        self.chip = lookup_chip(0x0B4018)

    def test_cfg1_enables_only_sfc_nor(self):
        policy = records(x1600.make_cfg1())[0x504F4C49]
        self.assertEqual(struct.unpack("<41I", policy), (0,) * 5 + (1,) + (0,) * 35)

    def test_cfg2_matches_vendor_structure_sizes_and_alignment(self):
        data = x1600.make_cfg2(self.chip)
        self.assertEqual(data, (FW / "cfg2_bulk.bin").read_bytes())
        self.assertEqual(len(data), 980)
        values = records(data)
        self.assertEqual(struct.unpack("<5I", values[0x44424700]), (1, 1, 1, 65536, 200))
        sfc = values[0x53464300]
        self.assertEqual(len(sfc), 772)
        self.assertEqual(struct.unpack_from("<8I", sfc),
                         (0, 0, 32768, 0, 50000000, 0, 0, 0x5800))
        self.assertEqual(struct.unpack_from("<II", sfc, 32), (0x00726F6E, 2))
        full = sfc[40:180]
        self.assertEqual(full[:32].rstrip(b"\x00"), b"XT25F128F-W")
        self.assertEqual(struct.unpack_from("<I", full, 32)[0], 0x0B4018)
        self.assertEqual(struct.unpack_from("<HH", full, 102), (1, 0))
        self.assertEqual(full[106:108], bytes(2))
        self.assertEqual(struct.unpack_from("<4I3IB", full, 108),
                         (5, 5, 20, 20, x1600.FLASH_SIZE, 256, 32768, 0x60))
        partition = sfc[180:664]
        self.assertEqual(struct.unpack_from("<32s4I", partition),
                         (b"full".ljust(32, b"\x00"), x1600.FLASH_SIZE, 0, 0, 2))
        self.assertEqual(struct.unpack_from("<I", partition, 480)[0], 1)
        mini = sfc[672:]
        self.assertEqual(len(mini), 100)
        self.assertEqual(mini[:36], full[:36])
        self.assertEqual(struct.unpack_from("<3I", mini, 88), (x1600.FLASH_SIZE, 256, 32768))

    def test_full_erase_is_explicit_and_never_enables_parameter_injection(self):
        sfc = records(x1600.make_cfg2(self.chip, erase_all=True))[0x53464300]
        self.assertEqual(struct.unpack_from("<I", sfc)[0], 0)
        self.assertEqual(struct.unpack_from("<I", sfc, 12)[0], 1)

    def test_unsupported_flash_geometry_is_rejected(self):
        for chip in (replace(self.chip, size=32 * 1024 * 1024),
                     replace(self.chip, erase_size=131072),
                     replace(self.chip, erase_size=0)):
            with self.subTest(chip=chip), self.assertRaises(ValueError):
                x1600.make_cfg2(chip)

    def test_image_profile_checks(self):
        valid = image()
        x1600.validate_image(valid)
        for data, offset in ((valid[:-1], 0), (valid, 65536), (bytes(len(valid)), 0)):
            with self.subTest(size=len(data), offset=offset), self.assertRaises(ValueError):
                x1600.validate_image(data, offset)


class X1600ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "full.bin"
        self.path.write_bytes(image())
        self.dev = Mock()
        self.dev.stage2_get_ack.return_value = bytes(4)
        self.dev.stage2_get_flash_info.return_value = bytes.fromhex("18400b")
        self.dev._dev.ctrl_transfer.return_value = bytes(4)

    @patch.object(protocol, "time")
    @patch.object(protocol, "boot_device")
    def test_native_flash_uses_new_sfc_layout_and_preserves_image(self, boot, time):
        boot.return_value = self.dev
        with patch("ingenic_flash.spiflash_db.patch_cfg2") as old_patcher:
            protocol.flash_firmware(self.dev, chip_by_name("x1600"), self.path, FW, reboot=False)
        old_patcher.assert_not_called()
        configs = self.dev.stage2_update_cfg.call_args_list
        self.assertEqual(len(configs), 2)
        self.assertEqual(configs[1].args[1], x1600.make_cfg2(lookup_chip(0x0B4018)))
        written = b"".join(call.args[0] for call in self.dev.bulk_write.call_args_list)
        self.assertEqual(written, self.path.read_bytes())
        self.dev.stage2_reboot.assert_not_called()

    @patch.object(protocol, "boot_device")
    def test_invalid_image_is_rejected_before_boot(self, boot):
        self.path.write_bytes(bytes(128))
        with self.assertRaises(ValueError):
            protocol.flash_firmware(self.dev, chip_by_name("x1600"), self.path, FW)
        boot.assert_not_called()
        self.dev.stage2_update_cfg.assert_not_called()

    @patch.object(protocol, "boot_device")
    def test_unknown_jedec_never_initializes_or_writes_flash(self, boot):
        boot.return_value = self.dev
        self.dev.stage2_get_flash_info.return_value = bytes.fromhex("abcdef")
        with self.assertRaisesRegex(RuntimeError, "refusing to erase"):
            protocol.flash_firmware(self.dev, chip_by_name("x1600"), self.path, FW)
        self.dev._dev.ctrl_transfer.assert_not_called()
        self.dev.bulk_write.assert_not_called()

    @patch.object(protocol, "boot_device")
    def test_oversized_live_chip_is_rejected_before_init(self, boot):
        boot.return_value = self.dev
        self.dev.stage2_get_flash_info.return_value = bytes.fromhex("1940ef")
        with self.assertRaisesRegex(ValueError, "16 MiB"):
            protocol.flash_firmware(self.dev, chip_by_name("x1600"), self.path, FW)
        self.dev._dev.ctrl_transfer.assert_not_called()
        self.dev.bulk_write.assert_not_called()

    @patch.object(protocol, "time")
    @patch.object(protocol, "boot_device")
    def test_t33_keeps_its_existing_patcher_and_control_packet(self, boot, time):
        boot.return_value = self.dev
        self.path.write_bytes(bytes(65536))
        self.dev.stage2_get_flash_info.return_value = bytes.fromhex("1870ef")
        directory = protocol.find_firmware_dir("prj008")
        from ingenic_flash.spiflash_db import patch_cfg2
        with patch("ingenic_flash.spiflash_db.patch_cfg2", wraps=patch_cfg2) as old_patcher:
            protocol.flash_firmware(self.dev, chip_by_name("prj008"), self.path, directory, reboot=False)
        old_patcher.assert_called_once()
        self.assertEqual(self.dev.stage2_update_cfg.call_args_list[1].args[0],
                         (directory / "cfg2_ep0.bin").read_bytes())

    def test_vendor_spl_requires_32_byte_info_and_ddr_release_before_stage2(self):
        controller = Mock()
        state = ["rom"]
        writes = []
        info_lengths = []

        def control(kind, request, value, index, data, timeout):
            if request == usb.VR_GET_CPU_INFO:
                info_lengths.append(data)
                if state[0] == "spl":
                    if data < 32:
                        raise usb.usb.core.USBError("Overflow", errno=75)
                    return b"X1600EN".ljust(32, b"\x00")
                return b"PRJ\x00\x00\x00\x00\x00" if state[0] == "burner" else b"X1600"
            if request == usb.VR_PROGRAM_START1:
                state[0] = "spl"
            elif request == usb.VR_START_DDR:
                self.assertEqual(state[0], "spl")
                state[0] = "ddr-ready"
            elif request == usb.VR_PROGRAM_START2:
                self.assertEqual(state[0], "ddr-ready")
                state[0] = "burner"
            return 0

        def write(endpoint, data, timeout):
            self.assertNotEqual(state[0], "spl", "DRAM must not be written before DDR init")
            writes.append((state[0], bytes(data)))
            return len(data)

        controller.ctrl_transfer.side_effect = control
        controller.write.side_effect = write
        dev = usb.USBDevice(controller)
        protocol.boot_device(dev, chip_by_name("x1600"),
                             FW / "ginfo.bin", FW / "spl.bin", FW / "uboot.bin")
        self.assertEqual(info_lengths, [8, 32, 8, 8])
        stage2 = b"".join(data for phase, data in writes if phase == "ddr-ready")
        self.assertEqual(stage2, (FW / "uboot.bin").read_bytes())

    def test_wrong_cpu_never_receives_ram_writes(self):
        self.dev.get_cpu_info.return_value = b"T33\x00"
        with self.assertRaisesRegex(RuntimeError, "requires an X1600"):
            protocol.boot_device(self.dev, chip_by_name("x1600"),
                                 FW / "ginfo.bin", FW / "spl.bin", FW / "uboot.bin")
        self.dev.bulk_write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
