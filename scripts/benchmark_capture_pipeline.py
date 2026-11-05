#!/usr/bin/env python3
"""Offline benchmarks for the capture pipeline hot path."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.perf_metrics import benchmark_recorder_core


def _find_mac_capture_benchmark() -> Path | None:
    candidates = [
        ROOT_DIR / "mac-capture" / ".build" / "release" / "mac-capture-benchmark",
        ROOT_DIR / "mac-capture" / ".build" / "debug" / "mac-capture-benchmark",
    ]
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate
    return None


def run_mac_capture_benchmark() -> dict | None:
    binary = _find_mac_capture_benchmark()
    if binary is None:
        return None

    output = subprocess.check_output([str(binary)], text=True).strip()
    return json.loads(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--iterations",
        type=int,
        default=50_000,
        help="Iterations for the Python recorder_core benchmark",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=19_200,
        help="PCM chunk size in bytes (default matches 100ms @ 48kHz stereo s16le)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path to write the JSON report",
    )
    args = parser.parse_args()

    report = {
        "benchmarks": [
            benchmark_recorder_core(
                chunk_size=args.chunk_size,
                iterations=args.iterations,
            )
        ]
    }

    native = run_mac_capture_benchmark()
    if native is not None:
        report["benchmarks"].append(native)
    else:
        report["notes"] = [
            "mac-capture-benchmark not found; build with "
            "'(cd mac-capture && swift build -c release --product mac-capture-benchmark)'"
        ]

    encoded = json.dumps(report, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
