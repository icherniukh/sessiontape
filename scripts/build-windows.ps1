$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

Write-Host "Building standalone executable for Windows..."
uv run pyinstaller --name=auto-rb-recorder `
            --onefile `
            --clean `
            --noconfirm `
            --hidden-import=src.config `
            --hidden-import=src.capture `
            --hidden-import=src.daemon `
            --hidden-import=src.process_monitor `
            --hidden-import=src.recorder_core `
            src\__main__.py

Write-Host "Build complete. Executable is at dist\auto-rb-recorder.exe"
