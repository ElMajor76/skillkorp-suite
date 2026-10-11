#!/usr/bin/env python3
"""
SkillKorp Suite - Unified Multi-Profile Manager & Auto-Switching

Manages persistent configuration profiles and automatic switching based on
the active application, for either the keyboard or the mouse. Each device
type keeps its own profile schema and storage location (a keyboard profile
and a mouse profile are never interchangeable), but both are handled by
this single ProfileManager class parameterized by `device_type`.

Storage layout:
    ~/.config/skillkorp/keyboard/profiles/*.json
    ~/.config/skillkorp/keyboard/profile_state.json
    ~/.config/skillkorp/mouse/profiles/*.json
    ~/.config/skillkorp/mouse/profile_state.json

All writes are atomic and every profile is validated (on import, on save and
before being applied), so a hand-edited or truncated file can never push
garbage to the hardware. `AutoSwitcher` implements the per-application
switching shared by `skillkorpctl daemon` and the tray.
"""

import copy
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from skillkorp import config_store

CONFIG_DIR = os.path.expanduser("~/.config/skillkorp")

AUTOSTART_DIR = os.path.expanduser("~/.config/autostart")
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "io.github.skillkorp.suite.tray.desktop")

VALID_DEVICE_TYPES = ("keyboard", "mouse")
PROFILE_ID_RE = re.compile(r"[^a-z0-9_\-]")


AUTOSTART_ENTRY = """[Desktop Entry]
Type=Application
Name=SkillKorp Suite Tray
Comment=Indicateur de batterie et raccourcis SkillKorp (clavier K20 & souris M20)
Exec=skillkorp-tray
Icon=skillkorp-suite
Terminal=false
Categories=Utility;
StartupNotify=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=5
"""


def system_autostart_files() -> List[str]:
    """Autostart entries installed by the packages (/etc/xdg/autostart)."""
    dirs = os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg"
    name = os.path.basename(AUTOSTART_FILE)
    return [os.path.join(d, "autostart", name) for d in dirs.split(":") if d]


def _entry_enabled(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = [line.strip().lower() for line in f]
    except OSError:
        return False
    return "hidden=true" not in lines and "x-gnome-autostart-enabled=false" not in lines


def is_autostart_enabled() -> bool:
    # A user entry (same file name) overrides the system one, as in the XDG spec.
    if os.path.exists(AUTOSTART_FILE):
        return _entry_enabled(AUTOSTART_FILE)
    return any(_entry_enabled(p) for p in system_autostart_files())


def set_autostart(enabled: bool):
    system = any(os.path.exists(p) for p in system_autostart_files())
    if enabled:
        content = AUTOSTART_ENTRY
    elif system:
        content = AUTOSTART_ENTRY + "Hidden=true\n"  # masks the packaged entry
    else:
        if os.path.exists(AUTOSTART_FILE):
            os.remove(AUTOSTART_FILE)
        return
    os.makedirs(AUTOSTART_DIR, exist_ok=True)
    tmp = AUTOSTART_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, AUTOSTART_FILE)


def sanitize_profile_id(prof_id: str) -> str:
    safe = PROFILE_ID_RE.sub("_", str(prof_id).strip().lower())
    if not safe or safe.strip("_-") == "":
        raise ValueError(f"Identifiant de profil invalide: {prof_id!r}")
    return safe[:64]


