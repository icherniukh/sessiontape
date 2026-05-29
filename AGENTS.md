# SessionTape — Agent Instructions

## Known Issues / Not Yet Verified

- **Full lifecycle with music + silence gap** — the splitter works on synthetic audio, but a real test with play→silence→play→quit hasn't been completed clean yet (Rekordbox startup cycle caused short captures in testing).
- **LaunchAgent** — `install/dev.icherniukh.sessiontape.plist` and `scripts/install.sh` exist but haven't been tested with actual login-time auto-start.
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

## Key Decisions & Pitfalls

- **Product rename decision: SessionTape** — The project has been renamed from `auto-rb-recorder` to **SessionTape** before public winget/Chocolatey/App Store submissions harden the old identity. Public identifiers should use `sessiontape` where package-manager conventions allow it (CLI/binary/package/config dir), with display name `SessionTape`.
- **AudioCapCLI doesn't work from scripts** — macOS TCC blocks it outside interactive terminals. Don't switch back to it.
- **ProcTap/ScreenCaptureKit returns all zeros for Rekordbox** — Rekordbox uses a custom audio engine that bypasses ScreenCaptureKit's hooks. Don't revisit this path.
- **Rekordbox bundle ID is `com.pioneerdj.rekordboxdj`** (not `com.pioneerdj.rekordbox`).
- **Rekordbox spawns multiple processes during startup** — the process monitor must debounce both start and stop detection.
