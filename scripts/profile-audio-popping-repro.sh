#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CAPTURE_SCRIPT="$ROOT_DIR/scripts/capture-audio-popping-logs.sh"
RUN_LOG="$(mktemp /tmp/profile-audio-popping-run.XXXXXX.log)"

if [[ ! -x "$CAPTURE_SCRIPT" ]]; then
    echo "missing capture script: $CAPTURE_SCRIPT" >&2
    exit 1
fi

cleanup() {
    set +e
    if [[ -n "${CAPTURE_SESSION_PID:-}" ]]; then
        kill -TERM "$CAPTURE_SESSION_PID" 2>/dev/null || true
        pkill -TERM -P "$CAPTURE_SESSION_PID" 2>/dev/null || true
        for _ in $(seq 1 20); do
            if ! kill -0 "$CAPTURE_SESSION_PID" 2>/dev/null; then
                break
            fi
            sleep 0.1
        done
        kill -KILL "$CAPTURE_SESSION_PID" 2>/dev/null || true
        pkill -KILL -P "$CAPTURE_SESSION_PID" 2>/dev/null || true
        wait "$CAPTURE_SESSION_PID" 2>/dev/null || true
    fi
    rm -f "$RUN_LOG"
}

trap cleanup EXIT INT TERM

"$CAPTURE_SCRIPT" >"$RUN_LOG" 2>&1 &
CAPTURE_SESSION_PID=$!

OUT_DIR=""
for _ in $(seq 1 50); do
    if [[ -f "$RUN_LOG" ]]; then
        OUT_DIR="$(sed -n 's/^output dir: //p' "$RUN_LOG" | head -n1)"
    fi
    if [[ -n "$OUT_DIR" ]]; then
        break
    fi
    sleep 0.2
done

if [[ -z "$OUT_DIR" ]]; then
    echo "failed to determine capture output dir" >&2
    cat "$RUN_LOG" >&2 || true
    exit 1
fi

echo "capture dir: $OUT_DIR"
echo "When the audio popping starts, press Enter once."
read -r _

MAC_CAPTURE_PID="$(pgrep -x mac-capture | head -n1 || true)"
REPLAYD_PID="$(pgrep -x replayd | head -n1 || true)"
REKORDBOX_PID="$(pgrep -x rekordbox | head -n1 || true)"

printf '%s\n' "mac-capture pid: ${MAC_CAPTURE_PID:-missing}" >>"$OUT_DIR/profile-meta.log"
printf '%s\n' "replayd pid: ${REPLAYD_PID:-missing}" >>"$OUT_DIR/profile-meta.log"
printf '%s\n' "rekordbox pid: ${REKORDBOX_PID:-missing}" >>"$OUT_DIR/profile-meta.log"

if [[ -n "$MAC_CAPTURE_PID" ]]; then
    sample "$MAC_CAPTURE_PID" 5 -file "$OUT_DIR/mac-capture.sample.txt" || true
fi

if [[ -n "$REPLAYD_PID" ]]; then
    sudo sample "$REPLAYD_PID" 5 -file "$OUT_DIR/replayd.sample.txt" || true
fi

if [[ -n "$REKORDBOX_PID" ]]; then
    sudo sample "$REKORDBOX_PID" 5 -file "$OUT_DIR/rekordbox.sample.txt" || true
fi

/usr/bin/log show --last 2m --style compact \
    --predicate 'process == "coreaudiod" AND eventMessage CONTAINS[c] "HALS_OverloadMessage"' \
    >"$OUT_DIR/coreaudiod-overload.log" 2>&1 || true

/usr/bin/log show --last 2m --style compact \
    --predicate 'process == "rekordbox" AND eventMessage CONTAINS[c] "skipping cycle due to overload"' \
    >"$OUT_DIR/rekordbox-overload.log" 2>&1 || true

/usr/bin/log show --last 2m --style compact \
    --predicate 'process == "replayd" AND (eventMessage CONTAINS[c] "AudioTap" OR eventMessage CONTAINS[c] "aggregate" OR eventMessage CONTAINS[c] "SLContentStream" OR eventMessage CONTAINS[c] "SCStream")' \
    >"$OUT_DIR/replayd-overload.log" 2>&1 || true

printf '%s\n' "profiling complete: $OUT_DIR"
