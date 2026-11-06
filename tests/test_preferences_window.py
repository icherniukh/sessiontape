import importlib.util
import sys
import unittest


@unittest.skipUnless(
    sys.platform == "darwin" and importlib.util.find_spec("objc") is not None,
    "macOS GUI dependencies are required",
)
class TestPreferencesWindow(unittest.TestCase):
    def test_module_imports(self):
        from src.ui.preferences_window import PreferencesWindowController

        self.assertIsNotNone(PreferencesWindowController)
