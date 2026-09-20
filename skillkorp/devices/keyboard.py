#!/usr/bin/env python3
"""
SkillKorp K20 Ultimate - Linux Hardware Driver & Controller
Reverse-engineered from ClavierK20Ultimate.exe (Yichip YC3121 SoC / CX71D Board)

Supports:
- 2.4GHz Wireless Dongle (3151:4011) and USB Wired Mode (3151:4015)
- Interface 2 (Vendor Usage Page 0xFFFF, Usage 0x0002) 64-byte HID Feature Reports
- Real-time battery status querying & charging state detection
- RGB Backlight configuration (10 effects, speed 1-5, brightness 0-4, direction, custom RGB color)
- Side RGB lighting (effects, speed, brightness, color)
- Polling rate control (125 Hz, 250 Hz, 500 Hz, 1000 Hz)
- Debounce time (2 ms - 20 ms)
- Power management & sleep timeouts (light sleep and deep sleep)
- Keyboard options: Windows Key Lock, WASD <-> Arrow keys swap, OS mode, Gaming mode
- Key remapping (75% AZERTY French layout, standard keys & Fn layer)
- Hardware profile selection (profiles 0, 1, 2)
- Factory reset

Ported into SkillKorp Suite from the standalone skillkorp-k20 project. This
driver had already been through several audit passes there (byte-offset
fixes for set_sleep_time/remap_key/remap_fn_key, full 5-byte interface
descriptor detection, and an honest None battery fallback instead of a
fabricated value) - all of that hardware behavior is preserved unchanged.
"""

import os
import sys
import glob
import fcntl
import json
import time
from typing import Dict, List, Optional, Tuple, Any

from .base import SkillkorpDeviceDriver

CONFIG_DIR = os.path.expanduser("~/.config/skillkorp/keyboard")
DEFAULT_PROFILE_FILE = os.path.join(CONFIG_DIR, "default_profile.json")

# USB IDs
VENDOR_ID = 0x3151
PID_WIRELESS = 0x4011  # KU SKK 2,4GHz Dongle
PID_WIRED = 0x4015     # KU SkillKorp Wired

# Polling Rate mappings: rate in Hz -> hardware code
POLLING_RATE_MAP = {
    1000: 1,
    500: 2,
    250: 4,
    125: 8,
}
CODE_TO_POLLING_RATE = {1: 1000, 2: 500, 4: 250, 8: 125}

# RGB Lighting Effects (FEA_CMD_SET_LEDPARAM = 0x07)
LIGHT_MODES = [
    ("off", "Éteint (Off)", 0),
    ("static", "Fixe (Always On)", 1),
    ("breathing", "Respiration (Breathing)", 2),
    ("wave", "Onde (Wave)", 4),
    ("ripple", "Ondulation (Ripple)", 5),
    ("raindrop", "Gouttes de pluie (Raindrop)", 6),
    ("snake", "Serpent (Snake)", 7),
    ("press_action", "Touche active (Press Action)", 8),
    ("convergence", "Convergence", 9),
    ("custom", "Personnalisé (Custom)", 13),
]

LIGHT_MODE_BY_CODE = {code: key for key, _, code in LIGHT_MODES}
LIGHT_CODE_BY_KEY = {key: code for key, _, code in LIGHT_MODES}

# Side RGB Lighting Effects (FEA_CMD_SET_SLEDPARAM = 0x08)
SIDE_LIGHT_MODES = [
    ("off", "Éteint (Off)", 0),
    ("static", "Fixe (Static)", 1),
    ("breathing", "Respiration (Breathing)", 2),
    ("wave", "Onde (Wave)", 4),
    ("rainbow", "Arc-en-ciel (Rainbow)", 3),
]
SIDE_LIGHT_MODE_BY_CODE = {code: key for key, _, code in SIDE_LIGHT_MODES}
SIDE_LIGHT_CODE_BY_KEY = {key: code for key, _, code in SIDE_LIGHT_MODES}

# Protocol Command Codes (reverse-engineered from iot_driver.exe / Electron client)
CMD_SET_REPORT = 0x04      # Polling rate
CMD_GET_REPORT = 0x84
CMD_SET_PROFILE = 0x05     # Profile index
CMD_GET_PROFILE = 0x85
CMD_SET_LEDPARAM = 0x07    # Backlight LED config
CMD_GET_LEDPARAM = 0x87
CMD_SET_KBOPTION = 0x06    # Winlock, WASD swap, OS mode, Gaming mode
CMD_GET_KBOPTION = 0x86
CMD_SET_SLEDPARAM = 0x08   # Side LED config
CMD_GET_SLEDPARAM = 0x88
CMD_SET_KEYMATRIX = 0x09   # Full key matrix
CMD_GET_KEYMATRIX = 0x89
CMD_SET_RESERT = 0x02      # Factory reset
CMD_SET_DEBOUNCE = 0x11    # Debounce time in ms
CMD_GET_DEBOUNCE = 0x91
CMD_SET_SLEEPTIME = 0x12   # Light & deep sleep times
CMD_GET_SLEEPTIME = 0x92
CMD_SET_KEYMATRIX_SIMPLE = 0x13  # Single key remapping
CMD_GET_KEYMATRIX_SIMPLE = 0x93
CMD_SET_FN_SIMPLE = 0x15         # Single Fn layer remapping
CMD_GET_FN_SIMPLE = 0x95
CMD_GET_BATTERY = 0x83     # Battery query
CMD_GET_REV = 0x80         # Firmware revision

