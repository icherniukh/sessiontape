#!/usr/bin/env bash
# scripts/build.sh — Build standalone macOS executable using PyInstaller
#
# Usage:
#   ./scripts/build.sh          # Build Python dist, bundle existing mac-capture binary
#   ./scripts/build.sh --full   # Rebuild mac-capture (release), then build Python dist
#   ./scripts/build.sh --debug  # Rebuild mac-capture (debug), then build Python dist
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

MACCAPTURE_RELEASE_BIN="$SCRIPT_DIR/mac-capture/.build/release/mac-capture"
MACCAPTURE_DEBUG_BIN="$SCRIPT_DIR/mac-capture/.build/debug/mac-capture"
MACCAPTURE_BIN="$MACCAPTURE_RELEASE_BIN"

BUILD_CONFIG="release"
REBUILD_MACCAPTURE=false

for arg in "$@"; do
    case $arg in
        --full)
            REBUILD_MACCAPTURE=true
            BUILD_CONFIG="release"
            MACCAPTURE_BIN="$MACCAPTURE_RELEASE_BIN"
            ;;
        --debug)
            REBUILD_MACCAPTURE=true
            BUILD_CONFIG="debug"
            MACCAPTURE_BIN="$MACCAPTURE_DEBUG_BIN"
            ;;
    esac
done

if [ "$REBUILD_MACCAPTURE" = true ]; then
    echo "Building mac-capture ($BUILD_CONFIG)..."
    (cd "$SCRIPT_DIR/mac-capture" && swift build -c "$BUILD_CONFIG" 2>&1)
elif [[ ! -f "$MACCAPTURE_BIN" ]]; then
    echo "Error: mac-capture binary not found at $MACCAPTURE_BIN" >&2
    echo "Run './scripts/build.sh --full' or '--debug' to build it first." >&2
    exit 1
fi

echo "Building standalone executable..."
uv run pyinstaller --name=auto-rb-recorder \
            --onedir \
            --clean \
            --noconfirm \
            --add-binary "$MACCAPTURE_BIN:." \
            --hidden-import=src.config \
            --hidden-import=src.capture \
            --hidden-import=src.daemon \
            --hidden-import=src.process_monitor \
            --hidden-import=src.recorder_core \
            src/__main__.py

echo "Build complete. Executable is at dist/auto-rb-recorder/auto-rb-recorder (mac-capture bundled)"
