# SessionTape — Agent Instructions

## Architecture

| File | Role |
|------|------|
| `src/daemon.py` | Orchestrator — drives capture lifecycle on Rekordbox start/stop |
| `src/process_monitor.py` | Polls for Rekordbox process with debounce |
| `src/capture.py` | Reads PCM from backend subprocess, forwards chunks to recorder |
| `src/recorder.py` | Silence detection, state machine, raw session writing |
| `src/exporter.py` | WAV/MP3 conversion queue (async, off hot path) |
| `src/recording_store.py` | Orphaned raw file recovery on startup |
| `src/audio_format.py` | `AudioFormat` dataclass (sample_rate, channels, bytes_per_sample) |
| `src/events.py` | Event types for inter-component communication |
| `src/backends/base.py` | `CaptureBackend` protocol |
| `src/backends/macos_capture.py` | Spawns `mac-capture`, reads PCM from stdout |
| `src/backends/windows_capture.py` | Spawns WASAPI capture process, reads PCM from stdout |
| `windows-capture/main.cpp` | WASAPI capture process — performs per-process audio loopback and streams raw PCM to stdout |
| `mac-capture/` | SCK capture process — performs per-process audio tap and streams raw PCM to stdout (macOS, Swift) |

Python owns orchestration and all PCM processing. Native capture processes handle only the platform-specific audio acquisition layer.

## Dev Setup

```bash
git clone https://github.com/icherniukh/sessiontape.git
cd sessiontape
uv sync
uv run pytest tests/ -v
```

Build native capture processes: `windows-capture/build.ps1` (Windows), Swift Package Manager for `mac-capture/` (macOS).

## Key Decisions [confirmed]

- **WASAPI capture process (Windows)**: `AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK` requires COM activation via `ActivateAudioInterfaceAsync`, which delivers its result through an `IActivateAudioInterfaceCompletionHandler` on a COM apartment thread. Hosting a COM apartment inside Python requires explicit thread affinity management via `pythoncom`/`comtypes` and risks contaminating Python's threading model. A separate process gives clean COM lifetime and avoids GIL contention on the audio hot path.
- **No full-system loopback fallback (Windows)**: Product requirement is Rekordbox audio isolation. Silently falling back captures all system audio on the output device, violating this.
- **Task Scheduler, not Windows Service**: App is per-user and depends on an interactive login session. Services run as SYSTEM with no user audio session.
- **Rekordbox bundle ID**: `com.pioneerdj.rekordboxdj` (not `com.pioneerdj.rekordbox`).
- **Rekordbox spawns multiple processes at startup**: Process monitor must debounce both start and stop detection.
- **AudioCapCLI blocked by TCC**: macOS TCC blocks AudioCapCLI outside interactive terminals. Do not revisit.
- **ProcTap returned zeros; mac-capture works**: The original ProcTap SCK path returned all zeros for Rekordbox. `mac-capture` uses `SCStream` + `SCShareableContent` which does capture Rekordbox audio. These are different SCK APIs.
- **mac-capture routes through AudioTap aggregate device**: Confirmed in `replayd` logs — macOS creates an `AudioTap` aggregate device under `replayd` when `mac-capture` attaches its SCK stream. Switching from `audiotee` to `mac-capture` did not escape this OS-level mechanism.
- **Homebrew tap**: Two formulas — `sessiontape` for stable releases, `sessiontape-preview` for `-rc`/`-beta` tags. Tap: `icherniukh/homebrew-tap`. Tracked in [issue #27](https://github.com/icherniukh/sessiontape/issues/27).

## macOS CoreAudio RT Rules [confirmed — IOProc context]

Violations cause CoreAudio deadline overruns. Discovered by analyzing `audiotee`'s `processAudio` callback.

- **No ARC in callbacks**: Swift `retain`/`release` is not lock-free. Use `Unmanaged._withUnsafeGuaranteedRef` inside IOProc callbacks.
- **No Swift exclusivity checks**: `inout` (`&`) on class properties triggers `swift_beginAccess` which takes locks. Use `UnsafeMutablePointer` for atomic operations instead.
- **No dynamic dispatch in the hot path**: Inline critical path logic or use `final`/`static` to avoid Swift/ObjC method table overhead at RT priority.

## Known Issues

- **Popping/cracking audio (macOS)**: Audio glitches during playback. Under investigation. See RESEARCH.md.
- **ASIO gap (Windows, uninvestigated)**: Current WASAPI process loopback does not capture Rekordbox audio when Rekordbox is configured to output via ASIO (common with DDJ-SX3, DJM-900NXS2, etc.), because ASIO bypasses the Windows shared audio mixer. Device loopback would cover this case but captures all system audio on the output device. No investigation into what ASIO support would require has been done.
- **Code signing (Windows)**: SignPath Foundation rejected the application pending existing user adoption. Currently distributing unsigned MSI with SmartScreen bypass instructions in README. Winget and Chocolatey submissions are gated on signing. Alternative: Azure Trusted Signing (~$10/month, no adoption requirement).
- **`sessiontape-preview` Homebrew formula**: Needs to be created in `icherniukh/homebrew-tap` repo. [Issue #27](https://github.com/icherniukh/sessiontape/issues/27)
- **Full lifecycle test**: Splitter works on synthetic audio; a clean play → silence → play → quit test with real Rekordbox has not been completed cleanly.
- **LaunchAgent**: `install/dev.icherniukh.sessiontape.plist` and `scripts/install.sh` exist but have not been tested with login-time auto-start.
- **Long session stability**: Not tested with multi-hour DJ sets. Disk usage ≈ 184 KB/s (660 MB/hour).
- **Terminal popups during tests**: Even with `CREATE_NO_WINDOW`, the test suite occasionally spawns empty terminal windows on Windows. [Issue #21](https://github.com/icherniukh/sessiontape/issues/21)
- **Permissions UX**: Screen Recording permission must be granted manually on macOS. No guided setup flow.

## Issue Tracking

```bash
gh issue list
gh issue view <num>
gh issue create
gh issue close <num>
```
