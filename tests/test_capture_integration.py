import os
import time
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.capture import AudioCapture


FIXTURE_DIR = Path(__file__).parent / "fixtures"
FAKE_AUDIOTEE = str(FIXTURE_DIR / "fake_audiotee.py")


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
            with patch("src.capture._find_executable", return_value=FAKE_AUDIOTEE):
                with patch.dict(
                    os.environ,
                    {"RB_TEST_AUDIO_SCENARIO": "sound:3,silence:3,sound:2"},
                    clear=False,
                ):
                    cap = AudioCapture(
                        pid=12345,
                        output_dir=tmpdir,
                        sample_rate=48000,
                        min_silence_duration=0.2,
                        decay_tail=0.0,
                        export_format="wav",
                    )
                    cap.start()

                    wait_until(lambda: cap._proc is not None and cap._proc.poll() is not None)
                    cap.stop()

            wait_until(lambda: len(list(Path(tmpdir).glob("rb_session_*.wav"))) == 2)
            wav_paths = sorted(Path(tmpdir).glob("rb_session_*.wav"))

            self.assertEqual(len(wav_paths), 2)
            self.assertEqual(wav_frame_count(wav_paths[0]), 24000)
            self.assertEqual(wav_frame_count(wav_paths[1]), 9600)

    def test_real_subprocess_stream_preserves_decay_tail(self):
        with TemporaryDirectory() as tmpdir:
            with patch("src.capture._find_executable", return_value=FAKE_AUDIOTEE):
                with patch.dict(
                    os.environ,
                    {"RB_TEST_AUDIO_SCENARIO": "silence:2,sound:1"},
                    clear=False,
                ):
                    cap = AudioCapture(
                        pid=12345,
                        output_dir=tmpdir,
                        sample_rate=48000,
                        min_silence_duration=0.2,
                        decay_tail=0.2,
                        export_format="wav",
                    )
                    cap.start()

                    wait_until(lambda: cap._proc is not None and cap._proc.poll() is not None)
                    cap.stop()

            wait_until(lambda: len(list(Path(tmpdir).glob("rb_session_*.wav"))) == 1)
            wav_paths = list(Path(tmpdir).glob("rb_session_*.wav"))

            self.assertEqual(len(wav_paths), 1)
            self.assertEqual(wav_frame_count(wav_paths[0]), 9600)


if __name__ == "__main__":
    unittest.main()
