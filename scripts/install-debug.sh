#!/usr/bin/env bash
# scripts/install-debug.sh — Install rb-recorder LaunchAgent with verbose debug logging.
#
# Usage:
#   ./scripts/install-debug.sh           # Enable debug mode (adds --verbose)
#   ./scripts/install-debug.sh --restore # Remove --verbose (back to normal)
#
# Logs land at /tmp/rb-recorder.log (same as always, now with DEBUG lines).
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.rb-recorder.plist"

if [ ! -f "$PLIST" ]; then
    echo "Error: LaunchAgent not found at $PLIST" >&2
    echo "Install the LaunchAgent first, then re-run this script." >&2
    exit 1
fi

RESTORE=${1:-}

python3 - "$PLIST" "$RESTORE" <<'PYEOF'
import plistlib, sys, os

plist_path = sys.argv[1]
restore = sys.argv[2] == "--restore"

with open(plist_path, "rb") as f:
    plist = plistlib.load(f)

args = plist.get("ProgramArguments", [])

if restore:
    if "--verbose" in args:
        args.remove("--verbose")
        plist["ProgramArguments"] = args
        with open(plist_path, "wb") as f:
            plistlib.dump(plist, f)
        print("Removed --verbose from LaunchAgent (normal mode restored)")
    else:
        print("--verbose was not set; nothing to restore")
else:
    if "--verbose" not in args:
        args.append("--verbose")
        plist["ProgramArguments"] = args
        with open(plist_path, "wb") as f:
            plistlib.dump(plist, f)
        print("Added --verbose to LaunchAgent (debug mode)")
    else:
        print("--verbose already set; nothing changed")
PYEOF

echo "Reloading LaunchAgent..."
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

if [[ "$RESTORE" == "--restore" ]]; then
    echo "Normal mode active. Logs: tail -f /tmp/rb-recorder.log"
else
    echo "Debug mode active. Logs: tail -f /tmp/rb-recorder.log"
fi
