import queue
import unittest
from unittest.mock import patch, MagicMock
from src.daemon import RecorderDaemon
from src.config import Config
from src.events import ProcessStarted, ProcessStopped, CaptureDied, ShutdownRequested, TapBroken


class TestRecorderDaemon(unittest.TestCase):
    @patch("src.daemon.AudioCapture")
    @patch("src.daemon.ProcessMonitor")
    @patch("src.daemon.recover_orphaned_raw_files")
    def test_start_recording_on_process_detected(self, MockRecover, MockMonitor, MockCapture):
        cfg = Config(output_dir="/tmp/test_output")
        daemon = RecorderDaemon(cfg)

        daemon._start_capture(pid=12345)

        MockCapture.assert_called_once_with(
            pid=12345, 
            output_dir="/tmp/test_output", 
            queue=daemon._queue,
            sample_rate=48000,
            silence_threshold_db=-50,
            min_silence_duration=15,
            decay_tail=5,
            export_format='wav'
        )
        MockCapture.return_value.start.assert_called_once()

    @patch("src.daemon.os.path.exists", return_value=True)
    @patch("src.daemon.AudioCapture")
    @patch("src.daemon.ProcessMonitor")
    @patch("src.daemon.recover_orphaned_raw_files")
    def test_stop_recording_on_process_exit(self, MockRecover, MockMonitor, MockCapture, mock_exists):
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
    def test_run_loop_handles_events(self, MockSignal, MockRecover, MockMonitor, MockCapture):
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
        self.assertEqual(daemon._start_capture.call_count, 3) # ProcessStarted, CaptureDied (restart), TapBroken (restart)
        self.assertEqual(daemon._stop_capture.call_count, 4)  # CaptureDied (stop old), TapBroken (stop old), ProcessStopped, finally cleanup
        
        # Verify monitor lifecycle
        MockMonitor.return_value.start.assert_called_once()
        MockMonitor.return_value.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
