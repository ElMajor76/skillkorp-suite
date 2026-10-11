"""
Tests du pilote clavier K20 contre le protocole du logiciel officiel
(KU_SkillKorp Driver 2.1.78, classe yc3121_cx71d_soc). Aucune trame n'atteint
un vrai périphérique : FakeHardware enregistre tout.
"""

import json
import os
import unittest
from unittest.mock import patch

import skillkorp.devices.keyboard as kb
from skillkorp.devices.keyboard import (
    CMD_GET_BATTERY, CMD_GET_KBOPTION, CMD_SET_DEBOUNCE, CMD_SET_FN_SIMPLE, CMD_SET_KBOPTION,
    CMD_SET_KEYMATRIX_SIMPLE, CMD_SET_LEDPARAM, CMD_SET_REPORT, CMD_SET_RESERT, CMD_SET_SLEDPARAM,
    CMD_SET_SLEEPTIME, KEY_LAYOUT, KEY_MATRIX_INDEX, LIGHT_MODES, POLLING_RATE_MAP, REMAP_ACTIONS,
    SkillkorpK20Driver, _HIDIOCGFEATURE, _HIDIOCSFEATURE,
)
from tests.fakes import K20_VENDOR_DESCRIPTOR, FakeHardware, isolated_home, make_node


def bit7_ok(p):
    return p[7] == (~sum(p[0:7])) & 0xFF


def bit8_ok(p):
    return p[8] == (~sum(p[0:8])) & 0xFF


class KeyboardTestCase(unittest.TestCase):
    wireless = False

    def setUp(self):
        self._home = isolated_home()
        self.tmp = self._home.__enter__()
        node = make_node(kb.VENDOR_ID, kb.PID_WIRELESS if self.wireless else kb.PID_WIRED, K20_VENDOR_DESCRIPTOR)
        self.hw = FakeHardware(SkillkorpK20Driver, node)
        self._hw = self.hw.installed()
        self._hw.__enter__()
        self.drv = SkillkorpK20Driver()

    def tearDown(self):
        self._hw.__exit__(None, None, None)
        self._home.__exit__(None, None, None)

    def last(self):
        return self.hw.payloads()[-1]


class TestConstants(unittest.TestCase):
    def test_ioctl_macros(self):
        self.assertEqual(_HIDIOCSFEATURE(65), (3 << 30) | (65 << 16) | (ord("H") << 8) | 0x06)
        self.assertEqual(_HIDIOCGFEATURE(65), (3 << 30) | (65 << 16) | (ord("H") << 8) | 0x07)

    def test_command_codes_match_vendor_class(self):
        self.assertEqual((CMD_SET_REPORT, CMD_SET_KBOPTION, CMD_SET_LEDPARAM, CMD_SET_SLEDPARAM),
                         (0x04, 0x06, 0x07, 0x08))
        self.assertEqual((CMD_SET_DEBOUNCE, CMD_SET_SLEEPTIME, CMD_SET_KEYMATRIX_SIMPLE, CMD_SET_FN_SIMPLE),
                         (0x11, 0x12, 0x13, 0x15))
        self.assertEqual(CMD_SET_RESERT, 0x02)

    def test_polling_rate_codes(self):
        self.assertEqual(POLLING_RATE_MAP, {1000: 1, 500: 2, 250: 4, 125: 8})

    def test_light_mode_codes(self):
        codes = {k: c for k, _, c in LIGHT_MODES}
        self.assertEqual(codes["off"], 0)
        self.assertEqual(codes["static"], 1)
        self.assertEqual(codes["wave"], 4)
        self.assertEqual(codes["custom"], 13)

    def test_every_key_has_a_matrix_index(self):
        self.assertEqual(len(KEY_LAYOUT), 85)
        self.assertEqual(set(KEY_MATRIX_INDEX), {k["name"] for k in KEY_LAYOUT})
        self.assertEqual(len(set(KEY_MATRIX_INDEX.values())), 85, "deux touches partagent un index")

    def test_matrix_indices_follow_vendor_column_layout(self):
        # defaultMatrix is column-major, 6 rows: Esc, ², Tab, Caps, LShift, LCtrl, then F1, 1, A...
        expected = {"Esc": 0, "²": 1, "Tab": 2, "Verr Maj": 3, "Maj G.": 4, "Ctrl G.": 5,
                    "F1": 6, "& 1": 7, "A": 8, "Q": 9, "Fn": 11, "F2": 12, "Espace": 41,
                    "Molette": 75, "Recherche": 78}
        for key, idx in expected.items():
            self.assertEqual(KEY_MATRIX_INDEX[key], idx, key)

    def test_azerty_shortcuts(self):
        self.assertEqual(REMAP_ACTIONS["undo"][1], [0, 224, 26, 0])        # Ctrl + (AZERTY Z)
        self.assertEqual(REMAP_ACTIONS["select_all"][1], [0, 224, 20, 0])  # Ctrl + (AZERTY A)
        self.assertEqual(REMAP_ACTIONS["copy"][1], [0, 224, 6, 0])


