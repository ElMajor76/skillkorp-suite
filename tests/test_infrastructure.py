"""
Tests de la couche hidraw (parsing sysfs/descripteurs, dimensionnement des
rapports), de la persistance atomique, du worker et du gestionnaire de profils.
"""

import json
import os
import tempfile
import threading
import time
import unittest

from skillkorp import config_store
from skillkorp.devices import hidraw
from skillkorp.profiles.manager import (
    DEFAULT_PROFILES_KEYBOARD, DEFAULT_PROFILES_MOUSE, AutoSwitcher, ProfileManager, find_matching_profile,
    sanitize_profile_id, validate_profile,
)
from skillkorp.worker import DeviceWorker
from tests.fakes import K20_VENDOR_DESCRIPTOR, M20_VENDOR_DESCRIPTOR, isolated_home, make_node


class TestDescriptorParsing(unittest.TestCase):
    def test_keyboard_vendor_interface(self):
        info = hidraw.parse_report_descriptor(K20_VENDOR_DESCRIPTOR)
        self.assertEqual(info.feature, {0: 64})
        self.assertIn(0xFFFF, info.usage_pages)
        self.assertFalse(info.uses_report_ids)

    def test_mouse_vendor_interface(self):
        info = hidraw.parse_report_descriptor(M20_VENDOR_DESCRIPTOR)
        self.assertEqual((info.feature[4], info.feature[5], info.feature[6], info.feature[8], info.feature[12]),
                         (51, 12, 8, 58, 5))
        self.assertEqual(info.input[3], 4)
        self.assertIn(0x0B, info.usage_pages)

    def test_garbage_descriptor_does_not_crash(self):
        hidraw.parse_report_descriptor(b"\xfe")
        hidraw.parse_report_descriptor(bytes(range(256)))

    def test_hid_id(self):
        self.assertEqual(hidraw.parse_hid_id("0003:00003151:00004011"), (3, 0x3151, 0x4011))
        with self.assertRaises(ValueError):
            hidraw.parse_hid_id("garbage")

    def test_fit_feature(self):
        dev = hidraw.HidrawDevice(make_node(0x1D57, 0xFA60, M20_VENDOR_DESCRIPTOR))
        self.assertEqual(len(dev.fit_feature(bytes([4]) + bytes(55))), 52)
        self.assertEqual(len(dev.fit_feature(bytes([5, 1]))), 13)
        # never drop non-zero bytes
        self.assertEqual(len(dev.fit_feature(bytes([6]) + bytes(8) + b"\x01")), 10)
        dev.exact_feature_sizes = False
        self.assertEqual(len(dev.fit_feature(bytes([4]) + bytes(55))), 56)


class TestSysfsEnumeration(unittest.TestCase):
    def _make(self, root, name, hid_id, desc, minor):
        dev_dir = os.path.join(root, "devices", name, "0003:XXXX")
        os.makedirs(dev_dir)
        with open(os.path.join(dev_dir, "uevent"), "w") as f:
            f.write(f"HID_ID={hid_id}\nHID_NAME=test\n")
        with open(os.path.join(dev_dir, "report_descriptor"), "wb") as f:
            f.write(desc)
        with open(os.path.join(root, "devices", name, "bInterfaceNumber"), "w") as f:
            f.write("02\n")
        cls_dir = os.path.join(root, "class", name)
        os.makedirs(cls_dir)
        os.symlink(dev_dir, os.path.join(cls_dir, "device"))
        with open(os.path.join(cls_dir, "dev"), "w") as f:
            f.write(f"238:{minor}\n")

    def test_natural_sort_and_filters(self):
        with tempfile.TemporaryDirectory() as root:
            self._make(root, "hidraw10", "0003:00003151:00004011", K20_VENDOR_DESCRIPTOR, 10)
            self._make(root, "hidraw2", "0003:00001D57:0000FA60", M20_VENDOR_DESCRIPTOR, 2)
            self._make(root, "hidraw9", "0003:0000046D:00000ACB", b"", 9)
            nodes = hidraw.list_hidraw_nodes(os.path.join(root, "class"), "/dev")
            self.assertEqual([n.name for n in nodes], ["hidraw2", "hidraw9", "hidraw10"])
            kb = hidraw.find_nodes(0x3151, (0x4011,), sysfs_root=os.path.join(root, "class"))
            self.assertEqual(kb[0].devnode, "/dev/hidraw10")
            self.assertEqual(kb[0].interface, 2)
            self.assertEqual(kb[0].dev_numbers, "238:10")


