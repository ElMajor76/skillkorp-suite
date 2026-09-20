"""
Tests unitaires pour skillkorp.devices.keyboard (SkillkorpK20Driver) et le
ProfileManager unifié côté clavier. Porté depuis skillkorp-k20/tests/test_driver.py.
Couvre les commandes HID, encodages de rapports, et la gestion des profils.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import skillkorp.devices.keyboard as keyboard
from skillkorp.devices.keyboard import (
    POLLING_RATE_MAP,
    LIGHT_MODES,
    SIDE_LIGHT_MODES,
    REMAP_ACTIONS,
    KEY_LAYOUT,
    SkillkorpK20Driver,
    _HIDIOCSFEATURE,
    _HIDIOCGFEATURE,
    CMD_SET_REPORT,
    CMD_SET_LEDPARAM,
    CMD_SET_KBOPTION,
    CMD_SET_RESERT,
    CMD_SET_DEBOUNCE,
    CMD_SET_SLEEPTIME,
    CMD_SET_KEYMATRIX_SIMPLE,
    CMD_SET_FN_SIMPLE,
    CMD_GET_BATTERY,
)
from skillkorp.profiles.manager import ProfileManager, DEFAULT_PROFILES_KEYBOARD


class TestHidiocsMacro(unittest.TestCase):
    """Tests du calcul des ioctl macros HIDIOCSFEATURE et HIDIOCGFEATURE."""

    def test_hidiocsfeature_65bytes(self):
        result = _HIDIOCSFEATURE(65)
        self.assertIsInstance(result, int)
        expected = (3 << 30) | (65 << 16) | (ord('H') << 8) | 0x06
        self.assertEqual(result, expected)

    def test_hidiocgfeature_65bytes(self):
        result = _HIDIOCGFEATURE(65)
        self.assertIsInstance(result, int)
        expected = (3 << 30) | (65 << 16) | (ord('H') << 8) | 0x07
        self.assertEqual(result, expected)


class TestConstants(unittest.TestCase):
    """Tests des dictionnaires et constantes du protocole."""

    def test_polling_rate_map(self):
        self.assertIn(1000, POLLING_RATE_MAP)
        self.assertIn(500, POLLING_RATE_MAP)
        self.assertIn(250, POLLING_RATE_MAP)
        self.assertIn(125, POLLING_RATE_MAP)
        self.assertEqual(POLLING_RATE_MAP[1000], 1)
        self.assertEqual(POLLING_RATE_MAP[500], 2)
        self.assertEqual(POLLING_RATE_MAP[250], 4)
        self.assertEqual(POLLING_RATE_MAP[125], 8)

    def test_light_modes(self):
        mode_keys = [k for k, _, _ in LIGHT_MODES]
        self.assertIn("off", mode_keys)
        self.assertIn("wave", mode_keys)
        self.assertIn("breathing", mode_keys)
        self.assertIn("static", mode_keys)
        self.assertEqual(len(LIGHT_MODES), 10)

    def test_key_layout(self):
        self.assertEqual(len(KEY_LAYOUT), 85)
        names = [k["name"] for k in KEY_LAYOUT]
        self.assertIn("Esc", names)
        self.assertIn("A", names)
        self.assertIn("Z", names)
        self.assertIn("Espace", names)
        self.assertIn("Entrée", names)

    def test_remap_actions(self):
        self.assertIn("media_vol_up", REMAP_ACTIONS)
        self.assertIn("mouse_left", REMAP_ACTIONS)
        self.assertIn("copy", REMAP_ACTIONS)
        self.assertIn("disabled", REMAP_ACTIONS)
        for act, (label, payload) in REMAP_ACTIONS.items():
            self.assertEqual(len(payload), 4)

    def test_device_type(self):
        self.assertEqual(SkillkorpK20Driver.device_type, "keyboard")


class TestDriverMethods(unittest.TestCase):
    """Tests des méthodes matérielles du pilote avec mock d'ioctl."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_config_dir = keyboard.CONFIG_DIR
        self.orig_profile_file = keyboard.DEFAULT_PROFILE_FILE
        keyboard.CONFIG_DIR = self.temp_dir.name
        keyboard.DEFAULT_PROFILE_FILE = os.path.join(self.temp_dir.name, "default_profile.json")

    def tearDown(self):
        keyboard.CONFIG_DIR = self.orig_config_dir
        keyboard.DEFAULT_PROFILE_FILE = self.orig_profile_file
        self.temp_dir.cleanup()

    @patch.object(SkillkorpK20Driver, "find_device", return_value="/dev/hidraw_test")
    @patch("os.path.exists", return_value=True)
    @patch("os.open", return_value=10)
    @patch("os.close")
    @patch("fcntl.ioctl", return_value=65)
    def test_set_rgb(self, mock_ioctl, mock_close, mock_open_fd, mock_exists, mock_find):
        driver = SkillkorpK20Driver()
        success = driver.set_rgb(mode="wave", speed=4, brightness=3, direction=1, color="#00FF00")
        self.assertTrue(success)
        self.assertTrue(mock_ioctl.called)
        args, _ = mock_ioctl.call_args
        buf = args[2]
        self.assertEqual(buf[0], 0x00)
        self.assertEqual(buf[1], CMD_SET_LEDPARAM)
        self.assertEqual(buf[2], 3)  # Wave code
        self.assertEqual(buf[3], 1)  # 5 - 4 = 1
        self.assertEqual(buf[4], 3)  # brightness
        self.assertEqual(buf[5], 1)  # direction
        self.assertEqual(buf[6], 0)  # R
        self.assertEqual(buf[7], 255)  # G
        self.assertEqual(buf[8], 0)  # B

    @patch.object(SkillkorpK20Driver, "find_device", return_value="/dev/hidraw_test")
    @patch("os.path.exists", return_value=True)
    @patch("os.open", return_value=10)
    @patch("os.close")
    @patch("fcntl.ioctl", return_value=65)
    def test_set_polling_rate(self, mock_ioctl, mock_close, mock_open_fd, mock_exists, mock_find):
        driver = SkillkorpK20Driver()
        success = driver.set_polling_rate(500)
        self.assertTrue(success)
        args, _ = mock_ioctl.call_args
        buf = args[2]
        self.assertEqual(buf[1], CMD_SET_REPORT)
        self.assertEqual(buf[3], 2)  # 500Hz -> 2

    @patch.object(SkillkorpK20Driver, "find_device", return_value="/dev/hidraw_test")
    @patch("os.path.exists", return_value=True)
    @patch("os.open", return_value=10)
    @patch("os.close")
    @patch("fcntl.ioctl", return_value=65)
    def test_set_debounce(self, mock_ioctl, mock_close, mock_open_fd, mock_exists, mock_find):
        driver = SkillkorpK20Driver()
        success = driver.set_debounce(4)
        self.assertTrue(success)
        args, _ = mock_ioctl.call_args
        buf = args[2]
        self.assertEqual(buf[1], CMD_SET_DEBOUNCE)
        self.assertEqual(buf[3], 4)

    @patch.object(SkillkorpK20Driver, "find_device", return_value="/dev/hidraw_test")
    @patch("os.path.exists", return_value=True)
    @patch("os.open", return_value=10)
    @patch("os.close")
    @patch("fcntl.ioctl", return_value=65)
    def test_set_keyboard_options(self, mock_ioctl, mock_close, mock_open_fd, mock_exists, mock_find):
        driver = SkillkorpK20Driver()
        success = driver.set_keyboard_options(win_lock=True, wasd_swap=False, os_mode="win", gaming_mode=True)
        self.assertTrue(success)
        args, _ = mock_ioctl.call_args
        buf = args[2]
        self.assertEqual(buf[1], CMD_SET_KBOPTION)
        bitfield = buf[3]
        self.assertTrue(bool(bitfield & (1 << 0)))  # win_lock bit
        self.assertTrue(bool(bitfield & (1 << 6)))  # gaming_mode bit
        self.assertFalse(bool(bitfield & (1 << 3)))  # wasd_swap bit

    @patch.object(SkillkorpK20Driver, "find_device", return_value="/dev/hidraw_test")
    @patch("os.path.exists", return_value=True)
    @patch("os.open", return_value=10)
    @patch("os.close")
    @patch("fcntl.ioctl", return_value=65)
    def test_remap_key(self, mock_ioctl, mock_close, mock_open_fd, mock_exists, mock_find):
        # Spec: FEA_CMD_SET_KEYMATRIX_SIMPLE places the 4-byte action frame at buf[8..11].
        driver = SkillkorpK20Driver()
        success = driver.remap_key("F1", "media_vol_up")
        self.assertTrue(success)
        args, _ = mock_ioctl.call_args
        buf = args[2]
        self.assertEqual(buf[1], CMD_SET_KEYMATRIX_SIMPLE)
        self.assertEqual(buf[3], 1)  # F1 index is 1
        self.assertEqual(buf[8], 3)
        self.assertEqual(buf[9], 0)
        self.assertEqual(buf[10], 233)  # 0xE9 volume up
        self.assertEqual(buf[11], 0)

    @patch.object(SkillkorpK20Driver, "find_device", return_value="/dev/hidraw_test")
    @patch("os.path.exists", return_value=True)
    @patch("os.open", return_value=10)
    @patch("os.close")
    @patch("fcntl.ioctl", return_value=65)
    def test_remap_fn_key(self, mock_ioctl, mock_close, mock_open_fd, mock_exists, mock_find):
        driver = SkillkorpK20Driver()
        success = driver.remap_fn_key("Q", "mouse_left")
        self.assertTrue(success)
        args, _ = mock_ioctl.call_args
        buf = args[2]
        self.assertEqual(buf[1], CMD_SET_FN_SIMPLE)
        self.assertEqual(buf[8], 1)
        self.assertEqual(buf[9], 0)
        self.assertEqual(buf[10], 240)
        self.assertEqual(buf[11], 0)

    @patch.object(SkillkorpK20Driver, "find_device", return_value="/dev/hidraw_test")
    @patch("os.path.exists", return_value=True)
    @patch("os.open", return_value=10)
    @patch("os.close")
    @patch("fcntl.ioctl", return_value=65)
    def test_set_sleep_time(self, mock_ioctl, mock_close, mock_open_fd, mock_exists, mock_find):
        driver = SkillkorpK20Driver()
        success = driver.set_sleep_time(light_sleep_sec=300, deep_sleep_sec=1680)
        self.assertTrue(success)
        args, _ = mock_ioctl.call_args
        buf = args[2]
        self.assertEqual(buf[1], CMD_SET_SLEEPTIME)

        light_lo, light_hi = 300 & 0xFF, (300 >> 8) & 0xFF
        deep_lo, deep_hi = 1680 & 0xFF, (1680 >> 8) & 0xFF

        self.assertEqual((buf[8], buf[9]), (light_lo, light_hi))
        self.assertEqual((buf[10], buf[11]), (light_lo, light_hi))
        self.assertEqual((buf[12], buf[13]), (deep_lo, deep_hi))
        self.assertEqual((buf[14], buf[15]), (deep_lo, deep_hi))

    @patch.object(SkillkorpK20Driver, "find_device", return_value="/dev/hidraw_test")
    @patch("os.path.exists", return_value=True)
    @patch("os.open", return_value=10)
    @patch("os.close")
    @patch("fcntl.ioctl", return_value=65)
    def test_factory_reset(self, mock_ioctl, mock_close, mock_open_fd, mock_exists, mock_find):
        driver = SkillkorpK20Driver()
        success = driver.factory_reset()
        self.assertTrue(success)
        args, _ = mock_ioctl.call_args
        buf = args[2]
        self.assertEqual(buf[1], CMD_SET_RESERT)

    @patch.object(SkillkorpK20Driver, "is_wireless", return_value=True)
    @patch.object(SkillkorpK20Driver, "is_connected", return_value=True)
    @patch.object(SkillkorpK20Driver, "find_device", return_value="/dev/hidraw_test")
    @patch("os.path.exists", return_value=True)
    @patch("os.open", return_value=10)
    @patch("os.close")
    @patch("fcntl.ioctl")
    def test_get_battery_wireless_calls_hidiocgfeature(self, mock_ioctl, mock_close, mock_open_fd, mock_exists, mock_find, mock_conn, mock_wl):
        driver = SkillkorpK20Driver()

        def fake_ioctl(fd, request, buf):
            if request == _HIDIOCGFEATURE(65):
                buf[0] = 0x00
                buf[1] = CMD_GET_BATTERY
                buf[2] = 82
                buf[3] = 1
                return 65
            elif request == _HIDIOCSFEATURE(65):
                return 65
            return 0

        mock_ioctl.side_effect = fake_ioctl

        bat = driver.get_battery()

        reqs = [call[0][1] for call in mock_ioctl.call_args_list]
        self.assertIn(_HIDIOCSFEATURE(65), reqs)
        self.assertIn(_HIDIOCGFEATURE(65), reqs)

        self.assertEqual(bat["percentage"], 82)
        self.assertTrue(bat["charging"])
        self.assertTrue(bat["wireless"])
        self.assertIn("82%", bat["status_str"])

    @patch.object(SkillkorpK20Driver, "is_wireless", return_value=True)
    @patch.object(SkillkorpK20Driver, "is_connected", return_value=True)
    @patch.object(SkillkorpK20Driver, "find_device", return_value="/dev/hidraw_test")
    @patch("os.path.exists", return_value=True)
    @patch("os.open", return_value=10)
    @patch("os.close")
    @patch("fcntl.ioctl")
    def test_get_battery_wireless_unpopulated_returns_none(self, mock_ioctl, mock_close, mock_open_fd, mock_exists, mock_find, mock_conn, mock_wl):
        driver = SkillkorpK20Driver()

        def fake_ioctl(fd, request, buf):
            return 65

        mock_ioctl.side_effect = fake_ioctl

        bat = driver.get_battery()

        reqs = [call[0][1] for call in mock_ioctl.call_args_list]
        self.assertIn(_HIDIOCGFEATURE(65), reqs)
        self.assertIsNone(bat["percentage"])
        self.assertFalse(bat["charging"])
        self.assertIn("non implémentée", bat["status_str"])

    @patch.object(SkillkorpK20Driver, "is_wireless", return_value=False)
    @patch.object(SkillkorpK20Driver, "is_connected", return_value=True)
    def test_get_battery_wired(self, mock_conn, mock_wl):
        driver = SkillkorpK20Driver()
        bat = driver.get_battery()
        self.assertEqual(bat["percentage"], 100)
        self.assertTrue(bat["charging"])
        self.assertFalse(bat["wireless"])

    @patch("skillkorp.devices.keyboard.glob.glob")
    @patch("skillkorp.devices.keyboard.os.path.exists")
    @patch("builtins.open")
    def test_find_device_distinguishes_interface_2_from_interface_1(self, mock_open_file, mock_exists, mock_glob):
        from unittest.mock import MagicMock

        mock_glob.return_value = [
            "/sys/class/hidraw/hidraw1",
            "/sys/class/hidraw/hidraw2",
        ]
        mock_exists.return_value = True

        iface1_desc = bytes([0x06, 0xFF, 0xFF, 0x09, 0x01] + [0x00] * 15)
        iface2_desc = bytes([0x06, 0xFF, 0xFF, 0x09, 0x02] + [0x00] * 15)
        uevent_content = "PRODUCT=3151/4011/1\nHID_ID=0003:00003151:00004011\n"

        def fake_open(path, mode="r", *args, **kwargs):
            m = MagicMock()
            if "report_descriptor" in str(path):
                if "hidraw1" in str(path):
                    m.read.return_value = iface1_desc
                elif "hidraw2" in str(path):
                    m.read.return_value = iface2_desc
                else:
                    m.read.return_value = b""
            elif "uevent" in str(path):
                m.read.return_value = uevent_content
            else:
                m.read.return_value = ""
            m.__enter__.return_value = m
            m.__exit__.return_value = None
            return m

        mock_open_file.side_effect = fake_open

        dev = SkillkorpK20Driver.find_device()
        self.assertEqual(dev, "/dev/hidraw2")


