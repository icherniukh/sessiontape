from datetime import datetime
import unittest

from src.app_state import AppState
from src.config import Config
from src.daemon_bridge import DaemonBridge


class TestDaemonBridge(unittest.TestCase):
    def test_capture_and_segment_events_update_state(self):
        bridge = DaemonBridge(Config())
        observed_states = []
        saved_segments = []

        bridge.on_state_change = observed_states.append
        bridge.on_segment_saved = lambda path, duration: saved_segments.append(
            (path, duration)
        )

        bridge._event_queue.put(("daemon_state", ("monitoring", {})))
        bridge._event_queue.put(("capture_active", None))
        bridge._event_queue.put(("segment_saved", ("/tmp/rb_session.wav", 12.5)))

        bridge.drain_events()

        self.assertEqual(
            observed_states,
            [AppState.MONITORING, AppState.RECORDING, AppState.MONITORING],
        )
        self.assertEqual(saved_segments, [("/tmp/rb_session.wav", 12.5)])
        self.assertEqual(bridge.state, AppState.MONITORING)
        self.assertEqual(bridge.segment_count, 1)
        self.assertEqual(bridge.last_saved_path, "/tmp/rb_session.wav")
        self.assertIsInstance(bridge.session_start, datetime)

    def test_error_event_transitions_to_error_state(self):
        bridge = DaemonBridge(Config())
        observed_states = []
        observed_errors = []

        bridge.on_state_change = observed_states.append
        bridge.on_error = observed_errors.append

        bridge._event_queue.put(("error", "boom"))
        bridge.drain_events()

        self.assertEqual(observed_errors, ["boom"])
        self.assertEqual(observed_states, [AppState.ERROR])
        self.assertEqual(bridge.state, AppState.ERROR)
