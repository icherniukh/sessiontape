#!/usr/bin/env python3
"""
Benchmark the recording pipeline's CPU and memory overhead.

Run this while Rekordbox (or any target app) is open to get a baseline
reading of that app's resource usage, then compare. The script measures
the recorder's own overhead so you can report it as:

    "auto-rb-recorder adds +X% CPU and +Y MB RAM on top of Rekordbox"

Usage:
    # Measure recorder overhead in isolation:
    python scripts/bench_recording.py

    # Measure with a specific target PID for baseline comparison:
    python scripts/bench_recording.py --compare-pid <rekordbox_pid>

    # Longer run for steadier averages:
    python scripts/bench_recording.py --duration 60
"""

import argparse
import math
import os
import statistics
import struct
import sys
import tempfile
import time
import tracemalloc
from array import array
from dataclasses import dataclass

import psutil

# Make src importable when run from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.recorder_core import Exporter, PCMStreamRecorder


# ── Audio constants (match recorder defaults) ──────────────────────────────────

SAMPLE_RATE = 48_000
CHANNELS = 2
BYTES_PER_SAMPLE = 2          # s16le
CHUNK_DURATION_S = 0.1        # 100 ms
CHUNK_BYTES = int(SAMPLE_RATE * CHUNK_DURATION_S * CHANNELS * BYTES_PER_SAMPLE)
CHUNKS_PER_SECOND = int(1 / CHUNK_DURATION_S)
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE  # 192 000 B/s


# ── Synthetic audio generators ────────────────────────────────────────────────

def _sine_chunk(frequency: float = 440.0, amplitude: int = 20_000) -> bytes:
    """One 100 ms chunk of a sine wave at the given frequency."""
    n = int(SAMPLE_RATE * CHUNK_DURATION_S)
    samples = array("h")
    for i in range(n):
        v = int(amplitude * math.sin(2 * math.pi * frequency * i / SAMPLE_RATE))
        samples.append(v)   # L
        samples.append(v)   # R
    return samples.tobytes()


def _silence_chunk() -> bytes:
    return b"\x00" * CHUNK_BYTES


# ── Result containers ─────────────────────────────────────────────────────────

@dataclass
class ChunkBenchResult:
    n_chunks: int
    total_s: float
    per_chunk_us: float      # microseconds per chunk
    budget_us: float         # available budget per chunk (100 000 µs)
    headroom_x: float        # how many times faster than real-time
    peak_rss_mb: float
    tracemalloc_peak_mb: float


@dataclass
class ExportBenchResult:
    format: str
    file_size_mb: float
    duration_s: float        # duration of audio content
    wall_time_s: float       # actual conversion time
    throughput_x: float      # multiples of real-time speed


@dataclass
class CpuBenchResult:
    duration_s: float
    cpu_samples: list
    avg_cpu_pct: float
    max_cpu_pct: float
    rss_mb_start: float
    rss_mb_peak: float
    rss_overhead_mb: float


# ── Benchmark functions ───────────────────────────────────────────────────────

def bench_chunk_processing(n_chunks: int = 1000) -> ChunkBenchResult:
    """Measure process_chunk() latency over n_chunks of mixed audio."""
    audio_chunk = _sine_chunk()
    silent_chunk = _silence_chunk()

    with tempfile.TemporaryDirectory() as tmpdir:
        recorder = PCMStreamRecorder(
            output_dir=tmpdir,
            sample_rate=SAMPLE_RATE,
            silence_threshold_db=-50,
            min_silence_duration=2.0,
            decay_tail=0.5,
            export_format="wav",
        )

        tracemalloc.start()
        proc = psutil.Process()
        rss_before = proc.memory_info().rss

        t0 = time.perf_counter()
        for i in range(n_chunks):
            # Simulate a realistic mix: 80% audio, 20% silence
            chunk = audio_chunk if (i % 5 != 0) else silent_chunk
            recorder.process_chunk(chunk)
        elapsed = time.perf_counter() - t0

        _, peak_traced = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        rss_after = proc.memory_info().rss
        peak_rss_mb = (rss_after - rss_before) / 1024 / 1024

    per_chunk_us = (elapsed / n_chunks) * 1_000_000
    budget_us = CHUNK_DURATION_S * 1_000_000
    headroom_x = budget_us / per_chunk_us

    return ChunkBenchResult(
        n_chunks=n_chunks,
        total_s=elapsed,
        per_chunk_us=per_chunk_us,
        budget_us=budget_us,
        headroom_x=headroom_x,
        peak_rss_mb=max(0.0, peak_rss_mb),
        tracemalloc_peak_mb=peak_traced / 1024 / 1024,
    )


