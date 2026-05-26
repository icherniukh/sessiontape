import json
import math
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from unittest.mock import MagicMock, patch

from src.config import Config
from src.events import ExportFinished, ExportStarted, SegmentClosed, SegmentOpened
from src.recorder_core import ExportManager, PCMStreamRecorder


def write_sine_raw(path: str, sample_rate: int, seconds: float = 0.5) -> None:
    frame_count = int(sample_rate * seconds)
    amplitude = 10000
    with open(path, "wb") as raw_file:
        for frame in range(frame_count):
            sample = int(amplitude * math.sin(2 * math.pi * 440 * frame / sample_rate))
            raw_file.write(struct.pack("<hh", sample, sample))


def ffprobe_audio_stream(path: str) -> dict:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise unittest.SkipTest("ffprobe is required for MP3 validity checks")

    output = subprocess.check_output(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels",
            "-of",
            "json",
            path,
        ],
        text=True,
    )
    streams = json.loads(output)["streams"]
    if not streams:
        raise AssertionError("No audio stream found")
    return streams[0]


class TestPCMStreamRecorder(unittest.TestCase):
    def test_calculate_rms(self):
        recorder = PCMStreamRecorder(output_dir="/tmp", sample_rate=48000)
        chunk = b"\x64\x00" * 4
        rms = recorder._calculate_rms(chunk)
        self.assertAlmostEqual(rms, 100.0)

    def test_transition_passive_to_active(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = PCMStreamRecorder(
                output_dir=tmpdir,
                on_tap_broken=lambda: None,
                sample_rate=48000,
                silence_threshold_db=-50,
                decay_tail=0,
            )

            loud_chunk = b"\xFF\x7F" * 10
            recorder.process_chunk(loud_chunk)

            self.assertEqual(recorder.state, "ACTIVE")
            self.assertIsNotNone(recorder._raw_file)
            self.assertIn("rb_session_", recorder._raw_path)
            self.assertIn("rb_session_", recorder._output_path)
            recorder._raw_file.close()

    def test_circular_buffer_limits(self):
        recorder = PCMStreamRecorder(output_dir="/tmp", on_tap_broken=lambda: None, decay_tail=5.0)
        self.assertEqual(recorder.buffer_maxlen, 50)
        self.assertEqual(recorder.ring_buffer.maxlen, 50)

        for i in range(60):
            recorder.ring_buffer.append(bytes([i % 255]))

        self.assertEqual(len(recorder.ring_buffer), 50)

    def test_mp3_export_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = PCMStreamRecorder(output_dir=tmpdir, on_tap_broken=lambda: None, export_format="mp3")
            recorder._open_new_file()
            self.assertTrue(recorder._output_path.endswith(".mp3"))
            self.assertTrue(recorder._raw_path.endswith(".raw"))
            recorder._raw_file.close()

    @patch("src.recorder_core.subprocess.run")
    @patch("src.recorder_core._find_executable", return_value="ffmpeg")
    def test_mp3_export_suppresses_window(self, mock_find, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        manager = ExportManager(
            sample_rate=48000,
            channels=2,
            bytes_per_sample=2,
            export_format="mp3",
        )
        manager._convert_mp3("in.raw", "out.mp3")

        import sys
        import subprocess
        args, kwargs = mock_run.call_args
        if sys.platform == "win32":
            self.assertEqual(kwargs.get("creationflags"), getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

    def test_mp3_config_produces_valid_mp3(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg is required for MP3 export")

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.toml")
            raw_path = os.path.join(tmpdir, "segment.raw")
            output_path = os.path.join(tmpdir, "segment.mp3")

            with open(config_path, "w") as config_file:
                config_file.write(
                    "[recording]\n"
                    "sample_rate = 44100\n"
                    f"output_dir = \"{tmpdir}\"\n"
                    "export_format = \"mp3\"\n"
                )

            cfg = Config.from_file(config_path)
            write_sine_raw(raw_path, cfg.sample_rate)

            manager = ExportManager(
                sample_rate=cfg.sample_rate,
                channels=2,
                bytes_per_sample=2,
                export_format=cfg.export_format,
            )
            manager._convert_mp3(raw_path, output_path)

            self.assertTrue(os.path.getsize(output_path) > 0)
            stream = ffprobe_audio_stream(output_path)
            self.assertEqual(stream["codec_name"], "mp3")
            self.assertEqual(int(stream["sample_rate"]), 44100)
            self.assertEqual(stream["channels"], 2)

    def test_wav_export_uses_configured_sample_rate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_path = os.path.join(tmpdir, "segment.raw")
            output_path = os.path.join(tmpdir, "segment.wav")
            write_sine_raw(raw_path, sample_rate=44100, seconds=0.1)

            manager = ExportManager(
                sample_rate=44100,
                channels=2,
                bytes_per_sample=2,
                export_format="wav",
            )
            manager._convert_wav(raw_path, output_path)

            with wave.open(output_path, "rb") as wav_file:
                self.assertEqual(wav_file.getframerate(), 44100)
                self.assertEqual(wav_file.getnchannels(), 2)

    def test_rejects_unsupported_export_format(self):
        with self.assertRaises(ValueError):
            PCMStreamRecorder(output_dir="/tmp", export_format="flac")

        with self.assertRaises(ValueError):
            ExportManager(
                sample_rate=48000,
                channels=2,
                bytes_per_sample=2,
                export_format="flac",
            )

    def test_finalize_exports_active_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            export_manager = MagicMock()
            recorder = PCMStreamRecorder(
                output_dir=tmpdir,
                on_tap_broken=lambda: None,
                decay_tail=0,
                min_segment_duration=0,
                export_manager=export_manager,
            )

            recorder.process_chunk(b"\xFF\x7F" * 10)
            raw_path = recorder._raw_path
            output_path = recorder._output_path

            recorder.finalize()

            export_manager.enqueue.assert_called_once_with(raw_path, output_path)
            self.assertEqual(recorder.state, "PASSIVE")

    def test_finalize_discards_short_segment_below_min_duration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            export_manager = MagicMock()
            recorder = PCMStreamRecorder(
                output_dir=tmpdir,
                on_tap_broken=lambda: None,
                decay_tail=0,
                min_segment_duration=1.0,
                export_manager=export_manager,
            )

            recorder.process_chunk(b"\xFF\x7F" * 10)
            raw_path = recorder._raw_path

            recorder.finalize()

            export_manager.enqueue.assert_not_called()
            self.assertFalse(os.path.exists(raw_path))

    def test_on_tap_broken_called_once_at_threshold(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_callback = MagicMock()
            recorder = PCMStreamRecorder(
                output_dir=tmpdir,
                on_tap_broken=mock_callback,
                sample_rate=48000,
                silence_threshold_db=-50,
                decay_tail=0,
            )

            # Transition to ACTIVE with one loud chunk
            loud_chunk = b"\xFF\x7F" * (recorder.chunk_size // 2)
            recorder.process_chunk(loud_chunk)
            self.assertEqual(recorder.state, "ACTIVE")

            # Feed 900 all-zero chunks — exactly at the threshold
            zero_chunk = b"\x00" * recorder.chunk_size
            for _ in range(900):
                recorder.process_chunk(zero_chunk)

            mock_callback.assert_called_once()

            # Feed another zero chunk — callback must NOT fire again (deduplication)
            recorder.process_chunk(zero_chunk)
            mock_callback.assert_called_once()

            recorder._raw_file.close() if recorder._raw_file else None

    def test_emits_segment_lifecycle_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            events = []
            export_manager = MagicMock()
            recorder = PCMStreamRecorder(
                output_dir=tmpdir,
                on_tap_broken=lambda: None,
                decay_tail=0,
                min_segment_duration=0,
                export_manager=export_manager,
                event_sink=events.append,
            )

            recorder.process_chunk(b"\xFF\x7F" * 10)
            recorder.finalize()

            self.assertIsInstance(events[0], SegmentOpened)
            self.assertIsInstance(events[1], SegmentClosed)
            self.assertFalse(events[1].discarded)

    def test_export_manager_drains_jobs_before_shutdown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_path = os.path.join(tmpdir, "segment.raw")
            output_path = os.path.join(tmpdir, "segment.wav")
            events = []

            with open(raw_path, "wb") as raw_file:
                raw_file.write(b"\x00\x00" * 200)

            manager = ExportManager(
                sample_rate=48000,
                channels=2,
                bytes_per_sample=2,
                export_format="wav",
                event_sink=events.append,
            )
            manager.enqueue(raw_path, output_path)
            manager.shutdown()

            self.assertTrue(os.path.exists(output_path))
            self.assertFalse(os.path.exists(raw_path))
            self.assertIsInstance(events[0], ExportStarted)
            self.assertIsInstance(events[1], ExportFinished)

    def test_last_active_at_updates_on_non_silent_chunk(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = PCMStreamRecorder(
                output_dir=tmpdir,
                on_tap_broken=lambda: None,
                sample_rate=48000,
                silence_threshold_db=-50,
                decay_tail=0,
            )

            with patch("src.recorder_core.time.time") as mock_time:
                # Transition PASSIVE -> ACTIVE
                mock_time.return_value = 100.0
                loud_chunk = b"\xFF\x7F" * 10
                recorder.process_chunk(loud_chunk)

                self.assertEqual(recorder.state, "ACTIVE")
                self.assertEqual(recorder.last_active_at, 100.0)

                # Process another loud chunk in ACTIVE state
                mock_time.return_value = 105.0
                recorder.process_chunk(loud_chunk)

                self.assertEqual(recorder.state, "ACTIVE")
                self.assertEqual(recorder.last_active_at, 105.0)

            if recorder._raw_file:
                recorder._raw_file.close()

if __name__ == "__main__":
    unittest.main()
