$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

Write-Host "Building Windows capture helper..."
& .\windows-capture\build.ps1
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to build capture helper."
}

Write-Host "Building standalone executable for Windows..."
uv run pyinstaller auto-rb-recorder.spec --clean --noconfirm

Write-Host "Build complete. Executable is at dist\auto-rb-recorder.exe"
