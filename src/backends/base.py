from typing import Protocol
import subprocess

class CaptureBackend(Protocol):
    def start(self, pid: int, sample_rate: int) -> subprocess.Popen:
        pass

    def stop(self, proc: subprocess.Popen) -> None:
        pass
