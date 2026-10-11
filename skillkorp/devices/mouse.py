#!/usr/bin/env python3
"""
SkillKorp M20 Ultimate - Linux Hardware Driver & Controller

Protocol reference: the official MU_SkillKorp.exe (XinMaiGao / Beken BK3633 +
PixArt PAW3395, VID 0x1D57, PID 0xFA60 receiver / 0xFA61 USB-C). Frame
builders were checked against the vendor code; see AUDIT.md for addresses.

Vendor configuration interface: Usage Page 0x000B, feature reports
  0x04 DPI & sensor (LOD, ripple, angle snap, motion sync, 1..8 stages,
       per-stage LED colours, 16-bit big-endian checksum of bytes 3..49)
  0x05 lighting + power + debounce (effect, speed, brightness, colour,
       sleep timer, LED-off timer, key debounce; checksum of bytes 3..10)
  0x06 polling rate (interval, ~interval)
  0x08 button mapping (18 slots of 3 bytes, checksum of bytes 3..56)
  0x0C status query, answered by input report 0x03 (battery / DPI stage).
Buffers are built with the vendor's lengths and trimmed to the size declared
by the device's report descriptor (what the Windows HID stack sends).

The "move to wake" switch of older versions of this driver does not exist on
this mouse (the vendor hides that option for the M20) and is ignored.
"""

import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from .base import SkillkorpDeviceDriver
from . import hidraw
from .hidraw import DeviceBusyError, DeviceError, DeviceNotFoundError, DevicePermissionError, HidrawNode

CONFIG_DIR = os.path.expanduser("~/.config/skillkorp/mouse")
DEFAULT_PROFILE_FILE = os.path.join(CONFIG_DIR, "default_profile.json")

VENDOR_ID = 0x1D57
PID_WIRELESS = 0xFA60
PID_WIRED = 0xFA61
USAGE_PAGE_VENDOR = 0x000B

REPORT_DPI = 0x04
REPORT_LIGHT_POWER = 0x05
REPORT_POLLING = 0x06
REPORT_BUTTONS = 0x08
REPORT_QUERY = 0x0C
INPUT_REPORT_STATUS = 0x03

# Polling Rate mappings: Hz -> interval in ms and its complement
POLLING_RATE_MAP = {
    125: {"interval": 0x08, "not_interval": 0xF7},
    250: {"interval": 0x04, "not_interval": 0xFB},
    500: {"interval": 0x02, "not_interval": 0xFD},
    1000: {"interval": 0x01, "not_interval": 0xFE},
}
INTERVAL_TO_HZ = {0x08: 125, 0x04: 250, 0x02: 500, 0x01: 1000}

# RGB lighting effects: the code is the index in the vendor combo box
# (ms_1_lightmode1..12 -> 0..11). The vendor only sends the brightness for
# the two static effects (codes 1 and 9), which confirms this numbering.
LIGHT_MODES = [
    ("off", "Éteint (Off)", 0),
    ("static", "Fixe (Static)", 1),
    ("breathing", "Respiration (Breathing)", 2),
    ("neon", "Néon (Neon)", 3),
    ("color_breathing", "Respiration couleur (Color Breathing)", 4),
    ("color_static", "Fixe couleur (Color Static)", 5),
    ("mixed_breathing", "Respiration mixte (Mixed Breathing)", 6),
    ("rainbow_wave", "Onde arc-en-ciel (Rainbow Wave)", 7),
    ("lightning", "Éclair (Lightning)", 8),
    ("static_mixed", "Couleur mixte fixe (Static Mixed Color)", 9),
    ("marquee_1", "Défilement 1 (Marquee 1)", 10),
    ("marquee_2", "Défilement 2 (Marquee 2)", 11),
]
LIGHT_CODE_BY_KEY = {key: code for key, _, code in LIGHT_MODES}
STATIC_LIGHT_CODES = (1, 9)

# Ranges of the vendor sliders (ms_1_page.xml)
BRIGHTNESS_RANGE = (1, 8)
SPEED_RANGE = (4, 8)
SLEEP_RANGE = (1, 60)       # minutes before deep sleep
LED_OFF_RANGE = (1, 60)     # minutes before the LEDs switch off
DEBOUNCE_RANGE = (2, 25)    # ms
DPI_RANGE = (50, 26000)
DPI_STEP = 50
MAX_STAGES = 8

