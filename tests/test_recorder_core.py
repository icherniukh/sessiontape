import os
import tempfile
import unittest
from unittest.mock import MagicMock

from src.events import ExportFinished, ExportStarted, SegmentClosed, SegmentOpened
from src.recorder_core import ExportManager, PCMStreamRecorder


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


if __name__ == "__main__":
    unittest.main()
