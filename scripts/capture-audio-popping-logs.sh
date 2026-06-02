#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_ROOT="${RB_AUDIO_POPPING_LOG_DIR:-/tmp/rb-audio-popping-captures}"
TS="$(date '+%Y%m%d-%H%M%S')"
OUT_DIR="$OUT_ROOT/$TS"
HELPER="$ROOT_DIR/mac-capture/.build/debug/mac-capture"

mkdir -p "$OUT_DIR"

if [[ ! -x "$HELPER" ]]; then
    echo "missing helper: $HELPER" >&2
    echo "build it first with: (cd $ROOT_DIR/mac-capture && swift build -c debug)" >&2
    exit 1
fi

RB_PID="${1:-}"
if [[ -z "$RB_PID" ]]; then
    RB_PID="$(pgrep -x rekordbox | head -n1 || true)"
fi

if [[ -z "$RB_PID" ]]; then
    echo "rekordbox PID not found" >&2
    exit 1
fi

echo "output dir: $OUT_DIR"
echo "rekordbox pid: $RB_PID"

note() {
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >>"$OUT_DIR/capture-meta.log"
}

cleanup() {
    set +e
    [[ -n "${HELPER_PID:-}" ]] && kill "$HELPER_PID" 2>/dev/null
    [[ -n "${COREAUDIO_PID:-}" ]] && kill "$COREAUDIO_PID" 2>/dev/null
    [[ -n "${REPLAYD_PID:-}" ]] && kill "$REPLAYD_PID" 2>/dev/null
    [[ -n "${REKORDBOX_PID:-}" ]] && kill "$REKORDBOX_PID" 2>/dev/null
    [[ -n "${STATUS_PID:-}" ]] && kill "$STATUS_PID" 2>/dev/null
    [[ -n "${HELPER_WATCH_PID:-}" ]] && kill "$HELPER_WATCH_PID" 2>/dev/null
    wait "${HELPER_PID:-}" "${COREAUDIO_PID:-}" "${REPLAYD_PID:-}" "${REKORDBOX_PID:-}" "${STATUS_PID:-}" "${HELPER_WATCH_PID:-}" 2>/dev/null
}

trap cleanup EXIT INT TERM

RB_MAC_CAPTURE_DIAG=1 \
RB_MAC_CAPTURE_LOG="$OUT_DIR/mac-capture.diag.log" \
RB_MAC_CAPTURE_FILTER_MODE="${RB_MAC_CAPTURE_FILTER_MODE:-display}" \
"$HELPER" "$RB_PID" 48000 \
    >"$OUT_DIR/mac-capture.raw" \
    2>"$OUT_DIR/mac-capture.stderr" &
HELPER_PID=$!
note "helper pid=$HELPER_PID started"

(
    set +e
    while kill -0 "$HELPER_PID" 2>/dev/null; do
        sleep 1
    done
    ps -o pid= -p "$HELPER_PID" >/dev/null 2>&1
    helper_status=$?
    if [[ $helper_status -eq 0 ]]; then
        exit_code="unknown"
    else
        exit_code="exited"
    fi
    printf '%s\n' "$exit_code" >"$OUT_DIR/mac-capture.exitcode"
    note "helper pid=$HELPER_PID no longer alive status=$exit_code"
) &
HELPER_WATCH_PID=$!

/usr/bin/log stream --style compact \
    --predicate 'process == "coreaudiod" AND eventMessage CONTAINS[c] "HALS_OverloadMessage"' \
    >"$OUT_DIR/coreaudiod.log" 2>&1 &
COREAUDIO_PID=$!

/usr/bin/log stream --style compact \
    --predicate 'process == "replayd" AND (eventMessage CONTAINS[c] "AudioTap" OR eventMessage CONTAINS[c] "aggregate" OR eventMessage CONTAINS[c] "SLContentStream" OR eventMessage CONTAINS[c] "SCStream")' \
    >"$OUT_DIR/replayd.log" 2>&1 &
REPLAYD_PID=$!

/usr/bin/log stream --style compact \
    --predicate 'process == "rekordbox" AND eventMessage CONTAINS[c] "overload"' \
    >"$OUT_DIR/rekordbox.log" 2>&1 &
REKORDBOX_PID=$!

(
    while true; do
        {
            printf '=== %s ===\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')"
            ps -o pid=,ppid=,state=,etime=,command= -p "$RB_PID" "${HELPER_PID:-}" 2>/dev/null || true
            wc -c "$OUT_DIR/mac-capture.raw" "$OUT_DIR/mac-capture.stderr" "$OUT_DIR/mac-capture.diag.log" 2>/dev/null || true
        } >>"$OUT_DIR/process-status.log"
        sleep 1
    done
) &
STATUS_PID=$!

cat >"$OUT_DIR/README.txt" <<EOF
Reproduce the audio popping now.

Suggested steps:
1. Let playback run cleanly first.
2. Scrub the second deck until it becomes vulnerable.
3. Trigger the audio popping.
4. Stop this script with Ctrl-C.

Captured files:
- mac-capture.diag.log
- mac-capture.stderr
- mac-capture.exitcode
- mac-capture.raw
- coreaudiod.log
- replayd.log
- rekordbox.log
- process-status.log
- capture-meta.log
EOF

echo "capture running; reproduce the issue, then stop with Ctrl-C"
note "capture ready"
while true; do
    sleep 1
done
