#!/usr/bin/env python3
"""
skillkorpctl - SkillKorp Suite Command Line Controller

Unifies the former k20ctl (keyboard) and m20ctl (mouse) command-line tools
under one entry point, with subcommands prefixed by device:

    skillkorpctl status                       # combined overview of both devices
    skillkorpctl keyboard <subcommand> ...     # everything k20ctl used to do
    skillkorpctl mouse <subcommand> ...        # everything m20ctl used to do
    skillkorpctl profile <device> ...          # profile management for one device
    skillkorpctl daemon                        # auto-switch profiles for both devices
    skillkorpctl tray {start,stop,status}
    skillkorpctl autostart {enable,disable,status}

All existing --json output flags from the standalone CLIs are preserved.
"""

import sys
import os
import time
import json
import select
import argparse
import subprocess
import signal

from skillkorp.devices.keyboard import (
    SkillkorpK20Driver,
    LIGHT_MODES as KB_LIGHT_MODES,
    SIDE_LIGHT_MODES,
    REMAP_ACTIONS,
)
from skillkorp.devices.mouse import (
    SkillkorpM20Driver,
    BUTTON_ACTIONS,
)
from skillkorp.profiles.manager import (
    ProfileManager,
    detect_active_window_class,
    find_matching_profile,
    is_autostart_enabled,
    set_autostart,
)
from skillkorp.profiles.migration import migrate_legacy_configs


# =============================================================================
# Combined status
# =============================================================================