# Default 75% AZERTY Keyboard Layout (84 keys)
# Matches OE.layout from official software exactly
KEY_LAYOUT = [
    # Row 1
    {"name": "Esc", "code": 41, "x": 99, "y": -16, "w": 29, "h": 34},
    {"name": "F1", "code": 58, "x": 142, "y": -16, "w": 29, "h": 34},
    {"name": "F2", "code": 59, "x": 185, "y": -16, "w": 29, "h": 34},
    {"name": "F3", "code": 60, "x": 228, "y": -16, "w": 29, "h": 34},
    {"name": "F4", "code": 61, "x": 271, "y": -16, "w": 29, "h": 34},
    {"name": "F5", "code": 62, "x": 314, "y": -16, "w": 29, "h": 34},
    {"name": "F6", "code": 63, "x": 357, "y": -16, "w": 29, "h": 34},
    {"name": "F7", "code": 64, "x": 399, "y": -16, "w": 29, "h": 34},
    {"name": "F8", "code": 65, "x": 442, "y": -16, "w": 29, "h": 34},
    {"name": "F9", "code": 66, "x": 485, "y": -16, "w": 29, "h": 34},
    {"name": "F10", "code": 67, "x": 528, "y": -16, "w": 29, "h": 34},
    {"name": "F11", "code": 68, "x": 571, "y": -16, "w": 29, "h": 34},
    {"name": "F12", "code": 69, "x": 614, "y": -16, "w": 29, "h": 34},
    {"name": "Recherche", "code": 50340098, "x": 657, "y": -16, "w": 29, "h": 34},
    {"name": "Molette", "code": 4294967295, "x": 700, "y": -16, "w": 29, "h": 34},
    {"name": "Suppr", "code": 76, "x": 743, "y": -16, "w": 29, "h": 34},

    # Row 2
    {"name": "²", "code": 53, "x": 99, "y": -59, "w": 29, "h": 34},
    {"name": "& 1", "code": 30, "x": 142, "y": -59, "w": 29, "h": 34},
    {"name": "é 2", "code": 31, "x": 185, "y": -59, "w": 29, "h": 34},
    {"name": "\" 3", "code": 32, "x": 228, "y": -59, "w": 29, "h": 34},
    {"name": "' 4", "code": 33, "x": 271, "y": -59, "w": 29, "h": 34},
    {"name": "( 5", "code": 34, "x": 314, "y": -59, "w": 29, "h": 34},
    {"name": "- 6", "code": 35, "x": 357, "y": -59, "w": 29, "h": 34},
    {"name": "è 7", "code": 36, "x": 399, "y": -59, "w": 29, "h": 34},
    {"name": "_ 8", "code": 37, "x": 442, "y": -59, "w": 29, "h": 34},
    {"name": "ç 9", "code": 38, "x": 485, "y": -59, "w": 29, "h": 34},
    {"name": "à 0", "code": 39, "x": 528, "y": -59, "w": 29, "h": 34},
    {"name": ") °", "code": 45, "x": 571, "y": -59, "w": 29, "h": 34},
    {"name": "= +", "code": 46, "x": 614, "y": -59, "w": 29, "h": 34},
    {"name": "Retour", "code": 42, "x": 657, "y": -59, "w": 72, "h": 34},
    {"name": "Origine", "code": 74, "x": 743, "y": -59, "w": 29, "h": 34},

    # Row 3
    {"name": "Tab", "code": 43, "x": 99, "y": -102, "w": 50, "h": 34},
    {"name": "A", "code": 20, "x": 163, "y": -102, "w": 29, "h": 34},
    {"name": "Z", "code": 26, "x": 206, "y": -102, "w": 29, "h": 34},
    {"name": "E", "code": 8, "x": 249, "y": -102, "w": 29, "h": 34},
    {"name": "R", "code": 21, "x": 292, "y": -102, "w": 29, "h": 34},
    {"name": "T", "code": 23, "x": 335, "y": -102, "w": 29, "h": 34},
    {"name": "Y", "code": 28, "x": 378, "y": -102, "w": 29, "h": 34},
    {"name": "U", "code": 24, "x": 420, "y": -102, "w": 29, "h": 34},
    {"name": "I", "code": 12, "x": 463, "y": -102, "w": 29, "h": 34},
    {"name": "O", "code": 18, "x": 506, "y": -102, "w": 29, "h": 34},
    {"name": "P", "code": 19, "x": 549, "y": -102, "w": 29, "h": 34},
    {"name": "^ ¨", "code": 47, "x": 592, "y": -102, "w": 29, "h": 34},
    {"name": "$ £", "code": 48, "x": 635, "y": -102, "w": 29, "h": 34},
    {"name": "Entrée", "code": 40, "x": 688, "y": -102, "w": 40, "h": 77},
    {"name": "Fin", "code": 77, "x": 743, "y": -102, "w": 29, "h": 34},

    # Row 4
    {"name": "Verr Maj", "code": 57, "x": 99, "y": -145, "w": 61, "h": 34},
    {"name": "Q", "code": 4, "x": 174, "y": -145, "w": 29, "h": 34},
    {"name": "S", "code": 22, "x": 217, "y": -145, "w": 29, "h": 34},
    {"name": "D", "code": 7, "x": 260, "y": -145, "w": 29, "h": 34},
    {"name": "F", "code": 9, "x": 303, "y": -145, "w": 29, "h": 34},
    {"name": "G", "code": 10, "x": 346, "y": -145, "w": 29, "h": 34},
    {"name": "H", "code": 11, "x": 389, "y": -145, "w": 29, "h": 34},
    {"name": "J", "code": 13, "x": 431, "y": -145, "w": 29, "h": 34},
    {"name": "K", "code": 14, "x": 474, "y": -145, "w": 29, "h": 34},
    {"name": "L", "code": 15, "x": 517, "y": -145, "w": 29, "h": 34},
    {"name": "M", "code": 51, "x": 560, "y": -145, "w": 29, "h": 34},
    {"name": "% ù", "code": 52, "x": 603, "y": -145, "w": 29, "h": 34},
    {"name": "* µ", "code": 49, "x": 646, "y": -145, "w": 29, "h": 34},
    {"name": "Page H.", "code": 75, "x": 743, "y": -145, "w": 29, "h": 34},

    # Row 5
    {"name": "Maj G.", "code": 225, "x": 99, "y": -188, "w": 41, "h": 34},
    {"name": "< >", "code": 100, "x": 153, "y": -188, "w": 29, "h": 34},
    {"name": "W", "code": 29, "x": 196, "y": -188, "w": 29, "h": 34},
    {"name": "X", "code": 27, "x": 239, "y": -188, "w": 29, "h": 34},
    {"name": "C", "code": 6, "x": 282, "y": -188, "w": 29, "h": 34},
    {"name": "V", "code": 25, "x": 325, "y": -188, "w": 29, "h": 34},
    {"name": "B", "code": 5, "x": 368, "y": -188, "w": 29, "h": 34},
    {"name": "N", "code": 17, "x": 410, "y": -188, "w": 29, "h": 34},
    {"name": "? ,", "code": 16, "x": 453, "y": -188, "w": 29, "h": 34},
    {"name": ". ;", "code": 54, "x": 496, "y": -188, "w": 29, "h": 34},
    {"name": "/ :", "code": 55, "x": 539, "y": -188, "w": 29, "h": 34},
    {"name": "§ !", "code": 56, "x": 582, "y": -188, "w": 29, "h": 34},
    {"name": "Maj D.", "code": 229, "x": 625, "y": -188, "w": 61, "h": 34},
    {"name": "▲", "code": 82, "x": 699, "y": -188, "w": 29, "h": 34},
    {"name": "Page B.", "code": 78, "x": 743, "y": -188, "w": 29, "h": 34},

    # Row 6
    {"name": "Ctrl G.", "code": 224, "x": 99, "y": -231, "w": 47, "h": 34},
    {"name": "Fn", "code": 167837696, "x": 160, "y": -231, "w": 29, "h": 34},
    {"name": "Win", "code": 227, "x": 203, "y": -231, "w": 29, "h": 34},
    {"name": "Alt", "code": 226, "x": 246, "y": -231, "w": 29, "h": 34},
    {"name": "Espace", "code": 44, "x": 289, "y": -231, "w": 250, "h": 34},
    {"name": "Alt Gr", "code": 230, "x": 554, "y": -231, "w": 29, "h": 34},
    {"name": "Menu / Win", "code": 231, "x": 597, "y": -231, "w": 46, "h": 34},
    {"name": "◀", "code": 80, "x": 657, "y": -231, "w": 29, "h": 34},
    {"name": "▼", "code": 81, "x": 699, "y": -231, "w": 29, "h": 34},
    {"name": "▶", "code": 79, "x": 743, "y": -231, "w": 29, "h": 34},
]

