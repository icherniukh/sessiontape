"""Performance metrics helpers for the capture pipeline."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("sessiontape.perf")

CAPTURE_PREFIX = "CAPTURE "
WINDOW_S = 1.0


def parse_capture_line(line: str) -> Optional[dict[str, int | float]]:
    """Parse a compact CAPTURE metric line."""
    payload = line
    if payload.startswith("INFO: "):
        payload = payload[6:]
    if not payload.startswith(CAPTURE_PREFIX):
        return None

    parsed: dict[str, int | float] = {}
    for token in payload[len(CAPTURE_PREFIX) :].split():
        if "=" not in token:
            continue
        key, _, raw_value = token.partition("=")
        parsed[key] = _coerce_metric_value(raw_value)
    return parsed or None


def _coerce_metric_value(raw_value: str) -> int | float:
    if "." in raw_value:
        return float(raw_value)
    return int(raw_value)


def format_capture_line(**fields: int | float) -> str:
    """Render the canonical focused CAPTURE metric line."""
    order = (
        "drops",
        "cb_max",
        "cb_p99",
        "slow",
        "fb",
        "cb",
        "gap_max",
        "proc_max",
        "bytes",
    )
    parts = [CAPTURE_PREFIX.strip()]
    for key in order:
        if key not in fields:
            continue
        value = fields[key]
        if isinstance(value, float):
            parts.append(f"{key}={value:.2f}")
        else:
            parts.append(f"{key}={value}")
    return " ".join(parts)


@dataclass
class NativeCaptureWindow:
    drops: int = 0
    cb_max: float = 0.0
    cb_p99: float = 0.0
    slow: int = 0
    fb: int = 0
    cb: int = 0
    bytes: int = 0


class PipelineCaptureMetrics:
    """Rolling 1-second pipeline metric for drain/SCK pressure diagnosis."""

    def __init__(self, window_s: float = WINDOW_S) -> None:
        self.window_s = window_s
        self._window_start = 0.0
        self._gap_max = 0.0
        self._proc_max = 0.0
        self._native = NativeCaptureWindow()

    def reset(self) -> None:
        now = time.monotonic()
        self._window_start = now
        self._gap_max = 0.0
        self._proc_max = 0.0
        self._native = NativeCaptureWindow()

    def ingest_native(self, parsed: dict[str, int | float]) -> None:
        self._native = NativeCaptureWindow(
            drops=int(parsed.get("drops", 0)),
            cb_max=float(parsed.get("cb_max", 0.0)),
            cb_p99=float(parsed.get("cb_p99", 0.0)),
            slow=int(parsed.get("slow", 0)),
            fb=int(parsed.get("fb", 0)),
            cb=int(parsed.get("cb", 0)),
            bytes=int(parsed.get("bytes", 0)),
        )

    def record_chunk(self, *, gap_ms: float, process_ms: float) -> None:
        self._gap_max = max(self._gap_max, gap_ms)
        self._proc_max = max(self._proc_max, process_ms)

    def maybe_emit(self, *, force: bool = False) -> Optional[str]:
        now = time.monotonic()
        if not force and now - self._window_start < self.window_s:
            return None

        line = format_capture_line(
            drops=self._native.drops,
            cb_max=self._native.cb_max,
            cb_p99=self._native.cb_p99,
            slow=self._native.slow,
            fb=self._native.fb,
            cb=self._native.cb,
            gap_max=self._gap_max,
            proc_max=self._proc_max,
            bytes=self._native.bytes,
        )

        self._window_start = now
        self._gap_max = 0.0
        self._proc_max = 0.0
        self._native = NativeCaptureWindow()
        return line



def benchmark_recorder_core(
    *,
    chunk_size: int,
    iterations: int,
    sample_rate: int = 48_000,
) -> dict[str, Any]:
    """Micro-benchmark PCMStreamRecorder.process_chunk throughput."""
    import tempfile

    from src.recorder_core import PCMStreamRecorder

    amplitude = 10_000
    frame_count = chunk_size // 4
    chunk = bytearray(chunk_size)
    for frame in range(frame_count):
        sample = int(amplitude * math.sin(2 * math.pi * 440 * frame / sample_rate))
        chunk[frame * 4] = sample & 0xFF
        chunk[frame * 4 + 1] = (sample >> 8) & 0xFF
        chunk[frame * 4 + 2] = sample & 0xFF
        chunk[frame * 4 + 3] = (sample >> 8) & 0xFF
    chunk_bytes = bytes(chunk)

    samples: list[float] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        recorder = PCMStreamRecorder(
            output_dir=tmpdir,
            on_tap_broken=lambda: None,
            sample_rate=sample_rate,
            silence_threshold_db=-50.0,
            decay_tail=0.0,
            min_segment_duration=1_000_000.0,
        )
        recorder.process_chunk(chunk_bytes)

        start = time.perf_counter()
        for _ in range(iterations):
            t0 = time.perf_counter()
            recorder.process_chunk(chunk_bytes)
            samples.append((time.perf_counter() - t0) * 1000.0)
        elapsed_s = time.perf_counter() - start

        if recorder._raw_file:
            recorder._raw_file.close()

    ordered = sorted(samples)
    return {
        "component": "recorder_core.process_chunk",
        "chunk_size_bytes": chunk_size,
        "iterations": iterations,
        "elapsed_s": round(elapsed_s, 6),
        "chunks_per_s": round(iterations / elapsed_s, 2),
        "proc_max_ms": ordered[-1] if ordered else 0.0,
        "proc_p99_ms": _percentile(ordered, 99) if ordered else 0.0,
    }


def _percentile(ordered: list[float], percentile: float) -> float:
    if not ordered:
        return 0.0
    index = int((percentile / 100.0) * (len(ordered) - 1))
    return ordered[index]