class TestProfileManagerKeyboard(unittest.TestCase):
    """Tests du gestionnaire multi-profils unifié, côté clavier."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.pm = ProfileManager(
            "keyboard",
            profiles_dir=os.path.join(self.temp_dir.name, "profiles"),
            state_file=os.path.join(self.temp_dir.name, "state.json"),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_profiles_created(self):
        profs = self.pm.list_profiles()
        ids = [p["id"] for p in profs]
        self.assertIn("default", ids)
        self.assertIn("gaming", ids)
        self.assertIn("bureau", ids)
        self.assertIn("eco", ids)

    def test_create_and_delete_profile(self):
        p = self.pm.create_profile("test_game", "Test Game", copy_from="gaming")
        self.assertEqual(p["id"], "test_game")
        self.assertEqual(p["name"], "Test Game")
        self.assertIn("test_game", [x["id"] for x in self.pm.list_profiles()])

        deleted = self.pm.delete_profile("test_game")
        self.assertTrue(deleted)
        self.assertNotIn("test_game", [x["id"] for x in self.pm.list_profiles()])

    def test_cannot_delete_default(self):
        with self.assertRaises(ValueError):
            self.pm.delete_profile("default")

    def test_switch_profile(self):
        self.pm.switch_profile("gaming")
        self.assertEqual(self.pm.get_active_profile_id(), "gaming")

    def test_profiles_tagged_with_device_type(self):
        for p in self.pm.list_profiles():
            self.assertEqual(p["device_type"], "keyboard")

    def test_default_profiles_dict_has_device_type(self):
        for prof_id, prof in DEFAULT_PROFILES_KEYBOARD.items():
            self.assertEqual(prof["device_type"], "keyboard")

    def test_import_rejects_invalid_debounce(self):
        import json
        path = os.path.join(self.temp_dir.name, "bad.json")
        with open(path, "w") as f:
            json.dump({"debounce_ms": 99}, f)
        with self.assertRaises(ValueError):
            self.pm.import_profile(path)


if __name__ == "__main__":
    unittest.main()
