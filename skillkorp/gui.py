#!/usr/bin/env python3
"""
SkillKorp Suite - Application Graphique Linux (GTK 4 / Libadwaita)

Une seule fenêtre gère les deux périphériques :
- "Vue d'ensemble" : état et batterie du clavier et de la souris côte à côte
- "Clavier" : les 4 onglets K20 existants (RGB, Performance, Clavier, Profils)
- "Souris"  : les 4 onglets M20 existants (Profils, DPI et Capteur, Boutons, Alimentation)
"""

import sys
import os
from typing import Optional, Dict, List, Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from skillkorp.devices.keyboard import (
    SkillkorpK20Driver,
    LIGHT_MODES as KB_LIGHT_MODES,
    SIDE_LIGHT_MODES,
    REMAP_ACTIONS,
    KEY_LAYOUT,
)
from skillkorp.devices.mouse import SkillkorpM20Driver, BUTTON_ACTIONS
from skillkorp.profiles.manager import (
    ProfileManager,
    is_autostart_enabled,
    set_autostart,
)
from skillkorp.profiles.migration import migrate_legacy_configs

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio, Gdk, GObject


class SkillkorpSuiteWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="SkillKorp Suite")
        self.set_default_size(1080, 800)
        self.set_size_request(860, 660)
        self.add_css_class("preferences")

        self.kb_driver = SkillkorpK20Driver()
        self.mouse_driver = SkillkorpM20Driver()
        self.kb_pm = ProfileManager("keyboard")
        self.mouse_pm = ProfileManager("mouse")
        self._updating_ui = False
        self.kb_profile_rows: List[Adw.ActionRow] = []
        self.mouse_current_profile_id = self.mouse_pm.get_active_profile_id()

        self._install_css()

        # Outer layout: ToolbarView + top-level ViewStack (overview / keyboard / mouse)
        self.toolbar_view = Adw.ToolbarView()
        self.outer_stack = Adw.ViewStack()

        self.view_switcher_title = Adw.ViewSwitcherTitle(
            stack=self.outer_stack,
            title="SkillKorp Suite",
        )
        self.header_bar = Adw.HeaderBar(title_widget=self.view_switcher_title)

        # Compact status pills + battery indicators for both devices
        self.kb_status_pill = Gtk.Label(label="⌨ --")
        self.kb_status_pill.add_css_class("status-pill-disconnected")
        self.header_bar.pack_start(self.kb_status_pill)

        self.mouse_status_pill = Gtk.Label(label="🖱 --")
        self.mouse_status_pill.add_css_class("status-pill-disconnected")
        self.header_bar.pack_start(self.mouse_status_pill)

        self.reload_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        self.reload_btn.set_tooltip_text("Actualiser l'état des périphériques")
        self.reload_btn.connect("clicked", lambda b: self._refresh_all())
        self.header_bar.pack_end(self.reload_btn)

        menu = Gio.Menu()
        menu.append("Réinitialiser le clavier (usine)", "app.factory_reset_keyboard")
        menu.append("Restaurer les boutons de la souris", "app.restore_mouse_buttons")
        menu.append("À propos de SkillKorp Suite", "app.about")
        self.menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        self.header_bar.pack_end(self.menu_button)

        self.toolbar_view.add_top_bar(self.header_bar)

        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.outer_stack)
        self.view_switcher_title.bind_property(
            "title-visible", self.switcher_bar, "reveal", GObject.BindingFlags.SYNC_CREATE,
        )
        self.toolbar_view.add_bottom_bar(self.switcher_bar)

        self.toast_overlay = Adw.ToastOverlay(child=self.outer_stack)
        self.toolbar_view.set_content(self.toast_overlay)
        self.set_content(self.toolbar_view)

        # Build the three top-level sections
        self._build_overview_page()
        self._build_keyboard_section()
        self._build_mouse_section()

        self._sync_kb_ui_from_driver()
        self._sync_mouse_ui_from_current_profile()
        self._update_connection_pills()

        GLib.timeout_add_seconds(2, self._on_status_poll_timer)

    def _install_css(self):
        css_provider = Gtk.CssProvider()
        css_provider.load_from_string("""
        .status-pill-connected {
            background-color: rgba(46, 194, 89, 0.2);
            color: #2ec259;
            border-radius: 12px;
            padding: 4px 10px;
            font-weight: bold;
            font-size: 0.85em;
        }
        .status-pill-disconnected {
            background-color: rgba(224, 27, 36, 0.2);
            color: #e01b24;
            border-radius: 12px;
            padding: 4px 10px;
            font-weight: bold;
            font-size: 0.85em;
        }
        .color-palette-btn {
            min-width: 28px;
            min-height: 28px;
            border-radius: 6px;
            border: 1px solid rgba(255, 255, 255, 0.2);
            margin: 2px;
            padding: 0;
        }
        .keyboard-preview-container {
            background-color: rgba(0, 0, 0, 0.15);
            border-radius: 12px;
            padding: 16px;
            border: 1px solid rgba(255, 255, 255, 0.08);
        }
        .color-swatch-red { background-color: #FF0000; }
        .color-swatch-orange { background-color: #FF7700; }
        .color-swatch-yellow { background-color: #FFFF00; }
        .color-swatch-green { background-color: #00FF00; }
        .color-swatch-cyan { background-color: #00FFFF; }
        .color-swatch-blue { background-color: #0066FF; }
        .color-swatch-purple { background-color: #9900FF; }
        .color-swatch-pink { background-color: #FF00AA; }
        .color-swatch-white { background-color: #FFFFFF; }
        .mouse-overlay-btn {
            background-color: transparent;
            border-radius: 8px;
            border: 1px solid transparent;
            transition: all 150ms ease-in-out;
        }
        .mouse-overlay-btn:hover {
            background-color: rgba(53, 132, 228, 0.35);
            border: 1px solid rgba(53, 132, 228, 0.7);
        }
        .mouse-overlay-btn:active {
            background-color: rgba(53, 132, 228, 0.6);
        }
        .button-listbox {
            min-width: 480px;
        }
        """)
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    # =========================================================================
    # Overview page
    # =========================================================================

    def _build_overview_page(self):
        page = Adw.PreferencesPage()
        page.set_title("Vue d'ensemble")
        page.set_icon_name("view-grid-symbolic")

        kb_group = Adw.PreferencesGroup(
            title="⌨ Clavier K20 Ultimate",
            description="État matériel et profil actif du clavier.",
        )
        self.ov_kb_conn_row = Adw.ActionRow(title="Connexion", use_markup=False)
        kb_group.add(self.ov_kb_conn_row)
        self.ov_kb_battery_row = Adw.ActionRow(title="Batterie", use_markup=False)
        kb_group.add(self.ov_kb_battery_row)
        self.ov_kb_profile_row = Adw.ActionRow(title="Profil actif", use_markup=False)
        kb_group.add(self.ov_kb_profile_row)
        btn_open_kb = Gtk.Button(label="Ouvrir les réglages du clavier")
        btn_open_kb.add_css_class("suggested-action")
        btn_open_kb.set_halign(Gtk.Align.START)
        btn_open_kb.set_margin_top(8)
        btn_open_kb.connect("clicked", lambda b: self.outer_stack.set_visible_child_name("keyboard"))
        kb_group.add(btn_open_kb)
        page.add(kb_group)

        mouse_group = Adw.PreferencesGroup(
            title="🖱 Souris M20 Ultimate",
            description="État matériel et profil actif de la souris.",
        )
        self.ov_mouse_conn_row = Adw.ActionRow(title="Connexion", use_markup=False)
        mouse_group.add(self.ov_mouse_conn_row)
        self.ov_mouse_battery_row = Adw.ActionRow(title="Batterie", use_markup=False)
        mouse_group.add(self.ov_mouse_battery_row)
        self.ov_mouse_dpi_row = Adw.ActionRow(title="DPI actif", use_markup=False)
        mouse_group.add(self.ov_mouse_dpi_row)
        self.ov_mouse_profile_row = Adw.ActionRow(title="Profil actif", use_markup=False)
        mouse_group.add(self.ov_mouse_profile_row)
        btn_open_mouse = Gtk.Button(label="Ouvrir les réglages de la souris")
        btn_open_mouse.add_css_class("suggested-action")
        btn_open_mouse.set_halign(Gtk.Align.START)
        btn_open_mouse.set_margin_top(8)
        btn_open_mouse.connect("clicked", lambda b: self.outer_stack.set_visible_child_name("mouse"))
        mouse_group.add(btn_open_mouse)
        page.add(mouse_group)

        self.outer_stack.add_titled(page, "overview", "Vue d'ensemble").set_icon_name("view-grid-symbolic")

    def _refresh_overview(self):
        kb_connected = self.kb_driver.is_connected()
        if kb_connected:
            mode_str = "Sans-fil 2.4GHz" if self.kb_driver.is_wireless() else "Câblé USB"
            self.ov_kb_conn_row.set_subtitle(f"Connecté ({mode_str})")
            bat = self.kb_driver.get_battery()
            pct = bat.get("percentage")
            self.ov_kb_battery_row.set_subtitle(f"{pct}%" if pct is not None else "Indisponible")
        else:
            self.ov_kb_conn_row.set_subtitle("Déconnecté")
            self.ov_kb_battery_row.set_subtitle("--")

        kb_active_id = self.kb_pm.get_active_profile_id()
        kb_prof = self.kb_pm.get_profile(kb_active_id)
        self.ov_kb_profile_row.set_subtitle(kb_prof.get("name", kb_active_id) if kb_prof else kb_active_id)

        mouse_connected = self.mouse_driver.is_connected()
        if mouse_connected:
            self.ov_mouse_conn_row.set_subtitle("Connectée")
            bat = self.mouse_driver.get_battery()
            self.ov_mouse_battery_row.set_subtitle(f"{bat.get('percentage')}%")
            st = self.mouse_driver.query_status()
            self.ov_mouse_dpi_row.set_subtitle(f"{st['active_dpi']} DPI (Étape {st['active_stage']}/{len(st['stages'])})")
        else:
            self.ov_mouse_conn_row.set_subtitle("Déconnectée")
            self.ov_mouse_battery_row.set_subtitle("--")
            self.ov_mouse_dpi_row.set_subtitle("--")

        mouse_active_id = self.mouse_pm.get_active_profile_id()
        mouse_prof = self.mouse_pm.get_profile(mouse_active_id)
        self.ov_mouse_profile_row.set_subtitle(mouse_prof.get("name", mouse_active_id) if mouse_prof else mouse_active_id)

    def _update_connection_pills(self):
        kb_connected = self.kb_driver.is_connected()
        if kb_connected:
            self.kb_status_pill.set_label("⌨ Connecté")
            self.kb_status_pill.remove_css_class("status-pill-disconnected")
            self.kb_status_pill.add_css_class("status-pill-connected")
        else:
            self.kb_status_pill.set_label("⌨ Déconnecté")
            self.kb_status_pill.remove_css_class("status-pill-connected")
            self.kb_status_pill.add_css_class("status-pill-disconnected")

        mouse_connected = self.mouse_driver.is_connected()
        if mouse_connected:
            self.mouse_status_pill.set_label("🖱 Connectée")
            self.mouse_status_pill.remove_css_class("status-pill-disconnected")
            self.mouse_status_pill.add_css_class("status-pill-connected")
        else:
            self.mouse_status_pill.set_label("🖱 Déconnectée")
            self.mouse_status_pill.remove_css_class("status-pill-connected")
            self.mouse_status_pill.add_css_class("status-pill-disconnected")

        self._refresh_overview()

    def _on_status_poll_timer(self) -> bool:
        self._update_connection_pills()
        self._mouse_periodic_refresh()
        return True

    def _refresh_all(self):
        self.kb_driver.dev_path = self.kb_driver.find_device()
        self.mouse_driver.dev_path = self.mouse_driver.find_device()
        self._sync_kb_ui_from_driver()
        self._update_connection_pills()

    def _show_toast(self, message: str):
        toast = Adw.Toast.new(message)
        toast.set_timeout(3)
        self.toast_overlay.add_toast(toast)

    # =========================================================================
    # KEYBOARD SECTION (ported from k20_gui.py)
    # =========================================================================

    def _build_keyboard_section(self):
        outer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.kb_stack = Adw.ViewStack()
        kb_switcher_bar = Adw.ViewSwitcherBar(stack=self.kb_stack, reveal=True)

        self.kb_banner = Adw.Banner(
            title="Clavier SkillKorp K20 non détecté. Branchez le récepteur 2.4GHz ou le câble USB-C.",
            revealed=False,
            button_label="Actualiser",
        )
        self.kb_banner.connect("button-clicked", lambda b: self._refresh_all())

        outer_box.append(self.kb_banner)
        outer_box.append(self.kb_stack)
        outer_box.append(kb_switcher_bar)

        self._build_kb_rgb_page()
        self._build_kb_perf_page()
        self._build_kb_options_page()
        self._build_kb_profiles_page()

        self.outer_stack.add_titled(outer_box, "keyboard", "Clavier").set_icon_name("input-keyboard-symbolic")

    def _build_kb_rgb_page(self):
        page = Adw.PreferencesPage()
        page.set_title("Éclairage RGB")
        page.set_icon_name("weather-clear-symbolic")

        rgb_group = Adw.PreferencesGroup(
            title="Rétroéclairage des Touches",
            description="Personnalisez les effets lumineux, la vitesse et la luminosité du clavier.",
        )

        self.rgb_mode_row = Adw.ComboRow(title="Effet d'éclairage")
        model = Gtk.StringList()
        for key, name, _ in KB_LIGHT_MODES:
            model.append(name)
        self.rgb_mode_row.set_model(model)
        self.rgb_mode_row.connect("notify::selected", self._on_rgb_mode_changed)
        rgb_group.add(self.rgb_mode_row)

        self.rgb_speed_row = Adw.ActionRow(title="Vitesse de l'effet")
        self.rgb_speed_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 5, 1)
        self.rgb_speed_scale.set_draw_value(True)
        self.rgb_speed_scale.set_size_request(220, -1)
        self.rgb_speed_scale.connect("value-changed", self._on_rgb_speed_changed)
        self.rgb_speed_row.add_suffix(self.rgb_speed_scale)
        rgb_group.add(self.rgb_speed_row)

        self.rgb_bright_row = Adw.ActionRow(title="Luminosité")
        self.rgb_bright_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 4, 1)
        self.rgb_bright_scale.set_draw_value(True)
        self.rgb_bright_scale.set_size_request(220, -1)
        self.rgb_bright_scale.connect("value-changed", self._on_rgb_bright_changed)
        self.rgb_bright_row.add_suffix(self.rgb_bright_scale)
        rgb_group.add(self.rgb_bright_row)

        self.rgb_dir_row = Adw.SwitchRow(
            title="Direction de l'onde",
            subtitle="Désactivé : Droite vers Gauche | Activé : Gauche vers Droite",
        )
        self.rgb_dir_row.connect("notify::active", self._on_rgb_dir_changed)
        rgb_group.add(self.rgb_dir_row)

        self.rgb_color_row = Adw.ActionRow(title="Couleur personnalisée", subtitle="Sélectionnez une teinte fixe ou personnalisée")
        palette_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        colors = [
            ("#FF0000", "Rouge", "color-swatch-red"),
            ("#FF7700", "Orange", "color-swatch-orange"),
            ("#FFFF00", "Jaune", "color-swatch-yellow"),
            ("#00FF00", "Vert", "color-swatch-green"),
            ("#00FFFF", "Cyan", "color-swatch-cyan"),
            ("#0066FF", "Bleu", "color-swatch-blue"),
            ("#9900FF", "Violet", "color-swatch-purple"),
            ("#FF00AA", "Rose", "color-swatch-pink"),
            ("#FFFFFF", "Blanc", "color-swatch-white"),
        ]
        for hex_col, name, css_cls in colors:
            btn = Gtk.Button()
            btn.add_css_class("color-palette-btn")
            btn.add_css_class(css_cls)
            btn.set_tooltip_text(name)
            btn.connect("clicked", lambda b, c=hex_col: self._on_palette_color_clicked(c))
            palette_box.append(btn)

        self.color_dialog_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog())
        self.color_dialog_btn.connect("notify::rgba", self._on_color_dialog_changed)
        palette_box.append(self.color_dialog_btn)

        self.rgb_color_row.add_suffix(palette_box)
        rgb_group.add(self.rgb_color_row)
        page.add(rgb_group)

        side_group = Adw.PreferencesGroup(
            title="Bandes LED Latérales",
            description="Contrôle des diffuseurs lumineux latéraux gauche et droit.",
        )

        self.side_mode_row = Adw.ComboRow(title="Effet latéral")
        smodel = Gtk.StringList()
        for key, name, _ in SIDE_LIGHT_MODES:
            smodel.append(name)
        self.side_mode_row.set_model(smodel)
        self.side_mode_row.connect("notify::selected", self._on_side_mode_changed)
        side_group.add(self.side_mode_row)

        self.side_bright_row = Adw.ActionRow(title="Luminosité latérale")
        self.side_bright_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 4, 1)
        self.side_bright_scale.set_draw_value(True)
        self.side_bright_scale.set_size_request(220, -1)
        self.side_bright_scale.connect("value-changed", self._on_side_bright_changed)
        self.side_bright_row.add_suffix(self.side_bright_scale)
        side_group.add(self.side_bright_row)

        page.add(side_group)
        self.kb_stack.add_titled(page, "kb_rgb", "Éclairage RGB").set_icon_name("weather-clear-symbolic")

    def _build_kb_perf_page(self):
        page = Adw.PreferencesPage()
        page.set_title("Performance")
        page.set_icon_name("speedometer-symbolic")

        perf_group = Adw.PreferencesGroup(
            title="Fréquence et Réactivité",
            description="Ajustez la cadence de communication USB et le filtrage mécanique des commutateurs.",
        )

        self.poll_row = Adw.ComboRow(title="Taux de rafraîchissement (Polling Rate)")
        pmodel = Gtk.StringList()
        for hz in [1000, 500, 250, 125]:
            desc = "Recommandé pour le jeu (1 ms)" if hz == 1000 else "Standard" if hz == 500 else "Éco"
            pmodel.append(f"{hz} Hz - {desc}")
        self.poll_row.set_model(pmodel)
        self.poll_row.connect("notify::selected", self._on_poll_changed)
        perf_group.add(self.poll_row)

        self.deb_row = Adw.ActionRow(
            title="Temps d'anti-rebond (Debounce)",
            subtitle="Plus faible = temps de réaction ultra-rapide (2 ms conseillé pour le gaming compétitif)",
        )
        self.deb_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 2, 20, 1)
        self.deb_scale.set_draw_value(True)
        self.deb_scale.set_size_request(220, -1)
        self.deb_scale.connect("value-changed", self._on_deb_changed)
        self.deb_row.add_suffix(self.deb_scale)
        perf_group.add(self.deb_row)
        page.add(perf_group)

        sleep_group = Adw.PreferencesGroup(
            title="Gestion de l'Alimentation et Veille",
            description="Économise la batterie en mode sans-fil 2.4GHz lors des périodes d'inactivité.",
        )

        self.light_sleep_row = Adw.ActionRow(
            title="Veille légère (Extinction RGB)",
            subtitle="Éteint le rétroéclairage après inactivité (rallumage instantané à la frappe)",
        )
        self.light_sleep_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 10, 600, 10)
        self.light_sleep_scale.set_draw_value(True)
        self.light_sleep_scale.set_size_request(220, -1)
        self.light_sleep_scale.connect("value-changed", self._on_sleep_changed)
        self.light_sleep_row.add_suffix(self.light_sleep_scale)
        sleep_group.add(self.light_sleep_row)

        self.deep_sleep_row = Adw.ActionRow(
            title="Veille profonde (Coupure radio)",
            subtitle="Coupe la liaison radio sans-fil après longue inactivité pour préserver la batterie",
        )
        self.deep_sleep_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 600, 7200, 60)
        self.deep_sleep_scale.set_draw_value(True)
        self.deep_sleep_scale.set_size_request(220, -1)
        self.deep_sleep_scale.connect("value-changed", self._on_sleep_changed)
        self.deep_sleep_row.add_suffix(self.deep_sleep_scale)
        sleep_group.add(self.deep_sleep_row)

        page.add(sleep_group)
        self.kb_stack.add_titled(page, "kb_perf", "Performance").set_icon_name("speedometer-symbolic")

    def _build_kb_options_page(self):
        page = Adw.PreferencesPage()
        page.set_title("Clavier")
        page.set_icon_name("input-keyboard-symbolic")

        opts_group = Adw.PreferencesGroup(
            title="Options Matérielles",
            description="Fonctions avancées intégrées au microprogramme du clavier.",
        )

        self.win_lock_row = Adw.SwitchRow(
            title="Verrouiller la touche Windows",
            subtitle="Empêche les retours inopinés sur le bureau pendant les sessions de jeu",
        )
        self.win_lock_row.connect("notify::active", self._on_win_lock_changed)
        opts_group.add(self.win_lock_row)

        self.wasd_swap_row = Adw.SwitchRow(
            title="Échanger ZQSD et les Flèches",
            subtitle="Permet de diriger les déplacements avec ZQSD ou les touches directionnelles",
        )
        self.wasd_swap_row.connect("notify::active", self._on_wasd_swap_changed)
        opts_group.add(self.wasd_swap_row)

        self.gaming_mode_row = Adw.SwitchRow(
            title="Mode Gaming",
            subtitle="Priorité maximale au traitement des frappes simultanées (N-Key Rollover optimisé)",
        )
        self.gaming_mode_row.connect("notify::active", self._on_gaming_mode_changed)
        opts_group.add(self.gaming_mode_row)

        self.os_mode_row = Adw.ComboRow(title="Système d'exploitation cible")
        os_model = Gtk.StringList()
        os_model.append("Windows / Linux (Défaut)")
        os_model.append("macOS (Inversion Cmd/Option)")
        os_model.append("iOS")
        os_model.append("Android")
        self.os_mode_row.set_model(os_model)
        self.os_mode_row.connect("notify::selected", self._on_os_mode_changed)
        opts_group.add(self.os_mode_row)

        page.add(opts_group)

        remap_group = Adw.PreferencesGroup(
            title="Disposition et Remappage des Touches",
            description="Format compact 75% (84 touches, disposition AZERTY France).",
        )

        preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        preview_box.add_css_class("keyboard-preview-container")

        img_path = os.path.join(BASE_DIR, "assets", "keyboard.png")
        if os.path.exists(img_path):
            kb_img = Gtk.Image.new_from_file(img_path)
            kb_img.set_pixel_size(480)
            preview_box.append(kb_img)

        remap_group.add(preview_box)

        remap_ctrl_box = Adw.ActionRow(title="Modifier l'action d'une touche")
        ctrl_h_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        self.key_selector_combo = Gtk.DropDown()
        key_names = [k["name"] for k in KEY_LAYOUT]
        k_model = Gtk.StringList()
        for kn in key_names:
            k_model.append(kn)
        self.key_selector_combo.set_model(k_model)
        ctrl_h_box.append(self.key_selector_combo)

        lbl_to = Gtk.Label(label="➔")
        ctrl_h_box.append(lbl_to)

        self.action_selector_combo = Gtk.DropDown()
        act_model = Gtk.StringList()
        self.remap_action_keys = list(REMAP_ACTIONS.keys())
        for ak in self.remap_action_keys:
            act_model.append(REMAP_ACTIONS[ak][0])
        self.action_selector_combo.set_model(act_model)
        ctrl_h_box.append(self.action_selector_combo)

        self.btn_apply_remap = Gtk.Button(label="Assigner")
        self.btn_apply_remap.add_css_class("suggested-action")
        self.btn_apply_remap.connect("clicked", self._on_apply_remap_clicked)
        ctrl_h_box.append(self.btn_apply_remap)

        remap_ctrl_box.add_suffix(ctrl_h_box)
        remap_group.add(remap_ctrl_box)

        page.add(remap_group)
        self.kb_stack.add_titled(page, "kb_options", "Clavier").set_icon_name("input-keyboard-symbolic")

    def _build_kb_profiles_page(self):
        page = Adw.PreferencesPage()
        page.set_title("Profils")
        page.set_icon_name("folder-symbolic")

        self.kb_profiles_group = Adw.PreferencesGroup(
            title="Profil Actif",
            description="Sélectionnez ou gérez vos profils de configuration.",
        )

        header_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_new_prof = Gtk.Button(label="Nouveau profil", icon_name="list-add-symbolic")
        btn_new_prof.connect("clicked", self._on_kb_new_profile_clicked)
        header_btn_box.append(btn_new_prof)

        btn_import_prof = Gtk.Button(label="Importer", icon_name="document-open-symbolic")
        btn_import_prof.connect("clicked", self._on_kb_import_profile_clicked)
        header_btn_box.append(btn_import_prof)

        btn_export_prof = Gtk.Button(label="Exporter", icon_name="document-save-symbolic")
        btn_export_prof.set_tooltip_text("Exporter le profil actif en fichier JSON")
        btn_export_prof.connect("clicked", lambda b: self._on_kb_export_profile_clicked())
        header_btn_box.append(btn_export_prof)
        self.kb_profiles_group.set_header_suffix(header_btn_box)

        self.kb_profile_combo = Adw.ComboRow(title="Profil en cours d'utilisation")
        self.kb_profile_combo.connect("notify::selected", self._on_kb_profile_combo_changed)
        self.kb_profiles_group.add(self.kb_profile_combo)

        page.add(self.kb_profiles_group)

        self.kb_profiles_list_group = Adw.PreferencesGroup(
            title="Profils Enregistrés",
            description="Liste détaillée des profils stockés.",
        )
        page.add(self.kb_profiles_list_group)

        auto_group = Adw.PreferencesGroup(
            title="Automatisation et Intégration",
            description="Basculez de profil selon l'application ouverte et lancez l'application en arrière-plan.",
        )

        self.kb_auto_switch_row = Adw.SwitchRow(
            title="Bascule automatique des profils",
            subtitle="Détecte la fenêtre active pour appliquer le profil correspondant",
        )
        self.kb_auto_switch_row.set_active(self.kb_pm.is_auto_switch_enabled())
        self.kb_auto_switch_row.connect("notify::active", self._on_kb_auto_switch_toggled)
        auto_group.add(self.kb_auto_switch_row)

        self.autostart_row = Adw.SwitchRow(
            title="Démarrer automatiquement avec la session",
            subtitle="Lance l'indicateur unifié dans la barre des tâches au démarrage du système",
        )
        self.autostart_row.set_active(is_autostart_enabled())
        self.autostart_row.connect("notify::active", self._on_autostart_toggled)
        auto_group.add(self.autostart_row)

        page.add(auto_group)
        self.kb_stack.add_titled(page, "kb_profiles", "Profils").set_icon_name("folder-symbolic")

        self._refresh_kb_profiles_list()

    # --- Keyboard UI sync & handlers -----------------------------------

    def _sync_kb_ui_from_driver(self):
        self._updating_ui = True
        try:
            self.kb_driver.reload_config_if_changed()
            cfg = self.kb_driver.config

            rgb = cfg.get("rgb", {})
            cur_mode = rgb.get("mode", "wave")
            mode_idx = 0
            for idx, (k, _, _) in enumerate(KB_LIGHT_MODES):
                if k == cur_mode:
                    mode_idx = idx
                    break
            self.rgb_mode_row.set_selected(mode_idx)
            self.rgb_speed_scale.set_value(rgb.get("speed", 3))
            self.rgb_bright_scale.set_value(rgb.get("brightness", 4))
            self.rgb_dir_row.set_active(rgb.get("direction", 0) == 1)

            srgb = cfg.get("side_rgb", {})
            cur_smode = srgb.get("mode", "rainbow")
            smode_idx = 0
            for idx, (k, _, _) in enumerate(SIDE_LIGHT_MODES):
                if k == cur_smode:
                    smode_idx = idx
                    break
            self.side_mode_row.set_selected(smode_idx)
            self.side_bright_scale.set_value(srgb.get("brightness", 4))

            rate = cfg.get("polling_rate", 1000)
            rate_map = {1000: 0, 500: 1, 250: 2, 125: 3}
            self.poll_row.set_selected(rate_map.get(rate, 0))
            self.deb_scale.set_value(cfg.get("debounce_ms", 8))

            sleep = cfg.get("sleep", {})
            self.light_sleep_scale.set_value(sleep.get("light_sleep_sec", 120))
            self.deep_sleep_scale.set_value(sleep.get("deep_sleep_sec", 1680))

            opts = cfg.get("options", {})
            self.win_lock_row.set_active(opts.get("win_lock", False))
            self.wasd_swap_row.set_active(opts.get("wasd_swap", False))
            self.gaming_mode_row.set_active(opts.get("gaming_mode", False))

            os_mode_map = {"win": 0, "mac": 1, "ios": 2, "android": 3}
            self.os_mode_row.set_selected(os_mode_map.get(opts.get("os_mode", "win"), 0))

            connected = self.kb_driver.is_connected()
            self.kb_banner.set_revealed(not connected)
        finally:
            self._updating_ui = False

    def _on_rgb_mode_changed(self, row, param):
        if self._updating_ui:
            return
        mode_key = KB_LIGHT_MODES[row.get_selected()][0]
        self.kb_driver.set_rgb(
            mode=mode_key,
            speed=int(self.rgb_speed_scale.get_value()),
            brightness=int(self.rgb_bright_scale.get_value()),
            direction=1 if self.rgb_dir_row.get_active() else 0,
        )

    def _on_rgb_speed_changed(self, scale):
        if self._updating_ui:
            return
        self.kb_driver.set_rgb(
            mode=KB_LIGHT_MODES[self.rgb_mode_row.get_selected()][0],
            speed=int(scale.get_value()),
            brightness=int(self.rgb_bright_scale.get_value()),
            direction=1 if self.rgb_dir_row.get_active() else 0,
        )

    def _on_rgb_bright_changed(self, scale):
        if self._updating_ui:
            return
        self.kb_driver.set_rgb(
            mode=KB_LIGHT_MODES[self.rgb_mode_row.get_selected()][0],
            speed=int(self.rgb_speed_scale.get_value()),
            brightness=int(scale.get_value()),
            direction=1 if self.rgb_dir_row.get_active() else 0,
        )

    def _on_rgb_dir_changed(self, row, param):
        if self._updating_ui:
            return
        self.kb_driver.set_rgb(
            mode=KB_LIGHT_MODES[self.rgb_mode_row.get_selected()][0],
            speed=int(self.rgb_speed_scale.get_value()),
            brightness=int(self.rgb_bright_scale.get_value()),
            direction=1 if row.get_active() else 0,
        )

    def _on_palette_color_clicked(self, hex_col: str):
        self.kb_driver.set_rgb(
            mode="static",
            speed=int(self.rgb_speed_scale.get_value()),
            brightness=int(self.rgb_bright_scale.get_value()),
            color=hex_col,
        )
        self.rgb_mode_row.set_selected(1)

    def _on_color_dialog_changed(self, btn, param):
        rgba = btn.get_rgba()
        r = int(rgba.red * 255)
        g = int(rgba.green * 255)
        b = int(rgba.blue * 255)
        self._on_palette_color_clicked(f"#{r:02X}{g:02X}{b:02X}")

    def _on_side_mode_changed(self, row, param):
        if self._updating_ui:
            return
        smode_key = SIDE_LIGHT_MODES[row.get_selected()][0]
        self.kb_driver.set_side_rgb(mode=smode_key, brightness=int(self.side_bright_scale.get_value()))

    def _on_side_bright_changed(self, scale):
        if self._updating_ui:
            return
        self.kb_driver.set_side_rgb(
            mode=SIDE_LIGHT_MODES[self.side_mode_row.get_selected()][0],
            brightness=int(scale.get_value()),
        )

    def _on_poll_changed(self, row, param):
        if self._updating_ui:
            return
        hz_list = [1000, 500, 250, 125]
        self.kb_driver.set_polling_rate(hz_list[row.get_selected()])

    def _on_deb_changed(self, scale):
        if self._updating_ui:
            return
        self.kb_driver.set_debounce(int(scale.get_value()))

    def _on_sleep_changed(self, scale):
        if self._updating_ui:
            return
        self.kb_driver.set_sleep_time(
            light_sleep_sec=int(self.light_sleep_scale.get_value()),
            deep_sleep_sec=int(self.deep_sleep_scale.get_value()),
        )

    def _on_win_lock_changed(self, row, param):
        if self._updating_ui:
            return
        self.kb_driver.set_keyboard_options(win_lock=row.get_active())

    def _on_wasd_swap_changed(self, row, param):
        if self._updating_ui:
            return
        self.kb_driver.set_keyboard_options(wasd_swap=row.get_active())

    def _on_gaming_mode_changed(self, row, param):
        if self._updating_ui:
            return
        self.kb_driver.set_keyboard_options(gaming_mode=row.get_active())

    def _on_os_mode_changed(self, row, param):
        if self._updating_ui:
            return
        modes = ["win", "mac", "ios", "android"]
        self.kb_driver.set_keyboard_options(os_mode=modes[row.get_selected()])

    def _on_apply_remap_clicked(self, btn):
        key_idx = self.key_selector_combo.get_selected()
        key_name = KEY_LAYOUT[key_idx]["name"]
        act_idx = self.action_selector_combo.get_selected()
        action_key = self.remap_action_keys[act_idx]
        try:
            self.kb_driver.remap_key(key_name, action_key)
            self._show_toast(f"Touche '{key_name}' remappée vers '{REMAP_ACTIONS[action_key][0]}'")
        except Exception as e:
            self._show_toast(f"Erreur: {e}")

    def _refresh_kb_profiles_list(self):
        old_updating = self._updating_ui
        self._updating_ui = True
        try:
            profiles = self.kb_pm.list_profiles()
            self.kb_profile_ids = [p["id"] for p in profiles]
            labels = [f"{p.get('name', p['id'])} ({p.get('description', '')})" for p in profiles]
            self.kb_profile_combo.set_model(Gtk.StringList.new(labels))

            cur_id = self.kb_pm.get_active_profile_id()
            if cur_id in self.kb_profile_ids:
                self.kb_profile_combo.set_selected(self.kb_profile_ids.index(cur_id))

            for r in self.kb_profile_rows:
                self.kb_profiles_list_group.remove(r)
            self.kb_profile_rows = []

            for p in profiles:
                pid = p["id"]
                name = p.get("name", pid)
                desc = p.get("description", "")
                is_active = (pid == cur_id)

                row = Adw.ActionRow()
                row.set_use_markup(False)
                row.set_title(name)
                row.set_subtitle(desc if desc else f"Identifiant : {pid}")
                row.set_activatable(True)

                prefix_icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic" if is_active else "folder-symbolic")
                row.add_prefix(prefix_icon)

                def _make_activate_cb(target_id, target_name):
                    return lambda r: self._switch_kb_profile(target_id, target_name)
                row.connect("activated", _make_activate_cb(pid, name))

                if is_active:
                    badge = Gtk.Label(label="Actif")
                    badge.add_css_class("status-pill-connected")
                    badge.set_valign(Gtk.Align.CENTER)
                    row.add_suffix(badge)

                btn_export = Gtk.Button(icon_name="document-save-symbolic")
                btn_export.set_tooltip_text(f"Exporter le profil '{name}' en JSON")
                btn_export.set_valign(Gtk.Align.CENTER)
                btn_export.connect("clicked", lambda b, target_id=pid: self._on_kb_export_profile_clicked(target_id))
                row.add_suffix(btn_export)

                if pid != "default":
                    btn_del = Gtk.Button(icon_name="user-trash-symbolic")
                    btn_del.set_tooltip_text(f"Supprimer le profil '{name}'")
                    btn_del.add_css_class("destructive-action")
                    btn_del.set_valign(Gtk.Align.CENTER)
                    btn_del.connect("clicked", lambda b, target_id=pid, target_name=name: self._on_kb_delete_profile_clicked(target_id, target_name))
                    row.add_suffix(btn_del)

                self.kb_profiles_list_group.add(row)
                self.kb_profile_rows.append(row)
        finally:
            self._updating_ui = old_updating

    def _switch_kb_profile(self, target_id: str, target_name: Optional[str] = None):
        cur_id = self.kb_pm.get_active_profile_id()
        if target_id != cur_id:
            try:
                prof = self.kb_pm.switch_profile(target_id, driver=self.kb_driver)
                self._sync_kb_ui_from_driver()
                self._refresh_kb_profiles_list()
                self._show_toast(f"Profil '{target_name or prof.get('name', target_id)}' activé")
            except Exception as e:
                self._show_toast(f"Erreur : {e}")

    def _on_kb_profile_combo_changed(self, row, param):
        if self._updating_ui:
            return
        idx = row.get_selected()
        if hasattr(self, "kb_profile_ids") and 0 <= idx < len(self.kb_profile_ids):
            self._switch_kb_profile(self.kb_profile_ids[idx])

    def _on_kb_new_profile_clicked(self, btn):
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Créer un nouveau profil clavier",
            body="Entrez le nom du nouveau profil de configuration.",
        )
        dialog.add_response("cancel", "Annuler")
        dialog.add_response("create", "Créer")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)

        entry = Gtk.Entry(placeholder_text="Nom du profil (ex: CS2 Compétitif)")
        dialog.set_extra_child(entry)

        def on_response(d, resp):
            if resp == "create":
                name = entry.get_text().strip()
                if name:
                    prof_id = name.lower().replace(" ", "_")
                    try:
                        self.kb_pm.create_profile(prof_id, name)
                        self.kb_pm.switch_profile(prof_id, driver=self.kb_driver)
                        self._sync_kb_ui_from_driver()
                        self._refresh_kb_profiles_list()
                        self._show_toast(f"Profil '{name}' créé et activé")
                    except Exception as e:
                        self._show_toast(f"Erreur création profil : {e}")

        dialog.connect("response", on_response)
        dialog.present()

    def _on_kb_import_profile_clicked(self, btn):
        dialog = Gtk.FileDialog()
        dialog.set_title("Importer un profil clavier SkillKorp")
        filter_json = Gtk.FileFilter()
        filter_json.set_name("Fichiers JSON de profil (*.json)")
        filter_json.add_pattern("*.json")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filter_json)
        dialog.set_filters(filters)

        def on_open_finish(d, result):
            try:
                gfile = d.open_finish(result)
                if gfile:
                    path = gfile.get_path()
                    prof_id = self.kb_pm.import_profile(path)
                    self.kb_pm.switch_profile(prof_id, driver=self.kb_driver)
                    self._sync_kb_ui_from_driver()
                    self._refresh_kb_profiles_list()
                    self._show_toast(f"Profil importé : {prof_id}")
            except Exception as e:
                if "dismissed" not in str(e).lower():
                    self._show_toast(f"Erreur d'import : {e}")

        dialog.open(self, None, on_open_finish)

    def _on_kb_export_profile_clicked(self, prof_id: Optional[str] = None):
        if not prof_id:
            prof_id = self.kb_pm.get_active_profile_id()
        prof = self.kb_pm.get_profile(prof_id)
        prof_name = prof.get("name", prof_id) if prof else prof_id

        dialog = Gtk.FileDialog()
        dialog.set_title(f"Exporter le profil '{prof_name}'")
        dialog.set_initial_name(f"{prof_id}.json")
        filter_json = Gtk.FileFilter()
        filter_json.set_name("Fichiers JSON de profil (*.json)")
        filter_json.add_pattern("*.json")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filter_json)
        dialog.set_filters(filters)

        def on_save_finish(d, result):
            try:
                gfile = d.save_finish(result)
                if gfile:
                    path = gfile.get_path()
                    self.kb_pm.export_profile(prof_id, path)
                    self._refresh_kb_profiles_list()
                    self._show_toast(f"Profil '{prof_name}' exporté : {path}")
            except Exception as e:
                if "dismissed" not in str(e).lower():
                    self._show_toast(f"Erreur d'export : {e}")

        dialog.save(self, None, on_save_finish)

    def _on_kb_delete_profile_clicked(self, prof_id: str, prof_name: Optional[str] = None):
        if prof_id == "default":
            self._show_toast("Le profil par défaut ne peut pas être supprimé.")
            return
        if not prof_name:
            prof = self.kb_pm.get_profile(prof_id)
            prof_name = prof.get("name", prof_id) if prof else prof_id

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Supprimer le profil",
            body=f"Voulez-vous vraiment supprimer définitivement le profil '{prof_name}' ({prof_id}) ?",
        )
        dialog.add_response("cancel", "Annuler")
        dialog.add_response("delete", "Supprimer")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(d, resp):
            if resp == "delete":
                try:
                    was_active = (self.kb_pm.get_active_profile_id() == prof_id)
                    self.kb_pm.delete_profile(prof_id)
                    if was_active:
                        self.kb_pm.switch_profile("default", driver=self.kb_driver)
                        self._sync_kb_ui_from_driver()
                    self._refresh_kb_profiles_list()
                    self._show_toast(f"Profil '{prof_name}' supprimé.")
                except Exception as e:
                    self._show_toast(f"Erreur : {e}")

        dialog.connect("response", on_response)
        dialog.present()

    def _on_kb_auto_switch_toggled(self, row, param):
        self.kb_pm.set_auto_switch_enabled(row.get_active())

    def _on_autostart_toggled(self, row, param):
        set_autostart(row.get_active())

    # =========================================================================
    # MOUSE SECTION (ported from m20_gui.py)
    # =========================================================================

    def _build_mouse_section(self):
        outer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.mouse_stack = Adw.ViewStack()
        mouse_switcher_bar = Adw.ViewSwitcherBar(stack=self.mouse_stack, reveal=True)

        self.mouse_banner = Adw.Banner(
            title="Souris SkillKorp M20 non détectée. Branchez le récepteur 2.4GHz ou le câble USB-C.",
            revealed=False,
            button_label="Actualiser",
        )
        self.mouse_banner.connect("button-clicked", lambda b: self._refresh_all())

        outer_box.append(self.mouse_banner)
        outer_box.append(self.mouse_stack)
        outer_box.append(mouse_switcher_bar)

        self._init_mouse_profiles_page()
        self._init_mouse_dpi_page()
        self._init_mouse_buttons_page()
        self._init_mouse_power_page()

        self.outer_stack.add_titled(outer_box, "mouse", "Souris").set_icon_name("input-mouse-symbolic")

    def _init_mouse_profiles_page(self):
        page = Adw.PreferencesPage(title="Profils", icon_name="document-properties-symbolic")
        self.mouse_stack.add_titled_with_icon(page, "mouse_profiles", "Profils", "document-properties-symbolic")

        prof_group = Adw.PreferencesGroup(
            title="Profil de Configuration Actif",
            description="Choisissez ou personnalisez les profils de configuration de la souris",
        )
        page.add(prof_group)

        self.mouse_profile_combo = Adw.ComboRow(title="Profil actuel")
        self._refresh_mouse_profile_combo()
        self.mouse_profile_combo.connect("notify::selected", self._on_mouse_profile_combo_changed)
        prof_group.add(self.mouse_profile_combo)

        actions_group = Adw.PreferencesGroup(title="Gestion des Profils")
        page.add(actions_group)

        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_box.set_halign(Gtk.Align.CENTER)

        new_btn = Gtk.Button(label="Nouveau profil...")
        new_btn.add_css_class("suggested-action")
        new_btn.connect("clicked", self._on_mouse_new_profile_clicked)
        action_box.append(new_btn)

        self.mouse_del_btn = Gtk.Button(label="Supprimer le profil")
        self.mouse_del_btn.add_css_class("destructive-action")
        self.mouse_del_btn.connect("clicked", self._on_mouse_delete_profile_clicked)
        action_box.append(self.mouse_del_btn)

        export_btn = Gtk.Button(label="Exporter...")
        export_btn.connect("clicked", self._on_mouse_export_profile_clicked)
        action_box.append(export_btn)

        import_btn = Gtk.Button(label="Importer...")
        import_btn.connect("clicked", self._on_mouse_import_profile_clicked)
        action_box.append(import_btn)

        actions_group.add(action_box)

        auto_group = Adw.PreferencesGroup(
            title="Bascule Automatique par Jeu / Application",
            description="Permet à la souris d'adapter ses réglages automatiquement selon l'application active",
        )
        page.add(auto_group)

        self.mouse_auto_switch_row = Adw.SwitchRow(
            title="Activer la bascule automatique",
            subtitle="Détecte le lancement de vos jeux (CS2, Valorant...) ou logiciels de bureautique",
        )
        self.mouse_auto_switch_row.set_active(self.mouse_pm.is_auto_switch_enabled())
        self.mouse_auto_switch_row.connect("notify::active", self._on_mouse_auto_switch_toggled)
        auto_group.add(self.mouse_auto_switch_row)

    def _refresh_mouse_profile_combo(self):
        profiles = self.mouse_pm.list_profiles()
        self.mouse_profile_ids = [p["id"] for p in profiles]
        labels = [f"{p.get('name', p['id'])} ({p.get('description', '')})" for p in profiles]
        self.mouse_profile_combo.set_model(Gtk.StringList.new(labels))

        cur_id = self.mouse_pm.get_active_profile_id()
        if cur_id in self.mouse_profile_ids:
            self.mouse_profile_combo.set_selected(self.mouse_profile_ids.index(cur_id))

    def _on_mouse_profile_combo_changed(self, row, param):
        if self._updating_ui:
            return
        idx = row.get_selected()
        if 0 <= idx < len(self.mouse_profile_ids):
            target_id = self.mouse_profile_ids[idx]
            if target_id != self.mouse_current_profile_id:
                try:
                    prof = self.mouse_pm.switch_profile(target_id, driver=self.mouse_driver)
                    self.mouse_current_profile_id = target_id
                    self._sync_mouse_ui_from_current_profile()
                    self._show_toast(f"✓ Profil '{prof.get('name', target_id)}' activé et appliqué !")
                except Exception as e:
                    self._show_toast(f"Erreur : {e}")

    def _on_mouse_new_profile_clicked(self, button):
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Créer un nouveau profil souris",
            body="Entrez le nom de votre nouveau profil de souris :",
        )
        entry = Gtk.Entry()
        entry.set_placeholder_text("Ex : Apex Legends, Montage Vidéo...")
        entry.set_margin_start(24)
        entry.set_margin_end(24)
        dialog.set_extra_child(entry)

        dialog.add_response("cancel", "Annuler")
        dialog.add_response("create", "Créer")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)

        def on_response(dlg, response):
            if response == "create":
                text = entry.get_text().strip()
                if text:
                    safe_id = text.lower().replace(" ", "_")
                    try:
                        self.mouse_pm.create_profile(safe_id, name=text, copy_from=self.mouse_current_profile_id)
                        self.mouse_pm.switch_profile(safe_id, driver=self.mouse_driver)
                        self.mouse_current_profile_id = safe_id
                        self._refresh_mouse_profile_combo()
                        self._sync_mouse_ui_from_current_profile()
                        self._show_toast(f"✓ Profil '{text}' créé avec succès !")
                    except Exception as e:
                        self._show_toast(f"Erreur : {e}")

        dialog.connect("response", on_response)
        dialog.present()

    def _on_mouse_delete_profile_clicked(self, button):
        if self.mouse_current_profile_id == "default":
            self._show_toast("Le profil par défaut ne peut pas être supprimé.")
            return

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Supprimer le profil",
            body=f"Voulez-vous vraiment supprimer définitivement le profil '{self.mouse_current_profile_id}' ?",
        )
        dialog.add_response("cancel", "Annuler")
        dialog.add_response("delete", "Supprimer")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(dlg, response):
            if response == "delete":
                try:
                    self.mouse_pm.delete_profile(self.mouse_current_profile_id)
                    self.mouse_current_profile_id = "default"
                    self._refresh_mouse_profile_combo()
                    self._sync_mouse_ui_from_current_profile()
                    self._show_toast("✓ Profil supprimé.")
                except Exception as e:
                    self._show_toast(f"Erreur : {e}")

        dialog.connect("response", on_response)
        dialog.present()

    def _on_mouse_export_profile_clicked(self, button):
        export_path = os.path.expanduser(f"~/{self.mouse_current_profile_id}.json")
        try:
            self.mouse_pm.export_profile(self.mouse_current_profile_id, export_path)
            self._show_toast(f"✓ Profil exporté dans : {export_path}")
        except Exception as e:
            self._show_toast(f"Erreur d'export : {e}")

    def _on_mouse_import_profile_clicked(self, button):
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Importer un profil JSON",
            body="Indiquez le chemin absolu du fichier JSON à importer :",
        )
        entry = Gtk.Entry()
        entry.set_placeholder_text(os.path.expanduser("~/mon_profil.json"))
        entry.set_margin_start(24)
        entry.set_margin_end(24)
        dialog.set_extra_child(entry)

        dialog.add_response("cancel", "Annuler")
        dialog.add_response("import", "Importer")
        dialog.set_response_appearance("import", Adw.ResponseAppearance.SUGGESTED)

        def on_response(dlg, response):
            if response == "import":
                path = entry.get_text().strip()
                if path and os.path.exists(path):
                    try:
                        new_id = self.mouse_pm.import_profile(path)
                        self.mouse_pm.switch_profile(new_id, driver=self.mouse_driver)
                        self.mouse_current_profile_id = new_id
                        self._refresh_mouse_profile_combo()
                        self._sync_mouse_ui_from_current_profile()
                        self._show_toast(f"✓ Profil '{new_id}' importé avec succès !")
                    except Exception as e:
                        self._show_toast(f"Erreur d'import : {e}")
                else:
                    self._show_toast("Fichier introuvable.")

        dialog.connect("response", on_response)
        dialog.present()

    def _on_mouse_auto_switch_toggled(self, row, param):
        self.mouse_pm.set_auto_switch_enabled(row.get_active())

    def _init_mouse_dpi_page(self):
        page = Adw.PreferencesPage(title="DPI et Capteur", icon_name="input-mouse-symbolic")
        self.mouse_stack.add_titled_with_icon(page, "mouse_dpi", "DPI et Capteur", "input-mouse-symbolic")

        dpi_group = Adw.PreferencesGroup(title="Étapes de Sensibilité (DPI)", description="Capteur PixArt PAW3395 (50 à 26 000 DPI)")
        page.add(dpi_group)

        stages = self.mouse_driver.config.get("dpi_stages", [400, 800, 1600, 3200, 6400, 26000])
        active_idx = self.mouse_driver.config.get("active_stage", 2)

        self.mouse_stage_spinners = []
        self.mouse_stage_radios = []
        first_radio = None

        for i in range(6):
            val = stages[i] if i < len(stages) else 800
            row = Adw.ActionRow(title=f"Étape {i+1}")

            radio = Gtk.CheckButton(label="Actif")
            if first_radio is None:
                first_radio = radio
            else:
                radio.set_group(first_radio)

            if (i + 1) == active_idx:
                radio.set_active(True)

            radio.connect("toggled", self._on_mouse_stage_radio_toggled, i + 1)
            self.mouse_stage_radios.append(radio)
            row.add_prefix(radio)

            adjustment = Gtk.Adjustment(value=val, lower=50, upper=26000, step_increment=50, page_increment=500)
            spin = Gtk.SpinButton(adjustment=adjustment, numeric=True)
            spin.set_valign(Gtk.Align.CENTER)
            self.mouse_stage_spinners.append(spin)
            row.add_suffix(spin)

            dpi_group.add(row)

        sensor_group = Adw.PreferencesGroup(title="Paramètres Avancés du Capteur")
        page.add(sensor_group)

        self.mouse_lod_row = Adw.ComboRow(title="Distance de levée (LOD)", subtitle="Hauteur de coupure du capteur optique")
        self.mouse_lod_row.set_model(Gtk.StringList.new(["1 mm (Bas)", "2 mm (Haut)"]))
        cur_lod = self.mouse_driver.config.get("lod", 1)
        self.mouse_lod_row.set_selected(0 if cur_lod <= 1 else 1)
        sensor_group.add(self.mouse_lod_row)

        cur_deb = self.mouse_driver.config.get("debounce", 4)
        deb_adj = Gtk.Adjustment(value=cur_deb, lower=0, upper=20, step_increment=1, page_increment=2)
        self.mouse_debounce_row = Adw.SpinRow(title="Temps de réponse touches (Debounce)", subtitle="Anti-rebond matériel des switchs", adjustment=deb_adj)
        sensor_group.add(self.mouse_debounce_row)

        self.mouse_msync_row = Adw.SwitchRow(title="Synchronisation du mouvement (Motion Sync)", subtitle="Synchronise les données du capteur aux requêtes USB")
        self.mouse_msync_row.set_active(self.mouse_driver.config.get("motion_sync", True))
        sensor_group.add(self.mouse_msync_row)

        self.mouse_angle_row = Adw.SwitchRow(title="Correction en ligne droite (Angle Snapping)", subtitle="Aide à tracer des lignes droites parfaites")
        self.mouse_angle_row.set_active(self.mouse_driver.config.get("angle_snap", False))
        sensor_group.add(self.mouse_angle_row)

        self.mouse_ripple_row = Adw.SwitchRow(title="Contrôle des ondulations (Ripple Control)", subtitle="Lissage des tremblements à haut DPI")
        self.mouse_ripple_row.set_active(self.mouse_driver.config.get("ripple", False))
        sensor_group.add(self.mouse_ripple_row)

        apply_group = Adw.PreferencesGroup()
        page.add(apply_group)
        btn = Gtk.Button(label="Enregistrer & Appliquer les paramètres DPI")
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.set_halign(Gtk.Align.CENTER)
        btn.connect("clicked", self._on_mouse_apply_dpi_clicked)
        apply_group.add(btn)

    def _init_mouse_buttons_page(self):
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.mouse_stack.add_titled_with_icon(scrolled, "mouse_buttons", "Boutons", "input-gaming-symbolic")

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        content_box.set_margin_start(24)
        content_box.set_margin_end(24)
        content_box.set_margin_top(16)
        content_box.set_margin_bottom(24)
        content_box.set_halign(Gtk.Align.CENTER)

        main_group = Adw.PreferencesGroup(
            title="Attribution Visuelle des Boutons",
            description="Cliquez sur un bouton du schéma ou sélectionnez une action dans la liste",
        )
        content_box.append(main_group)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        hbox.set_margin_top(12)
        hbox.set_margin_bottom(12)
        hbox.set_halign(Gtk.Align.FILL)
        hbox.set_hexpand(True)

        self.mouse_btn_action_keys = list(BUTTON_ACTIONS.keys())
        btn_action_labels = [info[0] for info in BUTTON_ACTIONS.values()]
        button_defs = [
            ("1. Clic Gauche", "Bouton principal (Action par défaut : Clic Gauche)"),
            ("2. Clic Droit", "Bouton secondaire (Action par défaut : Clic Droit)"),
            ("3. Molette", "Bouton central de la molette (Action par défaut : Clic Central)"),
            ("4. Latéral Avant", "Bouton latéral avant (Action par défaut : Suivant)"),
            ("5. Latéral Arrière", "Bouton latéral arrière (Action par défaut : Précédent)"),
            ("6. DPI Cycle", "Bouton de cycle DPI situé sous la souris (Action par défaut : Cycle DPI)"),
        ]
        button_names = [b[0] for b in button_defs]
        button_tooltips = [b[1] for b in button_defs]
        default_actions = ["left_click", "right_click", "middle_click", "forward", "backward", "dpi_cycle"]
        current_buttons = self.mouse_driver.config.get("buttons", {})

        schematic_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        schematic_card.add_css_class("card")
        schematic_card.set_margin_top(4)
        schematic_card.set_margin_bottom(4)
        schematic_card.set_margin_start(4)
        schematic_card.set_margin_end(4)
        schematic_card.set_size_request(240, 420)
        schematic_card.set_halign(Gtk.Align.CENTER)

        overlay = Gtk.Overlay()
        overlay.set_size_request(194, 360)
        overlay.set_halign(Gtk.Align.CENTER)
        overlay.set_margin_top(8)

        img_path = os.path.join(BASE_DIR, "assets", "mouse_view.png")
        if not os.path.exists(img_path):
            img_path = os.path.join(BASE_DIR, "assets", "mouse_clean.png")
        if not os.path.exists(img_path):
            img_path = os.path.join(BASE_DIR, "assets", "m20_schematic.svg")

        if os.path.exists(img_path):
            picture = Gtk.Picture.new_for_filename(img_path)
            picture.set_can_shrink(False)
            picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            picture.set_size_request(194, 360)
            overlay.set_child(picture)

            zones = [
                (1, "1. Clic Gauche", 18, 5, 68, 135),
                (2, "2. Clic Droit", 110, 5, 68, 135),
                (3, "3. Molette (Clic Central)", 84, 28, 26, 72),
                (4, "4. Latéral Avant (Suivant)", 0, 110, 26, 50),
                (5, "5. Latéral Arrière (Précédent)", 0, 166, 26, 52),
            ]
            for btn_num, tooltip, x, y, w, h in zones:
                btn_zone = Gtk.Button()
                btn_zone.add_css_class("flat")
                btn_zone.add_css_class("mouse-overlay-btn")
                btn_zone.set_tooltip_text(tooltip)
                btn_zone.set_halign(Gtk.Align.START)
                btn_zone.set_valign(Gtk.Align.START)
                btn_zone.set_margin_start(x)
                btn_zone.set_margin_top(y)
                btn_zone.set_size_request(w, h)
                btn_zone.connect("clicked", self._on_mouse_schematic_btn_clicked, btn_num)
                overlay.add_overlay(btn_zone)

        schematic_card.append(overlay)

        pill_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        pill_box.set_halign(Gtk.Align.CENTER)
        pill_box.set_margin_top(4)
        pill_box.set_margin_bottom(8)

        for btn_num in range(1, 6):
            btn_pill = Gtk.Button(label=f"[{btn_num}]")
            btn_pill.add_css_class("circular")
            btn_pill.set_tooltip_text(button_tooltips[btn_num - 1])
            btn_pill.connect("clicked", self._on_mouse_schematic_btn_clicked, btn_num)
            pill_box.append(btn_pill)

        btn6_pill = Gtk.Button(label="[6] DPI (dessous)")
        btn6_pill.add_css_class("pill")
        btn6_pill.set_tooltip_text("6. DPI Cycle — Bouton situé sous la souris")
        btn6_pill.connect("clicked", self._on_mouse_schematic_btn_clicked, 6)
        pill_box.append(btn6_pill)

        schematic_card.append(pill_box)
        hbox.append(schematic_card)

        btn_rows_box = Gtk.ListBox()
        btn_rows_box.add_css_class("boxed-list")
        btn_rows_box.add_css_class("button-listbox")
        btn_rows_box.set_selection_mode(Gtk.SelectionMode.NONE)
        btn_rows_box.set_size_request(480, -1)
        btn_rows_box.set_hexpand(True)
        btn_rows_box.set_valign(Gtk.Align.CENTER)

        self.mouse_btn_rows = []
        self.mouse_btn_dropdowns = []
        for i in range(1, 7):
            row = Adw.ActionRow(title=button_names[i - 1])
            row.set_title_lines(1)
            row.set_tooltip_text(button_tooltips[i - 1])

            dd = Gtk.DropDown.new_from_strings(btn_action_labels)
            dd.set_valign(Gtk.Align.CENTER)
            dd.set_enable_search(True)

            cur_act = current_buttons.get(str(i), default_actions[i - 1])
            try:
                selected_idx = self.mouse_btn_action_keys.index(cur_act)
            except ValueError:
                selected_idx = 0
            dd.set_selected(selected_idx)

            row.add_suffix(dd)
            row.set_activatable_widget(dd)

            self.mouse_btn_rows.append(row)
            self.mouse_btn_dropdowns.append(dd)
            btn_rows_box.append(row)

        self.mouse_btn_combos = self.mouse_btn_dropdowns

        hbox.append(btn_rows_box)
        main_group.add(hbox)

        apply_group = Adw.PreferencesGroup()
        content_box.append(apply_group)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btn_box.set_halign(Gtk.Align.CENTER)
        btn_box.set_margin_top(8)
        btn_box.set_margin_bottom(8)

        btn = Gtk.Button(label="Enregistrer & Appliquer les Boutons")
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.connect("clicked", self._on_mouse_apply_buttons_clicked)
        btn_box.append(btn)

        rst_btn = Gtk.Button(label="Restaurer par Défaut")
        rst_btn.add_css_class("destructive-action")
        rst_btn.add_css_class("pill")
        rst_btn.connect("clicked", self._on_mouse_restore_buttons_clicked)
        btn_box.append(rst_btn)

        apply_group.add(btn_box)
        scrolled.set_child(content_box)

    def _on_mouse_schematic_btn_clicked(self, widget, btn_num: int):
        idx = btn_num - 1
        if 0 <= idx < len(self.mouse_btn_rows):
            row = self.mouse_btn_rows[idx]
            row.grab_focus()
            row.activate()
            self._show_toast(f"Bouton {btn_num} sélectionné")

    def _init_mouse_power_page(self):
        page = Adw.PreferencesPage(title="Alimentation et Veille", icon_name="battery-level-100-charged-symbolic")
        self.mouse_stack.add_titled_with_icon(page, "mouse_power", "Alimentation et Veille", "battery-level-100-charged-symbolic")

        bat_group = Adw.PreferencesGroup(title="Batterie et Autonomie")
        page.add(bat_group)

        self.mouse_battery_row = Adw.ActionRow(title="Niveau de batterie", subtitle="100% (Chargée)")
        self.mouse_bat_progress = Gtk.ProgressBar()
        self.mouse_bat_progress.set_fraction(1.0)
        self.mouse_bat_progress.set_valign(Gtk.Align.CENTER)
        self.mouse_bat_progress.set_size_request(160, -1)
        self.mouse_battery_row.add_suffix(self.mouse_bat_progress)
        bat_group.add(self.mouse_battery_row)

        sleep_group = Adw.PreferencesGroup(
            title="Gestion Matérielle de la Mise en Veille",
            description="Configure les seuils de mise en veille automatique de la souris pour maximiser la batterie",
        )
        page.add(sleep_group)

        cur_sleep = self.mouse_driver.config.get("sleep_timer_minutes", 5)
        sleep_adj = Gtk.Adjustment(value=cur_sleep, lower=1, upper=60, step_increment=1, page_increment=5)
        self.mouse_sleep_row = Adw.SpinRow(
            title="Délai d'inactivité avant mise en veille",
            subtitle="Durée sans mouvement avant passage en mode veille profonde (1 à 60 minutes)",
            adjustment=sleep_adj,
        )
        sleep_group.add(self.mouse_sleep_row)

        cur_wake = self.mouse_driver.config.get("move_to_wake", True)
        self.mouse_wake_row = Adw.SwitchRow(
            title="Réveil au mouvement (Move to wake)",
            subtitle="Si désactivé, un clic sur un bouton est requis pour réveiller la souris (idéal en déplacement)",
        )
        self.mouse_wake_row.set_active(cur_wake)
        sleep_group.add(self.mouse_wake_row)

        rate_group = Adw.PreferencesGroup(
            title="Taux de Rapport USB (Polling Rate)",
            description="Fréquence de communication entre la souris et le PC (125 Hz à 1000 Hz)",
        )
        page.add(rate_group)

        self.mouse_rate_row = Adw.ComboRow(title="Fréquence de rapport")
        rate_options = [
            "125 Hz (Économie maximale d'énergie - 8 ms)",
            "250 Hz (Bureautique fluide - 4 ms)",
            "500 Hz (Gaming Standard - 2 ms)",
            "1000 Hz (E-Sports Ultra Rapide - 1 ms)",
        ]
        self.mouse_rate_row.set_model(Gtk.StringList.new(rate_options))
        cur_rate = self.mouse_driver.config.get("polling_rate", 1000)
        rate_idx = 3 if cur_rate == 1000 else 2 if cur_rate == 500 else 1 if cur_rate == 250 else 0
        self.mouse_rate_row.set_selected(rate_idx)
        rate_group.add(self.mouse_rate_row)

        apply_group = Adw.PreferencesGroup()
        page.add(apply_group)
        btn = Gtk.Button(label="Enregistrer & Appliquer les Paramètres d'Alimentation")
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.set_halign(Gtk.Align.CENTER)
        btn.connect("clicked", self._on_mouse_apply_power_clicked)
        apply_group.add(btn)

    # --- Mouse apply handlers & sync -------------------------------------

    def _save_to_mouse_active_profile(self, updates: Dict[str, Any]):
        prof = self.mouse_pm.get_profile(self.mouse_current_profile_id)
        if prof:
            prof.update(updates)
            self.mouse_pm.save_profile(self.mouse_current_profile_id, prof)

    def _on_mouse_apply_dpi_clicked(self, button=None):
        try:
            stages = [int(spin.get_value()) for spin in self.mouse_stage_spinners]
            active_stage = 1
            for idx, r in enumerate(self.mouse_stage_radios):
                if r.get_active():
                    active_stage = idx + 1
                    break

            lod = 1 if self.mouse_lod_row.get_selected() == 0 else 2
            debounce = int(self.mouse_debounce_row.get_value())
            msync = self.mouse_msync_row.get_active()
            angle = self.mouse_angle_row.get_active()
            ripple = self.mouse_ripple_row.get_active()

            self.mouse_driver.set_dpi_and_sensor(
                stages=stages, active_stage=active_stage, lod=lod, debounce=debounce,
                motion_sync=msync, angle_snap=angle, ripple=ripple,
            )
            self._save_to_mouse_active_profile({
                "dpi_stages": stages, "active_stage": active_stage, "lod": lod,
                "debounce": debounce, "motion_sync": msync, "angle_snap": angle, "ripple": ripple,
            })
            self._show_toast("✓ Paramètres DPI et capteur appliqués !")
        except Exception as e:
            self._show_toast(f"Erreur DPI : {e}")

    def _on_mouse_apply_buttons_clicked(self, button=None):
        try:
            btn_map = {}
            for i, combo in enumerate(self.mouse_btn_combos):
                action_idx = combo.get_selected()
                act_key = self.mouse_btn_action_keys[action_idx]
                btn_map[i + 1] = act_key
            self.mouse_driver.set_buttons(btn_map)
            self._save_to_mouse_active_profile({"buttons": {str(k): v for k, v in btn_map.items()}})
            self._show_toast("✓ Mappage des boutons appliqué à la souris !")
        except Exception as e:
            self._show_toast(f"Erreur Boutons : {e}")

    def _on_mouse_apply_power_clicked(self, button=None):
        try:
            rate_idx = self.mouse_rate_row.get_selected()
            rate_hz = [125, 250, 500, 1000][rate_idx]
            self.mouse_driver.set_polling_rate(rate_hz)

            sleep_min = int(self.mouse_sleep_row.get_value())
            move_wake = self.mouse_wake_row.get_active()
            self.mouse_driver.set_power_settings(sleep_timer_minutes=sleep_min, move_to_wake=move_wake)

            self._save_to_mouse_active_profile({
                "polling_rate": rate_hz, "sleep_timer_minutes": sleep_min, "move_to_wake": move_wake,
            })
            self._show_toast(f"✓ Veille ({sleep_min} min) et taux ({rate_hz} Hz) appliqués !")
        except Exception as e:
            self._show_toast(f"Erreur Alimentation : {e}")

    def _on_mouse_restore_buttons_clicked(self, widget=None):
        try:
            self.mouse_driver.restore_factory_buttons()
            default_keys = ["left_click", "right_click", "middle_click", "forward", "backward", "dpi_cycle"]
            for i, combo in enumerate(self.mouse_btn_combos):
                def_key = default_keys[i]
                if def_key in self.mouse_btn_action_keys:
                    combo.set_selected(self.mouse_btn_action_keys.index(def_key))
            self._show_toast("✓ Mappage des boutons d'usine restauré !")
        except Exception as e:
            self._show_toast(f"Erreur : {e}")

    def _on_mouse_stage_radio_toggled(self, radio, stage_num):
        if self._updating_ui:
            return
        if radio.get_active():
            try:
                self.mouse_driver.set_dpi_and_sensor(active_stage=stage_num)
                stages = self.mouse_driver.config.get("dpi_stages", [400, 800, 1600, 3200, 6400, 26000])
                dpi_val = stages[stage_num - 1] if 1 <= stage_num <= len(stages) else ""
                self._show_toast(f"✓ Étape {stage_num} activée ({dpi_val} DPI)")
            except Exception as e:
                self._show_toast(f"Erreur : {e}")

    def _sync_mouse_ui_from_current_profile(self):
        prof = self.mouse_pm.get_profile(self.mouse_current_profile_id)
        if not prof:
            return

        self._updating_ui = True
        try:
            stages = prof.get("dpi_stages", [400, 800, 1600, 3200, 6400, 26000])
            for i, spin in enumerate(self.mouse_stage_spinners):
                if i < len(stages):
                    spin.set_value(stages[i])
            active_stage = prof.get("active_stage", 2)
            if 1 <= active_stage <= len(self.mouse_stage_radios):
                self.mouse_stage_radios[active_stage - 1].set_active(True)

            lod = prof.get("lod", 1)
            self.mouse_lod_row.set_selected(0 if lod <= 1 else 1)
            self.mouse_debounce_row.set_value(prof.get("debounce", 4))
            self.mouse_msync_row.set_active(prof.get("motion_sync", True))
            self.mouse_angle_row.set_active(prof.get("angle_snap", False))
            self.mouse_ripple_row.set_active(prof.get("ripple", False))

            buttons = prof.get("buttons", {})
            default_actions = ["left_click", "right_click", "middle_click", "forward", "backward", "dpi_cycle"]
            for i, combo in enumerate(self.mouse_btn_combos):
                act = buttons.get(str(i + 1), default_actions[i])
                if act in self.mouse_btn_action_keys:
                    combo.set_selected(self.mouse_btn_action_keys.index(act))

            polling = prof.get("polling_rate", 1000)
            rate_idx = 3 if polling == 1000 else 2 if polling == 500 else 1 if polling == 250 else 0
            self.mouse_rate_row.set_selected(rate_idx)
            self.mouse_sleep_row.set_value(prof.get("sleep_timer_minutes", 5))
            self.mouse_wake_row.set_active(prof.get("move_to_wake", True))

            self.mouse_del_btn.set_sensitive(self.mouse_current_profile_id != "default")

            connected = self.mouse_driver.is_connected()
            self.mouse_banner.set_revealed(not connected)
        finally:
            self._updating_ui = False

    def _mouse_periodic_refresh(self):
        st = self.mouse_driver.query_status(poll_hardware=False)
        bat = st["battery"]
        charging = st["charging"]
        ch_str = " (En charge ⚡)" if charging else ""
        self.mouse_battery_row.set_subtitle(f"{bat}%{ch_str}")
        self.mouse_bat_progress.set_fraction(max(0.0, min(1.0, bat / 100.0)))

        active_stage = st.get("active_stage", 2)
        if hasattr(self, "mouse_stage_radios") and 1 <= active_stage <= len(self.mouse_stage_radios):
            radio = self.mouse_stage_radios[active_stage - 1]
            if not radio.get_active():
                self._updating_ui = True
                try:
                    radio.set_active(True)
                finally:
                    self._updating_ui = False

        external_profile_id = self.mouse_pm.get_active_profile_id()
        if external_profile_id != self.mouse_current_profile_id:
            self.mouse_current_profile_id = external_profile_id
            self._updating_ui = True
            try:
                if hasattr(self, "mouse_profile_ids"):
                    for idx, pid in enumerate(self.mouse_profile_ids):
                        if pid == external_profile_id:
                            self.mouse_profile_combo.set_selected(idx)
                            break
                self._sync_mouse_ui_from_current_profile()
            finally:
                self._updating_ui = False


class SkillkorpSuiteApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="io.github.skillkorp.suite",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.connect("startup", self._on_startup)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = SkillkorpSuiteWindow(self)
        win.present()

    def _on_startup(self, app):
        migrate_legacy_configs()

        about_act = Gio.SimpleAction.new("about", None)
        about_act.connect("activate", self._on_about)
        self.add_action(about_act)

        reset_act = Gio.SimpleAction.new("factory_reset_keyboard", None)
        reset_act.connect("activate", self._on_factory_reset_keyboard)
        self.add_action(reset_act)

        restore_btn_act = Gio.SimpleAction.new("restore_mouse_buttons", None)
        restore_btn_act.connect("activate", self._on_restore_mouse_buttons)
        self.add_action(restore_btn_act)

    def _on_about(self, action, param):
        dialog = Adw.AboutDialog(
            application_name="SkillKorp Suite",
            application_icon="skillkorp-suite",
            developer_name="SkillKorp Linux Community",
            version="1.0.0",
            copyright="© 2026 SkillKorp Linux Project",
            license_type=Gtk.License.MIT_X11,
            website="https://github.com/ElMajor76/skillkorp-suite",
            issue_url="https://github.com/ElMajor76/skillkorp-suite/issues",
        )
        dialog.add_legal_section(
            "Garantie et Compatibilité", None, Gtk.License.CUSTOM,
            "Pilote non officiel pour le clavier SkillKorp K20 Ultimate et la souris SkillKorp M20 Ultimate.",
        )
        dialog.present(self.props.active_window)

    def _on_factory_reset_keyboard(self, action, param):
        win = self.props.active_window
        dialog = Adw.MessageDialog(
            transient_for=win,
            heading="Réinitialisation d'usine du clavier",
            body="Voulez-vous vraiment rétablir les réglages d'usine du clavier ? Tous vos profils matériels seront réinitialisés.",
        )
        dialog.add_response("cancel", "Annuler")
        dialog.add_response("reset", "Réinitialiser")
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(d, resp):
            if resp == "reset" and win:
                win.kb_driver.factory_reset()
                win._sync_kb_ui_from_driver()
                win._show_toast("Clavier réinitialisé avec succès.")

        dialog.connect("response", on_response)
        dialog.present()

    def _on_restore_mouse_buttons(self, action, param):
        win = self.props.active_window
        if win:
            win._on_mouse_restore_buttons_clicked()


def main():
    app = SkillkorpSuiteApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
