#!/usr/bin/env python3
"""
SkillKorp M20 Ultimate - Linux Hardware Driver & Controller
Reverse-engineered from SourisM20Ultimate.exe (XinMaiGao Beken PAW3395 Solution)

Supports:
- 2.4GHz Wireless (1d57:fa60) and USB-C Wired (1d57:fa61)
- Real-time battery status and charging detection
- DPI stages configuration (50 - 26,000 DPI in 50 DPI increments, up to 8 stages)
- Stage LED colors customization
- Polling rate control (125 Hz, 250 Hz, 500 Hz, 1000 Hz)
- Sensor attributes: Lift-off distance (1mm/2mm), Debounce time (0-20ms),
  Motion Sync, Angle Snapping, Ripple Control
- RGB Lighting modes (12 effects), brightness (1-8), speed (1-8), color
- Button remapping (Left, Right, Middle, Forward, Backward, DPI, Media, etc.)
- Profile saving & loading

Ported into SkillKorp Suite from the standalone skillkorp-m20 project. Unlike
the keyboard driver, this one had not previously been through a dedicated
byte-offset audit against the protocol description in skillkorp-m20/README.md.
It was re-checked during this port (Report 0x05 sleep/lighting checksum window
and the 0x03/0x10/0x40 battery input report parsing): both match the documented
protocol and the existing test suite, so the frame-encoding logic below is
carried over unchanged. Only two additive methods were introduced to satisfy
the common SkillkorpDeviceDriver interface - `is_wireless()` (the standalone
driver never exposed this) and `apply_profile()` (a thin wrapper around the
same steps `profile_manager.apply_profile_to_driver()` used to perform
inline) - neither changes any existing hardware behavior.
"""

import os
import sys
import glob
import fcntl
import json
import time
import select
from typing import Dict, List, Optional, Tuple, Any

from .base import SkillkorpDeviceDriver

CONFIG_DIR = os.path.expanduser("~/.config/skillkorp/mouse")
DEFAULT_PROFILE_FILE = os.path.join(CONFIG_DIR, "default_profile.json")

# Polling Rate mappings: interval in ms -> Hz
POLLING_RATE_MAP = {
    125: {"interval": 0x08, "not_interval": 0xF7},
    250: {"interval": 0x04, "not_interval": 0xFB},
    500: {"interval": 0x02, "not_interval": 0xFD},
    1000: {"interval": 0x01, "not_interval": 0xFE},
}
INTERVAL_TO_HZ = {0x08: 125, 0x04: 250, 0x02: 500, 0x01: 1000}

# RGB Lighting Modes
LIGHT_MODES = [
    ("off", "Éteint (Off)", 1),
    ("static", "Fixe (Static)", 2),
    ("breathing", "Respiration (Breathing)", 3),
    ("neon", "Néon (Neon)", 4),
    ("color_breathing", "Respiration couleur (Color Breathing)", 5),
    ("color_static", "Fixe couleur (Color Static)", 6),
    ("mixed_breathing", "Respiration mixte (Mixed Breathing)", 7),
    ("rainbow_wave", "Onde arc-en-ciel (Rainbow Wave)", 8),
    ("lightning", "Éclair (Lightning)", 9),
    ("static_mixed", "Couleur mixte fixe (Static Mixed Color)", 10),
    ("marquee_1", "Défilement 1 (Marquee 1)", 11),
    ("marquee_2", "Défilement 2 (Marquee 2)", 12),
]

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
    "select_all": ("Tout Sélectionner (Ctrl+A)", 0x11, 0x01, 0x04),
    "save": ("Enregistrer (Ctrl+S)", 0x11, 0x01, 0x16),
    "find": ("Rechercher (Ctrl+F)", 0x11, 0x01, 0x09),
    "undo": ("Annuler (Ctrl+Z)", 0x11, 0x01, 0x1D),
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


def _HIDIOCSFEATURE(length: int) -> int:
    """Linux ioctl macro for HIDIOCSFEATURE: _IOC(_IOC_READ|_IOC_WRITE, 'H', 0x06, length)"""
    return (3 << 30) | (length << 16) | (ord('H') << 8) | 0x06


