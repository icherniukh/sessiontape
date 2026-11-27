import argparse
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time

from src.config import Config, resolve_config_path
from src.daemon import RecorderDaemon
from src.log_retention import daily_file_handler, prune_logs


class _DedupFilter(logging.Filter):
    """Suppress repeated identical messages within a rolling time window."""

    def __init__(self, window: float = 5.0):
        super().__init__()
        self._window = window
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.name in ("sessiontape.capture", "sessiontape.perf") or message.startswith("CAPTURE "):
            return True
        key = f"{record.name}:{record.levelno}:{message}"
        now = time.monotonic()
        with self._lock:
            last = self._seen.get(key)
            if last is not None and now - last < self._window:
                return False
            self._seen[key] = now
            if len(self._seen) > 500:
                cutoff = now - self._window
                self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
                if len(self._seen) > 500:
                    sorted_items = sorted(self._seen.items(), key=lambda x: x[1])
                    self._seen = dict(sorted_items[-500:])
            return True


def build_log_handlers(
    log_file: str, fmt: logging.Formatter, stderr=None
) -> list[logging.Handler]:
    """daemon.log (daily files, RETENTION_DAYS kept), plus the console when it is a terminal.

    Under launchd (brew services) stderr is a file nothing rotates; daemon.log already has every
    line, so the console mirror is skipped there.
    """
    stderr = sys.stderr if stderr is None else stderr
    prune_logs(log_file)
    file_handler = daily_file_handler(log_file)
    file_handler.setFormatter(fmt)
    handlers: list[logging.Handler] = [file_handler]
    if stderr is not None and stderr.isatty():
        stream_handler = logging.StreamHandler(stderr)
        stream_handler.setFormatter(fmt)
        handlers.append(stream_handler)
    return handlers


def main():
    parser = argparse.ArgumentParser(description="SessionTape")
    parser.add_argument(
        "-c", "--config",
        help="Path to config file",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    config_path = resolve_config_path(args.config)
    log_dir = os.path.dirname(config_path)
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "daemon.log")

    level = logging.DEBUG if args.verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    handlers = build_log_handlers(log_file, fmt)

    log_queue: queue.Queue = queue.Queue()
    queue_handler = logging.handlers.QueueHandler(log_queue)
    queue_handler.addFilter(_DedupFilter())

    logging.basicConfig(level=level, handlers=[queue_handler])

    # Background thread handles file and console I/O so audio reader threads never block on logging.
    listener = logging.handlers.QueueListener(
        log_queue, *handlers, respect_handler_level=True
    )

    try:
        listener.start()
        if os.path.exists(config_path):
            config = Config.from_file(config_path)
        else:
            config = Config()

        daemon = RecorderDaemon(config)
        daemon.run()
    finally:
        listener.stop()


if __name__ == "__main__":
    main()
