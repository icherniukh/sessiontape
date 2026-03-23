import logging
import subprocess
import threading
from typing import Optional

from src.recorder_core import PCMStreamRecorder, _find_executable, db_to_rms

log = logging.getLogger("rb-recorder")


class AudioCapture:
    """Captures PCM from audiotee and forwards it to the recorder core."""

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
    ):
        self.pid = pid
        self.output_dir = output_dir
        self.sample_rate = sample_rate
        self.source_name = source_name
        self.is_recording = False

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
        while self.is_recording and self._proc and self._proc.stdout:
            chunk = self._proc.stdout.read(self.chunk_size)
            if not chunk:
                break
            self.recorder.process_chunk(chunk)

    def start(self) -> None:
        if self.is_recording:
            return

        self.is_recording = True
        self.recorder.reset()

        self._proc = subprocess.Popen(
            [
                _find_executable("audiotee"),
                "--include-processes",
                str(self.pid),
                "--sample-rate",
                str(self.sample_rate),
                "--stereo",
                "--flush",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def stop(self) -> None:
        if not self.is_recording:
            return

        if self._proc:
            if self._proc.poll() is None:
                self._proc.terminate()
            self._proc.wait(timeout=10)
            self._proc = None

        self.is_recording = False

        if self._reader_thread:
            self._reader_thread.join(timeout=2.0)
            self._reader_thread = None

        self.recorder.finalize()
