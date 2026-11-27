#!/usr/bin/env bash
# scripts/build.sh — Build standalone macOS executable using PyInstaller
#
# Usage:
#   ./scripts/build.sh                    # Build Python dist, bundle existing mac-capture binary
#   ./scripts/build.sh --full             # Rebuild mac-capture (release), then build Python dist
#   ./scripts/build.sh --debug            # Rebuild mac-capture (debug), then build Python dist
#   ./scripts/build.sh --full --install   # Build release dist, install into Homebrew prefix, and restart service
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

MACCAPTURE_RELEASE_BIN="$SCRIPT_DIR/mac-capture/.build/release/mac-capture"
MACCAPTURE_DEBUG_BIN="$SCRIPT_DIR/mac-capture/.build/debug/mac-capture"
MACCAPTURE_BIN="$MACCAPTURE_RELEASE_BIN"

BUILD_CONFIG="release"
REBUILD_MACCAPTURE=false
INSTALL_LOCAL=false

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
        --install)
            INSTALL_LOCAL=true
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

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
    echo "Code signing $MACCAPTURE_BIN with $CODESIGN_IDENTITY..."
    xcrun codesign --force --options runtime --timestamp \
        --identifier dev.icherniukh.sessiontape.mac-capture \
        --sign "$CODESIGN_IDENTITY" "$MACCAPTURE_BIN"
fi

echo "Building standalone executable..."
rm -rf dist/
uv run --with pyinstaller pyinstaller sessiontape.spec --clean --noconfirm

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
    echo "Code signing dist/sessiontape with $CODESIGN_IDENTITY..."
    xcrun codesign --force --options runtime --timestamp \
        --identifier dev.icherniukh.sessiontape \
        --entitlements entitlements.plist \
        --sign "$CODESIGN_IDENTITY" dist/sessiontape
fi

echo "Build complete. Executable is at dist/sessiontape"

if [ "$INSTALL_LOCAL" = true ]; then
    BREW_PREFIX="$(brew --prefix sessiontape 2>/dev/null || true)"
    if [[ -z "$BREW_PREFIX" || ! -d "$BREW_PREFIX/bin" ]]; then
        echo "Error: Homebrew sessiontape prefix not found. Install 'icherniukh/tap/sessiontape' first." >&2
        exit 1
    fi
    echo "Installing dist/sessiontape -> $BREW_PREFIX/bin/sessiontape..."
    install -m 755 dist/sessiontape "$BREW_PREFIX/bin/sessiontape"
    echo "Restarting brew service sessiontape..."
    brew services restart sessiontape
fi
