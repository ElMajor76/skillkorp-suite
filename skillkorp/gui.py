#!/usr/bin/env python3
"""
SkillKorp Suite - Application Graphique Linux (GTK 4 / Libadwaita)

Une seule fenêtre gère les deux périphériques :
- "Vue d'ensemble" : état et batterie du clavier et de la souris côte à côte
- "Clavier" : Éclairage, Performance, Clavier (options & remappage), Profils
- "Souris"  : Profils, DPI et Capteur, Boutons, Alimentation

Toutes les opérations matérielles passent par un DeviceWorker (un thread par
périphérique) : l'interface ne se fige jamais, les erreurs sont affichées et
les curseurs n'envoient que leur valeur finale.

Compatible libadwaita >= 1.2 / GTK >= 4.8 (Debian 12, Ubuntu 22.04+,
Fedora, Arch, openSUSE) : les widgets plus récents sont utilisés lorsqu'ils
sont disponibles, avec repli sur des équivalents sinon.
"""

import os
import sys
from typing import Any, Callable, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from skillkorp import __version__  # noqa: E402
from skillkorp.devices import keyboard as kbmod  # noqa: E402
from skillkorp.devices import mouse as msmod  # noqa: E402
from skillkorp.devices.hidraw import DeviceError, DeviceNotFoundError, DevicePermissionError  # noqa: E402
from skillkorp.devices.keyboard import SkillkorpK20Driver  # noqa: E402
from skillkorp.devices.mouse import SkillkorpM20Driver  # noqa: E402
from skillkorp.profiles.manager import ProfileManager, is_autostart_enabled, set_autostart  # noqa: E402
from skillkorp.profiles.migration import migrate_legacy_configs  # noqa: E402
from skillkorp import traylaunch  # noqa: E402
from skillkorp.worker import DeviceWorker  # noqa: E402

APP_ID = traylaunch.GUI_APP_ID
STATUS_PERIOD_S = 3
KB_BATTERY_PERIOD_S = 60
MOUSE_BATTERY_PERIOD_S = 30
SLIDER_DEBOUNCE_MS = 300
SWATCHES = (("#FF0000", "Rouge"), ("#FF7700", "Orange"), ("#FFFF00", "Jaune"), ("#00FF00", "Vert"),
            ("#00FFFF", "Cyan"), ("#0066FF", "Bleu"), ("#9900FF", "Violet"), ("#FFFFFF", "Blanc"))


def asset_path(name: str) -> Optional[str]:
    for base in (os.path.join(BASE_DIR, "assets"), "/usr/share/skillkorp-suite/assets"):
        path = os.path.join(base, name)
        if os.path.exists(path):
            return path
    return None


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, DevicePermissionError):
        return "Accès refusé au périphérique : installez la règle udev (voir « skillkorpctl doctor »)."
    if isinstance(exc, DeviceNotFoundError):
        return "Périphérique déconnecté."
    if isinstance(exc, (DeviceError, ValueError)):
        return str(exc)
    return f"Erreur inattendue : {exc}"


# =============================================================================
# Small widget wrappers (compatibility + "programmatic set never fires callbacks")
# =============================================================================

class _Bound:
    def __init__(self):
        self._callback: Optional[Callable[[Any], None]] = None
        self._silent = False

    def on_change(self, callback: Callable[[Any], None]):
        self._callback = callback
        return self

    def _emit(self, *_args):
        if not self._silent and self._callback is not None:
            self._callback(self.get())

    def set(self, value):
        self._silent = True
        try:
            self._set(value)
        finally:
            self._silent = False


class Toggle(_Bound):
    def __init__(self, title: str, subtitle: str = ""):
        super().__init__()
        if hasattr(Adw, "SwitchRow"):
            self.row = Adw.SwitchRow(title=title, subtitle=subtitle)
            self._widget = self.row
        else:
            self.row = Adw.ActionRow(title=title, subtitle=subtitle)
            self._widget = Gtk.Switch(valign=Gtk.Align.CENTER)
            self.row.add_suffix(self._widget)
            self.row.set_activatable_widget(self._widget)
        self._widget.connect("notify::active", self._emit)

    def get(self) -> bool:
        return bool(self._widget.get_active())

    def _set(self, value):
        self._widget.set_active(bool(value))


class Spin(_Bound):
    def __init__(self, title: str, subtitle: str, lo: int, hi: int, step: int = 1):
        super().__init__()
        adj = Gtk.Adjustment(value=lo, lower=lo, upper=hi, step_increment=step, page_increment=step * 5)
        if hasattr(Adw, "SpinRow"):
            self.row = Adw.SpinRow(title=title, subtitle=subtitle, adjustment=adj)
            self._widget = self.row
        else:
            self.row = Adw.ActionRow(title=title, subtitle=subtitle)
            self._widget = Gtk.SpinButton(adjustment=adj, numeric=True, valign=Gtk.Align.CENTER)
            self.row.add_suffix(self._widget)
        self._widget.connect("notify::value", self._emit)

    def get(self) -> int:
        return int(round(self._widget.get_value()))

    def _set(self, value):
        self._widget.set_value(float(value))


class Slider(_Bound):
    def __init__(self, title: str, subtitle: str, lo: int, hi: int, step: int = 1):
        super().__init__()
        self.row = Adw.ActionRow(title=title, subtitle=subtitle)
        self._widget = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        self._widget.set_draw_value(True)
        self._widget.set_digits(0)
        self._widget.set_hexpand(True)
        self._widget.set_size_request(220, -1)
        self._widget.set_valign(Gtk.Align.CENTER)
        self.row.add_suffix(self._widget)
        self._widget.connect("value-changed", self._emit)

    def get(self) -> int:
        return int(round(self._widget.get_value()))

    def _set(self, value):
        self._widget.set_value(float(value))


class Combo(_Bound):
    def __init__(self, title: str, items: List[tuple], subtitle: str = ""):
        """items: list of (value, label)."""
        super().__init__()
        self.values = [v for v, _ in items]
        self.row = Adw.ComboRow(title=title, subtitle=subtitle)
        self.row.set_model(Gtk.StringList.new([label for _, label in items]))
        self.row.connect("notify::selected", self._emit)

    def get(self):
        idx = self.row.get_selected()
        return self.values[idx] if 0 <= idx < len(self.values) else None

    def _set(self, value):
        if value in self.values:
            self.row.set_selected(self.values.index(value))

    def set_items(self, items: List[tuple], selected=None):
        self._silent = True
        try:
            self.values = [v for v, _ in items]
            self.row.set_model(Gtk.StringList.new([label for _, label in items]))
            if selected in self.values:
                self.row.set_selected(self.values.index(selected))
        finally:
            self._silent = False


class ColorPicker(_Bound):
    def __init__(self):
        super().__init__()
        if hasattr(Gtk, "ColorDialogButton"):
            self.widget = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False))
            self.widget.connect("notify::rgba", self._emit)
        else:
            self.widget = Gtk.ColorButton()
            self.widget.connect("color-set", self._emit)
        self.widget.set_valign(Gtk.Align.CENTER)

    def get(self) -> str:
        rgba = self.widget.get_rgba()
        return "#{:02X}{:02X}{:02X}".format(round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255))

    def _set(self, value):
        rgba = Gdk.RGBA()
        if isinstance(value, (list, tuple)):
            value = "#{:02X}{:02X}{:02X}".format(*value)
        if rgba.parse(value or "#FF0000"):
            self.widget.set_rgba(rgba)


class Banner:
    """Adw.Banner (libadwaita >= 1.3) or an equivalent revealer."""

    def __init__(self, title: str, button_label: Optional[str] = None, on_button: Optional[Callable] = None):
        if hasattr(Adw, "Banner"):
            self.widget = Adw.Banner(title=title)
            if button_label:
                self.widget.set_button_label(button_label)
                self.widget.connect("button-clicked", lambda b: on_button())
            self._set = self.widget.set_revealed
        else:
            self.widget = Gtk.Revealer()
            box = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
            label = Gtk.Label(label=title, wrap=True, hexpand=True, xalign=0)
            box.append(label)
            if button_label:
                btn = Gtk.Button(label=button_label)
                btn.connect("clicked", lambda b: on_button())
                box.append(btn)
            self.widget.set_child(box)
            self._set = self.widget.set_reveal_child

    def set_revealed(self, value: bool):
        self._set(bool(value))


