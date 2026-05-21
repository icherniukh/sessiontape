import argparse
import logging
import os
from pathlib import Path

from src.config import Config, resolve_config_path
from src.daemon import RecorderDaemon


def main():
    parser = argparse.ArgumentParser(description="Rekordbox Auto-Recorder")
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

    handlers = [
        logging.StreamHandler(),
        logging.FileHandler(log_file, encoding="utf-8")
    ]

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers
    )

    if os.path.exists(config_path):
        config = Config.from_file(config_path)
    else:
        config = Config()

    daemon = RecorderDaemon(config)
    daemon.run()


if __name__ == "__main__":
    main()
