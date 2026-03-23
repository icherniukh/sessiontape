import os
import time
from typing import Callable, Optional

import psutil


class ProcessMonitor:
    def __init__(self, process_name: str, poll_interval: float = 2.0,
                 startup_delay: float = 10.0, stop_delay: float = 10.0):
        self.process_name = process_name
        self.poll_interval = poll_interval
        self.startup_delay = startup_delay
        self.stop_delay = stop_delay
        self.on_start: Callable[[int], None] = lambda pid: None
        self.on_stop: Callable[[], None] = lambda: None
        self._current_pid: Optional[int] = None

    def _find_pid(self) -> Optional[int]:
        target = self.process_name.lower()
        target_stem = os.path.splitext(target)[0]
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = proc.info.get("name")
                if not name:
                    continue
                normalized = name.lower()
                if normalized == target or os.path.splitext(normalized)[0] == target_stem:
                    return proc.info["pid"]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return None

    def poll_once(self):
        pid = self._find_pid()
        was_running = self._current_pid is not None
        is_running = pid is not None

        if is_running and not was_running:
            # Wait for startup to settle (Rekordbox spawns multiple
            # short-lived processes during launch), then re-check
            time.sleep(self.startup_delay)
            pid = self._find_pid()
            if pid is None:
                return  # Transient process — ignore
            self._current_pid = pid
            self.on_start(pid)
        elif not is_running and was_running:
            # Debounce stop: Rekordbox briefly disappears between
            # process restarts during startup. Wait and re-check.
            time.sleep(self.stop_delay)
            pid = self._find_pid()
            if pid is not None:
                # Process came back — update PID, don't fire stop
                self._current_pid = pid
                return
            self._current_pid = None
            self.on_stop()