class Debouncer:
    """Call `func` once the value stopped changing for `delay_ms`."""

    def __init__(self, delay_ms: int = SLIDER_DEBOUNCE_MS):
        self.delay_ms = delay_ms
        self._sources: Dict[str, int] = {}

    def __call__(self, key: str, func: Callable[[], None]):
        if key in self._sources:
            GLib.source_remove(self._sources[key])

        def fire():
            self._sources.pop(key, None)
            func()
            return False
        self._sources[key] = GLib.timeout_add(self.delay_ms, fire)


# =============================================================================
# Dialog helpers (Adw.AlertDialog >= 1.5, Adw.MessageDialog otherwise)
# =============================================================================

def ask(parent, heading: str, body: str, accept_label: str, on_accept: Callable[[Optional[str]], None],
        destructive: bool = False, entry_placeholder: Optional[str] = None):
    entry = None
    if entry_placeholder is not None:
        entry = Gtk.Entry(placeholder_text=entry_placeholder, activates_default=True)
    appearance = Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED

    def handle(response):
        if response == "accept":
            on_accept(entry.get_text().strip() if entry is not None else None)

    if hasattr(Adw, "AlertDialog"):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Annuler")
        dialog.add_response("accept", accept_label)
        dialog.set_response_appearance("accept", appearance)
        dialog.set_default_response("accept")
        if entry is not None:
            dialog.set_extra_child(entry)
        dialog.connect("response", lambda d, r: handle(r))
        dialog.present(parent)
    else:
        dialog = Adw.MessageDialog(transient_for=parent, heading=heading, body=body)
        dialog.add_response("cancel", "Annuler")
        dialog.add_response("accept", accept_label)
        dialog.set_response_appearance("accept", appearance)
        dialog.set_default_response("accept")
        if entry is not None:
            dialog.set_extra_child(entry)
        dialog.connect("response", lambda d, r: handle(r))
        dialog.present()


def _json_filter():
    f = Gtk.FileFilter()
    f.set_name("Profils JSON (*.json)")
    f.add_pattern("*.json")
    return f


def choose_file(parent, title: str, save: bool, initial_name: str, on_path: Callable[[str], None]):
    if hasattr(Gtk, "FileDialog"):
        dialog = Gtk.FileDialog(title=title)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(_json_filter())
        dialog.set_filters(filters)
        if save:
            dialog.set_initial_name(initial_name)

        def done(d, result):
            try:
                gfile = d.save_finish(result) if save else d.open_finish(result)
            except GLib.Error:
                return  # cancelled
            if gfile and gfile.get_path():
                on_path(gfile.get_path())
        (dialog.save if save else dialog.open)(parent, None, done)
    else:
        action = Gtk.FileChooserAction.SAVE if save else Gtk.FileChooserAction.OPEN
        native = Gtk.FileChooserNative.new(title, parent, action, None, None)
        native.add_filter(_json_filter())
        if save:
            native.set_current_name(initial_name)

        def response(n, resp):
            if resp == Gtk.ResponseType.ACCEPT and n.get_file():
                on_path(n.get_file().get_path())
            parent._keep_native = None
        native.connect("response", response)
        parent._keep_native = native  # keep a reference until the dialog closes
        native.show()


def text_row(title: str, subtitle: str = "") -> Adw.ActionRow:
    """ActionRow showing arbitrary text (profile names, key names like "& 1")
    without Pango markup interpretation."""
    row = Adw.ActionRow()
    row.set_use_markup(False)
    row.set_title(title)
    if subtitle:
        row.set_subtitle(subtitle)
    return row


def battery_icon_name(bat: Optional[Dict[str, Any]]) -> str:
    """Themed battery icon for a battery dict (battery-level-N0[-charging])."""
    pct = (bat or {}).get("percentage")
    charging = bool((bat or {}).get("charging"))
    if pct is None:
        return "battery-good-charging-symbolic" if charging else "battery-missing-symbolic"
    level = max(0, min(100, int(round(pct / 10.0)) * 10))
    if charging and level == 100:
        return "battery-level-100-charged-symbolic"
    return f"battery-level-{level}{'-charging' if charging else ''}-symbolic"


class StatusPill(Gtk.Box):
    """Header-bar status: themed device icon + text, coloured by CSS class."""

    def __init__(self, icon_name: str):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, valign=Gtk.Align.CENTER)
        self.add_css_class("status-pill")
        self.append(Gtk.Image.new_from_icon_name(icon_name))
        self._label = Gtk.Label(label="…")
        self.append(self._label)
        self._battery = Gtk.Image(visible=False)
        self.append(self._battery)

    def set_label(self, text: str):
        self._label.set_label(text)

    def set_battery_icon(self, icon_name: Optional[str]):
        """Themed battery icon after the text (level, charging); None hides it."""
        self._battery.set_visible(icon_name is not None)
        if icon_name:
            self._battery.set_from_icon_name(icon_name)

    def get_label(self) -> str:
        return self._label.get_label()


def prefs_page(title: str, icon: str) -> Adw.PreferencesPage:
    page = Adw.PreferencesPage()
    page.set_title(title)
    page.set_icon_name(icon)
    return page


# =============================================================================
# Generic profile page (used for both devices)
# =============================================================================

