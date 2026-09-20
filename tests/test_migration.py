"""
Tests unitaires pour skillkorp.profiles.migration - migration ponctuelle des
anciens dossiers ~/.config/skillkorp-k20 et ~/.config/skillkorp-m20 vers le
nouvel emplacement unifié ~/.config/skillkorp/{keyboard,mouse}.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import skillkorp.profiles.migration as migration


class TestMigrateLegacyConfigs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = os.path.join(self.tmp.name, "skillkorp")
        self.legacy_k20 = os.path.join(self.tmp.name, "skillkorp-k20")
        self.legacy_m20 = os.path.join(self.tmp.name, "skillkorp-m20")

        self._patches = [
            patch.object(migration, "CONFIG_DIR", self.config_dir),
            patch.object(migration, "MARKER_FILE", os.path.join(self.config_dir, ".migrated")),
            patch.object(migration, "_LEGACY_SOURCES", {"keyboard": self.legacy_k20, "mouse": self.legacy_m20}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _write_legacy_keyboard_config(self):
        os.makedirs(os.path.join(self.legacy_k20, "profiles"), exist_ok=True)
        with open(os.path.join(self.legacy_k20, "default_profile.json"), "w") as f:
            json.dump({"polling_rate": 500}, f)
        with open(os.path.join(self.legacy_k20, "profiles", "gaming.json"), "w") as f:
            json.dump({"id": "gaming", "polling_rate": 1000}, f)
        with open(os.path.join(self.legacy_k20, "profile_state.json"), "w") as f:
            json.dump({"active_profile": "gaming", "auto_switch_enabled": True}, f)

    def test_no_legacy_dirs_is_a_noop(self):
        result = migration.migrate_legacy_configs()
        self.assertEqual(result, {})
        self.assertTrue(os.path.exists(migration.MARKER_FILE))

    def test_migrates_keyboard_files(self):
        self._write_legacy_keyboard_config()
        result = migration.migrate_legacy_configs()

        self.assertIn("keyboard", result)
        self.assertNotIn("mouse", result)

        new_default = os.path.join(self.config_dir, "keyboard", "default_profile.json")
        self.assertTrue(os.path.exists(new_default))
        with open(new_default) as f:
            self.assertEqual(json.load(f)["polling_rate"], 500)

        new_profile = os.path.join(self.config_dir, "keyboard", "profiles", "gaming.json")
        self.assertTrue(os.path.exists(new_profile))

        new_state = os.path.join(self.config_dir, "keyboard", "profile_state.json")
        self.assertTrue(os.path.exists(new_state))

        # Legacy directory must be left untouched (never deleted by migration)
        self.assertTrue(os.path.exists(self.legacy_k20))
        self.assertTrue(os.path.exists(os.path.join(self.legacy_k20, "default_profile.json")))

    def test_runs_only_once(self):
        self._write_legacy_keyboard_config()
        first = migration.migrate_legacy_configs()
        self.assertIn("keyboard", first)

        # Modify the legacy file after the first migration; a second run must not re-copy it
        with open(os.path.join(self.legacy_k20, "default_profile.json"), "w") as f:
            json.dump({"polling_rate": 125}, f)

        second = migration.migrate_legacy_configs()
        self.assertEqual(second, {})

        new_default = os.path.join(self.config_dir, "keyboard", "default_profile.json")
        with open(new_default) as f:
            # Still the value from the first migration, not overwritten
            self.assertEqual(json.load(f)["polling_rate"], 500)

    def test_does_not_overwrite_existing_unified_config(self):
        self._write_legacy_keyboard_config()
        os.makedirs(os.path.join(self.config_dir, "keyboard"), exist_ok=True)
        new_default = os.path.join(self.config_dir, "keyboard", "default_profile.json")
        with open(new_default, "w") as f:
            json.dump({"polling_rate": 250}, f)

        migration.migrate_legacy_configs()

        with open(new_default) as f:
            self.assertEqual(json.load(f)["polling_rate"], 250)


if __name__ == "__main__":
    unittest.main()
