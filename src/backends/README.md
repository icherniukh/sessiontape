# Capture Backends

This directory contains the platform-specific audio capture backends for `sessiontape`.

## Architecture

The recorder relies on a platform-neutral core (`PCMStreamRecorder`) and delegates raw PCM audio acquisition to platform-specific backends:

* `base.py`: Defines the `CaptureBackend` protocol.
* `macos_capture.py`: macOS backend invoking `mac-capture` (native Swift CoreAudio process tap with ScreenCaptureKit fallback).
* `windows_capture.py`: Windows backend invoking `sessiontape-capture-win.exe` (native C++ WASAPI process loopback capture) via a subprocess.

## Windows Design Notes

The Windows implementation avoids COM/WASAPI audio API calls directly in Python to keep hot-path overhead minimal. `sessiontape-capture-win.exe` performs per-process WASAPI loopback capture (`PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE`), converts frames to stereo `s16le`, streams raw PCM to `stdout`, and emits structured diagnostics and `CAPTURE` metrics on `stderr`.

Python handles process monitoring, silence detection, decay-tail ring buffering, `.raw` segment writing, and WAV/MP3 export.

## Collecting Debug Logs on Windows

1. Enable verbose daemon logging with `-v` or set `mac_capture_diag = true` under `[capture]` in `%APPDATA%\sessiontape\config.toml` (which passes `--debug` to `sessiontape-capture-win.exe`):
   ```powershell
   sessiontape -v
   ```
2. Inspect `%APPDATA%\sessiontape\daemon.log`. Native `sessiontape-capture-win.exe` `stderr` lines (`INFO:`, `WARNING:`, `ERROR:`, `DEBUG:`, and `CAPTURE` metrics) are routed automatically into `daemon.log`.
