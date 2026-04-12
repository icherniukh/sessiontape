import logging
import os
import queue
import signal
from typing import Optional

import psutil

from src.capture import AudioCapture
from src.config import Config
from src.events import (CaptureDied, EventQueue, ExportFailed, ExportFinished,
                        ExportStarted, ProcessReplaced, ProcessStarted,
                        ProcessStopped, SegmentClosed, SegmentOpened,
                        ShutdownRequested, TapBroken)
from src.process_monitor import ProcessMonitor
from src.recorder_core import ExportManager, recover_orphaned_raw_files

log = logging.getLogger("rb-recorder")

_STALE_PROCESS_NAMES = {"audiotee", "mac-capture", "auto-rb-recorder"}


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
        self.config = config
        self._capture: Optional[AudioCapture] = None
        self._current_pid: Optional[int] = None
        self._restart_count: int = 0
        self._queue: EventQueue = queue.Queue()
        self._export_manager = ExportManager(
            sample_rate=self.config.sample_rate,
            channels=2,
            bytes_per_sample=2,
            export_format=self.config.export_format,
            event_sink=self._queue.put,
        )

        self._monitor = ProcessMonitor(
            config.process_name,
            queue=self._queue,
            poll_interval=config.poll_interval
        )

        # Recover any orphaned files from previous interrupted runs
        recover_orphaned_raw_files(
            output_dir=self.config.output_dir,
            sample_rate=self.config.sample_rate,
            export_format=self.config.export_format,
            export_manager=self._export_manager,
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
            min_segment_duration=self.config.min_segment_duration,
            decay_tail=self.config.decay_tail,
            export_format=self.config.export_format,
            export_manager=self._export_manager,
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
        _cleanup_stale_processes()

        # Register signal handlers to push ShutdownRequested to the queue
        def _on_signal(signum, frame):
            # Using put_nowait because signal handlers should be fast
            try:
                self._queue.put_nowait(ShutdownRequested())
            except queue.Full:
                # queue.Queue() is unbounded (maxsize=0); queue.Full is never raised, but kept as defensive code
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
                    self._restart_count = 0
                    self._start_capture(event.pid)

                elif isinstance(event, ProcessReplaced):
                    if self._current_pid is None:
                        log.warning(
                            "ProcessReplaced received with no active capture; starting fresh on PID %s.",
                            event.new_pid,
                        )
                        self._restart_count = 0
                        self._start_capture(event.new_pid)
                    elif event.old_pid != self._current_pid:
                        log.warning(
                            "ProcessReplaced old PID %s does not match active PID %s; ignoring stale event.",
                            event.old_pid,
                            self._current_pid,
                        )
                    else:
                        log.info(
                            "Rekordbox PID changed from %s to %s. Restarting capture on the new process.",
                            event.old_pid,
                            event.new_pid,
                        )
                        self._restart_count = 0
                        self._stop_capture()
                        self._start_capture(event.new_pid)

                elif isinstance(event, ProcessStopped):
                    if self._current_pid is None:
                        log.warning("ProcessStopped received but no capture is active; ignoring.")
                    elif event.pid != self._current_pid:
                        log.warning(
                            f"ProcessStopped PID {event.pid} does not match active PID "
                            f"{self._current_pid}; ignoring stale event."
                        )
                    else:
                        log.info("Rekordbox closed.")
                        self._stop_capture()

                elif isinstance(event, CaptureDied):
                    if self._current_pid:
                        if self._restart_count >= 5:
                            log.critical(
                                f"Capture helper died (exit code {event.exit_code}) and restart limit "
                                f"({self._restart_count}) reached. Giving up."
                            )
                        else:
                            self._restart_count += 1
                            log.warning(
                                f"Capture helper died (exit code {event.exit_code}). "
                                f"Restarting capture (attempt {self._restart_count})."
                            )
                            self._stop_capture(keep_pid=True)
                            # Rekordbox is likely still running if we haven't received ProcessStopped
                            self._start_capture(self._current_pid)

                elif isinstance(event, TapBroken):
                    if self._current_pid:
                        if self._restart_count >= 5:
                            log.critical(
                                f"Tap broken and restart limit ({self._restart_count}) reached. Giving up."
                            )
                        else:
                            self._restart_count += 1
                            log.warning(
                                f"Watchdog: Tap broken (all zeros). Restarting capture for fresh tap "
                                f"(attempt {self._restart_count})."
                            )
                            self._stop_capture(keep_pid=True)
                            self._start_capture(self._current_pid)
                
                elif isinstance(event, ShutdownRequested):
                    log.info("Shutdown requested.")
                    break

                elif isinstance(event, SegmentOpened):
                    log.info(f"Segment opened: {event.raw_path}")

                elif isinstance(event, SegmentClosed):
                    if event.discarded:
                        log.info(
                            "Segment discarded: %.1fs %s",
                            event.duration_seconds,
                            event.raw_path,
                        )
                    else:
                        log.info(
                            "Segment closed: %.1fs %s",
                            event.duration_seconds,
                            event.raw_path,
                        )

                elif isinstance(event, ExportStarted):
                    phase = "recovery export" if event.recovery else "export"
                    log.info(f"{phase.capitalize()} started: {event.output_path}")

                elif isinstance(event, ExportFinished):
                    phase = "Recovery export" if event.recovery else "Export"
                    log.info(f"{phase} finished: {event.output_path}")

                elif isinstance(event, ExportFailed):
                    phase = "Recovery export" if event.recovery else "Export"
                    log.error(f"{phase} failed for {event.output_path}: {event.error}")
        finally:
            self._stop_capture()
            self._monitor.stop()
            self._export_manager.shutdown()
            log.info("Daemon exit.")
