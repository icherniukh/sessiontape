# mac-capture Investigation

## Scope

This work is being done only in the dedicated worktree:

- `/Users/ivan/proj/sessiontape/.worktrees/codex-mac-capture-fix`

Do not continue `mac-capture` edits in the main repo because the main checkout is being refactored separately.

## Branch / Worktree State

- Main repo branch: `main`
- Menu bar rebase worktree: `/Users/ivan/proj/sessiontape/.worktrees/macos-menubar-review`
- mac-capture investigation worktree: `/Users/ivan/proj/sessiontape/.worktrees/codex-mac-capture-fix`

## High-Level History

Capture backend history reconstructed from git:

1. `717380f` ProcTap Python callback path
2. `47132c5` switch to `AudioCapCLI`
3. `1a6b4ef` switch to `audiotee`
4. `75b13a6` pivot to `mac-capture`

Important contradiction:

- `AGENTS.md` says `ProcTap/ScreenCaptureKit returns all zeros for Rekordbox` and says not to revisit it.
- At the time of investigation, the active runtime used `mac-capture`, which is a ScreenCaptureKit helper.

So `mac-capture` is a later tactical pivot back to SCK despite earlier notes rejecting that path.

## What `mac-capture` Is

The Swift helper in:

- [main.swift](/Users/ivan/proj/sessiontape/.worktrees/codex-mac-capture-fix/mac-capture/Sources/mac-capture/main.swift)

uses:

- `SCShareableContent`
- `SCContentFilter`
- `SCStream`
- `SCStreamOutput`

So this is definitely a ScreenCaptureKit-based capture path.

## Current Code Changes In This Worktree

### Helper instrumentation

Added:

- file-backed diagnostics via `RB_MAC_CAPTURE_LOG`
- stderr + diag logging helpers
- callback timing / buffer format logs
- explicit filter mode via `RB_MAC_CAPTURE_FILTER_MODE`

### Helper hot-path changes

Added:

- `OutputWriter` with non-blocking stdout
- `FastPCMConverter` fast path for common float32 stereo SCK buffers
- `AVAudioConverter` fallback only for unsupported formats

### Filter experiments

Current default:

- `RB_MAC_CAPTURE_FILTER_MODE=display`

Optional experimental mode:

- `RB_MAC_CAPTURE_FILTER_MODE=window`

Window mode currently crashes on startup with a CoreGraphics init assertion:

- `Assertion failed: (did_initialize), function CGS_REQUIRE_INIT`

So the capture script forces `display` mode for now.

## Confirmed Runtime Findings

### `audiotee` is not the active runtime

When live recorder processes were inspected, the active helper was `mac-capture`, not `audiotee`.

### Clean helper probe can start

Short direct helper runs against a live Rekordbox PID have shown:

- SCK finds Rekordbox
- stream can start successfully
- early audio callbacks arrive
- fast converter path is used

### D1 vs D2 user observation

User reported:

- scrubbing D1 can be safe
- scrubbing D2 can trigger the audio popping
- over time D2 becomes more vulnerable
- once in bad state, small movement can retrigger it

This strongly suggests a stateful failure, not a pure one-shot startup bug.

## Confirmed Log Findings

### `coreaudio2.log`

From `/Users/ivan/proj/coreaudio2.log`:

- first overload onset around `2026-03-31 16:39:59.928`
- Rekordbox logs `HALC_ProxyIOContext::IOWorkLoop: skipping cycle due to overload`
- `coreaudiod` immediately reports:
  - `Overload possibly due to HAL client proc exceeding io cycle budget`
  - `Overload possibly due to safety violation`

### `coreaudio_rekordbox.log`

From `/Users/ivan/proj/coreaudio_rekordbox.log`:

- first overload onset around `2026-03-31 16:43:24.895`
- repeated overload bursts continue long after onset
- same `coreaudiod` diagnostics repeat:
  - `ClientHALIODurationExceededBudget`
  - `SafetyViolationOccurred`
- analytics payloads identify host app as `com.pioneerdj.rekordboxdj`

### Interpretation supported by logs

This is enough to say:

- the audio popping correlates with a real Rekordbox/CoreAudio overload storm
- this is not just a harmless helper-side logging issue
- the failure is stateful and can recur after the initial transition

### Synchronized capture: `/tmp/rb-audio-popping-captures/20260331-164759`

This is the first fully usable direct-helper repro.

What it proves:

- `mac-capture` stayed alive for the whole run
- `mac-capture.raw` kept growing through the popping periods
- helper diagnostics stayed normal enough that the helper was not obviously stalling or restarting
- Rekordbox entered repeated overload storms anyway
- `replayd` explicitly created an `AudioTap` aggregate for this SCK session

Relevant files:

- `/tmp/rb-audio-popping-captures/20260331-164759/process-status.log`
- `/tmp/rb-audio-popping-captures/20260331-164759/mac-capture.diag.log`
- `/tmp/rb-audio-popping-captures/20260331-164759/rekordbox.log`
- `/tmp/rb-audio-popping-captures/20260331-164759/coreaudiod.log`
- `/tmp/rb-audio-popping-captures/20260331-164759/replayd.log`

