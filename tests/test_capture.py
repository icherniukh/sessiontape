import os
import queue
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

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


class NoopStopBackend(StubBackend):
    def stop(self, proc):
        super().stop(proc)


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
                self.assertEqual(
                    os.path.basename(args[0]), "sessiontape-capture-win.exe"
                )
                self.assertIn("--pid", args)
            else:
                self.assertEqual(os.path.basename(args[0]), "mac-capture")
            self.assertIn("12345", args)

            # Three threads are started: stderr logger + PCM reader + metrics ticker
            self.assertEqual(mock_thread.call_count, 3)
            self.assertEqual(mock_thread.return_value.start.call_count, 3)
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

    @patch("src.capture.threading.Thread")
    def test_stop_forces_cleanup_when_backend_leaves_process_running(self, mock_thread):
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, None, 0]
        backend = NoopStopBackend(mock_proc)

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
            cap.stop()

        self.assertIs(backend.stopped_proc, mock_proc)
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_called_once_with(timeout=2.0)

    @patch("src.backends.macos_capture.subprocess.Popen")
    def test_mac_capture_backend_passes_cli_flags(self, mock_popen):
        from src.backends.macos_capture import MacCaptureBackend

        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        backend = MacCaptureBackend(
            backend_mode="tap",
            metrics=False,
            diag=True,
            log_path="/tmp/mac-capture.log",
        )
        backend.start(12345, 48000)

        args = mock_popen.call_args[0][0]
        self.assertEqual(os.path.basename(args[0]), "mac-capture")
        self.assertEqual(
            args[1:],
            [
                "12345",
                "48000",
                "--backend",
                "tap",
                "--diag",
                "--no-metrics",
                "--log",
                "/tmp/mac-capture.log",
            ],
        )

    @patch("src.backends.windows_capture.subprocess.Popen")
    def test_windows_backend_suppresses_window(self, mock_popen):
        import subprocess

        from src.backends.windows_capture import WindowsCaptureBackend

        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        backend = WindowsCaptureBackend()
        backend.start(12345, 48000)

        args, kwargs = mock_popen.call_args
        self.assertEqual(os.path.basename(args[0][0]), "sessiontape-capture-win.exe")
        self.assertEqual(args[0][1:], ["--pid", "12345", "--sample-rate", "48000"])

        if sys.platform == "win32":
            expected_flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(
                subprocess, "CREATE_NO_WINDOW", 0x08000000
            )
            self.assertEqual(kwargs.get("creationflags"), expected_flags)


if __name__ == "__main__":
    unittest.main()


class TestChunkAssembly(unittest.TestCase):
    def _run_child(self, write_size, writes, delay, bufsize):
        import subprocess

        code = (
            "import sys,time\n"
            f"for _ in range({writes}):\n"
            f"    sys.stdout.buffer.write(b'\\x01\\x00'*{write_size // 2}); sys.stdout.buffer.flush()\n"
            f"    time.sleep({delay})\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, bufsize=bufsize
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            cap = AudioCapture(
                pid=1,
                output_dir=tmpdir,
                queue=queue.Queue(),
                sample_rate=48000,
                backend=StubBackend(proc),
                perf_metrics=False,
            )
            sizes, gaps = [], []
            cap.recorder.process_chunk = lambda c: sizes.append(len(c))
            cap._metrics.record_chunk = lambda gap_ms, process_ms: gaps.append(gap_ms)
            cap._proc = proc
            cap._read_loop()
            return cap.chunk_size, sizes, gaps

    def test_short_reads_are_assembled_into_full_chunks(self):
        # 1920 B per 10 ms callback, unbuffered pipe (the bufsize=0 bug); 1002 is misaligned.
        for write_size in (1920, 1002):
            chunk_size, sizes, _ = self._run_child(write_size, 30, 0.005, bufsize=0)
            self.assertEqual(chunk_size, 19200)
            self.assertGreater(len(sizes), 1)
            self.assertTrue(all(s == chunk_size for s in sizes[:-1]), sizes)
            self.assertEqual(sizes[-1] % 4, 0)

    def test_gap_ms_tracks_chunk_duration(self):
        # 20 writes of 1920 B every 20 ms -> one 19200 B chunk per ~200 ms.
        _, sizes, gaps = self._run_child(1920, 20, 0.02, bufsize=0)
        self.assertEqual(sizes[:2], [19200, 19200])
        self.assertGreater(gaps[1], 150.0)

    def test_format_mismatch_line_sets_rate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cap = AudioCapture(
                pid=1, output_dir=tmpdir, queue=queue.Queue(), backend=StubBackend(None)
            )
            cap._proc = MagicMock()
            cap._proc.stderr = [b"FORMAT_MISMATCH tap=44100 requested=48000\n"]
            cap._log_stderr()
            self.assertEqual(cap.format_mismatch_rate, 44100)
