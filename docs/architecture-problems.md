# Architecture Problems

Working document for design session. Not committed.

## TL;DR

The daemon is a polling loop that reaches into component internals and makes decisions based on heuristic timers. Every bug we've hit this session traces back to this. The fix is an event-driven supervisor. See `architecture-options.md` for approach comparison.

---

## Problem 1: God Object Daemon with Polling Loop

**File:** `src/daemon.py`

`RecorderDaemon.run()` is a `while True` loop that calls `poll_once()` + `_watchdog_check()` + `time.sleep(2)`. It reaches into `capture.recorder.state` and `capture.recorder.last_active_at` directly to make decisions. This is a god object — it knows too much about the internals of every other component.

**Consequences:**
- Any new behavior requires modifying daemon.py
- Impossible to test components in isolation
- State bugs are easy to introduce (seen this session: watchdog firing during active recording because it polled `.state` at the wrong time)

---

## Problem 2: Watchdog is a Heuristic Timer, Not an Event Reaction

**File:** `src/daemon.py:_watchdog_check()`

The watchdog was added to detect "audiotee tap silently broke." Its implementation: check every 5 minutes if `last_active_at` is stale. This is wrong in multiple ways:

1. `last_active_at` is set only on PASSIVE→ACTIVE transition. After a 50-minute recording ends naturally, `last_active_at` is 50 minutes old — watchdog fires 3 seconds after silence is detected, even though nothing is broken.

   **Observed:** 20:37:46 session closes normally → 20:37:49 watchdog fires "no audio for 50m" → SIGTERM/SIGKILL cycle → 10s of unnecessary silence.

2. The ACTIVE-state skip (added this session) is a workaround for problem 1, not a fix.

3. The real signal is `CaptureDied` (audiotee exited unexpectedly). The watchdog is approximating that signal with a time heuristic. The event-driven design eliminates the watchdog entirely — a `CaptureDied` event triggers immediate restart.

---

## Problem 3: Process Monitor Uses Blocking Sleeps

**File:** `src/process_monitor.py`

`poll_once()` calls `time.sleep(startup_delay)` (10s) and `time.sleep(stop_delay)` (10s) inline. These block the entire main loop. If audiotee dies during the startup delay, the daemon is frozen for 10 seconds before it can react.

Additionally, polling psutil every 2 seconds works but wastes cycles and adds up to 2 seconds of latency to process start/stop detection.

---

## Problem 4: No Reaction to Capture Helper Death

**File:** `src/capture.py:_read_loop()`, `src/daemon.py`

When audiotee exits unexpectedly (exit code 1, 0 chunks), `_read_loop` exits silently. The daemon is not notified. `is_recording` stays `True`. The daemon won't attempt recovery until the watchdog fires — up to 5 minutes later.

**Observed:** 19:44:07 audiotee timed out on AudioDeviceStart, exit code 1, 0 chunks. Daemon was stuck until manual reload at 19:47:30.

---

## Problem 5: PCMStreamRecorder Mixes Concerns

**File:** `src/recorder_core.py`

`PCMStreamRecorder` does three things:
1. Analyzes PCM for silence/activity (RMS calculation, state machine)
2. Manages raw file lifecycle (open/close/path generation)
3. Triggers export

These should be separate units. As written, the daemon must reach into `recorder.state` and `recorder.last_active_at` to understand what's happening — creating coupling in both directions.

---

## Problem 6: Async Export Threads Die on Process Kill

**File:** `src/recorder_core.py:Exporter.export_async()`

Export threads are `daemon=True`. When LaunchAgent reloads the process, the old process is killed and in-flight conversion threads die mid-write. Raw files survive but are unfinished.

Orphan recovery handles this, but it's a band-aid. The 30-second mtime gate (added this session) prevents double-conversion on rapid restart but adds a 30s blind spot.

The right fix is synchronous shutdown: when stopping, wait for in-flight exports to complete before exiting. This requires the daemon to know about exports — currently it doesn't.

---

## Problem 7: State Transition Logging Is Coarse

The debug RMS log fires every 600 chunks (60s). This means audio dropouts that resolve in under 60 seconds are invisible in the logs. The fart bug (20:37:xx) showed RMS=0 at the 60s log point — we don't know when the dropout actually started because there's no log between chunk #29400 (20:36:43) and #30000 (20:37:43).

Fine-grained logging shouldn't be on a fixed interval — it should log on state changes and on significant RMS transitions.

---

## Immediate Bugs Not Yet Fixed

### Watchdog false-fire after natural session end

`last_active_at` is set on PASSIVE→ACTIVE but never updated on ACTIVE→PASSIVE. After a long recording ends naturally, the watchdog sees stale `last_active_at` and fires immediately.

**Fix:** Update `last_active_at = time.time()` when transitioning ACTIVE→PASSIVE in `PCMStreamRecorder`. This resets the watchdog timer to "silence just started now."

### No restart on capture helper death (0 chunks, non-zero exit)

The daemon has no path to restart audiotee when it dies without delivering any audio. Watchdog is the only recovery mechanism and fires after 5 minutes.

**Fix (short-term):** In `_read_loop`, after exiting with 0 chunks and non-zero exit code, push a restart signal. In the event-driven design, this becomes a `CaptureDied` event.

---

## Desired Architecture

See `architecture-options.md`. Option 1 (typed event queue) eliminates all of the above:
- No polling loop — main thread blocks on queue
- No watchdog — `CaptureDied` event triggers immediate restart
- No internal state access — components communicate only via events
- Clean shutdown — supervisor drains event queue before exiting
- Testable — inject events without real subprocess/hardware