Key timestamps:

- first Rekordbox overload burst: `2026-03-31 16:48:26.482`
- later overload bursts continue around `16:48:28`, `16:48:29`, `16:48:40`, `16:48:43`, `16:48:45`

Key `replayd` evidence from that run:

- `newAudioTapForSystemAudioCapture`
- `Built valid aggregate 9932`
- `AudioTap-6AE116C7-178B-478B-9967-E2C483D15C9E`

Conclusion from this run:

- ScreenCaptureKit on this path still lands on Apple `AudioTap` / aggregate-device machinery under `replayd`
- the audio popping is not explained by the helper crashing or obviously stopping
- the strongest current explanation is a Rekordbox/CoreAudio/replayd interaction while the SCK tap is attached

## Replayd / AudioTap Status

This is now confirmed for the current repro path, not just hinted by older logs.

Confirmed in `/tmp/rb-audio-popping-captures/20260331-164759/replayd.log`:

- `newAudioTapForSystemAudioCapture`
- `Built valid aggregate 9932`
- `AudioTap-*`
- `AudioDeviceStart` on the `AudioTap-*` device

So switching from `audiotee` to `mac-capture` did not eliminate the underlying OS audio-tap / aggregate-device mechanism.

## Latest SCK Experiments

### Audio-only display/app filter

Run:

- `/tmp/rb-audio-popping-captures/20260401-004649`

Configuration:

- `filter_mode=display`
- `screen_output=false`

Result:

- helper remained healthy
- user reported the audio popping was harder to trigger and required more aggressive scrubbing
- however `replayd` still showed heavy `SCScreenShotSession` / `SLContentStream` churn
- overloads still occurred in Rekordbox

Interpretation:

- removing the explicit `.screen` output improved behavior
- but did not remove the underlying `replayd` visual/display churn
- so that churn is not caused only by our explicit screen output registration

### Display/windows filter

Run:

- `/tmp/rb-audio-popping-captures/20260401-013544`

Configuration:

- `filter_mode=windows`
- `screen_output=false`
- filter used `SCContentFilter(display: including: windows)`

Result:

- helper remained healthy
- user reported the audio popping still required fairly aggressive scrubbing
- `replayd` still showed `newAudioTapForSystemAudioCapture`
- `replayd` still built an aggregate `AudioTap-*` device
- `SLContentStream` churn did not disappear

Observed counts in this run versus the previous audio-only display/app run:

- display/audio-only overloads: `32`
- windows-filter overloads: `25`
- display/audio-only screenshot churn count: `830`
- windows-filter screenshot churn count: `2236`

Important caveat:

- Rekordbox was updated between these runs
- so cross-run behavioral comparison is informative but not perfectly controlled

Interpretation:

- the display/windows filter is not a convincing improvement
- it does not remove the `AudioTap` / aggregate path
- it does not remove `SLContentStream` churn
- any apparent reduction in overload count is confounded by the Rekordbox update and by changed user repro effort

## Hardened Capture Script

Current script:

- [capture-audio-popping-logs.sh](/Users/ivan/proj/sessiontape/.worktrees/codex-mac-capture-fix/scripts/capture-audio-popping-logs.sh)

It captures:

- `mac-capture.diag.log`
- `mac-capture.stderr`
- `mac-capture.raw`
- `coreaudiod.log`
- `replayd.log`
- `rekordbox.log`
- `process-status.log`
- `capture-meta.log`

Current live capture directory at time of writing:

- `/tmp/rb-audio-popping-captures/20260331-164759`

Known limitation in the current script:

- helper liveness is now tracked without the broken subshell `wait`
- it records that the helper is no longer alive, but it does not capture a precise child exit code

## Current Best Hypotheses

1. Rekordbox enters a state where some seek/scrub action causes repeated CoreAudio deadline overruns.
2. `mac-capture` attachment may be making that state easier to trigger, but current evidence does not yet prove whether it is the root cause or just a catalyst.
3. The current SCK route appears to trigger `replayd` `SLContentStream` screenshot/display churn even in audio-only mode.
4. Changing from display/app filtering to display/windows filtering did not remove that churn.
5. The crashing desktop-independent window filter remains unvalidated, but the non-crashing filter variants tested so far do not look like a clean fix.

## Immediate Next Steps

1. Review:
   - `replayd.log`
   - `process-status.log`
   - helper diag log
   around the exact first overload timestamp.
2. If needed, add even more targeted helper state logging around:
   - stream start
   - first audio callback
   - callback gaps
   - stream stop/error
3. Treat `audio-only display/app` as the current best SCK variant found so far.
4. If SCK work resumes later, prefer investigating whether the remaining `replayd` churn is intrinsic to process-audio capture rather than spending more time on small filter permutations.
5. Decide later whether `mac-capture` is still worth pursuing if the product goal was specifically to escape aggregate/tap behavior, because current evidence says it does not.

## Non-Goals Right Now

- Do not clean commit history yet.
- Do not edit the main repo for `mac-capture` work.
- Do not treat the menu bar rebase worktree as the place for low-level helper experiments.
