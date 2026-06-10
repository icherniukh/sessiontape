import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import wave
from dataclasses import dataclass
from typing import Callable, Optional

from src.audio_format import AudioFormat
from src.config import SUPPORTED_EXPORT_FORMATS
from src.events import Event, ExportFailed, ExportFinished, ExportStarted

log = logging.getLogger("sessiontape")


def _find_executable(name: str) -> str:
    if hasattr(sys, "_MEIPASS"):
        bundle_path = os.path.join(sys._MEIPASS, name)
        if os.path.exists(bundle_path):
            return bundle_path
        bundle_exe_path = os.path.join(sys._MEIPASS, name + ".exe")
        if os.path.exists(bundle_exe_path):
            return bundle_exe_path

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


@dataclass
class _ExportJob:
    raw_path: str
    output_path: str
    recovery: bool = False


class ExportManager:
    def __init__(
        self,
        sample_rate: int,
        channels: int,
        bytes_per_sample: int,
        export_format: str,
        event_sink: Optional[Callable[[Event], None]] = None,
    ):
        self.audio_fmt = AudioFormat(sample_rate, channels, bytes_per_sample)
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

    def enqueue(self, raw_path: str, output_path: str, recovery: bool = False) -> None:
        self.start()
        self._jobs.put(_ExportJob(raw_path=raw_path, output_path=output_path, recovery=recovery))

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
        try:
            if self.export_format == "mp3":
                self._convert_mp3(raw_path, output_path)
            else:
                self._convert_wav(raw_path, output_path)
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

    def _convert_wav(self, raw_path: str, output_path: str) -> None:
        with open(raw_path, "rb") as raw_file, wave.open(output_path, "wb") as wav_file:
            wav_file.setnchannels(self.audio_fmt.channels)
            wav_file.setsampwidth(self.audio_fmt.bytes_per_sample)
            wav_file.setframerate(self.audio_fmt.sample_rate)
            while chunk := raw_file.read(1024 * 1024):
                wav_file.writeframes(chunk)

    def _convert_mp3(self, raw_path: str, output_path: str) -> None:
        cmd = [
            _find_executable("ffmpeg"),
            "-y",
            "-f",
            "s16le",
            "-ar",
            str(self.audio_fmt.sample_rate),
            "-ac",
            str(self.audio_fmt.channels),
            "-i",
            raw_path,
            "-c:a",
            "libmp3lame",
            "-b:a",
            "320k",
            output_path,
        ]

        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        result = subprocess.run(cmd, capture_output=True, **kwargs)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="ignore").strip())
