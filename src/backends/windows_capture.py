import subprocess
import os

from src.backends.base import CaptureBackend
from src.recorder_core import _find_executable


class WindowsCaptureBackend(CaptureBackend):
    def start(self, pid: int, sample_rate: int) -> subprocess.Popen:
        return subprocess.Popen(
            [
                _find_executable("rb-capture-win.exe"),
                "--pid",
                str(pid),
                "--sample-rate",
                str(sample_rate),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def stop(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
