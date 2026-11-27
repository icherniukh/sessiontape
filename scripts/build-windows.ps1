param(
    [switch]$SkipCaptureBuild,
    [switch]$RunTests
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

$CaptureExe = "windows-capture\sessiontape-capture-win.exe"
if ($SkipCaptureBuild -and (Test-Path $CaptureExe)) {
    Write-Host "Skipping Windows capture binary rebuild (-SkipCaptureBuild)."
} else {
    Write-Host "Building Windows capture binary..."
    Push-Location "$ProjectRoot\windows-capture"
    try {
        if ($RunTests) {
            & .\build.ps1 -RunTests
        } else {
            & .\build.ps1
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to build capture binary (exit code $LASTEXITCODE)"
        }
    } finally {
        Pop-Location
    }
}

Write-Host "Building standalone executable..."
if (Test-Path "dist") { Remove-Item -Path "dist" -Recurse -Force }
uv run --with pyinstaller pyinstaller "$ProjectRoot\sessiontape.spec" --distpath "$ProjectRoot\dist" --workpath "$ProjectRoot\build" --clean --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$isccCmd = Get-Command iscc -ErrorAction SilentlyContinue
$iscc = if ($isccCmd) { $isccCmd.Source } else { $null }
if (-not $iscc) {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}

if ($iscc) {
    $version = (git describe --tags --abbrev=0 2>$null) -replace '^v', ''
    if (-not $version) { $version = "dev" }
    Write-Host "Building Inno Setup installer (version $version)..."
    & $iscc "/DAppVersion=$version" "$ScriptDir\installer.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
} else {
    Write-Warning "iscc not found. Skipping Inno Setup installer build."
}

$wixCmd = Get-Command wix -ErrorAction SilentlyContinue
if ($wixCmd) {
    $version = (git describe --tags --abbrev=0 2>$null) -replace '^v', ''
    if (-not $version) { $version = "1.4.0" } # WiX requires a strict version format like x.y.z
    if ($version -notmatch '^\d+\.\d+\.\d+(\.\d+)?$') {
        $version = "1.4.0" # Fallback if tag is not a valid MSI version
    }
    Write-Host "Building WiX installer (version $version)..."
    & wix build "$ScriptDir\installer.wxs" -acceptEula wix7 -d AppVersion=$version -b "$ProjectRoot" -o "$ProjectRoot\dist\sessiontape.msi"
    if ($LASTEXITCODE -ne 0) { throw "WiX build failed with exit code $LASTEXITCODE" }
} else {
    Write-Warning "wix not found. Skipping WiX installer build."
}

Write-Host "Build complete. Output is in dist\"
