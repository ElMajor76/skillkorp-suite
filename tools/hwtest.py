#!/usr/bin/env python3
"""
Guided hardware test of SkillKorp Suite on the real K20 keyboard / M20 mouse.

    python3 tools/hwtest.py            # keyboard, mouse and cables
    python3 tools/hwtest.py --only keyboard

For every setting: read the current state, apply a visible change, check it
(read back from the keyboard, receiver acknowledgement for the mouse, or by
asking you), then restore the original state. A report is written to
skillkorp-hwtest-<date>.txt in the current directory.

Nothing is left changed: original settings are restored after each step,
even when a step fails or is skipped (Ctrl+C restores too).
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skillkorp.devices import hidraw  # noqa: E402
from skillkorp.devices.hidraw import DeviceError  # noqa: E402
from skillkorp.devices import keyboard as kbmod  # noqa: E402
from skillkorp.devices.keyboard import KeyboardOfflineError, SkillkorpK20Driver, apply_checksum  # noqa: E402
from skillkorp.devices.mouse import FACTORY_BUTTONS, SkillkorpM20Driver  # noqa: E402

GREEN, RED, YELLOW, GREY, BOLD, RESET = ("\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[1m", "\033[0m") \
    if sys.stdout.isatty() else ("",) * 6
DEVICES = {(0x3151, 0x4011): "clavier, récepteur 2.4 GHz", (0x3151, 0x4015): "clavier, câble USB",
           (0x1D57, 0xFA60): "souris, récepteur 2.4 GHz", (0x1D57, 0xFA61): "souris, câble USB-C"}


class Report:
    def __init__(self):
        self.lines = []
        self.results = []

    def log(self, text: str = ""):
        print(text)
        self.lines.append(text.replace(GREEN, "").replace(RED, "").replace(YELLOW, "")
                          .replace(GREY, "").replace(BOLD, "").replace(RESET, ""))

    def result(self, name: str, status: str, detail: str = ""):
        colour = {"OK": GREEN, "ÉCHEC": RED}.get(status, YELLOW)
        self.results.append((name, status, detail))
        self.log(f"  {colour}{status}{RESET} {name}" + (f" — {detail}" if detail else ""))

    def save(self) -> str:
        path = os.path.abspath(time.strftime("skillkorp-hwtest-%Y%m%d-%H%M%S.txt"))
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lines) + "\n\nRÉSUMÉ\n")
            for name, status, detail in self.results:
                f.write(f"{status:7s} {name}" + (f" — {detail}" if detail else "") + "\n")
        return path


def ask(question: str) -> str:
    """'o' (yes), 'n' (no) or 'p' (skip)."""
    while True:
        try:
            answer = input(f"  {BOLD}?{RESET} {question} [o/n/p] ").strip().lower()
        except EOFError:
            return "p"
        if answer[:1] in ("o", "y", "n", "p", "s"):
            return {"y": "o", "s": "p"}.get(answer[:1], answer[:1])


def pause(text: str) -> bool:
    try:
        return input(f"  {BOLD}»{RESET} {text} (Entrée, ou « p » pour passer) ").strip().lower()[:1] != "p"
    except EOFError:
        return False


def hexs(data: bytes, n: int = 9) -> str:
    return bytes(data[:n]).hex(" ")


# =============================================================================
# Keyboard
# =============================================================================

class KeyboardTests:
    def __init__(self, rep: Report):
        self.rep = rep
        self.drv = SkillkorpK20Driver()
        self.drv.defer_when_offline = False  # a sleeping keyboard must fail, not be deferred

    def call(self, func, *args, **kwargs):
        """Run a driver call, asking to wake the keyboard up when it sleeps."""
        for _ in range(3):
            try:
                result = func(*args, **kwargs)
            except KeyboardOfflineError:
                result = False
            if result is not False or "veille" not in (self.drv.last_error or ""):
                return result
            if not pause("Clavier en veille : appuyez sur une touche du K20 puis"):
                return False
        return False

    def query(self, cmd: int, arg: int = 0) -> bytes:
        for _ in range(3):
            try:
                return self.drv._query(cmd, arg)
            except KeyboardOfflineError:
                if not pause("Clavier en veille : appuyez sur une touche du K20 puis"):
                    raise
        return self.drv._query(cmd, arg)

    def send_raw(self, cmd: int, data: bytes, checksum: str) -> bool:
        frame = bytearray(kbmod.PAYLOAD_LEN)
        frame[0] = cmd
        frame[1:1 + len(data)] = data
        apply_checksum(frame, checksum)
        return self.call(self.drv._guard, self.drv._send, frame)

    def step(self, name, read_cmd, apply, restore, question=None, expect=None, arg=0):
        """Generic step: read, apply, check (readback and/or question), restore."""
        self.rep.log(f"\n{BOLD}▶ Clavier — {name}{RESET}")
        before = b""
        try:
            before = self.query(read_cmd, arg) if read_cmd else b""
            if read_cmd:
                self.rep.log(f"  état initial : {hexs(before)}")
            if not self.call(apply):
                self.rep.result(name, "ÉCHEC", f"commande refusée ({self.drv.last_error})")
                return
            time.sleep(1.2)
            verdicts = []
            if read_cmd:
                after = self.query(read_cmd, arg)
                self.rep.log(f"  après réglage : {hexs(after)}")
                if expect is not None:
                    verdicts.append(("relecture", expect(after)))
                else:
                    verdicts.append(("relecture", after[1:9] != before[1:9]))
            if question:
                answer = ask(question)
                if answer != "p":
                    verdicts.append(("observation", answer == "o"))
        except KeyboardInterrupt:
            self.rep.log("  interrompu : restauration…")
            if before or not read_cmd:
                self.call(restore, before)
            raise
        except DeviceError as e:
            self.rep.result(name, "ÉCHEC", str(e))
            if read_cmd and not before:
                return  # nothing changed yet
            verdicts = None
        restored = self.call(restore, before)
        restore_note = ""
        if read_cmd and restored:
            time.sleep(0.8)
            again = self.query(read_cmd, arg)
            ok_restore = again[1:9] == before[1:9]
            restore_note = "restauré" if ok_restore else f"restauration différente : {hexs(again)}"
        elif not restored:
            restore_note = f"restauration refusée ({self.drv.last_error})"
        if verdicts is None:
            return
        if not verdicts:
            self.rep.result(name, "PASSÉ", restore_note)
        elif all(v for _, v in verdicts):
            self.rep.result(name, "OK", ", ".join(k for k, _ in verdicts) + (f" ; {restore_note}" if restore_note else ""))
        else:
            bad = ", ".join(k for k, v in verdicts if not v)
            self.rep.result(name, "ÉCHEC", f"{bad} négative" + (f" ; {restore_note}" if restore_note else ""))

    def run(self):
        drv = self.drv
        if not drv.is_connected():
            self.rep.log(f"{GREY}○ Clavier non détecté : tests clavier ignorés.{RESET}")
            return
        mode = "2.4 GHz" if drv.is_wireless() else "câble USB"
        self.rep.log(f"\n{BOLD}=== Clavier K20 ({mode}, {drv.dev_path}) ==={RESET}")
        pause("Appuyez sur une touche du K20 pour le réveiller, puis")
        fw = drv.get_firmware_version()
        self.rep.log(f"  firmware : {f'0x{fw:04X}' if fw is not None else 'non lu'}")
        self.rep.result("lecture firmware", "OK" if fw else "ÉCHEC")
        bat = drv.get_battery()
        self.rep.log(f"  batterie : {bat['status_str']}")
        profile = drv.config.get("profile_index", 0)

        led = lambda raw: self.send_raw(kbmod.CMD_SET_LEDPARAM, raw[1:8], "bit8")  # noqa: E731
        self.step("rétroéclairage fixe vert", kbmod.CMD_GET_LEDPARAM,
                  lambda: drv._guard(drv._send, kbmod.build_led_frame("static", 3, 4, 0, (0, 255, 0), False)),
                  led, "Les touches sont-elles vertes (fixe) ?",
                  expect=lambda r: r[1] == 1 and tuple(r[5:8]) == (0, 255, 0))
        self.step("rétroéclairage onde multicolore", kbmod.CMD_GET_LEDPARAM,
                  lambda: drv._guard(drv._send, kbmod.build_led_frame("wave", 4, 4, 0, (255, 0, 0), True)),
                  led, "Une vague arc-en-ciel défile-t-elle sur les touches ?",
                  expect=lambda r: r[1] == 4)
        self.step("luminosité minimale", kbmod.CMD_GET_LEDPARAM,
                  lambda: drv._guard(drv._send, kbmod.build_led_frame("static", 3, 1, 0, (255, 255, 255), False)),
                  led, "Les touches sont-elles blanches et nettement moins lumineuses ?",
                  expect=lambda r: r[3] == 1)
        side = lambda raw: self.send_raw(kbmod.CMD_SET_SLEDPARAM, raw[1:8], "bit8")  # noqa: E731
        self.step("bandes latérales fixes rouges", kbmod.CMD_GET_SLEDPARAM,
                  lambda: drv._guard(drv._send, kbmod.build_side_led_frame("static", 3, 4, (255, 0, 0), False)),
                  side, "Les bandes LED latérales sont-elles rouges (fixe) ?",
                  expect=lambda r: tuple(r[5:8]) == (255, 0, 0))
        self.step("fréquence 500 Hz", kbmod.CMD_GET_REPORT,
                  lambda: drv._guard(drv._send, kbmod.build_polling_rate_frame(500, profile), kbmod.DELAY_BIG),
                  lambda raw: self.send_raw(kbmod.CMD_SET_REPORT, bytes([profile, raw[2]]), "bit7"),
                  expect=lambda r: r[2] == kbmod.POLLING_RATE_MAP[500], arg=profile)
        self.step("anti-rebond 12 ms", kbmod.CMD_GET_DEBOUNCE,
                  lambda: drv._guard(drv._send, kbmod.build_debounce_frame(12)),
                  lambda raw: self.send_raw(kbmod.CMD_SET_DEBOUNCE, bytes([0, raw[2]]), "bit7"),
                  expect=lambda r: r[2] == 12)

        def sleep_restore(raw):
            light, deep = raw[3] | (raw[4] << 8), raw[7] | (raw[8] << 8)
            return drv._guard(drv._send, kbmod.build_sleep_frame(light, deep))
        self.step("veille 5 min / 20 min", kbmod.CMD_GET_SLEEPTIME,
                  lambda: drv._guard(drv._send, kbmod.build_sleep_frame(300, 1200)), sleep_restore,
                  expect=lambda r: (r[3] | r[4] << 8, r[7] | r[8] << 8) == (300, 1200))

        def opt_restore(raw):
            return self.send_raw(kbmod.CMD_SET_KBOPTION, bytes([profile, raw[2], raw[3], raw[4]]), "bit7")

        def win_lock_on():
            raw = self.query(kbmod.CMD_GET_KBOPTION, profile)
            return drv._guard(drv._send, kbmod.build_options_frame(raw[2] | kbmod.OPT_WIN_LOCK, raw[3], raw[4], profile))
        self.step("verrouillage de la touche Windows", kbmod.CMD_GET_KBOPTION, win_lock_on, opt_restore,
                  "Appuyez sur la touche Windows/Super : le menu des activités reste-t-il FERMÉ ?",
                  expect=lambda r: bool(r[2] & kbmod.OPT_WIN_LOCK), arg=profile)

        def remap_step(name, key, action, fn, question):
            section = "fn_remaps" if fn else "key_remaps"
            original = drv.config.get(section, {}).get(key, "default")  # before the change
            self.step(name, None, lambda: drv._remap(key, action, None, fn),
                      lambda raw: drv._remap(key, original, None, fn), question)
        remap_step("remappage F12 → volume +", "F12", "media_vol_up", False,
                   "Appuyez sur F12 (sans Fn) : le volume monte-t-il ?")
        remap_step("remappage Fn+F12 → muet", "F12", "media_mute", True,
                   "Appuyez sur Fn+F12 : le son est-il coupé/rétabli ?")


# =============================================================================
# Mouse
# =============================================================================

class MouseTests:
    def __init__(self, rep: Report):
        self.rep = rep
        self.drv = SkillkorpM20Driver()

    def step(self, name, apply, restore, question=None):
        self.rep.log(f"\n{BOLD}▶ Souris — {name}{RESET}")
        verdicts = []
        try:
            if not apply():
                self.rep.result(name, "ÉCHEC", f"non confirmé par la souris ({self.drv.last_error})")
                restore()
                return
            verdicts.append(("accusé du récepteur", True))
            if question:
                answer = ask(question)
                if answer != "p":
                    verdicts.append(("observation", answer == "o"))
        except DeviceError as e:
            self.rep.result(name, "ÉCHEC", str(e))
            restore()
            return
        except KeyboardInterrupt:
            self.rep.log("  interrompu : restauration…")
            restore()
            raise
        restored = restore()
        note = "restauré" if restored else f"restauration non confirmée ({self.drv.last_error})"
        ok = all(v for _, v in verdicts)
        self.rep.result(name, "OK" if ok else "ÉCHEC", ", ".join(k for k, _ in verdicts) + f" ; {note}")

    def run(self):
        drv = self.drv
        if not drv.is_connected():
            self.rep.log(f"{GREY}○ Souris non détectée : tests souris ignorés.{RESET}")
            return
        mode = "2.4 GHz" if drv.is_wireless() else "câble USB-C"
        self.rep.log(f"\n{BOLD}=== Souris M20 ({mode}, {drv.dev_path}) ==={RESET}")
        pause("Bougez la souris pour la réveiller, puis")
        bat = drv.get_battery(poll_hardware=True)
        self.rep.log(f"  batterie : {bat['status_str']}")
        self.rep.result("lecture batterie", "OK" if bat.get("percentage") is not None else "ÉCHEC")
        cfg = drv.config
        stages = list(cfg.get("dpi_stages") or [800])
        active = cfg.get("active_stage", 1)
        slow = [400] * len(stages)
        self.step("DPI 400 sur toutes les étapes",
                  lambda: drv.set_dpi_and_sensor(stages=slow, active_stage=active),
                  lambda: drv.set_dpi_and_sensor(stages=stages, active_stage=active),
                  f"Le pointeur est-il nettement plus lent qu'avant (DPI actuel {stages[active - 1]}) ?")
        rate = cfg.get("polling_rate", 1000)
        other = 500 if rate != 500 else 1000
        self.step(f"fréquence {other} Hz", lambda: drv.set_polling_rate(other), lambda: drv.set_polling_rate(rate))
        deb = cfg.get("debounce", 4)
        self.step("anti-rebond 10 ms", lambda: drv.set_debounce(10), lambda: drv.set_debounce(deb))
        sleep_min, led_off = cfg.get("sleep_timer_minutes", 10), cfg.get("led_off_minutes", 1)
        self.step("veille 2 / 15 min", lambda: drv.set_power_settings(sleep_timer_minutes=15, led_off_minutes=2),
                  lambda: drv.set_power_settings(sleep_timer_minutes=sleep_min, led_off_minutes=led_off))
        buttons = {int(k): v for k, v in (cfg.get("buttons") or FACTORY_BUTTONS).items()}
        self.step("bouton latéral avant → volume +",
                  lambda: drv.set_buttons({**buttons, 4: "media_vol_up"}),
                  lambda: drv.set_buttons(buttons),
                  "Appuyez sur le bouton latéral AVANT de la souris : le volume monte-t-il ?")


# =============================================================================
# Access / cables
# =============================================================================

def access_report(rep: Report):
    rep.log(f"\n{BOLD}=== Accès (règle udev) ==={RESET}")
    found = False
    for node in hidraw.list_hidraw_nodes():
        label = DEVICES.get((node.vendor_id, node.product_id))
        if not label:
            continue
        found = True
        readable = os.access(node.devnode, os.R_OK)
        expected = node.interface == 2
        status = "OK" if readable == expected else "ÉCHEC"
        what = "configuration" if expected else "saisie (frappes/pointeur)"
        rep.result(f"{node.devnode} — {label}, interface {node.interface} ({what})", status,
                   "accessible" if readable else "fermé")
    if not found:
        rep.log("  aucun périphérique SkillKorp détecté")


def cable_tests(rep: Report, only):
    if only in (None, "keyboard") and pause("Branchez le câble USB-C du CLAVIER (mode filaire), puis"):
        time.sleep(2)
        access_report(rep)
        kb = SkillkorpK20Driver()
        if kb.is_connected() and not kb.is_wireless():
            fw = kb.get_firmware_version()
            rep.result("clavier filaire : lecture firmware", "OK" if fw else "ÉCHEC",
                       f"0x{fw:04X}" if fw else kb.last_error)
        else:
            rep.result("clavier filaire", "ÉCHEC", "non détecté en filaire")
        pause("Remettez le clavier en 2.4 GHz (câble débranché), puis")
    if only in (None, "mouse") and pause("Branchez le câble USB-C de la SOURIS, puis"):
        time.sleep(2)
        access_report(rep)
        ms = SkillkorpM20Driver()
        if ms.is_connected() and not ms.is_wireless():
            rate = ms.config.get("polling_rate", 1000)
            ok = ms.set_polling_rate(rate)
            rep.result("souris filaire : écriture (fréquence inchangée)", "OK" if ok else "ÉCHEC", ms.last_error)
        else:
            rep.result("souris filaire", "ÉCHEC", "non détectée en filaire")
        pause("Débranchez le câble de la souris, puis")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=["keyboard", "mouse"])
    ap.add_argument("--no-cables", action="store_true", help="ne pas proposer les tests en filaire")
    args = ap.parse_args()
    rep = Report()
    rep.log(f"{BOLD}SkillKorp Suite — test matériel guidé{RESET} ({time.strftime('%Y-%m-%d %H:%M')})")
    rep.log("Répondez o (oui), n (non) ou p (passer). Chaque réglage est restauré après son test.")
    try:
        access_report(rep)
        if args.only in (None, "keyboard"):
            KeyboardTests(rep).run()
        if args.only in (None, "mouse"):
            MouseTests(rep).run()
        if not args.no_cables:
            rep.log(f"\n{BOLD}=== Câbles ==={RESET}")
            cable_tests(rep, args.only)
    except KeyboardInterrupt:
        rep.log("\nTest interrompu.")
    failed = [r for r in rep.results if r[1] == "ÉCHEC"]
    rep.log(f"\n{BOLD}Résultat : {sum(r[1] == 'OK' for r in rep.results)} OK, {len(failed)} échec(s), "
            f"{sum(r[1] == 'PASSÉ' for r in rep.results)} passé(s).{RESET}")
    rep.log(f"Rapport : {rep.save()}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
