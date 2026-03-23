# Capture Backends

This directory contains the platform-specific audio capture backends for `auto-rb-recorder`.

## Architecture

The recorder relies on a platform-neutral core (`PCMStreamRecorder`) and delegates the actual raw PCM audio acquisition to platform-specific backends.

* `base.py`: Defines the `CaptureBackend` protocol.
* `macos_capture.py`: macOS backend utilizing `audiotee` via a subprocess.
* `windows_capture.py`: Windows backend utilizing `rb-capture-win.exe` (a native Windows WASAPI loopback capture helper) via a subprocess.

## Windows Design Notes

The Windows implementation avoids doing heavy Windows COM/WASAPI audio API calls directly in Python to maintain low overhead in the hot path. Instead, it relies on a small native helper executable `rb-capture-win.exe` which performs process-level loopback capture and streams raw PCM (s16le) to stdout.

This design mirrors the macOS `audiotee` implementation. Python remains responsible for orchestration, silence detection, ring buffering, and export.

## Testing on Windows

To test the Windows capture backend:

1. **Build the executable**: 
   Run `scripts/build-windows.ps1` to compile the Windows standalone executable. This will bundle the main Python script and its dependencies.
2. **Ensure the Native Helper is present**:
   The `rb-capture-win.exe` needs to be in the same directory or accessible via PATH.
3. **Run the recorder**:
   You can run the built executable from `dist\auto-rb-recorder.exe` or run the python script directly.

### Collecting Debug Logs

To troubleshoot issues with the Windows capture backend, you can collect debug logs:

1. Run the script or the built executable with the environment variable `LOG_LEVEL=DEBUG` or standard logging configured.
   ```powershell
   $env:LOG_LEVEL="DEBUG"
   .\dist\auto-rb-recorder.exe
   ```
2. **Native Helper Logs**: The `rb-capture-win.exe` helper outputs errors or diagnostic information to `stderr`. The current `WindowsCaptureBackend` suppresses `stderr` by default (`stderr=subprocess.DEVNULL`). To capture native helper logs during active debugging, modify `src/backends/windows_capture.py` locally to remove `stderr=subprocess.DEVNULL` so the helper's output reaches the main console.
3. Ensure Rekordbox is running and check the Python console output to confirm transitions between `PASSIVE` and `ACTIVE` states based on the RMS levels.
