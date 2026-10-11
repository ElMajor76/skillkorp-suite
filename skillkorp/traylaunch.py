#!/usr/bin/env python3
"""
SkillKorp Suite - links between the GUI and the tray indicator.

Kept free of any GTK import: the GUI runs GTK 4 and the tray GTK 3, which
cannot be loaded in the same process.

- the tray is single-instance through a lock on its PID file; the GUI uses
  the same lock to know whether the tray runs and starts it otherwise;
- the tray's "Quitter" also closes the GUI through the "quit" action that
  GApplication exports on the session bus.
"""

import fcntl
import os
import subprocess
import sys

GUI_APP_ID = "io.github.skillkorp.suite"
GUI_OBJECT_PATH = "/" + GUI_APP_ID.replace(".", "/")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def pid_file() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or os.path.expanduser("~/.config/skillkorp")
    return os.path.join(runtime, "skillkorp-tray.pid")


def is_tray_running() -> bool:
    """True when a tray process holds the single-instance lock."""
    try:
        fd = os.open(pid_file(), os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
    return False


def launch_tray() -> bool:
    """Start the tray in its own session unless it already runs.

    Returns True when a new tray process was started. Disabled by
    SKILLKORP_NO_TRAY=1 (tests, or users who do not want the indicator)."""
    if os.environ.get("SKILLKORP_NO_TRAY") == "1" or is_tray_running():
        return False
    code = f"import sys; sys.path.insert(0, {BASE_DIR!r}); from skillkorp.tray import main; sys.exit(main())"
    try:
        subprocess.Popen([sys.executable, "-I", "-c", code], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
    except OSError as e:
        print(f"[skillkorp] impossible de lancer l'indicateur : {e}", file=sys.stderr)
        return False
    return True
