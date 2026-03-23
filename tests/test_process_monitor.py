import unittest
from unittest.mock import patch
from src.process_monitor import ProcessMonitor


class TestProcessMonitor(unittest.TestCase):
    @patch("src.process_monitor.psutil.process_iter")
    def test_find_pid_matches_stem(self, mock_process_iter):
        mock_process_iter.return_value = [
            type("Proc", (), {"info": {"pid": 42, "name": "rekordbox.exe"}})()
        ]
        mon = ProcessMonitor("rekordbox")
        self.assertEqual(mon._find_pid(), 42)

    @patch("src.process_monitor.time.sleep")
    @patch("src.process_monitor.ProcessMonitor._find_pid")
    def test_detects_process_start(self, mock_find, mock_sleep):
        # Poll: None, None, 12345 (detected), re-check after delay: 12345 (confirmed)
        mock_find.side_effect = [None, None, 12345, 12345]
        events = []
        mon = ProcessMonitor("rekordbox", poll_interval=0, startup_delay=0, stop_delay=0)
        mon.on_start = lambda pid: events.append(("start", pid))
        mon.on_stop = lambda: events.append(("stop",))
        for _ in range(3):
            mon.poll_once()
        self.assertEqual(events, [("start", 12345)])

    @patch("src.process_monitor.time.sleep")
    @patch("src.process_monitor.ProcessMonitor._find_pid")
    def test_detects_process_stop(self, mock_find, mock_sleep):
        # Poll 1: 12345 (start+confirm). Poll 2: 12345. Poll 3: None, re-check: None (stop)
        mock_find.side_effect = [12345, 12345, 12345, None, None]
        events = []
        mon = ProcessMonitor("rekordbox", poll_interval=0, startup_delay=0, stop_delay=0)
        mon.on_start = lambda pid: events.append(("start", pid))
        mon.on_stop = lambda: events.append(("stop",))
        for _ in range(3):
            mon.poll_once()
        self.assertEqual(events, [("start", 12345), ("stop",)])

    @patch("src.process_monitor.time.sleep")
    @patch("src.process_monitor.ProcessMonitor._find_pid")
    def test_ignores_duplicate_start(self, mock_find, mock_sleep):
        mock_find.side_effect = [12345, 12345, 12345, 12345]
        events = []
        mon = ProcessMonitor("rekordbox", poll_interval=0, startup_delay=0, stop_delay=0)
        mon.on_start = lambda pid: events.append(("start", pid))
        mon.on_stop = lambda: events.append(("stop",))
        for _ in range(3):
            mon.poll_once()
        self.assertEqual(events, [("start", 12345)])

    @patch("src.process_monitor.time.sleep")
    @patch("src.process_monitor.ProcessMonitor._find_pid")
    def test_ignores_transient_process(self, mock_find, mock_sleep):
        # Process appears then disappears during startup delay
        mock_find.side_effect = [12345, None]
        events = []
        mon = ProcessMonitor("rekordbox", poll_interval=0, startup_delay=0, stop_delay=0)
        mon.on_start = lambda pid: events.append(("start", pid))
        mon.on_stop = lambda: events.append(("stop",))
        mon.poll_once()
        self.assertEqual(events, [])

    @patch("src.process_monitor.time.sleep")
    @patch("src.process_monitor.ProcessMonitor._find_pid")
    def test_ignores_brief_disappearance(self, mock_find, mock_sleep):
        # Process starts, then briefly disappears but comes back during stop delay
        # Poll 1: 12345 (start+confirm). Poll 2: None (gone), re-check: 67890 (back!)
        mock_find.side_effect = [12345, 12345, None, 67890]
        events = []
        mon = ProcessMonitor("rekordbox", poll_interval=0, startup_delay=0, stop_delay=0)
        mon.on_start = lambda pid: events.append(("start", pid))
        mon.on_stop = lambda: events.append(("stop",))
        mon.poll_once()  # start
        mon.poll_once()  # brief disappearance — should NOT fire stop
        self.assertEqual(events, [("start", 12345)])
        self.assertEqual(mon._current_pid, 67890)  # PID updated


if __name__ == "__main__":
    unittest.main()
