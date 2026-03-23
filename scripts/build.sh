#!/usr/bin/env bash
# scripts/build.sh — Build standalone macOS executable using PyInstaller
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

echo "Building standalone executable..."
uv run pyinstaller --name=auto-rb-recorder \
            --onefile \
            --clean \
            --noconfirm \
            --target-architecture universal2 \
            --hidden-import=src.config \
            --hidden-import=src.capture \
            --hidden-import=src.daemon \
            --hidden-import=src.process_monitor \
            --hidden-import=src.recorder_core \
            src/__main__.py

echo "Build complete. Executable is at dist/auto-rb-recorder"