def cmd_status(args):
    kb = SkillkorpK20Driver()
    mouse = SkillkorpM20Driver()
    kb_pm = ProfileManager("keyboard")
    mouse_pm = ProfileManager("mouse")

    kb_connected = kb.is_connected()
    kb_bat = kb.get_battery()
    kb_prof_id = kb_pm.get_active_profile_id()
    kb_prof = kb_pm.get_profile(kb_prof_id)

    mouse_connected = mouse.is_connected()
    mouse_status = mouse.query_status() if mouse_connected else None
    mouse_prof_id = mouse_pm.get_active_profile_id()
    mouse_prof = mouse_pm.get_profile(mouse_prof_id)

    if getattr(args, "json", False):
        out = {
            "keyboard": {
                "connected": kb_connected,
                "wireless": kb.is_wireless() if kb_connected else False,
                "battery": kb_bat,
                "active_profile": {"id": kb_prof_id, "name": kb_prof.get("name", kb_prof_id) if kb_prof else kb_prof_id},
                "polling_rate_hz": kb.config.get("polling_rate", 1000),
            },
            "mouse": {
                "connected": mouse_connected,
                "wireless": mouse.is_wireless() if mouse_connected else False,
                "battery": mouse.get_battery(),
                "active_profile": {"id": mouse_prof_id, "name": mouse_prof.get("name", mouse_prof_id) if mouse_prof else mouse_prof_id},
                "polling_rate_hz": mouse.config.get("polling_rate", 1000),
                "active_dpi": mouse_status["active_dpi"] if mouse_status else None,
            },
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    print("============================================================")
    print("                    SKILLKORP SUITE                        ")
    print("============================================================")
    print("⌨  Clavier K20 Ultimate")
    if kb_connected:
        mode_str = "Sans-fil 2.4GHz" if kb.is_wireless() else "Filaire USB"
        print(f"   Statut       : \033[92mConnecté\033[0m ({mode_str})")
        pct = kb_bat.get("percentage")
        charge_str = " (En charge ⚡)" if kb_bat.get("charging") else ""
        print(f"   Batterie     : {pct}%{charge_str}" if pct is not None else "   Batterie     : N/A")
    else:
        print("   Statut       : \033[91mDéconnecté\033[0m")
    print(f"   Profil actif : \033[95m{kb_prof.get('name', kb_prof_id) if kb_prof else kb_prof_id}\033[0m")
    print(f"   Taux de poll : {kb.config.get('polling_rate', 1000)} Hz")

    print("------------------------------------------------------------")
    print("🖱  Souris M20 Ultimate")
    if mouse_connected:
        m_bat = mouse.get_battery()
        print("   Statut       : \033[92mConnectée\033[0m")
        charge_str = " (En charge ⚡)" if m_bat.get("charging") else ""
        print(f"   Batterie     : {m_bat.get('percentage')}%{charge_str}")
        print(f"   DPI actif    : {mouse_status['active_dpi']} (Étape {mouse_status['active_stage']}/{len(mouse_status['stages'])})")
    else:
        print("   Statut       : \033[91mDéconnectée\033[0m")
    print(f"   Profil actif : \033[95m{mouse_prof.get('name', mouse_prof_id) if mouse_prof else mouse_prof_id}\033[0m")
    print(f"   Taux de poll : {mouse.config.get('polling_rate', 1000)} Hz")
    print("============================================================")


# =============================================================================
# Keyboard subcommands (ported from k20ctl)
# =============================================================================

def kb_cmd_status(driver: SkillkorpK20Driver, args):
    driver.reload_config_if_changed()
    connected = driver.is_connected()
    is_wl = driver.is_wireless()
    bat = driver.get_battery()

    pm = ProfileManager("keyboard")
    active_prof_id = pm.get_active_profile_id()
    active_prof = pm.get_profile(active_prof_id)
    prof_name = active_prof.get("name", active_prof_id) if active_prof else active_prof_id

    cfg = driver.config
    rgb = cfg.get("rgb", {})
    srgb = cfg.get("side_rgb", {})
    opts = cfg.get("options", {})
    sleep_cfg = cfg.get("sleep", {})

    if getattr(args, "json", False):
        out = {
            "connected": connected,
            "wireless": is_wl,
            "device_node": driver.dev_path,
            "battery": bat,
            "active_profile": {"id": active_prof_id, "name": prof_name},
            "polling_rate_hz": cfg.get("polling_rate", 1000),
            "debounce_ms": cfg.get("debounce_ms", 8),
            "sleep": sleep_cfg,
            "options": opts,
            "rgb": rgb,
            "side_rgb": srgb,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    print("========================================")
    print("       SKILLKORP K20 ULTIMATE           ")
    print("========================================")
    if connected:
        mode_str = "Sans-fil 2.4GHz" if is_wl else "Filaire USB"
        conn_str = f"\033[92mConnecté\033[0m ({mode_str}, {driver.dev_path})"
    else:
        conn_str = "\033[91mDéconnecté\033[0m"
    print(f"Statut        : {conn_str}")

    if connected:
        charge_str = " (En charge ⚡)" if bat.get("charging") else ""
        pct = bat.get("percentage")
        bat_color = "\033[92m" if (pct or 0) > 20 else "\033[91m"
        print(f"Batterie      : {bat_color}{pct}%\033[0m{charge_str}")

    print(f"Profil Actif  : \033[95m{prof_name}\033[0m ({active_prof_id})")
    print(f"Taux de poll  : \033[96m{cfg.get('polling_rate', 1000)} Hz\033[0m")
    print(f"Anti-rebond   : {cfg.get('debounce_ms', 8)} ms")
    print(f"Veille légère : {sleep_cfg.get('light_sleep_sec', 120)} s")
    print(f"Veille prof.  : {sleep_cfg.get('deep_sleep_sec', 1680)} s ({sleep_cfg.get('deep_sleep_sec', 1680)//60} min)")

    win_lock = "\033[91mVerrouillée\033[0m" if opts.get("win_lock") else "\033[92mDéverrouillée\033[0m"
    print(f"Touche Windows: {win_lock}")
    wasd_swap = "\033[93mInversées avec flèches\033[0m" if opts.get("wasd_swap") else "Normal"
    print(f"Touches ZQSD  : {wasd_swap}")
    os_mode = opts.get("os_mode", "win").upper()
    print(f"Mode Système  : {os_mode}")
    gaming_mode = "\033[92mActivé\033[0m" if opts.get("gaming_mode") else "Désactivé"
    print(f"Mode Gaming   : {gaming_mode}")

    mode_label = dict((k, label) for k, label, _ in KB_LIGHT_MODES).get(rgb.get("mode"), rgb.get("mode"))
    dir_str = "Droite" if rgb.get("direction") == 0 else "Gauche"
    print(f"Éclairage RGB : \033[93m{mode_label}\033[0m (Vitesse {rgb.get('speed', 3)}/5, Luminosité {rgb.get('brightness', 4)}/4, {dir_str})")
    if rgb.get("color"):
        print(f"Couleur RGB   : {rgb.get('color')}")

    smode_label = dict((k, label) for k, label, _ in SIDE_LIGHT_MODES).get(srgb.get("mode"), srgb.get("mode"))
    print(f"Bandes LED    : {smode_label} (Luminosité {srgb.get('brightness', 4)}/4)")
    print("========================================")


def kb_cmd_battery(driver: SkillkorpK20Driver, args):
    bat = driver.get_battery()
    if getattr(args, "json", False):
        print(json.dumps(bat, indent=2, ensure_ascii=False))
        return
    if not bat.get("connected"):
        print("\033[91mClavier non connecté.\033[0m")
        sys.exit(1)
    pct = bat.get("percentage")
    charge = " (En charge ⚡)" if bat.get("charging") else ""
    color = "\033[92m" if (pct or 0) > 20 else "\033[91m"
    print(f"Niveau de batterie: {color}{pct}%\033[0m{charge}")


def kb_cmd_rgb(driver: SkillkorpK20Driver, args):
    cfg = driver.config.get("rgb", {})
    mode = args.mode or cfg.get("mode", "wave")
    speed = args.speed if args.speed is not None else cfg.get("speed", 3)
    brightness = args.brightness if args.brightness is not None else cfg.get("brightness", 4)
    direction = args.direction if args.direction is not None else cfg.get("direction", 0)
    color = args.color or cfg.get("color", "#FF0000")

    success = driver.set_rgb(mode=mode, speed=speed, brightness=brightness, direction=direction, color=color)
    if success:
        print(f"\033[92mÉclairage RGB configuré:\033[0m Mode={mode}, Vitesse={speed}, Luminosité={brightness}, Couleur={color}")
    else:
        print("\033[91mÉchec de la configuration RGB.\033[0m", file=sys.stderr)
        sys.exit(1)


def kb_cmd_side_rgb(driver: SkillkorpK20Driver, args):
    cfg = driver.config.get("side_rgb", {})
    mode = args.mode or cfg.get("mode", "rainbow")
    speed = args.speed if args.speed is not None else cfg.get("speed", 3)
    brightness = args.brightness if args.brightness is not None else cfg.get("brightness", 4)
    color = args.color or cfg.get("color", "#00FFCC")

    success = driver.set_side_rgb(mode=mode, speed=speed, brightness=brightness, color=color)
    if success:
        print(f"\033[92mÉclairage latéral configuré:\033[0m Mode={mode}, Vitesse={speed}, Luminosité={brightness}, Couleur={color}")
    else:
        print("\033[91mÉchec de la configuration latérale.\033[0m", file=sys.stderr)
        sys.exit(1)


def kb_cmd_polling_rate(driver: SkillkorpK20Driver, args):
    rate = args.rate
    success = driver.set_polling_rate(rate)
    if success:
        print(f"\033[92mTaux de rafraîchissement configuré à {rate} Hz.\033[0m")
    else:
        print("\033[91mÉchec du réglage du taux de rafraîchissement.\033[0m", file=sys.stderr)
        sys.exit(1)


def kb_cmd_debounce(driver: SkillkorpK20Driver, args):
    ms = args.ms
    success = driver.set_debounce(ms)
    if success:
        print(f"\033[92mTemps d'anti-rebond configuré à {ms} ms.\033[0m")
    else:
        print("\033[91mÉchec du réglage de l'anti-rebond.\033[0m", file=sys.stderr)
        sys.exit(1)


def kb_cmd_sleep(driver: SkillkorpK20Driver, args):
    cfg = driver.config.get("sleep", {})
    light = args.light if args.light is not None else cfg.get("light_sleep_sec", 120)
    deep = args.deep if args.deep is not None else cfg.get("deep_sleep_sec", 1680)

    success = driver.set_sleep_time(light, deep)
    if success:
        print(f"\033[92mDélais de mise en veille configurés:\033[0m Légère={light}s, Profonde={deep}s ({deep//60} min)")
    else:
        print("\033[91mÉchec du réglage des délais de veille.\033[0m", file=sys.stderr)
        sys.exit(1)


def kb_cmd_win_lock(driver: SkillkorpK20Driver, args):
    cur = driver.config.get("options", {}).get("win_lock", False)
    target = (not cur) if args.state == "toggle" else (args.state == "on")

    success = driver.set_keyboard_options(win_lock=target)
    if success:
        st_str = "verrouillée" if target else "déverrouillée"
        print(f"\033[92mTouche Windows {st_str}.\033[0m")
    else:
        print("\033[91mÉchec du réglage du verrouillage Windows.\033[0m", file=sys.stderr)
        sys.exit(1)


def kb_cmd_wasd_swap(driver: SkillkorpK20Driver, args):
    cur = driver.config.get("options", {}).get("wasd_swap", False)
    target = (not cur) if args.state == "toggle" else (args.state == "on")

    success = driver.set_keyboard_options(wasd_swap=target)
    if success:
        st_str = "inversées avec les flèches" if target else "en mode normal"
        print(f"\033[92mTouches ZQSD {st_str}.\033[0m")
    else:
        print("\033[91mÉchec du réglage de l'inversion ZQSD.\033[0m", file=sys.stderr)
        sys.exit(1)


def kb_cmd_os_mode(driver: SkillkorpK20Driver, args):
    mode = args.mode.lower()
    success = driver.set_keyboard_options(os_mode=mode)
    if success:
        print(f"\033[92mMode système configuré: {mode.upper()}.\033[0m")
    else:
        print("\033[91mÉchec du réglage du mode système.\033[0m", file=sys.stderr)
        sys.exit(1)


def kb_cmd_gaming_mode(driver: SkillkorpK20Driver, args):
    target = (args.state == "on")
    success = driver.set_keyboard_options(gaming_mode=target)
    if success:
        st_str = "activé" if target else "désactivé"
        print(f"\033[92mMode Gaming {st_str}.\033[0m")
    else:
        print("\033[91mÉchec du réglage du mode Gaming.\033[0m", file=sys.stderr)
        sys.exit(1)


def kb_cmd_remap(driver: SkillkorpK20Driver, args):
    key = args.key
    action = args.action
    fn_mode = getattr(args, "fn", False)

    try:
        if fn_mode:
            success = driver.remap_fn_key(key, action)
            type_str = "Fn + "
        else:
            success = driver.remap_key(key, action)
            type_str = ""

        if success:
            act_name = REMAP_ACTIONS.get(action, (action, []))[0]
            print(f"\033[92mTouche {type_str}'{key}' remappée vers '{act_name}'.\033[0m")
        else:
            print("\033[91mÉchec du remappage de la touche.\033[0m", file=sys.stderr)
            sys.exit(1)
    except ValueError as e:
        print(f"\033[91mErreur: {e}\033[0m", file=sys.stderr)
        sys.exit(1)


def kb_cmd_reset(driver: SkillkorpK20Driver, args):
    if not args.force:
        confirm = input("Êtes-vous sûr de vouloir réinitialiser le clavier aux réglages d'usine ? (o/N) ")
        if confirm.lower() not in ("o", "oui", "y", "yes"):
            print("Opération annulée.")
            return

    success = driver.factory_reset()
    if success:
        print("\033[92mClavier réinitialisé aux réglages d'usine avec succès.\033[0m")
    else:
        print("\033[91mÉchec de la réinitialisation d'usine.\033[0m", file=sys.stderr)
        sys.exit(1)


def _profile_common(pm: ProfileManager, driver, args, remap_action_keys=None):
    action = args.action

    if action == "list":
        profiles = pm.list_profiles()
        if getattr(args, "json", False):
            print(json.dumps(profiles, indent=2, ensure_ascii=False))
            return
        print("Profils disponibles :")
        for p in profiles:
            star = "\033[92m* \033[0m" if p.get("active") else "  "
            print(f"{star}\033[1m{p['id']:10s}\033[0m - {p.get('name', p['id'])}")
            if p.get("description"):
                print(f"     {p['description']}")
            apps = p.get("auto_switch_apps", [])
            if apps:
                print(f"     Applications: {', '.join(apps)}")

    elif action == "get":
        prof_id = args.profile_id or pm.get_active_profile_id()
        p = pm.get_profile(prof_id)
        if not p:
            print(f"\033[91mProfil '{prof_id}' introuvable.\033[0m", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(p, indent=2, ensure_ascii=False))

    elif action == "set":
        try:
            p = pm.switch_profile(args.profile_id, driver=driver)
            print(f"\033[92mProfil actif : {p.get('name', args.profile_id)} ({args.profile_id})\033[0m")
        except ValueError as e:
            print(f"\033[91mErreur: {e}\033[0m", file=sys.stderr)
            sys.exit(1)

    elif action == "new":
        name = args.name or args.profile_id
        pm.create_profile(args.profile_id, name, args.copy_from)
        print(f"\033[92mProfil '{args.profile_id}' créé avec succès.\033[0m")

    elif action == "delete":
        try:
            pm.delete_profile(args.profile_id)
            print(f"\033[92mProfil '{args.profile_id}' supprimé avec succès.\033[0m")
        except ValueError as e:
            print(f"\033[91mErreur: {e}\033[0m", file=sys.stderr)
            sys.exit(1)

    elif action == "export":
        try:
            pm.export_profile(args.profile_id, args.file)
            print(f"\033[92mProfil '{args.profile_id}' exporté vers {args.file}.\033[0m")
        except ValueError as e:
            print(f"\033[91mErreur: {e}\033[0m", file=sys.stderr)
            sys.exit(1)

    elif action == "import":
        try:
            safe_id = pm.import_profile(args.file, args.profile_id)
            print(f"\033[92mProfil importé sous l'identifiant '{safe_id}'.\033[0m")
        except Exception as e:
            print(f"\033[91mErreur lors de l'import: {e}\033[0m", file=sys.stderr)
            sys.exit(1)


def kb_cmd_profile(driver: SkillkorpK20Driver, args):
    _profile_common(ProfileManager("keyboard"), driver, args)


# =============================================================================
# Mouse subcommands (ported from m20ctl)
# =============================================================================

def mouse_cmd_status(driver: SkillkorpM20Driver, args):
    status = driver.query_status(poll_hardware=True)
    pm = ProfileManager("mouse")
    active_prof_id = pm.get_active_profile_id()
    active_prof = pm.get_profile(active_prof_id)
    prof_display = f"{active_prof.get('name', active_prof_id)} ({active_prof_id})" if active_prof else active_prof_id

    if getattr(args, "json", False):
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return

    print("========================================")
    print("       SKILLKORP M20 ULTIMATE           ")
    print("========================================")
    conn_str = "\033[92mConnectée\033[0m" if status["connected"] else "\033[91mDéconnectée\033[0m"
    print(f"Statut        : {conn_str} ({status['device'] or 'N/A'})")

    charge_str = " (En charge ⚡)" if status["charging"] else ""
    bat_color = "\033[92m" if status["battery"] > 20 else "\033[91m"
    print(f"Batterie      : {bat_color}{status['battery']}%\033[0m{charge_str}")

    print(f"Profil Actif  : \033[95m{prof_display}\033[0m")
    print(f"Taux de poll  : {status['polling_rate_hz']} Hz")
    print(f"DPI Actif     : \033[96m{status['active_dpi']} DPI\033[0m (Étape {status['active_stage']}/{len(status['stages'])})")
    stages_str = ", ".join(f"[{s}]" if i + 1 == status['active_stage'] else str(s) for i, s in enumerate(status['stages']))
    print(f"Étapes DPI    : {stages_str}")
    print(f"Distance LOD  : {status['lod_mm']} mm")
    print(f"Debounce      : {status['debounce_ms']} ms")
    print(f"Motion Sync   : {'Activé' if status['motion_sync'] else 'Désactivé'}")
    print(f"Angle Snap    : {'Activé' if status['angle_snapping'] else 'Désactivé'}")
    print(f"Ripple Control: {'Activé' if status['ripple_control'] else 'Désactivé'}")
    print(f"Mise en veille: {status.get('sleep_timer_minutes', 5)} min")
    wake_str = "Mouvement (Move to wake)" if status.get("move_to_wake", True) else "Clic requis (Click to wake)"
    print(f"Mode de réveil: {wake_str}")
    print("========================================")


def mouse_cmd_dpi(driver: SkillkorpM20Driver, args):
    stages = None
    if args.stages:
        stages = [int(s.strip()) for s in args.stages.split(",")]
    active = args.active

    if stages is None and active is None:
        status = driver.query_status()
        print(f"DPI Actif: {status['active_dpi']} (Étape {status['active_stage']})")
        print(f"Étapes   : {status['stages']}")
        return

    driver.set_dpi_and_sensor(stages=stages, active_stage=active)
    new_status = driver.query_status()
    print(f"✓ DPI mis à jour : {new_status['active_dpi']} DPI (Étape {new_status['active_stage']})")
    print(f"  Étapes configurées : {new_status['stages']}")


def mouse_cmd_rate(driver: SkillkorpM20Driver, args):
    if args.set:
        driver.set_polling_rate(args.set)
        print(f"✓ Taux de scrutation configuré à {args.set} Hz.")
    else:
        status = driver.query_status()
        print(f"Taux de scrutation actuel : {status['polling_rate_hz']} Hz")


def mouse_cmd_sensor(driver: SkillkorpM20Driver, args):
    kwargs = {}
    if args.lod is not None:
        kwargs["lod"] = args.lod
    if args.debounce is not None:
        kwargs["debounce"] = args.debounce
    if args.motion_sync is not None:
        kwargs["motion_sync"] = (args.motion_sync.lower() in ["on", "1", "true", "oui"])
    if args.angle_snap is not None:
        kwargs["angle_snap"] = (args.angle_snap.lower() in ["on", "1", "true", "oui"])
    if args.ripple is not None:
        kwargs["ripple"] = (args.ripple.lower() in ["on", "1", "true", "oui"])

    if not kwargs:
        st = driver.query_status()
        print(f"Distance de levée (LOD) : {st['lod_mm']} mm")
        print(f"Temps de réponse touches : {st['debounce_ms']} ms")
        print(f"Motion Sync              : {st['motion_sync']}")
        print(f"Angle Snapping           : {st['angle_snapping']}")
        print(f"Ripple Control           : {st['ripple_control']}")
        return

    driver.set_dpi_and_sensor(**kwargs)
    st = driver.query_status()
    print("✓ Paramètres capteur mis à jour :")
    print(f"  LOD: {st['lod_mm']}mm | Debounce: {st['debounce_ms']}ms | MotionSync: {st['motion_sync']} | AngleSnap: {st['angle_snapping']} | Ripple: {st['ripple_control']}")


def mouse_cmd_power(driver: SkillkorpM20Driver, args):
    sleep_min = args.sleep_timer
    wake_mode = args.wake_mode

    if sleep_min is None and wake_mode is None:
        st = driver.query_status()
        s_min = st.get("sleep_timer_minutes", 5)
        m_wake = st.get("move_to_wake", True)
        print("Paramètres d'alimentation et veille actuels :")
        print(f"  Délai de mise en veille : {s_min} minutes")
        print(f"  Mode de réveil           : {'Mouvement (Move to wake)' if m_wake else 'Clic requis (Click to wake)'}")
        return

    cur_sleep = driver.config.get("sleep_timer_minutes", 5)
    cur_wake = driver.config.get("move_to_wake", True)

    new_sleep = sleep_min if sleep_min is not None else cur_sleep
    new_wake = (wake_mode.lower() == "move") if wake_mode is not None else cur_wake

    driver.set_power_settings(sleep_timer_minutes=new_sleep, move_to_wake=new_wake)
    print("✓ Paramètres d'alimentation mis à jour :")
    print(f"  Délai de veille: {new_sleep} min | Réveil: {'Mouvement' if new_wake else 'Clic requis'}")


def mouse_cmd_button(driver: SkillkorpM20Driver, args):
    if args.list_actions:
        print("Actions configurables disponibles :")
        for key, info in BUTTON_ACTIONS.items():
            print(f"  {key:<20} : {info[0]}")
        return

    if args.btn and args.action:
        if args.action not in BUTTON_ACTIONS:
            print(f"Erreur: action '{args.action}' inconnue. Utilisez 'skillkorpctl mouse button --list-actions'.")
            sys.exit(1)
        driver.set_buttons({args.btn: args.action})
        print(f"✓ Bouton {args.btn} réassigné à '{BUTTON_ACTIONS[args.action][0]}'")
    else:
        buttons = driver.config.get("buttons", {})
        print("Affectation actuelle des boutons :")
        for b in range(1, 7):
            act = buttons.get(str(b), "inconnu")
            desc = BUTTON_ACTIONS.get(act, (act,))[0]
            print(f"  Bouton {b}: {desc} ({act})")


def mouse_cmd_restore_buttons(driver: SkillkorpM20Driver, args):
    driver.restore_factory_buttons()
    print("✓ Mappage d'usine des boutons restauré (clic gauche, clic droit, molette, défilement et latéraux).")


def mouse_cmd_profile(driver: SkillkorpM20Driver, args):
    pm = ProfileManager("mouse")
    action = args.profile_action or "list"

    if action == "list":
        profiles = pm.list_profiles()
        print("Profils configurés :")
        for p in profiles:
            star = "\033[92m* \033[0m" if p.get("active") else "  "
            desc = p.get("description", "")
            print(f"{star}\033[1m{p['id']:<16}\033[0m - {p.get('name', p['id'])} ({desc})")
        print("\nUtilisez 'skillkorpctl mouse profile switch <id>' pour activer un profil.")

    elif action == "switch":
        if not args.id:
            print("Erreur: Spécifiez l'identifiant du profil. Ex: skillkorpctl mouse profile switch gaming_fps")
            sys.exit(1)
        try:
            prof = pm.switch_profile(args.id, driver=driver)
            print(f"✓ Profil activé : \033[92m{prof.get('name', args.id)}\033[0m ({args.id})")
        except Exception as e:
            print(f"Erreur: {e}")
            sys.exit(1)

    elif action == "create":
        if not args.id:
            print("Erreur: Spécifiez l'identifiant du profil à créer. Ex: skillkorpctl mouse profile create my_profile")
            sys.exit(1)
        name = args.name or args.id.replace("_", " ").title()
        prof = pm.create_profile(args.id, name=name, copy_from=args.copy_from)
        print(f"✓ Profil créé avec succès : {prof.get('name')} (id: {prof.get('id')})")

    elif action == "delete":
        if not args.id:
            print("Erreur: Spécifiez l'identifiant du profil à supprimer.")
            sys.exit(1)
        try:
            if pm.delete_profile(args.id):
                print(f"✓ Profil '{args.id}' supprimé.")
            else:
                print(f"Erreur: Profil '{args.id}' introuvable.")
        except Exception as e:
            print(f"Erreur: {e}")
            sys.exit(1)

    elif action == "export":
        if not args.id or not args.file:
            print("Erreur: Spécifiez l'id du profil et le chemin cible. Ex: skillkorpctl mouse profile export gaming_fps ~/gaming.json")
            sys.exit(1)
        try:
            pm.export_profile(args.id, args.file)
            print(f"✓ Profil '{args.id}' exporté vers {args.file}")
        except Exception as e:
            print(f"Erreur: {e}")
            sys.exit(1)

    elif action == "import":
        if not args.file:
            print("Erreur: Spécifiez le fichier à importer. Ex: skillkorpctl mouse profile import ~/gaming.json")
            sys.exit(1)
        try:
            new_id = pm.import_profile(args.file, new_id=args.id)
            print(f"✓ Profil importé avec succès sous l'identifiant '{new_id}'.")
        except Exception as e:
            print(f"Erreur lors de l'import: {e}")
            sys.exit(1)


def mouse_cmd_monitor(driver: SkillkorpM20Driver, args):
    if not driver.is_connected():
        print("Souris SkillKorp M20 non connectée.")
        return
    print("========================================", flush=True)
    print("       ÉCOUTE EN DIRECT DES ÉVÉNEMENTS  ", flush=True)
    print("========================================", flush=True)
    print("Appuyez sur le bouton physique DPI de la souris pour tester le changement.", flush=True)
    print("Appuyez sur Ctrl+C pour quitter.\n", flush=True)

    fd = os.open(driver.dev_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        while True:
            r, _, _ = select.select([fd], [], [], 1.0)
            if r:
                try:
                    data = os.read(fd, 64)
                    if len(data) >= 5 and data[0] == 0x03:
                        w_param = data[1] | (data[2] << 8)
                        if (w_param & 0xFF00) in (0x1000, 0x2000):
                            stage = data[3]
                            stages = driver.config.get("dpi_stages", [400, 800, 1600, 3200, 6400, 26000])
                            dpi_val = stages[stage - 1] if 1 <= stage <= len(stages) else "inconnu"
                            driver.config["active_stage"] = stage
                            driver.save_config()
                            print(f"⚡ Bouton DPI pressé -> \033[92mÉtape {stage}/{len(stages)}\033[0m : \033[96m{dpi_val} DPI\033[0m", flush=True)
                        elif w_param & 0xFF00 == 0x4000:
                            bat = data[4]
                            charging = (data[3] == 2)
                            ch = " (en charge ⚡)" if charging else ""
                            print(f"🔋 Batterie : {bat}%{ch}", flush=True)
                except BlockingIOError:
                    pass
    except KeyboardInterrupt:
        print("\nArrêt de l'écoute.", flush=True)
    finally:
        os.close(fd)


def mouse_cmd_apply(driver: SkillkorpM20Driver, args):
    print("Application de tous les paramètres enregistrés vers la souris...")
    success = driver.apply_all()
    if success:
        print("✓ Tous les paramètres ont été appliqués avec succès.")
    else:
        print("✗ Erreur lors de l'envoi de certains paramètres.")


# =============================================================================
# Shared: tray, autostart, daemon
# =============================================================================

def get_tray_pids() -> list:
    """Return list of running skillkorp-tray PIDs (PID file first, pgrep fallback)."""
    pid_file = os.path.expanduser("~/.config/skillkorp/tray.pid")
    my_pid = os.getpid()
    pids = []

    if os.path.exists(pid_file):
        try:
            stored_pid = int(open(pid_file).read().strip())
            if stored_pid != my_pid:
                os.kill(stored_pid, 0)
                pids.append(stored_pid)
        except (ValueError, OSError):
            try:
                os.remove(pid_file)
            except OSError:
                pass

    if not pids:
        try:
            out = subprocess.check_output(
                ["pgrep", "-f", r"skillkorp\.tray|skillkorp-tray"],
                text=True,
            )
            for line in out.strip().splitlines():
                try:
                    p = int(line.strip())
                    if p != my_pid and p not in pids:
                        pids.append(p)
                except ValueError:
                    pass
        except subprocess.CalledProcessError:
            pass

    return pids


def cmd_tray(args):
    action = args.tray_action or "status"
    pids = get_tray_pids()

    if action == "start":
        if pids:
            print(f"L'indicateur de barre des tâches est déjà en cours d'exécution (PID: {pids[0]}).")
            return
        if getattr(args, "foreground", False):
            from skillkorp.tray import main as tray_main
            tray_main()
            return
        cmd = [sys.executable, "-m", "skillkorp.tray"]
        env = dict(os.environ)
        # Ensure the spawned interpreter can import the "skillkorp" package
        # regardless of whether we're running from a source checkout or an
        # installed package (a fresh subprocess does not inherit sys.path).
        import skillkorp as _skillkorp_pkg
        pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(_skillkorp_pkg.__file__)))
        env["PYTHONPATH"] = pkg_parent + os.pathsep + env.get("PYTHONPATH", "")
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=env)
        time.sleep(0.5)
        new_pids = get_tray_pids()
        if new_pids:
            print(f"✓ Indicateur démarré en arrière-plan (PID: {new_pids[0]}).")
        else:
            print("✗ Erreur: Impossible de démarrer l'indicateur.")

    elif action == "stop":
        if not pids:
            print("L'indicateur n'est pas en cours d'exécution.")
            return
        for p in pids:
            try:
                os.kill(p, signal.SIGTERM)
            except ProcessLookupError:
                pass
        print(f"✓ Indicateur arrêté (PIDs: {pids}).")

    elif action == "status":
        if pids:
            print(f"● L'indicateur de barre des tâches est actif (PID: {pids[0]}).")
        else:
            print("○ L'indicateur de barre des tâches n'est pas actif.")


def cmd_autostart(args):
    action = args.autostart_action or "status"
    if action == "enable":
        set_autostart(True)
        print("✓ Lancement automatique activé (~/.config/autostart/io.github.skillkorp.suite.tray.desktop).")
    elif action == "disable":
        set_autostart(False)
        print("✓ Lancement automatique désactivé.")
    elif action == "status":
        enabled = is_autostart_enabled()
        status_str = "\033[92mActivé\033[0m" if enabled else "\033[90mDésactivé\033[0m"
        print(f"Lancement automatique au démarrage : {status_str}")


def cmd_daemon(args):
    """Auto-switch profiles for both the keyboard and the mouse based on the active window.

    When the same application matches an auto_switch_apps entry on both a
    keyboard profile and a mouse profile, both are switched on the same tick.
    """
    kb_driver = SkillkorpK20Driver()
    mouse_driver = SkillkorpM20Driver()
    kb_pm = ProfileManager("keyboard")
    mouse_pm = ProfileManager("mouse")

    print("Démarrage du démon de bascule automatique de profils SkillKorp Suite...")
    last_class = None
    running = True

    def sig_handler(signum, frame):
        nonlocal running
        running = False
        print("\nArrêt du démon.")

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    while running:
        cls = detect_active_window_class()
        if cls and cls != last_class:
            last_class = cls

            if kb_pm.is_auto_switch_enabled():
                matched = find_matching_profile(kb_pm.list_profiles(), cls)
                if matched and matched != kb_pm.get_active_profile_id():
                    print(f"[Clavier] Application active: {cls} -> Bascule vers le profil '{matched}'")
                    kb_pm.switch_profile(matched, driver=kb_driver)

            if mouse_pm.is_auto_switch_enabled():
                matched = find_matching_profile(mouse_pm.list_profiles(), cls)
                if matched and matched != mouse_pm.get_active_profile_id():
                    print(f"[Souris] Application active: {cls} -> Bascule vers le profil '{matched}'")
                    mouse_pm.switch_profile(matched, driver=mouse_driver)

        time.sleep(1.0)


