import logging
import subprocess
import sys

from src.backends.base import CaptureBackend
from src.exporter import _find_executable

# WASAPI process loopback (AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK) requires COM activation
# via ActivateAudioInterfaceAsync, which delivers its result through
# IActivateAudioInterfaceCompletionHandler on a COM apartment thread. Hosting a COM apartment in
# Python requires explicit MTA/STA thread affinity management via pythoncom/comtypes, which
# conflicts with Python's threading model. A separate process gives clean COM lifetime and avoids
# GIL contention on the audio capture hot path.

log = logging.getLogger("sessiontape")


class WindowsCaptureBackend(CaptureBackend):
    def __init__(self, capture_debug: bool = False) -> None:
        self._capture_debug = capture_debug

    def start(self, pid: int, sample_rate: int) -> subprocess.Popen:
        exe = _find_executable("sessiontape-capture-win.exe")
        cmd = [exe, "--pid", str(pid), "--sample-rate", str(sample_rate)]
        if self._capture_debug:
            cmd.append("--debug")
        log.info(f"Launching capture process: {' '.join(cmd)}")
        
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
