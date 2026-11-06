# macOS Audio Popping — Root Cause Analysis

> **Status:** The CoreAudio tap backend is implemented (`--backend tap|sck|auto`), but the tap-vs-SCK A/B on Rekordbox hardware has not been run. No result is recorded here; the root cause remains unconfirmed. Tracked in [Issue #33](https://github.com/icherniukh/sessiontape/issues/33).

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
