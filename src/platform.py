import sys
from src.backends.base import CaptureBackend

def get_platform_backend() -> CaptureBackend:
    if sys.platform == "win32":
        from src.backends.windows_capture import WindowsCaptureBackend
        return WindowsCaptureBackend()
    elif sys.platform == "darwin":
        from src.backends.macos_capture import AudioteeCaptureBackend
        return AudioteeCaptureBackend()
    else:
        raise NotImplementedError(f"Platform {sys.platform} is not supported")
