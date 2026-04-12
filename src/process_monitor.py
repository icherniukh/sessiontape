import logging
import os
import threading
from typing import Optional

import psutil

from src.events import EventQueue, ProcessReplaced, ProcessStarted, ProcessStopped

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

    def _find_matching_pids(self) -> list[int]:
        target = self.process_name.lower()
        target_stem = os.path.splitext(target)[0]
        matches: list[tuple[float, int]] = []
        for proc in psutil.process_iter(["pid", "name", "create_time"]):
            try:
                name = proc.info.get("name")
                if not name:
                    continue
                normalized = name.lower()
                if normalized == target or os.path.splitext(normalized)[0] == target_stem:
                    create_time = proc.info.get("create_time") or 0.0
                    matches.append((create_time, proc.info["pid"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        matches.sort()
        return [pid for _, pid in matches]

    def _find_pid(self) -> Optional[int]:
        matches = self._find_matching_pids()
        return matches[0] if matches else None

    def run(self):
        while not self._stop_event.is_set():
            self._poll_once()
            self._stop_event.wait(timeout=self.poll_interval)

    def stop(self):
        self._stop_event.set()

    def _poll_once(self):
        matching_pids = self._find_matching_pids()
        pid = matching_pids[0] if matching_pids else None
        was_running = self._current_pid is not None
        is_running = pid is not None

        if is_running and not was_running:
            log.info(f"Process detected (PID {pid}). Waiting {self.startup_delay}s for startup to settle...")
            # Wait for startup to settle (Rekordbox spawns multiple
            # short-lived processes during launch), then re-check
            self._stop_event.wait(timeout=self.startup_delay)
            matching_pids = self._find_matching_pids()
            if not matching_pids:
                return  # Transient process — ignore
            pid = matching_pids[0]
            self._current_pid = pid
            self.queue.put(ProcessStarted(pid=pid))
        elif is_running and was_running and self._current_pid not in matching_pids:
            old_pid = self._current_pid
            log.info(
                "Process PID changed from %s to %s. Waiting %.1fs to settle...",
                old_pid,
                pid,
                self.stop_delay,
            )
            self._stop_event.wait(timeout=self.stop_delay)
            matching_pids = self._find_matching_pids()
            if not matching_pids:
                self._current_pid = None
                self.queue.put(ProcessStopped(pid=old_pid))
                return
            if old_pid in matching_pids:
                return
            new_pid = matching_pids[0]
            self._current_pid = new_pid
            self.queue.put(ProcessReplaced(old_pid=old_pid, new_pid=new_pid))
        elif not is_running and was_running:
            # Debounce stop: Rekordbox briefly disappears between
            # process restarts during startup. Wait and re-check.
            self._stop_event.wait(timeout=self.stop_delay)
            matching_pids = self._find_matching_pids()
            if matching_pids:
                # Process came back — update PID, don't fire stop
                self._current_pid = matching_pids[0]
                return
            stopped_pid = self._current_pid
            self._current_pid = None
            self.queue.put(ProcessStopped(pid=stopped_pid))
