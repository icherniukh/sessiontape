"""Manage macOS Launch at Login via a LaunchAgent plist."""

import os
import subprocess

PLIST_PATH = os.path.expanduser("~/Library/LaunchAgents/com.rb-recorder.plist")

_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.rb-recorder</string>
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
    <string>/tmp/rb-recorder.log</string>
    <key>StandardOutPath</key>
    <string>/tmp/rb-recorder.log</string>
</dict>
</plist>
"""


def is_enabled() -> bool:
    return os.path.exists(PLIST_PATH)


def enable(executable_path: str) -> None:
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    plist_content = _PLIST_TEMPLATE.format(executable=executable_path)
    with open(PLIST_PATH, "w") as f:
        f.write(plist_content)
    subprocess.run(["launchctl", "load", PLIST_PATH], check=False)


def disable() -> None:
    subprocess.run(["launchctl", "unload", PLIST_PATH], check=False)
    try:
        os.unlink(PLIST_PATH)
    except FileNotFoundError:
        pass
