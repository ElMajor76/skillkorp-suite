#!/usr/bin/env python3
"""
SkillKorp Suite - Unified Multi-Profile Manager & Auto-Switching

Manages persistent configuration profiles and automatic switching based on
the active application, for either the keyboard or the mouse. Each device
type keeps its own profile schema and storage location (a keyboard profile
and a mouse profile are never interchangeable), but both are handled by
this single ProfileManager class parameterized by `device_type`.

Storage layout (unified, replacing the separate ~/.config/skillkorp-k20 and
~/.config/skillkorp-m20 directories from the standalone projects):
    ~/.config/skillkorp/keyboard/profiles/*.json
    ~/.config/skillkorp/keyboard/profile_state.json
    ~/.config/skillkorp/mouse/profiles/*.json
    ~/.config/skillkorp/mouse/profile_state.json

A daemon or tray process that wants "switch both devices when the same
application matches both" simply drives two ProfileManager instances (one
per device_type) against the same detected window class each tick - see
skillkorp/cli.py's `daemon` command and skillkorp/tray.py.
"""

import os
import sys
import json
import re
import subprocess
from typing import Any, Dict, List, Optional

CONFIG_DIR = os.path.expanduser("~/.config/skillkorp")

AUTOSTART_DIR = os.path.expanduser("~/.config/autostart")
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "io.github.skillkorp.suite.tray.desktop")

VALID_DEVICE_TYPES = ("keyboard", "mouse")


def is_autostart_enabled() -> bool:
    return os.path.exists(AUTOSTART_FILE)


def set_autostart(enabled: bool):
    os.makedirs(AUTOSTART_DIR, exist_ok=True)
    if enabled:
        content = """[Desktop Entry]
Type=Application
Name=SkillKorp Suite Tray
Comment=Indicateur de batterie et raccourcis SkillKorp (clavier K20 & souris M20)
Exec=skillkorpctl tray start --foreground
Icon=skillkorp-suite
Terminal=false
Categories=Utility;
StartupNotify=false
X-GNOME-Autostart-enabled=true
"""
        with open(AUTOSTART_FILE, "w", encoding="utf-8") as f:
            f.write(content)
    else:
        if os.path.exists(AUTOSTART_FILE):
            os.remove(AUTOSTART_FILE)


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
            "light_sleep_sec": 30,
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
        "sleep_timer_minutes": 5,
        "move_to_wake": True,
        "light_mode": "static",
        "brightness": 8,
        "speed": 4,
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
        "move_to_wake": True,
        "light_mode": "breathing",
        "brightness": 6,
        "speed": 4,
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
        "move_to_wake": False,
        "light_mode": "static",
        "brightness": 2,
        "speed": 4,
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


