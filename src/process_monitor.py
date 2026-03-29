import logging
import os
import threading
import time
from typing import Optional

import psutil

from src.events import EventQueue, ProcessStarted, ProcessStopped

log = logging.getLogger("rb-recorder")


class ProcessMonitor(threading.Thread):
    def __init__(self, process_name: str, queue: EventQueue, poll_interval: float = 2.0,
                 startup_delay: float = 10.0, stop_delay: float = 10.0):
        super().__init__(daemon=True, name="ProcessMonitor")
        self.process_name = process_name
        self.queue = queue
        self.poll_interval = poll_interval
        self.startup_delay = startup_delay
        self.stop_delay = stop_delay
        self._current_pid: Optional[int] = None
        self._stop_event = threading.Event()

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

    def run(self):
        self._stop_event.clear()
        while not self._stop_event.is_set():
            self._poll_once()
            self._stop_event.wait(timeout=self.poll_interval)

    def stop(self):
        self._stop_event.set()

    def _poll_once(self):
        pid = self._find_pid()
        was_running = self._current_pid is not None
        is_running = pid is not None

        if is_running and not was_running:
            log.info(f"Process detected (PID {pid}). Waiting {self.startup_delay}s for startup to settle...")
            # Wait for startup to settle (Rekordbox spawns multiple
            # short-lived processes during launch), then re-check
            self._stop_event.wait(timeout=self.startup_delay)
            pid = self._find_pid()
            if pid is None:
                return  # Transient process — ignore
            self._current_pid = pid
            self.queue.put(ProcessStarted(pid=pid))
        elif not is_running and was_running:
            # Debounce stop: Rekordbox briefly disappears between
            # process restarts during startup. Wait and re-check.
            self._stop_event.wait(timeout=self.stop_delay)
            pid = self._find_pid()
            if pid is not None:
                # Process came back — update PID, don't fire stop
                self._current_pid = pid
                return
            stopped_pid = self._current_pid
            self._current_pid = None
            self.queue.put(ProcessStopped(pid=stopped_pid))
