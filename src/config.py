import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath


APP_DIR = "auto-rb-recorder"
SUPPORTED_EXPORT_FORMATS = {"wav", "mp3"}


def default_output_dir() -> str:
    return str(Path.home() / "Music" / APP_DIR)


def unix_config_path() -> str:
    return str(Path.home() / ".config" / APP_DIR / "config.toml")


def platform_config_path() -> str:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return str(PureWindowsPath(appdata) / APP_DIR / "config.toml")
        user_home = os.environ.get("USERPROFILE", str(Path.home()))
        return str(
            PureWindowsPath(user_home) / "AppData" / "Roaming" / APP_DIR / "config.toml"
        )

    if sys.platform == "darwin":
        return str(
            Path.home() / "Library" / "Application Support" / APP_DIR / "config.toml"
        )

    return unix_config_path()


def resolve_config_path(explicit_path: str | None = None) -> str:
    if explicit_path:
        return explicit_path

    return platform_config_path()


@dataclass
class Config:
    sample_rate: int = 48000
    capture_backend: str = "auto"
    output_dir: str = field(
        default_factory=default_output_dir
    )
    silence_threshold_db: float = -50
    min_silence_duration: float = 15
    min_segment_duration: float = 10
    decay_tail: float = 5
    export_format: str = "wav"
    process_name: str = "rekordbox"
    poll_interval: float = 2.0

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if (
            not isinstance(self.sample_rate, int)
            or isinstance(self.sample_rate, bool)
            or self.sample_rate <= 0
        ):
            raise ValueError("recording.sample_rate must be a positive integer")

        if not isinstance(self.export_format, str):
            raise ValueError("recording.export_format must be a string")
        self.export_format = self.export_format.lower()
        if self.export_format not in SUPPORTED_EXPORT_FORMATS:
            supported = ", ".join(sorted(SUPPORTED_EXPORT_FORMATS))
            raise ValueError(f"recording.export_format must be one of: {supported}")

        if self.silence_threshold_db > 0:
            raise ValueError("trigger.silence_threshold_db must be 0 or lower")
        if self.min_silence_duration < 0:
            raise ValueError("trigger.min_silence_duration must be non-negative")
        if self.min_segment_duration < 0:
            raise ValueError("trigger.min_segment_duration must be non-negative")
        if self.decay_tail < 0:
            raise ValueError("trigger.decay_tail must be non-negative")
        if self.poll_interval <= 0:
            raise ValueError("monitor.poll_interval must be positive")

    @classmethod
    def from_file(cls, path: str) -> "Config":
        with open(path, "rb") as f:
            data = tomllib.load(f)

        cfg = cls()
        rec = data.get("recording", {})
        trig = data.get("trigger", {})
        monitor = data.get("monitor", {})

        if "sample_rate" in rec:
            cfg.sample_rate = rec["sample_rate"]
        if "capture_backend" in rec:
            cfg.capture_backend = rec["capture_backend"]
        if "output_dir" in rec:
            cfg.output_dir = os.path.expanduser(rec["output_dir"])
        if "export_format" in rec:
            cfg.export_format = rec["export_format"]
        if "silence_threshold_db" in trig:
            cfg.silence_threshold_db = trig["silence_threshold_db"]
        if "min_silence_duration" in trig:
            cfg.min_silence_duration = trig["min_silence_duration"]
        if "min_segment_duration" in trig:
            cfg.min_segment_duration = trig["min_segment_duration"]
        if "decay_tail" in trig:
            cfg.decay_tail = trig["decay_tail"]
        if "process_name" in monitor:
            cfg.process_name = monitor["process_name"]
        if "poll_interval" in monitor:
            cfg.poll_interval = monitor["poll_interval"]

        cfg.validate()
        return cfg