class TestLighting(KeyboardTestCase):
    def test_single_color_uses_normal_flag(self):
        self.assertTrue(self.drv.set_rgb(mode="static", speed=3, brightness=4, color="#123456", colorful=False))
        p = self.last()
        self.assertEqual(list(p[0:8]), [CMD_SET_LEDPARAM, 1, 2, 4, 7, 0x12, 0x34, 0x56])
        self.assertTrue(bit8_ok(p))

    def test_colorful_uses_dazzle_flag(self):
        self.drv.set_rgb(mode="breathing", color="#FF0000", colorful=True)
        self.assertEqual(self.last()[4] & 0x0F, 8)

    def test_speed_is_inverted(self):
        for speed, hw in ((1, 4), (3, 2), (5, 0)):
            self.drv.set_rgb(mode="static", speed=speed)
            self.assertEqual(self.last()[2], hw)

    def test_wave_direction_in_high_nibble(self):
        self.drv.set_rgb(mode="wave", direction=1, colorful=True)
        self.assertEqual(self.last()[4], (1 << 4) | 8)
        self.drv.set_rgb(mode="wave", direction=3, colorful=False)
        self.assertEqual(self.last()[4], (3 << 4) | 7)

    def test_pure_white_is_substituted(self):
        self.drv.set_rgb(mode="static", color="#FFFFFF", colorful=False)
        self.assertEqual(list(self.last()[5:8]), [0xFA, 0xFF, 0xFA])

    def test_unspecified_values_are_preserved(self):
        self.drv.set_rgb(mode="static", speed=2, brightness=1, color="#00FF00", colorful=False)
        self.drv.set_rgb(brightness=3)
        p = self.last()
        self.assertEqual((p[1], p[2], p[3], p[4], p[6]), (1, 3, 3, 7, 0xFF))

    def test_invalid_mode_raises_and_sends_nothing(self):
        with self.assertRaises(ValueError):
            self.drv.set_rgb(mode="disco")
        self.assertEqual(self.hw.writes, [])

    def test_invalid_color_raises(self):
        with self.assertRaises(ValueError):
            self.drv.set_rgb(mode="static", color="#12")

    def test_side_led_speed_not_inverted(self):
        self.assertTrue(self.drv.set_side_rgb(mode="static", speed=5, brightness=2, color="#010203", colorful=False))
        p = self.last()
        self.assertEqual(list(p[0:8]), [CMD_SET_SLEDPARAM, 1, 4, 2, 7, 1, 2, 3])
        self.assertTrue(bit8_ok(p))

    def test_side_rainbow_has_no_color(self):
        self.drv.set_side_rgb(mode="rainbow", color="#FF0000")
        p = self.last()
        self.assertEqual((p[1], p[4], p[5]), (3, 7, 0))

    def test_config_updated_only_on_success(self):
        self.hw.fail_writes = 1
        self.assertFalse(self.drv.set_rgb(mode="static", color="#0000FF", colorful=False))
        self.assertNotEqual(self.drv.config["rgb"]["color"], "#0000FF")
        self.assertTrue(self.drv.set_rgb(mode="static", color="#0000FF", colorful=False))
        with open(kb.DEFAULT_PROFILE_FILE) as f:
            self.assertEqual(json.load(f)["rgb"]["color"], "#0000FF")