class TestConfigStore(unittest.TestCase):
    def test_atomic_save_and_quarantine(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.json")
            config_store.save_json_atomic(path, {"x": 1})
            self.assertEqual(config_store.load_json(path, dict), {"x": 1})
            self.assertEqual([n for n in os.listdir(d) if n.endswith(".tmp")], [])
            with open(path, "w") as f:
                f.write("[1, 2")
            self.assertEqual(config_store.load_json(path, lambda: {"d": 1}), {"d": 1})
            self.assertTrue(any(".corrupt-" in n for n in os.listdir(d)))
            self.assertFalse(os.path.exists(path))

    def test_wrong_type_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.json")
            with open(path, "w") as f:
                json.dump([1, 2], f)
            self.assertEqual(config_store.load_json(path, dict), {})

    def test_lock_serializes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.json")
            order = []

            def worker(tag):
                with config_store.locked(path):
                    order.append(tag + "-in")
                    time.sleep(0.05)
                    order.append(tag + "-out")
            threads = [threading.Thread(target=worker, args=(t,)) for t in "ab"]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertIn(order, (["a-in", "a-out", "b-in", "b-out"], ["b-in", "b-out", "a-in", "a-out"]))

    def test_deep_merge(self):
        self.assertEqual(config_store.deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 3}}), {"a": {"b": 3, "c": 2}})


class TestWorker(unittest.TestCase):
    def test_coalescing_and_callbacks(self):
        results, errors = [], []
        gate = threading.Event()
        w = DeviceWorker("t", lambda f: f())
        w.submit(lambda: gate.wait(2))
        for i in range(5):
            w.submit(lambda i=i: i, results.append, key="slider")
        w.submit(lambda: 1 / 0, None, errors.append)
        gate.set()
        deadline = time.time() + 2
        while (not w.is_idle() or len(errors) < 1) and time.time() < deadline:
            time.sleep(0.01)
        w.stop()
        self.assertEqual(results, [4])
        self.assertIsInstance(errors[0], ZeroDivisionError)