# Exact hardware firmware button action codes from MU_SkillKorp.exe table at 0x41c450
BUTTON_ACTIONS = {
    # Basic Mouse Actions
    "left_click": ("Clic Gauche", 0x02, 0x00, 0x00),
    "right_click": ("Clic Droit", 0x03, 0x00, 0x00),
    "middle_click": ("Clic Molette", 0x04, 0x00, 0x00),
    "forward": ("Suivant / Avant", 0x06, 0x00, 0x00),
    "backward": ("Précédent / Arrière", 0x05, 0x00, 0x00),
    "double_click": ("Double Clic", 0x07, 0x00, 0x00),
    "fire_button": ("Tir Rapide (Rapid Fire)", 0x08, 0x00, 0x00),
    "scroll_up": ("Défilement Molette Haut", 0x09, 0x00, 0x00),
    "scroll_down": ("Défilement Molette Bas", 0x0A, 0x00, 0x00),

    # DPI Actions
    "dpi_cycle": ("Cycle DPI", 0x0D, 0x00, 0x00),
    "dpi_up": ("DPI +", 0x0E, 0x00, 0x00),
    "dpi_down": ("DPI -", 0x0F, 0x00, 0x00),

    # Multimedia (Consumer Control)
    "media_player": ("Lecteur Multimédia", 0x15, 0x00, 0x00),
    "media_play_pause": ("Lecture / Pause", 0x18, 0x00, 0x00),
    "media_stop": ("Arrêt Média", 0x19, 0x00, 0x00),
    "media_prev": ("Piste Précédente", 0x16, 0x00, 0x00),
    "media_next": ("Piste Suivante", 0x17, 0x00, 0x00),
    "media_vol_up": ("Volume +", 0x1B, 0x00, 0x00),
    "media_vol_down": ("Volume -", 0x1C, 0x00, 0x00),
    "media_mute": ("Muet", 0x1A, 0x00, 0x00),

    # Web Browser
    "browser_home": ("Accueil Navigateur", 0x25, 0x00, 0x00),
    "browser_favorites": ("Favoris (Ctrl+Shift+O)", 0x11, 0x03, 0x12),
    "browser_forward": ("Page Suivante", 0x20, 0x00, 0x00),
    "browser_backward": ("Page Précédente", 0x21, 0x00, 0x00),
    "browser_stop": ("Arrêter Chargement", 0x22, 0x00, 0x00),
    "browser_refresh": ("Actualiser Page", 0x24, 0x00, 0x00),
    "browser_search": ("Recherche Web", 0x26, 0x00, 0x00),

    # Office & System Shortcuts
    "copy": ("Copier (Ctrl+C)", 0x11, 0x01, 0x06),
    "paste": ("Coller (Ctrl+V)", 0x11, 0x01, 0x19),
    "cut": ("Couper (Ctrl+X)", 0x11, 0x01, 0x1B),
    # Shortcuts are HID usages translated by the OS layout: on AZERTY the "A"
    # key is usage 0x14 and "Z" is 0x1A (0x04 / 0x1D would give Ctrl+Q / Ctrl+W).
    "select_all": ("Tout Sélectionner (Ctrl+A)", 0x11, 0x01, 0x14),
    "select_all_qwerty": ("Tout Sélectionner (Ctrl+A, OS en QWERTY)", 0x11, 0x01, 0x04),
    "save": ("Enregistrer (Ctrl+S)", 0x11, 0x01, 0x16),
    "find": ("Rechercher (Ctrl+F)", 0x11, 0x01, 0x09),
    "undo": ("Annuler (Ctrl+Z)", 0x11, 0x01, 0x1A),
    "undo_qwerty": ("Annuler (Ctrl+Z, OS en QWERTY)", 0x11, 0x01, 0x1D),
    "redo": ("Rétablir (Ctrl+Y)", 0x11, 0x01, 0x1C),
    "close_window": ("Fermer (Alt+F4)", 0x11, 0x04, 0x3D),
    "show_desktop": ("Afficher Bureau (Win+D)", 0x11, 0x08, 0x07),
    "lock_pc": ("Verrouiller (Win+L)", 0x11, 0x08, 0x0F),
    "calculator": ("Calculatrice", 0x1D, 0x00, 0x00),
    "my_computer": ("Poste de Travail", 0x23, 0x00, 0x00),
    "email": ("Messagerie (Email)", 0x1E, 0x00, 0x00),

    # Gaming & Special
    "easy_aim": ("Visée Sniper", 0x10, 0x00, 0x03),
    "disabled": ("Désactivé", 0x01, 0x00, 0x00),
}