class TestSimpleCommands(KeyboardTestCase):
    def test_polling_rate(self):
        self.drv.set_polling_rate(500)
        p = self.last()
        self.assertEqual(list(p[0:3]), [CMD_SET_REPORT, 0, 2])
        self.assertTrue(bit7_ok(p))
        with self.assertRaises(ValueError):
            self.drv.set_polling_rate(333)

    def test_debounce_has_no_profile_byte(self):
        self.drv.config["profile_index"] = 2
        self.drv.set_debounce(5)
        self.assertEqual(list(self.last()[0:3]), [CMD_SET_DEBOUNCE, 0, 5])

    def test_debounce_clamped(self):
        self.drv.set_debounce(99)
        self.assertEqual(self.last()[2], kb.DEBOUNCE_RANGE[1])

    def test_sleep_layout_and_limits(self):
        self.drv.set_sleep_time(30, 99999)  # clamped to 60 s / 3600 s
        p = self.last()
        self.assertEqual(p[0], CMD_SET_SLEEPTIME)
        self.assertTrue(bit7_ok(p))
        light = p[10] | (p[11] << 8)
        deep = p[14] | (p[15] << 8)
        self.assertEqual((light, deep), (60, 3600))
        self.assertEqual(p[8:10], p[10:12])
        self.drv.set_sleep_time(0, 0)
        self.assertEqual(bytes(self.last()[8:16]), bytes(8))

    def test_factory_reset(self):
        self.drv.set_rgb(mode="static", color="#00FF00", colorful=False)
        self.assertTrue(self.drv.factory_reset())
        self.assertEqual(self.last()[0], CMD_SET_RESERT)
        self.assertEqual(self.drv.config["rgb"]["mode"], "wave")

    def test_profile_range(self):
        self.assertTrue(self.drv.set_profile(2))
        with self.assertRaises(ValueError):
            self.drv.set_profile(3)


class TestOptions(KeyboardTestCase):
    def test_read_modify_write_preserves_unknown_bits(self):
        # keyboard reports: LED-off bit (0x10) set, Fn byte 1, power-save 1
        self.hw.answers[CMD_GET_KBOPTION] = bytes([CMD_GET_KBOPTION, 0, 0x10, 1, 1])
        self.assertTrue(self.drv.set_keyboard_options(win_lock=True, gaming_mode=True))
        p = self.last()
        self.assertEqual(p[0], CMD_SET_KBOPTION)
        self.assertEqual(p[2], 0x10 | 0x01 | 0x40)
        self.assertEqual((p[3], p[4]), (1, 1))
        self.assertTrue(bit7_ok(p))

    def test_mac_mode_bit(self):
        self.drv.set_keyboard_options(os_mode="mac")
        self.assertEqual(self.last()[2] & 0x02, 0x02)
        with self.assertRaises(ValueError):
            self.drv.set_keyboard_options(os_mode="android")

    def test_wasd_bit(self):
        self.drv.set_keyboard_options(wasd_swap=True)
        self.assertEqual(self.last()[2] & 0x08, 0x08)


class TestRemap(KeyboardTestCase):
    def test_remap_uses_matrix_index(self):
        self.assertTrue(self.drv.remap_key("F1", "media_play_pause"))
        p = self.last()
        self.assertEqual(list(p[0:3]), [CMD_SET_KEYMATRIX_SIMPLE, 0, 6])
        self.assertTrue(bit7_ok(p))
        self.assertEqual(list(p[8:12]), [3, 0, 205, 0])
        self.assertEqual(self.drv.config["key_remaps"], {"F1": "media_play_pause"})

    def test_default_restores_factory_action(self):
        self.drv.remap_key("Esc", "disabled")
        self.drv.remap_key("Esc", "default")
        self.assertEqual(list(self.last()[8:12]), [0, 0, 41, 0])
        self.assertEqual(self.drv.config["key_remaps"], {})

    def test_fn_layer_default(self):
        self.drv.remap_fn_key("F5", "default")
        p = self.last()
        self.assertEqual((p[0], p[2]), (CMD_SET_FN_SIMPLE, KEY_MATRIX_INDEX["F5"]))
        self.assertEqual(list(p[8:12]), [3, 0, 226, 0])  # factory Fn+F5 = mute

    def test_unknown_action_or_key_raises(self):
        with self.assertRaises(ValueError):
            self.drv.remap_key("F1", "explode")
        with self.assertRaises(ValueError):
            self.drv.remap_key("F13", "copy")
        self.assertEqual(self.hw.writes, [])

    def test_uses_active_hardware_profile(self):
        self.drv.set_profile(1)
        self.drv.remap_key("A", "copy")
        self.assertEqual(self.last()[1], 1)


