"""Runs RecorderDaemon on a background thread and exposes state via a
thread-safe event queue intended to be drained on the main (AppKit) thread."""

import logging
import queue
import threading
from datetime import datetime
from typing import Callable, Optional

from src.app_state import AppState
from src.config import Config

log = logging.getLogger("rb-recorder")


class DaemonBridge:
    def __init__(self, config: Config):
        self.config = config
        self._event_queue: queue.Queue = queue.Queue()
        self._daemon = None
        self._thread: Optional[threading.Thread] = None

        self._state = AppState.IDLE
        self._session_start: Optional[datetime] = None
        self._segment_count = 0
        self._last_saved_path: Optional[str] = None

        # Callbacks set by MenuBarApp — always called on the main thread via drain_events()
        self.on_state_change: Optional[Callable[[AppState], None]] = None
        self.on_segment_saved: Optional[Callable[[str, float], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

    # ── Public properties ────────────────────────────────────────────────────

    @property
    def state(self) -> AppState:
        return self._state

    @property
    def session_start(self) -> Optional[datetime]:
        return self._session_start

    @property
    def segment_count(self) -> int:
        return self._segment_count

    @property
    def last_saved_path(self) -> Optional[str]:
        return self._last_saved_path

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        from src.daemon import RecorderDaemon

        self._daemon = RecorderDaemon(self.config)
        self._daemon.on_state_change = self._on_daemon_state
        self._daemon.on_capture_active = self._on_capture_active
        self._daemon.on_segment_saved = self._on_segment_saved

        self._thread = threading.Thread(
            target=self._run_daemon, daemon=True, name="daemon-bridge"
        )
        self._thread.start()

    def stop(self) -> None:
        if self._daemon:
            self._daemon.shutdown()

    # ── Event draining (call on main thread via rumps.Timer) ─────────────────

    def drain_events(self) -> None:
        while True:
            try:
                event, payload = self._event_queue.get_nowait()
                self._dispatch(event, payload)
            except queue.Empty:
                break

    # ── Background thread callbacks (enqueue only — no AppKit calls here) ───

    def _run_daemon(self) -> None:
        try:
            self._daemon.run()
        except Exception as exc:
            log.exception("Daemon thread raised an exception")
            self._event_queue.put(("error", str(exc)))

    def _on_daemon_state(self, state: str, info: dict) -> None:
        self._event_queue.put(("daemon_state", (state, info)))

    def _on_capture_active(self) -> None:
        self._event_queue.put(("capture_active", None))

    def _on_segment_saved(self, path: str, duration: float) -> None:
        self._event_queue.put(("segment_saved", (path, duration)))

    # ── Dispatch on main thread ───────────────────────────────────────────────

    def _dispatch(self, event: str, payload) -> None:
        if event == "daemon_state":
            state_str, _info = payload
            old_state = self._state
            if state_str == "monitoring":
                self._state = AppState.MONITORING
                self._session_start = None
                self._segment_count = 0
            elif state_str == "idle":
                self._state = AppState.IDLE
                self._session_start = None
            if self.on_state_change and self._state != old_state:
                self.on_state_change(self._state)

        elif event == "capture_active":
            self._state = AppState.RECORDING
            self._session_start = datetime.now()
            if self.on_state_change:
                self.on_state_change(self._state)

        elif event == "segment_saved":
            path, duration = payload
            self._segment_count += 1
            self._last_saved_path = path
            self._state = AppState.MONITORING
            if self.on_segment_saved:
                self.on_segment_saved(path, duration)
            if self.on_state_change:
                self.on_state_change(self._state)

        elif event == "error":
            self._state = AppState.ERROR
            if self.on_error:
                self.on_error(payload)
            if self.on_state_change:
                self.on_state_change(self._state)
