import queue
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.backends.macos_capture import MacCaptureOptions
from src.capture import (
    EXIT_CONFIG_ERROR,
    EXIT_FORMAT_CHANGED,
    EXIT_FORMAT_MISMATCH,
)
from src.config import Config
from src.daemon import RecorderDaemon
from src.events import (
    CaptureDied,
    ProcessReplaced,
    ProcessStarted,
    ProcessStopped,
    SegmentOpened,
    ShutdownRequested,
    TapBroken,
)


class TestRecorderDaemon(unittest.TestCase):
    @patch("src.daemon.get_platform_backend")
    @patch("src.daemon.AudioCapture")
    @patch("src.daemon.ProcessMonitor")
    @patch("src.daemon.recover_orphaned_raw_files")
    def test_start_recording_on_process_detected(
        self, MockRecover, MockMonitor, MockCapture, mock_get_backend
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Config(output_dir=tmpdir, mac_capture_diag=True)
            daemon = RecorderDaemon(cfg)

            daemon._start_capture(pid=12345)

            mock_get_backend.assert_called_once_with(
                "auto", mac_options=MacCaptureOptions(diag=True)
            )

            MockCapture.assert_called_once_with(
                pid=12345,
                output_dir=tmpdir,
                queue=daemon._queue,
                sample_rate=48000,
                backend=mock_get_backend.return_value,
                silence_threshold_db=-50,
                min_silence_duration=15,
                min_segment_duration=30,
                decay_tail=5,
                export_format="wav",
                export_manager=daemon._export_manager,
                perf_metrics=True,
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
    def test_rapid_crashes_during_active_audio_still_hit_restart_limit(
        self, MockCleanup, MockSignal, MockRecover, MockMonitor, MockCapture
    ):
        daemon = RecorderDaemon(Config(output_dir="/tmp/test_output"))
        daemon._queue.put(ProcessStarted(pid=12345))
        # Rapid crashes inside the 10-minute window must still stop at 5 even if audio opens a segment.
        for _ in range(7):
            daemon._queue.put(SegmentOpened(raw_path="/tmp/x.raw", output_path="/tmp/x.wav"))
            daemon._queue.put(CaptureDied(exit_code=1))
        daemon._queue.put(ShutdownRequested())

        def side_effect_start(pid):
            daemon._current_pid = pid

        daemon._start_capture = MagicMock(side_effect=side_effect_start)
        daemon._stop_capture = MagicMock()
        daemon.run()

        self.assertEqual(daemon._start_capture.call_count, 1 + 5)

    @patch("src.daemon.time.monotonic")
    @patch("src.daemon.AudioCapture")
    @patch("src.daemon.ProcessMonitor")
    @patch("src.daemon.recover_orphaned_raw_files")
    @patch("src.daemon.signal.signal")
    @patch("src.daemon._cleanup_stale_processes")
    def test_spaced_restarts_outside_window_do_not_exhaust_limit(
        self, MockCleanup, MockSignal, MockRecover, MockMonitor, MockCapture, mock_monotonic
    ):
        # Seven restarts spaced 11 minutes (660s > 600s window) apart across a multi-hour DJ set.
        clock = [1000.0 + i * 660.0 for i in range(7)]
        mock_monotonic.side_effect = clock

        daemon = RecorderDaemon(Config(output_dir="/tmp/test_output"))
        daemon._queue.put(ProcessStarted(pid=12345))
        for _ in range(7):
            daemon._queue.put(CaptureDied(exit_code=EXIT_FORMAT_CHANGED))
        daemon._queue.put(ShutdownRequested())

        def side_effect_start(pid):
            daemon._current_pid = pid

        daemon._start_capture = MagicMock(side_effect=side_effect_start)
        daemon._stop_capture = MagicMock()
        daemon.run()

        self.assertEqual(daemon._start_capture.call_count, 1 + 7)

    @patch("src.daemon.AudioCapture")
    @patch("src.daemon.ProcessMonitor")
    @patch("src.daemon.recover_orphaned_raw_files")
    @patch("src.daemon.signal.signal")
    @patch("src.daemon._cleanup_stale_processes")
    def test_tap_restart_limit_holds_without_audio(
        self, MockCleanup, MockSignal, MockRecover, MockMonitor, MockCapture
    ):
        daemon = RecorderDaemon(Config(output_dir="/tmp/test_output"))
        daemon._queue.put(ProcessStarted(pid=12345))
        for _ in range(7):
            daemon._queue.put(TapBroken())
        daemon._queue.put(ShutdownRequested())

        def side_effect_start(pid):
            daemon._current_pid = pid

        daemon._start_capture = MagicMock(side_effect=side_effect_start)
        daemon._stop_capture = MagicMock()
        daemon.run()

        self.assertEqual(daemon._start_capture.call_count, 1 + 5)

    @patch("src.daemon.AudioCapture")
    @patch("src.daemon.ProcessMonitor")
    @patch("src.daemon.recover_orphaned_raw_files")
    @patch("src.daemon.signal.signal")
    @patch("src.daemon._cleanup_stale_processes")
    def test_capture_died_restart_limit_stops_active_capture(
        self, MockCleanup, MockSignal, MockRecover, MockMonitor, MockCapture
    ):
        daemon = RecorderDaemon(Config(output_dir="/tmp/test_output"))
        daemon._queue.put(ProcessStarted(pid=12345))
        for _ in range(6):
            daemon._queue.put(CaptureDied(exit_code=1))
        daemon._queue.put(ShutdownRequested())

        def side_effect_start(pid):
            daemon._current_pid = pid
            daemon._capture = MagicMock()

        def side_effect_stop(keep_pid=False, **kwargs):
            daemon._capture = None
            if not keep_pid:
                daemon._current_pid = None

        daemon._start_capture = MagicMock(side_effect=side_effect_start)
        daemon._stop_capture = MagicMock(side_effect=side_effect_stop)
        daemon.run()

        self.assertEqual(daemon._start_capture.call_count, 1 + 5)
        self.assertIsNone(daemon._capture)
        # 5 restarts + 1 stop when giving up + 1 finally cleanup
        self.assertEqual(daemon._stop_capture.call_count, 7)

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
        self.assertTrue(any("tap format or HAL state changed" in line for line in logs.output))

    @patch("src.daemon.threading.current_thread")
    @patch("src.daemon.ProcessMonitor")
    @patch("src.daemon.recover_orphaned_raw_files")
    @patch("src.daemon.signal.signal")
    @patch("src.daemon._cleanup_stale_processes")
    def test_run_on_background_thread_skips_signal_handlers(
        self, MockCleanup, MockSignal, MockRecover, MockMonitor, mock_current_thread
    ):
        mock_current_thread.return_value = object()
        daemon = RecorderDaemon(Config(output_dir="/tmp/test_output"))
        daemon._queue.put(ShutdownRequested())
        daemon.run()
        MockSignal.assert_not_called()

    @patch("src.daemon._find_executable", return_value="ffmpeg")
    @patch("src.daemon.shutil.which", return_value=None)
    @patch("src.daemon.os.path.isfile", return_value=False)
    @patch("src.daemon.ProcessMonitor")
    @patch("src.daemon.recover_orphaned_raw_files")
    def test_mp3_warns_at_init_when_ffmpeg_missing(
        self, MockRecover, MockMonitor, mock_isfile, mock_which, mock_find
    ):
        with self.assertLogs("sessiontape", level="WARNING") as logs:
            RecorderDaemon(Config(output_dir="/tmp/test_output", export_format="mp3"))
        self.assertTrue(any("ffmpeg was not found" in line for line in logs.output))

    @patch("src.daemon.AudioCapture")
    @patch("src.daemon.ProcessMonitor")
    @patch("src.daemon.recover_orphaned_raw_files")
    @patch("src.daemon.signal.signal")
    @patch("src.daemon._cleanup_stale_processes")
    def test_exit_2_config_error_does_not_restart(
        self, MockCleanup, MockSignal, MockRecover, MockMonitor, MockCapture
    ):
        daemon = RecorderDaemon(Config(output_dir="/tmp/test_output"))
        daemon._queue.put(ProcessStarted(pid=12345))
        daemon._queue.put(CaptureDied(exit_code=EXIT_CONFIG_ERROR))
        daemon._queue.put(ShutdownRequested())

        def side_effect_start(pid):
            daemon._current_pid = pid
            daemon._capture = MagicMock(format_mismatch_rate=None)

        daemon._start_capture = MagicMock(side_effect=side_effect_start)
        daemon._stop_capture = MagicMock()

        with self.assertLogs("sessiontape", level="CRITICAL") as logs:
            daemon.run()

        self.assertEqual(EXIT_CONFIG_ERROR, 2)
        self.assertEqual(daemon._start_capture.call_count, 1)
        self.assertEqual(daemon._restart_count, 0)
        self.assertTrue(any("configuration error" in line for line in logs.output))

    @patch("src.daemon.psutil.process_iter")
    @patch("src.daemon.psutil.Process")
    @patch("src.daemon.os.getpid", return_value=2000)
    @patch("src.daemon.os.getppid", return_value=1999)
    def test_cleanup_stale_processes_kills_windows_exe_and_spares_ancestors(
        self, mock_getppid, mock_getpid, mock_process_cls, mock_process_iter
    ):
        from src.daemon import _cleanup_stale_processes

        grandparent = MagicMock(pid=1500)
        mock_process_cls.return_value.parents.return_value = [
            MagicMock(pid=1999),
            grandparent,
        ]

        stale_win = MagicMock(pid=888, info={"pid": 888, "name": "SessionTape.exe"})
        stale_cap = MagicMock(
            pid=889, info={"pid": 889, "name": "sessiontape-capture-win.exe"}
        )
        own_bootloader = MagicMock(
            pid=1500, info={"pid": 1500, "name": "sessiontape.exe"}
        )
        other_app = MagicMock(pid=900, info={"pid": 900, "name": "rekordbox.exe"})
        mock_process_iter.return_value = [
            stale_win,
            stale_cap,
            own_bootloader,
            other_app,
        ]

        _cleanup_stale_processes()

        stale_win.kill.assert_called_once()
        stale_cap.kill.assert_called_once()
        own_bootloader.kill.assert_not_called()
        other_app.kill.assert_not_called()

    def test_main_handles_none_stdio_and_logs_invalid_config(self):
        import os
        import sys
        from src.__main__ import main

        with tempfile.TemporaryDirectory() as tmpdir:
            bad_cfg = os.path.join(tmpdir, "config.toml")
            with open(bad_cfg, "w", encoding="utf-8") as f:
                f.write("[recording]\nsample_rate = 12345\n")

            with (
                patch.object(sys, "argv", ["sessiontape", "-c", bad_cfg]),
                patch.object(sys, "stdout", None),
                patch.object(sys, "stderr", None),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    main()
                self.assertEqual(ctx.exception.code, 2)

            log_path = os.path.join(tmpdir, "daemon.log")
            self.assertTrue(os.path.exists(log_path))
            with open(log_path, encoding="utf-8") as lf:
                contents = lf.read()
            self.assertIn("Failed to load configuration", contents)
