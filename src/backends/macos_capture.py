import logging
import subprocess
from dataclasses import dataclass

from src.backends.base import CaptureBackend
from src.recorder_core import _find_executable

log = logging.getLogger("sessiontape")


@dataclass(frozen=True)
class MacCaptureOptions:
    """mac-capture CLI options, set from the [capture] config section."""

    backend_mode: str = "auto"
    metrics: bool = True
    diag: bool = False
    log_path: str = ""

    def cli_args(self) -> list[str]:
        args: list[str] = []
        if self.backend_mode and self.backend_mode != "auto":
            args.extend(["--backend", self.backend_mode])
        if self.diag:
            args.append("--diag")
        if not self.metrics:
            args.append("--no-metrics")
        if self.log_path:
            args.extend(["--log", self.log_path])
        return args


class MacCaptureBackend(CaptureBackend):
    def __init__(self, options: MacCaptureOptions | None = None) -> None:
        self._options = options or MacCaptureOptions()

    def start(self, pid: int, sample_rate: int) -> subprocess.Popen:
        cmd = [
            _find_executable("mac-capture"),
            str(pid),
            str(sample_rate),
            *self._options.cli_args(),
        ]

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
