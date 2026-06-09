import os
import shutil
import subprocess
import sys
import time
import unittest


@unittest.skipUnless(sys.platform == "win32", "Windows-only")
class TestWindowsCaptureBinaryShutdown(unittest.TestCase):

    def _find_binary(self):
        candidate = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "windows-capture", "sessiontape-capture-win.exe")
        )
        if os.path.exists(candidate):
            return candidate
        return shutil.which("sessiontape-capture-win.exe")

    def test_stdin_close_exits_cleanly(self):
        """Closing the stdin pipe must cause a clean exit (code 0) without needing a forceful kill."""
        exe = self._find_binary()
        if not exe:
            self.skipTest("sessiontape-capture-win.exe not built")

        # stdin=PIPE is the shutdown channel: parent closes it, child detects EOF via PeekNamedPipe
        proc = subprocess.Popen(
            [exe, "--pid", str(os.getpid())],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        )

        time.sleep(0.4)

        if proc.poll() is not None:
            self.skipTest(
                f"Process exited early (code={proc.returncode}), "
                "WASAPI activation failed — cannot test shutdown"
            )

        proc.stdin.close()

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            self.fail("Process hung after stdin close (missing shutdown poll in capture loop)")

        self.assertEqual(
            proc.returncode, 0,
            f"Expected clean exit code 0, got {proc.returncode}"
        )


if __name__ == "__main__":
    unittest.main()
