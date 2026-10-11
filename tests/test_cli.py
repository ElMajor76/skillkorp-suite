"""Tests de skillkorpctl : codes de sortie et messages, sur faux périphériques."""

import contextlib
import io
import json
import unittest

import skillkorp.devices.keyboard as kb
import skillkorp.devices.mouse as ms
from skillkorp import cli
from skillkorp.devices.keyboard import SkillkorpK20Driver
from skillkorp.devices.mouse import SkillkorpM20Driver
from tests.fakes import K20_VENDOR_DESCRIPTOR, M20_VENDOR_DESCRIPTOR, FakeHardware, isolated_home, make_node


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(isolated_home())
        self.kb = FakeHardware(SkillkorpK20Driver, make_node(kb.VENDOR_ID, kb.PID_WIRED, K20_VENDOR_DESCRIPTOR))
        self.ms = FakeHardware(SkillkorpM20Driver, make_node(ms.VENDOR_ID, ms.PID_WIRELESS, M20_VENDOR_DESCRIPTOR))
        self._stack.enter_context(self.kb.installed())
        self._stack.enter_context(self.ms.installed())

    def tearDown(self):
        self._stack.close()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:  # argparse errors
                code = e.code
        return code, out.getvalue(), err.getvalue()


class TestCli(CliTestCase):
    def test_status_json(self):
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["keyboard"]["connected"])
        self.assertTrue(data["mouse"]["wireless"])

    def test_mouse_battery_polls_hardware(self):
        self.ms.input_queue.append(bytes([3, 0x10, 0x40, 0, 64]))
        code, out, _ = self.run_cli("mouse", "battery")
        self.assertEqual(code, 0)
        self.assertIn("64%", out)
        self.assertEqual(self.ms.writes[-1][0], 0x0C)
        code, out, _ = self.run_cli("mouse", "battery", "--json")
        self.assertEqual(json.loads(out)["percentage"], 64)

    def test_mouse_battery_absent_exit_2(self):
        self.ms.connected = False
        code, _, err = self.run_cli("mouse", "battery")
        self.assertEqual(code, cli.EXIT_DEVICE)

    def test_keyboard_rgb_success(self):
        code, out, _ = self.run_cli("keyboard", "rgb", "--mode", "static", "--color", "#00FF00", "--single-color")
        self.assertEqual(code, 0)
        self.assertIn("✓", out)
        self.assertEqual(self.kb.payloads()[-1][1], 1)

    def test_refused_command_exit_3(self):
        self.kb.fail_writes = 5
        code, out, err = self.run_cli("keyboard", "debounce", "4")
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertNotIn("✓", out)
        self.assertIn("Échec", err)

    def test_absent_device_exit_2(self):
        self.ms.connected = False
        code, _, err = self.run_cli("mouse", "rate", "--set", "500")
        self.assertEqual(code, cli.EXIT_DEVICE)
        self.assertIn("non détect", err)

    def test_invalid_input_exit_1_without_traceback(self):
        code, _, err = self.run_cli("mouse", "dpi", "--stages", "400,abc")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertNotIn("Traceback", err)
        code, _, _ = self.run_cli("mouse", "dpi", "--stages", "10")
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_argparse_rejects_out_of_range(self):
        code, _, _ = self.run_cli("keyboard", "rgb", "--speed", "9")
        self.assertEqual(code, 2)

    def test_profile_flow(self):
        self.assertEqual(self.run_cli("mouse", "profile", "new", "fps", "--copy-from", "gaming_fps")[0], 0)
        self.assertEqual(self.run_cli("mouse", "profile", "apps", "fps", "--apps", "cs2,quake")[0], 0)
        self.assertEqual(self.run_cli("mouse", "profile", "switch", "fps")[0], 0)
        code, out, _ = self.run_cli("mouse", "profile", "list", "--json")
        active = [p["id"] for p in json.loads(out) if p["active"]]
        self.assertEqual(active, ["fps"])
        self.assertEqual(self.run_cli("mouse", "profile", "new")[0], cli.EXIT_USAGE)
        self.assertEqual(self.run_cli("keyboard", "profile", "set", "nope")[0], cli.EXIT_USAGE)

    def test_profile_set_without_device_records_choice(self):
        self.kb.connected = False
        code, _, err = self.run_cli("keyboard", "profile", "set", "gaming")
        self.assertEqual(code, 0)
        self.assertIn("prochaine connexion", err)

    def test_remap_list_and_default(self):
        code, out, _ = self.run_cli("keyboard", "remap", "--list")
        self.assertEqual(code, 0)
        self.assertIn("media_vol_up", out)
        self.assertEqual(self.run_cli("keyboard", "remap", "F12", "default")[0], 0)

    def test_mouse_light_and_power(self):
        self.assertEqual(self.run_cli("mouse", "light", "--mode", "breathing", "--color", "#102030")[0], 0)
        code, _, err = self.run_cli("mouse", "power", "--led-off", "5", "--wake-mode", "move")
        self.assertEqual(code, 0)
        self.assertIn("mode de réveil", err)
        frame = [w for w in self.ms.writes if w[0] == 5][-1]
        self.assertEqual((frame[3] >> 4, frame[9]), (2, 5))


if __name__ == "__main__":
    unittest.main()


class TestKeyboardAsleepCli(CliTestCase):
    def setUp(self):
        super().setUp()
        self.kb.node = make_node(kb.VENDOR_ID, kb.PID_WIRELESS, K20_VENDOR_DESCRIPTOR)
        self.kb.rf_status = bytes([0, 0x54, 0, 1, 1, 1, 1, 0x90, 6])

    def test_setting_saved_and_reported_then_synced(self):
        code, out, _ = self.run_cli("keyboard", "debounce", "4")
        self.assertEqual(code, 0)
        self.assertIn("en veille", out)
        self.kb.rf_status = bytes([0, 0x54, 0, 0, 1, 1, 1, 0x90, 6])
        code, out, _ = self.run_cli("keyboard", "sync")
        self.assertEqual(code, 0)
        self.assertIn("appliqué", out)
        self.assertEqual(self.kb.payloads()[-1][0], kb.CMD_SET_DEBOUNCE)
        code, out, _ = self.run_cli("keyboard", "sync")
        self.assertIn("Aucun réglage en attente", out)
