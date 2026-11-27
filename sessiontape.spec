# -*- mode: python ; coding: utf-8 -*-


import os
import sys

codesign_id = os.environ.get("CODESIGN_IDENTITY") or None
entitlements = "entitlements.plist" if (sys.platform == "darwin" and codesign_id) else None
win_version_file = "windows-version-info.txt" if sys.platform == "win32" else None

if sys.platform == 'win32':
    binaries = [('windows-capture\\sessiontape-capture-win.exe', '.')]
elif sys.platform == 'darwin':
    binaries = [('mac-capture/.build/release/mac-capture', '.')]
else:
    binaries = []

a = Analysis(
    ['src/__main__.py'],
    pathex=[],
    binaries=binaries,
    datas=[],
    hiddenimports=[
        'src.backends.macos_capture',
        'src.backends.windows_capture',
        'src.capture',
        'src.config',
        'src.daemon',
        'src.events',
        'src.log_retention',
        'src.perf_metrics',
        'src.platform',
        'src.process_monitor',
        'src.recorder_core',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='sessiontape',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=codesign_id,
    entitlements_file=entitlements,
    version=win_version_file,
)
