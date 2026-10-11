"""
Tests du pilote souris M20 contre MU_SkillKorp.exe (construction des rapports
0x04, 0x05, 0x06, 0x08, 0x0C). FakeHardware utilise le vrai descripteur HID
de la souris, donc les longueurs envoyées sont celles déclarées par l'appareil.
"""

import json
import unittest

import skillkorp.devices.mouse as ms
from skillkorp.devices.mouse import (
    BUTTON_ACTIONS, LIGHT_MODES, PHYSICAL_BTN_TO_SLOT, POLLING_RATE_MAP, SkillkorpM20Driver,
    build_dpi_frame, build_light_power_frame, build_query_frame, parse_status_report,
)
from tests.fakes import M20_VENDOR_DESCRIPTOR, FakeHardware, isolated_home, make_node


def csum16(buf, start, end):
    s = sum(buf[start:end]) & 0xFFFF
    return [(s >> 8) & 0xFF, s & 0xFF]


class MouseTestCase(unittest.TestCase):
    def setUp(self):
        self._home = isolated_home()
        self._home.__enter__()
        self.hw = FakeHardware(SkillkorpM20Driver, make_node(ms.VENDOR_ID, ms.PID_WIRELESS, M20_VENDOR_DESCRIPTOR))
        self._hw = self.hw.installed()
        self._hw.__enter__()
        self.drv = SkillkorpM20Driver()

    def tearDown(self):
        self._hw.__exit__(None, None, None)
        self._home.__exit__(None, None, None)

    def sent(self, report_id):
        return [w for w in self.hw.writes if w[0] == report_id]


class TestConstants(unittest.TestCase):
    def test_polling_complement(self):
        for rate, info in POLLING_RATE_MAP.items():
            self.assertEqual(info["interval"] + info["not_interval"], 0xFF, rate)

    def test_light_modes_are_zero_based(self):
        codes = [c for _, _, c in LIGHT_MODES]
        self.assertEqual(codes, list(range(12)))
        self.assertEqual(dict((k, c) for k, _, c in LIGHT_MODES)["static"], 1)

    def test_buttons(self):
        self.assertEqual(set(PHYSICAL_BTN_TO_SLOT), {1, 2, 3, 4, 5, 6})
        self.assertEqual(BUTTON_ACTIONS["undo"][1:], (0x11, 0x01, 0x1A))
        self.assertEqual(BUTTON_ACTIONS["select_all"][1:], (0x11, 0x01, 0x14))


