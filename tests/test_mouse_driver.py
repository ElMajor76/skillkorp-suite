"""
Tests unitaires pour skillkorp.devices.mouse (SkillkorpM20Driver) et le
ProfileManager unifié côté souris. Porté depuis skillkorp-m20/tests/test_driver.py,
avec la même rigueur : mock de fcntl.ioctl, vérification des offsets exacts
par rapport à la description du protocole dans skillkorp-m20/README.md.
"""

import os
import json
import tempfile
import unittest
from unittest.mock import patch

import skillkorp.devices.mouse as mouse
from skillkorp.devices.mouse import (
    POLLING_RATE_MAP,
    LIGHT_MODES,
    BUTTON_ACTIONS,
    PHYSICAL_BTN_TO_SLOT,
    SkillkorpM20Driver,
    _HIDIOCSFEATURE,
)
from skillkorp.profiles.manager import ProfileManager, DEFAULT_PROFILES_MOUSE


class TestHidiocsMacro(unittest.TestCase):
    """Tests du calcul ioctl HIDIOCSFEATURE."""

    def test_hidiocsfeature_9bytes(self):
        """Report ID 0x06 polling rate: 9 bytes."""
        result = _HIDIOCSFEATURE(9)
        self.assertIsInstance(result, int)
        self.assertGreater(result, 0)
        expected = (3 << 30) | (9 << 16) | (ord('H') << 8) | 0x06
        self.assertEqual(result, expected)

    def test_hidiocsfeature_56bytes(self):
        """Report ID 0x04 DPI sensor: 56 bytes."""
        result = _HIDIOCSFEATURE(56)
        expected = (3 << 30) | (56 << 16) | (ord('H') << 8) | 0x06
        self.assertEqual(result, expected)

    def test_hidiocsfeature_15bytes(self):
        """Report ID 0x05 lighting/power: 15 bytes."""
        result = _HIDIOCSFEATURE(15)
        expected = (3 << 30) | (15 << 16) | (ord('H') << 8) | 0x06
        self.assertEqual(result, expected)


class TestConstants(unittest.TestCase):
    """Tests des constantes du driver."""

    def test_polling_rate_map_keys(self):
        self.assertEqual(set(POLLING_RATE_MAP.keys()), {125, 250, 500, 1000})

    def test_polling_rate_map_complement(self):
        """interval + not_interval doit valoir 0xFF pour chaque taux."""
        for rate, info in POLLING_RATE_MAP.items():
            self.assertEqual(
                info["interval"] + info["not_interval"], 0xFF,
                f"Complément incorrect pour {rate} Hz",
            )

    def test_light_modes_structure(self):
        ids = [m[2] for m in LIGHT_MODES]
        self.assertEqual(len(ids), len(set(ids)), "IDs de modes RGB en double")
        for mode in LIGHT_MODES:
            self.assertEqual(len(mode), 3)
            self.assertIsInstance(mode[0], str)
            self.assertIsInstance(mode[1], str)
            self.assertIsInstance(mode[2], int)

    def test_button_actions_structure(self):
        for key, val in BUTTON_ACTIONS.items():
            self.assertEqual(len(val), 4, f"Action '{key}' mal formée")
            self.assertIsInstance(val[0], str)
            for byte_val in val[1:]:
                self.assertGreaterEqual(byte_val, 0)
                self.assertLessEqual(byte_val, 0xFF)

    def test_physical_btn_to_slot_keys(self):
        self.assertEqual(set(PHYSICAL_BTN_TO_SLOT.keys()), {1, 2, 3, 4, 5, 6})

    def test_physical_btn_to_slot_slots_in_range(self):
        for btn, slot in PHYSICAL_BTN_TO_SLOT.items():
            self.assertGreaterEqual(slot, 0)
            self.assertLessEqual(slot, 17, f"Slot {slot} du bouton {btn} hors limites")

    def test_device_type(self):
        self.assertEqual(SkillkorpM20Driver.device_type, "mouse")


