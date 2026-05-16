# Windows Support Design

## Status

Draft

Current repo state:

- `src/recorder_core.py` now owns PCM chunk processing, silence splitting, and export.
- `src/capture.py` is reduced to the macOS `audiotee` subprocess wrapper.
- `src/process_monitor.py` uses `psutil` instead of `pgrep`.
- The test suite includes subprocess-driven integration tests using `tests/fixtures/fake_audiotee.py`.

## Goal

Add Windows support to `auto-rb-recorder` without regressing the current macOS recording path and without introducing refactors that materially hurt throughput, latency, or idle resource usage.

The Windows implementation should preserve the current product behavior:

- Start recording when Rekordbox launches.
- Capture only Rekordbox audio, not full-system audio.
- Split sessions on prolonged silence.
- Export to `wav` or `mp3`.
- Run unattended in the background after user login.

## Non-Goals

- Supporting Linux.
- Replacing the existing macOS capture backend.
- Building a generic cross-platform audio framework beyond what this app needs.
- Solving ASIO-specific capture if Rekordbox bypasses the Windows shared audio engine.
- Adding a GUI.

## Performance Constraints

Any refactor done to support Windows must satisfy these constraints:

1. The hot path remains chunk-based and streaming. No full-session buffering in memory.
2. Silence detection continues to operate on fixed-size PCM chunks with O(n) work per chunk and no additional per-sample object allocation.
3. Platform abstraction must not add extra copies of PCM data beyond what is already required for subprocess/stdout transport.
4. The recorder must continue writing raw session data incrementally to disk while active.
5. Export must remain asynchronous relative to live capture.
6. Idle overhead must stay low. When Rekordbox is not producing audio, the process should remain mostly polling-bound, not CPU-bound.
7. Cross-platform support must not slow the macOS path by routing hot-path audio through dynamic plugin loading, reflection-heavy dispatch, or large intermediate adapters.

## Current Architecture

The current codebase mixes platform-neutral session logic with macOS-specific process and audio capture assumptions:

- `src/daemon.py`
  - Creates a `ProcessMonitor`.
  - Starts `AudioCapture` on Rekordbox start.
  - Stops capture on Rekordbox exit.
- `src/process_monitor.py`
  - Uses `pgrep` to find a matching process by name.
  - Debounces startup and shutdown.
- `src/capture.py`
  - Launches `audiotee`.
  - Reads `s16le` stereo PCM from `stdout`.
  - Performs silence detection.
  - Maintains a decay-tail ring buffer.
  - Writes `.raw` session files.
  - Converts them asynchronously with `ffmpeg`.

This split is useful, but the current `AudioCapture` class combines two different responsibilities:

- platform-specific PCM acquisition
- platform-neutral session segmentation and export

That coupling makes a Windows port harder than it needs to be.

## Design Summary

Refactor the code into a small cross-platform core and thin platform backends:

- A platform-neutral recorder core that consumes PCM chunks.
- A platform-neutral process monitor API.
- A macOS backend that preserves the current `audiotee` subprocess model.
- A Windows backend that uses a small native helper executable for per-process loopback capture.

The design intentionally keeps Python out of the Windows audio API hot path. Python remains responsible for orchestration, chunk processing, segmentation, and export. Native code is used only where Windows audio capture is inherently platform-specific.

## Why A Native Windows Helper

Windows per-process audio capture is the hard part. The closest analogue to the existing macOS implementation is WASAPI process loopback capture.

Using a small native helper has these advantages:

- Matches the existing `audiotee -> stdout PCM` contract.
- Minimizes changes to the Python recorder flow.
- Keeps COM, activation, and device details out of the main app.
- Avoids building a Python-only capture path around `ctypes` or `pywin32` in the performance-sensitive path.
- Lets the Windows backend fail fast with a clear compatibility error on unsupported Windows builds.

This is the lowest-risk way to add Windows support while preserving the current architecture's streaming behavior.

## Proposed Architecture

### 1. Recorder Core

Introduce a platform-neutral recorder component that owns:

- chunk size and audio format assumptions
- RMS calculation
- silence thresholding
- passive/active state machine
- decay-tail ring buffer
- raw file management
- asynchronous export

Proposed module shape:

- `src/recorder_core.py`
  - `PCMStreamRecorder`
  - `Exporter`

`PCMStreamRecorder` should accept byte chunks in the existing `s16le`, stereo, fixed sample rate format and perform the same logic currently implemented in `AudioCapture`.