# Default profiles created on initial setup, per device type.
DEFAULT_PROFILES_KEYBOARD: Dict[str, Dict[str, Any]] = {
    "default": {
        "id": "default",
        "device_type": "keyboard",
        "name": "Par défaut",
        "description": "Configuration standard polyvalente (1000 Hz, effet Onde RGB)",
        "polling_rate": 1000,
        "debounce_ms": 8,
        "sleep": {
            "light_sleep_sec": 120,
            "deep_sleep_sec": 1680,
        },
        "options": {
            "win_lock": False,
            "wasd_swap": False,
            "os_mode": "win",
            "gaming_mode": False,
        },
        "rgb": {
            "mode": "wave",
            "speed": 3,
            "brightness": 4,
            "direction": 0,
            "color": "#FF0000",
            "colorful": True,
        },
        "side_rgb": {
            "mode": "rainbow",
            "speed": 3,
            "brightness": 4,
            "color": "#00FFCC",
        },
        "key_remaps": {},
        "fn_remaps": {},
        "auto_switch_apps": [],
    },
    "gaming": {
        "id": "gaming",
        "device_type": "keyboard",
        "name": "Gaming / Compétition",
        "description": "Performances maximales (1000 Hz, anti-rebond 2 ms, verrouillage touche Windows activé)",
        "polling_rate": 1000,
        "debounce_ms": 2,
        "sleep": {
            "light_sleep_sec": 300,
            "deep_sleep_sec": 3600,
        },
        "options": {
            "win_lock": True,
            "wasd_swap": False,
            "os_mode": "win",
            "gaming_mode": True,
        },
        "rgb": {
            "mode": "wave",
            "speed": 4,
            "brightness": 4,
            "direction": 0,
            "color": "#FF0055",
            "colorful": True,
        },
        "side_rgb": {
            "mode": "rainbow",
            "speed": 4,
            "brightness": 4,
            "color": "#FF0055",
        },
        "key_remaps": {},
        "fn_remaps": {},
        "auto_switch_apps": ["steam", "lutris", "heroic", "cs2", "valorant", "dota2", "overwatch"],
    },
    "bureau": {
        "id": "bureau",
        "device_type": "keyboard",
        "name": "Bureautique / Travail",
        "description": "Confort de frappe et économie d'énergie (500 Hz, éclairage discret)",
        "polling_rate": 500,
        "debounce_ms": 10,
        "sleep": {
            "light_sleep_sec": 120,
            "deep_sleep_sec": 1200,
        },
        "options": {
            "win_lock": False,
            "wasd_swap": False,
            "os_mode": "win",
            "gaming_mode": False,
        },
        "rgb": {
            "mode": "breathing",
            "speed": 2,
            "brightness": 2,
            "direction": 0,
            "color": "#0088FF",
        },
        "side_rgb": {
            "mode": "static",
            "speed": 2,
            "brightness": 2,
            "color": "#0088FF",
        },
        "key_remaps": {},
        "fn_remaps": {},
        "auto_switch_apps": ["code", "libreoffice", "firefox", "chromium", "thunderbird", "slack"],
    },
    "eco": {
        "id": "eco",
        "device_type": "keyboard",
        "name": "Nuit & Économie d'énergie",
        "description": "Autonomie maximale en sans-fil (125 Hz, luminosité faible, mise en veille rapide)",
        "polling_rate": 125,
        "debounce_ms": 12,
        "sleep": {
            "light_sleep_sec": 60,
            "deep_sleep_sec": 600,
        },
        "options": {
            "win_lock": False,
            "wasd_swap": False,
            "os_mode": "win",
            "gaming_mode": False,
        },
        "rgb": {
            "mode": "static",
            "speed": 1,
            "brightness": 1,
            "direction": 0,
            "color": "#FFAA00",
        },
        "side_rgb": {
            "mode": "off",
            "speed": 1,
            "brightness": 0,
            "color": "#000000",
        },
        "key_remaps": {},
        "fn_remaps": {},
        "auto_switch_apps": [],
    },
}