# Physical mouse buttons to 18-slot indices mapping
PHYSICAL_BTN_TO_SLOT = {
    1: 0,  # Bouton 1: Clic Gauche -> Slot 0
    2: 1,  # Bouton 2: Clic Droit -> Slot 1
    3: 2,  # Bouton 3: Clic Molette -> Slot 2
    4: 6,  # Bouton 4: Latéral Avant -> Slot 6
    5: 7,  # Bouton 5: Latéral Arrière -> Slot 7
    6: 3,  # Bouton 6: Bouton DPI -> Slot 3
}
FACTORY_BUTTONS = {
    1: "left_click",
    2: "right_click",
    3: "middle_click",
    4: "forward",
    5: "backward",
    6: "dpi_cycle",
}
DEFAULT_STAGE_COLORS = [
    (0xFF, 0x00, 0x00), (0x00, 0xFF, 0x00), (0x00, 0x00, 0xFF), (0xFF, 0xFF, 0x00),
    (0x00, 0xFF, 0xFF), (0xFF, 0x00, 0xFF), (0xFF, 0xFF, 0xFF), (0xFF, 0x80, 0x00),
]

# Backwards-compatible name (CI smoke test & external scripts).
_HIDIOCSFEATURE = hidraw.HIDIOCSFEATURE


def _clamp(value, lo, hi) -> int:
    return max(lo, min(hi, int(value)))


def _checksum16(buf: bytearray, start: int, end: int) -> Tuple[int, int]:
    csum = sum(buf[start:end]) & 0xFFFF
    return (csum >> 8) & 0xFF, csum & 0xFF


def normalize_dpi(value: int) -> int:
    """Validate a DPI value and round it to the sensor's 50 DPI step."""
    value = int(value)
    if not DPI_RANGE[0] <= value <= DPI_RANGE[1]:
        raise ValueError(f"DPI {value} hors limites ({DPI_RANGE[0]} à {DPI_RANGE[1]} DPI).")
    return int(round(value / DPI_STEP)) * DPI_STEP


def _rgb(color) -> Tuple[int, int, int]:
    if isinstance(color, str):
        text = color.strip().lstrip("#")
        if len(text) != 6:
            raise ValueError(f"Couleur invalide: {color!r}")
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    r, g, b = color
    return _clamp(r, 0, 255), _clamp(g, 0, 255), _clamp(b, 0, 255)


# =============================================================================
# Pure frame builders
# =============================================================================

def build_polling_frame(rate_hz: int) -> bytearray:
    info = POLLING_RATE_MAP[rate_hz]
    buf = bytearray(9)
    buf[0] = REPORT_POLLING
    buf[1] = 0x09
    buf[2] = 0x01
    buf[3] = info["interval"]
    buf[4] = info["not_interval"]
    return buf


def build_dpi_frame(stages: List[int], active_stage: int, lod: int, ripple: bool, angle_snap: bool,
                    motion_sync: bool, colors: List[Tuple[int, int, int]]) -> bytearray:
    buf = bytearray(56)
    buf[0] = REPORT_DPI
    buf[1] = 0x38
    buf[2] = 0x01
    buf[3] = 0 if int(lod) <= 1 else 1        # 0 = 1 mm, 1 = 2 mm
    buf[4] = 1 if ripple else 0
    buf[5] = (1 << len(stages)) - 1           # enabled stages
    buf[6] = 1 if angle_snap else 0
    buf[7] = 1 if motion_sync else 0
    for i in range(MAX_STAGES):
        if i < len(stages):
            enc = stages[i] // DPI_STEP - 1
            buf[8 + i] = enc & 0xFF
            buf[16 + i] = (enc >> 8) & 0xFF
    buf[24] = active_stage                    # 1-based
    for i in range(MAX_STAGES):
        r, g, b = _rgb(colors[i]) if i < len(colors) else (0, 0, 0)
        buf[25 + i * 3:28 + i * 3] = bytes((r, g, b))
    buf[49] = 0x01
    buf[50], buf[51] = _checksum16(buf, 3, 50)
    return buf


def build_light_power_frame(mode: str, brightness: int, speed: int, color: Tuple[int, int, int],
                            sleep_minutes: int, led_off_minutes: int, debounce_ms: int) -> bytearray:
    """Report 0x05 exactly as MU_SkillKorp.exe builds it.

    byte 3  : effect << 4
    byte 4  : sleep timer high nibble | (9 - speed)
    byte 5  : sleep timer low nibble << 4 | (brightness for static effects, else 8)
    byte 6-8: R, G, B
    byte 9  : LED-off timer (minutes)
    byte 10 : key debounce (ms)
    byte 11-12: big-endian 16-bit sum of bytes 3..10
    """
    code = LIGHT_CODE_BY_KEY[mode]
    sleep_minutes = _clamp(sleep_minutes, *SLEEP_RANGE)
    speed = _clamp(speed, *SPEED_RANGE)
    brightness = _clamp(brightness, *BRIGHTNESS_RANGE)
    r, g, b = _rgb(color)
    buf = bytearray(15)
    buf[0] = REPORT_LIGHT_POWER
    buf[1] = 0x0F
    buf[2] = 0x01
    buf[3] = (code << 4) & 0xF0
    buf[4] = (sleep_minutes & 0xF0) | ((9 - speed) & 0x0F)
    buf[5] = ((sleep_minutes << 4) & 0xF0) | ((brightness if code in STATIC_LIGHT_CODES else 8) & 0x0F)
    buf[6], buf[7], buf[8] = r, g, b
    buf[9] = _clamp(led_off_minutes, *LED_OFF_RANGE)
    buf[10] = _clamp(debounce_ms, *DEBOUNCE_RANGE)
    buf[11], buf[12] = _checksum16(buf, 3, 11)
    return buf


