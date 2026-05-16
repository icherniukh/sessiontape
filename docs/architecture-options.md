# Daemon Architecture Options

Working document — not committed. For design evaluation before spec.

## Context

Current architecture problems:
- `daemon.py` is a god object: polls every 2s, reaches into `capture.recorder.state`, `capture.recorder.last_active_at`
- Watchdog is a heuristic timer (5 min) rather than reacting to capture helper death
- No clean extension points for control surface (API/UI), post-processing, or smart session detection
- `PCMStreamRecorder` mixes audio analysis with file lifecycle
- Stale state bugs caused by polling interval gaps (watchdog fired during active recording)

---

## Option 1 — Typed Event Queue (Supervisor pattern) ⭐ Recommended

Central `queue.Queue` that background threads push typed dataclass events onto. Main thread is a pure event handler with no polling or sleep. Each component knows only how to emit events — nothing about other components.

```
ProcessMonitor thread  ──┐
CaptureReader thread   ──┼──▶  Queue  ──▶  Supervisor (main thread)
AudioAnalyzer          ──┘                    │
                                              ├─ starts/stops CaptureProcess
                                              ├─ opens/closes RecordingManager
                                              └─ routes to future subscribers (API, UI, post-processing)
```

Events (typed dataclasses): `ProcessStarted(pid)`, `ProcessStopped`, `PcmChunk(data)`, `CaptureDied(exit_code)`, `AudioActive`, `AudioSilent`, `SessionComplete(path, duration)`, `ShutdownRequested`

### Evaluation

| Dimension | Score | Notes |
|-----------|-------|-------|
| Performance & lightweight | ✅ Excellent | Main thread blocks on queue.get() — zero spin. No timers. Background threads are long-lived. Queue overhead is nanoseconds. |
| Robustness & flexibility | ✅ Excellent | Isolated components. CaptureDied event triggers immediate restart — no watchdog heuristics. Typed events make all contracts explicit and compiler-checkable. |
| Simplicity & maintenance | ✅ Good | Python stdlib only (queue, threading, dataclasses). Causality is traceable: event → handler. More boilerplate than current but far cleaner boundaries. Any component can be tested by pushing events into a queue. |
| Cross-platform & extensibility | ✅ Excellent | No platform primitives. Pure Python. New platform = new capture backend, same event types. New feature = new event handler, no surgery on existing code. |
| Reliability | ✅ Excellent | No heuristic timers. State transitions explicit and exhaustive. Supervisor reacts deterministically to component failure. Easy to add dead-letter queue for unhandled events. |

---

## Option 2 — asyncio throughout

Every component is a coroutine. `asyncio.subprocess` for capture helper. `asyncio.Queue` for events. Single-threaded coordination via event loop.

### Evaluation

| Dimension | Score | Notes |
|-----------|-------|-------|
| Performance & lightweight | ✅ Good | Single coordinating thread. But: audiotee stdout reads are blocking — need `loop.run_in_executor()` or asyncio.subprocess which has macOS edge cases with large reads. |
| Robustness & flexibility | ⚠️ Mixed | Async/await makes happy-path flow clear. But unhandled task exceptions are silently swallowed unless explicitly awaited. Task cancellation during I/O requires careful teardown. |
| Simplicity & maintenance | ⚠️ Mixed | Modern idiom but requires understanding event loops, tasks, gather, cancellation, and shield. Async stack traces are harder to follow. asyncio.subprocess behaves differently between ProactorEventLoop (Windows) and SelectorEventLoop (macOS/Linux). |
| Cross-platform & extensibility | ⚠️ Mixed | asyncio works everywhere in theory. In practice, Windows ProactorEventLoop vs macOS SelectorEventLoop have behavioral differences, especially for subprocess I/O. Extra platform-specific care needed. |
| Reliability | ⚠️ Mixed | Silent task failure is a real risk. Exception handling in coroutines requires discipline. Cancellation semantics are non-obvious — easy to leave resources in inconsistent state. |

---

## Option 3 — Reactive Streams (RxPY)

PCM stream as Observable. Silence detection, session segmentation, export are pipeline operators. Subscribe to streams for control surface, post-processing, etc.

### Evaluation

| Dimension | Score | Notes |
|-----------|-------|-------|
| Performance & lightweight | ⚠️ Mixed | Pipelines are efficient for hot paths. RxPY is a non-trivial dependency (~500KB). Observable allocation overhead at high chunk rates. |
| Robustness & flexibility | ⚠️ Mixed | Excellent for stream composition. Poor fit for operational failures (process crashes, file I/O) — these don't map cleanly to onError semantics, which terminate the stream rather than recover. |
| Simplicity & maintenance | ❌ Poor | Requires full reactive programming mental model. Debugging marble diagrams in production is painful. Stack traces point into library internals. onError swallows context. Hard to hand off to anyone unfamiliar with Rx. |
| Cross-platform & extensibility | ✅ Good | Pure Python, platform-agnostic. RxPY less actively maintained (last release 2021). |
| Reliability | ❌ Poor | onError terminates a stream — recovery requires re-subscribing, which is non-trivial. Error propagation through operator chains is hard to reason about. |

---

## Summary Matrix

| Dimension | Option 1 (Queue) | Option 2 (asyncio) | Option 3 (RxPY) |
|-----------|:---:|:---:|:---:|
| Performance & lightweight | ✅ | ✅ | ⚠️ |
| Robustness & flexibility | ✅ | ⚠️ | ⚠️ |
| Simplicity & maintenance | ✅ | ⚠️ | ❌ |
| Cross-platform compatibility | ✅ | ⚠️ | ✅ |
| Reliability | ✅ | ⚠️ | ❌ |

**Recommendation: Option 1.** Simplest thing that is fully event-driven, zero polling, clean isolation, and trivial extension points. No new dependencies. Debuggable with standard Python tooling.

---

## Open Questions (for design phase)

- Thread model: how many threads, owned by whom?
- Queue: single global queue vs per-component queues?
- Error policy: how does Supervisor handle repeated CaptureDied in a tight loop (backoff? give up after N retries?)?
- Shutdown ordering: which components drain in which order?
- Testing: how do we inject events without real subprocess/audio hardware?