This refactor must preserve:

- 100ms chunking
- `array("h", chunk)`-style numeric processing or equivalent
- ring-buffer semantics
- async export behavior

### 2. Capture Backend Interface

Introduce a small backend contract:

```python
class CaptureBackend(Protocol):
    def start(self, pid: int) -> BinaryIO: ...
    def stop(self) -> None: ...
```

In practice the current implementation can still use a subprocess with `stdout`, but the daemon should no longer know which tool provides the bytes.

Proposed modules:

- `src/backends/base.py`
- `src/backends/macos_capture.py`
- `src/backends/windows_capture.py`

`AudioCapture` can either become a thin coordinator over `CaptureBackend + PCMStreamRecorder`, or be replaced by a new orchestration class. The important part is that PCM session logic is no longer platform-specific.

### 3. Process Detection

Replace the shell-based `pgrep` implementation with a cross-platform Python implementation using `psutil`.

Reasons:

- removes one OS-specific shell dependency
- avoids parsing shell output
- simplifies Windows support
- keeps debounce behavior intact

Proposed modules:

- `src/process_monitor.py`
  - keep the debounce state machine
  - replace `_find_pid()` implementation

The interface should stay simple: find the current Rekordbox PID, then debounce start/stop transitions as it already does.

### 4. Platform Factory

Add a small runtime selection layer in the entrypoint or a dedicated module:

- detect `sys.platform`
- construct the right capture backend
- apply platform-specific default paths and process names

Proposed module:

- `src/platform.py` or `src/factory.py`

This should be cold-path code only.

## Windows Capture Backend

### Backend Choice

Preferred approach: a native helper executable, tentatively `rb-capture-win.exe`, that:

- accepts a target PID
- captures only that process tree's rendered audio using Windows process loopback APIs
- emits raw PCM to `stdout`
- writes logs to `stderr`
- supports clean termination by parent process

This mirrors the existing `audiotee` integration model closely enough that the Python side can remain simple.

### Contract

The helper should provide:

- `s16le`
- stereo
- configured sample rate where possible
- chunked `stdout` output
- stable exit codes
- a useful error when process loopback is unavailable or unsupported

If sample rate conversion must happen in the helper, it should be done there once. The Python side should continue to assume a stable input format.

### Windows Compatibility

The initial target should require Windows support for process loopback capture. The app must detect unsupported systems at runtime and fail with a clear message instead of silently degrading to full-system capture.

We should not automatically fall back to full-system loopback capture because that breaks the product requirement of isolating Rekordbox audio.

### Important Risk

If Rekordbox on Windows renders through ASIO or another path that bypasses the shared audio engine, WASAPI process loopback may not see the audio stream. This must be validated early with a prototype before the full port is implemented.

## Export Path

The current exporter shells out to `ffmpeg` for both `wav` and `mp3`.

For Windows support, the exporter should be split into:

- native Python WAV export using the standard library when input is already PCM
- `ffmpeg` only for MP3

Reasons:

- reduces Windows packaging complexity
- removes one external dependency for the default format
- avoids carrying a full `ffmpeg` dependency just to wrap PCM into WAV

This is also a net simplification for macOS and does not hurt runtime performance because export is already asynchronous and off the recording hot path.

## Config and Paths

Current config and output defaults are Unix-specific.

Required changes:

- replace hardcoded `~/.config/rb-recorder/config.toml`
- replace hardcoded output path logic with platform-aware defaults

Preferred library:

- `platformdirs`

Expected defaults:

- macOS config:
  - `~/.config/rb-recorder/config.toml` can remain supported for compatibility
- Windows config:
  - `%APPDATA%/rb-recorder/config.toml`
- macOS output:
  - `~/Music/auto-rb-recorder`
- Windows output:
  - `%USERPROFILE%/Music/auto-rb-recorder`

Backward compatibility matters here. Existing macOS users should not be broken by the path refactor.

## Startup / Background Execution

Windows should use Task Scheduler, not a Windows Service.

Reasons:

- the app is a per-user background recorder
- it depends on an interactive logged-in user session
- output and config are user-scoped
- Task Scheduler is simpler to install and diagnose

Planned deliverables:

- `scripts/install-windows.ps1`
- `scripts/uninstall-windows.ps1`
- documentation for creating or updating the scheduled task

## Packaging

Windows packaging should mirror the macOS packaging philosophy: ship a single executable app bundle where practical, with the capture helper included alongside the main binary.

