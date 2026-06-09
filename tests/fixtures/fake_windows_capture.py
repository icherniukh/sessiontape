#!/usr/bin/env python3
"""Fake sessiontape-capture-win.exe for integration tests on Windows."""
import argparse
import os
import sys
import threading
import time
from array import array


CHUNK_DURATION = 0.1
CHANNELS = 2
DEFAULT_SCENARIO = "sound:3,silence:3,sound:2"


def build_chunk(sample_rate: int, kind: str) -> bytes:
    samples_per_chunk = int(sample_rate * CHUNK_DURATION * CHANNELS)
    amplitude = 16000 if kind == "sound" else 0
    return array("h", [amplitude] * samples_per_chunk).tobytes()


def iter_scenario(sample_rate: int, scenario: str):
    for part in scenario.split(","):
        kind, count = part.split(":", 1)
        chunk = build_chunk(sample_rate, kind)
        for _ in range(int(count)):
            yield chunk


def _watch_stdin():
    while True:
        data = sys.stdin.buffer.read(1)
        if data == b'':
            os._exit(0)


def continuous_mode(sample_rate: int) -> None:
    t = threading.Thread(target=_watch_stdin, daemon=True)
    t.start()
    chunk = build_chunk(sample_rate, "sound")
    while True:
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        time.sleep(0.05)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int)
    parser.add_argument("--sample-rate", type=int, default=48000)
    parser.add_argument("--continuous", action="store_true",
                        help="Stream audio indefinitely; exit when stdin closes")
    args = parser.parse_args()

    sample_rate = int(os.environ.get("RB_TEST_AUDIO_SAMPLE_RATE", str(args.sample_rate)))

    if args.continuous:
        continuous_mode(sample_rate)
        return 0  # unreachable; continuous_mode loops forever

    scenario = os.environ.get("RB_TEST_AUDIO_SCENARIO", DEFAULT_SCENARIO)
    for chunk in iter_scenario(sample_rate, scenario):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