# =============================================================================
# Argument parsing
# =============================================================================

def _build_keyboard_subparser(subparsers, common_parser):
    p_kb = subparsers.add_parser("keyboard", help="Commandes du clavier SkillKorp K20 Ultimate")
    kb_sub = p_kb.add_subparsers(dest="kb_command")

    kb_sub.add_parser("status", parents=[common_parser], help="Afficher l'état complet du clavier")
    kb_sub.add_parser("battery", parents=[common_parser], help="Afficher l'état et le pourcentage de batterie")

    p_rgb = kb_sub.add_parser("rgb", parents=[common_parser], help="Configurer l'éclairage RGB des touches")
    p_rgb.add_argument("--mode", choices=[k for k, _, _ in KB_LIGHT_MODES])
    p_rgb.add_argument("--speed", type=int, choices=range(1, 6))
    p_rgb.add_argument("--brightness", type=int, choices=range(0, 5))
    p_rgb.add_argument("--direction", type=int, choices=[0, 1])
    p_rgb.add_argument("--color")

    p_srgb = kb_sub.add_parser("side-rgb", parents=[common_parser], help="Configurer les bandes LED latérales")
    p_srgb.add_argument("--mode", choices=[k for k, _, _ in SIDE_LIGHT_MODES])
    p_srgb.add_argument("--speed", type=int, choices=range(1, 6))
    p_srgb.add_argument("--brightness", type=int, choices=range(0, 5))
    p_srgb.add_argument("--color")

    p_poll = kb_sub.add_parser("polling-rate", parents=[common_parser], help="Configurer le taux de rafraîchissement")
    p_poll.add_argument("rate", type=int, choices=[125, 250, 500, 1000])

    p_deb = kb_sub.add_parser("debounce", parents=[common_parser], help="Configurer le temps d'anti-rebond")
    p_deb.add_argument("ms", type=int, choices=range(2, 21))

    p_sleep = kb_sub.add_parser("sleep", parents=[common_parser], help="Configurer les délais de mise en veille")
    p_sleep.add_argument("--light", type=int)
    p_sleep.add_argument("--deep", type=int)

    p_win = kb_sub.add_parser("win-lock", parents=[common_parser], help="Verrouiller/déverrouiller la touche Windows")
    p_win.add_argument("state", choices=["on", "off", "toggle"])

    p_wasd = kb_sub.add_parser("wasd-swap", parents=[common_parser], help="Inverser les touches ZQSD et les flèches")
    p_wasd.add_argument("state", choices=["on", "off", "toggle"])

    p_os = kb_sub.add_parser("os-mode", parents=[common_parser], help="Configurer le mode de compatibilité système")
    p_os.add_argument("mode", choices=["win", "mac", "ios", "android"])

    p_game = kb_sub.add_parser("gaming-mode", parents=[common_parser], help="Activer ou désactiver le mode Gaming")
    p_game.add_argument("state", choices=["on", "off"])

    p_remap = kb_sub.add_parser("remap", parents=[common_parser], help="Remapper une touche standard ou Fn")
    p_remap.add_argument("key")
    p_remap.add_argument("action", choices=list(REMAP_ACTIONS.keys()))
    p_remap.add_argument("--fn", action="store_true")

    p_reset = kb_sub.add_parser("reset", parents=[common_parser], help="Réinitialiser le clavier aux réglages d'usine")
    p_reset.add_argument("-f", "--force", action="store_true")

    p_prof = kb_sub.add_parser("profile", parents=[common_parser], help="Gestion des profils du clavier")
    p_prof.add_argument("action", choices=["list", "get", "set", "new", "delete", "export", "import"])
    p_prof.add_argument("profile_id", nargs="?")
    p_prof.add_argument("--name")
    p_prof.add_argument("--copy-from")
    p_prof.add_argument("--file")

    return p_kb


