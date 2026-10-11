#!/usr/bin/env python3
"""
SkillKorp Suite - Unified System Tray Indicator & Notifications

One AppIndicator for both devices:
- only the SkillKorp logo in the panel; battery levels in the menu (and in
  the indicator title, used as tooltip/accessible name by some panels)
- "Clavier" and "Souris" submenus: profiles, lighting, polling rate, DPI...
- low battery alerts and connection notifications per device
- per-application automatic profile switching (shared AutoSwitcher)

Hardware I/O runs in DeviceWorker threads; the GTK main loop never blocks.
A single instance runs per user (lock on the PID file).
"""

import fcntl
import os
import signal
import subprocess
import sys
from typing import Any, Dict, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as appindicator  # noqa: E402
except (ValueError, ImportError):
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as appindicator  # noqa: E402
    except (ValueError, ImportError):
        appindicator = None

from gi.repository import Gio, GLib, Gtk  # noqa: E402

try:
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify  # noqa: E402
    HAS_NOTIFY = True
except (ValueError, ImportError):
    HAS_NOTIFY = False

from skillkorp.devices.hidraw import DeviceError  # noqa: E402
from skillkorp.devices.keyboard import SkillkorpK20Driver, LIGHT_MODES as KB_LIGHT_MODES  # noqa: E402
from skillkorp.devices.mouse import SkillkorpM20Driver  # noqa: E402
from skillkorp.profiles.manager import (  # noqa: E402
    AutoSwitcher,
    ProfileManager,
    enable_gnome_focus_extension,
    gnome_focus_extension_status,
    is_autostart_enabled,
    set_autostart,
)
from skillkorp.profiles.migration import migrate_legacy_configs  # noqa: E402
from skillkorp.traylaunch import GUI_APP_ID, GUI_OBJECT_PATH, pid_file  # noqa: E402
from skillkorp.worker import DeviceWorker  # noqa: E402

STATUS_PERIOD_S = 5
BATTERY_PERIOD_S = 60
AUTOSWITCH_PERIOD_MS = 1000