DEFAULT_PROFILES_MOUSE: Dict[str, Dict[str, Any]] = {
    "default": {
        "id": "default",
        "device_type": "mouse",
        "name": "Par défaut",
        "description": "Profil polyvalent pour usage quotidien",
        "dpi_stages": [400, 800, 1600, 3200, 6400, 26000],
        "active_stage": 2,
        "polling_rate": 1000,
        "lod": 1,
        "debounce": 4,
        "motion_sync": True,
        "angle_snap": False,
        "ripple": False,
        "sleep_timer_minutes": 10,
        "led_off_minutes": 1,
        "light_mode": "static",
        "brightness": 8,
        "speed": 6,
        "light_color": [255, 0, 0],
        "buttons": {
            "1": "left_click",
            "2": "right_click",
            "3": "middle_click",
            "4": "forward",
            "5": "backward",
            "6": "dpi_cycle",
        },
        "auto_switch_apps": [],
    },
    "gaming_fps": {
        "id": "gaming_fps",
        "device_type": "mouse",
        "name": "Gaming FPS",
        "description": "Optimisé pour CS2, Valorant, Apex (1000Hz, 800 DPI, LOD 1mm)",
        "dpi_stages": [400, 800, 1600, 3200],
        "active_stage": 2,
        "polling_rate": 1000,
        "lod": 1,
        "debounce": 2,
        "motion_sync": True,
        "angle_snap": False,
        "ripple": False,
        "sleep_timer_minutes": 10,
        "led_off_minutes": 1,
        "light_mode": "breathing",
        "brightness": 6,
        "speed": 6,
        "light_color": [255, 64, 0],
        "buttons": {
            "1": "left_click",
            "2": "right_click",
            "3": "middle_click",
            "4": "forward",
            "5": "backward",
            "6": "dpi_cycle",
        },
        "auto_switch_apps": [
            "cs2",
            "csgo_linux64",
            "valorant",
            "apex",
            "tf2",
            "hl2_linux",
            "steam_app",
            "lutris",
            "heroic",
        ],
    },
    "office_economy": {
        "id": "office_economy",
        "device_type": "mouse",
        "name": "Bureautique & Autonomie",
        "description": "Économie de batterie maximale (250Hz, raccourcis Copier/Coller)",
        "dpi_stages": [800, 1200, 1600],
        "active_stage": 2,
        "polling_rate": 250,
        "lod": 2,
        "debounce": 8,
        "motion_sync": False,
        "angle_snap": False,
        "ripple": False,
        "sleep_timer_minutes": 3,
        "led_off_minutes": 1,
        "light_mode": "static",
        "brightness": 2,
        "speed": 6,
        "light_color": [0, 120, 255],
        "buttons": {
            "1": "left_click",
            "2": "right_click",
            "3": "middle_click",
            "4": "copy",
            "5": "paste",
            "6": "dpi_cycle",
        },
        "auto_switch_apps": [
            "code",
            "firefox",
            "google-chrome",
            "chromium",
            "soffice.bin",
            "libreoffice",
            "blender",
            "gimp",
            "inkscape",
        ],
    },
}



_DEFAULT_PROFILES_BY_TYPE = {
    "keyboard": DEFAULT_PROFILES_KEYBOARD,
    "mouse": DEFAULT_PROFILES_MOUSE,
}

VALID_RATES = (125, 250, 500, 1000)


def _check_int(data: Dict[str, Any], key: str, lo: int, hi: int, errors: List[str], label: Optional[str] = None):
    if key not in data or data[key] is None:
        return
    v = data[key]
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        errors.append(f"{label or key} invalide: {v!r} (attendu {lo}..{hi})")


def _check_bool(data: Dict[str, Any], key: str, errors: List[str]):
    if key in data and data[key] is not None and not isinstance(data[key], bool):
        errors.append(f"{key} doit valoir true ou false (reçu {data[key]!r})")


def _check_color(value, label: str, errors: List[str]):
    if value is None:
        return
    if isinstance(value, str):
        if not re.fullmatch(r"#?[0-9A-Fa-f]{6}", value.strip()):
            errors.append(f"{label}: couleur invalide {value!r}")
    elif isinstance(value, (list, tuple)):
        if len(value) != 3 or not all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255 for c in value):
            errors.append(f"{label}: couleur invalide {value!r}")
    else:
        errors.append(f"{label}: couleur invalide {value!r}")