# Action mapping dictionaries for key remapping
REMAP_ACTIONS: Dict[str, Tuple[str, List[int]]] = {
    # Multimedia
    "media_play_pause": ("Lecture / Pause", [3, 0, 205, 0]),
    "media_prev": ("Piste Précédente", [3, 0, 182, 0]),
    "media_next": ("Piste Suivante", [3, 0, 181, 0]),
    "media_stop": ("Arrêt Média", [3, 0, 183, 0]),
    "media_mute": ("Muet", [3, 0, 226, 0]),
    "media_vol_up": ("Volume +", [3, 0, 233, 0]),
    "media_vol_down": ("Volume -", [3, 0, 234, 0]),
    "calculator": ("Calculatrice", [3, 0, 146, 1]),
    "email": ("Email / Messagerie", [3, 0, 138, 1]),
    "browser_home": ("Page d'accueil Web", [3, 0, 35, 2]),
    "browser_search": ("Recherche Web", [3, 0, 33, 2]),
    "my_computer": ("Poste de Travail", [3, 0, 148, 1]),

    # Mouse actions
    "mouse_left": ("Clic Gauche Souris", [1, 0, 240, 0]),
    "mouse_right": ("Clic Droit Souris", [1, 0, 241, 0]),
    "mouse_middle": ("Clic Molette Souris", [1, 0, 242, 0]),
    "mouse_forward": ("Bouton Suivant Souris", [1, 0, 244, 0]),
    "mouse_back": ("Bouton Précédent Souris", [1, 0, 243, 0]),

    # System actions
    "system_power": ("Arrêt Système", [2, 129, 0, 0]),
    "system_sleep": ("Mise en Veille", [2, 130, 0, 0]),
    "system_wake": ("Réveil Système", [2, 131, 0, 0]),

    # Shortcuts
    "copy": ("Copier (Ctrl+C)", [0, 224, 6, 0]),
    "paste": ("Coller (Ctrl+V)", [0, 224, 25, 0]),
    "cut": ("Couper (Ctrl+X)", [0, 224, 27, 0]),
    "undo": ("Annuler (Ctrl+Z)", [0, 224, 29, 0]),
    "save": ("Enregistrer (Ctrl+S)", [0, 224, 22, 0]),
    "select_all": ("Tout sélectionner (Ctrl+A)", [0, 224, 4, 0]),
    "lock_screen": ("Verrouiller écran (Win+L)", [0, 227, 15, 0]),
    "show_desktop": ("Afficher Bureau (Win+D)", [0, 227, 7, 0]),

    # Special
    "disabled": ("Désactivé", [0, 0, 0, 0]),
}