class TestReport0x05Checksum(unittest.TestCase):
    """Tests du buffer et checksum du Report 0x05 (éclairage + veille).

    README (skillkorp-m20) : "Mise en veille & Réveil (Report ID 0x05 - 15 octets) :
    délai avant mise en veille matérielle et mode de réveil avec somme de contrôle
    sur 16 bits." Ce test verrouille le format exact : le checksum couvre les octets
    3 à 10 (mode/luminosité-vitesse/couleur/minuteur/réveil) et est stocké en
    big-endian aux octets 11-12.
    """

    def _make_driver(self, config=None):
        default_config = {
            "light_mode": "static",
            "brightness": 8,
            "speed": 4,
            "light_color": [255, 0, 0],
            "sleep_timer_minutes": 5,
            "move_to_wake": True,
            "polling_rate": 1000,
            "dpi_stages": [800, 1600, 3200],
            "active_stage": 2,
            "lod": 1,
            "debounce": 4,
            "ripple": False,
            "angle_snap": False,
            "motion_sync": True,
            "buttons": {"1": "left_click", "2": "right_click"},
            "battery": 100,
            "charging": False,
        }
        if config:
            default_config.update(config)
        with patch.object(SkillkorpM20Driver, "_load_or_default_config", return_value=default_config):
            with patch.object(SkillkorpM20Driver, "find_device", return_value=None):
                driver = SkillkorpM20Driver.__new__(SkillkorpM20Driver)
                driver.dev_path = None
                driver.config = default_config
                driver._config_mtime = os.path.getmtime(mouse.DEFAULT_PROFILE_FILE) if os.path.exists(mouse.DEFAULT_PROFILE_FILE) else 0.0
        return driver

    def _call_lighting_report(self, driver, **kwargs):
        captured = []

        def mock_send(self, buf, retries=3):
            captured.append(bytes(buf))
            return True

        with patch.object(SkillkorpM20Driver, "_send_feature_report", mock_send):
            driver._send_lighting_and_power_report(**kwargs)

        self.assertTrue(captured, "Aucun buffer envoyé")
        return captured[0]

    def test_report_id_and_length(self):
        driver = self._make_driver()
        buf = self._call_lighting_report(
            driver, mode="static", brightness=8, speed=4,
            color=(255, 0, 0), sleep_timer_minutes=5, move_to_wake=True,
        )
        self.assertEqual(buf[0], 0x05, "Report ID incorrect")
        self.assertEqual(buf[1], 0x0F, "Longueur incorrecte (attendu 15)")

    def test_profile_index(self):
        driver = self._make_driver()
        buf = self._call_lighting_report(
            driver, mode="static", brightness=8, speed=4,
            color=(255, 0, 0), sleep_timer_minutes=5, move_to_wake=True,
        )
        self.assertEqual(buf[2], 0x01)

    def test_checksum_static_red(self):
        driver = self._make_driver()
        buf = self._call_lighting_report(
            driver, mode="static", brightness=8, speed=4,
            color=(255, 0, 0), sleep_timer_minutes=5, move_to_wake=True,
        )
        csum_expected = sum(buf[3:11]) & 0xFFFF
        csum_actual = (buf[11] << 8) | buf[12]
        self.assertEqual(csum_actual, csum_expected, f"Checksum incorrect: buf={buf.hex()}")

    def test_checksum_breathing_blue(self):
        driver = self._make_driver()
        buf = self._call_lighting_report(
            driver, mode="breathing", brightness=4, speed=6,
            color=(0, 0, 255), sleep_timer_minutes=10, move_to_wake=False,
        )
        csum_expected = sum(buf[3:11]) & 0xFFFF
        csum_actual = (buf[11] << 8) | buf[12]
        self.assertEqual(csum_actual, csum_expected)

    def test_sleep_timer_byte(self):
        driver = self._make_driver()
        for minutes in [1, 5, 30, 60]:
            buf = self._call_lighting_report(
                driver, mode="static", brightness=8, speed=4,
                color=(255, 255, 255), sleep_timer_minutes=minutes, move_to_wake=True,
            )
            self.assertEqual(buf[9], minutes, f"sleep_timer {minutes} min mal encodé")

    def test_move_to_wake_encoding(self):
        driver = self._make_driver()
        buf_move = self._call_lighting_report(
            driver, mode="static", brightness=8, speed=4,
            color=(255, 0, 0), sleep_timer_minutes=5, move_to_wake=True,
        )
        self.assertEqual(buf_move[10], 0x00, "move_to_wake=True doit encoder 0x00")

        buf_click = self._call_lighting_report(
            driver, mode="static", brightness=8, speed=4,
            color=(255, 0, 0), sleep_timer_minutes=5, move_to_wake=False,
        )
        self.assertEqual(buf_click[10], 0x01, "move_to_wake=False doit encoder 0x01")

    def test_rgb_color_bytes(self):
        driver = self._make_driver()
        buf = self._call_lighting_report(
            driver, mode="static", brightness=8, speed=4,
            color=(123, 200, 42), sleep_timer_minutes=5, move_to_wake=True,
        )
        self.assertEqual(buf[6], 123)
        self.assertEqual(buf[7], 200)
        self.assertEqual(buf[8], 42)

    def test_brightness_speed_packing(self):
        driver = self._make_driver()
        buf = self._call_lighting_report(
            driver, mode="static", brightness=5, speed=3,
            color=(0, 0, 0), sleep_timer_minutes=5, move_to_wake=True,
        )
        expected = ((5 & 0x0F) << 4) | (3 & 0x0F)
        self.assertEqual(buf[4], expected)

    def test_mode_id_static(self):
        driver = self._make_driver()
        buf = self._call_lighting_report(
            driver, mode="static", brightness=8, speed=4,
            color=(255, 0, 0), sleep_timer_minutes=5, move_to_wake=True,
        )
        self.assertEqual(buf[3], 0x20)

    def test_mode_id_breathing(self):
        driver = self._make_driver()
        buf = self._call_lighting_report(
            driver, mode="breathing", brightness=8, speed=4,
            color=(255, 0, 0), sleep_timer_minutes=5, move_to_wake=True,
        )
        self.assertEqual(buf[3], 0x30)

    def test_buffer_length(self):
        driver = self._make_driver()
        buf = self._call_lighting_report(
            driver, mode="static", brightness=8, speed=4,
            color=(255, 0, 0), sleep_timer_minutes=5, move_to_wake=True,
        )
        self.assertEqual(len(buf), 15)