def validate_profile(device_type: str, data: Any) -> List[str]:
    """Return a list of human readable problems (empty if the profile is valid)."""
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["Le profil doit être un objet JSON."]
    if data.get("device_type") not in (None, device_type):
        errors.append(f"Ce profil est destiné au périphérique '{data.get('device_type')}', pas '{device_type}'.")
    if "polling_rate" in data and data["polling_rate"] not in VALID_RATES:
        errors.append(f"polling_rate invalide: {data['polling_rate']!r}. Valeurs acceptées: {list(VALID_RATES)}.")
    apps = data.get("auto_switch_apps", [])
    if not isinstance(apps, list) or not all(isinstance(a, str) for a in apps):
        errors.append("auto_switch_apps doit être une liste de chaînes.")

    if device_type == "keyboard":
        from skillkorp.devices import keyboard as kb
        _check_int(data, "debounce_ms", kb.DEBOUNCE_RANGE[0], kb.DEBOUNCE_RANGE[1], errors)
        sleep = data.get("sleep")
        if sleep is not None:
            if not isinstance(sleep, dict):
                errors.append("sleep doit être un objet.")
            else:
                _check_int(sleep, "light_sleep_sec", 0, 86400, errors)
                _check_int(sleep, "deep_sleep_sec", 0, 86400, errors)
        opts = data.get("options")
        if opts is not None:
            if not isinstance(opts, dict):
                errors.append("options doit être un objet.")
            else:
                for key in ("win_lock", "wasd_swap", "gaming_mode"):
                    _check_bool(opts, key, errors)
                if opts.get("os_mode") not in (None,) + kb.OS_MODES:
                    errors.append(f"os_mode invalide: {opts.get('os_mode')!r} (win ou mac).")
        for section, table in (("rgb", kb.LIGHT_CODE_BY_KEY), ("side_rgb", kb.SIDE_LIGHT_CODE_BY_KEY)):
            led = data.get(section)
            if led is None:
                continue
            if not isinstance(led, dict):
                errors.append(f"{section} doit être un objet.")
                continue
            if led.get("mode") is not None and led.get("mode") not in table:
                errors.append(f"{section}.mode invalide: {led.get('mode')!r}.")
            _check_int(led, "speed", 1, 5, errors, f"{section}.speed")
            _check_int(led, "brightness", 0, 4, errors, f"{section}.brightness")
            _check_int(led, "direction", 0, 3, errors, f"{section}.direction")
            _check_bool(led, "colorful", errors)
            _check_color(led.get("color"), f"{section}.color", errors)
        for section in ("key_remaps", "fn_remaps"):
            remaps = data.get(section)
            if remaps is None:
                continue
            if not isinstance(remaps, dict):
                errors.append(f"{section} doit être un objet.")
                continue
            for key_name, action in remaps.items():
                if key_name not in kb.KEY_MATRIX_INDEX:
                    errors.append(f"{section}: touche inconnue {key_name!r}.")
                if action not in kb.REMAP_ACTIONS:
                    errors.append(f"{section}: action inconnue {action!r} pour {key_name}.")
    else:
        from skillkorp.devices import mouse as ms
        stages = data.get("dpi_stages")
        if stages is not None:
            if not isinstance(stages, list) or not 1 <= len(stages) <= ms.MAX_STAGES:
                errors.append(f"dpi_stages doit être une liste de 1 à {ms.MAX_STAGES} valeurs.")
            else:
                for s in stages:
                    if isinstance(s, bool) or not isinstance(s, int) or not ms.DPI_RANGE[0] <= s <= ms.DPI_RANGE[1]:
                        errors.append(f"DPI invalide: {s!r} (plage: {ms.DPI_RANGE[0]}–{ms.DPI_RANGE[1]}).")
        n_stages = len(stages) if isinstance(stages, list) and stages else 6
        _check_int(data, "active_stage", 1, n_stages, errors)
        _check_int(data, "lod", 1, 2, errors)
        _check_int(data, "debounce", ms.DEBOUNCE_RANGE[0], ms.DEBOUNCE_RANGE[1], errors)
        _check_int(data, "brightness", ms.BRIGHTNESS_RANGE[0], ms.BRIGHTNESS_RANGE[1], errors)
        _check_int(data, "speed", 1, ms.SPEED_RANGE[1], errors)
        _check_int(data, "sleep_timer_minutes", ms.SLEEP_RANGE[0], ms.SLEEP_RANGE[1], errors)
        _check_int(data, "led_off_minutes", ms.LED_OFF_RANGE[0], ms.LED_OFF_RANGE[1], errors)
        for key in ("motion_sync", "angle_snap", "ripple"):
            _check_bool(data, key, errors)
        if data.get("light_mode") is not None and data.get("light_mode") not in ms.LIGHT_CODE_BY_KEY:
            errors.append(f"light_mode invalide: {data.get('light_mode')!r}.")
        _check_color(data.get("light_color"), "light_color", errors)
        buttons = data.get("buttons", {})
        if not isinstance(buttons, dict):
            errors.append("'buttons' doit être un objet JSON.")
        else:
            for btn_key, action in buttons.items():
                try:
                    btn_num = int(btn_key)
                except (ValueError, TypeError):
                    errors.append(f"Clé de bouton invalide: {btn_key!r} (doit être un entier).")
                    continue
                if not 1 <= btn_num <= 6:
                    errors.append(f"Numéro de bouton invalide: {btn_key} (1–6).")
                if action not in ms.BUTTON_ACTIONS:
                    errors.append(f"Action invalide pour le bouton {btn_key}: '{action}'.")
            if buttons and "left_click" not in buttons.values() and len(buttons) >= 6:
                errors.append("Au moins un bouton doit rester en 'left_click'.")
    return errors


