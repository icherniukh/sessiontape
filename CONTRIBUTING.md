# Contributing to sessiontape

## Architecture

The recorder uses a platform-neutral core with platform-specific capture backends:

- `src/capture.py` — orchestrates capture sessions: starts a backend subprocess, reads PCM from stdout, and forwards it to the recorder core.
- `src/recorder_core.py` — PCM chunk processing, silence-based splitting, raw session writing, and export.
- `src/process_monitor.py` — Rekordbox process detection with debounce logic.
- `src/backends/base.py` — `CaptureBackend` protocol that all platform backends implement.
- `src/backends/macos_capture.py` — macOS backend using `mac-capture` (native Swift CoreAudio process tap).
- `src/backends/windows_capture.py` — Windows backend using `sessiontape-capture-win.exe` (native WASAPI loopback helper).

WAV export is written directly from PCM data. MP3 export shells out to `ffmpeg`.

## Development setup

```bash
git clone https://github.com/icherniukh/sessiontape.git
cd sessiontape
uv sync
```

## Running tests

```bash
uv run pytest
```

The test suite includes a subprocess-driven integration harness that feeds deterministic PCM through a fake capture backend and verifies real on-disk WAV output.

## Configuration

Default settings live in `config.default.toml`. At runtime the recorder loads user overrides from `%APPDATA%\sessiontape\config.toml` (Windows) or `~/Library/Application Support/sessiontape/config.toml` (macOS).