def bench_export(duration_s: float = 30.0) -> list[ExportBenchResult]:
    """Measure WAV (and optionally MP3) export throughput."""
    import shutil

    n_samples = int(SAMPLE_RATE * duration_s)
    raw_bytes = struct.pack(f"<{n_samples * CHANNELS}h", *([16000] * n_samples * CHANNELS))
    results = []

    for fmt in ("wav", "mp3"):
        if fmt == "mp3":
            if not (shutil.which("ffmpeg") or
                    any(os.path.exists(p) for p in
                        ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"))):
                print(f"  [skip mp3] ffmpeg not found in PATH")
                continue

        with tempfile.TemporaryDirectory() as tmpdir:
            raw_path = os.path.join(tmpdir, "test.raw")
            out_path = os.path.join(tmpdir, f"test.{fmt}")

            with open(raw_path, "wb") as f:
                f.write(raw_bytes)

            exporter = Exporter(
                sample_rate=SAMPLE_RATE,
                channels=CHANNELS,
                bytes_per_sample=BYTES_PER_SAMPLE,
                export_format=fmt,
            )

            t0 = time.perf_counter()
            # Run synchronously for measurement
            if fmt == "wav":
                exporter._convert_wav(raw_path, out_path)
            else:
                exporter._convert_mp3(raw_path, out_path)
            wall = time.perf_counter() - t0

            out_size_mb = os.path.getsize(out_path) / 1024 / 1024

        results.append(ExportBenchResult(
            format=fmt,
            file_size_mb=out_size_mb,
            duration_s=duration_s,
            wall_time_s=wall,
            throughput_x=duration_s / wall,
        ))

    return results


def bench_cpu_overhead(duration_s: float = 20.0, sample_interval: float = 0.5) -> CpuBenchResult:
    """
    Simulate a continuous recording session and sample CPU/memory via psutil.

    The result represents the recorder process's overhead — subtract your
    baseline (idle Python process) to get the net overhead attributable to
    the recording pipeline.
    """
    audio_chunk = _sine_chunk()
    silent_chunk = _silence_chunk()

    proc = psutil.Process()
    proc.cpu_percent()  # prime (first call always returns 0.0)
    time.sleep(0.1)

    rss_start = proc.memory_info().rss / 1024 / 1024

    with tempfile.TemporaryDirectory() as tmpdir:
        recorder = PCMStreamRecorder(
            output_dir=tmpdir,
            sample_rate=SAMPLE_RATE,
            silence_threshold_db=-50,
            min_silence_duration=2.0,
            decay_tail=0.5,
        )

        cpu_samples = []
        rss_peak = rss_start
        deadline = time.perf_counter() + duration_s
        chunk_idx = 0
        next_sample = time.perf_counter() + sample_interval

        while time.perf_counter() < deadline:
            # Process at real-time rate: sleep to match the 100ms chunk cadence
            t_chunk_start = time.perf_counter()
            chunk = audio_chunk if (chunk_idx % 5 != 0) else silent_chunk
            recorder.process_chunk(chunk)
            chunk_idx += 1

            # Sleep to simulate real-time pacing
            elapsed_chunk = time.perf_counter() - t_chunk_start
            sleep_for = CHUNK_DURATION_S - elapsed_chunk
            if sleep_for > 0:
                time.sleep(sleep_for)

            now = time.perf_counter()
            if now >= next_sample:
                cpu_samples.append(proc.cpu_percent())
                rss_now = proc.memory_info().rss / 1024 / 1024
                rss_peak = max(rss_peak, rss_now)
                next_sample = now + sample_interval

        recorder.finalize()
        time.sleep(2.0)  # let non-daemon conversion threads finish before tmpdir is removed

    return CpuBenchResult(
        duration_s=duration_s,
        cpu_samples=cpu_samples,
        avg_cpu_pct=statistics.mean(cpu_samples) if cpu_samples else 0.0,
        max_cpu_pct=max(cpu_samples) if cpu_samples else 0.0,
        rss_mb_start=rss_start,
        rss_mb_peak=rss_peak,
        rss_overhead_mb=rss_peak - rss_start,
    )


def measure_pid_resources(pid: int, duration_s: float = 5.0, sample_interval: float = 0.5) -> dict:
    """Sample CPU% and RSS for an external process (e.g. Rekordbox)."""
    try:
        target = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return {}

    target.cpu_percent()
    time.sleep(0.1)

    cpu_samples = []
    rss_samples = []
    deadline = time.perf_counter() + duration_s

    while time.perf_counter() < deadline:
        try:
            cpu_samples.append(target.cpu_percent(interval=sample_interval))
            rss_samples.append(target.memory_info().rss / 1024 / 1024)
        except psutil.NoSuchProcess:
            break

    return {
        "pid": pid,
        "name": target.name() if target.is_running() else "?",
        "avg_cpu_pct": statistics.mean(cpu_samples) if cpu_samples else 0.0,
        "avg_rss_mb": statistics.mean(rss_samples) if rss_samples else 0.0,
    }


# ── Report printer ────────────────────────────────────────────────────────────

def print_report(
    chunk: ChunkBenchResult,
    exports: list[ExportBenchResult],
    cpu: CpuBenchResult,
    baseline_pid: dict | None,
) -> None:
    sep = "─" * 60

    print(f"\n{'auto-rb-recorder  —  Recording Pipeline Benchmark':^60}")
    print(sep)

    print("\n[1] process_chunk() latency")
    print(f"    chunks processed   : {chunk.n_chunks:,}")
    print(f"    time per chunk     : {chunk.per_chunk_us:.1f} µs")
    print(f"    real-time budget   : {chunk.budget_us:,.0f} µs  (100 ms chunks)")
    print(f"    headroom           : {chunk.headroom_x:.0f}× faster than real-time")
    print(f"    peak RSS delta     : {chunk.peak_rss_mb:.1f} MB")
    print(f"    tracemalloc peak   : {chunk.tracemalloc_peak_mb:.2f} MB")

    print(f"\n[2] Export throughput")
    if exports:
        for e in exports:
            print(f"    {e.format.upper():<4}  {e.file_size_mb:.1f} MB  "
                  f"({e.duration_s:.0f}s audio)  →  {e.wall_time_s:.2f}s wall  "
                  f"({e.throughput_x:.0f}× real-time)")
    else:
        print("    (no export results)")

    print(f"\n[3] CPU & memory during {cpu.duration_s:.0f}s real-time simulation")
    print(f"    avg CPU            : {cpu.avg_cpu_pct:.1f}%")
    print(f"    max CPU spike      : {cpu.max_cpu_pct:.1f}%")
    print(f"    RSS at start       : {cpu.rss_mb_start:.1f} MB")
    print(f"    RSS peak           : {cpu.rss_mb_peak:.1f} MB")
    print(f"    RSS growth         : +{cpu.rss_overhead_mb:.1f} MB")

    if baseline_pid:
        print(f"\n[4] Comparison vs {baseline_pid['name']} (PID {baseline_pid['pid']})")
        print(f"    {baseline_pid['name']:<20} avg CPU  : {baseline_pid['avg_cpu_pct']:.1f}%")
        print(f"    recorder overhead              : {cpu.avg_cpu_pct:.1f}%")
        delta_cpu = cpu.avg_cpu_pct
        relative_pct = (delta_cpu / baseline_pid['avg_cpu_pct'] * 100) if baseline_pid['avg_cpu_pct'] > 0 else float("inf")
        print(f"    relative overhead              : +{relative_pct:.0f}% of {baseline_pid['name']}'s CPU")
        print(f"    {baseline_pid['name']:<20} avg RSS  : {baseline_pid['avg_rss_mb']:.0f} MB")
        print(f"    recorder RSS                   : {cpu.rss_mb_peak:.0f} MB")

    print(f"\n{sep}")
    print("System:")
    print(f"    CPU cores          : {psutil.cpu_count(logical=True)}")
    print(f"    Python             : {sys.version.split()[0]}")
    print(f"    Platform           : {sys.platform}")
    print(sep)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chunks", type=int, default=1000,
                        help="Number of chunks for latency benchmark (default: 1000)")
    parser.add_argument("--export-duration", type=float, default=30.0,
                        help="Seconds of audio to export in throughput test (default: 30)")
    parser.add_argument("--cpu-duration", type=float, default=20.0,
                        help="Seconds for real-time CPU simulation (default: 20)")
    parser.add_argument("--compare-pid", type=int, default=None,
                        help="PID of the target app (e.g. Rekordbox) to compare against")
    args = parser.parse_args()

    print("Running benchmarks — this will take ~30s...")

    print("  [1/3] chunk latency...")
    chunk_result = bench_chunk_processing(n_chunks=args.chunks)

    print("  [2/3] export throughput...")
    export_results = bench_export(duration_s=args.export_duration)

    print(f"  [3/3] real-time CPU simulation ({args.cpu_duration:.0f}s)...")
    cpu_result = bench_cpu_overhead(duration_s=args.cpu_duration)

    baseline = None
    if args.compare_pid:
        print(f"  [4/4] sampling PID {args.compare_pid}...")
        baseline = measure_pid_resources(args.compare_pid)

    print_report(chunk_result, export_results, cpu_result, baseline)


if __name__ == "__main__":
    main()
