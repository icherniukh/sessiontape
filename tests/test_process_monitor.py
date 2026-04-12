import queue
import unittest
from unittest.mock import patch
from src.process_monitor import ProcessMonitor
from src.events import ProcessReplaced, ProcessStarted, ProcessStopped


class TestProcessMonitor(unittest.TestCase):
    @patch("src.process_monitor.psutil.process_iter")
    def test_find_pid_matches_stem(self, mock_process_iter):
        class MockProc:
            def __init__(self, pid, name, create_time=0.0):
                self.info = {"pid": pid, "name": name, "create_time": create_time}
                self.pid = pid

        mock_process_iter.return_value = [MockProc(42, "rekordbox.exe")]
        q = queue.Queue()
        mon = ProcessMonitor("rekordbox", queue=q)
        self.assertEqual(mon._find_pid(), 42)

    @patch("src.process_monitor.ProcessMonitor._find_matching_pids")
    def test_detects_process_start(self, mock_find):
        # Poll: none, none, 12345 (detected), re-check after delay: 12345 (confirmed)
        mock_find.side_effect = [[], [], [12345], [12345]]
        q = queue.Queue()
        mon = ProcessMonitor("rekordbox", queue=q, poll_interval=0, startup_delay=0, stop_delay=0)

        # Test 3 calls to _poll_once
        mon._poll_once()  # None
        mon._poll_once()  # None
        mon._poll_once()  # 12345 -> ProcessStarted

        self.assertEqual(q.qsize(), 1)
        event = q.get()
        self.assertIsInstance(event, ProcessStarted)
        self.assertEqual(event.pid, 12345)

    @patch("src.process_monitor.ProcessMonitor._find_matching_pids")
    def test_detects_process_stop(self, mock_find):
        # Poll 1: 12345 (start+confirm). Poll 2: None, re-check: None (stop)
        mock_find.side_effect = [[12345], [12345], [], []]
        q = queue.Queue()
        mon = ProcessMonitor("rekordbox", queue=q, poll_interval=0, startup_delay=0, stop_delay=0)

        mon._poll_once()  # start
        mon._poll_once()  # stop

        self.assertEqual(q.qsize(), 2)
        e1 = q.get()
        self.assertIsInstance(e1, ProcessStarted)
        e2 = q.get()
        self.assertIsInstance(e2, ProcessStopped)
        self.assertEqual(e2.pid, 12345)

    @patch("src.process_monitor.ProcessMonitor._find_matching_pids")
    def test_ignores_duplicate_start(self, mock_find):
        mock_find.side_effect = [[12345], [12345], [12345]]
        q = queue.Queue()
        mon = ProcessMonitor("rekordbox", queue=q, poll_interval=0, startup_delay=0, stop_delay=0)

        mon._poll_once()
        mon._poll_once()

        self.assertEqual(q.qsize(), 1)
        self.assertIsInstance(q.get(), ProcessStarted)

    @patch("src.process_monitor.ProcessMonitor._find_matching_pids")
    def test_ignores_transient_process(self, mock_find):
        # Process appears then disappears during startup delay
        mock_find.side_effect = [[12345], []]
        q = queue.Queue()
        mon = ProcessMonitor("rekordbox", queue=q, poll_interval=0, startup_delay=0, stop_delay=0)
        mon._poll_once()
        self.assertEqual(q.qsize(), 0)

    @patch("src.process_monitor.ProcessMonitor._find_matching_pids")
    def test_ignores_brief_disappearance(self, mock_find):
        # Process starts, then briefly disappears but comes back during stop delay
        # Poll 1: 12345 (start+confirm). Poll 2: None (gone), re-check: 67890 (back!)
        mock_find.side_effect = [[12345], [12345], [], [67890]]
        q = queue.Queue()
        mon = ProcessMonitor("rekordbox", queue=q, poll_interval=0, startup_delay=0, stop_delay=0)
        mon._poll_once()  # start
        mon._poll_once()  # brief disappearance — should NOT fire stop

        self.assertEqual(q.qsize(), 1)
        self.assertIsInstance(q.get(), ProcessStarted)
        self.assertEqual(mon._current_pid, 67890)  # PID updated

    @patch("src.process_monitor.ProcessMonitor._find_matching_pids")
    def test_emits_replacement_when_current_pid_disappears_but_process_remains(self, mock_find):
        # Poll 1: start on 12345. Poll 2: 12345 disappears, 67890 remains after debounce.
        mock_find.side_effect = [[12345], [12345], [67890], [67890]]
        q = queue.Queue()
        mon = ProcessMonitor("rekordbox", queue=q, poll_interval=0, startup_delay=0, stop_delay=0)

        mon._poll_once()  # start
        mon._poll_once()  # replace

        self.assertEqual(q.qsize(), 2)
        self.assertIsInstance(q.get(), ProcessStarted)
        event = q.get()
        self.assertIsInstance(event, ProcessReplaced)
        self.assertEqual(event.old_pid, 12345)
        self.assertEqual(event.new_pid, 67890)
        self.assertEqual(mon._current_pid, 67890)

    def test_stop_sets_stop_event(self):
        mon = ProcessMonitor(queue=queue.Queue(), process_name="test")
        self.assertFalse(mon._stop_event.is_set())
        mon.stop()
        self.assertTrue(mon._stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
