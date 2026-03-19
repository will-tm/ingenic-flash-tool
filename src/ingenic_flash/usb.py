"""Low-level USB communication with Ingenic SoCs in boot mode."""

import time
from typing import Optional

import usb.core
import usb.util

from .chips import INGENIC_VID, USB_PIDS

# USB request types
REQTYPE_OUT = 0x40  # Host-to-Device, Vendor, Device
REQTYPE_IN = 0xC0   # Device-to-Host, Vendor, Device

# Boot ROM vendor requests
VR_GET_CPU_INFO = 0x00
VR_SET_DATA_ADDRESS = 0x01
VR_SET_DATA_LENGTH = 0x02
VR_FLUSH_CACHES = 0x03
VR_PROGRAM_START1 = 0x04
VR_PROGRAM_START2 = 0x05

# Stage2 (cloner/burner) vendor requests
VR_GET_ACK = 0x10
VR_INIT = 0x11
VR_WRITE = 0x12
VR_READ = 0x13
VR_UPDATE_CFG = 0x14
VR_REBOOT = 0x16
VR_GET_FLASH_INFO = 0x26

# Bulk endpoints
EP_OUT = 0x01
EP_IN = 0x81

# Timeouts (ms)
CTRL_TIMEOUT = 5000
BULK_TIMEOUT = 30000


def _split_addr(addr: int) -> tuple[int, int]:
    """Split a 32-bit address into (wValue, wIndex) for control transfers."""
    return (addr >> 16) & 0xFFFF, addr & 0xFFFF


class USBDevice:
    """Wrapper around a pyusb device for Ingenic USB boot communication."""

    def __init__(self, dev: usb.core.Device):
        self._dev = dev
        self._claimed = False

    @property
    def pid(self) -> int:
        return self._dev.idProduct

    @property
    def pid_name(self) -> str:
        return USB_PIDS.get(self._dev.idProduct, f"0x{self._dev.idProduct:04x}")

    def open(self) -> None:
        if self._claimed:
            return
        try:
            if self._dev.is_kernel_driver_active(0):
                self._dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass
        try:
            self._dev.set_configuration()
        except usb.core.USBError:
            pass
        try:
            cfg = self._dev.get_active_configuration()
            intf = cfg[(0, 0)]
            usb.util.claim_interface(self._dev, intf)
        except usb.core.USBError:
            pass
        self._claimed = True

    def close(self) -> None:
        if self._claimed:
            usb.util.dispose_resources(self._dev)
            self._claimed = False

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    # --- Boot ROM vendor requests ---

    def get_cpu_info(self) -> bytes:
        return bytes(self._dev.ctrl_transfer(
            REQTYPE_IN, VR_GET_CPU_INFO, 0, 0, 8, timeout=CTRL_TIMEOUT))

    def set_data_address(self, addr: int) -> None:
        wval, widx = _split_addr(addr)
        self._dev.ctrl_transfer(
            REQTYPE_OUT, VR_SET_DATA_ADDRESS, wval, widx, b"", timeout=CTRL_TIMEOUT)

    def set_data_length(self, length: int) -> None:
        wval, widx = _split_addr(length)
        self._dev.ctrl_transfer(
            REQTYPE_OUT, VR_SET_DATA_LENGTH, wval, widx, b"", timeout=CTRL_TIMEOUT)

    def flush_caches(self) -> None:
        self._dev.ctrl_transfer(
            REQTYPE_OUT, VR_FLUSH_CACHES, 0, 0, b"", timeout=CTRL_TIMEOUT)

    def program_start1(self, entry: int) -> None:
        wval, widx = _split_addr(entry)
        self._dev.ctrl_transfer(
            REQTYPE_OUT, VR_PROGRAM_START1, wval, widx, b"", timeout=CTRL_TIMEOUT)

    def program_start2(self, entry: int) -> None:
        wval, widx = _split_addr(entry)
        self._dev.ctrl_transfer(
            REQTYPE_OUT, VR_PROGRAM_START2, wval, widx, b"", timeout=CTRL_TIMEOUT)

    # --- Bulk transfers ---

    def bulk_write(self, data: bytes | bytearray, chunk_size: int = 65536) -> None:
        offset = 0
        while offset < len(data):
            end = min(offset + chunk_size, len(data))
            written = self._dev.write(EP_OUT, data[offset:end], timeout=BULK_TIMEOUT)
            if written <= 0:
                raise IOError(f"Bulk write failed at offset {offset}")
            offset += written

    def bulk_read(self, length: int, chunk_size: int = 65536) -> bytearray:
        buf = bytearray()
        while len(buf) < length:
            to_read = min(chunk_size, length - len(buf))
            chunk = self._dev.read(EP_IN, to_read, timeout=BULK_TIMEOUT)
            buf.extend(chunk)
        return buf

    # --- Stage2 (burner) vendor requests ---

    def stage2_get_ack(self) -> bytes:
        return bytes(self._dev.ctrl_transfer(
            REQTYPE_IN, VR_GET_ACK, 0, 0, 4, timeout=CTRL_TIMEOUT))

    def stage2_update_cfg(self, ep0_data: bytes, bulk_data: bytes) -> None:
        self._dev.ctrl_transfer(
            REQTYPE_OUT, VR_UPDATE_CFG, 0, 0, ep0_data, timeout=CTRL_TIMEOUT)
        self.bulk_write(bulk_data)

    def stage2_get_flash_info(self) -> bytes:
        return bytes(self._dev.ctrl_transfer(
            REQTYPE_IN, VR_GET_FLASH_INFO, 0, 0, 3, timeout=CTRL_TIMEOUT))

    def stage2_reboot(self) -> None:
        try:
            self._dev.ctrl_transfer(
                REQTYPE_OUT, VR_REBOOT, 0, 0, b"", timeout=CTRL_TIMEOUT)
        except Exception:
            pass  # device reboots immediately


def find_device(vid: int = INGENIC_VID, pid: Optional[int] = None) -> Optional[USBDevice]:
    """Find an Ingenic device in USB boot mode."""
    if pid is not None:
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        return USBDevice(dev) if dev else None
    for known_pid in USB_PIDS:
        dev = usb.core.find(idVendor=vid, idProduct=known_pid)
        if dev is not None:
            return USBDevice(dev)
    return None
