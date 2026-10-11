#!/usr/bin/env python3
"""
SkillKorp Suite - Linux hidraw transport layer shared by both drivers.

Everything that touches /dev/hidraw* lives here so that both device drivers
get the same robustness guarantees:

- enumeration that parses sysfs properly (HID_ID bus/vendor/product, USB
  interface number, raw report descriptor) and sorts nodes numerically
  (hidraw10 after hidraw9, not after hidraw1);
- a minimal HID report-descriptor parser, used to locate the vendor
  configuration interface and to size feature reports exactly as the device
  declares them (this is what the Windows HID stack puts on the wire);
- an exclusive per-device lock (flock on the hidraw node itself) so the GUI,
  the tray and the daemon never interleave two multi-frame transactions;
- an identity check on every open (st_rdev of the opened fd must match the
  sysfs node we enumerated), which protects against hidraw renumbering when a
  device is unplugged and another one takes its minor number;
- retries on transient USB errors and typed exceptions for everything else.
"""

import errno
import fcntl
import glob
import os
import re
import select
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

SYSFS_HIDRAW = "/sys/class/hidraw"
DEV_DIR = "/dev"

BUS_USB = 0x03

UDEV_HINT = (
    "Installez la règle udev fournie (70-skillkorp-suite.rules) puis "
    "débranchez/rebranchez le périphérique."
)

# Transient errors worth retrying: the device stalled the control transfer
# (EPIPE), the 2.4GHz link dropped one frame (EIO/ETIMEDOUT/EPROTO) or the
# kernel asked us to try again (EAGAIN/EINTR/EBUSY).
_TRANSIENT_ERRNOS = {
    errno.EPIPE, errno.EIO, errno.ETIMEDOUT, errno.EAGAIN, errno.EINTR,
    errno.EBUSY, errno.EPROTO,
}
# The device went away: retrying cannot help.
_GONE_ERRNOS = {errno.ENODEV, errno.ENOENT, errno.ENXIO, errno.ESHUTDOWN}


class DeviceError(Exception):
    """Base class for every hardware communication failure."""


class DeviceNotFoundError(DeviceError):
    """The device is not plugged in (or disappeared mid-transaction)."""


class DevicePermissionError(DeviceError):
    """The hidraw node exists but the current user may not open it."""


class DeviceBusyError(DeviceError):
    """Another SkillKorp process kept the device locked for too long."""


class DeviceIOError(DeviceError):
    """The device rejected or did not acknowledge a report."""


def _ioc(direction: int, type_char: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(type_char) << 8) | nr


def HIDIOCSFEATURE(length: int) -> int:
    """_IOC(_IOC_READ|_IOC_WRITE, 'H', 0x06, len)"""
    return _ioc(3, "H", 0x06, length)


def HIDIOCGFEATURE(length: int) -> int:
    """_IOC(_IOC_READ|_IOC_WRITE, 'H', 0x07, len)"""
    return _ioc(3, "H", 0x07, length)


# =============================================================================
# HID report descriptor parsing
# =============================================================================

@dataclass
class ReportInfo:
    """Sizes (in bytes, *excluding* the report ID byte) of every report."""
    input: Dict[int, int] = field(default_factory=dict)
    output: Dict[int, int] = field(default_factory=dict)
    feature: Dict[int, int] = field(default_factory=dict)
    usage_pages: List[int] = field(default_factory=list)

    @property
    def uses_report_ids(self) -> bool:
        return any(rid != 0 for table in (self.input, self.output, self.feature) for rid in table)


