import logging
import subprocess
import threading
from typing import Optional

from src.backends.base import CaptureBackend
from src.platform import get_platform_backend
from src.recorder_core import PCMStreamRecorder

log = logging.getLogger("rb-recorder")

class AudioCapture:
    """Captures PCM from a backend subprocess and forwards it to the recorder core."""

    def __init__(
        self,
        pid: int,
        output_dir: str,
        sample_rate: int = 48000,
        source_name: str = "rekordbox",
        silence_threshold_db: float = -50.0,
        min_silence_duration: float = 15.0,
        decay_tail: float = 5.0,
        export_format: str = "wav",
        backend: CaptureBackend | None = None,
    ):
        self.pid = pid
        self.output_dir = output_dir
        self.sample_rate = sample_rate
        self.source_name = source_name
        self.is_recording = False
        self.backend = backend or get_platform_backend()

        self.recorder = PCMStreamRecorder(
            output_dir=output_dir,
            sample_rate=sample_rate,
            silence_threshold_db=silence_threshold_db,
            min_silence_duration=min_silence_duration,
            decay_tail=decay_tail,
            export_format=export_format,
        )

        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None

    @property
    def chunk_size(self) -> int:
        return self.recorder.chunk_size

    def _read_loop(self) -> None:
        chunks_read = 0
        log.debug(f"Read loop started (chunk_size={self.chunk_size})")
        while self.is_recording and self._proc and self._proc.stdout:
            chunk = self._proc.stdout.read(self.chunk_size)
            if not chunk:
                break
            chunks_read += 1
            if chunks_read == 1:
                log.debug("First PCM chunk received from capture helper")
            self.recorder.process_chunk(chunk)
        exit_code = self._proc.poll() if self._proc else None
        log.info(f"Read loop exited after {chunks_read} chunks (helper exit code: {exit_code})")

    def start(self) -> None:
        if self.is_recording:
            return

        self.is_recording = True
        self.recorder.reset()
        self._proc = self.backend.start(self.pid, self.sample_rate)
        log.info(f"Capture helper started (PID={self._proc.pid})")

        if self._proc.stderr:
            threading.Thread(target=self._log_stderr, daemon=True).start()

        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _log_stderr(self) -> None:
        for line in self._proc.stderr:
            decoded = line.decode(errors="replace").rstrip()
            if decoded:
                log.debug(f"[helper stderr] {decoded}")

    def stop(self) -> None:
        if not self.is_recording:
            return

        if self._proc:
            self.backend.stop(self._proc)
            self._proc = None

        self.is_recording = False

        if self._reader_thread:
            self._reader_thread.join(timeout=2.0)
            self._reader_thread = None

        self.recorder.finalize()