class SkillkorpSuiteTray:
    def __init__(self):
        self.kb_driver = SkillkorpK20Driver()
        self.mouse_driver = SkillkorpM20Driver()
        self.kb_pm = ProfileManager("keyboard")
        self.mouse_pm = ProfileManager("mouse")
        self.kb_worker = DeviceWorker("tray-keyboard", GLib.idle_add)
        self.mouse_worker = DeviceWorker("tray-mouse", GLib.idle_add)
        self.state: Dict[str, Dict[str, Any]] = {"keyboard": {}, "mouse": {}}
        self.warned: Dict[str, int] = {"keyboard": 100, "mouse": 100}
        self._ticks = 0
        if HAS_NOTIFY:
            Notify.init("SkillKorp Suite")

        # The switcher runs in its own worker with its own driver instances, so a
        # driver object is never used from two threads (the device itself is
        # serialized by the hidraw lock).
        self.switch_worker = DeviceWorker("tray-autoswitch", GLib.idle_add)
        self.switcher = AutoSwitcher([(ProfileManager("keyboard"), self._ConnectedOnly(SkillkorpK20Driver())),
                                      (ProfileManager("mouse"), self._ConnectedOnly(SkillkorpM20Driver()))],
                                     on_switch=self._on_auto_switched)
        self._switch_busy = False

        self.indicator = appindicator.Indicator.new(
            "skillkorp-suite-tray", "skillkorp-suite", appindicator.IndicatorCategory.HARDWARE)
        self.indicator.set_status(appindicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title("SkillKorp Suite")
        if not Gtk.IconTheme.get_default().has_icon("skillkorp-suite"):
            icon = os.path.join(BASE_DIR, "assets", "skillkorp-suite.png")
            if os.path.exists(icon):
                self.indicator.set_icon_full(icon, "SkillKorp Suite")

        self.menu = Gtk.Menu()
        self._build_menu()
        self.indicator.set_menu(self.menu)

        self._poll(force_battery=True)
        if self.kb_pm.is_auto_switch_enabled() or self.mouse_pm.is_auto_switch_enabled():
            self._ensure_focus_extension()
        GLib.timeout_add_seconds(STATUS_PERIOD_S, self._on_status_timer)
        GLib.timeout_add(AUTOSWITCH_PERIOD_MS, self._on_auto_switch_timer)

    class _ConnectedOnly:
        def __init__(self, drv):
            self.drv = drv

        def apply_profile(self, prof):
            return self.drv.apply_profile(prof) if self.drv.is_connected() else True

    # ------------------------------------------------------------------ utils
    def _notify(self, title: str, body: str, urgency: int = 1):
        if not HAS_NOTIFY:
            return
        try:
            n = Notify.Notification.new(title, body, "skillkorp-suite")
            n.set_urgency(urgency)
            n.set_timeout(4000)
            n.show()
        except GLib.Error:
            pass

    def _run(self, device: str, func, ok_msg: Optional[str] = None):
        worker = self.kb_worker if device == "keyboard" else self.mouse_worker
        driver = self.kb_driver if device == "keyboard" else self.mouse_driver
        label = "Clavier" if device == "keyboard" else "Souris"

        def done(success):
            if success is False:
                self._notify(f"{label} : commande refusée", driver.last_error or "Le périphérique n'a pas répondu.")
            elif getattr(driver, "last_deferred", False):
                self._notify(label, "Clavier en veille : réglage enregistré, il sera appliqué à son réveil.")
            elif ok_msg:
                self._notify(label, ok_msg)
            self._refresh_menus()

        def failed(exc):
            self._notify(f"{label} : erreur", str(exc))
        worker.submit(func, done, failed)

    # --------------------------------------------------------------- menu
    def _item(self, label: str, callback=None, sensitive: bool = True) -> Gtk.MenuItem:
        item = Gtk.MenuItem(label=label)
        if callback:
            item.connect("activate", lambda w: callback())
        item.set_sensitive(sensitive)
        return item

    @staticmethod
    def _icon_item(icon_name: str, label: str) -> Gtk.MenuItem:
        """Read-only menu line with a themed icon (exported by the indicator)."""
        item = Gtk.ImageMenuItem(label=label)
        item.set_image(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU))
        item.set_always_show_image(True)
        item.set_sensitive(False)
        return item

    def _build_menu(self):
        self.item_kb_battery = self._icon_item("input-keyboard", "Clavier : …")
        self.item_mouse_battery = self._icon_item("input-mouse", "Souris : …")
        self.menu.append(self.item_kb_battery)
        self.menu.append(self.item_mouse_battery)
        self.menu.append(Gtk.SeparatorMenuItem())

        kb_item = Gtk.MenuItem(label="Clavier")
        kb_menu = Gtk.Menu()
        self.kb_profile_item = Gtk.MenuItem(label="Profil")
        self.kb_profile_menu = Gtk.Menu()
        self.kb_profile_item.set_submenu(self.kb_profile_menu)
        kb_menu.append(self.kb_profile_item)
        rgb_item = Gtk.MenuItem(label="Éclairage")
        rgb_menu = Gtk.Menu()
        for key, name, _ in KB_LIGHT_MODES:
            rgb_menu.append(self._item(name, lambda k=key, n=name: self._run(
                "keyboard", lambda: self.kb_driver.set_rgb(mode=k), f"Éclairage : {n}")))
        rgb_item.set_submenu(rgb_menu)
        kb_menu.append(rgb_item)
        rate_item = Gtk.MenuItem(label="Taux de rafraîchissement")
        rate_menu = Gtk.Menu()
        for hz in (1000, 500, 250, 125):
            rate_menu.append(self._item(f"{hz} Hz", lambda r=hz: self._run(
                "keyboard", lambda: self.kb_driver.set_polling_rate(r), f"{r} Hz")))
        rate_item.set_submenu(rate_menu)
        kb_menu.append(rate_item)
        self.kb_winlock = Gtk.CheckMenuItem(label="Verrouiller la touche Windows")
        self._winlock_handler = self.kb_winlock.connect("toggled", self._on_toggle_winlock)
        kb_menu.append(self.kb_winlock)
        kb_item.set_submenu(kb_menu)
        self.menu.append(kb_item)

        mouse_item = Gtk.MenuItem(label="Souris")
        mouse_menu = Gtk.Menu()
        self.mouse_profile_item = Gtk.MenuItem(label="Profil")
        self.mouse_profile_menu = Gtk.Menu()
        self.mouse_profile_item.set_submenu(self.mouse_profile_menu)
        mouse_menu.append(self.mouse_profile_item)
        dpi_item = Gtk.MenuItem(label="Sensibilité DPI")
        self.mouse_dpi_menu = Gtk.Menu()
        dpi_item.set_submenu(self.mouse_dpi_menu)
        mouse_menu.append(dpi_item)
        mrate_item = Gtk.MenuItem(label="Taux de rapport")
        mrate_menu = Gtk.Menu()
        for hz in (1000, 500, 250, 125):
            mrate_menu.append(self._item(f"{hz} Hz", lambda r=hz: self._run(
                "mouse", lambda: self.mouse_driver.set_polling_rate(r), f"{r} Hz")))
        mrate_item.set_submenu(mrate_menu)
        mouse_menu.append(mrate_item)
        mouse_item.set_submenu(mouse_menu)
        self.menu.append(mouse_item)

        self.menu.append(Gtk.SeparatorMenuItem())
        self.menu.append(self._item("Ouvrir SkillKorp Suite…", self._open_gui))
        self.item_autoswitch = Gtk.CheckMenuItem(label="Bascule automatique des profils")
        self.item_autoswitch.set_active(self.kb_pm.is_auto_switch_enabled() or self.mouse_pm.is_auto_switch_enabled())
        self.item_autoswitch.connect("toggled", self._on_toggle_autoswitch)
        self.menu.append(self.item_autoswitch)
        self.item_autostart = Gtk.CheckMenuItem(label="Lancer avec la session")
        self.item_autostart.set_active(is_autostart_enabled())
        self.item_autostart.connect("toggled", lambda i: set_autostart(i.get_active()))
        self.menu.append(self.item_autostart)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.menu.append(self._item("Quitter", self._quit_all))
        self.menu.show_all()
        self._refresh_menus()

    def _fill_profiles(self, menu: Gtk.Menu, pm: ProfileManager, device: str):
        for child in menu.get_children():
            menu.remove(child)
        active = pm.get_active_profile_id()
        driver = self.kb_driver if device == "keyboard" else self.mouse_driver
        for p in pm.list_profiles():
            pid, name = p["id"], p.get("name", p["id"])
            label = f"✓ {name}" if pid == active else f"   {name}"

            def switch(i=pid, n=name):
                def task():
                    pm.switch_profile(i, driver=self._ConnectedOnly(driver))
                    return pm.last_apply_ok
                self._run(device, task, f"Profil « {n} » appliqué")
            menu.append(self._item(label, switch))
        menu.show_all()

    def _refresh_menus(self):
        for pm, item, menu, dev in ((self.kb_pm, self.kb_profile_item, self.kb_profile_menu, "keyboard"),
                                    (self.mouse_pm, self.mouse_profile_item, self.mouse_profile_menu, "mouse")):
            pid = pm.get_active_profile_id()
            prof = pm.get_profile(pid)
            item.set_label(f"Profil : {prof.get('name', pid) if prof else pid}")
            self._fill_profiles(menu, pm, dev)

        for child in self.mouse_dpi_menu.get_children():
            self.mouse_dpi_menu.remove(child)
        self.mouse_driver.reload_config_if_changed()
        stages = self.mouse_driver.config.get("dpi_stages", [])
        active = self.mouse_driver.config.get("active_stage", 1)
        for idx, dpi in enumerate(stages, start=1):
            label = f"{'✓' if idx == active else '   '} Étape {idx} : {dpi} DPI"
            self.mouse_dpi_menu.append(self._item(label, lambda s=idx, d=dpi: self._run(
                "mouse", lambda: self.mouse_driver.set_dpi_and_sensor(active_stage=s), f"{d} DPI")))
        self.mouse_dpi_menu.show_all()

        self.kb_driver.reload_config_if_changed()
        self.kb_winlock.handler_block(self._winlock_handler)
        self.kb_winlock.set_active(self.kb_driver.config.get("options", {}).get("win_lock", False))
        self.kb_winlock.handler_unblock(self._winlock_handler)

    def _on_toggle_winlock(self, item):
        target = item.get_active()
        self._run("keyboard", lambda: self.kb_driver.set_keyboard_options(win_lock=target),
                  f"Touche Windows {'verrouillée' if target else 'déverrouillée'}")

    def _on_toggle_autoswitch(self, item):
        for pm in (self.kb_pm, self.mouse_pm):
            pm.set_auto_switch_enabled(item.get_active())
        if item.get_active():
            self._ensure_focus_extension()

    def _ensure_focus_extension(self):
        """GNOME: enable the SkillKorp focus extension needed by the automatic
        profile switching, and say so when a new login is required."""
        def task():
            return gnome_focus_extension_status(), enable_gnome_focus_extension()

        def done(result):
            before, after = result
            if before == "disabled" and after == "inactive":
                self._notify("Bascule automatique des profils",
                             "Extension GNOME SkillKorp activée : elle sera chargée à votre prochaine "
                             "connexion (déconnectez-vous puis reconnectez-vous).")
        self.switch_worker.submit(task, done, lambda e: None)

    def _quit_all(self):
        """Quit the tray and the SkillKorp Suite window if it is open."""
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call_sync(GUI_APP_ID, GUI_OBJECT_PATH, "org.gtk.Actions", "Activate",
                          GLib.Variant("(sava{sv})", ("quit", [], {})), None,
                          Gio.DBusCallFlags.NO_AUTO_START, 2000, None)
        except GLib.Error:
            pass  # the window is not open
        Gtk.main_quit()

    def _open_gui(self):
        try:
            subprocess.Popen(["skillkorp-gui"], start_new_session=True)
        except OSError:
            code = f"import sys; sys.path.insert(0, {BASE_DIR!r}); from skillkorp.gui import main; sys.exit(main())"
            subprocess.Popen([sys.executable, "-c", code], start_new_session=True)

    # ------------------------------------------------------------- status
    def _on_status_timer(self) -> bool:
        self._ticks += 1
        self._poll()
        return True

    def _poll(self, force_battery: bool = False):
        battery = force_battery or (self._ticks * STATUS_PERIOD_S) % BATTERY_PERIOD_S == 0

        def kb_task():
            connected = self.kb_driver.is_connected()
            state = {"connected": connected}
            if connected:
                state["wireless"] = self.kb_driver.is_wireless()
                if battery:
                    state["battery"] = self.kb_driver.get_battery()
                if self.kb_driver.pending_settings():
                    state["synced"] = self.kb_driver.sync_pending()
            return state

        def mouse_task():
            connected = self.mouse_driver.is_connected()
            state = {"connected": connected}
            if connected:
                state["wireless"] = self.mouse_driver.is_wireless()
                state["battery"] = self.mouse_driver.get_battery(poll_hardware=battery)
            return state

        self.kb_worker.submit(kb_task, lambda s: self._on_state("keyboard", s), lambda e: None, key="status")
        self.mouse_worker.submit(mouse_task, lambda s: self._on_state("mouse", s), lambda e: None, key="status")

    def _on_state(self, device: str, state: Dict[str, Any]):
        prev = self.state[device]
        if "battery" not in state and "battery" in prev and state.get("connected"):
            state["battery"] = prev["battery"]
        self.state[device] = state
        label = "Clavier" if device == "keyboard" else "Souris"
        name = "SkillKorp K20" if device == "keyboard" else "SkillKorp M20"
        if state.get("synced"):
            self._notify(label, "Clavier réveillé : réglages en attente appliqués.")

        if prev and prev.get("connected") != state.get("connected"):
            if state.get("connected"):
                mode = "sans-fil 2.4 GHz" if state.get("wireless") else "filaire USB"
                self._notify(f"{label} connecté(e)", f"{name} détecté(e) en mode {mode}.")
            else:
                self._notify(f"{label} déconnecté(e)", f"{name} n'est plus détecté(e).")

        bat = state.get("battery") or {}
        pct = bat.get("percentage")
        if state.get("connected") and pct is not None and not bat.get("charging"):
            for threshold, urgency in ((10, 2), (20, 1)):
                if pct <= threshold < self.warned[device]:
                    self._notify(f"Batterie {'critique' if threshold == 10 else 'faible'} ({label})",
                                 f"{name} : {pct}% restant.", urgency)
                    self.warned[device] = threshold
                    break
        if pct is not None and (pct > 30 or bat.get("charging")):
            self.warned[device] = 100

        item = self.item_kb_battery if device == "keyboard" else self.item_mouse_battery
        if not state.get("connected"):
            item.set_label(f"{label} : déconnecté(e)")
        else:
            item.set_label(f"{label} : {bat.get('status_str', '…')}")
        self._update_label()

    def _update_label(self):
        # The panel shows the logo alone; the levels stay in the menu.
        parts = []
        for device, name in (("keyboard", "clavier"), ("mouse", "souris")):
            st = self.state[device]
            pct = (st.get("battery") or {}).get("percentage")
            parts.append(f"{name} {pct} %" if st.get("connected") and pct is not None else f"{name} --")
        self.indicator.set_title("SkillKorp Suite — " + ", ".join(parts))

    # ---------------------------------------------------------- auto switch
    def _on_auto_switch_timer(self) -> bool:
        if self._switch_busy:
            return True
        self._switch_busy = True

        def done(switched):
            self._switch_busy = False
            if switched:
                self._refresh_menus()

        def failed(exc):
            self._switch_busy = False
            print(f"[tray] bascule automatique : {exc}", file=sys.stderr)
        # Window detection spawns helper processes: keep it off the main loop.
        self.switch_worker.submit(self.switcher.tick, done, failed, key="autoswitch")
        return True

    def _on_auto_switched(self, device, pid, prof, ok, window_class):
        label = "clavier" if device == "keyboard" else "souris"
        msg = f"Profil {label} « {prof.get('name', pid)} » ({window_class})"
        GLib.idle_add(lambda: (self._notify("Bascule automatique", msg if ok else msg + " — échec partiel"), False)[1])

    def shutdown(self):
        for worker in (self.kb_worker, self.mouse_worker, self.switch_worker):
            worker.stop()


