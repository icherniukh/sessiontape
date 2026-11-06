import queue
import unittest
from unittest.mock import MagicMock, patch

from src.capture import EXIT_FORMAT_CHANGED, EXIT_FORMAT_MISMATCH
from src.config import Config
from src.daemon import RecorderDaemon
from src.events import (
    CaptureDied,
    ProcessReplaced,
    ProcessStarted,
    ProcessStopped,
    ShutdownRequested,
    TapBroken,
)


class TestRecorderDaemon(unittest.TestCase):
    @patch("src.daemon.AudioCapture")
    @patch("src.daemon.ProcessMonitor")
    @patch("src.daemon.recover_orphaned_raw_files")
    def test_start_recording_on_process_detected(
        self, MockRecover, MockMonitor, MockCapture
    ):
        cfg = Config(output_dir="/tmp/test_output")
        daemon = RecorderDaemon(cfg)

        daemon._start_capture(pid=12345)

        MockCapture.assert_called_once_with(
            pid=12345,
            output_dir="/tmp/test_output",
            queue=daemon._queue,
            sample_rate=48000,
            backend_name="auto",
            silence_threshold_db=-50,
            min_silence_duration=15,
            min_segment_duration=10,
            decay_tail=5,
            export_format="wav",
            export_manager=daemon._export_manager,
            perf_metrics=True,
            mac_capture_backend="auto",
            mac_capture_metrics=True,
            mac_capture_diag=False,
            mac_capture_log="",
        )
        MockCapture.return_value.start.assert_called_once()

    @patch("src.daemon.os.path.exists", return_value=True)
    @patch("src.daemon.AudioCapture")
    @patch("src.daemon.ProcessMonitor")
    @patch("src.daemon.recover_orphaned_raw_files")
    def test_stop_recording_on_process_exit(
        self, MockRecover, MockMonitor, MockCapture, mock_exists
    ):
        cfg = Config(output_dir="/tmp/test_output")
        daemon = RecorderDaemon(cfg)

        mock_capture = MagicMock()
        daemon._capture = mock_capture

        daemon._stop_capture()

        mock_capture.stop.assert_called_once()
        self.assertIsNone(daemon._capture)

    @patch("src.daemon.AudioCapture")
    @patch("src.daemon.ProcessMonitor")
    @patch("src.daemon.recover_orphaned_raw_files")
    @patch("src.daemon.signal.signal")
    @patch("src.daemon._cleanup_stale_processes")
    def test_run_loop_handles_events(
        self, MockCleanup, MockSignal, MockRecover, MockMonitor, MockCapture
    ):
        cfg = Config(output_dir="/tmp/test_output")
        daemon = RecorderDaemon(cfg)

        # Feed events into the queue
        daemon._queue.put(ProcessStarted(pid=12345))
        daemon._queue.put(CaptureDied(exit_code=1))
        daemon._queue.put(TapBroken())
        daemon._queue.put(ProcessStopped(pid=12345))
        daemon._queue.put(ShutdownRequested())

        # Mock start/stop capture but ensure pid state is maintained
        def side_effect_start(pid):
            daemon._current_pid = pid

        def side_effect_stop(keep_pid=False, **kwargs):
            if not keep_pid:
                daemon._current_pid = None
            daemon._capture = None

        daemon._start_capture = MagicMock(side_effect=side_effect_start)
        daemon._stop_capture = MagicMock(side_effect=side_effect_stop)

        daemon.run()

        # Verify event processing
        self.assertEqual(
            daemon._start_capture.call_count, 3
        )  # ProcessStarted, CaptureDied (restart), TapBroken (restart)
        self.assertEqual(
            daemon._stop_capture.call_count, 4
        )  # CaptureDied (stop old), TapBroken (stop old), ProcessStopped, finally cleanup

        # Verify monitor lifecycle
        MockMonitor.return_value.start.assert_called_once()
        MockMonitor.return_value.stop.assert_called_once()

    @patch("src.daemon.AudioCapture")
    @patch("src.daemon.ProcessMonitor")
    @patch("src.daemon.recover_orphaned_raw_files")
    @patch("src.daemon.signal.signal")
    @patch("src.daemon._cleanup_stale_processes")
    def test_restarts_capture_on_process_replacement(
        self, MockCleanup, MockSignal, MockRecover, MockMonitor, MockCapture
    ):
        cfg = Config(output_dir="/tmp/test_output")
        daemon = RecorderDaemon(cfg)

        daemon._queue.put(ProcessStarted(pid=12345))
        daemon._queue.put(ProcessReplaced(old_pid=12345, new_pid=67890))
        daemon._queue.put(ShutdownRequested())

        def side_effect_start(pid):
            daemon._current_pid = pid

        def side_effect_stop(keep_pid=False, **kwargs):
            if not keep_pid:
                daemon._current_pid = None
            daemon._capture = None

        daemon._start_capture = MagicMock(side_effect=side_effect_start)
        daemon._stop_capture = MagicMock(side_effect=side_effect_stop)

        daemon.run()

        self.assertEqual(
            [call.args[0] for call in daemon._start_capture.call_args_list],
            [12345, 67890],
        )
        self.assertEqual(
            daemon._stop_capture.call_count, 2
        )  # replacement + finally cleanup
        self.assertEqual(daemon._current_pid, None)


