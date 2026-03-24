import logging
import subprocess
import os

from src.backends.base import CaptureBackend
from src.recorder_core import _find_executable

log = logging.getLogger("rb-recorder")


class WindowsCaptureBackend(CaptureBackend):
    def start(self, pid: int, sample_rate: int) -> subprocess.Popen:
        exe = _find_executable("rb-capture-win.exe")
        cmd = [exe, "--pid", str(pid), "--sample-rate", str(sample_rate)]
        log.info(f"Launching capture helper: {' '.join(cmd)}")
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def stop(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
