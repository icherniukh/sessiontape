import logging
import subprocess

from src.backends.base import CaptureBackend
from src.recorder_core import _find_executable

log = logging.getLogger("sessiontape")

class MacCaptureBackend(CaptureBackend):
    def __init__(
        self,
        *,
        backend_mode: str = "auto",
        metrics: bool = True,
        diag: bool = False,
        log_path: str = "",
    ) -> None:
        self._backend_mode = backend_mode
        self._metrics = metrics
        self._diag = diag
        self._log_path = log_path

    def start(self, pid: int, sample_rate: int) -> subprocess.Popen:
        cmd = [
            _find_executable("mac-capture"),
            str(pid),
            str(sample_rate),
        ]
        if self._backend_mode and self._backend_mode != "auto":
            cmd.extend(["--backend", self._backend_mode])
        if self._diag:
            cmd.append("--diag")
        if not self._metrics:
            cmd.append("--no-metrics")
        if self._log_path:
            cmd.extend(["--log", self._log_path])

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return proc

    def stop(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.warning(
                f"mac-capture (PID {proc.pid}) did not exit after SIGTERM — sending SIGKILL"
            )
            proc.kill()
            proc.wait()
