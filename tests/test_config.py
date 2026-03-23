import unittest
import tempfile
import os
from unittest.mock import patch

from src.config import (
    Config,
    default_output_dir,
    legacy_config_path,
    platform_config_path,
    resolve_config_path,
)


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = Config()
        self.assertEqual(cfg.sample_rate, 48000)
        self.assertEqual(cfg.silence_threshold_db, -50)
        self.assertEqual(cfg.min_silence_duration, 15)
        self.assertEqual(cfg.decay_tail, 5)
        self.assertEqual(cfg.poll_interval, 2.0)
        self.assertEqual(cfg.process_name, "rekordbox")
        self.assertTrue(cfg.output_dir.endswith("auto-rb-recorder"))
        self.assertEqual(cfg.output_dir, default_output_dir())

    def test_platform_config_path_windows(self):
        with patch("src.config.os.name", "nt"):
            with patch.dict("src.config.os.environ", {"APPDATA": r"C:\Users\ivan\AppData\Roaming"}, clear=False):
                self.assertEqual(
                    platform_config_path(),
                    r"C:\Users\ivan\AppData\Roaming\rb-recorder\config.toml",
                )

    def test_resolve_config_path_prefers_legacy_when_present(self):
        legacy_path = legacy_config_path()
        with patch("src.config.os.path.exists", side_effect=lambda path: path == legacy_path):
            self.assertEqual(resolve_config_path(), legacy_path)

    def test_resolve_config_path_uses_explicit_override(self):
        self.assertEqual(resolve_config_path("/tmp/custom.toml"), "/tmp/custom.toml")

    def test_load_from_toml(self):
        toml_content = (
            '[recording]\n'
            'sample_rate = 44100\n'
            'output_dir = "/tmp/my_sets"\n'
            '\n'
            '[trigger]\n'
            'silence_threshold_db = -40\n'
            'min_silence_duration = 20\n'
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(toml_content)
            f.flush()
            cfg = Config.from_file(f.name)

        os.unlink(f.name)
        self.assertEqual(cfg.sample_rate, 44100)
        self.assertEqual(cfg.output_dir, "/tmp/my_sets")
        self.assertEqual(cfg.silence_threshold_db, -40)
        self.assertEqual(cfg.min_silence_duration, 20)
        self.assertEqual(cfg.decay_tail, 5)

if __name__ == "__main__":
    unittest.main()
