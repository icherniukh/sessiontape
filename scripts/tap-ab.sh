#!/usr/bin/env bash
# scripts/tap-ab.sh — A/B the CoreAudio tap configuration against Rekordbox overloads.
#
# Usage: scripts/tap-ab.sh <variant> [seconds]     (default 90 s; scrub the decks while it runs)
#
# Variants (env toggles read by mac-capture's CoreAudioTapCapture):
#   base      current default: unpinned tap, drift=0, clock=0, aggregate buffer=4096
#   v130      1.3.0 baseline: unpinned tap, drift=1, clock=0, HAL default buffer (512)
#   buf512    drift=0, aggregate buffer=512 (isolates drift=0 without buffer change)
#   drift1    drift=1, aggregate buffer=4096 (isolates 4096 buffer with drift on)
#   pin       pinned to Rekordbox's output device (with drift=0, buffer=4096)
#   clockdev  kAudioAggregateDeviceClockDeviceKey = output device (zero output streams)
#   subdev    output device as main sub-device (1.3.0 experiment B; adds output stream)
#   sck       ScreenCaptureKit backend (--sck)
#
# Counts Rekordbox's own "skipping cycle due to overload" log lines for exactly the run window
# and checks that real audio was flowing. Quit sessiontape first: two taps would confound it.
# TAP_AB_PID=<pid> taps another process instead (for testing the harness itself).
set -euo pipefail

variant=${1:?usage: tap-ab.sh <base|v130|buf512|drift1|pin|clockdev|subdev|sck> [seconds]}
secs=${2:-90}
root="$(cd "$(dirname "$0")/.." && pwd)"
bin="$root/mac-capture/.build/release/mac-capture"

extra_args=()
case "$variant" in
    base) envs=() ;;
    v130) envs=(SESSIONTAPE_TAP_DRIFT=1 SESSIONTAPE_TAP_BUFFER_FRAMES=0) ;;
    buf512) envs=(SESSIONTAPE_TAP_BUFFER_FRAMES=512) ;;
    drift1) envs=(SESSIONTAPE_TAP_DRIFT=1) ;;
    pin) envs=(SESSIONTAPE_TAP_PIN=1) ;;
    clockdev) envs=(SESSIONTAPE_TAP_CLOCK=clockdev) ;;
    subdev) envs=(SESSIONTAPE_TAP_CLOCK=subdev) ;;
    sck) envs=(); extra_args=(--sck) ;;
    *) echo "unknown variant: $variant" >&2; exit 2 ;;
esac

[[ -x "$bin" ]] || { echo "build first: (cd mac-capture && swift build -c release)" >&2; exit 1; }
pid=${TAP_AB_PID:-$(pgrep -xo rekordbox)} || { echo "Rekordbox is not running (launch '/Applications/rekordbox 7/rekordbox.app' and start playing a track first)" >&2; exit 1; }
if pgrep -f "mac-capture $pid " >/dev/null; then
    echo "another mac-capture is already tapping Rekordbox (quit sessiontape first):" >&2
    pgrep -fl "mac-capture $pid " >&2
    exit 1
fi

out="${TMPDIR:-/tmp}/tap-ab/$(date +%Y%m%d-%H%M%S)-$variant"
mkdir -p "$out"
mkfifo "$out/pcm"

# Per-second RMS of the s16le stereo stream, so a silent tap can't pass as a clean run.
python3 -I -c '
import array, math, sys
f = sys.stdin.buffer
while True:
    b = f.read(48000 * 4)
    if not b:
        break
    a = array.array("h"); a.frombytes(b[: len(b) // 2 * 2])
    s = a[::16]
    ms = sum(x * x for x in s) / max(1, len(s))
    print(f"{10 * math.log10(ms / 32768 ** 2):.1f}" if ms > 0 else "-inf", flush=True)
' <"$out/pcm" >"$out/rms.log" &
rms_pid=$!

start=$(date '+%Y-%m-%d %H:%M:%S')
env ${envs[@]+"${envs[@]}"} "$bin" "$pid" 48000 ${extra_args[@]+"${extra_args[@]}"} --log "$out/mc.log" >"$out/pcm" 2>/dev/null &
mc_pid=$!
trap 'kill -TERM $mc_pid 2>/dev/null || true' INT TERM

echo "variant $variant: tapping Rekordbox PID $pid for ${secs}s — scrub now. Logs: $out"
sleep "$secs" || true
kill -TERM "$mc_pid" 2>/dev/null || true
wait "$mc_pid" 2>/dev/null || true
wait "$rms_pid" 2>/dev/null || true
end=$(date '+%Y-%m-%d %H:%M:%S')

overloads=$(/usr/bin/log show --start "$start" --end "$end" --style compact \
    --predicate 'process == "rekordbox" AND eventMessage CONTAINS "skipping cycle due to overload"' \
    2>/dev/null | tail -n +2 | grep -c 'skipping cycle' || true)
audio_s=$(awk '$1 != "-inf" && $1 > -60' "$out/rms.log" | wc -l | tr -d ' ')
total_s=$(wc -l <"$out/rms.log" | tr -d ' ')
cb_max=$(grep -o 'cb_max=[0-9.]*' "$out/mc.log" | cut -d= -f2 | sort -n | tail -1)
drops=$(grep -o 'drops=[0-9]*' "$out/mc.log" | cut -d= -f2 | awk '{s += $1} END {print s + 0}')
power=$(pmset -g batt | head -1 | sed "s/.*'\(.*\)'.*/\1/")

echo
grep -E 'Tap config:|IO buffer frame sizes|Aggregate clock|pinned to device|ScreenCaptureKit audio stream started|ERROR' "$out/mc.log" | sed 's/^/  /' || true
printf '  window      %s → %s (%ss)\n' "$start" "$end" "$secs"
printf '  overloads   %s  (%.2f/min)\n' "$overloads" "$(echo "$overloads * 60 / $secs" | bc -l)"
printf '  audio       %s of %s s above -60 dBFS\n' "$audio_s" "$total_s"
printf '  callback    cb_max=%s ms, drops=%s\n' "${cb_max:-?}" "$drops"
printf '  power       %s\n' "$power"
printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$start" "$variant" "$secs" "$overloads" "$audio_s/$total_s" "$power" \
    >>"${TMPDIR:-/tmp}/tap-ab/results.tsv"