def parse_report_descriptor(desc: bytes) -> ReportInfo:
    """Parse a HID report descriptor just enough to know report sizes.

    Handles short items, long items (skipped), Push/Pop of the global state
    and Report ID switching. Sizes accumulate per (type, report ID) and are
    rounded up to whole bytes, like the kernel does.
    """
    info = ReportInfo()
    bits: Dict[str, Dict[int, int]] = {"input": {}, "output": {}, "feature": {}}
    glob_state = {"report_size": 0, "report_count": 0, "report_id": 0}
    stack: List[dict] = []
    i = 0
    n = len(desc)
    while i < n:
        prefix = desc[i]
        if prefix == 0xFE:  # long item: 0xFE, size, tag, data...
            if i + 1 >= n:
                break
            i += 3 + desc[i + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        item_type = (prefix >> 2) & 0x03
        tag = (prefix >> 4) & 0x0F
        data = desc[i + 1:i + 1 + size]
        value = int.from_bytes(data, "little") if data else 0
        i += 1 + size

        if item_type == 1:  # Global
            if tag == 0x0:  # Usage Page
                if value not in info.usage_pages:
                    info.usage_pages.append(value)
            elif tag == 0x7:
                glob_state["report_size"] = value
            elif tag == 0x8:
                glob_state["report_id"] = value
            elif tag == 0x9:
                glob_state["report_count"] = value
            elif tag == 0xA:
                stack.append(dict(glob_state))
            elif tag == 0xB and stack:
                glob_state = stack.pop()
        elif item_type == 0:  # Main
            kind = {0x8: "input", 0x9: "output", 0xB: "feature"}.get(tag)
            if kind:
                rid = glob_state["report_id"]
                bits[kind][rid] = bits[kind].get(rid, 0) + \
                    glob_state["report_size"] * glob_state["report_count"]

    for kind, table in bits.items():
        sizes = {rid: (nbits + 7) // 8 for rid, nbits in table.items()}
        setattr(info, kind, sizes)
    return info


# =============================================================================
# Enumeration
# =============================================================================

@dataclass
class HidrawNode:
    name: str                    # "hidraw5"
    devnode: str                 # "/dev/hidraw5"
    bus: int
    vendor_id: int
    product_id: int
    interface: Optional[int]     # USB bInterfaceNumber when known
    descriptor: bytes
    dev_numbers: Optional[str]   # "major:minor" from sysfs, for identity checks
    hid_name: str = ""

    @property
    def reports(self) -> ReportInfo:
        cached = getattr(self, "_reports", None)
        if cached is None:
            cached = parse_report_descriptor(self.descriptor)
            self._reports = cached
        return cached


def _natural_key(path: str):
    m = re.search(r"(\d+)$", path)
    return (int(m.group(1)) if m else -1, path)


def parse_uevent(text: str) -> Dict[str, str]:
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse_hid_id(hid_id: str):
    """Parse 'HID_ID=0003:00003151:00004011' -> (bus, vid, pid)."""
    parts = hid_id.split(":")
    if len(parts) != 3:
        raise ValueError(f"HID_ID invalide: {hid_id!r}")
    return int(parts[0], 16), int(parts[1], 16), int(parts[2], 16)


def _read_interface_number(device_dir: str) -> Optional[int]:
    # <usb interface dir>/<hid device dir>: bInterfaceNumber lives one level up.
    path = os.path.join(os.path.dirname(device_dir), "bInterfaceNumber")
    try:
        with open(path, "r") as f:
            return int(f.read().strip(), 16)
    except (OSError, ValueError):
        return None


def list_hidraw_nodes(sysfs_root: str = SYSFS_HIDRAW, dev_dir: str = DEV_DIR) -> List[HidrawNode]:
    nodes: List[HidrawNode] = []
    for path in sorted(glob.glob(os.path.join(sysfs_root, "hidraw*")), key=_natural_key):
        name = os.path.basename(path)
        try:
            device_dir = os.path.realpath(os.path.join(path, "device"))
            with open(os.path.join(device_dir, "uevent"), "r", errors="replace") as f:
                uevent = parse_uevent(f.read())
            bus, vid, pid = parse_hid_id(uevent.get("HID_ID", ""))
            with open(os.path.join(device_dir, "report_descriptor"), "rb") as f:
                descriptor = f.read()
            dev_numbers = None
            try:
                with open(os.path.join(path, "dev"), "r") as f:
                    dev_numbers = f.read().strip()
            except OSError:
                pass
            nodes.append(HidrawNode(
                name=name,
                devnode=os.path.join(dev_dir, name),
                bus=bus,
                vendor_id=vid,
                product_id=pid,
                interface=_read_interface_number(device_dir),
                descriptor=descriptor,
                dev_numbers=dev_numbers,
                hid_name=uevent.get("HID_NAME", ""),
            ))
        except (OSError, ValueError):
            continue
    return nodes


def find_nodes(vendor_id: int, product_ids, predicate: Optional[Callable[[HidrawNode], bool]] = None,
               sysfs_root: str = SYSFS_HIDRAW, dev_dir: str = DEV_DIR) -> List[HidrawNode]:
    pids = set(product_ids)
    result = []
    for node in list_hidraw_nodes(sysfs_root, dev_dir):
        if node.vendor_id != vendor_id or node.product_id not in pids:
            continue
        if predicate is not None and not predicate(node):
            continue
        result.append(node)
    return result


def node_for_path(dev_path: str, sysfs_root: str = SYSFS_HIDRAW, dev_dir: str = DEV_DIR) -> Optional[HidrawNode]:
    name = os.path.basename(dev_path)
    for node in list_hidraw_nodes(sysfs_root, dev_dir):
        if node.name == name:
            return node
    return None


def usb_device_path(node: HidrawNode, sysfs_root: str = SYSFS_HIDRAW) -> Optional[str]:
    """/dev/bus/usb/BBB/DDD of the USB device a hidraw node belongs to."""
    path = os.path.realpath(os.path.join(sysfs_root, node.name, "device"))
    while path and path != "/":
        try:
            with open(os.path.join(path, "busnum")) as f:
                bus = int(f.read())
            with open(os.path.join(path, "devnum")) as f:
                dev = int(f.read())
            return f"/dev/bus/usb/{bus:03d}/{dev:03d}"
        except (OSError, ValueError):
            path = os.path.dirname(path)
    return None


# =============================================================================
# Device access
# =============================================================================

class HidrawDevice:
    """An opened, exclusively-locked hidraw node. Use as a context manager."""

    def __init__(self, node: HidrawNode, lock_timeout: float = 5.0,
                 exact_feature_sizes: bool = True, exclusive: bool = True):
        self.node = node
        self.exclusive = exclusive
        self.lock_timeout = lock_timeout
        self.exact_feature_sizes = exact_feature_sizes
        self.fd: Optional[int] = None

    # -- lifecycle -----------------------------------------------------------
    def open(self) -> "HidrawDevice":
        try:
            fd = os.open(self.node.devnode, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        except PermissionError as e:
            raise DevicePermissionError(
                f"Accès refusé à {self.node.devnode}. {UDEV_HINT}") from e
        except FileNotFoundError as e:
            raise DeviceNotFoundError(f"{self.node.devnode} a disparu.") from e
        except OSError as e:
            if e.errno in _GONE_ERRNOS:
                raise DeviceNotFoundError(f"{self.node.devnode} a disparu.") from e
            raise DeviceIOError(f"Ouverture de {self.node.devnode} impossible: {e}") from e
        try:
            self._check_identity(fd)
            if self.exclusive:
                self._lock(fd)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def close(self):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self.open() if self.fd is None else self

    def __exit__(self, *exc):
        self.close()
        return False

    def _check_identity(self, fd: int):
        if not self.node.dev_numbers:
            return
        st = os.fstat(fd)
        actual = f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
        if actual != self.node.dev_numbers:
            raise DeviceNotFoundError(
                f"{self.node.devnode} ne correspond plus au périphérique détecté "
                f"(renumérotation hidraw). Nouvelle détection nécessaire.")

    def _lock(self, fd: int):
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DeviceBusyError(
                        f"{self.node.devnode} est utilisé par un autre processus SkillKorp.")
                time.sleep(0.02)

    # -- helpers ---------------------------------------------------------------
    def _require_open(self) -> int:
        if self.fd is None:
            raise DeviceIOError("Périphérique non ouvert.")
        return self.fd

    def feature_length(self, report_id: int) -> Optional[int]:
        """Total buffer length (report ID byte included) declared for a feature report."""
        size = self.node.reports.feature.get(report_id)
        if size is None:
            return None
        return size + 1

    def fit_feature(self, data: bytes) -> bytearray:
        """Size a feature report buffer to what the descriptor declares.

        Trailing padding beyond the declared size is dropped (it is always
        zero in our frames); a buffer that would lose non-zero bytes is left
        untouched so we never silently corrupt a frame.
        """
        buf = bytearray(data)
        if not self.exact_feature_sizes or not buf:
            return buf
        expected = self.feature_length(buf[0])
        if expected is None:
            return buf
        if len(buf) < expected:
            buf.extend(b"\x00" * (expected - len(buf)))
        elif len(buf) > expected and not any(buf[expected:]):
            del buf[expected:]
        return buf

    # -- I/O -------------------------------------------------------------------
    def set_feature(self, data: bytes, retries: int = 3, retry_delay: float = 0.05) -> int:
        fd = self._require_open()
        buf = self.fit_feature(data)
        last_error: Optional[OSError] = None
        for attempt in range(max(1, retries)):
            try:
                res = fcntl.ioctl(fd, HIDIOCSFEATURE(len(buf)), bytearray(buf), True)
                if res != len(buf):
                    raise OSError(errno.EIO, f"écriture partielle ({res}/{len(buf)} octets)")
                return res
            except OSError as e:
                last_error = e
                if e.errno in _GONE_ERRNOS:
                    raise DeviceNotFoundError(f"{self.node.devnode} a été déconnecté.") from e
                if e.errno not in _TRANSIENT_ERRNOS:
                    break
            if attempt < retries - 1:
                time.sleep(retry_delay * (attempt + 1))
        raise DeviceIOError(
            f"Le périphérique n'a pas accepté le rapport 0x{buf[0]:02X} "
            f"({len(buf)} octets): {last_error}")

    def get_feature(self, report_id: int, length: Optional[int] = None, retries: int = 3,
                    retry_delay: float = 0.05) -> bytes:
        fd = self._require_open()
        if length is None:
            length = self.feature_length(report_id) or 65
        last_error: Optional[OSError] = None
        for attempt in range(max(1, retries)):
            buf = bytearray(length)
            buf[0] = report_id
            try:
                n = fcntl.ioctl(fd, HIDIOCGFEATURE(length), buf, True)
                if isinstance(n, int) and 0 < n < length:
                    return bytes(buf[:n])
                return bytes(buf)
            except OSError as e:
                last_error = e
                if e.errno in _GONE_ERRNOS:
                    raise DeviceNotFoundError(f"{self.node.devnode} a été déconnecté.") from e
                if e.errno not in _TRANSIENT_ERRNOS:
                    break
            if attempt < retries - 1:
                time.sleep(retry_delay * (attempt + 1))
        raise DeviceIOError(f"Lecture du rapport 0x{report_id:02X} impossible: {last_error}")

    def drain_input(self):
        """Discard input reports already queued on this fd."""
        fd = self._require_open()
        while True:
            try:
                if not os.read(fd, 256):
                    return
            except BlockingIOError:
                return
            except OSError:
                return

    def read_input(self, timeout: float, until: Optional[Callable[[bytes], bool]] = None,
                   max_reports: int = 64) -> List[bytes]:
        """Collect input reports for up to *timeout* seconds.

        Returns early as soon as *until(report)* is true for a report.
        """
        fd = self._require_open()
        reports: List[bytes] = []
        deadline = time.monotonic() + max(0.0, timeout)
        while len(reports) < max_reports:
            remaining = deadline - time.monotonic()
            if remaining < 0:
                break
            r, _, _ = select.select([fd], [], [], remaining)
            if not r:
                break
            try:
                data = os.read(fd, 256)
            except BlockingIOError:
                continue
            except OSError as e:
                if e.errno in _GONE_ERRNOS:
                    raise DeviceNotFoundError(f"{self.node.devnode} a été déconnecté.") from e
                break
            if not data:
                break
            reports.append(data)
            if until is not None and until(data):
                break
        return reports
