import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath


APP_DIR = "rb-recorder"
APP_OUTPUT_DIR = "auto-rb-recorder"


def default_output_dir() -> str:
    return str(Path.home() / "Music" / APP_OUTPUT_DIR)


def legacy_config_path() -> str:
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

    return legacy_config_path()


def resolve_config_path(explicit_path: str | None = None) -> str:
    if explicit_path:
        return explicit_path

    legacy_path = legacy_config_path()
    if os.path.exists(legacy_path):
        return legacy_path

    return platform_config_path()


@dataclass
class Config:
    sample_rate: int = 48000
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

        return cfg

    def to_toml_string(self) -> str:
        output_dir = self.output_dir.replace("\\", "/")
        return (
            f'[recording]\n'
            f'sample_rate = {self.sample_rate}\n'
            f'output_dir = "{output_dir}"\n'
            f'export_format = "{self.export_format}"\n'
            f'\n'
            f'[trigger]\n'
            f'silence_threshold_db = {self.silence_threshold_db}\n'
            f'min_silence_duration = {self.min_silence_duration}\n'
            f'min_segment_duration = {self.min_segment_duration}\n'
            f'decay_tail = {self.decay_tail}\n'
            f'\n'
            f'[monitor]\n'
            f'process_name = "{self.process_name}"\n'
            f'poll_interval = {self.poll_interval}\n'
        )
