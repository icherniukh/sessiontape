import logging
import math
import os
import shutil
import subprocess
import threading
import wave
from array import array
from collections import deque
from datetime import datetime
from typing import Optional

log = logging.getLogger("rb-recorder")


def db_to_rms(db: float) -> float:
    # 0 dBFS = 32768 for 16-bit audio
    return 32768.0 * (10.0 ** (db / 20.0))


def _find_executable(name: str) -> str:
    path = shutil.which(name)
    if path:
        return path
    # macOS LaunchAgents often lack standard interactive PATHs
    for fallback in [
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/usr/bin/{name}",
    ]:
        if os.path.exists(fallback):
            return fallback
    return name


class Exporter:
    def __init__(
        self,
        sample_rate: int,
        channels: int,
        bytes_per_sample: int,
        export_format: str,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.bytes_per_sample = bytes_per_sample
        self.export_format = export_format.lower()

    def export_async(self, raw_path: str, output_path: str) -> None:
        thread = threading.Thread(
            target=self._convert,
            args=(raw_path, output_path),
            daemon=True,
        )
        thread.start()

    def _convert(self, raw_path: str, output_path: str) -> None:
        log.info(f"Converting {raw_path} to {output_path}")
        try:
            if self.export_format == "mp3":
                self._convert_mp3(raw_path, output_path)
            else:
                self._convert_wav(raw_path, output_path)
        except Exception:
            log.exception(f"Failed conversion for {raw_path}")
            return

        os.unlink(raw_path)
        log.info(f"Finished conversion: {output_path}")

    def _convert_wav(self, raw_path: str, output_path: str) -> None:
        with open(raw_path, "rb") as raw_file, wave.open(output_path, "wb") as wav_file:
            wav_file.setnchannels(self.channels)
            wav_file.setsampwidth(self.bytes_per_sample)
            wav_file.setframerate(self.sample_rate)
            while chunk := raw_file.read(1024 * 1024):
                wav_file.writeframes(chunk)

    def _convert_mp3(self, raw_path: str, output_path: str) -> None:
        cmd = [
            _find_executable("ffmpeg"),
            "-y",
            "-f",
            "s16le",
            "-ar",
            str(self.sample_rate),
            "-ac",
            str(self.channels),
            "-i",
            raw_path,
            "-c:a",
            "libmp3lame",
            "-b:a",
            "320k",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="ignore").strip())


class PCMStreamRecorder:
    """Processes PCM chunks into segmented recording sessions."""

    def __init__(
        self,
        output_dir: str,
        sample_rate: int = 48000,
        silence_threshold_db: float = -50.0,
        min_silence_duration: float = 15.0,
        decay_tail: float = 5.0,
        export_format: str = "wav",
    ):
        self.output_dir = output_dir
        self.sample_rate = sample_rate
        self.export_format = export_format.lower()

        self.chunk_duration = 0.1  # 100ms chunks
        self.channels = 2
        self.bytes_per_sample = 2  # s16le
        self.chunk_size = int(
            self.sample_rate
            * self.chunk_duration
            * self.channels
            * self.bytes_per_sample
        )

        self.rms_threshold = db_to_rms(silence_threshold_db)
        self.silence_chunks_threshold = int(min_silence_duration / self.chunk_duration)
        self.buffer_maxlen = int(decay_tail / self.chunk_duration)

        self.exporter = Exporter(
            sample_rate=self.sample_rate,
            channels=self.channels,
            bytes_per_sample=self.bytes_per_sample,
            export_format=self.export_format,
        )

        self.state = "PASSIVE"
        self.ring_buffer = deque(maxlen=self.buffer_maxlen)
        self.silence_count = 0

        self._raw_path: Optional[str] = None
        self._output_path: Optional[str] = None
        self._raw_file = None

    def reset(self) -> None:
        self.state = "PASSIVE"
        self.ring_buffer.clear()
        self.silence_count = 0

    def process_chunk(self, chunk: bytes) -> None:
        rms = self._calculate_rms(chunk)
        is_silent = rms < self.rms_threshold

        if self.state == "PASSIVE":
            self.ring_buffer.append(chunk)
            if not is_silent:
                buffered_chunks = list(self.ring_buffer)
                log.info(
                    "Audio detected! Transitioning to ACTIVE. "
                    f"RMS={rms:.1f} > {self.rms_threshold:.1f}"
                )
                self.state = "ACTIVE"
                self._open_new_file()
                for buffered_chunk in buffered_chunks:
                    self._raw_file.write(buffered_chunk)
                if not buffered_chunks and self._raw_file:
                    self._raw_file.write(chunk)
                self.ring_buffer.clear()
                self.silence_count = 0
            return

        if self._raw_file:
            self._raw_file.write(chunk)

        if is_silent:
            self.silence_count += 1
            if self.silence_count >= self.silence_chunks_threshold:
                log.info("Continuous silence detected. Transitioning to PASSIVE.")
                self._close_current_file()
                self.state = "PASSIVE"
        else:
            self.silence_count = 0

    def finalize(self) -> None:
        if self.state == "ACTIVE":
            self._close_current_file()
            self.state = "PASSIVE"

    def _open_new_file(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self._raw_path = os.path.join(self.output_dir, f".rb_session_{timestamp}.raw")
        self._output_path = os.path.join(
            self.output_dir,
            f"rb_session_{timestamp}.{self.export_format}",
        )
        self._raw_file = open(self._raw_path, "wb")
        log.info(f"Opened new recording session: {self._raw_path}")

    def _close_current_file(self) -> None:
        if self._raw_file:
            self._raw_file.close()
            self._raw_file = None

        if not self._raw_path or not os.path.exists(self._raw_path):
            self._raw_path = None
            self._output_path = None
            return

        raw_size = os.path.getsize(self._raw_path)
        duration = raw_size / (
            self.sample_rate * self.channels * self.bytes_per_sample
        )
        log.info(f"Raw capture finished: {raw_size} bytes ({duration:.1f}s)")

        if raw_size > 0:
            self.exporter.export_async(self._raw_path, self._output_path)
        else:
            os.unlink(self._raw_path)

        self._raw_path = None
        self._output_path = None

    def _calculate_rms(self, chunk: bytes) -> float:
        audio_samples = array("h", chunk)
        if not audio_samples:
            return 0.0
        sum_squares = sum(sample * sample for sample in audio_samples)
        return math.sqrt(sum_squares / len(audio_samples))
