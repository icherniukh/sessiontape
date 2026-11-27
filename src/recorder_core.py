import glob
import logging
import math
import operator
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from src.config import SUPPORTED_EXPORT_FORMATS
from src.events import (
    Event,
    ExportFailed,
    ExportFinished,
    ExportStarted,
    SegmentClosed,
    SegmentOpened,
)

log = logging.getLogger("sessiontape")

_RAW_RATE_RE = re.compile(r"_(\d+)hz$")


def db_to_rms(db: float) -> float:
    # 0 dBFS = 32768 for 16-bit audio
    return 32768.0 * (10.0 ** (db / 20.0))


def _find_executable(name: str) -> str:
    if hasattr(sys, "_MEIPASS"):
        bundle_path = os.path.join(sys._MEIPASS, name)
        if os.path.exists(bundle_path):
            return bundle_path
        bundle_exe_path = os.path.join(sys._MEIPASS, name + ".exe")
        if os.path.exists(bundle_exe_path):
            return bundle_exe_path

    # When running from a source checkout, prefer the local native capture build so an older
    # system-installed binary on PATH can never shadow workspace changes.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for local_build in [
        os.path.join(repo_root, "mac-capture", ".build", "release", name),
        os.path.join(repo_root, "mac-capture", ".build", "debug", name),
        os.path.join(repo_root, "windows-capture", name),
        os.path.join(repo_root, "windows-capture", f"{name}.exe"),
    ]:
        if os.path.isfile(local_build):
            return local_build

    path = shutil.which(name)
    if path:
        return path
    if sys.platform == "win32":
        exe_name = name if name.lower().endswith(".exe") else f"{name}.exe"
        win_dirs = [
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links"),
            os.path.join(os.environ.get("USERPROFILE", ""), "scoop", "shims"),
            os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "chocolatey", "bin"),
        ]
        for directory in win_dirs:
            if directory:
                candidate = os.path.join(directory, exe_name)
                if os.path.isfile(candidate):
                    return candidate
        return name
    # macOS LaunchAgents often lack standard interactive PATHs
    for fallback in [
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/usr/bin/{name}",
    ]:
        if os.path.isfile(fallback):
            return fallback
    return name


@dataclass
class _ExportJob:
    raw_path: str
    output_path: str
    recovery: bool = False
    sample_rate: Optional[int] = None


class ExportManager:
    def __init__(
        self,
        sample_rate: int,
        channels: int,
        bytes_per_sample: int,
        export_format: str,
        event_sink: Optional[Callable[[Event], None]] = None,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.bytes_per_sample = bytes_per_sample
        if not isinstance(export_format, str):
            raise ValueError("export_format must be a string")
        self.export_format = export_format.lower()
        if self.export_format not in SUPPORTED_EXPORT_FORMATS:
            supported = ", ".join(sorted(SUPPORTED_EXPORT_FORMATS))
            raise ValueError(f"export_format must be one of: {supported}")
        self.event_sink = event_sink
        self._jobs: queue.Queue[Optional[_ExportJob]] = queue.Queue()
        self._worker: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._run,
            name="ExportManager",
            daemon=False,
        )
        self._worker.start()

    def enqueue(
        self,
        raw_path: str,
        output_path: str,
        recovery: bool = False,
        sample_rate: Optional[int] = None,
    ) -> None:
        self.start()
        self._jobs.put(
            _ExportJob(
                raw_path=raw_path,
                output_path=output_path,
                recovery=recovery,
                sample_rate=sample_rate,
            )
        )

    def shutdown(self) -> None:
        if not self._worker:
            return
        self._jobs.put(None)
        self._worker.join()
        self._worker = None

    def _emit(self, event: Event) -> None:
        if self.event_sink:
            self.event_sink(event)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            self._convert(job)

    def _convert(self, job: _ExportJob) -> None:
        raw_path = job.raw_path
        output_path = job.output_path
        log.info(f"Converting {raw_path} to {output_path}")
        self._emit(
            ExportStarted(
                raw_path=raw_path,
                output_path=output_path,
                recovery=job.recovery,
            )
        )
        effective_rate = job.sample_rate or self.sample_rate
        try:
            if self.export_format == "mp3":
                self._convert_mp3(raw_path, output_path, sample_rate=effective_rate)
            else:
                self._convert_wav(raw_path, output_path, sample_rate=effective_rate)
        except Exception as exc:
            log.exception(f"Failed conversion for {raw_path}")
            self._emit(
                ExportFailed(
                    raw_path=raw_path,
                    output_path=output_path,
                    error=str(exc),
                    recovery=job.recovery,
                )
            )
            return

        os.unlink(raw_path)
        log.info(f"Finished conversion: {output_path}")
        self._emit(
            ExportFinished(
                raw_path=raw_path,
                output_path=output_path,
                recovery=job.recovery,
            )
        )

    def _convert_wav(
        self, raw_path: str, output_path: str, sample_rate: Optional[int] = None
    ) -> None:
        effective_rate = sample_rate or self.sample_rate
        with open(raw_path, "rb") as raw_file, wave.open(output_path, "wb") as wav_file:
            wav_file.setnchannels(self.channels)
            wav_file.setsampwidth(self.bytes_per_sample)
            wav_file.setframerate(effective_rate)
            while chunk := raw_file.read(1024 * 1024):
                wav_file.writeframes(chunk)

    def _convert_mp3(
        self, raw_path: str, output_path: str, sample_rate: Optional[int] = None
    ) -> None:
        effective_rate = sample_rate or self.sample_rate
        cmd = [
            _find_executable("ffmpeg"),
            "-y",
            "-f",
            "s16le",
            "-ar",
            str(effective_rate),
            "-ac",
            str(self.channels),
            "-i",
            raw_path,
            "-c:a",
            "libmp3lame",
            "-b:a",
            "320k",
        ]
        # MPEG-1 Layer III caps output sample rate at 48 kHz; downsample 88.2/96/192 kHz taps.
        if effective_rate > 48000:
            cmd.extend(["-ar", "48000"])
        cmd.append(output_path)

        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        result = subprocess.run(cmd, capture_output=True, **kwargs)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="ignore").strip())


