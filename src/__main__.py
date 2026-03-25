import argparse
import logging
import os
import sys

from src.config import Config, resolve_config_path
from src.daemon import RecorderDaemon


def main():
    parser = argparse.ArgumentParser(description="Rekordbox Auto-Recorder")
    parser.add_argument(
        "-c", "--config",
        help="Path to config file",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--gui", action="store_true", help="Launch macOS menu bar UI (macOS only)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    config_path = resolve_config_path(args.config)

    if os.path.exists(config_path):
        config = Config.from_file(config_path)
    else:
        config = Config()

    if args.gui:
        if sys.platform != "darwin":
            print("Error: --gui is only supported on macOS", file=sys.stderr)
            sys.exit(1)
        from src.menu_bar import MenuBarApp
        MenuBarApp(config).run()
    else:
        daemon = RecorderDaemon(config)
        daemon.run()


if __name__ == "__main__":
    main()
