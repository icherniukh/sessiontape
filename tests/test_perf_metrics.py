import unittest

from src.perf_metrics import (
    PipelineCaptureMetrics,
    benchmark_recorder_core,
    format_capture_line,
    parse_capture_line,
)


class TestPerfMetrics(unittest.TestCase):
    def test_parse_capture_line(self):
        line = "CAPTURE drops=2 cb_max=12.40 cb_p99=8.10 slow=1 fb=0 cb=94 bytes=180480"
        parsed = parse_capture_line(line)
        self.assertEqual(parsed["drops"], 2)
        self.assertEqual(parsed["cb_max"], 12.40)
        self.assertEqual(parsed["cb_p99"], 8.10)
        self.assertEqual(parsed["slow"], 1)
        self.assertEqual(parsed["bytes"], 180480)

    def test_parse_capture_line_with_info_prefix(self):
        line = "INFO: CAPTURE drops=0 cb_max=1.20 slow=0"
        parsed = parse_capture_line(line)
        self.assertEqual(parsed["drops"], 0)
        self.assertEqual(parsed["cb_max"], 1.20)

    def test_format_capture_line_order(self):
        line = format_capture_line(
            drops=1,
            cb_max=9.5,
            cb_p99=4.2,
            slow=2,
            gap_max=120.0,
            proc_max=0.4,
        )
        self.assertTrue(line.startswith("CAPTURE "))
        self.assertIn("drops=1", line)
        self.assertLess(line.index("drops=1"), line.index("gap_max=120.00"))

    def test_pipeline_metrics_emits_every_second(self):
        metrics = PipelineCaptureMetrics(window_s=0.05)
        metrics.reset()
        metrics.ingest_native(
            {
                "drops": 3,
                "cb_max": 11.2,
                "cb_p99": 7.1,
                "slow": 1,
                "fb": 0,
                "cb": 90,
                "bytes": 172800,
            }
        )
        metrics.record_chunk(gap_ms=150.0, process_ms=0.6)

        import time

        time.sleep(0.06)
        line = metrics.maybe_emit()
        self.assertIsNotNone(line)
        assert line is not None
        self.assertIn("drops=3", line)
        self.assertIn("cb_max=11.20", line)
        self.assertIn("gap_max=150.00", line)
        self.assertIn("proc_max=0.60", line)

    def test_pipeline_metrics_preserves_and_accumulates_tap_counters(self):
        metrics = PipelineCaptureMetrics(window_s=0.01)
        metrics.reset()
        metrics.ingest_native(
            {
                "drops": 1,
                "cb_max": 2.0,
                "cb_p99": 1.0,
                "slow": 0,
                "fb": 0,
                "cb": 40,
                "bytes": 1000,
                "hal_overload": 2,
                "io_stopped": 0,
                "trunc": 1,
            }
        )
        metrics.ingest_native(
            {
                "drops": 0,
                "cb_max": 3.5,
                "cb_p99": 2.0,
                "slow": 1,
                "fb": 0,
                "cb": 50,
                "bytes": 2000,
                "hal_overload": 1,
                "io_stopped": 1,
                "trunc": 2,
            }
        )
        line = metrics.maybe_emit(force=True)
        self.assertIsNotNone(line)
        assert line is not None
        self.assertIn("drops=1", line)
        self.assertIn("cb_max=3.50", line)
        self.assertIn("cb=90", line)
        self.assertIn("bytes=2000", line)
        self.assertIn("hal_overload=3", line)
        self.assertIn("io_stopped=1", line)
        self.assertIn("trunc=3", line)
        self.assertIn("cb_p99=2.00", line)

    def test_dedup_filter_exempts_periodic_capture_metrics(self):
        import logging
        from src.__main__ import _DedupFilter

        flt = _DedupFilter(window=60.0)
        cap_rec = logging.LogRecord(
            name="sessiontape.capture",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="CAPTURE drops=0 cb_max=0.02",
            args=(),
            exc_info=None,
        )
        plain_rec = logging.LogRecord(
            name="sessiontape",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="repeated status line",
            args=(),
            exc_info=None,
        )
        self.assertTrue(flt.filter(cap_rec))
        self.assertTrue(flt.filter(cap_rec))
        self.assertTrue(flt.filter(plain_rec))
        self.assertFalse(flt.filter(plain_rec))

    def test_benchmark_recorder_core_smoke(self):
        report = benchmark_recorder_core(chunk_size=1920, iterations=200)
        self.assertEqual(report["component"], "recorder_core.process_chunk")
        self.assertEqual(report["iterations"], 200)
        self.assertGreater(report["chunks_per_s"], 0)
        self.assertGreater(report["proc_max_ms"], 0)


if __name__ == "__main__":
    unittest.main()
