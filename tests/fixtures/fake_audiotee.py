#!/usr/bin/env python3
import argparse
import os
import sys
from array import array


CHUNK_DURATION = 0.1
CHANNELS = 2
BYTES_PER_SAMPLE = 2
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-processes", nargs="*")
    parser.add_argument("--sample-rate", type=int, default=48000)
    parser.add_argument("--stereo", action="store_true")
    parser.add_argument("--flush", action="store_true")
    args = parser.parse_args()

    scenario = os.environ.get("RB_TEST_AUDIO_SCENARIO", DEFAULT_SCENARIO)
    sample_rate = int(os.environ.get("RB_TEST_AUDIO_SAMPLE_RATE", str(args.sample_rate)))

    for chunk in iter_scenario(sample_rate, scenario):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
