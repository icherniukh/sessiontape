"""macOS menu bar application for auto-rb-recorder."""

import os
import subprocess
import sys
from datetime import datetime, timedelta
from typing import Optional

import rumps

from src.app_state import AppState
from src.config import Config
from src.daemon_bridge import DaemonBridge


class MenuBarApp(rumps.App):
    def __init__(self, config: Config):
        super().__init__("auto-rb-recorder", quit_button=None)
        self.config = config
        self._bridge = DaemonBridge(config)
        self._bridge.on_state_change = self._on_state_change
        self._bridge.on_segment_saved = self._on_segment_saved
        self._bridge.on_error = self._on_error

        self._prefs_window = None
        self._tick_running = False

        self._build_menu()
        self._apply_state(AppState.IDLE)

        # 10 Hz timer drains events from the daemon background thread
        self._event_timer = rumps.Timer(self._drain_events, 0.1)
        self._event_timer.start()

        # 1 Hz timer updates the recording duration label
        self._tick_timer = rumps.Timer(self._update_tick, 1.0)

    # ── Menu construction ────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        self._status_item = rumps.MenuItem("Idle \u2014 waiting for Rekordbox")
        self._status_item.set_callback(None)

        self._duration_item = rumps.MenuItem("Duration: 0:00")
        self._duration_item.set_callback(None)
        self._segments_item = rumps.MenuItem("Segments: 0")
        self._segments_item.set_callback(None)
        self._last_saved_item = rumps.MenuItem("Last saved: \u2014")
        self._last_saved_item.set_callback(None)

        session_menu = rumps.MenuItem("Session Info")
        session_menu.add(self._duration_item)
        session_menu.add(self._segments_item)
        session_menu.add(self._last_saved_item)

        quit_item = rumps.MenuItem("Quit auto-rb-recorder", callback=self._quit, key="q")

        self.menu = [
            self._status_item,
            None,
            session_menu,
            None,
            rumps.MenuItem("Show Recordings Folder", callback=self._open_recordings_folder),
            None,
            rumps.MenuItem("Preferences\u2026", callback=self._open_preferences, key=","),
            None,
            rumps.MenuItem("Launch at Login", callback=self._toggle_launch_at_login),
            None,
            rumps.MenuItem("About auto-rb-recorder", callback=self._show_about),
            None,
            quit_item,
        ]

        self._refresh_launch_at_login()

    # ── State machine ────────────────────────────────────────────────────────

    def _apply_state(self, state: AppState) -> None:
        if state == AppState.IDLE:
            self._set_icon("circle", red=False)
            self.title = ""
            self._status_item.title = "Idle \u2014 waiting for Rekordbox"
            self._stop_tick()

        elif state == AppState.MONITORING:
            self._set_icon("circle.dotted", red=False)
            self.title = ""
            self._status_item.title = "Monitoring \u2014 listening\u2026"
            self._segments_item.title = f"Segments: {self._bridge.segment_count}"
            self._stop_tick()

        elif state == AppState.RECORDING:
            self._set_icon("record.circle.fill", red=True)
            self.title = "REC"
            self._start_tick()

        elif state == AppState.ERROR:
            self._set_icon("exclamationmark.triangle.fill", red=False)
            self.title = ""
            self._stop_tick()

    def _set_icon(self, symbol_name: str, red: bool) -> None:
        try:
            from AppKit import NSColor, NSImage
            image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol_name, None)
            if image is None:
                return
            image.setTemplate_(not red)
            self.icon = image
            btn = self._statusitem.button()
            if btn:
                btn.setContentTintColor_(NSColor.systemRedColor() if red else None)
        except Exception:
            pass

    # ── Timer helpers ────────────────────────────────────────────────────────

    def _start_tick(self) -> None:
        if not self._tick_running:
            self._tick_timer.start()
            self._tick_running = True

    def _stop_tick(self) -> None:
        if self._tick_running:
            self._tick_timer.stop()
            self._tick_running = False

    def _update_tick(self, _sender) -> None:
        start = self._bridge.session_start
        if self._bridge.state == AppState.RECORDING and start:
            elapsed = datetime.now() - start
            formatted = self._format_duration(elapsed)
            self._status_item.title = f"\u25CF Recording  {formatted}"
            self._duration_item.title = f"Duration: {formatted}"

    def _drain_events(self, _sender) -> None:
        self._bridge.drain_events()

    @staticmethod
    def _format_duration(td: timedelta) -> str:
        total = int(td.total_seconds())
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    # ── DaemonBridge callbacks (main thread) ──────────────────────────────────

    def _on_state_change(self, state: AppState) -> None:
        self._apply_state(state)

    def _on_segment_saved(self, path: str, duration: float) -> None:
        filename = os.path.basename(path)
        self._last_saved_item.title = f"Last saved: {filename}"
        self._segments_item.title = f"Segments: {self._bridge.segment_count}"
        rumps.notification(
            "Recording Saved",
            filename,
            f"Duration: {self._format_duration(timedelta(seconds=duration))}",
            sound=True,
        )

    def _on_error(self, message: str) -> None:
        truncated = (message[:57] + "\u2026") if len(message) > 60 else message
        self._status_item.title = f"Error: {truncated}"
        rumps.notification("auto-rb-recorder", "Error", truncated)

    # ── Menu item callbacks ───────────────────────────────────────────────────

    def _open_recordings_folder(self, _) -> None:
        os.makedirs(self.config.output_dir, exist_ok=True)
        subprocess.run(["open", self.config.output_dir])

    def _open_preferences(self, _) -> None:
        if self._prefs_window is None:
            from src.ui.preferences_window import PreferencesWindowController
            self._prefs_window = (
                PreferencesWindowController.alloc()
                .initWithConfig_callback_(self.config, self._on_prefs_saved)
            )
        self._prefs_window.showWindow()

    def _on_prefs_saved(self, new_config: Config) -> None:
        self.config = new_config
        self._bridge.config = new_config

    def _toggle_launch_at_login(self, _sender) -> None:
        from src import login_item
        if login_item.is_enabled():
            login_item.disable()
        else:
            login_item.enable(sys.argv[0])
        self._refresh_launch_at_login()

    def _refresh_launch_at_login(self) -> None:
        from src import login_item
        try:
            self.menu["Launch at Login"].state = login_item.is_enabled()
        except Exception:
            pass

    def _show_about(self, _) -> None:
        rumps.alert(
            title="auto-rb-recorder",
            message=(
                "Automatic Rekordbox session recorder.\n\n"
                "https://github.com/icherniukh/auto-rb-recorder"
            ),
        )

    def _quit(self, _) -> None:
        if self._bridge.state == AppState.RECORDING:
            response = rumps.alert(
                title="Recording in Progress",
                message="A recording is in progress. Quit and save the current segment?",
                ok="Quit and Save",
                cancel="Cancel",
            )
            if not response:
                return
        self._bridge.stop()
        rumps.quit_application()

    # ── App lifecycle ─────────────────────────────────────────────────────────

    def run(self) -> None:
        self._bridge.start()
        super().run()