class ProfilesPage:
    def __init__(self, win: "SkillkorpSuiteWindow", device: str, pm: ProfileManager, driver, worker: DeviceWorker,
                 on_switched: Callable[[], None]):
        self.win, self.device, self.pm, self.driver, self.worker = win, device, pm, driver, worker
        self.on_switched = on_switched
        self.rows: List[Gtk.Widget] = []
        self.page = prefs_page("Profils", "folder-symbolic")

        active_group = Adw.PreferencesGroup(title="Profil actif",
                                            description="Le profil choisi est appliqué immédiatement au périphérique.")
        buttons = Gtk.Box(spacing=6)
        for label, icon, cb in (("Nouveau", "list-add-symbolic", self._new),
                                ("Importer", "document-open-symbolic", self._import),
                                ("Exporter", "document-save-symbolic", lambda *_: self._export(None))):
            b = Gtk.Button(icon_name=icon, tooltip_text=label)
            b.add_css_class("flat")
            b.connect("clicked", cb)
            buttons.append(b)
        active_group.set_header_suffix(buttons)
        self.combo = Combo("Profil en cours d'utilisation", [])
        self.combo.on_change(lambda pid: pid and self.switch(pid))
        active_group.add(self.combo.row)
        self.apps_row = Adw.EntryRow(title="Applications déclenchant ce profil (séparées par des virgules)")
        self.apps_row.set_show_apply_button(True)
        self.apps_row.connect("apply", self._save_apps)
        active_group.add(self.apps_row)
        self.page.add(active_group)

        self.list_group = Adw.PreferencesGroup(title="Profils enregistrés")
        self.page.add(self.list_group)

        auto_group = Adw.PreferencesGroup(
            title="Automatisation",
            description="Bascule selon l'application au premier plan (via l'indicateur ou « skillkorpctl daemon ») ; "
                        "le profil précédent est rétabli quand l'application n'est plus active.")
        self.auto_toggle = Toggle("Bascule automatique des profils")
        self.auto_toggle.on_change(lambda v: self.pm.set_auto_switch_enabled(v))
        auto_group.add(self.auto_toggle.row)
        self.autostart_toggle = Toggle("Démarrer l'indicateur avec la session",
                                       "Indicateur unique clavier + souris dans la barre système")
        self.autostart_toggle.on_change(self._toggle_autostart)
        auto_group.add(self.autostart_toggle.row)
        self.page.add(auto_group)
        self.refresh()

    def _toggle_autostart(self, value):
        try:
            set_autostart(value)
        except OSError as e:
            self.win.toast(f"Impossible de modifier le démarrage automatique : {e}")

    def refresh(self):
        profiles = self.pm.list_profiles()
        active = self.pm.get_active_profile_id()
        self.combo.set_items([(p["id"], p.get("name", p["id"])) for p in profiles], active)
        prof = next((p for p in profiles if p["id"] == active), None)
        self.apps_row.set_text(", ".join(prof.get("auto_switch_apps", [])) if prof else "")
        self.auto_toggle.set(self.pm.is_auto_switch_enabled())
        self.autostart_toggle.set(is_autostart_enabled())
        for r in self.rows:
            self.list_group.remove(r)
        self.rows = []
        for p in profiles:
            pid, name = p["id"], p.get("name", p["id"])
            row = text_row(name, p.get("description") or f"Identifiant : {pid}")
            row.set_title_lines(1)
            row.set_subtitle_lines(2)
            row.add_prefix(Gtk.Image.new_from_icon_name("emblem-ok-symbolic" if pid == active else "folder-symbolic"))
            if pid != active:
                use = Gtk.Button(label="Activer", valign=Gtk.Align.CENTER)
                use.add_css_class("flat")
                use.connect("clicked", lambda b, i=pid: self.switch(i))
                row.add_suffix(use)
            exp = Gtk.Button(icon_name="document-save-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Exporter")
            exp.add_css_class("flat")
            exp.connect("clicked", lambda b, i=pid: self._export(i))
            row.add_suffix(exp)
            if pid != "default":
                rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Supprimer")
                rm.add_css_class("flat")
                rm.connect("clicked", lambda b, i=pid, n=name: self._delete(i, n))
                row.add_suffix(rm)
            self.list_group.add(row)
            self.rows.append(row)

    def switch(self, pid: str):
        if pid == self.pm.get_active_profile_id():
            return
        connected_driver = self.driver

        def task():
            drv = connected_driver if connected_driver.is_connected() else None
            prof = self.pm.switch_profile(pid, driver=drv)
            return prof, self.pm.last_apply_ok

        def done(result):
            prof, applied = result
            if applied is None:
                self.win.toast(f"Profil « {prof.get('name', pid)} » sélectionné (périphérique absent : "
                               "il sera appliqué à la reconnexion).")
            elif applied:
                self.win.toast(f"Profil « {prof.get('name', pid)} » appliqué.")
            else:
                self.win.toast(f"Profil activé mais certains réglages ont été refusés : {self.driver.last_error}")
            self.refresh()
            self.on_switched()

        def failed(exc):
            self.win.toast(describe_error(exc))
            self.refresh()
            self.on_switched()
        self.worker.submit(task, done, failed)

    def _save_apps(self, row):
        apps = [a.strip() for a in row.get_text().split(",") if a.strip()]
        try:
            self.pm.set_auto_switch_apps(self.pm.get_active_profile_id(), apps)
            self.win.toast("Applications enregistrées.")
        except (ValueError, OSError) as e:
            self.win.toast(str(e))

    def _new(self, *_):
        def create(name):
            if not name:
                return
            try:
                prof = self.pm.create_profile(name, name, copy_from=self.pm.get_active_profile_id())
            except (ValueError, OSError) as e:
                self.win.toast(str(e))
                return
            self.refresh()
            self.switch(prof["id"])
        ask(self.win, "Nouveau profil", "Le nouveau profil reprend les réglages du profil actif.",
            "Créer", create, entry_placeholder="Nom du profil (ex : CS2 compétitif)")

    def _import(self, *_):
        def load(path):
            try:
                pid = self.pm.import_profile(path)
            except (ValueError, OSError) as e:
                self.win.toast(f"Import impossible : {e}")
                return
            self.refresh()
            self.win.toast(f"Profil « {pid} » importé.")
        choose_file(self.win, "Importer un profil", False, "", load)

    def _export(self, pid: Optional[str]):
        pid = pid or self.pm.get_active_profile_id()

        def save(path):
            try:
                self.pm.export_profile(pid, path)
                self.win.toast(f"Profil exporté : {path}")
            except (ValueError, OSError) as e:
                self.win.toast(f"Export impossible : {e}")
        choose_file(self.win, "Exporter le profil", True, f"{pid}.json", save)

    def _delete(self, pid: str, name: str):
        def delete(_):
            def task():
                drv = self.driver if self.driver.is_connected() else None
                return self.pm.delete_profile(pid, driver=drv)

            def done(_res):
                self.refresh()
                self.on_switched()
                self.win.toast(f"Profil « {name} » supprimé.")
            self.worker.submit(task, done, lambda e: self.win.toast(describe_error(e)))
        ask(self.win, "Supprimer le profil", f"Supprimer définitivement « {name} » ?", "Supprimer", delete,
            destructive=True)


# =============================================================================
# Main window
# =============================================================================

class SkillkorpSuiteWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="SkillKorp Suite")
        self.set_default_size(1040, 780)
        self.set_size_request(380, 500)

        self.kb_driver = SkillkorpK20Driver()
        self.mouse_driver = SkillkorpM20Driver()
        self.kb_pm = ProfileManager("keyboard")
        self.mouse_pm = ProfileManager("mouse")
        self.kb_worker = DeviceWorker("keyboard", GLib.idle_add)
        self.mouse_worker = DeviceWorker("mouse", GLib.idle_add)
        self.debounce = Debouncer()
        self._ticks = 0
        self._kb_state: Dict[str, Any] = {}
        self._mouse_state: Dict[str, Any] = {}
        self._known_profiles = (None, None)

        self._install_css()
        self._build_shell()
        self._build_overview()
        self._build_keyboard()
        self._build_mouse()

        self._sync_keyboard_ui()
        self._sync_mouse_ui()
        self._poll_status(force_battery=True)
        self._timer = GLib.timeout_add_seconds(STATUS_PERIOD_S, self._on_timer)
        self.connect("close-request", self._on_close)

    # ------------------------------------------------------------------ shell
    def _install_css(self):
        css = Gtk.CssProvider()
        data = """
        .status-pill { border-radius: 12px; padding: 2px 10px; font-weight: bold; font-size: 0.85em; }
        .pill-ok { background-color: alpha(@success_color, 0.18); color: @success_color; }
        .pill-off { background-color: alpha(@error_color, 0.18); color: @error_color; }
        .swatch { min-width: 24px; min-height: 24px; border-radius: 6px; padding: 0; }
        """ + "".join(f".swatch-{c[1:]} {{ background: {c}; }}" for c, _ in SWATCHES) + """
        .mouse-zone { background: transparent; border-radius: 8px; border: 1px solid transparent; }
        .mouse-zone:hover { background-color: alpha(@accent_bg_color, 0.30); border-color: @accent_bg_color; }
        """
        if hasattr(css, "load_from_string"):
            css.load_from_string(data)
        else:
            css.load_from_data(data.encode())
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _build_shell(self):
        self.stack = Adw.ViewStack()
        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        self.kb_pill = StatusPill("input-keyboard-symbolic")
        self.mouse_pill = StatusPill("input-mouse-symbolic")
        for pill in (self.kb_pill, self.mouse_pill):
            header.pack_start(pill)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Actualiser")
        refresh.connect("clicked", lambda b: self._poll_status(force_battery=True))
        header.pack_end(refresh)
        menu = Gio.Menu()
        menu.append("Réinitialiser le clavier (usine)", "app.factory_reset_keyboard")
        menu.append("Restaurer les boutons de la souris", "app.restore_mouse_buttons")
        menu.append("À propos de SkillKorp Suite", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))

        self.perm_banner = Banner("Accès aux périphériques refusé : la règle udev n'est pas active.",
                                  "Aide", self._show_permission_help)

        self.toasts = Adw.ToastOverlay(child=self.stack)
        bottom = Adw.ViewSwitcherBar(stack=self.stack)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(self.perm_banner.widget)
        box.append(self.toasts)
        self.toasts.set_vexpand(True)
        box.append(bottom)
        self.set_content(box)
        if hasattr(Adw, "Breakpoint"):
            bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 700sp"))
            bp.add_setter(bottom, "reveal", True)
            self.add_breakpoint(bp)

    def toast(self, message: str):
        t = Adw.Toast.new(message)
        t.set_timeout(4)
        self.toasts.add_toast(t)

    def _show_permission_help(self):
        ask(self, "Autoriser l'accès aux périphériques",
            "Installez le paquet SkillKorp Suite de votre distribution (il fournit la règle udev), "
            "ou exécutez :\n\nsudo cp udev/70-skillkorp-suite.rules /etc/udev/rules.d/\n"
            "sudo udevadm control --reload-rules && sudo udevadm trigger\n\n"
            "puis débranchez et rebranchez le récepteur. « skillkorpctl doctor » vérifie l'installation.",
            "Compris", lambda _: None)

    def _on_close(self, *_):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None
        self.kb_worker.stop()
        self.mouse_worker.stop()
        return False

    # ------------------------------------------------------------ status loop
    def _on_timer(self):
        self._ticks += 1
        self._poll_status()
        return True

    def _poll_status(self, force_battery: bool = False):
        elapsed = self._ticks * STATUS_PERIOD_S
        kb_battery = force_battery or elapsed % KB_BATTERY_PERIOD_S == 0
        mouse_battery = force_battery or elapsed % MOUSE_BATTERY_PERIOD_S == 0

        def access(driver):
            node = driver._node
            return bool(node and os.access(node.devnode, os.R_OK | os.W_OK))

        def kb_task():
            connected = self.kb_driver.is_connected()
            state = {"connected": connected, "access": connected and access(self.kb_driver),
                     "wireless": connected and self.kb_driver.is_wireless()}
            if connected and state["access"] and kb_battery:
                state["battery"] = self.kb_driver.get_battery()
            if connected and state["access"] and self.kb_driver.pending_settings():
                state["synced"] = self.kb_driver.sync_pending()
            state["config_changed"] = self.kb_driver.reload_config_if_changed()
            return state

        def mouse_task():
            connected = self.mouse_driver.is_connected()
            state = {"connected": connected, "access": connected and access(self.mouse_driver),
                     "wireless": connected and self.mouse_driver.is_wireless()}
            if connected and state["access"] and mouse_battery:
                try:
                    self.mouse_driver.poll_status()
                except DeviceError:
                    pass
            state["config_changed"] = self.mouse_driver.reload_config_if_changed()
            state["battery"] = self.mouse_driver.get_battery() if connected else None
            state["status"] = self.mouse_driver.query_status()
            return state

        self.kb_worker.submit(kb_task, self._on_kb_state, lambda e: None, key="status")
        self.mouse_worker.submit(mouse_task, self._on_mouse_state, lambda e: None, key="status")

    def _set_pill(self, pill: StatusPill, state: Dict[str, Any]):
        for cls in ("pill-ok", "pill-off"):
            pill.remove_css_class(cls)
        if not state.get("connected"):
            pill.set_label("Déconnecté")
            pill.set_battery_icon(None)
            pill.add_css_class("pill-off")
            return
        bat = state.get("battery") or {}
        pct = bat.get("percentage")
        mode = "2.4 GHz" if state.get("wireless") else "USB"
        pill.set_label(mode + (f" · {pct}%" if pct is not None else ""))
        pill.set_battery_icon(battery_icon_name(bat) if (pct is not None or bat.get("charging")) else None)
        pill.add_css_class("pill-ok" if state.get("access") else "pill-off")

    def _update_banner(self):
        denied = any(s.get("connected") and not s.get("access") for s in (self._kb_state, self._mouse_state))
        self.perm_banner.set_revealed(denied)

    def _on_kb_state(self, state):
        if "battery" not in state and "battery" in self._kb_state:
            state["battery"] = self._kb_state["battery"]
        self._kb_state = state
        if state.get("synced"):
            self.toast("Clavier réveillé : réglages en attente appliqués.")
        self._set_pill(self.kb_pill, state)
        self._update_banner()
        self._refresh_overview()
        self.kb_banner.set_revealed(not state.get("connected"))
        if state.get("config_changed"):
            self._sync_keyboard_ui()
        self._check_external_profile_changes()

    def _on_mouse_state(self, state):
        self._mouse_state = state
        self._set_pill(self.mouse_pill, state)
        self._update_banner()
        self._refresh_overview()
        self.mouse_banner.set_revealed(not state.get("connected"))
        if state.get("config_changed"):
            self._sync_mouse_ui()
        self._update_mouse_battery(state.get("battery"))
        self._check_external_profile_changes()

    def _check_external_profile_changes(self):
        current = (self.kb_pm.get_active_profile_id(), self.mouse_pm.get_active_profile_id())
        if current != self._known_profiles:
            self._known_profiles = current
            self.kb_profiles.refresh()
            self.mouse_profiles.refresh()
            self._refresh_overview()

    # --------------------------------------------------------------- overview
    def _build_overview(self):
        page = prefs_page("Vue d'ensemble", "view-grid-symbolic")
        self.ov = {}
        self.ov_battery_icons = {}
        for dev, icon, title in (("kb", "input-keyboard-symbolic", "Clavier K20 Ultimate"),
                                 ("mouse", "input-mouse-symbolic", "Souris M20 Ultimate")):
            group = Adw.PreferencesGroup(title=title)
            rows = {}
            for key, label in (("conn", "Connexion"), ("battery", "Batterie"), ("extra", "DPI actif"),
                               ("profile", "Profil actif")):
                if dev == "kb" and key == "extra":
                    continue
                row = text_row(label, "…")
                group.add(row)
                rows[key] = row
            open_btn = Gtk.Button(label="Réglages", valign=Gtk.Align.CENTER)
            open_btn.connect("clicked", lambda b, d=dev: self.stack.set_visible_child_name(
                "keyboard" if d == "kb" else "mouse"))
            group.set_header_suffix(open_btn)
            row_icons = {"conn": icon, "battery": "battery-missing-symbolic",
                         "extra": "power-profile-performance-symbolic", "profile": "folder-symbolic"}
            for key, row in rows.items():
                image = Gtk.Image.new_from_icon_name(row_icons[key])
                row.add_prefix(image)
                if key == "battery":
                    self.ov_battery_icons[dev] = image
            page.add(group)
            self.ov[dev] = rows
        self.stack.add_titled_with_icon(page, "overview", "Vue d'ensemble", "view-grid-symbolic")

    def _refresh_overview(self):
        for dev, state, pm in (("kb", self._kb_state, self.kb_pm), ("mouse", self._mouse_state, self.mouse_pm)):
            rows = self.ov[dev]
            bat = state.get("battery") if state.get("connected") else None
            self.ov_battery_icons[dev].set_from_icon_name(battery_icon_name(bat))
            if not state.get("connected"):
                rows["conn"].set_subtitle("Déconnecté")
                rows["battery"].set_subtitle("—")
            else:
                mode = "Sans-fil 2.4 GHz" if state.get("wireless") else "Câble USB"
                rows["conn"].set_subtitle(mode + ("" if state.get("access") else " — accès refusé (règle udev)"))
                rows["battery"].set_subtitle(bat["status_str"] if bat else "—")
            pid = pm.get_active_profile_id()
            prof = pm.get_profile(pid)
            rows["profile"].set_subtitle(prof.get("name", pid) if prof else pid)
        st = self._mouse_state.get("status")
        if st:
            self.ov["mouse"]["extra"].set_subtitle(
                f"{st['active_dpi']} DPI (étape {st['active_stage']}/{len(st['stages'])})")

    # ================================================================ KEYBOARD
    def _build_keyboard(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.kb_stack = Adw.ViewStack()
        self.kb_banner = Banner("Clavier K20 non détecté : branchez le récepteur 2.4 GHz ou le câble USB-C.")
        outer.append(self.kb_banner.widget)
        self.kb_stack.set_vexpand(True)
        outer.append(self.kb_stack)
        outer.append(Adw.ViewSwitcherBar(stack=self.kb_stack, reveal=True))

        self._build_kb_lighting()
        self._build_kb_performance()
        self._build_kb_options()
        self.kb_profiles = ProfilesPage(self, "keyboard", self.kb_pm, self.kb_driver, self.kb_worker,
                                        self._sync_keyboard_ui)
        self.kb_stack.add_titled_with_icon(self.kb_profiles.page, "kb_profiles", "Profils", "folder-symbolic")
        self.stack.add_titled_with_icon(outer, "keyboard", "Clavier", "input-keyboard-symbolic")

    def _kb_call(self, label: str, func: Callable[[], bool], profile_patch: Optional[Callable[[], Dict]] = None,
                 key: Optional[str] = None):
        """Run a keyboard setter in the worker and report the outcome."""
        def done(success):
            if success:
                if profile_patch:
                    self._save_to_active_profile(self.kb_pm, profile_patch())
                if self.kb_driver.last_deferred:
                    self.toast(f"{label} : clavier en veille, réglage enregistré. "
                               f"Il sera appliqué à son réveil.")
            else:
                self.toast(f"{label} : le clavier n'a pas accepté la commande. {self.kb_driver.last_error}")
                self._sync_keyboard_ui()

        def failed(exc):
            self.toast(f"{label} : {describe_error(exc)}")
            self._sync_keyboard_ui()
        self.kb_worker.submit(func, done, failed, key=key)

    def _save_to_active_profile(self, pm: ProfileManager, patch: Dict[str, Any]):
        pid = pm.get_active_profile_id()
        prof = pm.get_profile(pid)
        if not prof:
            return
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(prof.get(k), dict):
                prof[k].update(v)
            else:
                prof[k] = v
        try:
            pm.save_profile(pid, prof)
        except (ValueError, OSError) as e:
            self.toast(f"Profil non enregistré : {e}")

    def _build_kb_lighting(self):
        page = prefs_page("Éclairage", "weather-clear-symbolic")
        g = Adw.PreferencesGroup(title="Rétroéclairage des touches")
        self.kb_mode = Combo("Effet", [(k, n) for k, n, _ in kbmod.LIGHT_MODES])
        self.kb_speed = Slider("Vitesse", "", 1, 5)
        self.kb_bright = Slider("Luminosité", "", 0, 4)
        self.kb_dir = Combo("Sens de l'onde", [("right", "Vers la droite"), ("left", "Vers la gauche"),
                                                ("down", "Vers le bas"), ("up", "Vers le haut")])
        self.kb_colorful = Toggle("Multicolore", "Désactivé : l'effet utilise la couleur choisie ci-dessous")
        color_row = Adw.ActionRow(title="Couleur")
        swatches = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        for hex_col, name in SWATCHES:
            b = Gtk.Button(tooltip_text=name)
            b.add_css_class("swatch")
            b.add_css_class(f"swatch-{hex_col[1:]}")
            b.connect("clicked", lambda btn, c=hex_col: self._kb_pick_color(c))
            swatches.append(b)
        self.kb_color = ColorPicker()
        swatches.append(self.kb_color.widget)
        color_row.add_suffix(swatches)
        for w in (self.kb_mode, self.kb_speed, self.kb_bright, self.kb_dir, self.kb_colorful):
            g.add(w.row)
        g.add(color_row)
        page.add(g)

        self.kb_mode.on_change(lambda v: self._kb_apply_rgb())
        self.kb_dir.on_change(lambda v: self._kb_apply_rgb())
        self.kb_colorful.on_change(lambda v: self._kb_apply_rgb())
        self.kb_speed.on_change(lambda v: self.debounce("kb_rgb", self._kb_apply_rgb))
        self.kb_bright.on_change(lambda v: self.debounce("kb_rgb", self._kb_apply_rgb))
        self.kb_color.on_change(lambda v: self._kb_pick_color(v))

        sg = Adw.PreferencesGroup(title="Bandes LED latérales")
        self.side_mode = Combo("Effet", [(k, n) for k, n, _ in kbmod.SIDE_LIGHT_MODES])
        self.side_speed = Slider("Vitesse", "", 1, 5)
        self.side_bright = Slider("Luminosité", "", 0, 4)
        self.side_colorful = Toggle("Multicolore")
        side_color_row = Adw.ActionRow(title="Couleur")
        self.side_color = ColorPicker()
        side_color_row.add_suffix(self.side_color.widget)
        for w in (self.side_mode, self.side_speed, self.side_bright, self.side_colorful):
            sg.add(w.row)
        sg.add(side_color_row)
        page.add(sg)
        self.side_mode.on_change(lambda v: self._kb_apply_side())
        self.side_colorful.on_change(lambda v: self._kb_apply_side())
        self.side_color.on_change(lambda v: self._kb_apply_side())
        self.side_speed.on_change(lambda v: self.debounce("kb_side", self._kb_apply_side))
        self.side_bright.on_change(lambda v: self.debounce("kb_side", self._kb_apply_side))
        self.kb_stack.add_titled_with_icon(page, "kb_rgb", "Éclairage", "weather-clear-symbolic")

    def _kb_pick_color(self, color: str):
        self.kb_color.set(color)
        self.kb_colorful.set(False)
        if self.kb_mode.get() == "off":
            self.kb_mode.set("static")
        self._kb_apply_rgb()

    def _kb_apply_rgb(self):
        values = dict(mode=self.kb_mode.get(), speed=self.kb_speed.get(), brightness=self.kb_bright.get(),
                      direction=kbmod.WAVE_DIRECTIONS.get(self.kb_dir.get(), 0), color=self.kb_color.get(),
                      colorful=self.kb_colorful.get())
        self.kb_dir.row.set_sensitive(values["mode"] == "wave")
        self._kb_call("Éclairage", lambda: self.kb_driver.set_rgb(**values),
                      lambda: {"rgb": dict(self.kb_driver.config["rgb"])}, key="rgb")

    def _kb_apply_side(self):
        values = dict(mode=self.side_mode.get(), speed=self.side_speed.get(), brightness=self.side_bright.get(),
                      color=self.side_color.get(), colorful=self.side_colorful.get())
        self._kb_call("Éclairage latéral", lambda: self.kb_driver.set_side_rgb(**values),
                      lambda: {"side_rgb": dict(self.kb_driver.config["side_rgb"])}, key="side")

    def _build_kb_performance(self):
        page = prefs_page("Performance", "power-profile-performance-symbolic")
        g = Adw.PreferencesGroup(title="Réactivité")
        self.kb_rate = Combo("Taux de rafraîchissement", [(1000, "1000 Hz (1 ms)"), (500, "500 Hz (2 ms)"),
                                                          (250, "250 Hz (4 ms)"), (125, "125 Hz (8 ms)")])
        self.kb_rate.on_change(lambda v: self._kb_call(
            "Taux de rafraîchissement", lambda: self.kb_driver.set_polling_rate(v), lambda: {"polling_rate": v}, "rate"))
        self.kb_debounce = Slider("Anti-rebond (ms)", "Plus faible = plus réactif ; augmentez en cas de doubles frappes",
                                  *kbmod.DEBOUNCE_RANGE)
        self.kb_debounce.on_change(lambda v: self.debounce("kb_deb", lambda: self._kb_call(
            "Anti-rebond", lambda: self.kb_driver.set_debounce(v), lambda: {"debounce_ms": v}, "debounce")))
        g.add(self.kb_rate.row)
        g.add(self.kb_debounce.row)
        page.add(g)

        sg = Adw.PreferencesGroup(title="Mise en veille (sans-fil)",
                                  description="0 = jamais. Veille légère : 1 à 60 min ; veille profonde : 10 à 60 min.")
        self.kb_light_sleep = Spin("Veille légère (minutes)", "Extinction du rétroéclairage", 0, 60)
        self.kb_deep_sleep = Spin("Veille profonde (minutes)", "Coupure de la liaison radio", 0, 60)
        for w in (self.kb_light_sleep, self.kb_deep_sleep):
            sg.add(w.row)
            w.on_change(lambda v: self.debounce("kb_sleep", self._kb_apply_sleep))
        page.add(sg)
        self.kb_stack.add_titled_with_icon(page, "kb_perf", "Performance", "power-profile-performance-symbolic")

    def _kb_apply_sleep(self):
        light = self.kb_light_sleep.get() * 60
        deep = self.kb_deep_sleep.get() * 60
        self._kb_call("Veille", lambda: self.kb_driver.set_sleep_time(light, deep),
                      lambda: {"sleep": dict(self.kb_driver.config["sleep"])}, "sleep")
        GLib.timeout_add(1500, lambda: (self._sync_keyboard_ui(), False)[1])

    def _build_kb_options(self):
        page = prefs_page("Clavier", "input-keyboard-symbolic")
        g = Adw.PreferencesGroup(title="Options matérielles")
        self.kb_winlock = Toggle("Verrouiller la touche Windows", "Évite les retours bureau en jeu")
        self.kb_wasd = Toggle("Échanger ZQSD et les flèches")
        self.kb_gaming = Toggle("Mode Gaming")
        self.kb_os = Combo("Système", [("win", "Windows / Linux"), ("mac", "macOS")])
        for w in (self.kb_winlock, self.kb_wasd, self.kb_gaming, self.kb_os):
            g.add(w.row)
        self.kb_winlock.on_change(lambda v: self._kb_set_option(win_lock=v))
        self.kb_wasd.on_change(lambda v: self._kb_set_option(wasd_swap=v))
        self.kb_gaming.on_change(lambda v: self._kb_set_option(gaming_mode=v))
        self.kb_os.on_change(lambda v: self._kb_set_option(os_mode=v))
        page.add(g)

        rg = Adw.PreferencesGroup(title="Remappage des touches",
                                  description="Disposition AZERTY 75 %. « Action d'origine » rétablit la touche.")
        img = asset_path("keyboard.png")
        if img:
            pic = Gtk.Picture.new_for_filename(img)
            pic.set_can_shrink(True)
            pic.set_size_request(-1, 180)
            rg.add(pic)
        names = [k["name"] for k in kbmod.KEY_LAYOUT if k["name"] in kbmod.KEY_MATRIX_INDEX]
        self.kb_remap_key = Combo("Touche", [(n, n) for n in names])
        self.kb_remap_action = Combo("Action", [(k, v[0]) for k, v in kbmod.REMAP_ACTIONS.items()])
        self.kb_remap_fn = Toggle("Couche Fn", "Remapper la combinaison Fn + touche")
        assign = Gtk.Button(label="Assigner", halign=Gtk.Align.END, margin_top=6)
        assign.add_css_class("suggested-action")
        assign.connect("clicked", lambda b: self._kb_remap())
        for w in (self.kb_remap_key, self.kb_remap_action, self.kb_remap_fn):
            rg.add(w.row)
        rg.add(assign)
        page.add(rg)
        self.kb_remaps_group = Adw.PreferencesGroup(title="Remappages actifs")
        self.kb_remap_rows: List[Gtk.Widget] = []
        page.add(self.kb_remaps_group)
        self.kb_stack.add_titled_with_icon(page, "kb_options", "Clavier", "input-keyboard-symbolic")

    def _kb_set_option(self, **kwargs):
        self._kb_call("Options", lambda: self.kb_driver.set_keyboard_options(**kwargs),
                      lambda: {"options": dict(self.kb_driver.config["options"])})

    def _kb_remap(self, key: Optional[str] = None, action: Optional[str] = None, fn: Optional[bool] = None):
        key = key or self.kb_remap_key.get()
        action = action or self.kb_remap_action.get()
        fn = self.kb_remap_fn.get() if fn is None else fn
        func = self.kb_driver.remap_fn_key if fn else self.kb_driver.remap_key

        def done(success):
            if success:
                cfg = self.kb_driver.config
                pid = self.kb_pm.get_active_profile_id()
                prof = self.kb_pm.get_profile(pid)
                if prof:  # replace (not merge) so that restored keys disappear from the profile
                    prof["key_remaps"] = dict(cfg["key_remaps"])
                    prof["fn_remaps"] = dict(cfg["fn_remaps"])
                    try:
                        self.kb_pm.save_profile(pid, prof)
                    except (ValueError, OSError) as e:
                        self.toast(f"Profil non enregistré : {e}")
                self.toast(f"{'Fn + ' if fn else ''}{key} → {kbmod.REMAP_ACTIONS[action][0]}")
                self._refresh_remap_list()
            else:
                self.toast(f"Remappage refusé par le clavier. {self.kb_driver.last_error}")
        self.kb_worker.submit(lambda: func(key, action), done, lambda e: self.toast(describe_error(e)))

    def _refresh_remap_list(self):
        for r in self.kb_remap_rows:
            self.kb_remaps_group.remove(r)
        self.kb_remap_rows = []
        cfg = self.kb_driver.config
        entries = [(k, a, False) for k, a in cfg.get("key_remaps", {}).items()] + \
                  [(k, a, True) for k, a in cfg.get("fn_remaps", {}).items()]
        if not entries:
            row = Adw.ActionRow(title="Aucun remappage", subtitle="Toutes les touches ont leur action d'origine.")
            self.kb_remaps_group.add(row)
            self.kb_remap_rows.append(row)
            return
        for key, action, fn in entries:
            label = kbmod.REMAP_ACTIONS.get(action, (action,))[0]
            row = text_row(f"{'Fn + ' if fn else ''}{key}", label)
            reset = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text="Rétablir l'action d'origine")
            reset.add_css_class("flat")
            reset.connect("clicked", lambda b, k=key, f=fn: self._kb_remap(k, "default", f))
            row.add_suffix(reset)
            self.kb_remaps_group.add(row)
            self.kb_remap_rows.append(row)

    def _sync_keyboard_ui(self):
        self.kb_driver.reload_config_if_changed()
        cfg = self.kb_driver.config
        rgb, side, opts, sleep = cfg["rgb"], cfg["side_rgb"], cfg["options"], cfg["sleep"]
        self.kb_mode.set(rgb.get("mode"))
        self.kb_speed.set(rgb.get("speed", 3))
        self.kb_bright.set(rgb.get("brightness", 4))
        self.kb_dir.set({v: k for k, v in kbmod.WAVE_DIRECTIONS.items()}.get(rgb.get("direction", 0), "right"))
        self.kb_dir.row.set_sensitive(rgb.get("mode") == "wave")
        self.kb_colorful.set(rgb.get("colorful", False))
        self.kb_color.set(rgb.get("color", "#FF0000"))
        self.side_mode.set(side.get("mode"))
        self.side_speed.set(side.get("speed", 3))
        self.side_bright.set(side.get("brightness", 4))
        self.side_colorful.set(side.get("colorful", False))
        self.side_color.set(side.get("color", "#00FFCC"))
        self.kb_rate.set(cfg.get("polling_rate", 1000))
        self.kb_debounce.set(cfg.get("debounce_ms", 8))
        self.kb_light_sleep.set(sleep.get("light_sleep_sec", 120) // 60)
        self.kb_deep_sleep.set(sleep.get("deep_sleep_sec", 1680) // 60)
        self.kb_winlock.set(opts.get("win_lock", False))
        self.kb_wasd.set(opts.get("wasd_swap", False))
        self.kb_gaming.set(opts.get("gaming_mode", False))
        self.kb_os.set(opts.get("os_mode", "win"))
        self._refresh_remap_list()

    # =================================================================== MOUSE
    def _build_mouse(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.mouse_stack = Adw.ViewStack()
        self.mouse_banner = Banner("Souris M20 non détectée : branchez le récepteur 2.4 GHz ou le câble USB-C.")
        outer.append(self.mouse_banner.widget)
        self.mouse_stack.set_vexpand(True)
        outer.append(self.mouse_stack)
        outer.append(Adw.ViewSwitcherBar(stack=self.mouse_stack, reveal=True))

        self.mouse_profiles = ProfilesPage(self, "mouse", self.mouse_pm, self.mouse_driver, self.mouse_worker,
                                           self._sync_mouse_ui)
        self.mouse_stack.add_titled_with_icon(self.mouse_profiles.page, "mouse_profiles", "Profils", "folder-symbolic")
        self._build_mouse_dpi()
        self._build_mouse_buttons()
        self._build_mouse_light_power()
        self.stack.add_titled_with_icon(outer, "mouse", "Souris", "input-mouse-symbolic")

    def _mouse_call(self, label: str, func: Callable[[], bool], profile_patch: Callable[[], Dict], success_msg: str):
        def done(success):
            if success:
                self._save_to_active_profile(self.mouse_pm, profile_patch())
                self.toast(success_msg)
            else:
                self.toast(f"{label} : la souris n'a pas accepté la commande. {self.mouse_driver.last_error}")
            self._sync_mouse_ui()

        def failed(exc):
            self.toast(f"{label} : {describe_error(exc)}")
            self._sync_mouse_ui()
        self.mouse_worker.submit(func, done, failed)

    def _build_mouse_dpi(self):
        page = prefs_page("DPI et capteur", "input-mouse-symbolic")
        g = Adw.PreferencesGroup(title="Étapes de sensibilité",
                                 description="PixArt PAW3395 : 50 à 26 000 DPI par pas de 50.")
        self.mouse_stage_count = Spin("Nombre d'étapes", "", 1, msmod.MAX_STAGES)
        self.mouse_stage_count.on_change(lambda v: self._mouse_show_stages(v))
        g.add(self.mouse_stage_count.row)
        self.mouse_stage_rows, self.mouse_stage_spins, self.mouse_stage_radios = [], [], []
        first = None
        for i in range(msmod.MAX_STAGES):
            row = Adw.ActionRow(title=f"Étape {i + 1}")
            radio = Gtk.CheckButton(valign=Gtk.Align.CENTER, tooltip_text="Étape active")
            if first is None:
                first = radio
            else:
                radio.set_group(first)
            row.add_prefix(radio)
            adj = Gtk.Adjustment(value=800, lower=msmod.DPI_RANGE[0], upper=msmod.DPI_RANGE[1],
                                 step_increment=msmod.DPI_STEP, page_increment=500)
            spin = Gtk.SpinButton(adjustment=adj, numeric=True, valign=Gtk.Align.CENTER)
            row.add_suffix(spin)
            g.add(row)
            self.mouse_stage_rows.append(row)
            self.mouse_stage_spins.append(spin)
            self.mouse_stage_radios.append(radio)
        page.add(g)

        sg = Adw.PreferencesGroup(title="Capteur et boutons")
        self.mouse_lod = Combo("Distance de levée (LOD)", [(1, "1 mm"), (2, "2 mm")])
        self.mouse_debounce = Spin("Anti-rebond des boutons (ms)", "2 à 25 ms", *msmod.DEBOUNCE_RANGE)
        self.mouse_msync = Toggle("Motion Sync", "Synchronise le capteur avec les rapports USB")
        self.mouse_angle = Toggle("Angle Snapping", "Aide à tracer des lignes droites")
        self.mouse_ripple = Toggle("Ripple Control", "Lissage à haut DPI")
        for w in (self.mouse_lod, self.mouse_debounce, self.mouse_msync, self.mouse_angle, self.mouse_ripple):
            sg.add(w.row)
        page.add(sg)
        btn = Gtk.Button(label="Appliquer", halign=Gtk.Align.CENTER, margin_top=12)
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.connect("clicked", lambda b: self._mouse_apply_dpi())
        ag = Adw.PreferencesGroup()
        ag.add(btn)
        page.add(ag)
        self.mouse_stack.add_titled_with_icon(page, "mouse_dpi", "DPI et capteur", "input-mouse-symbolic")

    def _mouse_show_stages(self, count: int):
        for i, row in enumerate(self.mouse_stage_rows):
            row.set_visible(i < count)
        if not any(r.get_active() for r in self.mouse_stage_radios[:count]):
            self.mouse_stage_radios[count - 1].set_active(True)

    def _mouse_apply_dpi(self):
        count = self.mouse_stage_count.get()
        stages = [int(s.get_value()) for s in self.mouse_stage_spins[:count]]
        active = next((i + 1 for i, r in enumerate(self.mouse_stage_radios[:count]) if r.get_active()), 1)
        kwargs = dict(stages=stages, active_stage=active, lod=self.mouse_lod.get(), debounce=self.mouse_debounce.get(),
                      motion_sync=self.mouse_msync.get(), angle_snap=self.mouse_angle.get(),
                      ripple=self.mouse_ripple.get())

        def patch():
            c = self.mouse_driver.config
            return {k: c[k] for k in ("dpi_stages", "active_stage", "lod", "debounce", "motion_sync",
                                      "angle_snap", "ripple")}
        self._mouse_call("DPI", lambda: self.mouse_driver.set_dpi_and_sensor(**kwargs), patch,
                         "Paramètres DPI et capteur appliqués.")

    def _build_mouse_buttons(self):
        page = prefs_page("Boutons", "input-gaming-symbolic")
        g = Adw.PreferencesGroup(title="Attribution des boutons",
                                 description="Cliquez sur le schéma ou choisissez une action. "
                                             "Au moins un bouton doit rester en clic gauche.")
        hbox = Gtk.Box(spacing=24, halign=Gtk.Align.CENTER)
        img = asset_path("mouse_view.png") or asset_path("mouse_clean.png") or asset_path("m20_schematic.svg")
        if img:
            overlay = Gtk.Overlay()
            pic = Gtk.Picture.new_for_filename(img)
            pic.set_can_shrink(False)
            pic.set_size_request(194, 360)
            overlay.set_child(pic)
            for num, tip, x, y, w, h in ((1, "Clic gauche", 18, 5, 68, 135), (2, "Clic droit", 110, 5, 68, 135),
                                         (3, "Molette", 84, 28, 26, 72), (4, "Latéral avant", 0, 110, 26, 50),
                                         (5, "Latéral arrière", 0, 166, 26, 52)):
                zone = Gtk.Button(tooltip_text=f"{num}. {tip}", halign=Gtk.Align.START, valign=Gtk.Align.START,
                                  margin_start=x, margin_top=y)
                zone.set_size_request(w, h)
                zone.add_css_class("mouse-zone")
                zone.connect("clicked", lambda b, n=num: self._mouse_focus_button(n))
                overlay.add_overlay(zone)
            hbox.append(overlay)
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.CENTER, hexpand=True)
        listbox.add_css_class("boxed-list")
        self.mouse_btn_keys = list(msmod.BUTTON_ACTIONS.keys())
        labels = [v[0] for v in msmod.BUTTON_ACTIONS.values()]
        self.mouse_btn_dropdowns = []
        names = ["1. Clic gauche", "2. Clic droit", "3. Molette", "4. Latéral avant", "5. Latéral arrière",
                 "6. Bouton DPI (dessous)"]
        self.mouse_btn_rows = []
        for i in range(6):
            row = Adw.ActionRow(title=names[i])
            dd = Gtk.DropDown.new_from_strings(labels)
            dd.set_valign(Gtk.Align.CENTER)
            if hasattr(dd, "set_enable_search"):
                dd.set_enable_search(True)
            row.add_suffix(dd)
            row.set_activatable_widget(dd)
            listbox.append(row)
            self.mouse_btn_dropdowns.append(dd)
            self.mouse_btn_rows.append(row)
        hbox.append(listbox)
        g.add(hbox)
        page.add(g)
        bg = Adw.PreferencesGroup()
        box = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=12)
        apply_btn = Gtk.Button(label="Appliquer")
        apply_btn.add_css_class("suggested-action")
        apply_btn.add_css_class("pill")
        apply_btn.connect("clicked", lambda b: self._mouse_apply_buttons())
        reset_btn = Gtk.Button(label="Réglages d'usine")
        reset_btn.add_css_class("pill")
        reset_btn.connect("clicked", lambda b: self.restore_mouse_buttons())
        box.append(apply_btn)
        box.append(reset_btn)
        bg.add(box)
        page.add(bg)
        self.mouse_stack.add_titled_with_icon(page, "mouse_buttons", "Boutons", "input-gaming-symbolic")

    def _mouse_focus_button(self, num: int):
        dd = self.mouse_btn_dropdowns[num - 1]
        dd.grab_focus()
        if hasattr(dd, "activate"):
            self.mouse_btn_rows[num - 1].activate()

    def _mouse_apply_buttons(self):
        mapping = {i + 1: self.mouse_btn_keys[dd.get_selected()] for i, dd in enumerate(self.mouse_btn_dropdowns)}
        self._mouse_call("Boutons", lambda: self.mouse_driver.set_buttons(mapping),
                         lambda: {"buttons": dict(self.mouse_driver.config["buttons"])},
                         "Mappage des boutons appliqué.")

    def restore_mouse_buttons(self):
        self._mouse_call("Boutons", self.mouse_driver.restore_factory_buttons,
                         lambda: {"buttons": dict(self.mouse_driver.config["buttons"])},
                         "Boutons d'usine restaurés.")

    def _build_mouse_light_power(self):
        # The M20 has no RGB lighting: only power settings are exposed (the
        # lighting fields of report 0x05 keep their stored/default values).
        page = prefs_page("Alimentation", "battery-good-symbolic")
        bg = Adw.PreferencesGroup(title="Batterie")
        self.mouse_battery_row = text_row("Niveau", "…")
        self.mouse_level = Gtk.LevelBar(min_value=0, max_value=100, valign=Gtk.Align.CENTER)
        self.mouse_level.set_size_request(160, -1)
        self.mouse_battery_row.add_suffix(self.mouse_level)
        bg.add(self.mouse_battery_row)
        page.add(bg)

        pg = Adw.PreferencesGroup(title="Alimentation et fréquence")
        self.mouse_led_off = Spin("Veille légère (minutes)", "Premier niveau de veille après inactivité",
                                  *msmod.LED_OFF_RANGE)
        self.mouse_sleep = Spin("Veille profonde (minutes)", "Mise en veille complète après inactivité",
                                *msmod.SLEEP_RANGE)
        self.mouse_rate = Combo("Taux de rapport", [(1000, "1000 Hz (1 ms)"), (500, "500 Hz (2 ms)"),
                                                    (250, "250 Hz (4 ms)"), (125, "125 Hz (8 ms)")])
        for w in (self.mouse_led_off, self.mouse_sleep, self.mouse_rate):
            pg.add(w.row)
        page.add(pg)
        btn = Gtk.Button(label="Appliquer", halign=Gtk.Align.CENTER, margin_top=12)
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.connect("clicked", lambda b: self._mouse_apply_light_power())
        ag = Adw.PreferencesGroup()
        ag.add(btn)
        page.add(ag)
        self.mouse_stack.add_titled_with_icon(page, "mouse_power", "Alimentation", "battery-good-symbolic")

    def _mouse_apply_light_power(self):
        led_off, sleep, rate = self.mouse_led_off.get(), self.mouse_sleep.get(), self.mouse_rate.get()
        drv = self.mouse_driver

        def task():
            if rate != drv.config.get("polling_rate") and not drv.set_polling_rate(rate):
                return False
            return drv.set_power_settings(sleep_timer_minutes=sleep, led_off_minutes=led_off)

        def patch():
            c = drv.config
            return {k: c[k] for k in ("led_off_minutes", "sleep_timer_minutes", "polling_rate")}
        self._mouse_call("Alimentation", task, patch, "Alimentation et fréquence appliquées.")

    def _update_mouse_battery(self, bat: Optional[Dict[str, Any]]):
        if not bat or not bat.get("connected"):
            self.mouse_battery_row.set_subtitle("Souris déconnectée")
            self.mouse_level.set_value(0)
            return
        self.mouse_battery_row.set_subtitle(bat["status_str"])
        self.mouse_level.set_value(bat["percentage"] or 0)

    def _sync_mouse_ui(self):
        self.mouse_driver.reload_config_if_changed()
        c = self.mouse_driver.config
        stages = c.get("dpi_stages", [800])
        self.mouse_stage_count.set(len(stages))
        for i, spin in enumerate(self.mouse_stage_spins):
            spin.set_value(stages[i] if i < len(stages) else 800)
        self._mouse_show_stages(len(stages))
        active = c.get("active_stage", 1)
        if 1 <= active <= len(stages):
            self.mouse_stage_radios[active - 1].set_active(True)
        self.mouse_lod.set(c.get("lod", 1))
        self.mouse_debounce.set(c.get("debounce", 4))
        self.mouse_msync.set(c.get("motion_sync", True))
        self.mouse_angle.set(c.get("angle_snap", False))
        self.mouse_ripple.set(c.get("ripple", False))
        buttons = c.get("buttons", {})
        for i, dd in enumerate(self.mouse_btn_dropdowns):
            act = buttons.get(str(i + 1), msmod.FACTORY_BUTTONS[i + 1])
            if act in self.mouse_btn_keys:
                dd.set_selected(self.mouse_btn_keys.index(act))
        self.mouse_led_off.set(c.get("led_off_minutes", 1))
        self.mouse_sleep.set(c.get("sleep_timer_minutes", 10))
        self.mouse_rate.set(c.get("polling_rate", 1000))


class SkillkorpSuiteApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.connect("startup", self._on_startup)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = SkillkorpSuiteWindow(self)
        win.present()

    def _on_startup(self, app):
        try:
            migrate_legacy_configs()
        except Exception as e:
            print(f"[skillkorp] migration impossible : {e}", file=sys.stderr)
        for name, cb in (("about", self._on_about), ("factory_reset_keyboard", self._on_factory_reset_keyboard),
                         ("restore_mouse_buttons", lambda a, p: self.props.active_window
                          and self.props.active_window.restore_mouse_buttons())):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
        # The indicator comes with the application (its "Quitter" closes both).
        traylaunch.launch_tray()
        quit_act = Gio.SimpleAction.new("quit", None)
        quit_act.connect("activate", lambda a, p: self.quit())
        self.add_action(quit_act)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def _on_about(self, action, param):
        kwargs = dict(application_name="SkillKorp Suite", application_icon="skillkorp-suite",
                      developer_name="Projet SkillKorp Linux", version=__version__,
                      license_type=Gtk.License.MIT_X11, website="https://github.com/ElMajor76/skillkorp-suite",
                      issue_url="https://github.com/ElMajor76/skillkorp-suite/issues",
                      comments="Pilote non officiel du clavier SkillKorp K20 Ultimate et de la souris "
                               "SkillKorp M20 Ultimate.")
        win = self.props.active_window
        if hasattr(Adw, "AboutDialog"):
            Adw.AboutDialog(**kwargs).present(win)
        else:
            Adw.AboutWindow(transient_for=win, **kwargs).present()

    def _on_factory_reset_keyboard(self, action, param):
        win = self.props.active_window
        if not win:
            return

        def reset(_):
            def done(success):
                win._sync_keyboard_ui()
                win.toast("Clavier réinitialisé." if success else f"Réinitialisation refusée. {win.kb_driver.last_error}")
            win.kb_worker.submit(win.kb_driver.factory_reset, done, lambda e: win.toast(describe_error(e)))
        ask(win, "Réinitialisation d'usine du clavier",
            "Tous les réglages du clavier (éclairage, remappages, options) reviendront aux valeurs d'usine.",
            "Réinitialiser", reset, destructive=True)


def main():
    app = SkillkorpSuiteApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
