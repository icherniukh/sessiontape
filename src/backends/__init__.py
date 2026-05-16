from src.backends.base import CaptureBackend
from src.backends.macos_capture import AudioteeCaptureBackend, MacCaptureBackend
from src.backends.windows_capture import WindowsCaptureBackend

__all__ = [
    "CaptureBackend",
    "MacCaptureBackend",
    "AudioteeCaptureBackend",
    "WindowsCaptureBackend",
]
