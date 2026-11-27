import os
import sys

from src.backends.base import CaptureBackend
from src.backends.macos_capture import MacCaptureOptions


def get_platform_backend(
    backend_name: str | None = None,
    *,
    mac_options: MacCaptureOptions | None = None,
    capture_diag: bool = False,
) -> CaptureBackend:
    selected = (
        (backend_name or os.environ.get("SESSIONTAPE_CAPTURE_BACKEND") or "auto")
        .strip()
        .lower()
    )

    if sys.platform == "win32":
        if selected not in {
            "auto",
            "windows",
            "sessiontape-capture-win",
            "sessiontape-capture-win.exe",
        }:
            raise ValueError(f"Unsupported capture backend {selected!r} on Windows")
        from src.backends.windows_capture import WindowsCaptureBackend

        diag = capture_diag or (mac_options.diag if mac_options else False)
        metrics = mac_options.metrics if mac_options is not None else True
        return WindowsCaptureBackend(diag=diag, metrics=metrics)
    elif sys.platform == "darwin":
        from src.backends.macos_capture import MacCaptureBackend

        if selected in {"auto", "mac-capture"}:
            return MacCaptureBackend(mac_options)
        raise ValueError(f"Unsupported capture backend {selected!r} on macOS")
    else:
        raise NotImplementedError(f"Platform {sys.platform} is not supported")