class TestWiredBattery(KeyboardTestCase):
    def test_cable_reports_charging_without_level(self):
        bat = self.drv.get_battery()
        self.assertEqual((bat["percentage"], bat["charging"], bat["wireless"]), (None, True, False))
        self.assertEqual(bat["status_str"], "En charge (câble USB)")
        self.assertEqual(self.hw.writes, [])


class TestWireless(KeyboardTestCase):
    wireless = True

    def commands(self):
        return [w[1] for w in self.hw.writes]

    INIT = [0xF7, 0xFE, 0xF7, 0xF6]

    def test_init_once_then_poll_before_each_frame(self):
        # Sequence captured from the vendor software.
        self.drv.set_debounce(4)
        self.assertEqual(self.commands()[:5], self.INIT + [CMD_SET_DEBOUNCE])
        self.assertEqual(self.commands()[5:], [0xF7, 0x91, 0xF7, 0xFC])  # readback check
        self.assertEqual(self.hw.writes[1][1:3], bytes([0xFE, 64]))
        self.assertEqual(self.hw.writes[3][1:3], bytes([0xF6, 0x0A]))
        self.assertTrue(all(len(w) == 65 for w in self.hw.writes))
        self.hw.writes.clear()
        self.drv.set_debounce(5)
        self.assertEqual(self.commands()[:2], [0xF7, CMD_SET_DEBOUNCE])
        self.assertNotIn(0xFE, self.commands())

    def test_wireless_read_fetches_answer(self):
        self.hw.answers[0x80] = bytes([0x80, 0x09, 0x01])
        self.assertEqual(self.drv.get_firmware_version(), 0x0109)
        cmds = self.commands()
        self.assertEqual(cmds[:5], self.INIT + [0x80])
        self.assertEqual(cmds[-2:], [0xF7, 0xFC])
        self.assertIsNone(self.hw.rf_pending)

    def test_lost_write_is_resent_after_readback(self):
        # A frame relayed while the keyboard sends keystrokes can be lost
        # silently (seen with tools/hwtest.py): the readback catches it.
        self.hw.rf_lose_writes = 1
        self.assertTrue(self.drv.set_rgb(mode="static", color="#00FF00", colorful=False))
        leds = [p for p in self.hw.payloads() if p[0] == CMD_SET_LEDPARAM]
        self.assertEqual(len(leds), 2)
        self.assertEqual(self.hw.answers[0x87][5:8], bytes([0, 255, 0]))

    def test_write_never_applied_fails(self):
        self.hw.rf_lose_writes = 10
        self.assertFalse(self.drv.set_debounce(4))
        self.assertIn("pas appliqué", self.drv.last_error)
        self.assertNotEqual(self.drv.config.get("debounce_ms"), 4)
        self.assertEqual(len([p for p in self.hw.payloads() if p[0] == CMD_SET_DEBOUNCE]),
                         kb.RF_WRITE_ATTEMPTS)

    def test_wireless_read_timeout(self):
        self.hw.rf_keyboard_answers = False
        clock = [0.0]

        def monotonic():
            clock[0] += 0.1
            return clock[0]
        with patch("time.monotonic", monotonic):
            self.assertIsNone(self.drv.get_firmware_version())
        self.assertNotIn(0xFC, self.commands())

    def test_leftover_answer_is_drained_before_writing(self):
        # An unread answer blocks the real receiver: it must be fetched first.
        self.hw.rf_pending = bytes([0x91, 0x00, 0x08])
        self.assertTrue(self.drv.set_debounce(4))
        self.assertEqual(self.commands()[:7], self.INIT + [0xFC, 0xF7, CMD_SET_DEBOUNCE])
        self.assertIsNone(self.hw.rf_pending)

    ASLEEP = bytes([0, 0x54, 0, 1, 1, 1, 1, 0x90, 6])  # captured while off/asleep
    AWAKE = bytes([0, 0x54, 0, 0, 1, 1, 1, 0x90, 6])

    def test_setting_deferred_while_asleep_then_synced(self):
        self.hw.rf_status = self.ASLEEP
        self.assertTrue(self.drv.set_debounce(4))
        self.assertTrue(self.drv.last_deferred)
        self.assertNotIn(CMD_SET_DEBOUNCE, self.commands())
        self.assertEqual(self.drv.config["debounce_ms"], 4)
        self.assertTrue(self.drv.set_rgb(mode="static", color="#00FF00", colorful=False))
        self.assertEqual(self.drv.pending_settings(), ["debounce_ms", "rgb"])
        # Still asleep: nothing sent, nothing lost.
        self.assertFalse(self.drv.sync_pending())
        self.assertEqual(self.drv.pending_settings(), ["debounce_ms", "rgb"])
        # Awake (another process may do the sync: it reads the saved config).
        self.hw.rf_status = self.AWAKE
        self.hw.writes.clear()
        other = SkillkorpK20Driver()
        self.assertTrue(other.sync_pending())
        sent = [p for p in self.hw.payloads()]
        self.assertEqual([p[0] for p in sent], [CMD_SET_DEBOUNCE, CMD_SET_LEDPARAM])
        self.assertEqual(sent[0][2], 4)
        self.assertEqual(list(sent[1][5:8]), [0, 255, 0])
        self.assertEqual(self.drv.pending_settings(), [])

    def test_next_successful_write_flushes_pending(self):
        self.hw.rf_status = self.ASLEEP
        self.assertTrue(self.drv.remap_key("F1", "copy"))
        self.hw.rf_status = self.AWAKE
        self.assertTrue(self.drv.set_debounce(6))
        cmds = [p[0] for p in self.hw.payloads()]
        self.assertEqual(cmds, [CMD_SET_DEBOUNCE, CMD_SET_KEYMATRIX_SIMPLE])
        self.assertEqual(self.drv.pending_settings(), [])

    def test_offline_without_deferral_is_an_error(self):
        self.hw.rf_status = self.ASLEEP
        self.drv.defer_when_offline = False
        self.assertFalse(self.drv.set_debounce(4))
        self.assertIn("veille", self.drv.last_error)
        self.assertNotEqual(self.drv.config.get("debounce_ms"), 4)
        self.assertFalse(self.drv.last_deferred)

    def test_factory_reset_is_never_deferred(self):
        self.hw.rf_status = self.ASLEEP
        self.assertFalse(self.drv.factory_reset())
        self.assertEqual(self.drv.pending_settings(), [])

    def test_battery_from_receiver_status(self):
        self.hw.rf_status = bytes([0, 76, 0, 0, 1, 1, 1, 0x90, 6])
        bat = self.drv.get_battery()
        self.assertEqual((bat["percentage"], bat["charging"], bat["wireless"]), (76, False, True))
        self.assertEqual(self.commands(), [0xF7])

    def test_battery_garbage_is_unknown(self):
        self.hw.rf_status = bytes([0, 0xC8])
        bat = self.drv.get_battery()
        self.assertIsNone(bat["percentage"])
        self.assertTrue(bat["connected"])

    def test_options_read_modify_write_over_receiver(self):
        self.hw.answers[CMD_GET_KBOPTION] = bytes([CMD_GET_KBOPTION, 0, 0x04, 0x02, 0x81])
        self.assertTrue(self.drv.set_keyboard_options(win_lock=True))
        frame = self.hw.payloads()[-1]
        self.assertEqual(frame[0], CMD_SET_KBOPTION)
        self.assertEqual((frame[2] & 0x05, frame[3], frame[4]), (0x05, 0x02, 0x81))

    def test_options_fall_back_to_bits_saved_over_cable(self):
        self.hw.rf_ignore_queries = 1  # the options read goes unanswered
        self.drv.commit({"options_raw": [0x04, 0x02, 0x81]})
        clock = [0.0]

        def monotonic():
            clock[0] += 0.1
            return clock[0]
        with patch("time.monotonic", monotonic):
            self.assertTrue(self.drv.set_keyboard_options(win_lock=True))
        frame = self.hw.payloads()[-1]
        self.assertEqual((frame[2] & 0x05, frame[3], frame[4]), (0x05, 0x02, 0x81))
        self.assertEqual(self.drv.config["options_raw"], [frame[2], 0x02, 0x81])