class TestFrames(unittest.TestCase):
    def test_report5_matches_vendor_defaults(self):
        # vendor defaults: static, brightness 8, speed 6, sleep 10 min, LED off 1 min, debounce 4 ms
        f = build_light_power_frame("static", 8, 6, (0, 0, 0xFF), 10, 1, 4)
        self.assertEqual(len(f), 15)
        self.assertEqual(list(f[0:11]), [0x05, 0x0F, 0x01, 0x10, 0x03, 0xA8, 0, 0, 0xFF, 1, 4])
        self.assertEqual(list(f[11:13]), csum16(f, 3, 11))

    def test_report5_sleep_split_over_nibbles(self):
        f = build_light_power_frame("static", 8, 8, (0, 0, 0), 60, 1, 4)  # 60 = 0x3C
        self.assertEqual(f[4] & 0xF0, 0x30)
        self.assertEqual(f[5] >> 4, 0xC)
        self.assertEqual(f[4] & 0x0F, 1)  # 9 - speed 8

    def test_report5_brightness_only_for_static_effects(self):
        self.assertEqual(build_light_power_frame("breathing", 3, 6, (1, 2, 3), 10, 1, 4)[5] & 0x0F, 8)
        self.assertEqual(build_light_power_frame("static_mixed", 3, 6, (1, 2, 3), 10, 1, 4)[5] & 0x0F, 3)
        self.assertEqual(build_light_power_frame("off", 3, 6, (1, 2, 3), 10, 1, 4)[3], 0x00)

    def test_report5_debounce_byte(self):
        self.assertEqual(build_light_power_frame("static", 8, 6, (0, 0, 0), 10, 1, 25)[10], 25)

    def test_report4(self):
        f = build_dpi_frame([400, 800, 26000], 2, 2, True, False, True, [(1, 2, 3)])
        self.assertEqual(list(f[0:8]), [0x04, 0x38, 0x01, 1, 1, 0b111, 0, 1])
        self.assertEqual((f[8], f[16]), (7, 0))          # 400/50 - 1
        self.assertEqual((f[10], f[18]), (519 & 0xFF, 519 >> 8))  # 26000/50 - 1
        self.assertEqual(f[24], 2)
        self.assertEqual(list(f[25:28]), [1, 2, 3])
        self.assertEqual(f[49], 1)
        self.assertEqual(list(f[50:52]), csum16(f, 3, 50))
        self.assertFalse(any(f[52:]), "padding must stay zero (trimmed to the descriptor size)")

    def test_query_frame(self):
        self.assertEqual(list(build_query_frame()[0:6]), [0x0C, 0x0A, 0x01, 0xFE, 0x01, 0xFE])

    def test_status_parsing(self):
        self.assertEqual(parse_status_report(bytes([3, 0x10, 0x40, 2, 87])),
                         {"type": "battery", "percentage": 87, "charging": True})
        self.assertEqual(parse_status_report(bytes([3, 0, 0x10, 3, 0])), {"type": "dpi_stage", "stage": 3})
        # Reports captured on the real M20: on battery (state 1), on USB-C (3).
        self.assertEqual(parse_status_report(bytes.fromhex("031040015b")),
                         {"type": "battery", "percentage": 91, "charging": False})
        self.assertEqual(parse_status_report(bytes.fromhex("0310400364")),
                         {"type": "battery", "percentage": 100, "charging": True})
        self.assertIsNone(parse_status_report(bytes([3, 0, 0x40, 0, 250])))
        self.assertIsNone(parse_status_report(bytes([2, 0, 0x40, 0, 50])))


class TestWiredMouse(MouseTestCase):
    def setUp(self):
        super().setUp()
        self.hw.node = make_node(ms.VENDOR_ID, ms.PID_WIRED, M20_VENDOR_DESCRIPTOR)

    def test_cable_means_charging_even_without_report(self):
        bat = self.drv.get_battery()
        self.assertTrue(bat["charging"])
        self.assertIsNone(bat["percentage"])
        self.assertIn("En charge", bat["status_str"])

    def test_full_on_cable(self):
        self.hw.input_queue.append(bytes.fromhex("0310400364"))
        bat = self.drv.get_battery(poll_hardware=True)
        self.assertEqual((bat["percentage"], bat["charging"]), (100, True))
        self.assertEqual(bat["status_str"], "Chargée (100%)")


