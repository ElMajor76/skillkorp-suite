#!/usr/bin/env python3
"""
SkillKorp Suite - background worker for the GUI and the tray.

Hardware calls can block for hundreds of milliseconds (USB retries, the
2.4GHz link, the firmware's flash-write delays). Running them on the GTK main
loop froze the interface, so every driver call goes through a DeviceWorker:
a single thread per device that executes tasks in order (the hardware needs
serialized access anyway) and hands results back through a `dispatch`
callable - GLib.idle_add in the UI, a plain call in tests.

Tasks submitted with the same `key` are coalesced: while one is still
waiting in the queue, a newer one replaces it. Dragging a slider therefore
sends the final value once instead of flooding the device with every
intermediate step.
"""

import queue
import threading
import traceback
from typing import Any, Callable, Dict, Optional


class DeviceWorker:
    def __init__(self, name: str, dispatch: Callable[[Callable[[], Any]], Any]):
        self.name = name
        self._dispatch = dispatch
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._pending: Dict[str, tuple] = {}
        self._lock = threading.Lock()
        self._counter = 0
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name=f"skillkorp-{name}", daemon=True)
        self._thread.start()

    def submit(self, func: Callable[[], Any], on_done: Optional[Callable[[Any], None]] = None,
               on_error: Optional[Callable[[BaseException], None]] = None, key: Optional[str] = None):
        """Run func() in the worker; on_done(result) / on_error(exc) run via dispatch."""
        with self._lock:
            if self._stopped:
                return
            if key is None:
                self._counter += 1
                key = f"__task{self._counter}"
                is_new = True
            else:
                is_new = key not in self._pending
            self._pending[key] = (func, on_done, on_error)
        if is_new:
            self._queue.put(key)

    def is_idle(self) -> bool:
        with self._lock:
            return not self._pending

    def stop(self):
        with self._lock:
            self._stopped = True
            self._pending.clear()
        self._queue.put(None)

    def _deliver(self, callback, value):
        def run():
            try:
                callback(value)
            except Exception:
                traceback.print_exc()
            return False  # GLib.idle_add: do not repeat
        self._dispatch(run)

    def _run(self):
        while True:
            key = self._queue.get()
            if key is None:
                return
            with self._lock:
                task = self._pending.pop(key, None)
            if task is None:
                continue
            func, on_done, on_error = task
            try:
                result = func()
            except BaseException as e:  # noqa: B902 - reported to the UI
                if on_error is not None:
                    self._deliver(on_error, e)
                else:
                    traceback.print_exc()
                continue
            if on_done is not None:
                self._deliver(on_done, result)