class TestNotConnected(KeyboardTestCase):
    def test_battery_when_absent(self):
        self.hw.connected = False
        bat = self.drv.get_battery()
        self.assertFalse(bat["connected"])
        self.assertIsNone(bat["percentage"])

    def test_commands_raise_when_absent(self):
        self.hw.connected = False
        from skillkorp.devices.hidraw import DeviceNotFoundError
        with self.assertRaises(DeviceNotFoundError):
            self.drv.set_debounce(4)


class TestApplyProfile(KeyboardTestCase):
    def test_applies_every_section_and_restores_stale_remaps(self):
        self.drv.remap_key("Esc", "disabled")
        self.hw.writes.clear()
        profile = {
            "polling_rate": 500, "debounce_ms": 4,
            "sleep": {"light_sleep_sec": 120, "deep_sleep_sec": 1200},
            "options": {"win_lock": True, "wasd_swap": False, "os_mode": "win", "gaming_mode": True},
            "rgb": {"mode": "static", "speed": 2, "brightness": 3, "direction": 0, "color": "#00FF00",
                    "colorful": False},
            "side_rgb": {"mode": "off", "speed": 1, "brightness": 0, "color": "#000000"},
            "key_remaps": {"F1": "media_mute"}, "fn_remaps": {},
        }
        self.assertTrue(self.drv.apply_profile(profile))
        cmds = [p[0] for p in self.hw.payloads()]
        for cmd in (CMD_SET_REPORT, CMD_SET_DEBOUNCE, CMD_SET_SLEEPTIME, CMD_SET_KBOPTION, CMD_SET_LEDPARAM,
                    CMD_SET_SLEDPARAM, CMD_SET_KEYMATRIX_SIMPLE):
            self.assertIn(cmd, cmds)
        restore = [p for p in self.hw.payloads() if p[0] == CMD_SET_KEYMATRIX_SIMPLE and p[2] == 0]
        self.assertEqual(list(restore[0][8:12]), [0, 0, 41, 0])
        self.assertEqual(self.drv.config["key_remaps"], {"F1": "media_mute"})

    def test_reports_failure(self):
        self.hw.fail_writes = 1
        self.assertFalse(self.drv.apply_profile({"debounce_ms": 4, "polling_rate": 1000}))
        self.assertIn("Échec", self.drv.last_error)


