import os
import sys
from src.backends.base import CaptureBackend


def get_platform_backend(backend_name: str | None = None, capture_debug: bool = False) -> CaptureBackend:
    selected = (backend_name or os.environ.get("SESSIONTAPE_CAPTURE_BACKEND") or "auto").strip().lower()

    if sys.platform == "win32":
        if selected not in {"auto", "windows", "sessiontape-capture-win", "sessiontape-capture-win.exe"}:
            raise ValueError(
                f"Unsupported capture backend {selected!r} on Windows"
            )
        from src.backends.windows_capture import WindowsCaptureBackend
        return WindowsCaptureBackend(capture_debug=capture_debug)
    elif sys.platform == "darwin":
        from src.backends.macos_capture import MacCaptureBackend
        if selected in {"auto", "mac-capture"}:
            return MacCaptureBackend()
        raise ValueError(f"Unsupported capture backend {selected!r} on macOS")
    else:
        raise NotImplementedError(f"Platform {sys.platform} is not supported")
