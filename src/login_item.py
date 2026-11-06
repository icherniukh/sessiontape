"""Manage macOS Launch at Login via a LaunchAgent plist."""

import os
import subprocess

LABEL = "dev.icherniukh.sessiontape"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
_LEGACY_PLIST_PATHS = (os.path.expanduser("~/Library/LaunchAgents/com.rb-recorder.plist"),)

_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{executable}</string>
        <string>--gui</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""


def is_enabled() -> bool:
    return any(os.path.exists(p) for p in (PLIST_PATH, *_LEGACY_PLIST_PATHS))


def enable(executable_path: str) -> None:
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    log_path = os.path.expanduser("~/Library/Logs/sessiontape.log")
    plist_content = _PLIST_TEMPLATE.format(label=LABEL, executable=executable_path, log_path=log_path)
    with open(PLIST_PATH, "w") as f:
        f.write(plist_content)
    subprocess.run(["launchctl", "load", PLIST_PATH], check=False)


def disable() -> None:
    for path in (PLIST_PATH, *_LEGACY_PLIST_PATHS):
        if not os.path.exists(path):
            continue
        subprocess.run(["launchctl", "unload", path], check=False)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
