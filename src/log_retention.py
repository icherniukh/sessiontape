"""Daily log files with a fixed retention window.

Logs roll over once per day into ``<name>.<YYYY-MM-DD>`` (the suffix ``TimedRotatingFileHandler``
uses for ``when="midnight"``), and dated files older than ``RETENTION_DAYS`` are deleted.
"""

import datetime as dt
import logging.handlers
import os
import re
import shutil

RETENTION_DAYS = 14

_DATE_SUFFIX = re.compile(r"^\.(\d{4}-\d{2}-\d{2})$")


def daily_file_handler(path: str) -> logging.handlers.TimedRotatingFileHandler:
    """File handler that rolls over at local midnight and keeps RETENTION_DAYS dated files."""
    return logging.handlers.TimedRotatingFileHandler(
        path, when="midnight", backupCount=RETENTION_DAYS, encoding="utf-8"
    )


def rotate_if_stale(path: str, today: dt.date | None = None) -> None:
    """Move a log last written before today to ``<path>.<last-write date>``.

    For logs written by another process (mac-capture appends to its --log file), so rotation
    happens here before that process is started.
    """
    if not os.path.exists(path):
        return
    day = dt.date.fromtimestamp(os.path.getmtime(path))
    if day >= (today or dt.date.today()):
        return
    target = f"{path}.{day:%Y-%m-%d}"
    if os.path.exists(target):
        with open(path, "rb") as src, open(target, "ab") as dst:
            shutil.copyfileobj(src, dst)
        os.remove(path)
    else:
        os.replace(path, target)


def prune_logs(path: str, days: int = RETENTION_DAYS, today: dt.date | None = None) -> None:
    """Delete ``<path>.<YYYY-MM-DD>`` files dated more than ``days`` days before today."""
    directory, base = os.path.split(path)
    directory = directory or "."
    cutoff = (today or dt.date.today()) - dt.timedelta(days=days)
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return
    for name in names:
        if not name.startswith(base):
            continue
        match = _DATE_SUFFIX.match(name[len(base):])
        if not match:
            continue
        try:
            day = dt.date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if day < cutoff:
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass
