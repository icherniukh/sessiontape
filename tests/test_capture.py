import queue
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import os

from src.capture import AudioCapture
# from src.backends.macos_capture import AudioteeCaptureBackend
from src.recorder_core import db_to_rms


class StubBackend:
    def __init__(self, proc):
        self.proc = proc
        self.started_with = None
        self.stopped_proc = None

    def start(self, pid: int, sample_rate: int):
        self.started_with = (pid, sample_rate)
        return self.proc

    def stop(self, proc):
        self.stopped_proc = proc


class TestAudioCapture(unittest.TestCase):
    def test_db_to_rms(self):
        rms = db_to_rms(-40.0)
        self.assertTrue(320 < rms < 330)

    @patch("src.capture.threading.Thread")
    @patch("src.backends.macos_capture.subprocess.Popen")
    def test_capture_lifecycle(self, mock_popen, mock_thread):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        with tempfile.TemporaryDirectory() as tmpdir:
            q = queue.Queue()
            
            # 1. Stop without start is no-op
            cap = AudioCapture(pid=12345, output_dir=tmpdir, queue=q, sample_rate=48000)
            cap.stop()
            self.assertFalse(cap.is_recording)

            # 2. Start spawns backend and threads
            cap.start()
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            if sys.platform == "win32":
                self.assertEqual(os.path.basename(args[0]), "sessiontape-capture-win.exe")
                self.assertIn("--pid", args)
            else:
                self.assertEqual(os.path.basename(args[0]), "mac-capture")
            self.assertIn("12345", args)

            # Two threads are started: stderr logger + PCM reader
            self.assertEqual(mock_thread.call_count, 2)
            self.assertEqual(mock_thread.return_value.start.call_count, 2)
            self.assertTrue(cap.is_recording)
            self.assertEqual(cap.recorder.state, "PASSIVE")

            # 3. Stop finalizes recorder and terminates process
            cap.recorder.finalize = MagicMock()
            cap.stop()
            cap.recorder.finalize.assert_called_once()
            mock_proc.terminate.assert_called_once()
            self.assertFalse(cap.is_recording)

            # 4. Stop skips terminate if process already exited
            mock_proc.reset_mock()
            mock_proc.poll.return_value = 0
            cap.start()
            cap.stop()
            mock_proc.terminate.assert_not_called()

    @patch("src.capture.threading.Thread")
    @patch("src.backends.macos_capture.subprocess.Popen")
    def test_start_can_use_injected_backend(self, mock_popen, mock_thread):
        mock_proc = MagicMock()
        backend = StubBackend(mock_proc)

        with tempfile.TemporaryDirectory() as tmpdir:
            q = queue.Queue()
            cap = AudioCapture(
                pid=12345,
                output_dir=tmpdir,
                queue=q,
                sample_rate=44100,
                backend=backend,
            )
            cap.start()

            mock_popen.assert_not_called()
            self.assertEqual(backend.started_with, (12345, 44100))
            cap.stop()
            self.assertIs(backend.stopped_proc, mock_proc)

    @patch("src.backends.windows_capture.subprocess.Popen")
    def test_windows_backend_suppresses_window(self, mock_popen):
        from src.backends.windows_capture import WindowsCaptureBackend
        import subprocess

        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        backend = WindowsCaptureBackend()
        backend.start(12345, 48000)

        args, kwargs = mock_popen.call_args
        self.assertEqual(os.path.basename(args[0][0]), "sessiontape-capture-win.exe")
        self.assertEqual(args[0][1:], ["--pid", "12345", "--sample-rate", "48000"])

        if sys.platform == "win32":
            expected_flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            self.assertEqual(kwargs.get("creationflags"), expected_flags)


if __name__ == "__main__":
    unittest.main()