def _acquire_single_instance():
    path = pid_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def main() -> int:
    if appindicator is None:
        print("Erreur : AppIndicator introuvable. Installez libayatana-appindicator (GTK3) "
              "et son introspection (gir1.2-ayatanaappindicator3-0.1 / typelib).", file=sys.stderr)
        return 1
    lock_fd = _acquire_single_instance()
    if lock_fd is None:
        print("L'indicateur SkillKorp est déjà en cours d'exécution.", file=sys.stderr)
        return 0
    try:
        migrate_legacy_configs()
    except Exception as e:
        print(f"[tray] migration impossible : {e}", file=sys.stderr)
    tray = None
    try:
        tray = SkillkorpSuiteTray()
        try:
            gi.require_version("GLibUnix", "2.0")
            from gi.repository import GLibUnix
            add_signal = GLibUnix.signal_add
        except (ValueError, ImportError):
            add_signal = GLib.unix_signal_add
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            add_signal(GLib.PRIORITY_DEFAULT, sig, lambda *a: (Gtk.main_quit(), False)[1])
        Gtk.main()
    except DeviceError as e:
        print(f"[tray] {e}", file=sys.stderr)
        return 1
    finally:
        if tray:
            tray.shutdown()
        try:
            os.remove(pid_file())
        except OSError:
            pass
        os.close(lock_fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