def build_buttons_frame(button_map: Dict[int, str]) -> bytearray:
    buf = bytearray(59)
    buf[0] = REPORT_BUTTONS
    buf[1] = 0x3B
    buf[2] = 0x01
    # Factory 18-slot table; unassigned slots are 0x01 (disabled), not 0x00.
    slots = [(0x01, 0x00, 0x00)] * 18
    slots[4] = (0x3C, 0x00, 0x00)
    slots[5] = (0x0F, 0x00, 0x00)
    slots[8] = (0x3C, 0x00, 0x00)
    slots[16] = (0x0A, 0x00, 0x00)  # scroll down
    slots[17] = (0x09, 0x00, 0x00)  # scroll up
    for phys_btn, act_name in button_map.items():
        slot_idx = PHYSICAL_BTN_TO_SLOT.get(int(phys_btn))
        if slot_idx is not None:
            _, a, b, c = BUTTON_ACTIONS[act_name]
            slots[slot_idx] = (a, b, c)
    for i, (a, b, c) in enumerate(slots):
        buf[3 + i * 3:6 + i * 3] = bytes((a, b, c))
    buf[57], buf[58] = _checksum16(buf, 3, 57)
    return buf


def build_query_frame() -> bytearray:
    buf = bytearray(10)
    buf[0:6] = bytes((REPORT_QUERY, 0x0A, 0x01, 0xFE, 0x01, 0xFE))
    return buf


def parse_status_report(rep: bytes) -> Optional[Dict[str, Any]]:
    """Decode an input report 0x03 (battery or DPI stage notification)."""
    if len(rep) < 5 or rep[0] != INPUT_REPORT_STATUS:
        return None
    kind = rep[2]
    if kind == 0x40:
        pct = rep[4]
        if pct > 100:
            return None
        return {"type": "battery", "percentage": pct, "charging": rep[3] == 2}
    if kind in (0x10, 0x20):
        return {"type": "dpi_stage", "stage": rep[3]}
    if kind == 0x50:
        # Acknowledgement sent by the receiver for every accepted feature
        # report: 03 10 50 00 <report id> (observed on the real 1d57:fa60).
        return {"type": "ack", "report": rep[4]}
    return None


