import tempfile
import unittest
from unittest.mock import MagicMock, patch
import os

from src.capture import AudioCapture
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
    @patch("src.capture.subprocess.Popen")
    def test_start_spawns_audiotee_and_thread(self, mock_popen, mock_thread):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        with tempfile.TemporaryDirectory() as tmpdir:
            cap = AudioCapture(pid=12345, output_dir=tmpdir, sample_rate=48000)
            cap.start()

            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            self.assertEqual(os.path.basename(args[0]), "audiotee")
            self.assertIn("--flush", args)
            self.assertIn("12345", args)

            mock_thread.assert_called_once()
            mock_thread.return_value.start.assert_called_once()

            self.assertTrue(cap.is_recording)
            self.assertEqual(cap.recorder.state, "PASSIVE")
            cap.stop()

    def test_stop_without_start_is_noop(self):
        cap = AudioCapture(pid=12345, output_dir="/tmp", sample_rate=48000)
        cap.stop()
        self.assertFalse(cap.is_recording)

    @patch("src.capture.threading.Thread")
    @patch("src.capture.subprocess.Popen")
    def test_start_can_use_injected_backend(self, mock_popen, mock_thread):
        mock_proc = MagicMock()
        backend = StubBackend(mock_proc)

        with tempfile.TemporaryDirectory() as tmpdir:
            cap = AudioCapture(
                pid=12345,
                output_dir=tmpdir,
                sample_rate=44100,
                backend=backend,
            )
            cap.start()

            mock_popen.assert_not_called()
            self.assertEqual(backend.started_with, (12345, 44100))
            cap.stop()
            self.assertIs(backend.stopped_proc, mock_proc)

    @patch("src.capture.threading.Thread")
    @patch("src.capture.subprocess.Popen")
    def test_stop_finalizes_recorder(self, mock_popen, mock_thread):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        with tempfile.TemporaryDirectory() as tmpdir:
            cap = AudioCapture(pid=12345, output_dir=tmpdir)
            cap.start()
            cap.recorder.finalize = MagicMock()

            cap.stop()

            cap.recorder.finalize.assert_called_once()
            mock_proc.terminate.assert_called_once()

    @patch("src.capture.threading.Thread")
    @patch("src.capture.subprocess.Popen")
    def test_stop_skips_terminate_for_exited_process(self, mock_popen, mock_thread):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc

        with tempfile.TemporaryDirectory() as tmpdir:
            cap = AudioCapture(pid=12345, output_dir=tmpdir)
            cap.start()
            cap.stop()

            mock_proc.terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