def upgrade_profile(device_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Bring a profile written by an older version up to the current schema."""
    data = copy.deepcopy(data)
    if device_type == "mouse":
        data.pop("move_to_wake", None)
        data.setdefault("led_off_minutes", 1)
        if isinstance(data.get("speed"), int) and data["speed"] < 4:
            data["speed"] = 4
    else:
        sleep = data.get("sleep")
        if isinstance(sleep, dict):
            for key, lo in (("light_sleep_sec", 60), ("deep_sleep_sec", 600)):
                v = sleep.get(key)
                if isinstance(v, int) and 0 < v < lo:
                    sleep[key] = lo
        opts = data.get("options")
        if isinstance(opts, dict) and opts.get("os_mode") in ("ios", "android"):
            opts["os_mode"] = "win"
    return data


class ProfileManager:
    """Multi-profile storage, active profile state and hardware sync for one device type."""

    def __init__(self, device_type: str, profiles_dir: Optional[str] = None, state_file: Optional[str] = None):
        if device_type not in VALID_DEVICE_TYPES:
            raise ValueError(f"device_type invalide: {device_type!r}. Attendu: {VALID_DEVICE_TYPES}.")
        self.device_type = device_type
        device_dir = os.path.join(CONFIG_DIR, device_type)
        self.profiles_dir = profiles_dir or os.path.join(device_dir, "profiles")
        self.state_file = state_file or os.path.join(device_dir, "profile_state.json")
        self._default_profiles = _DEFAULT_PROFILES_BY_TYPE[device_type]
        self.last_apply_ok: Optional[bool] = None
        self._ensure_init()

    # -- storage ------------------------------------------------------------
    def _ensure_init(self):
        os.makedirs(self.profiles_dir, exist_ok=True)
        for prof_id, prof_data in self._default_profiles.items():
            path = self._get_profile_path(prof_id)
            if not os.path.exists(path):
                config_store.save_json_atomic(path, prof_data)
        if not os.path.exists(self.state_file):
            self._save_state({"active_profile": "default", "auto_switch_enabled": True})

    def _get_profile_path(self, prof_id: str) -> str:
        safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", prof_id)
        return os.path.join(self.profiles_dir, f"{safe_id}.json")

    def _load_state(self) -> Dict[str, Any]:
        return config_store.load_json(self.state_file,
                                      lambda: {"active_profile": "default", "auto_switch_enabled": True})

    def _save_state(self, state: Dict[str, Any]):
        config_store.save_json_atomic(self.state_file, state)

    def _update_state(self, mutator: Callable[[Dict[str, Any]], None]):
        with config_store.locked(self.state_file):
            st = self._load_state()
            mutator(st)
            self._save_state(st)

    # -- state ----------------------------------------------------------------
    def is_auto_switch_enabled(self) -> bool:
        return bool(self._load_state().get("auto_switch_enabled", True))

    def set_auto_switch_enabled(self, enabled: bool):
        self._update_state(lambda st: st.__setitem__("auto_switch_enabled", bool(enabled)))

    def get_active_profile_id(self) -> str:
        active_id = self._load_state().get("active_profile", "default")
        if not isinstance(active_id, str) or not os.path.exists(self._get_profile_path(active_id)):
            return "default"
        return active_id

    # -- CRUD -----------------------------------------------------------------
    def list_profiles(self) -> List[Dict[str, Any]]:
        active_id = self.get_active_profile_id()
        profiles = []
        if os.path.isdir(self.profiles_dir):
            for fname in sorted(os.listdir(self.profiles_dir)):
                if fname.endswith(".json") and not fname.startswith("."):
                    prof_id = fname[:-5]
                    prof = self.get_profile(prof_id)
                    if prof:
                        prof["active"] = (prof_id == active_id)
                        profiles.append(prof)
        return profiles

    def get_profile(self, prof_id: str) -> Optional[Dict[str, Any]]:
        path = self._get_profile_path(prof_id)
        if not os.path.exists(path):
            return None
        data = config_store.load_json(path, lambda: None)
        if not isinstance(data, dict):
            if prof_id in self._default_profiles:  # self-heal a broken built-in profile
                data = copy.deepcopy(self._default_profiles[prof_id])
                config_store.save_json_atomic(path, data)
            else:
                return None
        data = upgrade_profile(self.device_type, data)
        data["id"] = prof_id
        data.setdefault("device_type", self.device_type)
        data.setdefault("name", prof_id)
        if not isinstance(data.get("auto_switch_apps"), list):
            data["auto_switch_apps"] = []
        return data

    def save_profile(self, prof_id: str, data: Dict[str, Any]):
        data = dict(data)
        data.pop("active", None)
        data["id"] = prof_id
        data["device_type"] = self.device_type
        errors = validate_profile(self.device_type, data)
        if errors:
            raise ValueError("Profil invalide: " + "; ".join(errors))
        config_store.save_json_atomic(self._get_profile_path(prof_id), data)

    def create_profile(self, prof_id: str, name: str, copy_from: Optional[str] = None) -> Dict[str, Any]:
        safe_id = sanitize_profile_id(prof_id)
        if os.path.exists(self._get_profile_path(safe_id)):
            raise ValueError(f"Le profil '{safe_id}' existe déjà.")
        source = self.get_profile(copy_from) if copy_from else self.get_profile("default")
        if copy_from and not source:
            raise ValueError(f"Profil source '{copy_from}' introuvable.")
        if not source:
            source = copy.deepcopy(self._default_profiles["default"])
        new_data = json.loads(json.dumps(source))
        new_data.pop("active", None)
        new_data["id"] = safe_id
        new_data["name"] = name or safe_id
        new_data["description"] = f"Profil personnalisé : {name or safe_id}"
        new_data["auto_switch_apps"] = []
        self.save_profile(safe_id, new_data)
        return new_data

    def delete_profile(self, prof_id: str, driver: Optional[Any] = None) -> bool:
        if prof_id == "default":
            raise ValueError("Le profil 'default' ne peut pas être supprimé.")
        path = self._get_profile_path(prof_id)
        if not os.path.exists(path):
            return False
        was_active = self.get_active_profile_id() == prof_id
        os.remove(path)
        if was_active:
            self.switch_profile("default", driver=driver)
        return True

    def export_profile(self, prof_id: str, target_file: str) -> bool:
        prof = self.get_profile(prof_id)
        if not prof:
            raise ValueError(f"Profil '{prof_id}' introuvable.")
        prof.pop("active", None)
        config_store.save_json_atomic(os.path.expanduser(target_file), prof)
        return True

    def import_profile(self, source_file: str, new_id: Optional[str] = None) -> str:
        """Import a profile from a JSON file, with per-device-type validation."""
        with open(os.path.expanduser(source_file), "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except ValueError as e:
                raise ValueError(f"Fichier JSON invalide: {e}") from None
        if not isinstance(data, dict):
            raise ValueError("Le fichier de profil doit être un objet JSON.")
        data = upgrade_profile(self.device_type, data)
        errors = validate_profile(self.device_type, data)
        if errors:
            raise ValueError("; ".join(errors))
        prof_id = new_id or data.get("id") or os.path.splitext(os.path.basename(source_file))[0]
        safe_id = sanitize_profile_id(prof_id)
        data["id"] = safe_id
        data.setdefault("name", safe_id)
        self.save_profile(safe_id, data)
        return safe_id

    def set_auto_switch_apps(self, prof_id: str, apps: List[str]):
        prof = self.get_profile(prof_id)
        if not prof:
            raise ValueError(f"Profil '{prof_id}' introuvable.")
        prof["auto_switch_apps"] = [a.strip().lower() for a in apps if a and a.strip()]
        self.save_profile(prof_id, prof)

    # -- activation -------------------------------------------------------------
    def switch_profile(self, prof_id: str, driver: Optional[Any] = None) -> Dict[str, Any]:
        prof = self.get_profile(prof_id)
        if not prof:
            raise ValueError(f"Profil '{prof_id}' introuvable.")
        errors = validate_profile(self.device_type, prof)
        if errors:
            raise ValueError(f"Profil '{prof_id}' invalide: " + "; ".join(errors))
        self._update_state(lambda st: st.__setitem__("active_profile", prof_id))
        self.last_apply_ok = None
        if driver is not None:
            self.last_apply_ok = self.apply_profile_to_driver(prof, driver)
        return prof

    def apply_profile_to_driver(self, prof: Dict[str, Any], driver: Any) -> bool:
        """Apply a profile to the hardware; returns False if the device refused part of it."""
        if hasattr(driver, "apply_profile"):
            return bool(driver.apply_profile(prof))
        return False


# =============================================================================
# Active window detection & auto-switching
# =============================================================================

def _run(cmd: List[str], timeout: float = 0.4) -> Optional[str]:
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True, timeout=timeout).strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


GNOME_FOCUS_EXTENSION = "skillkorp-focus@skillkorp.github.io"


def _is_gnome_session() -> bool:
    return "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()


def _query_gnome_focus() -> Optional[Tuple[str, str]]:
    """(wm_class, app_id) of the focused window from the SkillKorp GNOME
    Shell extension, or None when the extension is not running."""
    out = _run(["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
                "--object-path", "/io/github/skillkorp/Focus",
                "--method", "io.github.skillkorp.Focus.GetFocused"])
    if not out:
        return None
    m = re.fullmatch(r"\('((?:[^'\\]|\\.)*)', '((?:[^'\\]|\\.)*)'\)", out)
    return (m.group(1), m.group(2)) if m else None


def _detect_gnome() -> Optional[str]:
    focus = _query_gnome_focus()
    if focus is not None:
        wm_class, app_id = focus
        return wm_class or app_id or None
    # Third-party "Focused Window D-Bus" extension, when installed instead.
    out = _run(["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
                "--object-path", "/org/gnome/shell/extensions/FocusedWindow",
                "--method", "org.gnome.shell.extensions.FocusedWindow.Get"])
    if out:
        m = re.search(r'"wm_class"\s*:\s*"([^"]+)"', out.replace("\\", ""))
        if m:
            return m.group(1)
    return None


def _gnome_extension_dirs() -> List[str]:
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    data_dirs = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    return [os.path.join(d, "gnome-shell", "extensions") for d in [data_home] + data_dirs if d]


def _gnome_enabled_extensions() -> Optional[List[str]]:
    out = _run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"], timeout=2)
    if out is None:
        return None
    return re.findall(r"'([^']+)'", out)


def gnome_focus_extension_status() -> Optional[str]:
    """None outside GNOME, else "active", "inactive" (enabled, loaded at the
    next login), "disabled" or "missing" (not installed)."""
    if not _is_gnome_session():
        return None
    if _query_gnome_focus() is not None:
        return "active"
    if not any(os.path.isdir(os.path.join(d, GNOME_FOCUS_EXTENSION)) for d in _gnome_extension_dirs()):
        return "missing"
    enabled = _gnome_enabled_extensions() or []
    return "inactive" if GNOME_FOCUS_EXTENSION in enabled else "disabled"


def enable_gnome_focus_extension() -> Optional[str]:
    """Enable the SkillKorp focus extension for this user when it is installed
    but disabled (GNOME only). Returns the resulting status."""
    status = gnome_focus_extension_status()
    if status != "disabled":
        return status
    # Loaded at once when the running shell already knows the extension
    # (asked directly: the gnome-extensions tool may go through a service
    # that is not always available)...
    out = _run(["gdbus", "call", "--session", "--dest", "org.gnome.Shell", "--object-path", "/org/gnome/Shell",
                "--method", "org.gnome.Shell.Extensions.EnableExtension", GNOME_FOCUS_EXTENSION], timeout=3)
    if out and "true" in out:
        for _ in range(10):
            status = gnome_focus_extension_status()
            if status == "active":
                return status
            time.sleep(0.1)
    # ...otherwise (installed after login) it is loaded at the next login.
    enabled = _gnome_enabled_extensions()
    if enabled is not None and GNOME_FOCUS_EXTENSION not in enabled:
        value = "[" + ", ".join(f"'{e}'" for e in enabled + [GNOME_FOCUS_EXTENSION]) + "]"
        _run(["gsettings", "set", "org.gnome.shell", "enabled-extensions", value], timeout=2)
    return gnome_focus_extension_status()


def _detect_kde() -> Optional[str]:
    out = _run(["kdotool", "getactivewindow", "getwindowclassname"])
    return out or None


def detect_active_window_class() -> Optional[str]:
    """Detect the focused window's class/app id (X11, XWayland, Hyprland, Sway, KDE, GNOME)."""
    # Under GNOME Wayland the X11 tools only see XWayland windows (and report
    # a stale one when a native window has the focus): ask the shell first.
    if _is_gnome_session() and os.environ.get("XDG_SESSION_TYPE") == "wayland":
        res = _detect_gnome()
        if res:
            return res.lower()
    out = _run(["xdotool", "getactivewindow", "getwindowclassname"], timeout=0.3)
    if out:
        return out.lower()

    out = _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"], timeout=0.3)
    if out:
        win_id = out.split()[-1]
        if win_id and win_id not in ("0x0", "0x00"):
            prop_out = _run(["xprop", "-id", win_id, "WM_CLASS"], timeout=0.3)
            matches = re.findall(r'"([^"]+)"', prop_out or "")
            if matches:
                return matches[-1].lower()

    out = _run(["hyprctl", "activewindow", "-j"])
    if out:
        try:
            cls = json.loads(out).get("class")
            if cls:
                return cls.lower()
        except (ValueError, AttributeError):
            pass

    out = _run(["swaymsg", "-t", "get_tree"])
    if out:
        try:
            data = json.loads(out)
        except ValueError:
            data = None

        def find_focused(node):
            if node.get("focused"):
                return node.get("app_id") or node.get("window_properties", {}).get("class")
            for ch in node.get("nodes", []) + node.get("floating_nodes", []):
                res = find_focused(ch)
                if res:
                    return res
            return None

        if isinstance(data, dict):
            res = find_focused(data)
            if res:
                return res.lower()

    for detector in (_detect_kde, _detect_gnome):
        res = detector()
        if res:
            return res.lower()
    return None


def find_matching_profile(profiles: List[Dict[str, Any]], window_class: str) -> Optional[str]:
    """Return the id of the profile whose auto_switch_apps best matches window_class.

    Matching is case-insensitive and substring based (so "steam_app" matches
    "steam_app_730"); the longest matching pattern wins, which keeps a
    specific game profile ahead of a generic one. Empty patterns are ignored.
    """
    if not window_class:
        return None
    window_class = window_class.lower()
    best: Optional[Tuple[int, str]] = None
    for p in profiles:
        for app in p.get("auto_switch_apps", []) or []:
            pattern = str(app).strip().lower()
            if pattern and pattern in window_class:
                if best is None or len(pattern) > best[0]:
                    best = (len(pattern), p["id"])
    return best[1] if best else None


class AutoSwitcher:
    """Switch profiles of several devices from the focused application.

    When the focused application matches a profile, that profile is applied;
    when focus moves to an application that matches nothing, the profile that
    was active before the automatic switch is restored. A manual change made
    meanwhile (GUI, CLI, tray) becomes the new baseline.
    """

    def __init__(self, targets: List[Tuple[ProfileManager, Any]],
                 detect: Callable[[], Optional[str]] = detect_active_window_class,
                 on_switch: Optional[Callable[[str, str, Dict[str, Any], bool, str], None]] = None):
        self.targets = targets
        self.detect = detect
        self.on_switch = on_switch
        self.last_class: Optional[str] = None
        self._baseline: Dict[str, str] = {}
        self._auto_active: Dict[str, Optional[str]] = {}

    def tick(self) -> List[Tuple[str, str]]:
        cls = self.detect()
        if not cls or cls == self.last_class:
            return []
        self.last_class = cls
        switched: List[Tuple[str, str]] = []
        for pm, driver in self.targets:
            dev = pm.device_type
            if not pm.is_auto_switch_enabled():
                continue
            active = pm.get_active_profile_id()
            if self._auto_active.get(dev) != active:
                # first run or manual change since our last switch
                self._baseline[dev] = active
                self._auto_active[dev] = None
            matched = find_matching_profile(pm.list_profiles(), cls)
            target = matched or (self._baseline.get(dev) if self._auto_active.get(dev) else None)
            if not target or target == active:
                continue
            try:
                prof = pm.switch_profile(target, driver=driver)
            except ValueError as e:
                print(f"[auto-switch:{dev}] {e}", file=sys.stderr)
                continue
            self._auto_active[dev] = target if matched else None
            ok = pm.last_apply_ok is not False
            switched.append((dev, target))
            if self.on_switch:
                self.on_switch(dev, target, prof, ok, cls)
        return switched
