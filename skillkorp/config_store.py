#!/usr/bin/env python3
"""
SkillKorp Suite - crash-safe JSON persistence shared by drivers and profiles.

The CLI, GUI, tray and daemon may all run at the same time and write the same
files. Guarantees provided here:

- writes are atomic (unique temp file + fsync + rename), so a crash or a full
  disk never leaves a half-written JSON file behind;
- read-modify-write sequences can be serialized across processes with
  `locked(path)` (flock on a sibling ".lock" file);
- a corrupt file is moved aside (``*.corrupt-<timestamp>``) instead of being
  silently overwritten with defaults, so the user can still recover it.
"""

import contextlib
import fcntl
import json
import os
import sys
import tempfile
import time
from typing import Any, Callable, Iterator, Optional


def _log(msg: str):
    print(f"[skillkorp] {msg}", file=sys.stderr)


def save_json_atomic(path: str, data: Any, indent: int = 2):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    with contextlib.suppress(OSError):
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def quarantine(path: str) -> Optional[str]:
    """Move an unreadable file aside and return its new name."""
    target = f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(path, target)
        return target
    except OSError:
        return None


def load_json(path: str, default_factory: Callable[[], Any], expect_type: type = dict) -> Any:
    """Load JSON from *path*; on absence return default, on corruption quarantine + default."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return default_factory()
    except (OSError, ValueError, UnicodeDecodeError) as e:
        moved = quarantine(path)
        _log(f"Fichier illisible {path} ({e}); "
             + (f"sauvegardé sous {moved}; " if moved else "")
             + "valeurs par défaut utilisées.")
        return default_factory()
    if not isinstance(data, expect_type):
        moved = quarantine(path)
        _log(f"Contenu inattendu dans {path}; "
             + (f"sauvegardé sous {moved}; " if moved else "")
             + "valeurs par défaut utilisées.")
        return default_factory()
    return data


@contextlib.contextmanager
def locked(path: str, timeout: float = 10.0) -> Iterator[None]:
    """Exclusive inter-process lock associated with *path*."""
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)) or ".", exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    # Never deadlock the UI on a stale lock: proceed unlocked.
                    _log(f"Verrou {lock_path} non obtenu, poursuite sans verrou.")
                    break
                time.sleep(0.02)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (in place) and return base."""
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def mtime_or_none(path: str) -> Optional[float]:
    try:
        return os.stat(path).st_mtime_ns / 1e9
    except OSError:
        return None
