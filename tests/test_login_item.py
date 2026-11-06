import plistlib
from unittest import mock

from src import login_item


def test_enable_writes_valid_plist_and_disable_removes_legacy(tmp_path, monkeypatch):
    plist = tmp_path / f"{login_item.LABEL}.plist"
    legacy = tmp_path / "com.rb-recorder.plist"
    legacy.write_text("old")
    monkeypatch.setattr(login_item, "PLIST_PATH", str(plist))
    monkeypatch.setattr(login_item, "_LEGACY_PLIST_PATHS", (str(legacy),))

    with mock.patch.object(login_item.subprocess, "run") as run:
        assert login_item.is_enabled()  # legacy agent counts as enabled
        login_item.enable("/Applications/SessionTape.app/Contents/MacOS/sessiontape")
        data = plistlib.loads(plist.read_bytes())
        assert data["Label"] == login_item.LABEL
        assert data["ProgramArguments"] == ["/Applications/SessionTape.app/Contents/MacOS/sessiontape", "--gui"]

        login_item.disable()
        assert not plist.exists() and not legacy.exists()
        assert not login_item.is_enabled()
        assert run.call_count == 3  # load, then unload for each existing plist