def _build_mouse_subparser(subparsers, common_parser):
    p_mouse = subparsers.add_parser("mouse", help="Commandes de la souris SkillKorp M20 Ultimate")
    mouse_sub = p_mouse.add_subparsers(dest="mouse_command")

    mouse_sub.add_parser("status", parents=[common_parser], help="Afficher l'état complet de la souris et de la batterie")

    p_dpi = mouse_sub.add_parser("dpi", parents=[common_parser], help="Configurer les étapes DPI")
    p_dpi.add_argument("--stages", type=str)
    p_dpi.add_argument("--active", type=int)

    p_rate = mouse_sub.add_parser("rate", parents=[common_parser], help="Taux de scrutation (polling rate)")
    p_rate.add_argument("--set", type=int, choices=[125, 250, 500, 1000])

    p_sensor = mouse_sub.add_parser("sensor", parents=[common_parser], help="Paramètres avancés du capteur PixArt PAW3395")
    p_sensor.add_argument("--lod", type=int, choices=[1, 2])
    p_sensor.add_argument("--debounce", type=int)
    p_sensor.add_argument("--motion-sync", type=str, choices=["on", "off"])
    p_sensor.add_argument("--angle-snap", type=str, choices=["on", "off"])
    p_sensor.add_argument("--ripple", type=str, choices=["on", "off"])

    p_power = mouse_sub.add_parser("power", parents=[common_parser], help="Paramètres d'alimentation, veille et mode de réveil")
    p_power.add_argument("--sleep-timer", type=int, choices=range(1, 61))
    p_power.add_argument("--wake-mode", type=str, choices=["move", "click"])

    p_button = mouse_sub.add_parser("button", parents=[common_parser], help="Configuration des boutons")
    p_button.add_argument("--btn", type=int, choices=range(1, 7))
    p_button.add_argument("--action", type=str)
    p_button.add_argument("--list-actions", action="store_true")

    mouse_sub.add_parser("restore-buttons", parents=[common_parser], help="Restaurer le mappage par défaut d'usine des boutons")

    p_prof = mouse_sub.add_parser("profile", parents=[common_parser], help="Gestion multi-profils de la souris")
    p_prof.add_argument("profile_action", nargs="?", choices=["list", "switch", "create", "delete", "export", "import"], default="list")
    p_prof.add_argument("id", nargs="?")
    p_prof.add_argument("--name", type=str)
    p_prof.add_argument("--copy-from", type=str)
    p_prof.add_argument("--file", type=str)

    mouse_sub.add_parser("monitor", parents=[common_parser], help="Écouter en direct les changements de DPI et état batterie")
    mouse_sub.add_parser("apply", parents=[common_parser], help="Ré-appliquer la configuration enregistrée")

    return p_mouse


