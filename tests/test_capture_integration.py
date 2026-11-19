import os
import queue
import sys
import time
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.backends.windows_capture import WindowsCaptureBackend
from src.capture import AudioCapture


FIXTURE_DIR = Path(__file__).parent / "fixtures"
FAKE_AUDIOTEE = str(FIXTURE_DIR / "fake_audiotee.py")
FAKE_WINDOWS_CAPTURE = str(FIXTURE_DIR / "fake_windows_capture.py")


class FakeAudioteeBackend:
    def start(self, pid: int, sample_rate: int):
        import subprocess as _sp

        return _sp.Popen(
            [sys.executable, FAKE_AUDIOTEE, "--include-processes", str(pid), "--sample-rate", str(sample_rate)],
            stdout=_sp.PIPE,
            stderr=_sp.PIPE,
        )

    def stop(self, proc):
        import subprocess as _sp

        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except _sp.TimeoutExpired:
            proc.kill()
            proc.wait()


def _make_capture(tmpdir, q, **kwargs):
    """Create an AudioCapture that invokes the platform-appropriate fake capture process."""
    if sys.platform == "win32":
        from src.backends.windows_capture import WindowsCaptureBackend
        import subprocess as _sp

        class FakeWindowsBackend(WindowsCaptureBackend):
            def start(self, pid: int, sample_rate: int) -> _sp.Popen:
                return _sp.Popen(
                    [sys.executable, FAKE_WINDOWS_CAPTURE, "--pid", str(pid), "--sample-rate", str(sample_rate)],
                    stdout=_sp.PIPE,
                    stderr=_sp.DEVNULL,
                    creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0x08000000),
                )

        return AudioCapture(queue=q, backend=FakeWindowsBackend(), **kwargs)
    else:
        return AudioCapture(queue=q, backend=FakeAudioteeBackend(), **kwargs)


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("Timed out waiting for test condition")


def wav_frame_count(path: Path) -> int:
    with wave.open(str(path), "rb") as wav_file:
        return wav_file.getnframes()


class TestAudioCaptureIntegration(unittest.TestCase):
    def test_real_subprocess_stream_splits_into_multiple_wavs(self):
        with TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"SESSIONTAPE_TEST_AUDIO_SCENARIO": "sound:3,silence:3,sound:2"}, clear=False):
                q = queue.Queue()
                cap = _make_capture(
                    tmpdir,
                    q,
                    pid=12345,
                    output_dir=tmpdir,
                    sample_rate=48000,
                    min_silence_duration=0.2,
                    min_segment_duration=0.0,
                    decay_tail=0.0,
                    export_format="wav",
                )
                try:
                    cap.start()
                    wait_until(lambda: cap._proc is not None and cap._proc.poll() is not None)
                finally:
                    cap.stop()

            wait_until(lambda: len(list(Path(tmpdir).glob("rb_session_*.wav"))) == 2)
            wav_paths = sorted(Path(tmpdir).glob("rb_session_*.wav"))

            self.assertEqual(len(wav_paths), 2)
            self.assertEqual(wav_frame_count(wav_paths[0]), 24000)
            self.assertEqual(wav_frame_count(wav_paths[1]), 9600)

    def test_real_subprocess_stream_preserves_decay_tail(self):
        with TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"SESSIONTAPE_TEST_AUDIO_SCENARIO": "silence:2,sound:1"}, clear=False):
                q = queue.Queue()
                cap = _make_capture(
                    tmpdir,
                    q,
                    pid=12345,
                    output_dir=tmpdir,
                    sample_rate=48000,
                    min_silence_duration=0.2,
                    min_segment_duration=0.0,
                    decay_tail=0.2,
                    export_format="wav",
                )
                try:
                    cap.start()
                    wait_until(lambda: cap._proc is not None and cap._proc.poll() is not None)
                finally:
                    cap.stop()

            wait_until(lambda: len(list(Path(tmpdir).glob("rb_session_*.wav"))) == 1)
            wav_paths = list(Path(tmpdir).glob("rb_session_*.wav"))

            self.assertEqual(len(wav_paths), 1)
            self.assertEqual(wav_frame_count(wav_paths[0]), 9600)


class TestStdinCloseReadLoopExit(unittest.TestCase):
    def test_stop_via_stdin_close_terminates_read_loop_quickly(self):
        """Closing stdin must terminate the read loop within ~2s (not the 5s kill fallback)."""
        import subprocess as _sp

        class ContinuousFakeBackend(WindowsCaptureBackend):
            def start(self, pid: int, sample_rate: int) -> _sp.Popen:
                kwargs = {}
                if sys.platform == "win32":
                    kwargs["creationflags"] = getattr(_sp, "CREATE_NO_WINDOW", 0x08000000)
                return _sp.Popen(
                    [
                        sys.executable,
                        FAKE_WINDOWS_CAPTURE,
                        "--pid",
                        str(pid),
                        "--sample-rate",
                        str(sample_rate),
                        "--continuous",
                    ],
                    stdin=_sp.PIPE,
                    stdout=_sp.PIPE,
                    stderr=_sp.DEVNULL,
                    **kwargs,
                )

        with TemporaryDirectory() as tmpdir:
            q = queue.Queue()
            cap = AudioCapture(
                pid=12345,
                output_dir=tmpdir,
                queue=q,
                sample_rate=48000,
                backend=ContinuousFakeBackend(),
                export_format="wav",
            )
            cap.start()
            try:
                wait_until(
                    lambda: cap._proc is not None
                    and cap._last_read_at > 0
                    and cap.recorder._chunk_count >= 1,
                    timeout=3.0,
                )
                proc = cap._proc

                t0 = time.time()
                cap.stop()
                elapsed = time.time() - t0
            finally:
                cap.stop()

            self.assertEqual(proc.returncode, 0)
            self.assertLess(
                elapsed,
                3.0,
                f"stop() took {elapsed:.1f}s — stdin-close shutdown path appears broken",
            )


if __name__ == "__main__":
    unittest.main()
