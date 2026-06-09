import logging
import subprocess
import sys

from src.backends.base import CaptureBackend
from src.recorder_core import _find_executable

log = logging.getLogger("sessiontape")


class WindowsCaptureBackend(CaptureBackend):
    def start(self, pid: int, sample_rate: int) -> subprocess.Popen:
        exe = _find_executable("sessiontape-capture-win.exe")
        cmd = [exe, "--pid", str(pid), "--sample-rate", str(sample_rate)]
        log.info(f"Launching capture helper: {' '.join(cmd)}")
        
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs
        )

    def stop(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None and proc.stdin:
            try:
                proc.stdin.close()
            except OSError:
                pass

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
