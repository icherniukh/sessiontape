# macOS Audio Popping — Root Cause Analysis

> **Status (2026-11-27):** Live `coreaudiod` profiling and production logs from Nov 19–24 identified
> two distinct causes: (1) unnecessary `[ahal_dsp]` drift resampling (`drift=1`) and a 512-frame
> buffer on the private tap aggregate adding +41% steady-state `coreaudiod` CPU and 8x extra RT
> wakeups on top of Rekordbox's narrow 512-frame margin, and (2) `PCMStreamRecorder` misclassifying
> paused digital-zero audio (`rms == 0.0`) as `TapBroken`, forcing mid-session aggregate teardown
> restarts every 90s of pause. Both are fixed on `main` (`drift=0`, 4096-frame aggregate buffer,
> clean silence splitting); live Rekordbox A/B (`scripts/tap-ab.sh base` vs `v130`) is ready to run.
> Tracked in [Issue #33](https://github.com/icherniukh/sessiontape/issues/33).

## Problem

SessionTape captures Rekordbox's audio output on macOS using ScreenCaptureKit (SCK). When capture
is active, Rekordbox produces audible pops/crackles through the speakers. Triggered most reliably
by deck scrubbing (D2 more vulnerable than D1, failure is stateful — once triggered, small
movements retrigger it).

## Capture Architecture

```
mac-capture (Swift, SCStream + SCContentFilter)
    → SCK internally calls replayd
        → replayd: newAudioTapForSystemAudioCapture
            → Builds AudioTap aggregate device (e.g. "Built valid aggregate 9932")
                → Rekordbox's audio device is now a sub-device of the aggregate
                    → Rekordbox switches from direct HAL IO to HALC_ProxyIOContext
```

Before our tap: Rekordbox → hardware device (direct IO, no IPC per cycle).
After our tap: Rekordbox → ProxyIOContext → Mach IPC to coreaudiod → aggregate → hardware + AudioTap.

## Log Evidence

From `/tmp/rb-audio-popping-captures/20260331-164759`:

- `HALC_ProxyIOContext::IOWorkLoop: skipping cycle due to overload` — Rekordbox's log
- `ClientHALIODurationExceededBudget` — coreaudiod
- `SafetyViolationOccurred` — coreaudiod
- `replayd`: heavy `SLContentStream` / `SCScreenShotSession` churn even with `screen_output=false`
- Overload counts: display/app filter → 32 overloads; windows filter → 25 overloads

mac-capture itself stayed alive and healthy throughout. The capture process is not the problem.

## What The Proxy Context Adds (Hypothesis)

The aggregate device created by replayd causes Rekordbox's IO context to become a
`HALC_ProxyIOContext`. *Hypothesis*: this adds per-cycle IPC coordination that increases latency
variance compared to a direct HAL device. Note: normal CoreAudio clients already involve
coreaudiod-coordinated timing even without an aggregate — so the "before/after IPC" contrast is
inferred from the ProxyIOContext name, not directly measured.

- coreaudiod handles clients concurrently — it is not a single-threaded serialization point
- The memcpy of audio buffers (a few KB every 5–10ms) is cheap; not the bottleneck
- The plausible overhead is **latency determinism**, not compute: RT failure is about timing
  guarantees, not throughput. A normal client that occasionally gets scheduled late will miss its
  5ms deadline; extra aggregate coordination layers reduce the margin for that.

This is one candidate cause. The evidence does not yet distinguish it from the alternative below.

## The SLContentStream Problem (Candidate Cause)

replayd triggers display capture machinery (`SLContentStream`, `SCScreenShotSession`) even when we
explicitly request audio-only capture (`screen_output=false`). This appears to be architectural
coupling in SCK — audio process capture is internally tied to the display capture pipeline
regardless of what we request. This increases system load and IPC pressure.

**But**: "mac-capture stayed alive and raw file kept growing" during overloads only proves the
process did not die — it does not prove our IO proc or drain thread never stalled. The RMS log
fires every 60s, so sub-60s stalls are invisible. A slow or occasionally-blocking consumer
back-pressuring the shared aggregate ring buffer would hit Rekordbox too, and matches the symptom
(stateful, worsens over time, triggered by D2 scrub bursts). fart-bug.md records exactly this
failure mode: blocking write in the IO proc. "Our drain stalls under scrub burst load" remains
a live hypothesis, not a closed one.

Two candidate causes, not yet distinguished:
- **A**: replayd's display pipeline churn spikes IPC latency → Rekordbox misses RT deadline
- **B**: our drain/consumer stalls under scrub load → back-pressures the aggregate → same overload

## Attempted Solutions and Why They Failed

| Approach | Result |
|---|---|
| audiotee → mac-capture pivot | Both still land on AudioTap aggregate via replayd |
| screen_output=false | Reduces but does not eliminate SLContentStream churn |
| display/app filter vs windows filter | Marginal difference, confounded by Rekordbox update |
| BlackHole loopback | Eliminates aggregate issue entirely — but requires user setup; violates project pitch |

## Project Constraint

**Zero setup.** One command + one permission click. This rules out any solution that requires the
user to configure Audio MIDI Setup, install virtual audio devices, or change Rekordbox's output
routing.

## Key Open Questions

1. **What exactly does `ClientHALIODurationExceededBudget` measure?** Is it Rekordbox's own IO
   proc exceeding its time budget, or is it the proxy/aggregate IO proc that coreaudiod attributes
   to Rekordbox? This matters for identifying the exact failure point.

2. **Is `CATapDescription` (CoreAudio 14.4+) a viable alternative?** This is a CoreAudio-native
   process audio tap API that bypasses SCK and replayd entirely. If it avoids creating a
   ProxyIOContext and avoids replayd's SLContentStream churn, it could eliminate both sources of
   overhead. Unknown: does it still create an AudioTap aggregate under the hood?

3. **Is the SLContentStream churn intrinsic to process audio capture on macOS, or specific to
   SCK?** If it's SCK-specific, a non-SCK capture path (CATapDescription, or CoreAudio audio tap
   APIs) would avoid it.