if __name__ == "__main__":
    unittest.main()


class TestFormatMismatch(unittest.TestCase):
    @patch("src.daemon.AudioCapture")
    @patch("src.daemon.ProcessMonitor")
    @patch("src.daemon.recover_orphaned_raw_files")
    @patch("src.daemon.signal.signal")
    @patch("src.daemon._cleanup_stale_processes")
    def test_exit_3_does_not_restart_and_logs_rate(
        self, MockCleanup, MockSignal, MockRecover, MockMonitor, MockCapture
    ):
        daemon = RecorderDaemon(Config(output_dir="/tmp/test_output"))
        daemon._queue.put(ProcessStarted(pid=12345))
        daemon._queue.put(CaptureDied(exit_code=3))
        daemon._queue.put(ShutdownRequested())

        def side_effect_start(pid):
            daemon._current_pid = pid
            daemon._capture = MagicMock(format_mismatch_rate=44100)

        daemon._start_capture = MagicMock(side_effect=side_effect_start)
        daemon._stop_capture = MagicMock()

        with self.assertLogs("sessiontape", level="CRITICAL") as logs:
            daemon.run()

        self.assertEqual(daemon._start_capture.call_count, 1)
        self.assertEqual(daemon._restart_count, 0)
        self.assertIn("runs at 44100 Hz", logs.output[0])
        self.assertIn("recording.sample_rate = 44100", logs.output[0])

    @patch("src.daemon.AudioCapture")
    @patch("src.daemon.ProcessMonitor")
    @patch("src.daemon.recover_orphaned_raw_files")
    @patch("src.daemon.signal.signal")
    @patch("src.daemon._cleanup_stale_processes")
    def test_exit_4_format_changed_restarts_capture(
        self, MockCleanup, MockSignal, MockRecover, MockMonitor, MockCapture
    ):
        daemon = RecorderDaemon(Config(output_dir="/tmp/test_output"))
        daemon._queue.put(ProcessStarted(pid=12345))
        daemon._queue.put(CaptureDied(exit_code=EXIT_FORMAT_CHANGED))
        daemon._queue.put(ShutdownRequested())

        def side_effect_start(pid):
            daemon._current_pid = pid
            daemon._capture = MagicMock(format_mismatch_rate=None)

        daemon._start_capture = MagicMock(side_effect=side_effect_start)
        daemon._stop_capture = MagicMock()

        with self.assertLogs("sessiontape", level="WARNING") as logs:
            daemon.run()

        self.assertEqual(EXIT_FORMAT_CHANGED, 4)
        self.assertNotEqual(EXIT_FORMAT_CHANGED, EXIT_FORMAT_MISMATCH)
        # Initial start plus one restart on the same PID.
        self.assertEqual(daemon._start_capture.call_count, 2)
        daemon._start_capture.assert_called_with(12345)
        self.assertEqual(daemon._restart_count, 1)
        self.assertTrue(any("tap format changed" in line for line in logs.output))
