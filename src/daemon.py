import logging
import os
import signal
import time
from typing import Callable, Optional

from src.capture import AudioCapture
from src.config import Config
from src.process_monitor import ProcessMonitor
from src.recorder_core import recover_orphaned_raw_files

log = logging.getLogger("rb-recorder")


class RecorderDaemon:
    def __init__(self, config: Config):
        self.config = config
        self._capture: Optional[AudioCapture] = None
        self._running = False

        # Callbacks set by DaemonBridge (called from background thread)
        self.on_state_change: Optional[Callable[[str, dict], None]] = None
        self.on_capture_active: Optional[Callable[[], None]] = None
        self.on_segment_saved: Optional[Callable[[str, float], None]] = None

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
        self._capture = AudioCapture(
            pid=pid,
            output_dir=self.config.output_dir,
            sample_rate=self.config.sample_rate,
            silence_threshold_db=self.config.silence_threshold_db,
            min_silence_duration=self.config.min_silence_duration,
            decay_tail=self.config.decay_tail,
            export_format=self.config.export_format,
        )
        self._capture.recorder.on_active = self.on_capture_active
        self._capture.recorder.on_segment_saved = self.on_segment_saved
        self._capture.start()
        if self.on_state_change:
            self.on_state_change("monitoring", {"pid": pid})

    def _on_rekordbox_stop(self):
        if not self._capture:
            return
        log.info("Rekordbox closed. Stopping recording.")
        self._capture.stop()
        self._capture = None
        if self.on_state_change:
            self.on_state_change("idle", {})
    def run(self):
        self._running = True
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)
        log.info("Rekordbox Auto-Recorder armed. Waiting for Rekordbox...")

        while self._running:
            self._monitor.poll_once()
            time.sleep(self.config.poll_interval)

    def _handle_shutdown(self, signum, frame):
        log.info("Shutdown signal received.")
        self._running = False
        self._on_rekordbox_stop()
