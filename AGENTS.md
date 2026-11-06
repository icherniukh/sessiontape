# SessionTape — Agent Instructions

## Known Issues / Not Yet Verified

- **Full lifecycle with music + silence gap** — the splitter works on synthetic audio, but a real test with play→silence→play→quit hasn't been completed clean yet (Rekordbox startup cycle caused short captures in testing).
- **Long session stability** — not tested with multi-hour DJ sets. Disk space usage is ~184KB/s (660MB/hour).
- **Terminal popups during tests** — Even with `CREATE_NO_WINDOW` flags, the test suite occasionally spawns empty terminal windows on Windows. Tracked in [Issue #21](https://github.com/icherniukh/sessiontape/issues/21).
- **Permissions UX** — Screen Recording permission must be granted manually. No guided setup flow.

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
  - Exit code 3 = `FORMAT_MISMATCH`: the tap's sample rate differs from `recording.sample_rate`. `mac-capture` prints `FORMAT_MISMATCH tap=<rate> requested=<rate>` to stderr. The daemon logs a critical message and does not restart capture; the user must set `recording.sample_rate` to the output device rate.
  - **Unconfirmed:** the earlier claim that ProcTap/SCK "returns all zeros for Rekordbox" (cause: custom audio engine bypassing SCK hooks) is not confirmed; the current SCK path has captured audio. The root cause of Rekordbox popping under capture is also unconfirmed. Both wait on the tap-vs-SCK hardware A/B in [Issue #33](https://github.com/icherniukh/sessiontape/issues/33). See [docs/capture-popping-analysis.md](docs/capture-popping-analysis.md).
- **Rekordbox bundle ID is `com.pioneerdj.rekordboxdj`** (not `com.pioneerdj.rekordbox`).
- **Rekordbox spawns multiple processes during startup** — the process monitor must debounce both start and stop detection.
