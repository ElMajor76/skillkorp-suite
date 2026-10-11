"""
Test doubles: a fake hidraw device that records every frame and answers
GET_FEATURE / input-report reads, plus helpers to isolate the configuration
directories in a temporary HOME. No real /dev/hidraw* node is ever touched.
"""

import contextlib
import os
import tempfile
from typing import Callable, List, Optional
from unittest.mock import patch

from skillkorp.devices import hidraw
from skillkorp.devices.hidraw import DeviceIOError, HidrawNode

# Real report descriptors captured from the hardware (hidraw13 / hidraw5).
K20_VENDOR_DESCRIPTOR = bytes.fromhex("06ffff0902a10109021580257f95407508b102c0")
M20_VENDOR_DESCRIPTOR = bytes.fromhex(
    "05010980a101850105011981298315002501950375018106950175058101c0050c0901a1018502150026800319002a8003751095"
    "018100c0050a0900a10185031900290015002500750895048101c0050b0900a10185049533b1018505950cb10185069508b10185"
    "079507b1018508953ab1018509953fb101850a9507b101850b9507b101850c9505b10185109507b10185a09507b10185229583b1"
    "0185239583b10185249507b1018525950cb10185269508b10185279507b10185289583b10185299580b101852a9583b101852b95"
    "07b101852c9503b101852d9583b10185239582b101852e9504b101c0")


def make_node(vendor_id: int, product_id: int, descriptor: bytes, name: str = "hidraw99") -> HidrawNode:
    return HidrawNode(name=name, devnode=f"/dev/{name}", bus=3, vendor_id=vendor_id, product_id=product_id,
                      interface=2, descriptor=descriptor, dev_numbers=None)


class FakeHidrawDevice(hidraw.HidrawDevice):
    """Drop-in replacement for HidrawDevice that never touches the OS."""

    def __init__(self, node: HidrawNode, owner: "FakeHardware", **kwargs):
        super().__init__(node, **kwargs)
        self.owner = owner

    def open(self):
        self.owner.opens += 1
        self.fd = -1
        return self

    def close(self):
        self.fd = None

    def set_feature(self, data, retries=3, retry_delay=0.05):
        buf = bytes(self.fit_feature(data))
        if self.owner.fail_writes:
            self.owner.fail_writes -= 1
            raise DeviceIOError("refus simulé")
        self.owner.writes.append(buf)
        if self.owner.is_receiver:
            self.owner.rf_write(buf)
        if self.owner.auto_ack and buf[0] != 0:  # mouse receiver acknowledgement
            self.owner.input_queue.append(bytes([3, 0x10, 0x50, 0, buf[0]]))
        if self.owner.on_write:
            self.owner.on_write(buf)
        return len(buf)

    def get_feature(self, report_id, length=None, retries=3, retry_delay=0.05):
        self.owner.reads.append((report_id, length))
        answer = self.owner.feature_answer(bytes(self.owner.writes[-1]) if self.owner.writes else b"")
        length = length or len(answer)
        return bytes(answer[:length]).ljust(length, b"\x00")

    def drain_input(self):
        pass

    def read_input(self, timeout, until=None, max_reports=64):
        out = []
        while self.owner.input_queue and len(out) < max_reports:
            rep = self.owner.input_queue.pop(0)
            out.append(rep)
            if until and until(rep):
                break
        return out


