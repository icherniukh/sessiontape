# Fart Bug

Audio glitch heard through speakers during recording sessions. Sounds like a sharp distortion/pop. Confirmed to originate from audiotee's Core Audio aggregate device setup and teardown, not from the recording software's output path.

## Observed Triggers

### 1. Audiotee restart (any cause)
Every time audiotee stops and a new instance starts, Core Audio tears down and re-creates an aggregate device for the process tap. This routing change briefly affects audio output. Each restart = one fart.

**Causes of restart:**
- Watchdog fires (silent tap detected)
- Daemon reloaded (LaunchAgent reload, build deploy)
- Audiotee exits unexpectedly (exit code 1)
- SIGKILL after SIGTERM timeout

### 2. Silent tap → restart loop
Audiotee starts, delivers one or two chunks of real audio, then delivers only zeros. The silence threshold triggers (15s), recording closes, watchdog eventually fires (90s), audiotee restarts → fart. If the tap continues delivering zeros after restart, the cycle repeats.

**Observed:** 20:52:01 session — RMS=3382 on first chunk, RMS=0 from chunk #100 onward. Loop ran for several minutes before user stopped daemon.

### 3. Core Audio state corruption from rapid restarts
Multiple SIGKILL cycles in quick succession leave aggregate device resources partially unreleased. New tap instances start in a broken state (delivering zeros immediately), which then triggers more restarts. Self-reinforcing loop.

**Observed:** Today's session — many rapid reloads for debugging. Once the loop started, only stopping Rekordbox and restarting cleanly resolved it.

### 4. Original IO proc blocking (ring buffer bug, likely fixed)
`write(2)` + `fflush()` called from Core Audio's IO proc thread blocked on pipe backpressure, causing a multi-minute audio glitch. Fixed by Nick Payne's zero-alloc ring buffer (audiotee commits 85975d6, 65c2d58, a1eb465, rebased onto Ivan's device-change + timeout commits). Not confirmed eliminated in production — needs a soak test.

## Why It's Hard to Fix Completely

The fart is caused by Core Audio's aggregate device mechanism, which audiotee needs to read process audio. There is no way to tap a process's audio output on macOS without creating an aggregate device, and aggregate device creation/destruction inherently causes a brief audio routing change. This is an OS-level constraint.

What we **can** control:
- How often we restart audiotee (fewer restarts = fewer farts)
- How cleanly we shut down audiotee (SIGTERM with full cleanup > SIGKILL)
- How quickly we detect a broken tap (faster detection = shorter silent gap, but potentially more restarts if too aggressive)
- Whether we can distinguish "music actually stopped" from "tap delivering zeros"

## Investigation Needed

- [ ] Is the silent tap (zeros) a ring buffer overflow? If the drain thread can't keep up with the IO proc, the ring buffer fills up and writes zeros. Needs audiotee-side instrumentation (log ring buffer fill level, dropped frames).
- [ ] Is the SIGKILL cleanup path leaving Core Audio state dirty? Test: always allow full SIGTERM cleanup (longer timeout), see if silent tap loop goes away.
- [ ] Can we detect "tap is broken" vs "music stopped" without restarting? RMS=0 exactly is suspicious — real silence is rarely perfect zeros. If we detect a run of perfect zeros, that's a stronger signal than just "below threshold."
- [ ] Does the aggregate device teardown affect audio output on macOS 15+ differently than earlier versions?

## Current Mitigations (incomplete)

- Watchdog reduced from 5 min → 90s: detects broken tap faster
- `last_active_at` reset on ACTIVE→PASSIVE: prevents spurious watchdog fire after normal session end
- SIGTERM before SIGKILL with 10s timeout: gives audiotee time for cleanup (but SIGKILL still happens when audiotee hangs)
- DIAG logging: logs when RMS drops suddenly (has a bug — doesn't update `_last_rms` in PASSIVE path, needs fix)

## Related

- audiotee ring buffer PR: upstream makeusabrew/audiotee — needs to be submitted
- Architecture redesign (see `architecture-problems.md`): event-driven supervisor would reduce restart frequency by reacting immediately to `CaptureDied` rather than polling/watchdog, eliminating one class of unnecessary restarts