class TestDriver(MouseTestCase):
    def test_reports_are_sized_by_descriptor(self):
        self.drv.set_dpi_and_sensor(stages=[800, 1600])
        self.drv.set_rgb_lighting("static")
        self.drv.set_polling_rate(500)
        self.assertEqual(len(self.sent(0x04)[-1]), 52)   # 51 data bytes + report ID
        self.assertEqual(len(self.sent(0x05)[-1]), 13)
        self.assertEqual(len(self.sent(0x06)[-1]), 9)

    def test_dpi_rounding_and_validation(self):
        self.drv.set_dpi_and_sensor(stages=[812, 1600], active_stage=1)
        self.assertEqual(self.drv.config["dpi_stages"], [800, 1600])
        with self.assertRaises(ValueError):
            self.drv.set_dpi_and_sensor(stages=[10])
        with self.assertRaises(ValueError):
            self.drv.set_dpi_and_sensor(stages=[800] * 9)
        with self.assertRaises(ValueError):
            self.drv.set_dpi_and_sensor(active_stage=7)

    def test_debounce_goes_through_report5(self):
        self.assertTrue(self.drv.set_dpi_and_sensor(debounce=12))
        self.assertEqual(self.sent(0x05)[-1][10], 12)
        self.assertEqual(self.drv.config["debounce"], 12)
        with self.assertRaises(ValueError):
            self.drv.set_debounce(1)

    def test_lighting_preserves_power_settings(self):
        self.drv.set_power_settings(sleep_timer_minutes=20, led_off_minutes=5)
        self.drv.set_rgb_lighting("breathing", speed=8, color=(10, 20, 30))
        f = self.sent(0x05)[-1]
        self.assertEqual((f[4] & 0xF0) | (f[5] >> 4), 20)
        self.assertEqual(f[9], 5)
        self.assertEqual(list(f[6:9]), [10, 20, 30])

    def test_move_to_wake_is_ignored(self):
        self.assertTrue(self.drv.set_power_settings(move_to_wake=False))
        self.assertNotIn("move_to_wake", self.drv.config)

    def test_missing_ack_is_retried_then_fails(self):
        self.hw.auto_ack = False
        self.assertFalse(self.drv.set_polling_rate(500))
        self.assertEqual(len(self.sent(0x06)), 2)
        self.assertIn("confirmé", self.drv.last_error)
        self.assertEqual(self.drv.config["polling_rate"], 1000)

    def test_ack_parsing(self):
        self.assertEqual(parse_status_report(bytes([3, 0x10, 0x50, 0, 6])), {"type": "ack", "report": 6})

    def test_failure_keeps_config(self):
        self.hw.fail_writes = 1
        self.assertFalse(self.drv.set_polling_rate(125))
        self.assertEqual(self.drv.config["polling_rate"], 1000)

    def test_buttons_safety(self):
        with self.assertRaises(ValueError):
            self.drv.set_buttons({1: "copy"})
        self.assertTrue(self.drv.set_buttons({4: "copy"}))
        f = self.sent(0x08)[-1]
        self.assertEqual(len(f), 59)
        self.assertEqual(list(f[3 + 6 * 3:3 + 6 * 3 + 3]), [0x11, 0x01, 0x06])
        self.assertEqual(list(f[57:59]), csum16(f, 3, 57))

    def test_battery_unknown_until_reported(self):
        self.assertIsNone(self.drv.get_battery()["percentage"])
        self.hw.input_queue.append(bytes([3, 0x10, 0x40, 0, 64]))
        bat = self.drv.get_battery(poll_hardware=True)
        self.assertEqual(bat["percentage"], 64)
        self.assertEqual(self.sent(0x0C)[-1][0], 0x0C)
        self.assertEqual(SkillkorpM20Driver().config["battery"], 64)  # persisted

    def test_dpi_button_event_updates_active_stage(self):
        self.hw.input_queue.append(bytes([3, 0, 0x10, 4, 0]))
        self.drv.poll_status()
        self.assertEqual(self.drv.config["active_stage"], 4)

    def test_listen_collects_events_without_query(self):
        self.hw.input_queue += [bytes([3, 0, 0x20, 2, 0]), bytes([3, 0x10, 0x40, 2, 55])]
        events = self.drv.listen(0.1)
        self.assertEqual([e["type"] for e in events], ["dpi_stage", "battery"])
        self.assertEqual(self.sent(0x0C), [])
        self.assertTrue(self.drv.config["charging"])

    def test_apply_profile_sends_each_report_once(self):
        prof = {"dpi_stages": [400, 800], "active_stage": 1, "polling_rate": 250, "light_mode": "neon",
                "debounce": 6, "buttons": {"4": "copy"}}
        self.assertTrue(self.drv.apply_profile(prof))
        self.assertEqual(sorted(w[0] for w in self.hw.writes), [4, 5, 6, 8])
        self.assertEqual(self.sent(0x05)[0][10], 6)
        self.assertEqual(self.drv.config["buttons"]["4"], "copy")

    def test_old_config_is_upgraded(self):
        with open(ms.DEFAULT_PROFILE_FILE, "w") as f:
            json.dump({"move_to_wake": True, "speed": 2, "dpi_stages": [400, 99999], "battery": 100}, f)
        drv = SkillkorpM20Driver()
        self.assertNotIn("move_to_wake", drv.config)
        self.assertEqual(drv.config["speed"], 4)
        self.assertEqual(drv.config["dpi_stages"], [400, 800, 1600, 3200, 6400, 26000])
        self.assertIsNone(drv.config["battery"])  # 1.0.x fabricated 100 %


if __name__ == "__main__":
    unittest.main()
