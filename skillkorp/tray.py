#!/usr/bin/env python3
"""
SkillKorp Suite - Unified System Tray Indicator & Notifications

A single AppIndicator process manages both devices (replacing the separate
k20_tray.py and m20_tray.py processes):
- Combined battery label in the panel (e.g. "⌨82% 🖱91%")
- Submenus "Clavier" and "Souris", each with their own profile/RGB/DPI/
  polling-rate quick switches
- Low battery alerts & charging notifications, independently per device
- One shared active-window watch drives auto-switching for both devices
- One "Ouvrir SkillKorp Suite..." entry, one autostart toggle, one Quit
"""

import os
import sys
import subprocess
from typing import Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import gi
gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as appindicator
except Exception:
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as appindicator

from gi.repository import Gtk, GLib
try:
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify
    HAS_NOTIFY = True
except Exception:
    HAS_NOTIFY = False

from skillkorp.devices.keyboard import SkillkorpK20Driver, LIGHT_MODES as KB_LIGHT_MODES
from skillkorp.devices.mouse import SkillkorpM20Driver
from skillkorp.profiles.manager import (
    ProfileManager,
    detect_active_window_class,
    find_matching_profile,
    is_autostart_enabled,
    set_autostart,
)
from skillkorp.profiles.migration import migrate_legacy_configs

PID_FILE = os.path.join(os.path.expanduser("~/.config/skillkorp"), "tray.pid")