## Most Promising Next Step: Discriminating Experiment

`CATapDescription` / `CATap` (CoreAudio, macOS 14.2+) taps process audio without routing through
ScreenCaptureKit or replayd. **Important**: it still creates an aggregate device under the hood —
the documented Apple pattern is `AudioHardwareCreateProcessTap` + `AudioHardwareCreateAggregateDevice`
with the tap in `kAudioAggregateDeviceTapListKey`. So it likely still produces a ProxyIOContext on
Rekordbox.

What it cleanly avoids: **replayd and SCK's display capture pipeline** (no SLContentStream, no
SCScreenShotSession).

This makes it a decisive experiment, not a guaranteed fix:

- **Still overloads** → the aggregate/tap timing itself is the culprit. CATapDescription won't
  help; the whole process-tap family is suspect. Back to loopback or accepting it.
- **No overload** → replayd's display pipeline was the cause. Switching off SCK is a real
  zero-setup fix.

**Version constraint**: CATapDescription requires macOS 14.2+. mac-capture now targets macOS 15
(see README requirements); SCK stays as the fallback backend.
This tradeoff must be decided before shipping.

**Entitlement**: requires `com.apple.security.cs.allow-unsigned-executable-memory` or a specific
audio tap entitlement — needs verification.

## AGENTS.md Contradiction Note

AGENTS.md says "ProcTap/ScreenCaptureKit returns all zeros for Rekordbox — don't revisit." The
current mac-capture binary IS ScreenCaptureKit (SCStream). It works. The earlier failure was
likely a different SCK API variant (process tap via ProcTap), not the current SCContentFilter +
SCStream path. CATapDescription is a third distinct path worth testing.

## Hardware run 2026-11-19

Setup: SessionTape 1.3.0 (Homebrew), CoreAudio tap backend (`auto` chose the tap; no SCK
fallback), Rekordbox 7 playing to `BuiltInSpeakerDevice`, 48 kHz, 512-frame IO buffer, MacBook on
battery, macOS 27.2.

- **Pops need the tap.** The user reports none without SessionTape running, even when scrubbing.
- **Our process is not the cause.** For every one-second window across all runs: `drops=0`,
  `hal_overload=0`, `trunc=0`, and `cb_max` ≤ 0.05 ms in the IO proc. The Python reader kept up
  (`gap_max` ≈ 107 ms against a 2 s ring). (`PCMOutboundQueue` uses a drop-newest overflow policy
  with no silence fill so the real-time producer never touches `tail`; if `drops > 0`, the segment
  is shorter than wall-clock time and splices across the dropped chunk.)
- **The overload is in Rekordbox's IO cycle.** Each pop matches a Rekordbox
  `HALC_ProxyIOContext::IOWorkLoop: skipping cycle due to overload` line. For the same event,
  coreaudiod logs `cause: ClientHALIODurationExceededBudget,SafetyViolationOccurred` with
  `HostApplicationDisplayID: com.pioneerdj.rekordboxdj`, `HAL_client_IO_duration` of
  14.1–17.1 ms against an `io_cycle_budget` of 11.35 ms, and low `scheduler_latency` (18–20 µs).
  Rekordbox's IO thread is woken on time, then overruns. In one event it used ≈ 1.5 ms of CPU
  over a 15 ms cycle (waiting); in another, ≈ 14 ms of system time.
- **Trigger is load.** Five overloads between 20:13 and 20:45, all while tapped (two at
  20:15:43 and 20:17:58, a few minutes after Rekordbox launched). Three came within 40 s of
  deliberate scrubbing/rewinding (20:31:58, 20:32:28, 20:32:39). The ~10 min of tapped time
  between 20:17:58 and 20:31:58 without scrubbing (partly paused) had none, nor did 20:32:39–20:45.
