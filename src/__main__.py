import argparse
import logging
import logging.handlers
import os
import queue
import time

from src.config import Config, resolve_config_path
from src.daemon import RecorderDaemon


class _DedupFilter(logging.Filter):
    """Suppress repeated identical messages within a rolling time window."""

    def __init__(self, window: float = 5.0):
        super().__init__()
        self._window = window
        self._seen: dict[str, float] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        key = f"{record.name}:{record.levelno}:{record.getMessage()}"
        now = time.monotonic()
        last = self._seen.get(key)
        if last is not None and now - last < self._window:
            return False
        self._seen[key] = now
        if len(self._seen) > 500:
            cutoff = now - self._window
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
            if len(self._seen) > 500:
                # Hard cap: discard oldest entries
                sorted_items = sorted(self._seen.items(), key=lambda x: x[1])
                self._seen = dict(sorted_items[-500:])
        return True


def main():
    parser = argparse.ArgumentParser(description="SessionTape")
    parser.add_argument("-c", "--config", help="Path to config file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    config_path = resolve_config_path(args.config)
    log_dir = os.path.dirname(config_path)
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "daemon.log")

    level = logging.DEBUG if args.verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    dedup = _DedupFilter()
    file_handler.addFilter(dedup)
    stream_handler.addFilter(dedup)

    log_queue: queue.Queue = queue.Queue()
    queue_handler = logging.handlers.QueueHandler(log_queue)

    logging.basicConfig(level=level, handlers=[queue_handler])

    # Background thread does all file and console I/O, keeping the audio thread unblocked.
    listener = logging.handlers.QueueListener(
        log_queue, file_handler, stream_handler, respect_handler_level=True
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