class TestProfiles(unittest.TestCase):
    def setUp(self):
        self._home = isolated_home()
        self._home.__enter__()

    def tearDown(self):
        self._home.__exit__(None, None, None)

    def test_builtin_profiles_are_valid(self):
        for p in DEFAULT_PROFILES_KEYBOARD.values():
            self.assertEqual(validate_profile("keyboard", p), [])
        for p in DEFAULT_PROFILES_MOUSE.values():
            self.assertEqual(validate_profile("mouse", p), [])

    def test_validation_catches_errors(self):
        self.assertTrue(validate_profile("mouse", {"dpi_stages": [10]}))
        self.assertTrue(validate_profile("mouse", {"buttons": {"9": "left_click"}}))
        self.assertTrue(validate_profile("mouse", {"light_mode": "laser"}))
        self.assertTrue(validate_profile("keyboard", {"rgb": {"mode": "wave", "speed": 9}}))
        self.assertTrue(validate_profile("keyboard", {"key_remaps": {"F1": "nope"}}))
        self.assertTrue(validate_profile("keyboard", {"device_type": "mouse"}))
        self.assertTrue(validate_profile("keyboard", {"polling_rate": 2000}))

    def test_import_rejects_invalid_and_wrong_device(self):
        pm = ProfileManager("keyboard")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(dict(DEFAULT_PROFILES_MOUSE["default"]), f)
        try:
            with self.assertRaises(ValueError):
                pm.import_profile(f.name)
        finally:
            os.unlink(f.name)

    def test_import_upgrades_old_mouse_profile(self):
        pm = ProfileManager("mouse")
        old = dict(DEFAULT_PROFILES_MOUSE["default"])
        old.pop("led_off_minutes")
        old["move_to_wake"] = False
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(old, f)
        try:
            pid = pm.import_profile(f.name, "Mon Profil!")
        finally:
            os.unlink(f.name)
        self.assertEqual(pid, "mon_profil_")
        self.assertNotIn("move_to_wake", pm.get_profile(pid))

    def test_create_duplicate_and_ids(self):
        pm = ProfileManager("keyboard")
        pm.create_profile("Jeu", "Jeu")
        with self.assertRaises(ValueError):
            pm.create_profile("jeu", "Jeu bis")
        with self.assertRaises(ValueError):
            sanitize_profile_id("///")

    def test_corrupt_builtin_profile_self_heals(self):
        pm = ProfileManager("keyboard")
        with open(pm._get_profile_path("default"), "w") as f:
            f.write("{oops")
        self.assertEqual(pm.get_profile("default")["name"], "Par défaut")

    def test_delete_active_falls_back_to_default(self):
        pm = ProfileManager("mouse")
        pm.create_profile("tmp", "tmp")
        pm.switch_profile("tmp")
        self.assertTrue(pm.delete_profile("tmp"))
        self.assertEqual(pm.get_active_profile_id(), "default")

    def test_find_matching_profile_longest_wins(self):
        profiles = [{"id": "a", "auto_switch_apps": ["steam"]}, {"id": "b", "auto_switch_apps": ["steam_app_730"]},
                    {"id": "c", "auto_switch_apps": [""]}]
        self.assertEqual(find_matching_profile(profiles, "steam_app_730"), "b")
        self.assertEqual(find_matching_profile(profiles, "steamwebhelper"), "a")
        self.assertIsNone(find_matching_profile(profiles, "gedit"))

    def test_autoswitcher_restores_previous_profile(self):
        pm = ProfileManager("mouse")
        applied = []

        class Drv:
            def apply_profile(self, prof):
                applied.append(prof["id"])
                return True
        windows = iter(["cs2", "cs2", "gedit", "code", "nautilus"])
        sw = AutoSwitcher([(pm, Drv())], detect=lambda: next(windows))
        sw.tick()
        self.assertEqual(pm.get_active_profile_id(), "gaming_fps")
        sw.tick()  # same window: nothing
        sw.tick()  # unrelated window: back to the previous profile
        self.assertEqual(pm.get_active_profile_id(), "default")
        sw.tick()
        self.assertEqual(pm.get_active_profile_id(), "office_economy")
        pm.switch_profile("default")  # manual change becomes the new baseline
        sw.tick()
        self.assertEqual(pm.get_active_profile_id(), "default")
        self.assertEqual(applied, ["gaming_fps", "default", "office_economy"])


if __name__ == "__main__":
    unittest.main()


class TestAutostartAndTray(unittest.TestCase):
    def setUp(self):
        import contextlib
        self._stack = contextlib.ExitStack()
        self.tmp = self._stack.enter_context(isolated_home())
        import skillkorp.profiles.manager as pm
        self.pm = pm
        self.system_entry = pm.system_autostart_files()[0]

    def tearDown(self):
        self._stack.close()

    def install_system_entry(self):
        os.makedirs(os.path.dirname(self.system_entry), exist_ok=True)
        with open(self.system_entry, "w") as f:
            f.write(self.pm.AUTOSTART_ENTRY)

    def test_packaged_entry_enables_autostart_and_user_can_mask_it(self):
        pm = self.pm
        self.assertFalse(pm.is_autostart_enabled())
        self.install_system_entry()
        self.assertTrue(pm.is_autostart_enabled())
        pm.set_autostart(False)
        self.assertFalse(pm.is_autostart_enabled())
        with open(pm.AUTOSTART_FILE) as f:
            self.assertIn("Hidden=true", f.read())
        pm.set_autostart(True)
        self.assertTrue(pm.is_autostart_enabled())

    def test_without_packaged_entry_disable_removes_user_entry(self):
        pm = self.pm
        pm.set_autostart(True)
        self.assertTrue(os.path.exists(pm.AUTOSTART_FILE))
        pm.set_autostart(False)
        self.assertFalse(os.path.exists(pm.AUTOSTART_FILE))
        self.assertFalse(pm.is_autostart_enabled())

    def test_tray_running_detection_uses_its_lock(self):
        import fcntl
        from unittest.mock import patch
        from skillkorp import traylaunch
        self._stack.enter_context(patch.object(traylaunch, "pid_file",
                                               lambda: os.path.join(self.tmp, "run", "skillkorp-tray.pid")))
        self.assertFalse(traylaunch.is_tray_running())
        os.makedirs(os.path.dirname(traylaunch.pid_file()), exist_ok=True)
        fd = os.open(traylaunch.pid_file(), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            self.assertFalse(traylaunch.is_tray_running())  # stale file, no lock
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(traylaunch.is_tray_running())
        finally:
            os.close(fd)

    def test_launch_tray(self):
        from unittest.mock import patch
        from skillkorp import traylaunch
        with patch.object(traylaunch.subprocess, "Popen") as popen, \
                patch.object(traylaunch, "is_tray_running", lambda: False):
            self.assertFalse(traylaunch.launch_tray())  # SKILLKORP_NO_TRAY=1 in tests
            with patch.dict(os.environ, {"SKILLKORP_NO_TRAY": "0"}):
                self.assertTrue(traylaunch.launch_tray())
                with patch.object(traylaunch, "is_tray_running", lambda: True):
                    self.assertFalse(traylaunch.launch_tray())
        self.assertEqual(popen.call_count, 1)
        self.assertIn("skillkorp.tray", popen.call_args[0][0][-1])
        self.assertTrue(popen.call_args[1]["start_new_session"])

    def test_packaged_autostart_entry_matches_the_user_one(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "packaging", "common", "io.github.skillkorp.suite.tray.desktop")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), self.pm.AUTOSTART_ENTRY)


