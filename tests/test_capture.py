import os
import queue
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from src.capture import AudioCapture
from src.recorder import db_to_rms, RecorderState


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
    def test_capture_lifecycle(self, mock_thread):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        backend = StubBackend(mock_proc)

        with tempfile.TemporaryDirectory() as tmpdir:
            q = queue.Queue()
            cap = AudioCapture(pid=12345, output_dir=tmpdir, queue=q, sample_rate=48000, backend=backend)

            # Stop without start is a no-op
            cap.stop()
            self.assertFalse(cap.is_recording)
            self.assertIsNone(backend.stopped_proc)

            # Start delegates to backend and spawns reader + stderr threads
            cap.start()
            self.assertEqual(backend.started_with, (12345, 48000))
            self.assertEqual(mock_thread.call_count, 2)
            self.assertEqual(mock_thread.return_value.start.call_count, 2)
            self.assertTrue(cap.is_recording)
            self.assertEqual(cap.recorder.state, RecorderState.PASSIVE)

            # Stop delegates to backend and finalizes the recorder
            cap.recorder.finalize = MagicMock()
            cap.stop()
            cap.recorder.finalize.assert_called_once()
            self.assertIs(backend.stopped_proc, mock_proc)
            self.assertFalse(cap.is_recording)

            # Second stop while already stopped is a no-op
            backend.stopped_proc = None
            cap.stop()
            self.assertIsNone(backend.stopped_proc)

    @patch("src.capture.threading.Thread")
    def test_start_can_use_injected_backend(self, _mock_thread):
        mock_proc = MagicMock()
        backend = StubBackend(mock_proc)

        with tempfile.TemporaryDirectory() as tmpdir:
            q = queue.Queue()
            cap = AudioCapture(pid=12345, output_dir=tmpdir, queue=q, sample_rate=44100, backend=backend)
            cap.start()
            self.assertEqual(backend.started_with, (12345, 44100))
            cap.stop()
            self.assertIs(backend.stopped_proc, mock_proc)


class TestWindowsCaptureBackend(unittest.TestCase):
    @patch("src.backends.windows_capture.subprocess.Popen")
    def test_start_passes_pid_and_sample_rate(self, mock_popen):
        from src.backends.windows_capture import WindowsCaptureBackend

        mock_popen.return_value = MagicMock()
        WindowsCaptureBackend().start(12345, 48000)

        args, kwargs = mock_popen.call_args
        cmd = args[0]
        self.assertEqual(os.path.basename(cmd[0]), "sessiontape-capture-win.exe")
        self.assertEqual(cmd[1:], ["--pid", "12345", "--sample-rate", "48000"])
        self.assertEqual(kwargs.get("stdin"), subprocess.PIPE)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only")
    @patch("src.backends.windows_capture.subprocess.Popen")
    def test_start_suppresses_console_window(self, mock_popen):
        from src.backends.windows_capture import WindowsCaptureBackend

        mock_popen.return_value = MagicMock()
        WindowsCaptureBackend().start(12345, 48000)

        _, kwargs = mock_popen.call_args
        expected = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        self.assertEqual(kwargs.get("creationflags"), expected)

    @patch("src.backends.windows_capture.subprocess.Popen")
    def test_stop_closes_stdin_for_graceful_shutdown(self, mock_popen):
        from src.backends.windows_capture import WindowsCaptureBackend

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        backend = WindowsCaptureBackend()
        proc = backend.start(12345, 48000)
        backend.stop(proc)

        mock_proc.stdin.close.assert_called_once()

    @patch("src.backends.windows_capture.subprocess.Popen")
    def test_stop_skips_stdin_close_when_process_already_exited(self, mock_popen):
        from src.backends.windows_capture import WindowsCaptureBackend

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc

        backend = WindowsCaptureBackend()
        proc = backend.start(12345, 48000)
        backend.stop(proc)

        mock_proc.stdin.close.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "Windows-only")
class TestWindowsCaptureBinaryShutdown(unittest.TestCase):

    def _find_binary(self):
        candidate = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "windows-capture", "sessiontape-capture-win.exe")
        )
        if os.path.exists(candidate):
            return candidate
        return shutil.which("sessiontape-capture-win.exe")

    def test_stdin_close_exits_cleanly(self):
        """Closing the stdin pipe must cause a clean exit (code 0) without a forceful kill."""
        exe = self._find_binary()
        if not exe:
            self.skipTest("sessiontape-capture-win.exe not built")

        proc = subprocess.Popen(
            [exe, "--pid", str(os.getpid())],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        )

        time.sleep(0.4)

        if proc.poll() is not None:
            self.skipTest(
                f"Process exited early (code={proc.returncode}), "
                "WASAPI activation failed — cannot test shutdown"
            )

        proc.stdin.close()

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            self.fail("Process hung after stdin close (missing shutdown poll in capture loop)")

        self.assertEqual(proc.returncode, 0, f"Expected clean exit code 0, got {proc.returncode}")


if __name__ == "__main__":
    unittest.main()
