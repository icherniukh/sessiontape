import os
import sys

from src.backends.base import CaptureBackend


def get_platform_backend(
    backend_name: str | None = None,
    *,
    mac_capture_backend: str = "auto",
    mac_capture_metrics: bool = True,
    mac_capture_diag: bool = False,
    mac_capture_log: str = "",
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

        return WindowsCaptureBackend()
    elif sys.platform == "darwin":
        from src.backends.macos_capture import MacCaptureBackend

        if selected in {"auto", "mac-capture"}:
            return MacCaptureBackend(
                backend_mode=mac_capture_backend,
                metrics=mac_capture_metrics,
                diag=mac_capture_diag,
                log_path=mac_capture_log,
            )
        raise ValueError(f"Unsupported capture backend {selected!r} on macOS")
    else:
        raise NotImplementedError(f"Platform {sys.platform} is not supported")