- Not the trigger: Rekordbox's network/TLS logging (heavier during clean stretches), and macOS's own
  `AudioTap-…` (corespeechd, pid 815), which runs with the speaker in every case.

By this doc's own decision rule (a tap path without SCK/replayd still overloads), the remaining
suspect is the tap/aggregate work coreaudiod adds inside Rekordbox's cycle.

### Tap configuration profiling & fixes (2026-11-27)

1. **Corrected HAL mental model & untapped baseline:** Every user-space CoreAudio client already runs on
   `HALC_ProxyIOContext` <-> `HALS_IOContext_Legacy_Impl`. Tapping does not move Rekordbox onto our
   aggregate; `coreaudiod` keeps Rekordbox's `HALS_IOContext` on `BuiltInSpeakerDevice` and attaches
   `HALS_MultiTap_Engine` to copy rendered buffers. Crucially, Rekordbox 7 at 512 frames (`10.67 ms`
   cycle) produces audible `HALC_ProxyIOContext::IOWorkLoop: skipping cycle due to overload` glitches
   **with no SessionTape process running**: three isolated skips on `2026-11-24` (`18:47:03`,
   `20:46:47`, `22:45:22`) and two audible dropouts on `2026-11-27` (`03:43:22.749` and `03:43:32.738`
   on AC power, where Rekordbox's audio workgroup spent `31.17 ms` and `13.60 ms` in kernel/system time
   during a 1,564 events/s QUIC/TLS network burst while `playwright-mcp` `chrome-headless-shell`
   processes also cycled `BuiltInSpeakerDevice`). Rekordbox at 512 frames already operates near its
   cycle budget; an unoptimized tap simply pushes scrubbing spikes over that cliff more often.
2. **Measured `coreaudiod` overhead (`BuiltInSpeakerDevice`, 48 kHz / 512 frames):**
   - `untapped`: `170 ms` `coreaudiod` CPU per 2s
   - `v130` (`512` frames, `drift=1`, `clock=0`): `240 ms` (+41% `coreaudiod` CPU; 200 tap CBs/2s)
   - `subdev` (`512` frames, `drift=1`, speaker as `MainSubDevice`): `230 ms` (+35%; opens an output stream on the speaker)
   - `clockdev` (`4096` frames, `drift=0`, `ClockDevice=Speaker`): `170 ms`, 25 tap CBs/2s, `outStreams=0`, but aligns 4096-frame tap wakeups onto the speaker's sample boundary (reduced headroom at 10.2 ms spin)
   - **New `base` (`4096` frames, `drift=0`, `clock=0`):** `160 ms` (~0% overhead above `untapped`), speaker stays at `512` frames, `outStreams=0`, and tap RT wakeups drop 8x (200 -> 25 CBs/2s).
3. **Eliminated false 90s `TapBroken` restarts:** Production logs from `2026-11-19` to `2026-11-21`
   showed 7 of 8 DJ segments failed to split on 15s pauses because paused digital decks emit exact
   `0.0f` (`rms == 0.0`), which suppressed session close and killed/recreated the tap aggregate at
   90s (causing speaker routing pops). `PCMStreamRecorder` now splits cleanly on digital-zero silence.

### Tap configuration A/B

`scripts/tap-ab.sh <variant> [seconds]` runs `mac-capture/.build/release/mac-capture` against
Rekordbox, counts `skipping cycle due to overload` lines for that exact window, and verifies non-zero
audio. Scrub the same way in each run and keep the power state identical.

| Variant | Toggles | Description |
|---|---|---|
| `base` | none | Default: unpinned, `drift=0`, `clock=0`, 4096-frame aggregate buffer |
| `v130` | `DRIFT=1 BUFFER_FRAMES=0` | `v1.3.0` baseline: unpinned, `drift=1`, 512-frame HAL default buffer |
| `buf512` | `BUFFER_FRAMES=512` | `drift=0` with 512-frame aggregate buffer |
| `drift1` | `DRIFT=1` | `drift=1` with 4096-frame aggregate buffer |
| `pin` | `PIN=1` | Tap `deviceUID` pinned to Rekordbox's output device |
| `clockdev` | `CLOCK=clockdev` | `kAudioAggregateDeviceClockDeviceKey` = output device (`outStreams=0`) |
| `subdev` | `CLOCK=subdev` | Output device in `SubDeviceList` + `MainSubDevice` (`outStreams=1`) |
| `sck` | `--sck` | ScreenCaptureKit fallback backend |

Workaround if residual overloads persist under heavy battery scrubbing: a 1024-frame buffer in
Rekordbox (21.3 ms budget) fits every 14.1–17.1 ms overrun measured on `2026-11-19` at the cost of
~10 ms more output latency.
