#!/usr/bin/env python3
"""
SkillKorp Suite - One-Time Legacy Configuration Migration

On first launch (from the CLI, GUI, or tray - whichever runs first), if the
old standalone-project config directories exist:
    ~/.config/skillkorp-k20/   (keyboard: default_profile.json, profiles/, profile_state.json)
    ~/.config/skillkorp-m20/   (mouse:    default_profile.json, profiles/, profile_state.json)
...their contents are copied into the new unified layout:
    ~/.config/skillkorp/keyboard/
    ~/.config/skillkorp/mouse/

The old directories are left untouched (the user removes them manually once
satisfied). Migration runs at most once: a marker file records that it has
already been attempted, successfully or not, so every subsequent launch is a
cheap no-op.
"""

import os
import shutil
import sys
from datetime import datetime, timezone
from typing import Dict, List

CONFIG_DIR = os.path.expanduser("~/.config/skillkorp")
MARKER_FILE = os.path.join(CONFIG_DIR, ".migrated")

_LEGACY_SOURCES = {
    "keyboard": os.path.expanduser("~/.config/skillkorp-k20"),
    "mouse": os.path.expanduser("~/.config/skillkorp-m20"),
}


def _migrate_one(device_type: str, legacy_dir: str) -> List[str]:
    """Copy one legacy config directory's contents into its unified location.
    Returns the list of relative paths that were actually copied."""
    new_dir = os.path.join(CONFIG_DIR, device_type)
    copied: List[str] = []

    if not os.path.isdir(legacy_dir):
        return copied

    os.makedirs(new_dir, exist_ok=True)

    # 1. Top-level default_profile.json
    legacy_default = os.path.join(legacy_dir, "default_profile.json")
    new_default = os.path.join(new_dir, "default_profile.json")
    if os.path.isfile(legacy_default) and not os.path.exists(new_default):
        shutil.copy2(legacy_default, new_default)
        copied.append("default_profile.json")

    # 2. profiles/*.json
    legacy_profiles = os.path.join(legacy_dir, "profiles")
    new_profiles = os.path.join(new_dir, "profiles")
    if os.path.isdir(legacy_profiles):
        os.makedirs(new_profiles, exist_ok=True)
        for fname in sorted(os.listdir(legacy_profiles)):
            if not fname.endswith(".json"):
                continue
            src = os.path.join(legacy_profiles, fname)
            dst = os.path.join(new_profiles, fname)
            if os.path.isfile(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)
                copied.append(f"profiles/{fname}")

    # 3. profile_state.json (active profile & auto-switch flag)
    legacy_state = os.path.join(legacy_dir, "profile_state.json")
    new_state = os.path.join(new_dir, "profile_state.json")
    if os.path.isfile(legacy_state) and not os.path.exists(new_state):
        shutil.copy2(legacy_state, new_state)
        copied.append("profile_state.json")

    return copied


def migrate_legacy_configs(force: bool = False) -> Dict[str, List[str]]:
    """Run the one-time migration if it has not already been performed.

    Returns a dict of {device_type: [migrated relative paths]}, empty for a
    device type with nothing to migrate, and entirely empty if migration had
    already run (unless force=True).
    """
    if os.path.exists(MARKER_FILE) and not force:
        return {}

    os.makedirs(CONFIG_DIR, exist_ok=True)
    results: Dict[str, List[str]] = {}
    for device_type, legacy_dir in _LEGACY_SOURCES.items():
        migrated = _migrate_one(device_type, legacy_dir)
        if migrated:
            results[device_type] = migrated

    with open(MARKER_FILE, "w", encoding="utf-8") as f:
        f.write(f"migrated_at={datetime.now(timezone.utc).isoformat()}\n")
        for device_type, files in results.items():
            f.write(f"{device_type}: {len(files)} fichier(s)\n")

    if results:
        print("[SkillKorp Suite] Migration de la configuration existante :", file=sys.stderr)
        for device_type, files in results.items():
            legacy_dir = _LEGACY_SOURCES[device_type]
            print(f"  • {device_type} : {len(files)} fichier(s) importé(s) depuis {legacy_dir}", file=sys.stderr)
            for f_rel in files:
                print(f"      - {f_rel}", file=sys.stderr)
        print(
            "  (Les anciens dossiers ne sont pas supprimés automatiquement ; "
            "vous pouvez les effacer vous-même une fois satisfait.)",
            file=sys.stderr,
        )

    return results