class FakeHardware:
    """Patches a driver class so it sees one fake device."""

    def __init__(self, driver_cls, node: HidrawNode):
        self.driver_cls = driver_cls
        self.node = node
        self.connected = True
        self.writes: List[bytes] = []
        self.reads: List[tuple] = []
        self.input_queue: List[bytes] = []
        self.opens = 0
        self.fail_writes = 0
        self.auto_ack = True
        self.on_write: Optional[Callable[[bytes], None]] = None
        self.answers = {}
        # 2.4GHz keyboard receiver (3151:4011), modelled on USB captures:
        # F7 -> status [answer ready, battery %, 0, offline, 1, 1, 1, ...],
        # a query frame makes the keyboard's answer pending, FC fetches it.
        self.rf_status = bytes([0x00, 0x54, 0x00, 0x00, 0x01, 0x01, 0x01, 0x90, 0x06])
        self.rf_pending: Optional[bytes] = None
        self.rf_buffer = b""
        self.rf_keyboard_answers = True
        self.rf_lose_writes = 0  # next N keyboard writes lost over the air
        self.rf_ignore_queries = 0  # next N GET commands left unanswered
        self.rf_delivery_polls = 0  # F7 polls during which a frame stays pending
        self._rf_undelivered = 0
        self.rf_overwritten = 0     # frames replaced before delivery (lost)

    @property
    def is_receiver(self) -> bool:
        return self.node.vendor_id == 0x3151 and self.node.product_id == 0x4011

    def rf_write(self, frame: bytes) -> None:
        cmd = frame[1] if len(frame) > 1 else 0
        if cmd == 0xF7:
            status = bytearray(self.rf_status)
            if status:
                status[0] = 1 if self.rf_pending is not None else 0
            if len(status) > 5:
                status[5] = 0 if self._rf_undelivered else 1
            if self._rf_undelivered:
                self._rf_undelivered -= 1
            self.rf_buffer = bytes(status)
            return
        if cmd == 0xFC:
            if self.rf_pending is not None:
                self.rf_buffer, self.rf_pending = self.rf_pending, None
            return
        if cmd in (0xFE, 0xF6):
            return
        # A frame for the keyboard: the receiver holds it until delivery.
        if self._rf_undelivered:
            self.rf_overwritten += 1  # the real receiver would lose the previous frame
        self._rf_undelivered = self.rf_delivery_polls
        if cmd & 0x80:
            if self.rf_ignore_queries:
                self.rf_ignore_queries -= 1
            elif self.rf_keyboard_answers:
                self.rf_pending = self.answers.get(cmd, bytes([cmd]))
        elif self.rf_lose_writes:
            self.rf_lose_writes -= 1
        else:
            self.keyboard_applies(frame[1:])

    def keyboard_applies(self, p: bytes) -> None:
        """The keyboard's state as later read back (same layouts as the K20)."""
        state = {0x07: lambda: bytes([0x87]) + p[1:8], 0x08: lambda: bytes([0x88]) + p[1:8],
                 0x04: lambda: bytes([0x84, p[1], p[2]]), 0x05: lambda: bytes([0x85, p[1]]),
                 0x06: lambda: bytes([0x86]) + p[1:5], 0x11: lambda: bytes([0x91, 0, p[2]]),
                 0x12: lambda: bytes([0x92]) + p[8:16]}
        if p[0] in state:
            self.answers[p[0] | 0x80] = state[p[0]]()

    def feature_answer(self, last_frame: bytes) -> bytes:
        """Answer to a GET_FEATURE: payload registered for the last command byte."""
        if self.is_receiver:
            return b"\x00" + bytes(self.rf_buffer).ljust(64, b"\x00")
        cmd = last_frame[1] if len(last_frame) > 1 else 0
        payload = self.answers.get(cmd, bytes([cmd]))
        return b"\x00" + payload.ljust(64, b"\x00")

    def payloads(self, skip_prefix: bool = True) -> List[bytes]:
        """Keyboard payloads (report ID stripped), receiver commands removed;
        over the receiver, the GET frames used to verify writes too."""
        out = []
        for w in self.writes:
            p = w[1:]
            if skip_prefix and p[0] in (0xFE, 0xF6, 0xF7, 0xFC):
                continue
            if skip_prefix and self.is_receiver and p[0] & 0x80:
                continue
            out.append(p)
        return out

    @contextlib.contextmanager
    def installed(self):
        hw = self

        def find_nodes(cls=None):
            return [hw.node] if hw.connected else []

        def open_device(drv, exclusive=True):
            if not hw.connected:
                raise hidraw.DeviceNotFoundError(f"{drv.display_name} non détecté(e).")
            return FakeHidrawDevice(hw.node, hw).open()

        with patch.object(self.driver_cls, "find_nodes", classmethod(lambda cls: find_nodes())), \
                patch.object(self.driver_cls, "open_device", open_device), \
                patch("time.sleep", lambda s: None):
            yield self


@contextlib.contextmanager
def isolated_home():
    """Point every SkillKorp config path to a fresh temporary directory."""
    import skillkorp.devices.keyboard as kb
    import skillkorp.devices.mouse as ms
    import skillkorp.profiles.manager as pm
    import skillkorp.profiles.migration as mig
    with tempfile.TemporaryDirectory() as tmp:
        cfg = os.path.join(tmp, ".config", "skillkorp")
        with patch.object(kb, "CONFIG_DIR", os.path.join(cfg, "keyboard")), \
                patch.object(kb, "DEFAULT_PROFILE_FILE", os.path.join(cfg, "keyboard", "default_profile.json")), \
                patch.object(ms, "CONFIG_DIR", os.path.join(cfg, "mouse")), \
                patch.object(ms, "DEFAULT_PROFILE_FILE", os.path.join(cfg, "mouse", "default_profile.json")), \
                patch.object(pm, "CONFIG_DIR", cfg), \
                patch.object(pm, "AUTOSTART_DIR", os.path.join(tmp, ".config", "autostart")), \
                patch.object(pm, "AUTOSTART_FILE", os.path.join(tmp, ".config", "autostart", "x.desktop")), \
                patch.dict(os.environ, {"XDG_CONFIG_DIRS": os.path.join(tmp, "xdg"),
                                        "SKILLKORP_NO_TRAY": "1"}), \
                patch.object(mig, "CONFIG_DIR", cfg), \
                patch.object(mig, "MARKER_FILE", os.path.join(cfg, ".migrated")), \
                patch.object(mig, "_LEGACY_SOURCES", {"keyboard": os.path.join(tmp, "k20"),
                                                      "mouse": os.path.join(tmp, "m20")}):
            yield tmp
