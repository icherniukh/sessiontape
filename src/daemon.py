import logging
import os
import queue
import signal
from typing import Optional

import psutil

from src.capture import AudioCapture
from src.config import Config
from src.events import (CaptureDied, EventQueue, ProcessStarted, ProcessStopped,
                        ShutdownRequested, TapBroken)
from src.process_monitor import ProcessMonitor
from src.recorder_core import recover_orphaned_raw_files

log = logging.getLogger("rb-recorder")

_STALE_PROCESS_NAMES = {"audiotee", "auto-rb-recorder"}


def _cleanup_stale_processes() -> None:
    """Kill any leftover audiotee or auto-rb-recorder processes from a previous run."""
    current_pid = os.getpid()
    own_pids = {current_pid, os.getppid()}
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["name"] in _STALE_PROCESS_NAMES and proc.pid not in own_pids:
                log.warning(f"Killing stale process: {proc.info['name']} PID {proc.pid}")
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


class RecorderDaemon:
    def __init__(self, config: Config):
        _cleanup_stale_processes()
        self.config = config
        self._capture: Optional[AudioCapture] = None
        self._current_pid: Optional[int] = None
        self._queue: EventQueue = queue.Queue()

        self._monitor = ProcessMonitor(
            config.process_name,
            queue=self._queue,
            poll_interval=config.poll_interval
        )

        # Recover any orphaned files from previous interrupted runs
        recover_orphaned_raw_files(
            output_dir=self.config.output_dir,
            sample_rate=self.config.sample_rate,
            export_format=self.config.export_format
        )

    def _start_capture(self, pid: int):
        log.info(f"Rekordbox detected (PID {pid}). Starting recording.")
        os.makedirs(self.config.output_dir, exist_ok=True)
        self._current_pid = pid
        self._capture = AudioCapture(
            pid=pid,
            output_dir=self.config.output_dir,
            queue=self._queue,
            sample_rate=self.config.sample_rate,
            silence_threshold_db=self.config.silence_threshold_db,
            min_silence_duration=self.config.min_silence_duration,
            decay_tail=self.config.decay_tail,
            export_format=self.config.export_format,
        )
        self._capture.start()

    def _stop_capture(self, keep_pid: bool = False):
        if not self._capture:
            return
        log.info("Stopping recording.")
        self._capture.stop()
        self._capture = None
        if not keep_pid:
            self._current_pid = None

    def run(self):
        # Register signal handlers to push ShutdownRequested to the queue
        def _on_signal(signum, frame):
            # Using put_nowait because signal handlers should be fast
            try:
                self._queue.put_nowait(ShutdownRequested())
            except queue.Full:
                pass

        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)

        log.info(f"--- rb-recorder started · PID {os.getpid()} ---")
        
        # Start the process monitor thread
        self._monitor.start()

        try:
            while True:
                event = self._queue.get()
                
                if isinstance(event, ProcessStarted):
                    self._start_capture(event.pid)
                
                elif isinstance(event, ProcessStopped):
                    log.info("Rekordbox closed.")
                    self._stop_capture()
                
                elif isinstance(event, CaptureDied):
                    if self._current_pid:
                        log.warning(f"Capture helper died (exit code {event.exit_code}). Restarting capture.")
                        self._stop_capture(keep_pid=True)
                        # Rekordbox is likely still running if we haven't received ProcessStopped
                        self._start_capture(self._current_pid)
                
                elif isinstance(event, TapBroken):
                    if self._current_pid:
                        log.warning("Watchdog: Tap broken (all zeros). Restarting capture for fresh tap.")
                        self._stop_capture(keep_pid=True)
                        self._start_capture(self._current_pid)
                
                elif isinstance(event, ShutdownRequested):
                    log.info("Shutdown requested.")
                    break
        finally:
            self._stop_capture()
            self._monitor.stop()
            log.info("Daemon exit.")
