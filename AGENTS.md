# SessionTape — Agent Instructions

## Known Issues / Not Yet Verified

- **Full lifecycle with music + silence gap** — the splitter works on synthetic audio, but a real test with play→silence→play→quit hasn't been completed clean yet (Rekordbox startup cycle caused short captures in testing).
- **Long session stability** — not tested with multi-hour DJ sets. Disk space usage is ~184KB/s (660MB/hour).
- **Terminal popups during tests** — Even with `CREATE_NO_WINDOW` flags, the test suite occasionally spawns empty terminal windows on Windows. Tracked in [Issue #21](https://github.com/icherniukh/sessiontape/issues/21).
- **Permissions UX** — On macOS, **System Audio Recording** (`System Settings > Privacy & Security > Audio & Video Recording`, for the default CoreAudio tap) and **Screen Recording** (for the `--sck` ScreenCaptureKit fallback) must be granted manually. CoreAudio taps return `noErr` and deliver `0.0f` silence when System Audio Recording is denied (`mac-capture` logs a warning after 60 s of continuous zeros). No guided setup flow yet.

## Runtime Service, Releases & Git Workflow

- **Never leave `brew services` stopped after testing:** Before running `brew services stop sessiontape` (e.g., to avoid double-tapping during `scripts/tap-ab.sh`), note that the background service is active. Always restart and verify it (`brew services start sessiontape`, `brew services list`, and a fresh `--- sessiontape started · PID ... ---` entry in `~/Library/Application Support/sessiontape/daemon.log`) before ending work.
- **Reinstalling the local app after changes or releases:**
  - **After a new GitHub release (`vX.Y.Z`) completes CI:** Homebrew does not auto-upgrade the running service. Run:
    ```bash
    brew update && brew upgrade sessiontape && brew services restart sessiontape
    ```
    Then confirm `brew info sessiontape` shows the new version and `daemon.log` reports a fresh startup PID.
  - **Deploying local workspace changes before a release is published:** `brew services` runs `$(brew --prefix sessiontape)/bin/sessiontape` (a standalone PyInstaller binary bundling `mac-capture`), NOT the git checkout directly. Rebuild and install locally with:
    ```bash
    ./scripts/build.sh --full --install
    ```
    (Do not pass `CODESIGN_IDENTITY="Developer ID Application"` from non-interactive agent shells—macOS Keychain blocks `codesign` waiting for an interactive password prompt unless the key partition list is unlocked. Official GitHub Actions releases sign with `Developer ID Application` and `--identifier dev.icherniukh.sessiontape`; for local ad-hoc builds, verify in `daemon.log` that audio capture is permitted in `System Settings > Privacy & Security > Audio & Video Recording`.)
- **Git commits vs. pushing:** Commit verified work locally in logical Conventional Commits (`type(scope): summary`) without waiting for confirmation—never leave completed work uncommitted on a dirty tree. Exclude untracked local agent/scratch directories (`.agents/`, `.commandcode/`, `.kiro/`, `.pi/`, `NEW/`, `skills-lock.json`). **Only `git push` (and publishing remote tags/releases) requires explicit user supervision.**

## Issue Tracking

This project uses **GitHub Issues** for issue tracking.

```bash
gh issue list         # Find available work
gh issue view <num>   # View issue details
gh issue create       # Create a new issue
gh issue close <num>  # Close an issue
```

## Agent Skills (macOS audio/capture)

- **`axiom-macos`** — ScreenCaptureKit pipeline discipline (`skills/screencapturekit.md`). Installed in `.agents/skills/`.
- **`macos-core-audio-io`** — HAL aggregates, replayd overload diagnosis, IOProc RT rules, `CAPTURE` metrics. Lives in `~/proj/agentify/skills/`, symlinked into `.agents/skills/`.

## Key Decisions & Pitfalls

- **Product rename decision: SessionTape** — The project has been renamed from `auto-rb-recorder` to **SessionTape** before public winget/Chocolatey/App Store submissions harden the old identity. Public identifiers should use `sessiontape` where package-manager conventions allow it (CLI/binary/package/config dir), with display name `SessionTape`.
- **AudioCapCLI doesn't work from scripts** — macOS TCC blocks it outside interactive terminals. Don't switch back to it.
- **Capture backends (macOS)** — `mac-capture` has two backends: ScreenCaptureKit (SCK, `SCStream`) and a CoreAudio process tap (`CATapDescription`). Selected with `--backend tap|sck|auto` (also `--tap`, `--sck`); default `auto` uses the tap and falls back to SCK if the tap fails to start (or on macOS < 14.2). Python passes `--backend` only when the mode is not `auto` (`src/backends/macos_capture.py`).
  - `[capture]` config keys: `mac_capture_backend` (`"auto"`, `"tap"`, `"sck"`), `perf_metrics` (Python `CAPTURE` read-loop lines), `mac_capture_metrics` (Swift callback `CAPTURE` lines), `mac_capture_diag` (verbose per-buffer logs), `mac_capture_log` (optional diagnostic log path).
  - **Native sample-rate adoption & restart contract:** `CoreAudioTapCapture` adopts the tap's native sample rate (`TAP_FORMAT rate=<rate> channels=<ch>` on stderr) rather than exiting on mismatch, and `PCMStreamRecorder` / `ExportManager` stamp each segment (and `.<rate>hz.raw` orphan recovery file) with its recorded rate (`recording.sample_rate` still governs Windows WASAPI and `--sck`). Exit code `4` triggers a windowed daemon restart (max 5 per 10 minutes) on mid-session `FORMAT_CHANGED`, `IO_STOPPED_ABNORMALLY`, `AUDIO_SERVICE_RESTARTED` (`coreaudiod` restart), or `TAP_STALLED` (>5 s without an IO callback). Legacy exit code `3` (`FORMAT_MISMATCH`) remains handled as non-restartable for older binaries.
  - **Confirmed baseline vs. tap overhead:** Rekordbox 7 at a `512`-sample (`10.7 ms`) buffer on `BuiltInSpeakerDevice` produces occasional `HALC_ProxyIOContext::IOWorkLoop: skipping cycle due to overload` glitches even when SessionTape is **not** running (observed `2026-11-24` and `2026-11-27 03:43:22`/`03:43:32` on AC power, `wg_system_time_mach = 31.2 ms` and `13.6 ms`). Raising the private tap aggregate buffer to `4096` frames with `drift=0` removes steady-state `[ahal_dsp]` resampling overhead; tap-vs-SCK scrub A/B remains tracked in [Issue #33](https://github.com/icherniukh/sessiontape/issues/33). See [docs/capture-popping-analysis.md](docs/capture-popping-analysis.md).
- **Rekordbox bundle ID is `com.pioneerdj.rekordboxdj`** (not `com.pioneerdj.rekordbox`).
- **Rekordbox spawns multiple processes during startup** — the process monitor must debounce both start and stop detection.
