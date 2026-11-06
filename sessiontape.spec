# -*- mode: python ; coding: utf-8 -*-


import sys

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
    hiddenimports=['src.config', 'src.capture', 'src.daemon', 'src.process_monitor', 'src.recorder_core'],
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
    codesign_identity=None,
    entitlements_file=None,
)