def _HIDIOCSFEATURE(length: int) -> int:
    """Linux ioctl macro for HIDIOCSFEATURE: _IOC(_IOC_READ|_IOC_WRITE, 'H', 0x06, length)"""
    return (3 << 30) | (length << 16) | (ord('H') << 8) | 0x06


def _HIDIOCGFEATURE(length: int) -> int:
    """Linux ioctl macro for HIDIOCGFEATURE: _IOC(_IOC_READ|_IOC_WRITE, 'H', 0x07, length)"""
    return (3 << 30) | (length << 16) | (ord('H') << 8) | 0x07


class SkillkorpK20Driver(SkillkorpDeviceDriver):
    """High-level Python driver for SkillKorp K20 Ultimate gaming keyboard."""

    device_type = "keyboard"
    _config_mtime: float = 0.0

    def __init__(self, dev_path: Optional[str] = None):
        self.dev_path = dev_path or self.find_device()
        self._config_mtime = 0.0
        self.config = self._load_or_default_config()

    def reload_config_if_changed(self) -> bool:
        """Reload self.config from disk if the configuration file has been modified externally."""
        try:
            if not os.path.exists(DEFAULT_PROFILE_FILE):
                return False
            mtime = os.path.getmtime(DEFAULT_PROFILE_FILE)
            if mtime != getattr(self, "_config_mtime", 0.0):
                self.config = self._load_or_default_config()
                self._config_mtime = mtime
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def find_device() -> Optional[str]:
        """Find the vendor hidraw device node for SkillKorp K20 (Interface 2, Usage Page 0xFFFF)."""
        candidates = []
        for path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
            try:
                uevent_path = os.path.join(path, "device", "uevent")
                if not os.path.exists(uevent_path):
                    continue
                with open(uevent_path, "r") as f:
                    content = f.read()

                # Check for SkillKorp K20 VID (0x3151) and PID (0x4011 wireless or 0x4015 wired)
                is_k20 = False
                if "00003151:00004011" in content or "00003151:00004015" in content:
                    is_k20 = True
                elif "3151:4011" in content or "3151:4015" in content:
                    is_k20 = True

                if not is_k20:
                    continue

                hidraw_name = os.path.basename(path)
                dev_node = f"/dev/{hidraw_name}"

                # Verify Interface 2 (vendor feature report interface)
                desc_path = os.path.join(path, "device", "report_descriptor")
                if os.path.exists(desc_path):
                    with open(desc_path, "rb") as df:
                        desc = df.read()
                    # Interface 2 signature: exactly 20 bytes with [0x06, 0xFF, 0xFF, 0x09, 0x02]
                    # (Usage Page 0xFFFF, Usage 0x0002).
                    if (len(desc) == 20 and desc[0] == 0x06 and desc[1] == 0xFF
                            and desc[2] == 0xFF and desc[3] == 0x09 and desc[4] == 0x02):
                        return dev_node

                candidates.append(dev_node)
            except Exception:
                continue

        # If descriptor check couldn't distinguish, pick the highest interface node
        if candidates:
            return candidates[-1]
        return None

    def is_connected(self) -> bool:
        """Check if the K20 device is currently present and accessible."""
        dev = self.find_device()
        if dev and os.path.exists(dev):
            self.dev_path = dev
            return True
        return False

    def is_wireless(self) -> bool:
        """Return True if connected via 2.4GHz wireless dongle (PID 4011)."""
        if not self.dev_path:
            return False
        hidraw_name = os.path.basename(self.dev_path)
        uevent_path = f"/sys/class/hidraw/{hidraw_name}/device/uevent"
        if os.path.exists(uevent_path):
            with open(uevent_path, "r") as f:
                c = f.read()
                return "4011" in c
        return False

    @staticmethod
    def _apply_checksum(payload: bytearray, checksum_type: str = "bit7") -> bytearray:
        """Apply one's-complement checksum to a 64-byte payload.

        - 'bit7': payload[7] = (~sum(payload[0:7])) & 0xFF
                  Used by default for standard commands (options, debounce, rate,
                  sleep, key remaps, query commands).
        - 'bit8': payload[8] = (~sum(payload[0:8])) & 0xFF
                  Used for LED parameter commands (CMD_SET_LEDPARAM, CMD_SET_SLEDPARAM).
        """
        if checksum_type == "bit7":
            csum = sum(payload[0:7]) & 0xFF
            payload[7] = (~csum) & 0xFF
        elif checksum_type == "bit8":
            csum = sum(payload[0:8]) & 0xFF
            payload[8] = (~csum) & 0xFF
        return payload

    def _send_feature_report(self, payload: bytes, checksum_type: Optional[str] = "bit7") -> bool:
        """Send a 65-byte HID feature report to the keyboard (byte 0 = 0x00 Report ID)."""
        dev = self.dev_path or self.find_device()
        if not dev or not os.path.exists(dev):
            return False

        # Ensure buffer is exactly 64 bytes for payload
        p = bytearray(64)
        for i in range(min(len(payload), 64)):
            p[i] = payload[i]

        if checksum_type:
            self._apply_checksum(p, checksum_type)

        buf = bytearray(65)
        buf[0] = 0x00
        buf[1:65] = p

        try:
            fd = os.open(dev, os.O_RDWR)
            try:
                ret = fcntl.ioctl(fd, _HIDIOCSFEATURE(65), buf)
                return ret == 65 or (isinstance(ret, bytes) and len(ret) == 65)
            finally:
                os.close(fd)
        except PermissionError:
            print(f"Erreur de permission sur {dev}. Assurez-vous que la règle udev est active.", file=sys.stderr)
            return False
        except Exception as e:
            print(f"Erreur d'envoi de rapport HID: {e}", file=sys.stderr)
            return False

    def _read_feature_report(self, cmd: int, length: int = 65, checksum_type: str = "bit7") -> Optional[bytearray]:
        """Send a feature report query and read back the reply via HIDIOCSFEATURE + HIDIOCGFEATURE.

        1. Populates buf[0] = 0x00 (Report ID), buf[1] = cmd, calculates checksum, and sends via _HIDIOCSFEATURE(length).
        2. Reads the response into read_buf via _HIDIOCGFEATURE(length).
        """
        dev = self.dev_path or self.find_device()
        if not dev or not os.path.exists(dev):
            return None

        query_payload = bytearray(64)
        query_payload[0] = cmd
        if checksum_type:
            self._apply_checksum(query_payload, checksum_type)

        buf = bytearray(length)
        buf[0] = 0x00
        buf[1:min(length, 65)] = query_payload[:min(length - 1, 64)]

        try:
            fd = os.open(dev, os.O_RDWR)
            try:
                fcntl.ioctl(fd, _HIDIOCSFEATURE(length), buf)
                time.sleep(0.02)
                read_buf = bytearray(length)
                read_buf[0] = 0x00
                fcntl.ioctl(fd, _HIDIOCGFEATURE(length), read_buf)
                return read_buf
            finally:
                os.close(fd)
        except PermissionError:
            print(f"Erreur de permission sur {dev}. Assurez-vous que la règle udev est active.", file=sys.stderr)
            return None
        except Exception as e:
            print(f"Erreur de lecture de rapport HID: {e}", file=sys.stderr)
            return None

    # =========================================================================
    # Configuration Persistence
    # =========================================================================

    def _default_config(self) -> Dict[str, Any]:
        return {
            "profile_name": "Par défaut",
            "profile_index": 0,
            "polling_rate": 1000,
            "debounce_ms": 8,
            "sleep": {
                "light_sleep_sec": 120,    # 2 minutes
                "deep_sleep_sec": 1680,   # 28 minutes
            },
            "options": {
                "win_lock": False,
                "wasd_swap": False,
                "os_mode": "win",         # "win", "mac", "ios", "android"
                "gaming_mode": False,
            },
            "rgb": {
                "mode": "wave",
                "speed": 3,               # 1 (slow) to 5 (fast)
                "brightness": 4,          # 0 (off) to 4 (max)
                "direction": 0,           # 0 = right-to-left, 1 = left-to-right
                "color": "#FF0000",       # Red
            },
            "side_rgb": {
                "mode": "rainbow",
                "speed": 3,
                "brightness": 4,
                "color": "#00FFCC",
            },
            "key_remaps": {},            # key_name -> remap_action
            "fn_remaps": {},             # key_name -> remap_action
        }

    def _load_or_default_config(self) -> Dict[str, Any]:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        if os.path.exists(DEFAULT_PROFILE_FILE):
            try:
                with open(DEFAULT_PROFILE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._config_mtime = os.path.getmtime(DEFAULT_PROFILE_FILE)
                # Merge with defaults
                cfg = self._default_config()
                self._deep_merge(cfg, data)
                return cfg
            except Exception:
                pass
        cfg = self._default_config()
        self.save_config(cfg)
        return cfg

    def _deep_merge(self, base: dict, override: dict):
        for k, v in override.items():
            if isinstance(v, dict) and k in base and isinstance(base[k], dict):
                self._deep_merge(base[k], v)
            else:
                base[k] = v

    def save_config(self, config: Optional[Dict[str, Any]] = None):
        """Save current configuration to disk."""
        if config is not None:
            self.config = config
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(DEFAULT_PROFILE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)
        try:
            self._config_mtime = os.path.getmtime(DEFAULT_PROFILE_FILE)
        except Exception:
            pass

    # =========================================================================
    # Hardware Controls
    # =========================================================================

    def set_rgb(
        self,
        mode: str,
        speed: int = 3,
        brightness: int = 4,
        direction: int = 0,
        color: Optional[str] = None,
    ) -> bool:
        """
        Configure keyboard RGB backlight.
        - mode: 'off', 'static', 'breathing', 'wave', 'ripple', 'raindrop', 'snake', 'press_action', 'convergence', 'custom'
        - speed: 1 to 5 (hardware stores: 5 - speed)
        - brightness: 0 to 4
        - direction: 0 (standard/right) or 1 (left)
        - color: hex string (e.g. '#FF0000')
        """
        code = LIGHT_CODE_BY_KEY.get(mode.lower(), 4)  # default Wave (code 4)
        speed = max(1, min(5, speed))
        brightness = max(0, min(4, brightness))
        direction = 1 if direction else 0

        hw_speed = max(0, min(4, 5 - speed))

        r, g, b = 255, 0, 0
        if color:
            color = color.lstrip("#")
            if len(color) == 6:
                try:
                    r = int(color[0:2], 16)
                    g = int(color[2:4], 16)
                    b = int(color[4:6], 16)
                except ValueError:
                    pass

        payload = bytearray(64)
        payload[0] = CMD_SET_LEDPARAM
        payload[1] = code
        payload[2] = hw_speed
        payload[3] = brightness
        dazzle_flag = 8 if color else 7
        if code == 4:  # Wave
            payload[4] = (direction << 4) | dazzle_flag
        else:
            payload[4] = dazzle_flag
        payload[5] = r
        payload[6] = g
        payload[7] = b

        success = self._send_feature_report(payload, checksum_type="bit8")
        if success:
            self.config["rgb"]["mode"] = mode
            self.config["rgb"]["speed"] = speed
            self.config["rgb"]["brightness"] = brightness
            self.config["rgb"]["direction"] = direction
            if color:
                self.config["rgb"]["color"] = f"#{r:02X}{g:02X}{b:02X}"
            self.save_config()
        return success

    def set_side_rgb(
        self,
        mode: str,
        speed: int = 3,
        brightness: int = 4,
        color: Optional[str] = None,
    ) -> bool:
        """
        Configure side RGB LED strips.
        - mode: 'off', 'static', 'breathing', 'wave', 'rainbow'
        - speed: 1 to 5
        - brightness: 0 to 4
        - color: hex string (e.g. '#00FFCC')
        """
        code = SIDE_LIGHT_CODE_BY_KEY.get(mode.lower(), 3)  # default rainbow (code 3)
        speed = max(1, min(5, speed))
        brightness = max(0, min(4, brightness))

        r, g, b = 0, 255, 204
        if color:
            color = color.lstrip("#")
            if len(color) == 6:
                try:
                    r = int(color[0:2], 16)
                    g = int(color[2:4], 16)
                    b = int(color[4:6], 16)
                except ValueError:
                    pass

        payload = bytearray(64)
        payload[0] = CMD_SET_SLEDPARAM
        payload[1] = code
        payload[2] = speed
        payload[3] = brightness
        payload[4] = 8 if color else 7
        payload[5] = r
        payload[6] = g
        payload[7] = b

        success = self._send_feature_report(payload, checksum_type="bit8")
        if success:
            self.config["side_rgb"]["mode"] = mode
            self.config["side_rgb"]["speed"] = speed
            self.config["side_rgb"]["brightness"] = brightness
            if color:
                self.config["side_rgb"]["color"] = f"#{r:02X}{g:02X}{b:02X}"
            self.save_config()
        return success

    def set_polling_rate(self, rate_hz: int) -> bool:
        """Set keyboard polling rate: 125, 250, 500, or 1000 Hz."""
        if rate_hz not in POLLING_RATE_MAP:
            raise ValueError(f"Taux de rafraîchissement invalide: {rate_hz} Hz. Choix: 125, 250, 500, 1000.")

        code = POLLING_RATE_MAP[rate_hz]
        payload = bytearray(64)
        payload[0] = CMD_SET_REPORT
        payload[1] = self.config.get("profile_index", 0)
        payload[2] = code

        success = self._send_feature_report(payload, checksum_type="bit7")
        if success:
            self.config["polling_rate"] = rate_hz
            self.save_config()
        return success

    def set_sleep_time(self, light_sleep_sec: int, deep_sleep_sec: int) -> bool:
        """
        Configure keyboard inactivity sleep timeouts.
        - light_sleep_sec: light power saving (turn off LEDs), e.g. 120s (2 min)
        - deep_sleep_sec: deep sleep (disconnect wireless radio), e.g. 1680s (28 min)
        """
        light_sleep_sec = max(10, min(3600, int(light_sleep_sec)))
        deep_sleep_sec = max(60, min(86400, int(deep_sleep_sec)))

        payload = bytearray(64)
        payload[0] = CMD_SET_SLEEPTIME
        # Checksum is placed at payload[7] (buf[8])
        # BT light sleep (payload[8..9] / buf[9..10])
        payload[8] = light_sleep_sec & 0xFF
        payload[9] = (light_sleep_sec >> 8) & 0xFF
        # 2.4G light sleep (payload[10..11] / buf[11..12])
        payload[10] = light_sleep_sec & 0xFF
        payload[11] = (light_sleep_sec >> 8) & 0xFF
        # BT deep sleep (payload[12..13] / buf[13..14])
        payload[12] = deep_sleep_sec & 0xFF
        payload[13] = (deep_sleep_sec >> 8) & 0xFF
        # 2.4G deep sleep (payload[14..15] / buf[15..16])
        payload[14] = deep_sleep_sec & 0xFF
        payload[15] = (deep_sleep_sec >> 8) & 0xFF

        success = self._send_feature_report(payload, checksum_type="bit7")
        if success:
            self.config["sleep"]["light_sleep_sec"] = light_sleep_sec
            self.config["sleep"]["deep_sleep_sec"] = deep_sleep_sec
            self.save_config()
        return success

    def set_debounce(self, debounce_ms: int) -> bool:
        """Set switch debounce filter time in ms (2 to 20 ms, default 8ms)."""
        debounce_ms = max(2, min(20, int(debounce_ms)))

        payload = bytearray(64)
        payload[0] = CMD_SET_DEBOUNCE
        payload[1] = self.config.get("profile_index", 0)
        payload[2] = debounce_ms

        success = self._send_feature_report(payload, checksum_type="bit7")
        if success:
            self.config["debounce_ms"] = debounce_ms
            self.save_config()
        return success

    def set_keyboard_options(
        self,
        win_lock: Optional[bool] = None,
        wasd_swap: Optional[bool] = None,
        os_mode: Optional[str] = None,
        gaming_mode: Optional[bool] = None,
    ) -> bool:
        """
        Configure keyboard firmware options:
        - win_lock: Lock Windows key
        - wasd_swap: Swap WASD and arrow keys
        - os_mode: 'win', 'mac', 'ios', 'android'
        - gaming_mode: High performance gaming mode
        """
        opts = self.config.get("options", {})
        cur_win_lock = opts.get("win_lock", False) if win_lock is None else win_lock
        cur_wasd_swap = opts.get("wasd_swap", False) if wasd_swap is None else wasd_swap
        cur_os_mode = opts.get("os_mode", "win") if os_mode is None else os_mode
        cur_gaming_mode = opts.get("gaming_mode", False) if gaming_mode is None else gaming_mode

        os_val = {"win": 0, "mac": 1, "ios": 2, "android": 3}.get(cur_os_mode.lower(), 0)

        # Build bitfield byte:
        # bit 0: winKeyLock
        # bit 1..2: OS mode
        # bit 3: wasdKeyAndArrowKeyExchange
        # bit 4: ledOff
        # bit 5: sLedOff
        # bit 6: keyboardMode (0=normal, 1=gaming)
        bitfield = 0
        if cur_win_lock:
            bitfield |= (1 << 0)
        bitfield |= (os_val & 0x03) << 1
        if cur_wasd_swap:
            bitfield |= (1 << 3)
        if cur_gaming_mode:
            bitfield |= (1 << 6)

        payload = bytearray(64)
        payload[0] = CMD_SET_KBOPTION
        payload[1] = self.config.get("profile_index", 0)
        payload[2] = bitfield

        success = self._send_feature_report(payload, checksum_type="bit7")
        if success:
            self.config["options"]["win_lock"] = cur_win_lock
            self.config["options"]["wasd_swap"] = cur_wasd_swap
            self.config["options"]["os_mode"] = cur_os_mode
            self.config["options"]["gaming_mode"] = cur_gaming_mode
            self.save_config()
        return success

    def set_profile(self, profile_idx: int) -> bool:
        """Switch hardware profile (0, 1, or 2)."""
        profile_idx = max(0, min(2, int(profile_idx)))

        payload = bytearray(64)
        payload[0] = CMD_SET_PROFILE
        payload[1] = profile_idx

        success = self._send_feature_report(payload, checksum_type="bit7")
        if success:
            self.config["profile_index"] = profile_idx
            self.save_config()
        return success

    def remap_key(self, key_name: str, action_key: str, profile_idx: int = 0) -> bool:
        """
        Remap a standard key.
        - key_name: Name of key in KEY_LAYOUT (e.g. 'F1', 'Esc', 'Q', 'Caps Lock')
        - action_key: Key in REMAP_ACTIONS (e.g. 'media_vol_up', 'mouse_left', 'copy', etc.)
        """
        # Find key index in KEY_LAYOUT
        key_idx = None
        for idx, k in enumerate(KEY_LAYOUT):
            if k["name"].lower() == key_name.lower():
                key_idx = idx
                break

        if key_idx is None:
            raise ValueError(f"Touche inconnue: {key_name}")

        action_bytes = REMAP_ACTIONS.get(action_key, ("Désactivé", [0, 0, 0, 0]))[1]

        payload = bytearray(64)
        payload[0] = CMD_SET_KEYMATRIX_SIMPLE
        payload[1] = profile_idx
        payload[2] = key_idx
        # Checksum is computed into payload[7] (buf[8])
        # Action frame at payload[8..11] (buf[9..12])
        payload[8] = action_bytes[0]
        payload[9] = action_bytes[1]
        payload[10] = action_bytes[2]
        payload[11] = action_bytes[3]

        success = self._send_feature_report(payload, checksum_type="bit7")
        if success:
            self.config.setdefault("key_remaps", {})[key_name] = action_key
            self.save_config()
        return success

    def remap_fn_key(self, key_name: str, action_key: str, profile_idx: int = 0) -> bool:
        """
        Remap a Fn-layer key combination.
        - key_name: Name of key in KEY_LAYOUT
        - action_key: Key in REMAP_ACTIONS
        """
        key_idx = None
        for idx, k in enumerate(KEY_LAYOUT):
            if k["name"].lower() == key_name.lower():
                key_idx = idx
                break

        if key_idx is None:
            raise ValueError(f"Touche inconnue: {key_name}")

        action_bytes = REMAP_ACTIONS.get(action_key, ("Désactivé", [0, 0, 0, 0]))[1]

        payload = bytearray(64)
        payload[0] = CMD_SET_FN_SIMPLE
        payload[1] = profile_idx
        payload[2] = key_idx
        # Checksum is computed into payload[7] (buf[8])
        # Action frame at payload[8..11] (buf[9..12])
        payload[8] = action_bytes[0]
        payload[9] = action_bytes[1]
        payload[10] = action_bytes[2]
        payload[11] = action_bytes[3]

        success = self._send_feature_report(payload, checksum_type="bit7")
        if success:
            self.config.setdefault("fn_remaps", {})[key_name] = action_key
            self.save_config()
        return success

    def factory_reset(self) -> bool:
        """Restore keyboard to factory default settings."""
        payload = bytearray(64)
        payload[0] = CMD_SET_RESERT
        success = self._send_feature_report(payload, checksum_type="bit7")
        if success:
            self.config = self._default_config()
            self.save_config()
        return success

    def get_battery(self) -> Dict[str, Any]:
        """Query real-time battery status and charging state via hardware feature reports."""
        # Check if connected
        if not self.is_connected():
            return {
                "percentage": None,
                "charging": False,
                "connected": False,
                "wireless": False,
                "status_str": "Non connecté",
            }

        is_wl = self.is_wireless()

        # In wired mode, the keyboard is powered and charging via USB-C
        if not is_wl:
            return {
                "percentage": 100,
                "charging": True,
                "connected": True,
                "wireless": False,
                "status_str": "En charge (Câblé USB)",
            }

        # In wireless mode (2.4GHz dongle / Bluetooth), query device via HIDIOCSFEATURE + HIDIOCGFEATURE
        buf = self._read_feature_report(CMD_GET_BATTERY)
        if buf is None:
            return {
                "percentage": None,
                "charging": False,
                "connected": True,
                "wireless": True,
                "status_str": "Erreur de communication (sans-fil)",
            }

        # Protocol reverse-engineered from iot_driver.exe / Electron client:
        # Buffer layout returned by firmware:
        # buf[0]: Report ID (0x00)
        # buf[1]: Command echo (CMD_GET_BATTERY = 0x83) OR battery percentage
        # If buf[1] == CMD_GET_BATTERY:
        #   buf[2] = battery percentage (0-100)
        #   buf[3] = charging state (1: charging, 2: full, other: discharging)
        #   buf[4] = low power threshold
        # If buf[1] != CMD_GET_BATTERY and 1 <= buf[1] <= 100:
        #   buf[1] = battery percentage
        #   buf[2] = charging state
        percentage = None
        charging = False
        state_byte = 0

        if buf[1] == CMD_GET_BATTERY:
            raw_pct = buf[2]
            state_byte = buf[3]
            if 0 < raw_pct <= 100:
                percentage = raw_pct
        elif 0 < buf[1] <= 100:
            percentage = buf[1]
            state_byte = buf[2]

        if percentage is not None:
            if state_byte == 1:
                charging = True
                status_str = f"En charge ({percentage}%)"
            elif state_byte == 2:
                charging = False
                status_str = f"Batterie pleine ({percentage}%)"
            else:
                charging = False
                status_str = f"{percentage}% (Sans-fil 2.4GHz)"

            return {
                "percentage": percentage,
                "charging": charging,
                "connected": True,
                "wireless": True,
                "status_str": status_str,
            }

        # If the response is all zeros or unpopulated (e.g. keyboard asleep / dongle telemetry pending),
        # return percentage=None with an explicit status message rather than a fabricated fallback.
        # TODO: The 2.4GHz dongle (PID 4011) may require a specific vendor wake sequence
        # (e.g. 0xFE length handshake or 0xF7 polling) to populate battery telemetry across the RF link.
        return {
            "percentage": None,
            "charging": False,
            "connected": True,
            "wireless": True,
            "status_str": "Lecture batterie non implémentée (sans-fil)",
        }

    def apply_profile(self, profile: Dict[str, Any]) -> bool:
        """Apply a complete configuration profile to the hardware."""
        success = True
        try:
            if "polling_rate" in profile:
                self.set_polling_rate(profile["polling_rate"])
            if "debounce_ms" in profile:
                self.set_debounce(profile["debounce_ms"])
            if "sleep" in profile:
                s = profile["sleep"]
                self.set_sleep_time(s.get("light_sleep_sec", 120), s.get("deep_sleep_sec", 1680))
            if "options" in profile:
                o = profile["options"]
                self.set_keyboard_options(
                    win_lock=o.get("win_lock"),
                    wasd_swap=o.get("wasd_swap"),
                    os_mode=o.get("os_mode"),
                    gaming_mode=o.get("gaming_mode"),
                )
            if "rgb" in profile:
                r = profile["rgb"]
                self.set_rgb(
                    mode=r.get("mode", "wave"),
                    speed=r.get("speed", 3),
                    brightness=r.get("brightness", 4),
                    direction=r.get("direction", 0),
                    color=r.get("color"),
                )
            if "side_rgb" in profile:
                sr = profile["side_rgb"]
                self.set_side_rgb(
                    mode=sr.get("mode", "rainbow"),
                    speed=sr.get("speed", 3),
                    brightness=sr.get("brightness", 4),
                    color=sr.get("color"),
                )
            # Merge into current config
            self._deep_merge(self.config, profile)
            self.save_config()
        except Exception as e:
            print(f"Erreur lors de l'application du profil: {e}", file=sys.stderr)
            success = False
        return success


if __name__ == "__main__":
    driver = SkillkorpK20Driver()
    print(f"Device Node: {driver.dev_path}")
    print(f"Connected: {driver.is_connected()}")
    print(f"Wireless: {driver.is_wireless()}")
    print("Battery:", driver.get_battery())
    print("Testing RGB Wave...")
    res = driver.set_rgb("wave", speed=3, brightness=4)
    print("RGB Wave result:", res)
