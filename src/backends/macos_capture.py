import logging
import subprocess
import os
import sys

from src.backends.base import CaptureBackend
from src.recorder_core import _find_executable

log = logging.getLogger("rb-recorder")


class AudioteeCaptureBackend(CaptureBackend):
    def start(self, pid: int, sample_rate: int) -> subprocess.Popen:
        return subprocess.Popen(
            [
                _find_executable("audiotee"),
                "--include-processes",
                str(pid),
                "--sample-rate",
                str(sample_rate),
                "--stereo",
                "--flush",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def stop(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.warning(f"audiotee (PID {proc.pid}) did not exit after SIGTERM — sending SIGKILL")
            proc.kill()
            proc.wait()