class PCMStreamRecorder:
    """Processes PCM chunks into segmented recording sessions."""

    def __init__(
        self,
        output_dir: str,
        on_tap_broken: Optional[Callable[[], None]] = None,
        sample_rate: int = 48000,
        silence_threshold_db: float = -50.0,
        min_silence_duration: float = 15.0,
        min_segment_duration: float = 30.0,
        decay_tail: float = 5.0,
        export_format: str = "wav",
        export_manager: Optional[ExportManager] = None,
        event_sink: Optional[Callable[[Event], None]] = None,
    ):
        self.output_dir = os.path.abspath(os.path.expanduser(output_dir))
        self.on_tap_broken = on_tap_broken
        self.sample_rate = sample_rate
        self.min_segment_duration = min_segment_duration
        if not isinstance(export_format, str):
            raise ValueError("export_format must be a string")
        self.export_format = export_format.lower()
        if self.export_format not in SUPPORTED_EXPORT_FORMATS:
            supported = ", ".join(sorted(SUPPORTED_EXPORT_FORMATS))
            raise ValueError(f"export_format must be one of: {supported}")
        self.event_sink = event_sink

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

        self._owns_export_manager = export_manager is None
        self.export_manager = export_manager or ExportManager(
            sample_rate=self.sample_rate,
            channels=self.channels,
            bytes_per_sample=self.bytes_per_sample,
            export_format=self.export_format,
        )

        self.state = "PASSIVE"
        self.ring_buffer = deque(maxlen=self.buffer_maxlen)
        self.silence_count = 0
        self._chunk_count = 0
        self.last_active_at: float = (
            0.0  # epoch time of last real audio chunk in ACTIVE state
        )
        self._last_rms: float = 0.0

        self._raw_path: Optional[str] = None
        self._output_path: Optional[str] = None
        self._raw_file = None

    def set_sample_rate(self, sample_rate: int) -> None:
        if sample_rate <= 0 or sample_rate == self.sample_rate:
            return
        self.sample_rate = sample_rate
        self.chunk_size = int(
            self.sample_rate
            * self.chunk_duration
            * self.channels
            * self.bytes_per_sample
        )

    def reset(self) -> None:
        self.state = "PASSIVE"
        self.ring_buffer.clear()
        self.silence_count = 0
        self._chunk_count = 0
        self.last_active_at = 0.0
        self._last_rms = 0.0

    def _emit(self, event: Event) -> None:
        if self.event_sink:
            self.event_sink(event)

    def process_chunk(self, chunk: bytes) -> None:
        rms = self._calculate_rms(chunk)
        is_silent = rms < self.rms_threshold

        self._chunk_count += 1
        if self._chunk_count % 100 == 0:  # log every ~10s
            db = 20 * math.log10(rms / 32768.0) if rms > 0 else -math.inf
            log.debug(
                f"[{self.state}] chunk #{self._chunk_count} RMS={rms:.0f} ({db:.1f} dB), threshold={self.rms_threshold:.0f}"
            )

        if self.state == "ACTIVE" and self._last_rms > self.rms_threshold * 10 and is_silent:
            db_before = 20 * math.log10(self._last_rms / 32768.0)
            db_now = 20 * math.log10(rms / 32768.0) if rms > 0 else -math.inf
            log.debug(
                f"Audio dropped to silence ({db_before:.1f} dB -> {db_now:.1f} dB) at chunk #{self._chunk_count}"
            )
        self._last_rms = rms

        if self.state == "PASSIVE":
            self.ring_buffer.append(chunk)
            if not is_silent:
                buffered_chunks = list(self.ring_buffer)
                log.info(
                    "Audio detected! Transitioning to ACTIVE. "
                    f"RMS={rms:.1f} > {self.rms_threshold:.1f}"
                )
                self.state = "ACTIVE"
                self.last_active_at = time.time()
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
                self.last_active_at = time.time()
        else:
            self.silence_count = 0
            self.last_active_at = time.time()  # refresh on every real audio chunk

    def finalize(self) -> None:
        if self.state == "ACTIVE":
            self._close_current_file()
            self.state = "PASSIVE"
        if self._owns_export_manager:
            self.export_manager.shutdown()

    def _open_new_file(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self._raw_path = os.path.join(
            self.output_dir, f".rb_session_{timestamp}_{self.sample_rate}hz.raw"
        )
        self._output_path = os.path.join(
            self.output_dir,
            f"rb_session_{timestamp}.{self.export_format}",
        )
        self._raw_file = open(self._raw_path, "wb", buffering=1024 * 1024)
        log.info(f"Opened new recording session: {self._raw_path}")
        self._emit(
            SegmentOpened(raw_path=self._raw_path, output_path=self._output_path)
        )

    def _close_current_file(self) -> None:
        if self._raw_file:
            self._raw_file.close()
            self._raw_file = None

        if not self._raw_path or not os.path.exists(self._raw_path):
            self._raw_path = None
            self._output_path = None
            return

        raw_size = os.path.getsize(self._raw_path)
        duration = raw_size / (self.sample_rate * self.channels * self.bytes_per_sample)
        log.info(f"Raw capture finished: {raw_size} bytes ({duration:.1f}s)")
        output_path = self._output_path

        if raw_size > 0:
            discarded = duration < self.min_segment_duration
            self._emit(
                SegmentClosed(
                    raw_path=self._raw_path,
                    output_path=output_path,
                    duration_seconds=duration,
                    discarded=discarded,
                )
            )
            if discarded:
                log.info(
                    "Discarding short segment %.1fs < %.1fs: %s",
                    duration,
                    self.min_segment_duration,
                    self._raw_path,
                )
                os.unlink(self._raw_path)
            else:
                self.export_manager.enqueue(
                    self._raw_path, output_path, sample_rate=self.sample_rate
                )
        else:
            self._emit(
                SegmentClosed(
                    raw_path=self._raw_path,
                    output_path=output_path,
                    duration_seconds=duration,
                    discarded=True,
                )
            )
            os.unlink(self._raw_path)

        self._raw_path = None
        self._output_path = None

    def _calculate_rms(self, chunk: bytes) -> float:
        usable = len(chunk) & ~1  # ignore a trailing odd byte
        if usable == 0:
            return 0.0
        samples = memoryview(chunk)[:usable].cast("h")  # s16le; all supported hosts are little-endian
        return math.sqrt(sum(map(operator.mul, samples, samples)) / len(samples))


def recover_orphaned_raw_files(
    output_dir: str,
    sample_rate: int,
    export_format: str,
    export_manager: Optional[ExportManager] = None,
) -> None:
    if not os.path.exists(output_dir):
        return

    # Match both hidden (.rb_session...) and visible just in case
    escaped_dir = glob.escape(output_dir)
    patterns = [
        os.path.join(escaped_dir, ".rb_session_*.raw"),
        os.path.join(escaped_dir, "*.raw"),
    ]
    raw_files = set()
    for p in patterns:
        raw_files.update(glob.glob(p))

    if not raw_files:
        return

    log.info(f"Found {len(raw_files)} orphaned raw files. Starting recovery...")
    exporter = export_manager or ExportManager(
        sample_rate=sample_rate,
        channels=2,
        bytes_per_sample=2,
        export_format=export_format,
    )

    for raw_path in raw_files:
        # Skip files touched in the last 30s — previous process may still be converting them
        age = time.time() - os.path.getmtime(raw_path)
        if age < 30:
            log.info(
                f"Skipping recent raw file (age={age:.0f}s, likely in-flight): {raw_path}"
            )
            continue

        size = os.path.getsize(raw_path)
        if size == 0:
            log.info(f"Deleting empty orphaned file: {raw_path}")
            os.unlink(raw_path)
            continue

        base = os.path.splitext(os.path.basename(raw_path))[0]
        if base.startswith("."):
            base = base[1:]
        rate_match = _RAW_RATE_RE.search(base)
        if rate_match:
            file_rate = int(rate_match.group(1))
            base = base[: rate_match.start()]
        else:
            file_rate = sample_rate

        output_path = os.path.join(output_dir, f"{base}.{export_format}")
        log.info(f"Recovering {raw_path} to {output_path} ({file_rate} Hz)")
        exporter.enqueue(
            raw_path, output_path, recovery=True, sample_rate=file_rate
        )

    if export_manager is None:
        exporter.shutdown()