class TestConfigFile(KeyboardTestCase):
    def test_corrupt_config_is_quarantined(self):
        with open(kb.DEFAULT_PROFILE_FILE, "w") as f:
            f.write("{ not json")
        drv = SkillkorpK20Driver()
        self.assertEqual(drv.config["rgb"]["mode"], "wave")
        moved = [n for n in os.listdir(kb.CONFIG_DIR) if ".corrupt-" in n]
        self.assertEqual(len(moved), 1)

    def test_bad_values_are_sanitized(self):
        with open(kb.DEFAULT_PROFILE_FILE, "w") as f:
            json.dump({"polling_rate": 7, "rgb": {"mode": "bogus", "color": "red"}, "options": {"os_mode": "ios"}}, f)
        drv = SkillkorpK20Driver()
        self.assertEqual(drv.config["polling_rate"], 1000)
        self.assertEqual(drv.config["rgb"]["mode"], "wave")
        self.assertEqual(drv.config["rgb"]["color"], "#FF0000")
        self.assertEqual(drv.config["options"]["os_mode"], "win")

    def test_concurrent_instances_do_not_lose_updates(self):
        other = SkillkorpK20Driver()
        self.drv.set_debounce(5)
        other.set_polling_rate(250)  # other has a stale in-memory copy
        fresh = SkillkorpK20Driver()
        self.assertEqual((fresh.config["debounce_ms"], fresh.config["polling_rate"]), (5, 250))


if __name__ == "__main__":
    unittest.main()