def main():
    migrate_legacy_configs()

    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument("--json", action="store_true", help="Sortie au format JSON pour scripts")

    parser = argparse.ArgumentParser(
        prog="skillkorpctl",
        description="Utilitaire de configuration en ligne de commande pour SkillKorp Suite (clavier K20 & souris M20).",
        parents=[common_parser],
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("status", parents=[common_parser], help="Vue d'ensemble combinée clavier + souris")

    _build_keyboard_subparser(subparsers, common_parser)
    _build_mouse_subparser(subparsers, common_parser)

    subparsers.add_parser("daemon", parents=[common_parser], help="Lancer le démon de bascule automatique (clavier + souris)")

    p_tray = subparsers.add_parser("tray", parents=[common_parser], help="Gestion de l'indicateur systray unifié")
    p_tray.add_argument("tray_action", nargs="?", choices=["start", "stop", "status"], default="status")
    p_tray.add_argument("--foreground", action="store_true", help="Lancer le tray au premier plan (utilisé par autostart)")

    p_auto = subparsers.add_parser("autostart", parents=[common_parser], help="Gestion du lancement automatique au démarrage")
    p_auto.add_argument("autostart_action", nargs="?", choices=["enable", "disable", "status"], default="status")

    args = parser.parse_args()

    if not args.command:
        args.command = "status"

    if args.command == "status":
        cmd_status(args)
        return

    if args.command == "keyboard":
        driver = SkillkorpK20Driver()
        dispatch = {
            "status": kb_cmd_status,
            "battery": kb_cmd_battery,
            "rgb": kb_cmd_rgb,
            "side-rgb": kb_cmd_side_rgb,
            "polling-rate": kb_cmd_polling_rate,
            "debounce": kb_cmd_debounce,
            "sleep": kb_cmd_sleep,
            "win-lock": kb_cmd_win_lock,
            "wasd-swap": kb_cmd_wasd_swap,
            "os-mode": kb_cmd_os_mode,
            "gaming-mode": kb_cmd_gaming_mode,
            "remap": kb_cmd_remap,
            "reset": kb_cmd_reset,
            "profile": kb_cmd_profile,
        }
        fn = dispatch.get(args.kb_command, kb_cmd_status)
        fn(driver, args)
        return

    if args.command == "mouse":
        driver = SkillkorpM20Driver()
        if not driver.is_connected() and args.mouse_command not in ("status", "profile", None):
            print("\033[93mAttention: La souris SkillKorp M20 ne semble pas connectée sur USB.\033[0m")
        dispatch = {
            "status": mouse_cmd_status,
            "dpi": mouse_cmd_dpi,
            "rate": mouse_cmd_rate,
            "sensor": mouse_cmd_sensor,
            "power": mouse_cmd_power,
            "button": mouse_cmd_button,
            "restore-buttons": mouse_cmd_restore_buttons,
            "profile": mouse_cmd_profile,
            "monitor": mouse_cmd_monitor,
            "apply": mouse_cmd_apply,
        }
        fn = dispatch.get(args.mouse_command, mouse_cmd_status)
        fn(driver, args)
        return

    if args.command == "daemon":
        cmd_daemon(args)
        return

    if args.command == "tray":
        cmd_tray(args)
        return

    if args.command == "autostart":
        cmd_autostart(args)
        return


if __name__ == "__main__":
    main()