Expected build changes:

- add a Windows PyInstaller target
- include `rb-capture-win.exe` as a bundled binary
- include `ffmpeg.exe` only if MP3 export is enabled in distributed builds

Potential deliverables:

- `scripts/build-windows.ps1`
- PyInstaller spec updates for platform-specific bundles

## Testing Strategy

### Unit Tests

Add unit tests for:

- `PCMStreamRecorder` state transitions
- ring buffer behavior
- silent-to-active and active-to-passive transitions
- export dispatch logic
- `psutil`-based process detection
- platform factory selection

The existing tests in `tests/test_capture.py`, `tests/test_process_monitor.py`, and `tests/test_daemon.py` should be reshaped so most logic is backend-agnostic.

### Integration Tests

Add backend-level tests that mock subprocess/stdout behavior:

- macOS backend starts `audiotee` correctly
- Windows backend starts `rb-capture-win.exe` correctly
- stop path terminates subprocesses cleanly

### Manual Validation Matrix

Both macOS and Windows need end-to-end validation for:

1. Rekordbox start -> active playback -> Rekordbox exit
2. Rekordbox start -> silence -> playback -> silence -> exit
3. Rekordbox restart during debounce window
4. Another app playing audio while Rekordbox is active
5. Long-running session
6. Crash or forced kill during active recording

Windows-specific validation:

1. supported Windows version with shared-engine output
2. unsupported Windows version
3. Rekordbox configured for ASIO, if applicable
4. login-time auto-start via Task Scheduler

## Rollout Plan

### Phase 1: Core Refactor

- Extract recorder core from `src/capture.py`.
- Keep macOS behavior functionally unchanged.
- Replace shell process lookup with `psutil`.
- Preserve existing tests or migrate them with equivalent coverage.

Exit criteria:

- macOS behavior unchanged
- no measurable regression in chunk processing or idle behavior

### Phase 2: Windows Capture Prototype

- Build a minimal native helper that captures one target process to `stdout`.
- Prove Rekordbox audio is actually capturable on Windows.
- Validate process identity assumptions and child-process behavior.

Exit criteria:

- successful real capture from Rekordbox on a supported Windows system
- confirmation that isolation from non-Rekordbox audio works

### Phase 3: Windows App Integration

- add Windows backend
- add platform-aware config defaults
- add packaging
- add install/uninstall scripts

Exit criteria:

- packaged Windows build records real sessions end to end

### Phase 4: Hardening

- improve error reporting
- recover orphaned raw files on startup
- add more real-world validation
- document known limitations clearly

## Alternatives Considered

### Python-only Windows audio capture

Rejected for initial implementation.

Reason:

- high complexity in COM and audio client setup
- more fragile in the hot path
- harder to debug and package

### Full-system loopback on Windows

Rejected.

Reason:

- violates the product requirement to isolate Rekordbox audio

### Large cross-platform abstraction layer

Rejected.

Reason:

- adds design weight without solving the actual difficult problem
- risks performance regressions through over-generalization

## Open Questions

1. What is the canonical Rekordbox executable name on Windows, and does it spawn child processes that own audio rendering?
2. Does Rekordbox on Windows render through a path visible to WASAPI process loopback under its common configurations?
3. Should MP3 export be optional at packaging time to avoid bundling `ffmpeg` in the default Windows release?
4. Should startup recovery of orphaned `.raw` files be folded into this refactor or kept separate?

## Initial File Plan

Expected new or changed files:

- `src/recorder_core.py`
- `src/backends/base.py`
- `src/backends/macos_capture.py`
- `src/backends/windows_capture.py`
- `src/process_monitor.py`
- `src/daemon.py`
- `src/__main__.py`
- `src/config.py`
- `tests/test_recorder_core.py`
- `tests/test_process_monitor.py`
- `tests/test_daemon.py`
- `scripts/build-windows.ps1`
- `scripts/install-windows.ps1`
- `docs/windows-support-design.md`

## References

- Microsoft Learn: `AUDIOCLIENT_ACTIVATION_TYPE`
  - https://learn.microsoft.com/en-us/windows/win32/api/audioclientactivationparams/ne-audioclientactivationparams-audioclient_activation_type
- Microsoft Learn: loopback recording overview
  - https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording
- Microsoft Windows sample: Application Loopback Audio Capture Sample
  - https://learn.microsoft.com/en-us/samples/microsoft/windows-classic-samples/applicationloopbackaudio-sample/
