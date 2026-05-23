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
if (Test-Path "dist") { Remove-Item -Path "dist" -Recurse -Force }
uv run pyinstaller auto-rb-recorder.spec --clean --noconfirm

if (Get-Command iscc -ErrorAction SilentlyContinue) {
    Write-Host "Inno Setup found. Building installer..."
    iscc scripts\installer.iss
} else {
    Write-Warning "iscc not found. Skipping installer build."
}

Write-Host "Build complete. Output is in dist\"
