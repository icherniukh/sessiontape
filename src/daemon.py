import logging
import os
import signal
import time
from typing import Optional

from src.capture import AudioCapture
from src.config import Config
from src.process_monitor import ProcessMonitor
from src.recorder_core import recover_orphaned_raw_files

log = logging.getLogger("rb-recorder")

_WATCHDOG_INTERVAL = 5 * 60  # seconds between watchdog checks


class RecorderDaemon:
    def __init__(self, config: Config):
        self.config = config
        self._capture: Optional[AudioCapture] = None
        self._running = False
        self._current_pid: Optional[int] = None
        self._capture_started_at: float = 0.0
        self._next_watchdog_at: float = 0.0

        self._monitor = ProcessMonitor(
            config.process_name, poll_interval=config.poll_interval
        )
        self._monitor.on_start = self._on_rekordbox_start
        self._monitor.on_stop = self._on_rekordbox_stop

        # Recover any orphaned files from previous interrupted runs
        recover_orphaned_raw_files(
            output_dir=self.config.output_dir,
            sample_rate=self.config.sample_rate,
            export_format=self.config.export_format
        )

    def _on_rekordbox_start(self, pid: int):
        log.info(f"Rekordbox detected (PID {pid}). Starting recording.")
        os.makedirs(self.config.output_dir, exist_ok=True)
        self._current_pid = pid
        self._capture_started_at = time.time()
        self._next_watchdog_at = self._capture_started_at + _WATCHDOG_INTERVAL
        self._capture = AudioCapture(
            pid=pid,
            output_dir=self.config.output_dir,
            sample_rate=self.config.sample_rate,
            silence_threshold_db=self.config.silence_threshold_db,
            min_silence_duration=self.config.min_silence_duration,
            decay_tail=self.config.decay_tail,
            export_format=self.config.export_format,
        )
        self._capture.start()

    def _on_rekordbox_stop(self):
        if not self._capture:
            return
        log.info("Rekordbox closed. Stopping recording.")
        self._capture.stop()
        self._capture = None
        self._current_pid = None

    def _watchdog_check(self) -> None:
        if not self._capture or not self._current_pid:
            return
        if time.time() < self._next_watchdog_at:
            return

        self._next_watchdog_at = time.time() + _WATCHDOG_INTERVAL

        last_active = self._capture.recorder.last_active_at
        silent_for = time.time() - (last_active if last_active else self._capture_started_at)

        if silent_for < _WATCHDOG_INTERVAL:
            return

        log.warning(
            f"Watchdog: Rekordbox running but no audio detected for {silent_for / 60:.0f}m — "
            f"restarting capture for fresh tap (PID {self._current_pid})"
        )
        pid = self._current_pid
        self._capture.stop()
        self._capture = None
        self._on_rekordbox_start(pid)

    def run(self):
        self._running = True
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)
        log.info("Rekordbox Auto-Recorder armed. Waiting for Rekordbox...")

        while self._running:
            self._monitor.poll_once()
            self._watchdog_check()
            time.sleep(self.config.poll_interval)

    def _handle_shutdown(self, signum, frame):
        log.info("Shutdown signal received.")
        self._running = False
        self._on_rekordbox_stop()
