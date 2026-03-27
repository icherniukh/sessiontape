import json
import logging
import subprocess
import threading
from typing import Optional

from src.backends.base import CaptureBackend
from src.platform import get_platform_backend
from src.recorder_core import PCMStreamRecorder

log = logging.getLogger("rb-recorder")
audiotee_log = logging.getLogger("rb-recorder.audiotee")

_AUDIOTEE_LEVEL_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "error": logging.ERROR,
}


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
        self._stderr_thread: Optional[threading.Thread] = None

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

    def _log_stderr(self) -> None:
        if not self._proc or not self._proc.stderr:
            return
        for raw_line in self._proc.stderr:
            line = raw_line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                msg_type = msg.get("message_type", "info")
                data = msg.get("data") or {}
                text = data.get("message", "")
                context = data.get("context")
                if context:
                    ctx_str = " ".join(f"{k}={v}" for k, v in context.items())
                    text = f"{text} [{ctx_str}]"
                level = _AUDIOTEE_LEVEL_MAP.get(msg_type, logging.INFO)
                audiotee_log.log(level, "%s", text)
            except (json.JSONDecodeError, AttributeError, KeyError):
                decoded = line.decode(errors="replace") if isinstance(line, bytes) else line
                audiotee_log.debug(f"[stderr] {decoded}")

    def start(self) -> None:
        if self.is_recording:
            return

        self.is_recording = True
        self.recorder.reset()
        self._proc = self.backend.start(self.pid, self.sample_rate)
        log.info(f"Capture helper started (PID={self._proc.pid})")

        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        if self._proc.stderr:
            self._stderr_thread = threading.Thread(target=self._log_stderr, daemon=True)
            self._stderr_thread.start()

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

        if self._stderr_thread:
            self._stderr_thread.join(timeout=2.0)
            self._stderr_thread = None

        self.recorder.finalize()
