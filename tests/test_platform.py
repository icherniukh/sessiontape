import unittest
from unittest.mock import patch

from src.backends.macos_capture import AudioteeCaptureBackend, MacCaptureBackend
from src.backends.windows_capture import WindowsCaptureBackend
from src.platform import get_platform_backend


class TestPlatformBackendSelection(unittest.TestCase):
    @patch("src.platform.sys.platform", "darwin")
    def test_macos_defaults_to_mac_capture(self):
        backend = get_platform_backend()
        self.assertIsInstance(backend, MacCaptureBackend)

    @patch("src.platform.sys.platform", "darwin")
    def test_macos_can_select_audiotee(self):
        backend = get_platform_backend("audiotee")
        self.assertIsInstance(backend, AudioteeCaptureBackend)

    @patch("src.platform.sys.platform", "darwin")
    @patch.dict("src.platform.os.environ", {"RB_CAPTURE_BACKEND": "audiotee"}, clear=False)
    def test_env_override_selects_audiotee(self):
        backend = get_platform_backend()
        self.assertIsInstance(backend, AudioteeCaptureBackend)

    @patch("src.platform.sys.platform", "darwin")
    def test_invalid_macos_backend_raises(self):
        with self.assertRaises(ValueError):
            get_platform_backend("windows")

    @patch("src.platform.sys.platform", "win32")
    def test_windows_accepts_auto(self):
        backend = get_platform_backend("auto")
        self.assertIsInstance(backend, WindowsCaptureBackend)

    @patch("src.platform.sys.platform", "win32")
    def test_invalid_windows_backend_raises(self):
        with self.assertRaises(ValueError):
            get_platform_backend("audiotee")


if __name__ == "__main__":
    unittest.main()