class SkillkorpSuiteTray:
    def __init__(self):
        self.kb_driver = SkillkorpK20Driver()
        self.mouse_driver = SkillkorpM20Driver()
        self.kb_pm = ProfileManager("keyboard")
        self.mouse_pm = ProfileManager("mouse")

        if HAS_NOTIFY:
            Notify.init("SkillKorp Suite")

        self.last_kb_connected: Optional[bool] = None
        self.last_mouse_connected: Optional[bool] = None
        self.kb_warned_low_20 = False
        self.kb_warned_low_10 = False
        self.mouse_warned_low_20 = False
        self.mouse_warned_low_10 = False
        self.last_active_window: Optional[str] = None

        self.indicator = appindicator.Indicator.new(
            "skillkorp-suite-tray",
            "skillkorp-suite",
            appindicator.IndicatorCategory.HARDWARE,
        )
        self.indicator.set_status(appindicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title("SkillKorp Suite")

        icon_path = os.path.join(BASE_DIR, "assets", "skillkorp-k20.png")
        if os.path.exists(icon_path):
            self.indicator.set_icon_full(icon_path, "SkillKorp Suite")

        self.menu = Gtk.Menu()
        self._build_menu()
        self.indicator.set_menu(self.menu)

        self._update_status()

        GLib.timeout_add_seconds(4, self._on_status_timer)
        GLib.timeout_add(1000, self._on_auto_switch_timer)

    def _notify(self, title: str, body: str, urgency: int = 1):
        if not HAS_NOTIFY:
            return
        try:
            n = Notify.Notification.new(title, body, "skillkorp-suite")
            n.set_urgency(urgency)
            n.set_timeout(3000)
            n.show()
        except Exception:
            pass

    # -------------------------------------------------------------------
    # Menu construction
    # -------------------------------------------------------------------
    def _build_menu(self):
        # Battery headers
        self.item_kb_battery = Gtk.MenuItem(label="⌨ Clavier : --%")
        self.item_kb_battery.set_sensitive(False)
        self.menu.append(self.item_kb_battery)

        self.item_mouse_battery = Gtk.MenuItem(label="🖱 Souris : --%")
        self.item_mouse_battery.set_sensitive(False)
        self.menu.append(self.item_mouse_battery)

        self.menu.append(Gtk.SeparatorMenuItem())

        # Keyboard submenu
        item_kb = Gtk.MenuItem(label="Clavier")
        kb_submenu = Gtk.Menu()

        self.kb_profile_item = Gtk.MenuItem(label="Profil : Par défaut")
        self.kb_profile_submenu = Gtk.Menu()
        self._populate_profile_submenu(self.kb_profile_submenu, self.kb_pm, self._on_select_kb_profile)
        self.kb_profile_item.set_submenu(self.kb_profile_submenu)
        kb_submenu.append(self.kb_profile_item)

        item_kb_rgb = Gtk.MenuItem(label="Éclairage RGB")
        kb_rgb_submenu = Gtk.Menu()
        for key, name, _ in KB_LIGHT_MODES:
            sub_item = Gtk.MenuItem(label=name)
            sub_item.connect("activate", lambda w, k=key: self._on_select_kb_rgb(k))
            kb_rgb_submenu.append(sub_item)
        item_kb_rgb.set_submenu(kb_rgb_submenu)
        kb_submenu.append(item_kb_rgb)

        item_kb_poll = Gtk.MenuItem(label="Taux de rafraîchissement")
        kb_poll_submenu = Gtk.Menu()
        for hz in [1000, 500, 250, 125]:
            sub_item = Gtk.MenuItem(label=f"{hz} Hz")
            sub_item.connect("activate", lambda w, r=hz: self._on_select_kb_poll(r))
            kb_poll_submenu.append(sub_item)
        item_kb_poll.set_submenu(kb_poll_submenu)
        kb_submenu.append(item_kb_poll)

        self.kb_win_lock_item = Gtk.CheckMenuItem(label="Verrouiller touche Windows")
        self.kb_win_lock_item.connect("toggled", self._on_toggle_kb_win_lock)
        kb_submenu.append(self.kb_win_lock_item)

        item_kb.set_submenu(kb_submenu)
        self.menu.append(item_kb)

        # Mouse submenu
        item_mouse = Gtk.MenuItem(label="Souris")
        mouse_submenu = Gtk.Menu()

        self.mouse_profile_item = Gtk.MenuItem(label="Profil : Par défaut")
        self.mouse_profile_submenu = Gtk.Menu()
        self._populate_profile_submenu(self.mouse_profile_submenu, self.mouse_pm, self._on_select_mouse_profile)
        self.mouse_profile_item.set_submenu(self.mouse_profile_submenu)
        mouse_submenu.append(self.mouse_profile_item)

        self.mouse_dpi_item = Gtk.MenuItem(label="Sensibilité DPI")
        self.mouse_dpi_submenu = Gtk.Menu()
        self.mouse_dpi_item.set_submenu(self.mouse_dpi_submenu)
        mouse_submenu.append(self.mouse_dpi_item)

        item_mouse_poll = Gtk.MenuItem(label="Taux de rapport")
        mouse_poll_submenu = Gtk.Menu()
        for hz in [1000, 500, 250, 125]:
            sub_item = Gtk.MenuItem(label=f"{hz} Hz")
            sub_item.connect("activate", lambda w, r=hz: self._on_select_mouse_rate(r))
            mouse_poll_submenu.append(sub_item)
        item_mouse_poll.set_submenu(mouse_poll_submenu)
        mouse_submenu.append(item_mouse_poll)

        item_mouse.set_submenu(mouse_submenu)
        self.menu.append(item_mouse)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_open_gui = Gtk.MenuItem(label="Ouvrir SkillKorp Suite...")
        item_open_gui.connect("activate", self._on_open_gui)
        self.menu.append(item_open_gui)

        self.item_autostart = Gtk.CheckMenuItem(label="Lancer avec la session")
        self.item_autostart.set_active(is_autostart_enabled())
        self.item_autostart.connect("toggled", self._on_toggle_autostart)
        self.menu.append(self.item_autostart)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quitter")
        item_quit.connect("activate", self._on_quit)
        self.menu.append(item_quit)

        self.menu.show_all()
        self._populate_dpi_submenu()

    def _populate_profile_submenu(self, submenu: Gtk.Menu, pm: ProfileManager, callback):
        for child in submenu.get_children():
            submenu.remove(child)
        profiles = pm.list_profiles()
        active_id = pm.get_active_profile_id()
        for p in profiles:
            p_id = p["id"]
            p_name = p.get("name", p_id)
            label = f"✓ {p_name}" if p_id == active_id else f"   {p_name}"
            sub_item = Gtk.MenuItem(label=label)
            sub_item.connect("activate", lambda w, pid=p_id: callback(pid))
            submenu.append(sub_item)
        submenu.show_all()

    def _populate_dpi_submenu(self):
        for child in self.mouse_dpi_submenu.get_children():
            self.mouse_dpi_submenu.remove(child)
        stages = self.mouse_driver.config.get("dpi_stages", [400, 800, 1600, 3200, 6400, 26000])
        active_stage = self.mouse_driver.config.get("active_stage", 2)
        for idx, dpi in enumerate(stages):
            stage_num = idx + 1
            label = f"✓ Étape {stage_num} : {dpi} DPI" if stage_num == active_stage else f"   Étape {stage_num} : {dpi} DPI"
            sub_item = Gtk.MenuItem(label=label)
            sub_item.connect("activate", lambda w, s=stage_num: self._on_select_mouse_dpi_stage(s))
            self.mouse_dpi_submenu.append(sub_item)
        self.mouse_dpi_submenu.show_all()

    # -------------------------------------------------------------------
    # Status polling
    # -------------------------------------------------------------------
    def _update_status(self):
        self.kb_driver.reload_config_if_changed()
        self.mouse_driver.reload_config_if_changed()

        kb_connected = self.kb_driver.is_connected()
        kb_bat = self.kb_driver.get_battery()
        kb_pct = kb_bat.get("percentage") or 0
        kb_charging = kb_bat.get("charging", False)

        if self.last_kb_connected is not None and self.last_kb_connected != kb_connected:
            if kb_connected:
                mode_str = "sans-fil 2.4GHz" if self.kb_driver.is_wireless() else "filaire USB"
                self._notify("Clavier connecté", f"SkillKorp K20 détecté en mode {mode_str}.")
            else:
                self._notify("Clavier déconnecté", "SkillKorp K20 n'est plus détecté.")
        self.last_kb_connected = kb_connected

        if kb_connected and kb_pct > 0:
            if kb_pct <= 10 and not self.kb_warned_low_10 and not kb_charging:
                self._notify("Batterie Critique (Clavier)", f"SkillKorp K20 : {kb_pct}% restant.", 2)
                self.kb_warned_low_10 = True
            elif kb_pct <= 20 and not self.kb_warned_low_20 and not kb_charging:
                self._notify("Batterie Faible (Clavier)", f"SkillKorp K20 : {kb_pct}% restant.", 1)
                self.kb_warned_low_20 = True
            elif kb_pct > 30:
                self.kb_warned_low_20 = False
                self.kb_warned_low_10 = False

        kb_str = f"⌨{kb_pct}%" if kb_connected else "⌨--"
        self.item_kb_battery.set_label(f"⌨ Clavier : {kb_pct}%" + (" (charge)" if kb_charging else "") if kb_connected else "⌨ Clavier déconnecté")

        mouse_connected = self.mouse_driver.is_connected()
        mouse_bat = self.mouse_driver.get_battery()
        mouse_pct = mouse_bat.get("percentage") or 0
        mouse_charging = mouse_bat.get("charging", False)

        if self.last_mouse_connected is not None and self.last_mouse_connected != mouse_connected:
            if mouse_connected:
                self._notify("Souris connectée", "SkillKorp M20 détectée.")
            else:
                self._notify("Souris déconnectée", "SkillKorp M20 n'est plus détectée.")
        self.last_mouse_connected = mouse_connected

        if mouse_connected and mouse_pct > 0:
            if mouse_pct <= 10 and not self.mouse_warned_low_10 and not mouse_charging:
                self._notify("Batterie Critique (Souris)", f"SkillKorp M20 : {mouse_pct}% restant.", 2)
                self.mouse_warned_low_10 = True
            elif mouse_pct <= 20 and not self.mouse_warned_low_20 and not mouse_charging:
                self._notify("Batterie Faible (Souris)", f"SkillKorp M20 : {mouse_pct}% restant.", 1)
                self.mouse_warned_low_20 = True
            elif mouse_pct > 30:
                self.mouse_warned_low_20 = False
                self.mouse_warned_low_10 = False

        mouse_str = f"🖱{mouse_pct}%" if mouse_connected else "🖱--"
        self.item_mouse_battery.set_label(f"🖱 Souris : {mouse_pct}%" + (" (charge)" if mouse_charging else "") if mouse_connected else "🖱 Souris déconnectée")

        self.indicator.set_label(f"{kb_str} {mouse_str}", "⌨100% 🖱100%")

        kb_active_id = self.kb_pm.get_active_profile_id()
        kb_prof = self.kb_pm.get_profile(kb_active_id)
        self.kb_profile_item.set_label(f"Profil : {kb_prof.get('name', kb_active_id) if kb_prof else kb_active_id}")

        mouse_active_id = self.mouse_pm.get_active_profile_id()
        mouse_prof = self.mouse_pm.get_profile(mouse_active_id)
        self.mouse_profile_item.set_label(f"Profil : {mouse_prof.get('name', mouse_active_id) if mouse_prof else mouse_active_id}")

        opts = self.kb_driver.config.get("options", {})
        self.kb_win_lock_item.set_active(opts.get("win_lock", False))

        self._populate_dpi_submenu()

    def _on_status_timer(self) -> bool:
        self._update_status()
        return True

    def _on_auto_switch_timer(self) -> bool:
        cls = detect_active_window_class()
        if cls and cls != self.last_active_window:
            self.last_active_window = cls

            if self.kb_pm.is_auto_switch_enabled():
                matched = find_matching_profile(self.kb_pm.list_profiles(), cls)
                if matched and matched != self.kb_pm.get_active_profile_id():
                    p = self.kb_pm.switch_profile(matched, driver=self.kb_driver)
                    self._notify("Profil clavier automatique", f"Profil '{p.get('name', matched)}' pour {cls}.")
                    self._populate_profile_submenu(self.kb_profile_submenu, self.kb_pm, self._on_select_kb_profile)

            if self.mouse_pm.is_auto_switch_enabled():
                matched = find_matching_profile(self.mouse_pm.list_profiles(), cls)
                if matched and matched != self.mouse_pm.get_active_profile_id():
                    p = self.mouse_pm.switch_profile(matched, driver=self.mouse_driver)
                    self._notify("Profil souris automatique", f"Profil '{p.get('name', matched)}' pour {cls}.")
                    self._populate_profile_submenu(self.mouse_profile_submenu, self.mouse_pm, self._on_select_mouse_profile)

            self._update_status()

        return True

    # -------------------------------------------------------------------
    # Callbacks
    # -------------------------------------------------------------------
    def _on_select_kb_profile(self, prof_id: str):
        try:
            prof = self.kb_pm.switch_profile(prof_id, driver=self.kb_driver)
            self._populate_profile_submenu(self.kb_profile_submenu, self.kb_pm, self._on_select_kb_profile)
            self._update_status()
            self._notify("Profil clavier activé", f"Profil '{prof.get('name', prof_id)}' appliqué.")
        except Exception as e:
            print(f"Erreur sélection profil clavier : {e}", file=sys.stderr)

    def _on_select_mouse_profile(self, prof_id: str):
        try:
            prof = self.mouse_pm.switch_profile(prof_id, driver=self.mouse_driver)
            self._populate_profile_submenu(self.mouse_profile_submenu, self.mouse_pm, self._on_select_mouse_profile)
            self._update_status()
            self._notify("Profil souris activé", f"Profil '{prof.get('name', prof_id)}' appliqué.")
        except Exception as e:
            print(f"Erreur sélection profil souris : {e}", file=sys.stderr)

    def _on_select_kb_rgb(self, mode: str):
        self.kb_driver.set_rgb(mode=mode)
        self._notify("Éclairage RGB (Clavier)", f"Effet '{mode}' appliqué.")

    def _on_select_kb_poll(self, rate: int):
        self.kb_driver.set_polling_rate(rate)
        self._notify("Taux de rafraîchissement (Clavier)", f"Configuré à {rate} Hz.")

    def _on_toggle_kb_win_lock(self, item):
        target = item.get_active()
        self.kb_driver.set_keyboard_options(win_lock=target)
        st = "verrouillée" if target else "déverrouillée"
        self._notify("Touche Windows", f"Touche Windows {st}.")

    def _on_select_mouse_rate(self, rate: int):
        self.mouse_driver.set_polling_rate(rate)
        self._notify("Taux de rapport (Souris)", f"Configuré à {rate} Hz.")

    def _on_select_mouse_dpi_stage(self, stage_num: int):
        try:
            self.mouse_driver.set_dpi_and_sensor(active_stage=stage_num)
            stages = self.mouse_driver.config.get("dpi_stages", [])
            dpi_val = stages[stage_num - 1] if 1 <= stage_num <= len(stages) else "?"
            self._notify("Sensibilité DPI", f"Étape {stage_num} : {dpi_val} DPI")
        except Exception as e:
            print(f"Erreur changement DPI : {e}", file=sys.stderr)

    def _on_toggle_autostart(self, item):
        set_autostart(item.get_active())

    def _on_open_gui(self, item):
        try:
            subprocess.Popen(["skillkorp-gui"])
        except Exception:
            gui_module = os.path.join(BASE_DIR, "skillkorp", "gui.py")
            subprocess.Popen([sys.executable, gui_module])

    def _on_quit(self, item):
        Gtk.main_quit()


def _write_pid():
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _remove_pid():
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def main():
    migrate_legacy_configs()
    _write_pid()
    try:
        app = SkillkorpSuiteTray()
        Gtk.main()
    finally:
        _remove_pid()


if __name__ == "__main__":
    main()