class SkillkorpM20Driver(SkillkorpDeviceDriver):
    """High-level Python driver for SkillKorp M20 Ultimate gaming mouse."""

    device_type = "mouse"
    _config_mtime: float = 0.0

    def __init__(self, dev_path: Optional[str] = None):
        self.dev_path = dev_path or self.find_device()
        self._config_mtime = 0.0
        self.config = self._load_or_default_config()

    def reload_config_if_changed(self) -> bool:
        """Reload self.config from disk if the configuration file has been modified externally.

        Uses file modification time (mtime) for lightweight checking without unnecessary JSON parsing.
        Returns True if config was reloaded, False otherwise.
        """
        try:
            if not os.path.exists(DEFAULT_PROFILE_FILE):
                return False
            mtime = os.path.getmtime(DEFAULT_PROFILE_FILE)
            if mtime != getattr(self, "_config_mtime", 0.0):
                self.config = self._load_or_default_config()
                return True
        except OSError:
            pass
        return False

    @staticmethod
    def find_device() -> Optional[str]:
        """Auto-detect the mouse hidraw device node (prefers wired fa61 if plugged in, else wireless fa60)."""
        candidates = []
        for hidraw in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
            dev_dir = os.path.realpath(hidraw + "/device")
            uevent_file = os.path.join(dev_dir, "uevent")
            desc_file = os.path.join(dev_dir, "report_descriptor")
            if os.path.exists(uevent_file) and os.path.exists(desc_file):
                try:
                    with open(uevent_file, "r") as f:
                        uevent = f.read().lower()
                    if "1d57" in uevent and ("fa60" in uevent or "fa61" in uevent):
                        with open(desc_file, "rb") as f:
                            desc = f.read()
                        if b"\x05\x0b" in desc and b"\x85\x04" in desc:
                            node = f"/dev/{os.path.basename(hidraw)}"
                            if os.path.exists(node):
                                is_wired = "fa61" in uevent
                                candidates.append((is_wired, node))
                except Exception:
                    continue
        if candidates:
            # Wired first (is_wired=True)
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
        return None

    def is_connected(self) -> bool:
        """Check if the mouse node is available."""
        if not self.dev_path or not os.path.exists(self.dev_path):
            self.dev_path = self.find_device()
        return bool(self.dev_path and os.path.exists(self.dev_path))

    def is_wireless(self) -> bool:
        """Return True if connected via the 2.4GHz wireless dongle (PID fa60) rather than USB-C (fa61)."""
        if not self.dev_path:
            return False
        hidraw_name = os.path.basename(self.dev_path)
        uevent_path = f"/sys/class/hidraw/{hidraw_name}/device/uevent"
        if os.path.exists(uevent_path):
            with open(uevent_path, "r") as f:
                content = f.read().lower()
            return "fa60" in content
        return False

    def _send_feature_report(self, report_bytes: bytearray, retries: int = 3) -> bool:
        """Send a feature report via HIDIOCSFEATURE ioctl."""
        if not self.is_connected():
            raise IOError("Souris SkillKorp M20 non connectée.")

        fd = os.open(self.dev_path, os.O_RDWR)
        try:
            cmd = _HIDIOCSFEATURE(len(report_bytes))
            for i in range(retries):
                try:
                    res = fcntl.ioctl(fd, cmd, report_bytes, True)
                    if res == len(report_bytes):
                        return True
                except (BrokenPipeError, OSError):
                    pass
                if retries > 1 and i < retries - 1:
                    time.sleep(0.08)
            return False
        finally:
            os.close(fd)

    def _read_input_reports(self, timeout_sec: float = 0.0) -> List[bytes]:
        """Read pending input reports non-blockingly."""
        if not self.is_connected():
            return []

        try:
            fd = os.open(self.dev_path, os.O_RDWR | os.O_NONBLOCK)
        except Exception:
            return []

        reports = []
        try:
            r, _, _ = select.select([fd], [], [], timeout_sec)
            if r:
                while True:
                    try:
                        data = os.read(fd, 64)
                        if data:
                            reports.append(data)
                        else:
                            break
                    except (BlockingIOError, OSError):
                        break
        finally:
            os.close(fd)
        return reports

    def query_status(self, poll_hardware: bool = False) -> Dict:
        """Query mouse status. If poll_hardware=True, sends 0x0C query packet over RF."""
        self.reload_config_if_changed()

        if poll_hardware:
            query_buf = bytearray([0x0C, 0x0A, 0x01, 0xFE, 0x01, 0xFE, 0x00, 0x00, 0x00, 0x00])
            try:
                self._send_feature_report(query_buf, retries=1)
            except Exception:
                pass
            reports = self._read_input_reports(timeout_sec=0.1)
        else:
            reports = self._read_input_reports(timeout_sec=0.0)

        battery_pct = self.config.get("battery", 100)
        charging = self.config.get("charging", False)

        for rep in reports:
            if len(rep) >= 5 and rep[0] == 0x03:
                w_param = rep[1] | (rep[2] << 8)
                if (w_param & 0xFF00) == 0x4000:
                    status = rep[3]
                    battery_pct = rep[4]
                    charging = (status == 2)
                    if self.config.get("battery") != battery_pct or self.config.get("charging") != charging:
                        self.reload_config_if_changed()
                        self.config["battery"] = battery_pct
                        self.config["charging"] = charging
                        self._save_config()
                elif (w_param & 0xFF00) in (0x1000, 0x2000):
                    new_stage = rep[3]
                    if 1 <= new_stage <= len(self.config.get("dpi_stages", [])):
                        if self.config.get("active_stage") != new_stage:
                            self.reload_config_if_changed()
                            self.config["active_stage"] = new_stage
                            self._save_config()

        return {
            "connected": self.is_connected(),
            "device": self.dev_path,
            "battery": battery_pct,
            "charging": charging,
            "polling_rate_hz": self.config.get("polling_rate", 1000),
            "active_dpi": self.config.get("dpi_stages", [800])[
                max(1, min(self.config.get("active_stage", 2), len(self.config.get("dpi_stages", [800])))) - 1
            ],
            "active_stage": self.config.get("active_stage", 2),
            "stages": self.config.get("dpi_stages", [400, 800, 1600, 3200, 6400, 26000]),
            "lod_mm": self.config.get("lod", 1),
            "debounce_ms": self.config.get("debounce", 4),
            "motion_sync": self.config.get("motion_sync", True),
            "angle_snapping": self.config.get("angle_snap", False),
            "ripple_control": self.config.get("ripple", False),
            "light_mode": self.config.get("light_mode", "static"),
            "sleep_timer_minutes": self.config.get("sleep_timer_minutes", 5),
            "move_to_wake": self.config.get("move_to_wake", True),
        }

    def set_polling_rate(self, rate_hz: int) -> bool:
        """Set mouse polling rate (125, 250, 500, or 1000 Hz)."""
        self.reload_config_if_changed()
        if rate_hz not in POLLING_RATE_MAP:
            raise ValueError(f"Taux invalide {rate_hz}Hz. Choix valides: {list(POLLING_RATE_MAP.keys())}")

        rate_info = POLLING_RATE_MAP[rate_hz]
        buf = bytearray(9)
        buf[0] = 0x06  # Report ID 6
        buf[1] = 0x09
        buf[2] = 0x01
        buf[3] = rate_info["interval"]
        buf[4] = rate_info["not_interval"]

        success = self._send_feature_report(buf)
        if success:
            self.config["polling_rate"] = rate_hz
            self._save_config()
        return success

    def set_dpi_and_sensor(
        self,
        stages: Optional[List[int]] = None,
        active_stage: Optional[int] = None,
        lod: Optional[int] = None,
        debounce: Optional[int] = None,
        ripple: Optional[bool] = None,
        angle_snap: Optional[bool] = None,
        motion_sync: Optional[bool] = None,
        colors: Optional[List[Tuple[int, int, int]]] = None,
    ) -> bool:
        """
        Configure DPI stages and sensor parameters via Report ID 0x04.
        """
        self.reload_config_if_changed()
        if stages is not None:
            if not (1 <= len(stages) <= 8):
                raise ValueError("Le nombre d'étapes DPI doit être compris entre 1 et 8.")
            for s in stages:
                if not (50 <= s <= 26000):
                    raise ValueError(f"DPI {s} hors limites (50 à 26 000 DPI).")
            self.config["dpi_stages"] = stages

        current_stages = self.config.get("dpi_stages", [400, 800, 1600, 3200, 6400, 26000])

        if active_stage is not None:
            if not (1 <= active_stage <= len(current_stages)):
                raise ValueError(f"Étape active {active_stage} invalide (1 à {len(current_stages)}).")
            self.config["active_stage"] = active_stage

        cur_active = self.config.get("active_stage", 2)
        cur_active = max(1, min(cur_active, len(current_stages)))  # clamp si étapes réduites
        cur_lod = lod if lod is not None else self.config.get("lod", 1)
        cur_debounce = debounce if debounce is not None else self.config.get("debounce", 4)
        cur_ripple = ripple if ripple is not None else self.config.get("ripple", False)
        cur_angle = angle_snap if angle_snap is not None else self.config.get("angle_snap", False)
        cur_msync = motion_sync if motion_sync is not None else self.config.get("motion_sync", True)

        default_stage_colors = [
            (0xFF, 0x00, 0x00),  # Red
            (0x00, 0xFF, 0x00),  # Green
            (0x00, 0x00, 0xFF),  # Blue
            (0xFF, 0xFF, 0x00),  # Yellow
            (0x00, 0xFF, 0xFF),  # Cyan
            (0xFF, 0x00, 0xFF),  # Magenta
            (0xFF, 0xFF, 0xFF),  # White
            (0xFF, 0x80, 0x00),  # Orange
        ]
        cur_colors = colors or self.config.get("dpi_colors", default_stage_colors)

        buf = bytearray(56)
        buf[0] = 0x04  # Report ID 4
        buf[1] = 0x38  # Command 0x38
        buf[2] = 0x01
        buf[3] = 0 if cur_lod <= 1 else 1  # LOD: 0=1mm, 1=2mm
        buf[4] = 1 if cur_ripple else 0    # Ripple Control: 0=off, 1=on
        buf[5] = (1 << len(current_stages)) - 1  # Enabled stages mask
        buf[6] = 1 if cur_angle else 0     # Angle Snapping: 0=off, 1=on
        buf[7] = 1 if cur_msync else 0     # Motion Sync: 0=off, 1=on

        for i in range(8):
            if i < len(current_stages) and current_stages[i] > 0:
                enc = (current_stages[i] // 50) - 1
                buf[8 + i] = enc & 0xFF
                buf[16 + i] = (enc >> 8) & 0xFF
            else:
                buf[8 + i] = 0
                buf[16 + i] = 0

        buf[24] = cur_active  # 1-indexed active stage

        # 8 RGB stage LED colors (bytes 25..48)
        for i in range(8):
            r, g, b = cur_colors[i] if i < len(cur_colors) else (0, 0, 0)
            buf[25 + i * 3] = r
            buf[25 + i * 3 + 1] = g
            buf[25 + i * 3 + 2] = b

        buf[49] = 0x01

        # Checksum: 16-bit big-endian sum of bytes 3..49
        csum = sum(buf[3:50]) & 0xFFFF
        buf[50] = (csum >> 8) & 0xFF
        buf[51] = csum & 0xFF

        success = self._send_feature_report(buf)
        if success:
            self.config["lod"] = cur_lod
            self.config["debounce"] = cur_debounce
            self.config["ripple"] = cur_ripple
            self.config["angle_snap"] = cur_angle
            self.config["motion_sync"] = cur_msync
            self.config["dpi_colors"] = cur_colors
            self._save_config()
        return success

    def _send_lighting_and_power_report(
        self,
        mode: str,
        brightness: int,
        speed: int,
        color: Tuple[int, int, int],
        sleep_timer_minutes: int,
        move_to_wake: bool,
    ) -> bool:
        """
        Send combined Lighting (bytes 3..8) and Power/Sleep (bytes 9..10) Feature Report 0x05 (15 bytes).
        Ensures neither lighting nor sleep settings overwrite each other.
        """
        mode_id = 2  # default static
        for m_key, _, m_id in LIGHT_MODES:
            if m_key == mode.lower():
                mode_id = m_id
                break

        brightness = max(1, min(8, int(brightness)))
        speed = max(1, min(8, int(speed)))
        sleep_timer_minutes = max(1, min(60, int(sleep_timer_minutes)))
        r, g, b = color

        buf = bytearray(15)
        buf[0] = 0x05  # Report ID 5
        buf[1] = 0x0F  # Length 15
        buf[2] = 0x01  # Profile index
        buf[3] = (mode_id << 4) & 0xF0
        buf[4] = ((brightness & 0x0F) << 4) | (speed & 0x0F)
        buf[5] = 0x00
        buf[6] = max(0, min(255, int(r)))
        buf[7] = max(0, min(255, int(g)))
        buf[8] = max(0, min(255, int(b)))
        buf[9] = sleep_timer_minutes
        buf[10] = 0x00 if move_to_wake else 0x01

        csum = sum(buf[3:11]) & 0xFFFF
        buf[11] = (csum >> 8) & 0xFF
        buf[12] = csum & 0xFF

        return self._send_feature_report(buf)

    def set_rgb_lighting(
        self,
        mode: str,
        brightness: int = 8,
        speed: int = 4,
        color: Tuple[int, int, int] = (255, 0, 0),
    ) -> bool:
        """
        Configure RGB lighting via Report ID 0x05, preserving current sleep timer and wake mode.
        """
        self.reload_config_if_changed()
        sleep_min = self.config.get("sleep_timer_minutes", 5)
        move_wake = self.config.get("move_to_wake", True)
        success = self._send_lighting_and_power_report(
            mode=mode,
            brightness=brightness,
            speed=speed,
            color=color,
            sleep_timer_minutes=sleep_min,
            move_to_wake=move_wake,
        )
        if success:
            self.config["light_mode"] = mode
            self.config["brightness"] = brightness
            self.config["speed"] = speed
            self.config["light_color"] = list(color)
            self._save_config()
        return success

    def set_power_settings(
        self,
        sleep_timer_minutes: int = 5,
        move_to_wake: bool = True,
    ) -> bool:
        """
        Configure hardware sleep timer & wake mode via Report ID 0x05, preserving current lighting settings.
        """
        self.reload_config_if_changed()
        mode = self.config.get("light_mode", "static")
        brightness = self.config.get("brightness", 8)
        speed = self.config.get("speed", 4)
        color = tuple(self.config.get("light_color", [255, 0, 0]))
        success = self._send_lighting_and_power_report(
            mode=mode,
            brightness=brightness,
            speed=speed,
            color=color,
            sleep_timer_minutes=sleep_timer_minutes,
            move_to_wake=move_to_wake,
        )
        if success:
            self.config["sleep_timer_minutes"] = sleep_timer_minutes
            self.config["move_to_wake"] = move_to_wake
            self._save_config()
        return success

    def set_buttons(self, button_map: Dict[int, str]) -> bool:
        """
        Remap buttons via Report ID 0x08 with exact hardware slot assignments.
        Physical buttons 1..6:
        1: Left Click (Slot 0)
        2: Right Click (Slot 1)
        3: Middle Click (Slot 2)
        4: Forward / Suivant (Slot 6)
        5: Backward / Précédent (Slot 7)
        6: DPI Cycle (Slot 3)
        """
        self.reload_config_if_changed()
        # Normalize all button maps to integer keys to avoid type overwrite bugs
        merged_map: Dict[int, str] = {
            1: "left_click",
            2: "right_click",
            3: "middle_click",
            4: "forward",
            5: "backward",
            6: "dpi_cycle",
        }
        for k, v in self.config.get("buttons", {}).items():
            merged_map[int(k)] = v
        for k, v in button_map.items():
            merged_map[int(k)] = v

        # Safety check: at least one button must be left click
        has_left = any(act == "left_click" for act in merged_map.values())
        if not has_left:
            raise ValueError("Sécurité: Au moins un bouton doit être configuré en 'Clic Gauche'.")

        buf = bytearray(59)
        buf[0] = 0x08  # Report ID 8
        buf[1] = 0x3B  # Length 59
        buf[2] = 0x01

        # Factory base 18-slot mapping (from MU_SkillKorp.exe disassembly)
        # Unassigned/disabled slots must be 0x01 (Action 0), not 0x00
        # Slot 16 = Scroll Down (0x0A), Slot 17 = Scroll Up (0x09)
        slots_data = [(0x01, 0x00, 0x00)] * 18
        slots_data[4] = (0x3C, 0x00, 0x00)
        slots_data[5] = (0x0F, 0x00, 0x00)
        slots_data[8] = (0x3C, 0x00, 0x00)
        slots_data[16] = (0x0A, 0x00, 0x00)  # Scroll Down
        slots_data[17] = (0x09, 0x00, 0x00)  # Scroll Up

        # Populate physical buttons into their respective slots
        for phys_btn, act_name in merged_map.items():
            slot_idx = PHYSICAL_BTN_TO_SLOT.get(int(phys_btn))
            if slot_idx is not None:
                act_info = BUTTON_ACTIONS.get(act_name, ("Left Click", 0x02, 0x00, 0x00))
                slots_data[slot_idx] = (act_info[1], act_info[2], act_info[3])

        # Write to buffer
        for i in range(18):
            b_type, b_code, b_mod = slots_data[i]
            base_offset = 3 + i * 3
            buf[base_offset] = b_type
            buf[base_offset + 1] = b_code
            buf[base_offset + 2] = b_mod

        # Checksum: sum of bytes 3 to 56
        csum = sum(buf[3:57]) & 0xFFFF
        buf[57] = (csum >> 8) & 0xFF
        buf[58] = csum & 0xFF

        success = self._send_feature_report(buf)
        if success:
            self.config["buttons"] = {str(k): v for k, v in sorted(merged_map.items())}
            self._save_config()
            time.sleep(0.3)
        return success

    def restore_factory_buttons(self) -> bool:
        """Restore exact factory default button mapping."""
        factory_map = {
            1: "left_click",
            2: "right_click",
            3: "middle_click",
            4: "forward",
            5: "backward",
            6: "dpi_cycle",
        }
        return self.set_buttons(factory_map)

    def _load_or_default_config(self) -> Dict:
        """Load configuration from disk or create defaults."""
        os.makedirs(CONFIG_DIR, exist_ok=True)
        if os.path.exists(DEFAULT_PROFILE_FILE):
            try:
                with open(DEFAULT_PROFILE_FILE, "r") as f:
                    fcntl.flock(f, fcntl.LOCK_SH)
                    try:
                        data = json.load(f)
                        try:
                            self._config_mtime = os.path.getmtime(DEFAULT_PROFILE_FILE)
                        except OSError:
                            pass
                        return data
                    finally:
                        fcntl.flock(f, fcntl.LOCK_UN)
            except Exception as e:
                print(f"[mouse driver] Erreur lecture config: {e}", file=sys.stderr)

        default_conf = {
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
            "speed": 4,
            "light_color": [255, 0, 0],
            "dpi_colors": [
                [255, 0, 0],
                [0, 255, 0],
                [0, 0, 255],
                [255, 255, 0],
                [0, 255, 255],
                [255, 0, 255],
                [255, 255, 255],
                [255, 128, 0],
            ],
            "buttons": {
                "1": "left_click",
                "2": "right_click",
                "3": "middle_click",
                "4": "forward",
                "5": "backward",
                "6": "dpi_cycle",
            },
            "battery": 100,
            "charging": False,
            "sleep_timer_minutes": 5,
            "move_to_wake": True,
        }
        return default_conf

    def _save_config(self):
        """Save current configuration to disk (with exclusive lock to avoid concurrent write corruption)."""
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp_path = DEFAULT_PROFILE_FILE + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    json.dump(self.config, f, indent=2)
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            os.replace(tmp_path, DEFAULT_PROFILE_FILE)
            try:
                self._config_mtime = os.path.getmtime(DEFAULT_PROFILE_FILE)
            except OSError:
                pass
        except Exception as e:
            print(f"[mouse driver] Erreur sauvegarde config: {e}", file=sys.stderr)

    # save_config() is the SkillkorpDeviceDriver-interface alias for _save_config(),
    # kept alongside the original private name so existing call sites are unaffected.
    def save_config(self, config: Optional[Dict[str, Any]] = None) -> None:
        if config is not None:
            self.config = config
        self._save_config()

    def apply_all(self) -> bool:
        """Re-apply all saved configuration settings to the mouse."""
        self.reload_config_if_changed()
        success = True
        try:
            success &= self.set_polling_rate(self.config.get("polling_rate", 1000))
            time.sleep(0.05)
            success &= self.set_dpi_and_sensor(
                stages=self.config.get("dpi_stages"),
                active_stage=self.config.get("active_stage"),
                lod=self.config.get("lod"),
                debounce=self.config.get("debounce"),
                ripple=self.config.get("ripple"),
                angle_snap=self.config.get("angle_snap"),
                motion_sync=self.config.get("motion_sync"),
                colors=self.config.get("dpi_colors"),
            )
            time.sleep(0.05)
            success &= self.set_rgb_lighting(
                mode=self.config.get("light_mode", "static"),
                brightness=self.config.get("brightness", 8),
                speed=self.config.get("speed", 4),
                color=tuple(self.config.get("light_color", [255, 0, 0])),
            )
            time.sleep(0.05)
            button_map = {int(k): v for k, v in self.config.get("buttons", {}).items()}
            success &= self.set_buttons(button_map)
        except Exception as e:
            print("Erreur lors de l'application des paramètres:", e)
            return False
        return success

    def get_battery(self) -> Dict[str, Any]:
        """Return battery status in the common shape shared with the keyboard driver.

        The standalone m20 project only ever surfaced battery state as fields
        inside query_status()'s dict; this wraps the same config-backed values
        (populated by query_status() from the 0x03/0x10/0x40 input report) into
        the {percentage, charging, connected, wireless, status_str} shape the
        common SkillkorpDeviceDriver interface and the unified GUI/tray expect.
        """
        connected = self.is_connected()
        if not connected:
            return {
                "percentage": None,
                "charging": False,
                "connected": False,
                "wireless": False,
                "status_str": "Non connectée",
            }
        self.reload_config_if_changed()
        pct = self.config.get("battery", 100)
        charging = self.config.get("charging", False)
        wireless = self.is_wireless()
        status_str = f"En charge ({pct}%)" if charging else f"{pct}%"
        return {
            "percentage": pct,
            "charging": charging,
            "connected": True,
            "wireless": wireless,
            "status_str": status_str,
        }

    def apply_profile(self, profile: Dict[str, Any]) -> bool:
        """Apply a complete configuration profile dict to the hardware.

        Equivalent to what profile_manager.apply_profile_to_driver() used to do
        inline in the standalone m20 project - centralized here so the unified
        ProfileManager can treat both device drivers polymorphically.
        """
        success = True
        try:
            self.set_dpi_and_sensor(
                stages=profile.get("dpi_stages"),
                active_stage=profile.get("active_stage"),
                lod=profile.get("lod"),
                debounce=profile.get("debounce"),
                motion_sync=profile.get("motion_sync"),
                angle_snap=profile.get("angle_snap"),
                ripple=profile.get("ripple"),
            )
            if "polling_rate" in profile:
                self.set_polling_rate(profile["polling_rate"])

            self.set_rgb_lighting(
                mode=profile.get("light_mode", "static"),
                brightness=profile.get("brightness", 8),
                speed=profile.get("speed", 4),
                color=tuple(profile.get("light_color", [255, 0, 0])),
            )

            if "buttons" in profile:
                self.set_buttons({int(k): v for k, v in profile["buttons"].items()})

            self.set_power_settings(
                sleep_timer_minutes=profile.get("sleep_timer_minutes", 5),
                move_to_wake=profile.get("move_to_wake", True),
            )
        except Exception as e:
            print(f"Erreur lors de l'application du profil: {e}", file=sys.stderr)
            success = False
        return success