class TestReport0x04DPI(unittest.TestCase):
    """Tests du buffer Report 0x04 (DPI + capteur)."""

    def _make_driver_and_capture(self, stages, active_stage=1):
        config = {
            "polling_rate": 1000,
            "dpi_stages": stages,
            "active_stage": active_stage,
            "lod": 1,
            "debounce": 4,
            "ripple": False,
            "angle_snap": False,
            "motion_sync": True,
            "light_mode": "static",
            "brightness": 8,
            "speed": 4,
            "light_color": [255, 0, 0],
            "sleep_timer_minutes": 5,
            "move_to_wake": True,
            "buttons": {},
            "battery": 100,
            "charging": False,
        }
        with patch.object(SkillkorpM20Driver, "_load_or_default_config", return_value=config):
            with patch.object(SkillkorpM20Driver, "find_device", return_value=None):
                driver = SkillkorpM20Driver.__new__(SkillkorpM20Driver)
                driver.dev_path = None
                driver.config = config.copy()
                driver._config_mtime = os.path.getmtime(mouse.DEFAULT_PROFILE_FILE) if os.path.exists(mouse.DEFAULT_PROFILE_FILE) else 0.0

        captured = []

        def mock_send(self, buf, retries=3):
            captured.append(bytes(buf))
            return True

        with patch.object(SkillkorpM20Driver, "_send_feature_report", mock_send):
            with patch.object(SkillkorpM20Driver, "_save_config"):
                driver.set_dpi_and_sensor(stages=stages, active_stage=active_stage)

        return captured[0] if captured else None

    def test_report_id_0x04(self):
        buf = self._make_driver_and_capture([800, 1600], active_stage=1)
        self.assertIsNotNone(buf)
        self.assertEqual(buf[0], 0x04)

    def test_dpi_encoding_800(self):
        buf = self._make_driver_and_capture([800], active_stage=1)
        self.assertIsNotNone(buf)
        enc = (800 // 50) - 1
        self.assertEqual(buf[8], enc & 0xFF)
        self.assertEqual(buf[16], (enc >> 8) & 0xFF)

    def test_dpi_encoding_26000(self):
        buf = self._make_driver_and_capture([26000], active_stage=1)
        self.assertIsNotNone(buf)
        enc = (26000 // 50) - 1
        self.assertEqual(buf[8], enc & 0xFF)
        self.assertEqual(buf[16], (enc >> 8) & 0xFF)

    def test_active_stage_byte(self):
        buf = self._make_driver_and_capture([400, 800, 1600], active_stage=2)
        self.assertIsNotNone(buf)
        self.assertEqual(buf[24], 2)

    def test_stages_mask(self):
        for n in range(1, 7):
            stages = [800] * n
            buf = self._make_driver_and_capture(stages, active_stage=1)
            expected_mask = (1 << n) - 1
            self.assertEqual(buf[5], expected_mask, f"Masque incorrect pour {n} étapes")


class TestBatteryInputReportParsing(unittest.TestCase):
    """README : requête 0x0C, réception de la trame 0x03 0x10 0x40 [statut] [pourcentage]."""

    def _make_driver(self):
        config = {
            "polling_rate": 1000, "dpi_stages": [800, 1600], "active_stage": 1,
            "lod": 1, "debounce": 4, "ripple": False, "angle_snap": False,
            "motion_sync": True, "light_mode": "static", "brightness": 8,
            "speed": 4, "light_color": [255, 0, 0], "sleep_timer_minutes": 5,
            "move_to_wake": True, "buttons": {}, "battery": 50, "charging": False,
        }
        with patch.object(SkillkorpM20Driver, "_load_or_default_config", return_value=config):
            with patch.object(SkillkorpM20Driver, "find_device", return_value=None):
                driver = SkillkorpM20Driver.__new__(SkillkorpM20Driver)
                driver.dev_path = None
                driver.config = config.copy()
                driver._config_mtime = 0.0
        return driver

    def test_battery_frame_0x03_0x10_0x40_charging(self):
        driver = self._make_driver()
        # 0x03 0x10 0x40 [statut=2 (charging, per query_status: charging = status == 2)] [pourcentage=77]
        frame = bytes([0x03, 0x10, 0x40, 0x02, 77])
        with patch.object(driver, "is_connected", return_value=True):
            with patch.object(driver, "_read_input_reports", return_value=[frame]):
                with patch.object(SkillkorpM20Driver, "_save_config"):
                    status = driver.query_status()
        self.assertEqual(status["battery"], 77)
        self.assertTrue(status["charging"])

    def test_battery_frame_0x03_0x10_0x40_not_charging(self):
        driver = self._make_driver()
        frame = bytes([0x03, 0x10, 0x40, 0x00, 33])
        with patch.object(driver, "is_connected", return_value=True):
            with patch.object(driver, "_read_input_reports", return_value=[frame]):
                with patch.object(SkillkorpM20Driver, "_save_config"):
                    status = driver.query_status()
        self.assertEqual(status["battery"], 33)
        self.assertFalse(status["charging"])


class TestQueryStatusClamp(unittest.TestCase):
    """Tests du clamp active_stage dans query_status."""

    def _make_driver_with_config(self, config):
        with patch.object(SkillkorpM20Driver, "_load_or_default_config", return_value=config):
            with patch.object(SkillkorpM20Driver, "find_device", return_value=None):
                driver = SkillkorpM20Driver.__new__(SkillkorpM20Driver)
                driver.dev_path = None
                driver.config = config.copy()
                driver._config_mtime = os.path.getmtime(mouse.DEFAULT_PROFILE_FILE) if os.path.exists(mouse.DEFAULT_PROFILE_FILE) else 0.0
        return driver

    def test_active_stage_clamped_when_out_of_bounds(self):
        config = {
            "polling_rate": 1000, "dpi_stages": [800, 1600], "active_stage": 5,
            "lod": 1, "debounce": 4, "ripple": False, "angle_snap": False,
            "motion_sync": True, "light_mode": "static", "brightness": 8,
            "speed": 4, "light_color": [255, 0, 0], "sleep_timer_minutes": 5,
            "move_to_wake": True, "buttons": {}, "battery": 100, "charging": False,
        }
        driver = self._make_driver_with_config(config)
        with patch.object(driver, "is_connected", return_value=False):
            with patch.object(driver, "_read_input_reports", return_value=[]):
                status = driver.query_status()
                self.assertIsNotNone(status)
                self.assertIn(status["active_dpi"], config["dpi_stages"])

    def test_active_stage_1_is_valid(self):
        config = {
            "polling_rate": 1000, "dpi_stages": [400, 800], "active_stage": 1,
            "lod": 1, "debounce": 4, "ripple": False, "angle_snap": False,
            "motion_sync": True, "light_mode": "static", "brightness": 8,
            "speed": 4, "light_color": [255, 0, 0], "sleep_timer_minutes": 5,
            "move_to_wake": True, "buttons": {}, "battery": 100, "charging": False,
        }
        driver = self._make_driver_with_config(config)
        with patch.object(driver, "is_connected", return_value=False):
            with patch.object(driver, "_read_input_reports", return_value=[]):
                status = driver.query_status()
                self.assertEqual(status["active_dpi"], 400)


class TestSetButtonsSlotMapping(unittest.TestCase):
    """Tests du mappage boutons physiques -> slots firmware (Report 0x08)."""

    def _make_driver(self):
        config = {
            "polling_rate": 1000, "dpi_stages": [800], "active_stage": 1,
            "lod": 1, "debounce": 4, "ripple": False, "angle_snap": False,
            "motion_sync": True, "light_mode": "static", "brightness": 8,
            "speed": 4, "light_color": [255, 0, 0], "sleep_timer_minutes": 5,
            "move_to_wake": True, "buttons": {}, "battery": 100, "charging": False,
        }
        with patch.object(SkillkorpM20Driver, "_load_or_default_config", return_value=config):
            with patch.object(SkillkorpM20Driver, "find_device", return_value=None):
                driver = SkillkorpM20Driver.__new__(SkillkorpM20Driver)
                driver.dev_path = None
                driver.config = config.copy()
                driver._config_mtime = os.path.getmtime(mouse.DEFAULT_PROFILE_FILE) if os.path.exists(mouse.DEFAULT_PROFILE_FILE) else 0.0
        return driver

    def test_set_buttons_report_id(self):
        driver = self._make_driver()
        captured = []

        def mock_send(self, buf, retries=3):
            captured.append(bytes(buf))
            return True

        with patch.object(SkillkorpM20Driver, "_send_feature_report", mock_send):
            with patch.object(SkillkorpM20Driver, "_save_config"):
                driver.set_buttons({1: "left_click", 2: "right_click"})

        self.assertTrue(captured)
        self.assertEqual(captured[0][0], 0x08)

    def test_physical_btn_slot_mapping(self):
        driver = self._make_driver()
        captured = []

        def mock_send(self, buf, retries=3):
            captured.append(bytes(buf))
            return True

        with patch.object(SkillkorpM20Driver, "_send_feature_report", mock_send):
            with patch.object(SkillkorpM20Driver, "_save_config"):
                driver.set_buttons({1: "left_click", 2: "right_click", 6: "dpi_cycle"})

        self.assertTrue(captured)
        buf = captured[0]
        dpi_code = BUTTON_ACTIONS["dpi_cycle"][1]
        slot3_offset = 3 + 3 * 3  # = 12
        self.assertEqual(buf[slot3_offset], dpi_code, f"Bouton 6 (Slot 3) devrait avoir code {dpi_code:#04x}, buf={buf.hex()}")


class TestApplyProfile(unittest.TestCase):
    """apply_profile() est le nouveau point d'entrée unifié appelé par ProfileManager."""

    def _make_driver(self):
        config = {
            "polling_rate": 1000, "dpi_stages": [800, 1600], "active_stage": 1,
            "lod": 1, "debounce": 4, "ripple": False, "angle_snap": False,
            "motion_sync": True, "light_mode": "static", "brightness": 8,
            "speed": 4, "light_color": [255, 0, 0], "sleep_timer_minutes": 5,
            "move_to_wake": True, "buttons": {}, "battery": 100, "charging": False,
        }
        with patch.object(SkillkorpM20Driver, "_load_or_default_config", return_value=config):
            with patch.object(SkillkorpM20Driver, "find_device", return_value=None):
                driver = SkillkorpM20Driver.__new__(SkillkorpM20Driver)
                driver.dev_path = None
                driver.config = config.copy()
                driver._config_mtime = 0.0
        return driver

    def test_apply_profile_calls_all_setters(self):
        driver = self._make_driver()
        with patch.object(driver, "set_dpi_and_sensor") as m_dpi, \
             patch.object(driver, "set_polling_rate") as m_rate, \
             patch.object(driver, "set_rgb_lighting") as m_rgb, \
             patch.object(driver, "set_buttons") as m_btn, \
             patch.object(driver, "set_power_settings") as m_power:
            profile = DEFAULT_PROFILES_MOUSE["gaming_fps"]
            success = driver.apply_profile(profile)
            self.assertTrue(success)
            m_dpi.assert_called_once()
            m_rate.assert_called_once_with(profile["polling_rate"])
            m_rgb.assert_called_once()
            m_btn.assert_called_once()
            m_power.assert_called_once()


class TestProfileManagerMouse(unittest.TestCase):
    """Tests du gestionnaire multi-profils unifié, côté souris."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.pm = ProfileManager(
            "mouse",
            profiles_dir=os.path.join(self.tmpdir, "profiles"),
            state_file=os.path.join(self.tmpdir, "state.json"),
        )

    def _write_profile_file(self, data):
        path = os.path.join(self.tmpdir, "test_import.json")
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def test_default_profiles_created(self):
        ids = [p["id"] for p in self.pm.list_profiles()]
        self.assertIn("default", ids)
        self.assertIn("gaming_fps", ids)
        self.assertIn("office_economy", ids)

    def test_profiles_tagged_with_device_type(self):
        for p in self.pm.list_profiles():
            self.assertEqual(p["device_type"], "mouse")

    def test_valid_profile_imports_ok(self):
        data = {
            "id": "test", "name": "Test", "dpi_stages": [800, 1600],
            "active_stage": 1, "polling_rate": 1000,
            "buttons": {"1": "left_click", "2": "right_click"},
        }
        path = self._write_profile_file(data)
        result_id = self.pm.import_profile(path)
        self.assertEqual(result_id, "test")

    def test_invalid_dpi_raises(self):
        data = {"dpi_stages": [100000], "buttons": {}}
        path = self._write_profile_file(data)
        with self.assertRaises(ValueError):
            self.pm.import_profile(path)

    def test_invalid_polling_rate_raises(self):
        data = {"polling_rate": 750, "buttons": {}}
        path = self._write_profile_file(data)
        with self.assertRaises(ValueError):
            self.pm.import_profile(path)

    def test_invalid_button_action_raises(self):
        data = {"buttons": {"1": "teleport"}}
        path = self._write_profile_file(data)
        with self.assertRaises(ValueError):
            self.pm.import_profile(path)

    def test_invalid_button_number_raises(self):
        data = {"buttons": {"9": "left_click"}}
        path = self._write_profile_file(data)
        with self.assertRaises(ValueError):
            self.pm.import_profile(path)

    def test_invalid_active_stage_raises(self):
        data = {"dpi_stages": [800, 1600], "active_stage": 5, "buttons": {}}
        path = self._write_profile_file(data)
        with self.assertRaises(ValueError):
            self.pm.import_profile(path)

    def test_dpi_too_many_stages_raises(self):
        data = {"dpi_stages": [800] * 9, "buttons": {}}
        path = self._write_profile_file(data)
        with self.assertRaises(ValueError):
            self.pm.import_profile(path)


class TestDefaultProfilesHaveRGBFields(unittest.TestCase):
    def test_all_defaults_have_light_fields(self):
        required = {"light_mode", "brightness", "speed", "light_color"}
        for prof_id, prof in DEFAULT_PROFILES_MOUSE.items():
            for field in required:
                self.assertIn(field, prof, f"DEFAULT_PROFILES_MOUSE['{prof_id}'] manque le champ '{field}'")

    def test_light_color_is_rgb_list(self):
        for prof_id, prof in DEFAULT_PROFILES_MOUSE.items():
            color = prof.get("light_color")
            self.assertIsInstance(color, list, f"light_color de '{prof_id}' n'est pas une liste")
            self.assertEqual(len(color), 3, f"light_color de '{prof_id}' n'a pas 3 composantes")
            for c in color:
                self.assertGreaterEqual(c, 0)
                self.assertLessEqual(c, 255)


class TestReloadConfigIfChanged(unittest.TestCase):
    """Tests du rechargement conditionnel de configuration multi-processus."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.temp_dir.name, "default.json")
        self.initial_config = {
            "polling_rate": 500, "dpi_stages": [800, 1600], "active_stage": 1,
            "light_mode": "static", "brightness": 5, "speed": 3,
            "light_color": [255, 0, 0], "sleep_timer_minutes": 5, "move_to_wake": True,
            "buttons": {"1": "left_click"}, "battery": 80, "charging": False,
        }
        with open(self.config_path, "w") as f:
            json.dump(self.initial_config, f)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_reload_when_file_modified(self):
        with patch("skillkorp.devices.mouse.DEFAULT_PROFILE_FILE", self.config_path):
            with patch.object(SkillkorpM20Driver, "find_device", return_value=None):
                driver = SkillkorpM20Driver()
                self.assertEqual(driver.config.get("polling_rate"), 500)
                self.assertFalse(driver.reload_config_if_changed())

                updated_config = self.initial_config.copy()
                updated_config["polling_rate"] = 1000
                with open(self.config_path, "w") as f:
                    json.dump(updated_config, f)
                new_mtime = os.path.getmtime(self.config_path) + 10.0
                os.utime(self.config_path, (new_mtime, new_mtime))

                reloaded = driver.reload_config_if_changed()
                self.assertTrue(reloaded)
                self.assertEqual(driver.config.get("polling_rate"), 1000)

    def test_reload_when_file_unchanged(self):
        with patch("skillkorp.devices.mouse.DEFAULT_PROFILE_FILE", self.config_path):
            with patch.object(SkillkorpM20Driver, "find_device", return_value=None):
                driver = SkillkorpM20Driver()
                self.assertFalse(driver.reload_config_if_changed())

    def test_reload_when_file_missing(self):
        missing_path = os.path.join(self.temp_dir.name, "nonexistent.json")
        with patch("skillkorp.devices.mouse.DEFAULT_PROFILE_FILE", missing_path):
            with patch.object(SkillkorpM20Driver, "find_device", return_value=None):
                driver = SkillkorpM20Driver()
                self.assertFalse(driver.reload_config_if_changed())

    def test_query_status_triggers_reload(self):
        with patch("skillkorp.devices.mouse.DEFAULT_PROFILE_FILE", self.config_path):
            with patch.object(SkillkorpM20Driver, "find_device", return_value=None):
                driver = SkillkorpM20Driver()
                with patch.object(driver, "is_connected", return_value=False):
                    status = driver.query_status()
                    self.assertEqual(status["polling_rate_hz"], 500)

                    updated_config = self.initial_config.copy()
                    updated_config["polling_rate"] = 1000
                    with open(self.config_path, "w") as f:
                        json.dump(updated_config, f)
                    new_mtime = os.path.getmtime(self.config_path) + 10.0
                    os.utime(self.config_path, (new_mtime, new_mtime))

                    status = driver.query_status()
                    self.assertEqual(status["polling_rate_hz"], 1000)


if __name__ == "__main__":
    unittest.main()
