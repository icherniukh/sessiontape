import datetime as dt
import logging
import logging.handlers
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from src.log_retention import RETENTION_DAYS, daily_file_handler, prune_logs, rotate_if_stale

TODAY = dt.date(2026, 11, 19)


def _write(path: str, text: str, day: dt.date | None = None) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if day is not None:
        ts = time.mktime(dt.datetime.combine(day, dt.time(12, 0)).timetuple())
        os.utime(path, (ts, ts))


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestRotateIfStale(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "mac-capture.log")

    def tearDown(self):
        self.dir.cleanup()

    def test_moves_log_from_an_earlier_day_to_dated_file(self):
        _write(self.path, "old run\n", dt.date(2026, 11, 17))
        rotate_if_stale(self.path, today=TODAY)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(_read(self.path + ".2026-11-17"), "old run\n")

    def test_keeps_log_written_today(self):
        _write(self.path, "today\n", TODAY)
        rotate_if_stale(self.path, today=TODAY)
        self.assertEqual(_read(self.path), "today\n")

    def test_appends_to_existing_dated_file(self):
        _write(self.path + ".2026-11-17", "first\n")
        _write(self.path, "second\n", dt.date(2026, 11, 17))
        rotate_if_stale(self.path, today=TODAY)
        self.assertEqual(_read(self.path + ".2026-11-17"), "first\nsecond\n")
        self.assertFalse(os.path.exists(self.path))

    def test_missing_log_is_a_no_op(self):
        rotate_if_stale(self.path, today=TODAY)
        self.assertEqual(os.listdir(self.dir.name), [])


class TestPruneLogs(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "daemon.log")

    def tearDown(self):
        self.dir.cleanup()

    def test_deletes_only_dated_files_older_than_retention(self):
        keep_edge = TODAY - dt.timedelta(days=RETENTION_DAYS)
        too_old = keep_edge - dt.timedelta(days=1)
        names = [
            "daemon.log",
            f"daemon.log.{keep_edge:%Y-%m-%d}",
            f"daemon.log.{too_old:%Y-%m-%d}",
            "daemon.log.2025-01-01",
            "daemon.log.bak",
            "other.log.2025-01-01",
        ]
        for name in names:
            _write(os.path.join(self.dir.name, name), "x")

        prune_logs(self.path, today=TODAY)

        self.assertEqual(
            sorted(os.listdir(self.dir.name)),
            sorted(["daemon.log", f"daemon.log.{keep_edge:%Y-%m-%d}", "daemon.log.bak",
                    "other.log.2025-01-01"]),
        )

    def test_missing_directory_is_a_no_op(self):
        prune_logs(os.path.join(self.dir.name, "nope", "daemon.log"), today=TODAY)


class TestDailyFileHandler(unittest.TestCase):
    def test_rolls_over_at_midnight_with_dated_suffix(self):
        with tempfile.TemporaryDirectory() as d:
            handler = daily_file_handler(os.path.join(d, "daemon.log"))
            try:
                self.assertEqual(handler.when, "MIDNIGHT")
                self.assertEqual(handler.backupCount, RETENTION_DAYS)
                self.assertEqual(handler.suffix, "%Y-%m-%d")
            finally:
                handler.close()


class TestMacCaptureBackendRotation(unittest.TestCase):
    @patch("src.backends.macos_capture._find_executable", return_value="/usr/bin/true")
    @patch("src.backends.macos_capture.subprocess.Popen")
    def test_rotates_stale_log_before_spawning(self, mock_popen, _):
        from src.backends.macos_capture import MacCaptureBackend, MacCaptureOptions

        mock_popen.return_value = MagicMock()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "mac-capture.log")
            yesterday = dt.date.today() - dt.timedelta(days=1)
            _write(path, "yesterday\n", yesterday)

            MacCaptureBackend(MacCaptureOptions(log_path=path)).start(1, 48000)

            self.assertFalse(os.path.exists(path))
            self.assertEqual(_read(f"{path}.{yesterday:%Y-%m-%d}"), "yesterday\n")
            mock_popen.assert_called_once()


class TestBuildLogHandlers(unittest.TestCase):
    def _handlers(self, tty: bool):
        from src.__main__ import build_log_handlers

        stderr = MagicMock()
        stderr.isatty.return_value = tty
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        handlers = build_log_handlers(os.path.join(d.name, "daemon.log"), logging.Formatter(), stderr)
        for h in handlers:
            self.addCleanup(h.close)
        return handlers

    def test_console_mirror_only_on_a_terminal(self):
        self.assertEqual(
            [type(h) for h in self._handlers(tty=True)],
            [logging.handlers.TimedRotatingFileHandler, logging.StreamHandler],
        )

    def test_no_console_mirror_under_launchd(self):
        self.assertEqual(
            [type(h) for h in self._handlers(tty=False)],
            [logging.handlers.TimedRotatingFileHandler],
        )


if __name__ == "__main__":
    unittest.main()
