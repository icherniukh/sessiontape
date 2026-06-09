# SessionTape Research

Active investigations. Items here are work-in-progress or unconfirmed theories — treat as context, not rules. Concluded investigations move to AGENTS.md (conclusions only) or close as GitHub Issues.

---

## Popping/Cracking Audio (macOS)

**Root cause under investigation**: Rekordbox enters a stateful CoreAudio overload condition after certain seek/scrub operations. Evidence from logs at `/tmp/rb-audio-popping-captures/20260331-164759`:
- `HALC_ProxyIOContext::IOWorkLoop: skipping cycle due to overload` in Rekordbox's CoreAudio session
- `ClientHALIODurationExceededBudget` and `SafetyViolationOccurred` from `coreaudiod`
- `replayd` shows `SLContentStream` screenshot/display churn in audio-only `SCStream` mode
- Overload is stateful: once entered, small scrub movements re-trigger it
- `mac-capture` stayed alive through the popping — the helper itself is not crashing

**Filter mode experiments (as of 2026-03-31)**:

| Mode | Overloads | SCK churn events | Notes |
|------|-----------|-----------------|-------|
| display + audio-only | 32 | 830 | Current best working mode |
| display/windows + audio-only | 25 | 2236 | Confounded by Rekordbox update between runs |

Neither mode removes the `AudioTap`/aggregate path or the `SLContentStream` churn. `SCContentFilter(display: including: windows)` crashes on startup with `CGS_REQUIRE_INIT` in some configs and is not yet validated.

**Current hypotheses** (none confirmed):
1. Rekordbox enters a state where seek/scrub causes repeated CoreAudio deadline overruns regardless of whether a tap is attached
2. `mac-capture` SCK attachment may make that state easier to trigger
3. `replayd SLContentStream` churn is intrinsic to process-audio SCK capture, not removable via filter configuration

**Next steps if investigation resumes**:
1. Review `replayd.log` and `mac-capture.diag.log` around exact first overload timestamp (`2026-03-31 16:48:26`)
2. Determine whether `replayd` churn is intrinsic to `SCStream` process-audio capture or driven by our filter config
3. Test whether Rekordbox overloads occur without any tap attached (baseline)
4. If pop cannot be eliminated, investigate whether it can be predicted and the recording paused during aggregate device transitions

---

## macOS Silent Tap

**Status**: Single observation (2026-03-28), not reproduced. May be the same root cause as the overload investigation above.

**Observation**: After a clean 68-minute recording, `mac-capture` transitioned abruptly to delivering all-zero buffers. IO proc continued running on schedule (regular 0.1s intervals) but buffer contents were zeros. No CoreAudio error, no device change event, no helper stderr. All subsequent restarts for that session produced immediate zeros.

**Theories**:
- **A**: macOS bug — `AudioHardwareCreateProcessTap` silently loses connection to the source audio stream after extended runtime
- **B**: CoreAudio aggregate device state corruption accumulates over restarts (session showed 68 min → 30 sec → 0 sec degradation across restarts)
- **C**: Rekordbox audio engine idle/thread starvation (Rekordbox had ~300 threads after 9+ hours at observation time)

**Open questions**:
1. Does restarting Rekordbox (new PID) break the restart loop? Assumed yes from prior sessions, never confirmed during this incident.
2. `sudo log stream --predicate 'subsystem CONTAINS "coreaudio"'` during failure would give direct evidence.
3. Is the all-zero buffer a symptom of the overload condition above, or a separate failure mode?
