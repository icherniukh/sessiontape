# Rekordbox Auto-Recorder — Agent Instructions

## Known Issues / Not Yet Verified

- **Full lifecycle with music + silence gap** — the splitter works on synthetic audio, but a real test with play→silence→play→quit hasn't been completed clean yet (Rekordbox startup cycle caused short captures in testing).
- **LaunchAgent** — `install/com.rb-recorder.plist` and `scripts/install.sh` exist but haven't been tested with actual login-time auto-start.
- **Crash recovery** — if Rekordbox crashes mid-recording, the raw file persists but the WAV conversion + split won't run until the daemon's next cycle. No automatic recovery of orphaned `.raw` files.
- **Long session stability** — not tested with multi-hour DJ sets. Disk space usage is ~184KB/s (660MB/hour).
- **audiotee stability** — the user noted audiotee "is not stable nowadays." No issues observed during short tests but long sessions may reveal problems.
- **Permissions UX** — Screen Recording permission must be granted manually. No guided setup flow.

## Planned Work: audiotee Upstream PR Readiness

These tasks should be performed once the main feature work is complete.

1.  **Benchmarking:** Measure performance difference between raw `write(2)` and `FileHandle.write(Data)` in the audio hot path using a standalone Swift script.
2.  **Diagnostic Branch:** Create a dedicated branch for debug diagnostics:
    *   **Hot-Path Jitter:** Track and log delta time between `processAudio` callbacks (expecting ~10-20ms).
    *   **Alignment/Zero-Buffer Stats:** Log first few bytes and zero-buffer counts to debug "silent tap" issues.
    *   **CPU Budget Logging:** Measure execution time within the IO proc versus the callback interval.
3.  **Upstream Prep:**
    *   Revert `BinaryOutputHandler.swift` to idiomatic Swift if `write(2)` proves unnecessary.
    *   Verify all local hacks in `AudioTeeCLI` and `AudioTeeCore` are surgical and ready for a clean PR to `makeusabrew/audiotee`.

## Issue Tracking

This project uses **GitHub Issues** for issue tracking.

```bash
gh issue list         # Find available work
gh issue view <num>   # View issue details
gh issue create       # Create a new issue
gh issue close <num>  # Close an issue
```

## Key Decisions & Pitfalls

- **audiotee requires `--flush`** — without it, stdout buffering prevents subprocess pipe reads from getting data.
- **audiotee outputs s16le** (not float32) — conversion must use `-f s16le` not `-f f32le`.
- **AudioCapCLI doesn't work from scripts** — macOS TCC blocks it outside interactive terminals. Don't switch back to it.
- **ProcTap/ScreenCaptureKit returns all zeros for Rekordbox** — Rekordbox uses a custom audio engine that bypasses ScreenCaptureKit's hooks. Don't revisit this path.
- **Rekordbox bundle ID is `com.pioneerdj.rekordboxdj`** (not `com.pioneerdj.rekordbox`).
- **Rekordbox spawns multiple processes during startup** — the process monitor must debounce both start and stop detection.