class ProfileManager:
    """Manages multi-profile storage, active profile state, and hardware
    synchronization for a single device type ("keyboard" or "mouse")."""

    def __init__(self, device_type: str, profiles_dir: Optional[str] = None, state_file: Optional[str] = None):
        if device_type not in VALID_DEVICE_TYPES:
            raise ValueError(f"device_type invalide: {device_type!r}. Attendu: {VALID_DEVICE_TYPES}.")
        self.device_type = device_type
        device_dir = os.path.join(CONFIG_DIR, device_type)
        self.profiles_dir = profiles_dir or os.path.join(device_dir, "profiles")
        self.state_file = state_file or os.path.join(device_dir, "profile_state.json")
        self._default_profiles = _DEFAULT_PROFILES_BY_TYPE[device_type]
        self._ensure_init()

    def _ensure_init(self):
        os.makedirs(self.profiles_dir, exist_ok=True)
        for prof_id, prof_data in self._default_profiles.items():
            path = self._get_profile_path(prof_id)
            if not os.path.exists(path):
                self._save_profile_file(path, prof_data)

        if not os.path.exists(self.state_file):
            self._save_state({"active_profile": "default", "auto_switch_enabled": True})

    def _get_profile_path(self, prof_id: str) -> str:
        safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", prof_id)
        return os.path.join(self.profiles_dir, f"{safe_id}.json")

    def _save_profile_file(self, path: str, data: Dict[str, Any]):
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)

    def _load_state(self) -> Dict[str, Any]:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[profile_manager:{self.device_type}] Erreur lecture état: {e}", file=sys.stderr)
        return {"active_profile": "default", "auto_switch_enabled": True}

    def _save_state(self, state: Dict[str, Any]):
        tmp_path = self.state_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, self.state_file)

    def is_auto_switch_enabled(self) -> bool:
        return bool(self._load_state().get("auto_switch_enabled", True))

    def set_auto_switch_enabled(self, enabled: bool):
        st = self._load_state()
        st["auto_switch_enabled"] = bool(enabled)
        self._save_state(st)

    def get_active_profile_id(self) -> str:
        state = self._load_state()
        active_id = state.get("active_profile", "default")
        if not os.path.exists(self._get_profile_path(active_id)):
            return "default"
        return active_id

    def list_profiles(self) -> List[Dict[str, Any]]:
        active_id = self.get_active_profile_id()
        profiles = []
        if os.path.exists(self.profiles_dir):
            for fname in sorted(os.listdir(self.profiles_dir)):
                if fname.endswith(".json"):
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
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                data["id"] = prof_id
                data.setdefault("device_type", self.device_type)
                return data
        except Exception as e:
            print(f"[ProfileManager:{self.device_type}] Erreur chargement {path}: {e}", file=sys.stderr)
            return None

    def save_profile(self, prof_id: str, data: Dict[str, Any]):
        path = self._get_profile_path(prof_id)
        data["id"] = prof_id
        data["device_type"] = self.device_type
        self._save_profile_file(path, data)

    def create_profile(self, prof_id: str, name: str, copy_from: Optional[str] = None) -> Dict[str, Any]:
        safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", prof_id).lower()
        if copy_from:
            source = self.get_profile(copy_from)
        else:
            source = self.get_profile("default")

        if not source:
            source = self._default_profiles["default"].copy()

        new_data = json.loads(json.dumps(source))
        new_data["id"] = safe_id
        new_data["name"] = name
        new_data["description"] = f"Profil personnalisé : {name}"
        new_data["auto_switch_apps"] = []
        self.save_profile(safe_id, new_data)
        return new_data

    def delete_profile(self, prof_id: str) -> bool:
        if prof_id == "default":
            raise ValueError("Le profil 'default' ne peut pas être supprimé.")
        path = self._get_profile_path(prof_id)
        if os.path.exists(path):
            os.remove(path)
            if self.get_active_profile_id() == prof_id:
                self.switch_profile("default")
            return True
        return False

    def export_profile(self, prof_id: str, target_file: str) -> bool:
        prof = self.get_profile(prof_id)
        if not prof:
            raise ValueError(f"Profil '{prof_id}' introuvable.")
        with open(target_file, "w", encoding="utf-8") as f:
            json.dump(prof, f, indent=2, ensure_ascii=False)
        return True

    def import_profile(self, source_file: str, new_id: Optional[str] = None) -> str:
        """Import a profile from a JSON file, with per-device-type validation."""
        with open(source_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError("Le fichier de profil doit être un objet JSON.")

        valid_rates = [125, 250, 500, 1000]
        rate = data.get("polling_rate")
        if rate is not None and rate not in valid_rates:
            raise ValueError(f"polling_rate invalide: {rate}. Valeurs acceptées: {valid_rates}.")

        if self.device_type == "keyboard":
            debounce = data.get("debounce_ms")
            if debounce is not None and (not isinstance(debounce, int) or not (2 <= debounce <= 20)):
                raise ValueError(f"debounce_ms invalide: {debounce} (doit être entre 2 et 20 ms).")
        else:  # mouse
            stages = data.get("dpi_stages")
            if stages is not None:
                if not isinstance(stages, list) or not (1 <= len(stages) <= 8):
                    raise ValueError("dpi_stages doit être une liste de 1 à 8 valeurs.")
                for s in stages:
                    if not isinstance(s, int) or not (50 <= s <= 26000):
                        raise ValueError(f"DPI invalide: {s} (plage: 50–26000).")

            active = data.get("active_stage")
            if active is not None:
                stages_for_check = stages if stages is not None else [400, 800, 1600, 3200, 6400, 26000]
                if not isinstance(active, int) or not (1 <= active <= len(stages_for_check)):
                    raise ValueError(f"active_stage {active} hors limites (1–{len(stages_for_check)}).")

            from skillkorp.devices.mouse import BUTTON_ACTIONS
            valid_actions = set(BUTTON_ACTIONS.keys())
            buttons = data.get("buttons", {})
            if not isinstance(buttons, dict):
                raise ValueError("'buttons' doit être un objet JSON.")
            for btn_key, action in buttons.items():
                try:
                    btn_num = int(btn_key)
                    if not (1 <= btn_num <= 6):
                        raise ValueError(f"Numéro de bouton invalide: {btn_key} (1–6).")
                except (ValueError, TypeError):
                    raise ValueError(f"Clé de bouton invalide: {btn_key!r} (doit être un entier).")
                if action not in valid_actions:
                    raise ValueError(f"Action invalide pour le bouton {btn_key}: '{action}'.")

        prof_id = new_id or data.get("id") or os.path.basename(source_file).replace(".json", "")
        safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", prof_id).lower()
        data["id"] = safe_id
        self.save_profile(safe_id, data)
        return safe_id

    def switch_profile(self, prof_id: str, driver: Optional[Any] = None) -> Dict[str, Any]:
        prof = self.get_profile(prof_id)
        if not prof:
            raise ValueError(f"Profil '{prof_id}' introuvable.")

        st = self._load_state()
        st["active_profile"] = prof_id
        self._save_state(st)

        if driver is not None:
            self.apply_profile_to_driver(prof, driver)

        return prof

    def apply_profile_to_driver(self, prof: Dict[str, Any], driver: Any):
        """Applies profile configuration parameters directly to driver and hardware."""
        if hasattr(driver, "apply_profile"):
            driver.apply_profile(prof)


def detect_active_window_class() -> Optional[str]:
    """Detect active window process name or WM_CLASS on Linux (X11 & Wayland)."""
    # 1. Try xdotool (fastest on X11 / XWayland)
    try:
        out = subprocess.check_output(
            ["xdotool", "getactivewindow", "getwindowclassname"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.3,
        ).strip()
        if out:
            return out.lower()
    except Exception:
        pass

    # 2. Try xprop
    try:
        out = subprocess.check_output(
            ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.3,
        ).strip()
        win_id = out.split()[-1]
        if win_id and win_id != "0x0":
            prop_out = subprocess.check_output(
                ["xprop", "-id", win_id, "WM_CLASS"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=0.3,
            ).strip()
            matches = re.findall(r'"([^"]+)"', prop_out)
            if matches:
                return matches[-1].lower()
    except Exception:
        pass

    # 3. Try hyprctl (Wayland Hyprland)
    try:
        out = subprocess.check_output(
            ["hyprctl", "activewindow", "-j"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.3,
        )
        data = json.loads(out)
        cls = data.get("class")
        if cls:
            return cls.lower()
    except Exception:
        pass

    # 4. Try swaymsg (Wayland Sway)
    try:
        out = subprocess.check_output(
            ["swaymsg", "-t", "get_tree"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.4,
        )
        data = json.loads(out)

        def find_focused(node):
            if node.get("focused"):
                return node.get("app_id") or node.get("window_properties", {}).get("class")
            for ch in node.get("nodes", []) + node.get("floating_nodes", []):
                res = find_focused(ch)
                if res:
                    return res
            return None

        res = find_focused(data)
        if res:
            return res.lower()
    except Exception:
        pass

    return None


def find_matching_profile(profiles: List[Dict[str, Any]], window_class: str) -> Optional[str]:
    """Return the id of the first profile whose auto_switch_apps matches window_class, if any."""
    for p in profiles:
        for app in p.get("auto_switch_apps", []):
            if app.lower() in window_class:
                return p["id"]
    return None