class SkillkorpM20Driver(SkillkorpDeviceDriver):
    """High-level driver for the SkillKorp M20 Ultimate mouse."""

    device_type = "mouse"
    display_name = "Souris SkillKorp M20"
    last_error = ""

    # ------------------------------------------------------------------
    # Discovery / config plumbing
    # ------------------------------------------------------------------
    @staticmethod
    def _is_vendor_interface(node: HidrawNode) -> bool:
        rep = node.reports
        return USAGE_PAGE_VENDOR in rep.usage_pages and REPORT_DPI in rep.feature

    @classmethod
    def find_nodes(cls) -> List[HidrawNode]:
        nodes = hidraw.find_nodes(VENDOR_ID, (PID_WIRED, PID_WIRELESS), cls._is_vendor_interface)
        nodes.sort(key=lambda n: 0 if n.product_id == PID_WIRED else 1)
        return nodes

    def wireless_product_ids(self) -> tuple:
        return (PID_WIRELESS,)

    def config_file(self) -> str:
        return DEFAULT_PROFILE_FILE

    def default_config(self) -> Dict[str, Any]:
        return {
            "polling_rate": 1000,
            "dpi_stages": [400, 800, 1600, 3200, 6400, 26000],
            "active_stage": 2,
            "lod": 1,
            "debounce": 4,
            "ripple": False,
            "angle_snap": False,
            "motion_sync": True,
            "light_mode": "static",
            "brightness": 8,
            "speed": 6,
            "light_color": [255, 0, 0],
            "dpi_colors": [list(c) for c in DEFAULT_STAGE_COLORS],
            "buttons": {str(k): v for k, v in FACTORY_BUTTONS.items()},
            "battery": None,
            "charging": False,
            "battery_updated": None,
            "sleep_timer_minutes": 10,
            "led_off_minutes": 1,
        }

    def sanitize_config(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        d = self.default_config()
        try:
            if cfg.get("polling_rate") not in POLLING_RATE_MAP:
                cfg["polling_rate"] = d["polling_rate"]
            stages = cfg.get("dpi_stages")
            try:
                stages = [normalize_dpi(s) for s in stages][:MAX_STAGES]
            except (TypeError, ValueError):
                stages = []
            cfg["dpi_stages"] = stages or d["dpi_stages"]
            cfg["active_stage"] = _clamp(cfg.get("active_stage", 1), 1, len(cfg["dpi_stages"]))
            cfg["lod"] = 1 if int(cfg.get("lod", 1)) <= 1 else 2
            cfg["debounce"] = _clamp(cfg.get("debounce", 4), *DEBOUNCE_RANGE)
            if cfg.get("light_mode") not in LIGHT_CODE_BY_KEY:
                cfg["light_mode"] = d["light_mode"]
            cfg["brightness"] = _clamp(cfg.get("brightness", 8), *BRIGHTNESS_RANGE)
            cfg["speed"] = _clamp(cfg.get("speed", 6), *SPEED_RANGE)
            cfg["light_color"] = list(_rgb(cfg.get("light_color", [255, 0, 0])))
            colors = cfg.get("dpi_colors") or []
            cfg["dpi_colors"] = [list(_rgb(c)) for c in colors][:MAX_STAGES] or d["dpi_colors"]
            cfg["sleep_timer_minutes"] = _clamp(cfg.get("sleep_timer_minutes", 10), *SLEEP_RANGE)
            cfg["led_off_minutes"] = _clamp(cfg.get("led_off_minutes", 1), *LED_OFF_RANGE)
            buttons = cfg.get("buttons") if isinstance(cfg.get("buttons"), dict) else {}
            cfg["buttons"] = {str(k): v for k, v in buttons.items()
                              if str(k).isdigit() and 1 <= int(k) <= 6 and v in BUTTON_ACTIONS}
            for k, v in FACTORY_BUTTONS.items():
                cfg["buttons"].setdefault(str(k), v)
            cfg.pop("move_to_wake", None)
            if cfg.get("battery_updated") is None:
                # 1.0.x stored a made-up 100 % before the mouse ever reported anything
                cfg["battery"] = None
            for key in ("ripple", "angle_snap", "motion_sync", "charging"):
                cfg[key] = bool(cfg.get(key, d.get(key)))
        except (TypeError, ValueError):
            return d
        return cfg

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    ACK_TIMEOUT = 0.6

    def _send(self, *frames: bytes, settle: float = 0.05):
        """Send reports and wait for the receiver's acknowledgement of each one.

        Without an acknowledgement the report is sent once more (the mouse may
        just have woken up); a second silence raises DeviceIOError on the
        2.4GHz receiver. Status notifications seen meanwhile are recorded.
        """
        events: List[Dict[str, Any]] = []
        try:
            with self.open_device() as dev:
                for i, frame in enumerate(frames):
                    if i:
                        time.sleep(settle)
                    report_id = frame[0]
                    acked = False
                    for _attempt in range(2):
                        dev.drain_input()
                        dev.set_feature(frame)
                        for rep in dev.read_input(self.ACK_TIMEOUT, until=lambda r, rid=report_id: (
                                parse_status_report(r) or {}).get("report") == rid):
                            ev = parse_status_report(rep)
                            if ev and ev["type"] == "ack" and ev["report"] == report_id:
                                acked = True
                            elif ev and ev["type"] != "ack":
                                events.append(ev)
                        if acked:
                            break
                    if not acked:
                        if dev.node.product_id == PID_WIRELESS:
                            raise DeviceError(
                                f"La souris n'a pas confirmé le rapport 0x{report_id:02X} "
                                "(en veille ou hors de portée ? bougez-la puis réessayez).")
                        self.last_error = f"rapport 0x{report_id:02X} non confirmé"
        finally:
            if events:
                self._record_events(events)

    def _send_feature_report(self, report_bytes: bytearray, retries: int = 3) -> bool:
        """Compatibility wrapper returning True/False."""
        try:
            with self.open_device() as dev:
                dev.set_feature(report_bytes, retries=retries)
            return True
        except DeviceError as e:
            self.last_error = str(e)
            return False

    def _guard(self, *frames: bytes) -> bool:
        """True on success, False if the mouse refused a report. Missing device,
        permission and busy errors are raised."""
        try:
            self._send(*frames)
            self.last_error = ""
            return True
        except (DevicePermissionError, DeviceNotFoundError, DeviceBusyError):
            raise
        except DeviceError as e:
            self.last_error = str(e)
            return False

    def _light_power_frame(self, cfg: Dict[str, Any]) -> bytearray:
        return build_light_power_frame(
            cfg.get("light_mode", "static"), cfg.get("brightness", 8), cfg.get("speed", 6),
            tuple(cfg.get("light_color", [255, 0, 0])), cfg.get("sleep_timer_minutes", 10),
            cfg.get("led_off_minutes", 1), cfg.get("debounce", 4))

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def poll_status(self, timeout: float = 0.3) -> List[Dict[str, Any]]:
        """Send the 0x0C query and collect status notifications on the same fd."""
        events: List[Dict[str, Any]] = []
        with self.open_device() as dev:
            dev.drain_input()
            try:
                dev.set_feature(build_query_frame(), retries=1)
            except DeviceError:
                pass  # asleep mouse: the receiver may refuse, still read what is queued
            for rep in dev.read_input(timeout, until=lambda r: (parse_status_report(r) or {}).get("type") == "battery"):
                ev = parse_status_report(rep)
                if ev:
                    events.append(ev)
        self._record_events(events)
        return events

    def _record_events(self, events: List[Dict[str, Any]]):
        patch: Dict[str, Any] = {}
        for ev in events:
            if ev["type"] == "battery":
                patch.update({"battery": ev["percentage"], "charging": ev["charging"],
                              "battery_updated": time.time()})
            elif ev["type"] == "dpi_stage" and 1 <= ev["stage"] <= len(self.config.get("dpi_stages", [])):
                patch["active_stage"] = ev["stage"]
        changed = {k: v for k, v in patch.items() if k == "battery_updated" or self.config.get(k) != v}
        if any(k != "battery_updated" for k in changed) or (
                "battery_updated" in changed and time.time() - (self.config.get("battery_updated") or 0) > 300):
            self.commit(patch)

    def listen(self, duration: float) -> List[Dict[str, Any]]:
        """Passively collect status notifications for *duration* seconds.

        Uses a non-exclusive descriptor so the GUI/tray keep working meanwhile;
        reports are only queued by the kernel while a descriptor is open, so a
        caller that wants every DPI-button event should call this in a loop.
        """
        events: List[Dict[str, Any]] = []
        with self.open_device(exclusive=False) as dev:
            for rep in dev.read_input(duration):
                ev = parse_status_report(rep)
                if ev:
                    events.append(ev)
        self._record_events(events)
        return events

    def query_status(self, poll_hardware: bool = False) -> Dict[str, Any]:
        """Return the mouse status; with poll_hardware=True, ask the mouse first."""
        self.reload_config_if_changed()
        connected = self.is_connected()
        if connected and poll_hardware:
            try:
                self.poll_status()
            except DeviceError as e:
                self.last_error = str(e)
        cfg = self.config
        stages = cfg.get("dpi_stages") or [800]
        active = _clamp(cfg.get("active_stage", 1), 1, len(stages))
        return {
            "connected": connected,
            "wireless": self.is_wireless() if connected else False,
            "device": self.dev_path,
            "battery": cfg.get("battery"),
            "charging": cfg.get("charging", False),
            "polling_rate_hz": cfg.get("polling_rate", 1000),
            "active_dpi": stages[active - 1],
            "active_stage": active,
            "stages": stages,
            "lod_mm": cfg.get("lod", 1),
            "debounce_ms": cfg.get("debounce", 4),
            "motion_sync": cfg.get("motion_sync", True),
            "angle_snapping": cfg.get("angle_snap", False),
            "ripple_control": cfg.get("ripple", False),
            "light_mode": cfg.get("light_mode", "static"),
            "sleep_timer_minutes": cfg.get("sleep_timer_minutes", 10),
            "led_off_minutes": cfg.get("led_off_minutes", 1),
        }

    def get_battery(self, poll_hardware: bool = False) -> Dict[str, Any]:
        """Battery in the common shape. Percentage is None until the mouse reported it."""
        if not self.is_connected():
            return {"percentage": None, "charging": False, "connected": False,
                    "wireless": False, "status_str": "Non connectée"}
        if poll_hardware:
            try:
                self.poll_status()
            except DeviceError:
                pass
        self.reload_config_if_changed()
        pct = self.config.get("battery")
        charging = bool(self.config.get("charging", False))
        if pct is None:
            status = "Niveau inconnu (en attente d'un rapport de la souris)"
        else:
            status = f"En charge ({pct}%)" if charging else f"{pct}%"
        return {"percentage": pct, "charging": charging, "connected": True,
                "wireless": self.is_wireless(), "status_str": status}

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def set_polling_rate(self, rate_hz: int) -> bool:
        """Set mouse polling rate (125, 250, 500 or 1000 Hz)."""
        if rate_hz not in POLLING_RATE_MAP:
            raise ValueError(f"Taux invalide {rate_hz}Hz. Choix valides: {list(POLLING_RATE_MAP.keys())}")
        if not self._guard(build_polling_frame(rate_hz)):
            return False
        self.commit({"polling_rate": rate_hz})
        return True

    def set_dpi_and_sensor(self, stages: Optional[List[int]] = None, active_stage: Optional[int] = None,
                           lod: Optional[int] = None, debounce: Optional[int] = None,
                           ripple: Optional[bool] = None, angle_snap: Optional[bool] = None,
                           motion_sync: Optional[bool] = None,
                           colors: Optional[List[Tuple[int, int, int]]] = None) -> bool:
        """DPI stages and sensor options (report 0x04). The key debounce lives in
        report 0x05 and is sent alongside when it changes."""
        self.reload_config_if_changed()
        cfg = self.config
        if stages is not None:
            if not 1 <= len(stages) <= MAX_STAGES:
                raise ValueError(f"Le nombre d'étapes DPI doit être compris entre 1 et {MAX_STAGES}.")
            stages = [normalize_dpi(s) for s in stages]
        new_stages = stages if stages is not None else list(cfg.get("dpi_stages", [800]))
        if active_stage is not None and not 1 <= int(active_stage) <= len(new_stages):
            raise ValueError(f"Étape active {active_stage} invalide (1 à {len(new_stages)}).")
        new_active = int(active_stage) if active_stage is not None else _clamp(cfg.get("active_stage", 1), 1, len(new_stages))
        if lod is not None and int(lod) not in (1, 2):
            raise ValueError("LOD invalide (1 ou 2 mm).")
        if debounce is not None and not DEBOUNCE_RANGE[0] <= int(debounce) <= DEBOUNCE_RANGE[1]:
            raise ValueError(f"Debounce invalide ({DEBOUNCE_RANGE[0]} à {DEBOUNCE_RANGE[1]} ms).")
        new = {
            "dpi_stages": new_stages,
            "active_stage": new_active,
            "lod": int(lod) if lod is not None else cfg.get("lod", 1),
            "ripple": bool(ripple) if ripple is not None else cfg.get("ripple", False),
            "angle_snap": bool(angle_snap) if angle_snap is not None else cfg.get("angle_snap", False),
            "motion_sync": bool(motion_sync) if motion_sync is not None else cfg.get("motion_sync", True),
            "dpi_colors": [list(_rgb(c)) for c in colors] if colors else cfg.get("dpi_colors", DEFAULT_STAGE_COLORS),
        }
        frames = [build_dpi_frame(new["dpi_stages"], new["active_stage"], new["lod"], new["ripple"],
                                  new["angle_snap"], new["motion_sync"], new["dpi_colors"])]
        if debounce is not None and int(debounce) != cfg.get("debounce"):
            new["debounce"] = int(debounce)
            merged = dict(cfg)
            merged.update(new)
            frames.append(self._light_power_frame(merged))
        if not self._guard(*frames):
            return False
        self.update_config(lambda c: c.update(new))
        return True

    def set_rgb_lighting(self, mode: str, brightness: Optional[int] = None, speed: Optional[int] = None,
                         color: Optional[Tuple[int, int, int]] = None) -> bool:
        """Lighting effect (report 0x05); power settings and debounce are preserved."""
        self.reload_config_if_changed()
        mode = str(mode).lower()
        if mode not in LIGHT_CODE_BY_KEY:
            raise ValueError(f"Mode d'éclairage inconnu: {mode}. Choix: {', '.join(LIGHT_CODE_BY_KEY)}")
        new = {
            "light_mode": mode,
            "brightness": _clamp(self.config.get("brightness", 8) if brightness is None else brightness, *BRIGHTNESS_RANGE),
            "speed": _clamp(self.config.get("speed", 6) if speed is None else speed, *SPEED_RANGE),
            "light_color": list(_rgb(color)) if color is not None else self.config.get("light_color", [255, 0, 0]),
        }
        merged = dict(self.config)
        merged.update(new)
        if not self._guard(self._light_power_frame(merged)):
            return False
        self.update_config(lambda c: c.update(new))
        return True

    def set_power_settings(self, sleep_timer_minutes: Optional[int] = None,
                           led_off_minutes: Optional[int] = None,
                           move_to_wake: Optional[bool] = None) -> bool:
        """Deep-sleep and LED-off timers in minutes (1..60). *move_to_wake* is
        accepted for compatibility but not supported by the M20 hardware."""
        self.reload_config_if_changed()
        new = {
            "sleep_timer_minutes": _clamp(self.config.get("sleep_timer_minutes", 10)
                                          if sleep_timer_minutes is None else sleep_timer_minutes, *SLEEP_RANGE),
            "led_off_minutes": _clamp(self.config.get("led_off_minutes", 1)
                                      if led_off_minutes is None else led_off_minutes, *LED_OFF_RANGE),
        }
        merged = dict(self.config)
        merged.update(new)
        if not self._guard(self._light_power_frame(merged)):
            return False
        self.update_config(lambda c: c.update(new))
        return True

    def set_debounce(self, debounce_ms: int) -> bool:
        """Key debounce in ms (2..25), sent in report 0x05."""
        debounce_ms = int(debounce_ms)
        if not DEBOUNCE_RANGE[0] <= debounce_ms <= DEBOUNCE_RANGE[1]:
            raise ValueError(f"Debounce invalide ({DEBOUNCE_RANGE[0]} à {DEBOUNCE_RANGE[1]} ms).")
        self.reload_config_if_changed()
        merged = dict(self.config)
        merged["debounce"] = debounce_ms
        if not self._guard(self._light_power_frame(merged)):
            return False
        self.commit({"debounce": debounce_ms})
        return True

    def set_buttons(self, button_map: Dict[int, str]) -> bool:
        """Remap physical buttons 1..6 (report 0x08). At least one must stay a left click."""
        self.reload_config_if_changed()
        merged: Dict[int, str] = dict(FACTORY_BUTTONS)
        for k, v in self.config.get("buttons", {}).items():
            merged[int(k)] = v
        for k, v in button_map.items():
            k = int(k)
            if k not in PHYSICAL_BTN_TO_SLOT:
                raise ValueError(f"Bouton invalide: {k} (1 à 6).")
            if v not in BUTTON_ACTIONS:
                raise ValueError(f"Action inconnue: {v}.")
            merged[k] = v
        if not any(act == "left_click" for act in merged.values()):
            raise ValueError("Sécurité: Au moins un bouton doit être configuré en 'Clic Gauche'.")
        if not self._guard(build_buttons_frame(merged)):
            return False
        buttons = {str(k): v for k, v in sorted(merged.items())}
        self.update_config(lambda c: c.__setitem__("buttons", buttons))
        return True

    def restore_factory_buttons(self) -> bool:
        return self.set_buttons(dict(FACTORY_BUTTONS))

    def apply_all(self) -> bool:
        """Re-send the whole saved configuration to the mouse."""
        self.reload_config_if_changed()
        return self.apply_profile(dict(self.config))

    def apply_profile(self, profile: Dict[str, Any]) -> bool:
        """Apply a complete profile; returns True only if every report was accepted."""
        self.reload_config_if_changed()
        current = dict(self.config)
        candidate = dict(current)
        for key in ("polling_rate", "dpi_stages", "active_stage", "lod", "ripple", "angle_snap",
                    "motion_sync", "dpi_colors", "light_mode", "brightness", "speed", "light_color",
                    "sleep_timer_minutes", "led_off_minutes", "debounce"):
            if key in profile and profile[key] is not None:
                candidate[key] = profile[key]
        buttons = dict(FACTORY_BUTTONS)
        for k, v in (profile.get("buttons") or current.get("buttons", {})).items():
            buttons[int(k)] = v
        candidate["buttons"] = {str(k): v for k, v in buttons.items()}
        cfg = self.sanitize_config(candidate)
        frames = [
            build_polling_frame(cfg["polling_rate"]),
            build_dpi_frame(cfg["dpi_stages"], cfg["active_stage"], cfg["lod"], cfg["ripple"],
                            cfg["angle_snap"], cfg["motion_sync"], cfg["dpi_colors"]),
            self._light_power_frame(cfg),
        ]
        if any(act == "left_click" for act in buttons.values()):
            frames.append(build_buttons_frame({int(k): v for k, v in cfg["buttons"].items()}))
        else:
            print("[souris] Profil ignoré pour les boutons: aucun clic gauche.", file=sys.stderr)
        if not self._guard(*frames):
            return False
        keep = {k: current.get(k) for k in ("battery", "charging", "battery_updated")}
        self.update_config(lambda c: (c.update(cfg), c.update(keep)))
        return True
