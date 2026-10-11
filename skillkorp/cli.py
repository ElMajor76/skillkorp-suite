#!/usr/bin/env python3
"""
skillkorpctl - SkillKorp Suite Command Line Controller

    skillkorpctl status                        # combined overview of both devices
    skillkorpctl keyboard <subcommand> ...     # K20 Ultimate keyboard
    skillkorpctl mouse <subcommand> ...        # M20 Ultimate mouse
    skillkorpctl daemon                        # auto-switch profiles for both devices
    skillkorpctl doctor                        # installation / permission diagnostics
    skillkorpctl tray {start,stop,status}
    skillkorpctl autostart {enable,disable,status}

Exit codes: 0 success, 1 invalid usage/value, 2 device missing or not
accessible, 3 the device refused a command, 130 interrupted.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from typing import Any, Optional

from skillkorp import __version__
from skillkorp.devices.hidraw import (
    DeviceBusyError,
    DeviceError,
    DeviceNotFoundError,
    DevicePermissionError,
)
from skillkorp.devices.keyboard import (
    SkillkorpK20Driver,
    LIGHT_MODES as KB_LIGHT_MODES,
    SIDE_LIGHT_MODES,
    REMAP_ACTIONS,
    KEY_LAYOUT,
    KEY_MATRIX_INDEX,
    WAVE_DIRECTIONS,
    DEBOUNCE_RANGE as KB_DEBOUNCE_RANGE,
)
from skillkorp.devices import mouse as mouse_mod
from skillkorp.devices.mouse import SkillkorpM20Driver, BUTTON_ACTIONS
from skillkorp.profiles.manager import (
    AutoSwitcher,
    ProfileManager,
    detect_active_window_class,
    is_autostart_enabled,
    set_autostart,
)
from skillkorp.profiles.migration import migrate_legacy_configs

EXIT_USAGE = 1
EXIT_DEVICE = 2
EXIT_REFUSED = 3

GREEN, RED, YELLOW, MAGENTA, CYAN, GREY, BOLD, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[95m", "\033[96m", "\033[90m", "\033[1m", "\033[0m")
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    GREEN = RED = YELLOW = MAGENTA = CYAN = GREY = BOLD = RESET = ""


class CommandFailed(Exception):
    def __init__(self, message: str, code: int = EXIT_REFUSED):
        super().__init__(message)
        self.code = code


def ok(msg: str):
    print(f"{GREEN}✓ {msg}{RESET}")


def warn(msg: str):
    print(f"{YELLOW}⚠ {msg}{RESET}", file=sys.stderr)


def require(success: bool, what: str, driver=None):
    if not success:
        detail = getattr(driver, "last_error", "") if driver is not None else ""
        raise CommandFailed(f"Échec : {what}." + (f" ({detail})" if detail else ""))


def require_connected(driver):
    if not driver.is_connected():
        raise DeviceNotFoundError(f"{driver.display_name} non détecté(e). Vérifiez le récepteur ou le câble USB.")


def print_json(data: Any):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def parse_on_off(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    return value.lower() in ("on", "1", "true", "oui", "yes")


def _prof_label(pm: ProfileManager):
    pid = pm.get_active_profile_id()
    prof = pm.get_profile(pid)
    return pid, (prof.get("name", pid) if prof else pid)


# =============================================================================
# Combined status
# =============================================================================

def cmd_status(args):
    kb = SkillkorpK20Driver()
    mouse = SkillkorpM20Driver()
    kb_pid, kb_name = _prof_label(ProfileManager("keyboard"))
    m_pid, m_name = _prof_label(ProfileManager("mouse"))

    kb_connected = kb.is_connected()
    kb_bat = kb.get_battery() if kb_connected else None
    m_connected = mouse.is_connected()
    m_status = mouse.query_status(poll_hardware=True) if m_connected else None

    if args.json:
        print_json({
            "keyboard": {
                "connected": kb_connected,
                "wireless": kb.is_wireless() if kb_connected else False,
                "battery": kb_bat,
                "active_profile": {"id": kb_pid, "name": kb_name},
                "polling_rate_hz": kb.config.get("polling_rate", 1000),
            },
            "mouse": {
                "connected": m_connected,
                "wireless": m_status["wireless"] if m_status else False,
                "battery": mouse.get_battery() if m_connected else None,
                "active_profile": {"id": m_pid, "name": m_name},
                "polling_rate_hz": mouse.config.get("polling_rate", 1000),
                "active_dpi": m_status["active_dpi"] if m_status else None,
            },
        })
        return

    line = "=" * 60
    print(line)
    print(f"{'SKILLKORP SUITE ' + __version__:^60}")
    print(line)
    print("⌨  Clavier K20 Ultimate")
    if kb_connected:
        mode_str = "Sans-fil 2.4GHz" if kb.is_wireless() else "Filaire USB"
        print(f"   Statut       : {GREEN}Connecté{RESET} ({mode_str})")
        print(f"   Batterie     : {kb_bat['status_str']}")
    else:
        print(f"   Statut       : {RED}Déconnecté{RESET}")
    print(f"   Profil actif : {MAGENTA}{kb_name}{RESET}")
    print(f"   Taux de poll : {kb.config.get('polling_rate', 1000)} Hz")
    print("-" * 60)
    print("🖱  Souris M20 Ultimate")
    if m_connected:
        mode_str = "Sans-fil 2.4GHz" if m_status["wireless"] else "Filaire USB-C"
        print(f"   Statut       : {GREEN}Connectée{RESET} ({mode_str})")
        print(f"   Batterie     : {mouse.get_battery()['status_str']}")
        print(f"   DPI actif    : {m_status['active_dpi']} (Étape {m_status['active_stage']}/{len(m_status['stages'])})")
    else:
        print(f"   Statut       : {RED}Déconnectée{RESET}")
    print(f"   Profil actif : {MAGENTA}{m_name}{RESET}")
    print(f"   Taux de poll : {mouse.config.get('polling_rate', 1000)} Hz")
    print(line)


# =============================================================================
# Keyboard
# =============================================================================

def kb_cmd_status(driver: SkillkorpK20Driver, args):
    driver.reload_config_if_changed()
    connected = driver.is_connected()
    is_wl = driver.is_wireless() if connected else False
    bat = driver.get_battery() if connected else None
    prof_id, prof_name = _prof_label(ProfileManager("keyboard"))
    cfg = driver.config
    rgb, srgb, opts, sleep_cfg = cfg["rgb"], cfg["side_rgb"], cfg["options"], cfg["sleep"]

    if args.json:
        print_json({
            "connected": connected, "wireless": is_wl, "device_node": driver.dev_path,
            "battery": bat, "active_profile": {"id": prof_id, "name": prof_name},
            "polling_rate_hz": cfg["polling_rate"], "debounce_ms": cfg["debounce_ms"],
            "sleep": sleep_cfg, "options": opts, "rgb": rgb, "side_rgb": srgb,
            "key_remaps": cfg.get("key_remaps", {}), "fn_remaps": cfg.get("fn_remaps", {}),
        })
        return

    print("========================================")
    print("       SKILLKORP K20 ULTIMATE           ")
    print("========================================")
    if connected:
        mode_str = "Sans-fil 2.4GHz" if is_wl else "Filaire USB"
        print(f"Statut        : {GREEN}Connecté{RESET} ({mode_str}, {driver.dev_path})")
        print(f"Batterie      : {bat['status_str']}")
    else:
        print(f"Statut        : {RED}Déconnecté{RESET}")
    print(f"Profil Actif  : {MAGENTA}{prof_name}{RESET} ({prof_id})")
    print("(valeurs ci-dessous : derniers réglages envoyés par SkillKorp Suite)")
    print(f"Taux de poll  : {CYAN}{cfg['polling_rate']} Hz{RESET}")
    print(f"Anti-rebond   : {cfg['debounce_ms']} ms")
    light, deep = sleep_cfg.get("light_sleep_sec", 0), sleep_cfg.get("deep_sleep_sec", 0)
    print(f"Veille légère : {light} s" if light else "Veille légère : désactivée")
    print(f"Veille prof.  : {deep} s ({deep // 60} min)" if deep else "Veille prof.  : désactivée")
    print(f"Touche Windows: {RED + 'Verrouillée' if opts.get('win_lock') else GREEN + 'Déverrouillée'}{RESET}")
    print(f"Touches ZQSD  : {YELLOW + 'Inversées avec flèches' + RESET if opts.get('wasd_swap') else 'Normal'}")
    print(f"Mode Système  : {opts.get('os_mode', 'win').upper()}")
    print(f"Mode Gaming   : {GREEN + 'Activé' + RESET if opts.get('gaming_mode') else 'Désactivé'}")
    mode_label = {k: label for k, label, _ in KB_LIGHT_MODES}.get(rgb.get("mode"), rgb.get("mode"))
    colour = "multicolore" if rgb.get("colorful") else rgb.get("color")
    print(f"Éclairage RGB : {YELLOW}{mode_label}{RESET} (vitesse {rgb.get('speed')}/5, "
          f"luminosité {rgb.get('brightness')}/4, couleur {colour})")
    smode_label = {k: label for k, label, _ in SIDE_LIGHT_MODES}.get(srgb.get("mode"), srgb.get("mode"))
    print(f"Bandes LED    : {smode_label} (luminosité {srgb.get('brightness')}/4)")
    remaps = cfg.get("key_remaps", {})
    fn_remaps = cfg.get("fn_remaps", {})
    if remaps or fn_remaps:
        print(f"Remappages    : {len(remaps)} touche(s), {len(fn_remaps)} touche(s) Fn")
    print("========================================")


def kb_cmd_info(driver: SkillkorpK20Driver, args):
    require_connected(driver)
    fw = driver.get_firmware_version()
    info = {
        "device_node": driver.dev_path,
        "wireless": driver.is_wireless(),
        "firmware_version": fw,
        "firmware_version_hex": f"0x{fw:04X}" if fw is not None else None,
    }
    if args.json:
        print_json(info)
        return
    print(f"Nœud HID     : {info['device_node']}")
    print(f"Connexion    : {'Sans-fil 2.4GHz' if info['wireless'] else 'Filaire USB'}")
    if info["firmware_version_hex"]:
        print(f"Firmware     : {info['firmware_version_hex']}")
    elif info["wireless"]:
        print("Firmware     : non communiqué (clavier en veille ? appuyez sur une touche)")
    else:
        print("Firmware     : non communiqué")


def kb_cmd_sync(driver: SkillkorpK20Driver, args):
    require_connected(driver)
    pending = driver.pending_settings()
    if not pending:
        print("Aucun réglage en attente.")
        return
    require(driver.sync_pending(), "application des réglages en attente", driver)
    ok(f"{len(pending)} réglage(s) en attente appliqué(s).")


def kb_cmd_battery(driver: SkillkorpK20Driver, args):
    bat = driver.get_battery()
    if args.json:
        print_json(bat)
        return
    if not bat.get("connected"):
        raise DeviceNotFoundError("Clavier non connecté.")
    print(f"Batterie : {bat['status_str']}")


def kb_cmd_rgb(driver: SkillkorpK20Driver, args):
    direction = None
    if args.direction is not None:
        direction = int(args.direction) if args.direction.isdigit() else WAVE_DIRECTIONS[args.direction]
    require(driver.set_rgb(mode=args.mode, speed=args.speed, brightness=args.brightness,
                           direction=direction, color=args.color, colorful=args.colorful),
            "configuration de l'éclairage", driver)
    r = driver.config["rgb"]
    ok(f"Éclairage RGB : mode={r['mode']}, vitesse={r['speed']}, luminosité={r['brightness']}, "
       f"{'multicolore' if r['colorful'] else 'couleur=' + r['color']}")


def kb_cmd_side_rgb(driver: SkillkorpK20Driver, args):
    require(driver.set_side_rgb(mode=args.mode, speed=args.speed, brightness=args.brightness,
                                color=args.color, colorful=args.colorful),
            "configuration des bandes latérales", driver)
    r = driver.config["side_rgb"]
    ok(f"Éclairage latéral : mode={r['mode']}, vitesse={r['speed']}, luminosité={r['brightness']}, "
       f"{'multicolore' if r['colorful'] else 'couleur=' + r['color']}")


def kb_cmd_polling_rate(driver: SkillkorpK20Driver, args):
    require(driver.set_polling_rate(args.rate), "réglage du taux de rafraîchissement", driver)
    ok(f"Taux de rafraîchissement configuré à {args.rate} Hz.")


def kb_cmd_debounce(driver: SkillkorpK20Driver, args):
    require(driver.set_debounce(args.ms), "réglage de l'anti-rebond", driver)
    ok(f"Temps d'anti-rebond configuré à {driver.config['debounce_ms']} ms.")


def kb_cmd_sleep(driver: SkillkorpK20Driver, args):
    cfg = driver.config["sleep"]
    light = args.light if args.light is not None else cfg.get("light_sleep_sec", 120)
    deep = args.deep if args.deep is not None else cfg.get("deep_sleep_sec", 1680)
    require(driver.set_sleep_time(light, deep), "réglage des délais de veille", driver)
    s = driver.config["sleep"]
    if (s["light_sleep_sec"], s["deep_sleep_sec"]) != (light, deep):
        warn("Valeurs ajustées aux limites du K20 (veille légère 60–3600 s, profonde 600–3600 s, 0 = jamais).")
    ok(f"Délais de mise en veille : légère={s['light_sleep_sec']} s, profonde={s['deep_sleep_sec']} s")


def _toggle(driver, key: str, state: str) -> bool:
    cur = driver.config["options"].get(key, False)
    return (not cur) if state == "toggle" else (state == "on")


def kb_cmd_win_lock(driver, args):
    target = _toggle(driver, "win_lock", args.state)
    require(driver.set_keyboard_options(win_lock=target), "réglage du verrouillage Windows", driver)
    ok(f"Touche Windows {'verrouillée' if target else 'déverrouillée'}.")


def kb_cmd_wasd_swap(driver, args):
    target = _toggle(driver, "wasd_swap", args.state)
    require(driver.set_keyboard_options(wasd_swap=target), "réglage de l'inversion ZQSD", driver)
    ok(f"Touches ZQSD {'inversées avec les flèches' if target else 'en mode normal'}.")


def kb_cmd_os_mode(driver, args):
    require(driver.set_keyboard_options(os_mode=args.mode), "réglage du mode système", driver)
    ok(f"Mode système configuré : {args.mode.upper()}.")


def kb_cmd_gaming_mode(driver, args):
    target = _toggle(driver, "gaming_mode", args.state)
    require(driver.set_keyboard_options(gaming_mode=target), "réglage du mode Gaming", driver)
    ok(f"Mode Gaming {'activé' if target else 'désactivé'}.")


def kb_cmd_remap(driver: SkillkorpK20Driver, args):
    if args.list or not args.key:
        print("Touches remappables :")
        print("  " + ", ".join(k["name"] for k in KEY_LAYOUT if k["name"] in KEY_MATRIX_INDEX))
        print("\nActions disponibles :")
        for key, (label, _) in REMAP_ACTIONS.items():
            print(f"  {key:<20} : {label}")
        return
    if not args.action:
        raise CommandFailed("Précisez l'action (ou --list).", EXIT_USAGE)
    if args.fn:
        require(driver.remap_fn_key(args.key, args.action), "remappage de la touche", driver)
    else:
        require(driver.remap_key(args.key, args.action), "remappage de la touche", driver)
    ok(f"Touche {'Fn + ' if args.fn else ''}'{args.key}' → '{REMAP_ACTIONS[args.action][0]}'.")


def kb_cmd_reset(driver: SkillkorpK20Driver, args):
    if not args.force:
        if not sys.stdin.isatty():
            raise CommandFailed("Confirmation requise : relancez avec --force.", EXIT_USAGE)
        confirm = input("Réinitialiser le clavier aux réglages d'usine ? (o/N) ")
        if confirm.lower() not in ("o", "oui", "y", "yes"):
            print("Opération annulée.")
            return
    require(driver.factory_reset(), "réinitialisation d'usine", driver)
    ok("Clavier réinitialisé aux réglages d'usine.")


def kb_cmd_apply(driver: SkillkorpK20Driver, args):
    pm = ProfileManager("keyboard")
    prof = pm.get_profile(pm.get_active_profile_id())
    require_connected(driver)
    print("Application du profil actif au clavier...")
    require(driver.apply_profile(prof), "application du profil", driver)
    ok(f"Profil '{prof.get('name')}' appliqué.")


# =============================================================================
# Profiles (shared by both devices)
# =============================================================================

_PROFILE_ALIASES = {"switch": "set", "create": "new", "show": "get", "rm": "delete"}


def cmd_profile(device: str, driver, args):
    pm = ProfileManager(device)
    action = _PROFILE_ALIASES.get(args.profile_action, args.profile_action) or "list"
    pid = args.profile_id

    def need_id():
        if not pid:
            raise CommandFailed("Précisez l'identifiant du profil.", EXIT_USAGE)

    if action == "list":
        profiles = pm.list_profiles()
        if args.json:
            print_json(profiles)
            return
        print("Profils disponibles :")
        for p in profiles:
            star = f"{GREEN}* {RESET}" if p.get("active") else "  "
            print(f"{star}{BOLD}{p['id']:<16}{RESET} - {p.get('name', p['id'])}")
            if p.get("description"):
                print(f"     {p['description']}")
            if p.get("auto_switch_apps"):
                print(f"     Applications: {', '.join(p['auto_switch_apps'])}")
    elif action == "get":
        p = pm.get_profile(pid or pm.get_active_profile_id())
        if not p:
            raise CommandFailed(f"Profil '{pid}' introuvable.", EXIT_USAGE)
        print_json(p)
    elif action == "set":
        need_id()
        prof = pm.switch_profile(pid, driver=driver if driver.is_connected() else None)
        if pm.last_apply_ok is None:
            warn("Périphérique absent : le profil sera appliqué à la prochaine connexion "
                 f"(skillkorpctl {device} apply).")
        else:
            require(pm.last_apply_ok, "application du profil", driver)
        ok(f"Profil actif : {prof.get('name', pid)} ({pid})")
    elif action == "new":
        need_id()
        prof = pm.create_profile(pid, args.name or pid, args.copy_from)
        ok(f"Profil '{prof['id']}' créé.")
    elif action == "delete":
        need_id()
        if not pm.delete_profile(pid, driver=driver if driver.is_connected() else None):
            raise CommandFailed(f"Profil '{pid}' introuvable.", EXIT_USAGE)
        ok(f"Profil '{pid}' supprimé.")
    elif action == "export":
        need_id()
        target = args.file or f"{pid}.json"
        pm.export_profile(pid, target)
        ok(f"Profil '{pid}' exporté vers {target}.")
    elif action == "import":
        if not args.file:
            raise CommandFailed("Précisez le fichier avec --file.", EXIT_USAGE)
        new_id = pm.import_profile(args.file, pid)
        ok(f"Profil importé sous l'identifiant '{new_id}'.")
    elif action == "apps":
        need_id()
        apps = [a for a in (args.apps or "").split(",") if a.strip()]
        pm.set_auto_switch_apps(pid, apps)
        ok(f"Applications associées à '{pid}' : {', '.join(apps) or '(aucune)'}")
    elif action == "auto":
        if args.state is None:
            print(f"Bascule automatique : {'activée' if pm.is_auto_switch_enabled() else 'désactivée'}")
        else:
            pm.set_auto_switch_enabled(args.state == "on")
            ok(f"Bascule automatique {'activée' if args.state == 'on' else 'désactivée'}.")


# =============================================================================
# Mouse
# =============================================================================

def mouse_cmd_battery(driver: SkillkorpM20Driver, args):
    bat = driver.get_battery(poll_hardware=True)
    if args.json:
        print_json(bat)
        return
    if not bat.get("connected"):
        raise DeviceNotFoundError("Souris non connectée.")
    print(f"Batterie : {bat['status_str']}")


def mouse_cmd_status(driver: SkillkorpM20Driver, args):
    status = driver.query_status(poll_hardware=True)
    pid, pname = _prof_label(ProfileManager("mouse"))
    if args.json:
        status["active_profile"] = {"id": pid, "name": pname}
        status["battery_info"] = driver.get_battery() if status["connected"] else None
        print_json(status)
        return
    print("========================================")
    print("       SKILLKORP M20 ULTIMATE           ")
    print("========================================")
    if status["connected"]:
        mode = "Sans-fil 2.4GHz" if status["wireless"] else "Filaire USB-C"
        print(f"Statut        : {GREEN}Connectée{RESET} ({mode}, {status['device']})")
        print(f"Batterie      : {driver.get_battery()['status_str']}")
    else:
        print(f"Statut        : {RED}Déconnectée{RESET}")
    print(f"Profil Actif  : {MAGENTA}{pname}{RESET} ({pid})")
    print(f"Taux de poll  : {status['polling_rate_hz']} Hz")
    print(f"DPI Actif     : {CYAN}{status['active_dpi']} DPI{RESET} "
          f"(Étape {status['active_stage']}/{len(status['stages'])})")
    stages_str = ", ".join(f"[{s}]" if i + 1 == status["active_stage"] else str(s)
                           for i, s in enumerate(status["stages"]))
    print(f"Étapes DPI    : {stages_str}")
    print(f"Distance LOD  : {status['lod_mm']} mm")
    print(f"Anti-rebond   : {status['debounce_ms']} ms")
    print(f"Motion Sync   : {'Activé' if status['motion_sync'] else 'Désactivé'}")
    print(f"Angle Snap    : {'Activé' if status['angle_snapping'] else 'Désactivé'}")
    print(f"Ripple Control: {'Activé' if status['ripple_control'] else 'Désactivé'}")
    print(f"Veille légère : après {status['led_off_minutes']} min")
    print(f"Veille prof.  : après {status['sleep_timer_minutes']} min")
    print("========================================")


def mouse_cmd_dpi(driver: SkillkorpM20Driver, args):
    stages = None
    if args.stages:
        try:
            stages = [int(s.strip()) for s in args.stages.split(",") if s.strip()]
        except ValueError:
            raise CommandFailed("--stages attend des entiers séparés par des virgules (ex: 400,800,1600).",
                                EXIT_USAGE) from None
    if stages is None and args.active is None:
        st = driver.query_status()
        if args.json:
            print_json({"active_dpi": st["active_dpi"], "active_stage": st["active_stage"], "stages": st["stages"]})
        else:
            print(f"DPI Actif: {st['active_dpi']} (Étape {st['active_stage']})")
            print(f"Étapes   : {st['stages']}")
        return
    require(driver.set_dpi_and_sensor(stages=stages, active_stage=args.active), "réglage DPI", driver)
    st = driver.query_status()
    ok(f"DPI mis à jour : {st['active_dpi']} DPI (Étape {st['active_stage']}), étapes {st['stages']}")


def mouse_cmd_rate(driver: SkillkorpM20Driver, args):
    if args.set:
        require(driver.set_polling_rate(args.set), "réglage du taux de scrutation", driver)
        ok(f"Taux de scrutation configuré à {args.set} Hz.")
    else:
        print(f"Taux de scrutation actuel : {driver.config['polling_rate']} Hz")


def mouse_cmd_sensor(driver: SkillkorpM20Driver, args):
    kwargs = {}
    if args.lod is not None:
        kwargs["lod"] = args.lod
    if args.debounce is not None:
        kwargs["debounce"] = args.debounce
    for name in ("motion_sync", "angle_snap", "ripple"):
        value = parse_on_off(getattr(args, name))
        if value is not None:
            kwargs[name] = value
    if not kwargs:
        st = driver.query_status()
        print(f"Distance de levée (LOD) : {st['lod_mm']} mm")
        print(f"Anti-rebond (debounce)  : {st['debounce_ms']} ms")
        print(f"Motion Sync             : {st['motion_sync']}")
        print(f"Angle Snapping          : {st['angle_snapping']}")
        print(f"Ripple Control          : {st['ripple_control']}")
        return
    require(driver.set_dpi_and_sensor(**kwargs), "réglage du capteur", driver)
    st = driver.query_status()
    ok(f"Capteur : LOD {st['lod_mm']} mm | Debounce {st['debounce_ms']} ms | MotionSync {st['motion_sync']} | "
       f"AngleSnap {st['angle_snapping']} | Ripple {st['ripple_control']}")


def mouse_cmd_light(driver: SkillkorpM20Driver, args):
    if args.list_modes:
        for key, label, _ in mouse_mod.LIGHT_MODES:
            print(f"  {key:<18} : {label}")
        return
    color = None
    if args.color:
        try:
            color = mouse_mod._rgb(args.color)
        except ValueError as e:
            raise CommandFailed(str(e), EXIT_USAGE) from None
    mode = args.mode or driver.config.get("light_mode", "static")
    require(driver.set_rgb_lighting(mode, brightness=args.brightness, speed=args.speed, color=color),
            "réglage de l'éclairage", driver)
    c = driver.config
    ok(f"Éclairage : {c['light_mode']}, luminosité {c['brightness']}/8, vitesse {c['speed']}/8, "
       f"couleur #{''.join(f'{v:02X}' for v in c['light_color'])}")


def mouse_cmd_power(driver: SkillkorpM20Driver, args):
    if args.wake_mode is not None:
        warn("Le mode de réveil n'est pas réglable sur la M20 (option absente du firmware) : ignoré.")
    if args.sleep_timer is None and args.led_off is None:
        st = driver.query_status()
        print("Paramètres d'alimentation actuels :")
        print(f"  Veille légère   : après {st['led_off_minutes']} min")
        print(f"  Veille profonde : après {st['sleep_timer_minutes']} min")
        return
    require(driver.set_power_settings(sleep_timer_minutes=args.sleep_timer, led_off_minutes=args.led_off),
            "réglage de l'alimentation", driver)
    ok(f"Alimentation : veille légère après {driver.config['led_off_minutes']} min, "
       f"veille profonde après {driver.config['sleep_timer_minutes']} min.")


def mouse_cmd_button(driver: SkillkorpM20Driver, args):
    if args.list_actions:
        print("Actions configurables disponibles :")
        for key, info in BUTTON_ACTIONS.items():
            print(f"  {key:<20} : {info[0]}")
        return
    if args.btn and args.action:
        if args.action not in BUTTON_ACTIONS:
            raise CommandFailed(f"Action '{args.action}' inconnue. Voir 'skillkorpctl mouse button --list-actions'.",
                                EXIT_USAGE)
        require(driver.set_buttons({args.btn: args.action}), "réassignation du bouton", driver)
        ok(f"Bouton {args.btn} réassigné à '{BUTTON_ACTIONS[args.action][0]}'")
        return
    if args.btn or args.action:
        raise CommandFailed("--btn et --action doivent être utilisés ensemble.", EXIT_USAGE)
    buttons = driver.config.get("buttons", {})
    print("Affectation actuelle des boutons :")
    for b in range(1, 7):
        act = buttons.get(str(b), "inconnu")
        print(f"  Bouton {b}: {BUTTON_ACTIONS.get(act, (act,))[0]} ({act})")


def mouse_cmd_restore_buttons(driver: SkillkorpM20Driver, args):
    require(driver.restore_factory_buttons(), "restauration des boutons", driver)
    ok("Mappage d'usine des boutons restauré.")


def mouse_cmd_monitor(driver: SkillkorpM20Driver, args):
    require_connected(driver)
    print("Écoute des événements de la souris (bouton DPI, batterie). Ctrl+C pour quitter.", flush=True)
    last_query = 0.0
    while True:
        try:
            if time.monotonic() - last_query > 60:
                last_query = time.monotonic()
                events = driver.poll_status()
            else:
                events = driver.listen(args.interval)
        except DeviceNotFoundError:
            print("Souris déconnectée, attente de reconnexion...", flush=True)
            while not driver.is_connected():
                time.sleep(1.0)
            print("Souris reconnectée.", flush=True)
            continue
        for ev in events:
            if ev["type"] == "dpi_stage":
                stages = driver.config.get("dpi_stages", [])
                stage = ev["stage"]
                dpi = stages[stage - 1] if 1 <= stage <= len(stages) else "?"
                print(f"⚡ Étape DPI {stage}/{len(stages)} : {CYAN}{dpi} DPI{RESET}", flush=True)
            elif ev["type"] == "battery":
                ch = " (en charge ⚡)" if ev["charging"] else ""
                print(f"🔋 Batterie : {ev['percentage']}%{ch}", flush=True)


def mouse_cmd_apply(driver: SkillkorpM20Driver, args):
    require_connected(driver)
    print("Application de la configuration enregistrée à la souris...")
    require(driver.apply_all(), "application de la configuration", driver)
    ok("Configuration appliquée.")


# =============================================================================
# Tray, autostart, daemon, doctor
# =============================================================================

def _runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or os.path.expanduser("~/.config/skillkorp")


def tray_pid_file() -> str:
    return os.path.join(_runtime_dir(), "skillkorp-tray.pid")


def _pid_alive_tray(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().replace(b"\0", b" ")
        return b"skillkorp" in cmdline and b"tray" in cmdline
    except OSError:
        return True


def get_tray_pids() -> list:
    """Return running tray PIDs (PID file first, pgrep fallback)."""
    pids = []
    try:
        with open(tray_pid_file()) as f:
            pid = int(f.read().strip())
        if pid != os.getpid() and _pid_alive_tray(pid):
            pids.append(pid)
    except (OSError, ValueError):
        pass
    if not pids:
        try:
            out = subprocess.check_output(["pgrep", "-u", str(os.getuid()), "-f", r"skillkorp[.-]tray"], text=True)
            pids = [int(p) for p in out.split() if p.isdigit() and int(p) != os.getpid()]
        except (OSError, subprocess.CalledProcessError):
            pass
    return pids


def cmd_tray(args):
    action = args.tray_action or "status"
    pids = get_tray_pids()
    if action == "start":
        if pids:
            print(f"L'indicateur est déjà en cours d'exécution (PID: {pids[0]}).")
            return
        if args.foreground:
            from skillkorp.tray import main as tray_main
            sys.exit(tray_main())
        import skillkorp as pkg
        pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(pkg.__file__)))
        code = f"import sys; sys.path.insert(0, {pkg_parent!r}); from skillkorp.tray import main; sys.exit(main())"
        subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(20):
            time.sleep(0.25)
            new_pids = get_tray_pids()
            if new_pids:
                ok(f"Indicateur démarré en arrière-plan (PID: {new_pids[0]}).")
                return
        raise CommandFailed("Impossible de démarrer l'indicateur (lancez 'skillkorpctl tray start --foreground' "
                            "pour voir l'erreur).")
    elif action == "stop":
        if not pids:
            print("L'indicateur n'est pas en cours d'exécution.")
            return
        for p in pids:
            try:
                os.kill(p, signal.SIGTERM)
            except ProcessLookupError:
                pass
        ok(f"Indicateur arrêté (PIDs: {pids}).")
    else:
        print(f"● Indicateur actif (PID: {pids[0]})." if pids else "○ Indicateur inactif.")


def cmd_autostart(args):
    action = args.autostart_action or "status"
    if action == "enable":
        set_autostart(True)
        ok("Lancement automatique de l'indicateur activé.")
    elif action == "disable":
        set_autostart(False)
        ok("Lancement automatique désactivé.")
    else:
        print(f"Lancement automatique au démarrage : {GREEN + 'Activé' if is_autostart_enabled() else GREY + 'Désactivé'}{RESET}")


def cmd_daemon(args):
    """Auto-switch keyboard and mouse profiles from the focused application."""
    kb_pm, mouse_pm = ProfileManager("keyboard"), ProfileManager("mouse")
    kb, mouse = SkillkorpK20Driver(), SkillkorpM20Driver()
    running = True

    def stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    class _OnlyIfConnected:
        """Pass the driver only when the device is present (profile is still recorded)."""
        def __init__(self, drv):
            self.drv = drv

        def apply_profile(self, prof):
            if not self.drv.is_connected():
                return True
            return self.drv.apply_profile(prof)

    def report(dev, pid, prof, success, cls):
        label = "Clavier" if dev == "keyboard" else "Souris"
        status = "" if success else f" {RED}(échec partiel : {getattr(kb if dev == 'keyboard' else mouse, 'last_error', '')}){RESET}"
        print(f"[{time.strftime('%H:%M:%S')}] [{label}] {cls} → profil '{pid}'{status}", flush=True)

    switcher = AutoSwitcher([(kb_pm, _OnlyIfConnected(kb)), (mouse_pm, _OnlyIfConnected(mouse))], on_switch=report)
    print("Démon de bascule automatique SkillKorp Suite démarré (Ctrl+C pour arrêter).", flush=True)
    if detect_active_window_class() is None:
        warn("Fenêtre active indétectable avec les outils disponibles (xdotool/xprop, hyprctl, swaymsg, kdotool) : "
             "la bascule automatique ne fonctionnera pas dans cette session.")
    while running:
        try:
            switcher.tick()
        except DeviceError as e:
            warn(str(e))
        except Exception as e:  # never die on an unexpected error in a long-running daemon
            warn(f"Erreur inattendue : {e}")
        time.sleep(max(0.2, args.interval))
    print("Arrêt du démon.")


def cmd_doctor(args):
    from skillkorp.devices import hidraw
    problems = 0
    print(f"SkillKorp Suite {__version__} — diagnostic\n")
    rule_dirs = ("/etc/udev/rules.d", "/usr/lib/udev/rules.d", "/lib/udev/rules.d")
    rules = [os.path.join(d, n) for d in rule_dirs for n in ("70-skillkorp-suite.rules",) if os.path.exists(os.path.join(d, n))]
    legacy = [os.path.join(d, "99-skillkorp-suite.rules") for d in rule_dirs
              if os.path.exists(os.path.join(d, "99-skillkorp-suite.rules"))]
    if rules:
        ok(f"Règle udev installée : {rules[0]}")
    elif legacy:
        problems += 1
        print(f"{YELLOW}⚠ Ancienne règle {legacy[0]} : évaluée trop tard pour « uaccess ». "
              f"Réinstallez le paquet 1.1 (règle 70-skillkorp-suite.rules).{RESET}")
    else:
        problems += 1
        print(f"{RED}✗ Règle udev absente : accès aux périphériques réservé à root.{RESET}")
    for drv_cls in (SkillkorpK20Driver, SkillkorpM20Driver):
        nodes = drv_cls.find_nodes()
        if not nodes:
            print(f"{GREY}○ {drv_cls.display_name} : non détecté(e){RESET}")
            continue
        for node in nodes:
            kind = "sans-fil" if node.product_id in (0x4011, 0xFA60) else "filaire"
            try:
                with hidraw.HidrawDevice(node, lock_timeout=1.0).open():
                    pass
                ok(f"{drv_cls.display_name} ({kind}) : {node.devnode} accessible")
            except DevicePermissionError:
                problems += 1
                print(f"{RED}✗ {drv_cls.display_name} ({kind}) : {node.devnode} non accessible (permissions){RESET}")
            except DeviceBusyError:
                print(f"{YELLOW}⚠ {drv_cls.display_name} : {node.devnode} occupé par un autre processus SkillKorp{RESET}")
            except DeviceError as e:
                problems += 1
                print(f"{RED}✗ {drv_cls.display_name} : {e}{RESET}")
    # Only the vendor interface (2) may be open: the others carry keystrokes.
    exposed = []
    for node in hidraw.list_hidraw_nodes():
        if (node.vendor_id, node.product_id) in ((0x3151, 0x4011), (0x3151, 0x4015), (0x1D57, 0xFA60),
                                                 (0x1D57, 0xFA61)) \
                and node.interface not in (None, 2) and os.access(node.devnode, os.R_OK):
            exposed.append(f"{node.devnode} (interface {node.interface})")
    if exposed:
        problems += 1
        print(f"{YELLOW}⚠ Interfaces de saisie lisibles par la session : {', '.join(exposed)}. "
              f"Débranchez puis rebranchez le récepteur (ou redémarrez) pour appliquer la règle udev.{RESET}")
    else:
        ok("Seule l'interface de configuration est accessible (frappes protégées)")
    cls = detect_active_window_class()
    if cls:
        ok(f"Détection de la fenêtre active : OK ({cls})")
    else:
        print(f"{YELLOW}⚠ Fenêtre active non détectable : installez xdotool (X11), ou utilisez Hyprland/Sway/"
              f"kdotool (KDE). La bascule automatique de profils sera inactive.{RESET}")
    import importlib.util
    if importlib.util.find_spec("gi") is not None:
        ok("PyGObject disponible (interface graphique et indicateur)")
    else:
        problems += 1
        print(f"{RED}✗ PyGObject absent : skillkorp-gui et skillkorp-tray ne pourront pas démarrer.{RESET}")
    print()
    print("Aucun problème bloquant." if not problems else f"{problems} problème(s) détecté(s).")
    return 0 if not problems else EXIT_DEVICE


# =============================================================================
# Argument parsing
# =============================================================================

def _int_range(lo: int, hi: int):
    def parse(text):
        try:
            v = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"entier attendu, reçu {text!r}")
        if not lo <= v <= hi:
            raise argparse.ArgumentTypeError(f"valeur hors limites ({lo} à {hi})")
        return v
    return parse


def _add_profile_parser(sub, common, device: str):
    p = sub.add_parser("profile", parents=[common], help="Gestion des profils",
                       description="Actions : list, get, set|switch, new|create, delete, export, import, "
                                   "apps (applications de bascule auto), auto on|off")
    p.add_argument("profile_action", nargs="?", default="list",
                   choices=["list", "get", "show", "set", "switch", "new", "create", "delete", "rm",
                            "export", "import", "apps", "auto"])
    p.add_argument("profile_id", nargs="?")
    p.add_argument("--name")
    p.add_argument("--copy-from")
    p.add_argument("--file")
    p.add_argument("--apps", help="liste d'applications séparées par des virgules (action apps)")
    p.add_argument("--state", choices=["on", "off"], help="activer/désactiver la bascule auto (action auto)")
    p.set_defaults(func=lambda drv, args: cmd_profile(device, drv, args))
    return p


def _build_keyboard_subparser(subparsers, common):
    p_kb = subparsers.add_parser("keyboard", aliases=["kb"], help="Clavier SkillKorp K20 Ultimate")
    sub = p_kb.add_subparsers(dest="kb_command")

    sub.add_parser("status", parents=[common], help="État complet du clavier").set_defaults(func=kb_cmd_status)
    sub.add_parser("info", parents=[common], help="Informations matérielles (firmware)").set_defaults(func=kb_cmd_info)
    sub.add_parser("battery", parents=[common], help="Batterie").set_defaults(func=kb_cmd_battery)
    sub.add_parser("sync", parents=[common],
                   help="Applique les réglages enregistrés pendant la veille du clavier").set_defaults(func=kb_cmd_sync)

    p = sub.add_parser("rgb", parents=[common], help="Éclairage RGB des touches")
    p.add_argument("--mode", choices=[k for k, _, _ in KB_LIGHT_MODES])
    p.add_argument("--speed", type=_int_range(1, 5))
    p.add_argument("--brightness", type=_int_range(0, 4))
    p.add_argument("--direction", choices=list(WAVE_DIRECTIONS) + ["0", "1", "2", "3"],
                   help="sens de l'onde : right/left/down/up (ou 0..3)")
    colour = p.add_mutually_exclusive_group()
    colour.add_argument("--colorful", dest="colorful", action="store_true", default=None, help="effet multicolore")
    colour.add_argument("--single-color", dest="colorful", action="store_false", help="effet dans la couleur --color")
    p.add_argument("--color", help="couleur #RRGGBB")
    p.set_defaults(func=kb_cmd_rgb)

    p = sub.add_parser("side-rgb", parents=[common], help="Bandes LED latérales")
    p.add_argument("--mode", choices=[k for k, _, _ in SIDE_LIGHT_MODES])
    p.add_argument("--speed", type=_int_range(1, 5))
    p.add_argument("--brightness", type=_int_range(0, 4))
    colour = p.add_mutually_exclusive_group()
    colour.add_argument("--colorful", dest="colorful", action="store_true", default=None)
    colour.add_argument("--single-color", dest="colorful", action="store_false")
    p.add_argument("--color")
    p.set_defaults(func=kb_cmd_side_rgb)

    p = sub.add_parser("polling-rate", parents=[common], help="Taux de rafraîchissement")
    p.add_argument("rate", type=int, choices=[125, 250, 500, 1000])
    p.set_defaults(func=kb_cmd_polling_rate)

    p = sub.add_parser("debounce", parents=[common], help="Anti-rebond (ms)")
    p.add_argument("ms", type=_int_range(*KB_DEBOUNCE_RANGE))
    p.set_defaults(func=kb_cmd_debounce)

    p = sub.add_parser("sleep", parents=[common], help="Délais de veille (secondes, 0 = jamais)")
    p.add_argument("--light", type=int)
    p.add_argument("--deep", type=int)
    p.set_defaults(func=kb_cmd_sleep)

    for name, helptext, func in (("win-lock", "Verrouiller la touche Windows", kb_cmd_win_lock),
                                 ("wasd-swap", "Inverser ZQSD et flèches", kb_cmd_wasd_swap),
                                 ("gaming-mode", "Mode Gaming", kb_cmd_gaming_mode)):
        p = sub.add_parser(name, parents=[common], help=helptext)
        p.add_argument("state", choices=["on", "off", "toggle"])
        p.set_defaults(func=func)

    p = sub.add_parser("os-mode", parents=[common], help="Mode de compatibilité système")
    p.add_argument("mode", choices=["win", "mac"])
    p.set_defaults(func=kb_cmd_os_mode)

    p = sub.add_parser("remap", parents=[common], help="Remapper une touche ('default' = action d'origine)")
    p.add_argument("key", nargs="?")
    p.add_argument("action", nargs="?", choices=list(REMAP_ACTIONS.keys()))
    p.add_argument("--fn", action="store_true", help="couche Fn")
    p.add_argument("--list", action="store_true", help="lister touches et actions")
    p.set_defaults(func=kb_cmd_remap)

    p = sub.add_parser("reset", parents=[common], help="Réinitialisation d'usine")
    p.add_argument("-f", "--force", action="store_true")
    p.set_defaults(func=kb_cmd_reset)

    sub.add_parser("apply", parents=[common], help="Ré-appliquer le profil actif").set_defaults(func=kb_cmd_apply)
    _add_profile_parser(sub, common, "keyboard")
    return p_kb


def _build_mouse_subparser(subparsers, common):
    p_mouse = subparsers.add_parser("mouse", aliases=["ms"], help="Souris SkillKorp M20 Ultimate")
    sub = p_mouse.add_subparsers(dest="mouse_command")

    sub.add_parser("status", parents=[common], help="État complet de la souris").set_defaults(func=mouse_cmd_status)
    sub.add_parser("battery", parents=[common], help="Batterie").set_defaults(func=mouse_cmd_battery)

    p = sub.add_parser("dpi", parents=[common], help="Étapes DPI")
    p.add_argument("--stages", type=str, help="ex: 400,800,1600 (1 à 8 valeurs, 50 à 26000)")
    p.add_argument("--active", type=_int_range(1, 8))
    p.set_defaults(func=mouse_cmd_dpi)

    p = sub.add_parser("rate", parents=[common], help="Taux de scrutation")
    p.add_argument("--set", type=int, choices=[125, 250, 500, 1000])
    p.set_defaults(func=mouse_cmd_rate)

    p = sub.add_parser("sensor", parents=[common], help="Capteur PixArt PAW3395 et anti-rebond")
    p.add_argument("--lod", type=int, choices=[1, 2])
    p.add_argument("--debounce", type=_int_range(*mouse_mod.DEBOUNCE_RANGE))
    p.add_argument("--motion-sync", choices=["on", "off"])
    p.add_argument("--angle-snap", choices=["on", "off"])
    p.add_argument("--ripple", choices=["on", "off"])
    p.set_defaults(func=mouse_cmd_sensor)

    # The M20 has no RGB LEDs: the command stays for completeness but is hidden.
    p = sub.add_parser("light", parents=[common])
    p.add_argument("--mode", choices=[k for k, _, _ in mouse_mod.LIGHT_MODES])
    p.add_argument("--brightness", type=_int_range(*mouse_mod.BRIGHTNESS_RANGE))
    p.add_argument("--speed", type=_int_range(*mouse_mod.SPEED_RANGE))
    p.add_argument("--color", help="#RRGGBB")
    p.add_argument("--list-modes", action="store_true")
    p.set_defaults(func=mouse_cmd_light)

    p = sub.add_parser("power", parents=[common], help="Délais de mise en veille")
    p.add_argument("--sleep-timer", type=_int_range(*mouse_mod.SLEEP_RANGE), help="minutes avant la veille profonde")
    p.add_argument("--led-off", "--light-sleep", dest="led_off", type=_int_range(*mouse_mod.LED_OFF_RANGE),
                   help="minutes avant la veille légère (niveau 1)")
    p.add_argument("--wake-mode", choices=["move", "click"], help=argparse.SUPPRESS)
    p.set_defaults(func=mouse_cmd_power)

    p = sub.add_parser("button", parents=[common], help="Configuration des boutons")
    p.add_argument("--btn", type=int, choices=range(1, 7))
    p.add_argument("--action", type=str)
    p.add_argument("--list-actions", action="store_true")
    p.set_defaults(func=mouse_cmd_button)

    sub.add_parser("restore-buttons", parents=[common], help="Boutons d'usine").set_defaults(func=mouse_cmd_restore_buttons)

    p = sub.add_parser("monitor", parents=[common], help="Événements en direct (DPI, batterie)")
    p.add_argument("--interval", type=float, default=5.0, help=argparse.SUPPRESS)
    p.set_defaults(func=mouse_cmd_monitor)

    sub.add_parser("apply", parents=[common], help="Ré-appliquer la configuration enregistrée").set_defaults(
        func=mouse_cmd_apply)
    _add_profile_parser(sub, common, "mouse")
    return p_mouse


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="Sortie JSON pour scripts")
    parser = argparse.ArgumentParser(
        prog="skillkorpctl",
        description="Configuration du clavier SkillKorp K20 et de la souris SkillKorp M20.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"skillkorpctl {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", parents=[common], help="Vue d'ensemble clavier + souris")
    _build_keyboard_subparser(subparsers, common)
    _build_mouse_subparser(subparsers, common)
    p = subparsers.add_parser("daemon", parents=[common], help="Bascule automatique des profils")
    p.add_argument("--interval", type=float, default=1.0, help="période de détection en secondes")
    subparsers.add_parser("doctor", parents=[common], help="Diagnostic de l'installation")
    p = subparsers.add_parser("tray", parents=[common], help="Indicateur de la barre système")
    p.add_argument("tray_action", nargs="?", choices=["start", "stop", "status"], default="status")
    p.add_argument("--foreground", action="store_true")
    p = subparsers.add_parser("autostart", parents=[common], help="Lancement automatique de l'indicateur")
    p.add_argument("autostart_action", nargs="?", choices=["enable", "disable", "status"], default="status")
    return parser


def safe_migrate():
    try:
        migrate_legacy_configs()
    except Exception as e:  # migration must never prevent the tool from starting
        warn(f"Migration de l'ancienne configuration impossible : {e}")


def run(args) -> int:
    command = args.command or "status"
    if command == "status":
        cmd_status(args)
    elif command in ("keyboard", "kb"):
        func = getattr(args, "func", kb_cmd_status)
        driver = SkillkorpK20Driver()
        func(driver, args)
        pending = driver.pending_settings() if driver.is_connected() else []
        if pending and not args.json:
            print(f"{YELLOW}⏸ Clavier en veille : {len(pending)} réglage(s) enregistré(s), appliqué(s) "
                  f"automatiquement à son réveil (indicateur ou application ouverts, ou "
                  f"« skillkorpctl keyboard sync »).{RESET}")
    elif command in ("mouse", "ms"):
        func = getattr(args, "func", mouse_cmd_status)
        func(SkillkorpM20Driver(), args)
    elif command == "daemon":
        cmd_daemon(args)
    elif command == "doctor":
        return cmd_doctor(args)
    elif command == "tray":
        cmd_tray(args)
    elif command == "autostart":
        cmd_autostart(args)
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "json"):
        args.json = False
    safe_migrate()
    try:
        return run(args) or 0
    except CommandFailed as e:
        print(f"{RED}✗ {e}{RESET}", file=sys.stderr)
        return e.code
    except DevicePermissionError as e:
        print(f"{RED}✗ {e}{RESET}\n  Diagnostic : skillkorpctl doctor", file=sys.stderr)
        return EXIT_DEVICE
    except DeviceNotFoundError as e:
        print(f"{RED}✗ {e}{RESET}", file=sys.stderr)
        return EXIT_DEVICE
    except DeviceError as e:
        print(f"{RED}✗ Erreur de communication : {e}{RESET}", file=sys.stderr)
        return EXIT_REFUSED
    except ValueError as e:
        print(f"{RED}✗ {e}{RESET}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