class TestGnomeFocusExtension(unittest.TestCase):
    """Client side of gnome-extension/skillkorp-focus@skillkorp.github.io."""

    def setUp(self):
        import skillkorp.profiles.manager as pm
        from unittest.mock import patch
        self.pm = pm
        self.tmp = tempfile.mkdtemp()
        self.calls = []
        self.focus = None          # gdbus answer of GetFocused, None = extension not running
        self.enabled = []          # org.gnome.shell enabled-extensions
        self.shell_knows = True    # EnableExtension succeeds

        def run(cmd, timeout=0.4):
            self.calls.append(cmd)
            if cmd[:2] == ["gdbus", "call"] and "io.github.skillkorp.Focus.GetFocused" in cmd:
                return self.focus
            if "org.gnome.Shell.Extensions.EnableExtension" in cmd:
                if self.shell_knows:
                    self.focus = "('', '')"
                return "(true,)" if self.shell_knows else "(false,)"
            if cmd[:3] == ["gsettings", "get", "org.gnome.shell"]:
                return "[" + ", ".join(f"'{e}'" for e in self.enabled) + "]" if self.enabled else "@as []"
            if cmd[:3] == ["gsettings", "set", "org.gnome.shell"]:
                import re
                self.enabled = re.findall(r"'([^']+)'", cmd[4])
                return ""
            return None
        self._patches = [patch.object(pm, "_run", run),
                         patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME", "XDG_SESSION_TYPE": "wayland",
                                                 "XDG_DATA_HOME": self.tmp, "XDG_DATA_DIRS": self.tmp})]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install(self):
        os.makedirs(os.path.join(self.tmp, "gnome-shell", "extensions", self.pm.GNOME_FOCUS_EXTENSION))

    def test_focused_application_parsed(self):
        self.focus = "('org.gnome.Calculator', 'org.gnome.Calculator')"
        self.assertEqual(self.pm.detect_active_window_class(), "org.gnome.calculator")
        self.focus = "('', 'org.mozilla.firefox')"
        self.assertEqual(self.pm.detect_active_window_class(), "org.mozilla.firefox")

    def test_status_and_enabling_in_a_running_shell(self):
        self.assertEqual(self.pm.gnome_focus_extension_status(), "missing")
        self.install()
        self.assertEqual(self.pm.gnome_focus_extension_status(), "disabled")
        self.assertEqual(self.pm.enable_gnome_focus_extension(), "active")

    def test_installed_after_login_is_enabled_for_next_login(self):
        self.install()
        self.shell_knows = False
        self.enabled = ["other@ext"]
        self.assertEqual(self.pm.enable_gnome_focus_extension(), "inactive")
        self.assertEqual(self.enabled, ["other@ext", self.pm.GNOME_FOCUS_EXTENSION])

    def test_outside_gnome_nothing_happens(self):
        with __import__("unittest.mock").mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}):
            self.assertIsNone(self.pm.enable_gnome_focus_extension())
        self.assertFalse(any("gsettings" in c for c in self.calls))
