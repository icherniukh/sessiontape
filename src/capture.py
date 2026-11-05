import json
import logging
import os
import re
import subprocess
import threading
import time
from typing import Optional

from src.backends.base import CaptureBackend
from src.events import CaptureDied, Event, EventQueue, TapBroken
from src.perf_metrics import PipelineCaptureMetrics, parse_capture_line
from src.platform import get_platform_backend
from src.recorder_core import ExportManager, PCMStreamRecorder

log = logging.getLogger("sessiontape")
capture_log = logging.getLogger("sessiontape.capture")


_FORMAT_MISMATCH_RE = re.compile(r"FORMAT_MISMATCH tap=(\d+)")

# mac-capture exit status for "tap sample rate != requested sample rate".
EXIT_FORMAT_MISMATCH = 3


class AudioCapture:
    """Captures PCM from a backend subprocess and forwards it to the recorder core."""

    def __init__(
        self,
        pid: int,
        output_dir: str,
        queue: EventQueue,
        sample_rate: int = 48000,
        source_name: str = "rekordbox",
        silence_threshold_db: float = -50.0,
        min_silence_duration: float = 15.0,
        min_segment_duration: float = 10.0,
        decay_tail: float = 5.0,
        export_format: str = "wav",
        backend: CaptureBackend | None = None,
        backend_name: str | None = None,
        export_manager: ExportManager | None = None,
        perf_metrics: bool = True,
        mac_capture_backend: str = "auto",
        mac_capture_metrics: bool = True,
        mac_capture_diag: bool = False,
        mac_capture_log: str = "",
    ):
        self.pid = pid
        self.output_dir = output_dir
        self.queue = queue
        self.sample_rate = sample_rate
        self.source_name = source_name
        self.is_recording = False
        self.backend = backend or get_platform_backend(
            backend_name,
            mac_capture_backend=mac_capture_backend,
            mac_capture_metrics=mac_capture_metrics,
            mac_capture_diag=mac_capture_diag,
            mac_capture_log=mac_capture_log,
        )

        self.recorder = PCMStreamRecorder(
            output_dir=output_dir,
            on_tap_broken=lambda: self.queue.put(TapBroken()),
            sample_rate=sample_rate,
            silence_threshold_db=silence_threshold_db,
            min_silence_duration=min_silence_duration,
            min_segment_duration=min_segment_duration,
            decay_tail=decay_tail,
            export_format=export_format,
            export_manager=export_manager,
            event_sink=self._emit_event,
        )

        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._metrics_thread: Optional[threading.Thread] = None
        self._metrics = PipelineCaptureMetrics()
        self._metrics_enabled = perf_metrics
        self._metrics_stop = threading.Event()
        self._last_read_at = 0.0
        # Tap sample rate parsed from a FORMAT_MISMATCH stderr line, if any.
        self.format_mismatch_rate: int | None = None

    @property
    def chunk_size(self) -> int:
        return self.recorder.chunk_size

    def _emit_event(self, event: Event) -> None:
        self.queue.put(event)

    def _emit_capture_metric(self, *, force: bool = False) -> None:
        if not self._metrics_enabled:
            return
        line = self._metrics.maybe_emit(force=force)
        if line:
            capture_log.info(line)

    def _metrics_loop(self) -> None:
        while not self._metrics_stop.wait(1.0):
            self._emit_capture_metric()

    def _ingest_native_metric(self, line: str) -> bool:
        parsed = parse_capture_line(line)
        if parsed is None:
            return False
        self._metrics.ingest_native(parsed)
        return True

    def _read_chunk(self, stream) -> bytes:
        """Read exactly chunk_size bytes, looping over short reads.

        Returns fewer bytes only at EOF; a partial final chunk is trimmed to a
        whole number of s16le stereo frames (4 bytes), possibly to b"".
        """
        size = self.chunk_size
        buf = bytearray()
        while len(buf) < size:
            part = stream.read(size - len(buf))
            if not part:
                return bytes(buf[: len(buf) - len(buf) % 4])
            buf += part
        return bytes(buf)

    def _read_loop(self) -> None:
        chunks_read = 0
        log.debug(f"Read loop started (chunk_size={self.chunk_size})")
        self._last_read_at = time.monotonic()
        while self._proc and self._proc.stdout:
            chunk = self._read_chunk(self._proc.stdout)
            if not chunk:
                break
            chunks_read += 1
            if chunks_read == 1:
                log.debug("First PCM chunk received from capture process")

            now = time.monotonic()
            gap_ms = (now - self._last_read_at) * 1000.0 if chunks_read > 1 else 0.0
            self._last_read_at = now

            process_start = time.perf_counter()
            self.recorder.process_chunk(chunk)
            process_ms = (time.perf_counter() - process_start) * 1000.0
            self._metrics.record_chunk(gap_ms=gap_ms, process_ms=process_ms)

        exit_code = None
        if self._proc:
            try:
                exit_code = self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                exit_code = self._proc.poll()
        self._emit_capture_metric(force=True)
        if self.is_recording:
            # Let the stderr thread drain so FORMAT_MISMATCH is parsed before
            # the daemon sees CaptureDied.
            if self._stderr_thread:
                self._stderr_thread.join(timeout=1.0)
            log.warning(
                f"[DIAG] Capture process exited unexpectedly after {chunks_read} chunks "
                f"(exit code: {exit_code})"
            )
            self.queue.put(CaptureDied(exit_code=exit_code))
        else:
            log.info(
                f"Read loop exited after {chunks_read} chunks (capture exit code: {exit_code})"
            )

    def _log_stderr(self) -> None:
        if not self._proc or not self._proc.stderr:
            return
        for raw_line in self._proc.stderr:
            line = raw_line.strip()
            if not line:
                continue
            decoded = line.decode(errors="replace") if isinstance(line, bytes) else line
            m = _FORMAT_MISMATCH_RE.search(decoded)
            if m:
                self.format_mismatch_rate = int(m.group(1))
            if self._ingest_native_metric(decoded):
                continue
            if decoded.startswith("INFO: ") and self._ingest_native_metric(decoded[6:]):
                continue
            if decoded.startswith("INFO: "):
                capture_log.info(decoded[6:])
            elif decoded.startswith("ERROR: "):
                capture_log.error(decoded[7:])
            elif decoded.startswith("DEBUG: "):
                capture_log.debug(decoded[7:])
            else:
                capture_log.info(decoded)

    def start(self) -> None:
        if self.is_recording:
            return

        self.is_recording = True
        self.recorder.reset()
        self._metrics.reset()
        self._metrics_stop.clear()
        self._proc = self.backend.start(self.pid, self.sample_rate)
        log.info(f"Capture process started (PID={self._proc.pid})")

        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        if self._proc.stderr:
            self._stderr_thread = threading.Thread(target=self._log_stderr, daemon=True)
            self._stderr_thread.start()

        if self._metrics_enabled:
            self._metrics_thread = threading.Thread(
                target=self._metrics_loop,
                name="CaptureMetrics",
                daemon=True,
            )
            self._metrics_thread.start()

    def _ensure_proc_stopped(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return

        log.warning(
            "Capture process still running after backend.stop(); forcing cleanup"
        )
        proc.kill()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            log.error("Capture process did not exit after forced kill")

    def stop(self) -> None:
        if not self.is_recording:
            return

        self.is_recording = False
        self._metrics_stop.set()

        proc = self._proc
        if proc:
            self.backend.stop(proc)
            self._ensure_proc_stopped(proc)

        if self._metrics_thread:
            self._metrics_thread.join(timeout=2.0)
            self._metrics_thread = None

        if self._reader_thread:
            self._reader_thread.join(timeout=2.0)
            self._reader_thread = None

        if self._stderr_thread:
            self._stderr_thread.join(timeout=2.0)
            self._stderr_thread = None

        self._proc = None
        self.recorder.finalize()
