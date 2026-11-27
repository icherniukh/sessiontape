param(
    [switch]$RunTests
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

# Try to find cl.exe
$cl = Get-Command "cl.exe" -ErrorAction SilentlyContinue

if (-Not $cl) {
    # Attempt to locate vswhere
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        Write-Host "Found vswhere, attempting to setup MSVC environment..."
        $vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        if ($vsPath) {
            $DevCmd = "$vsPath\Common7\Tools\VsDevCmd.bat"
            if (Test-Path $DevCmd) {
                cmd.exe /c "call `"$DevCmd`" -arch=x64 && set" | ForEach-Object {
                    if ($_ -match "^(.*?)=(.*)$") {
                        Set-Item -Force -Path "ENV:\$($matches[1])" -Value $matches[2]
                    }
                }
            }
        }
    }
}

# Direct fallback for VS 2017 BuildTools (only needed if vswhere still found nothing)
if (-not (Get-Command "cl.exe" -ErrorAction SilentlyContinue)) {
    $bt2017 = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2017\BuildTools\Common7\Tools\VsDevCmd.bat"
    if (Test-Path $bt2017) {
        Write-Host "Falling back to VS 2017 BuildTools directly..."
        cmd.exe /c "call `"$bt2017`" -arch=x64 && set" | ForEach-Object {
            if ($_ -match "^(.*?)=(.*)$") {
                Set-Item -Force -Path "ENV:\$($matches[1])" -Value $matches[2]
            }
        }
    }
}

$cl = Get-Command "cl.exe" -ErrorAction SilentlyContinue
if (-Not $cl) {
    Write-Error "Could not find MSVC compiler (cl.exe). Please run this script from a x64 Native Tools Command Prompt for VS."
}

$rc = Get-Command "rc.exe" -ErrorAction SilentlyContinue
$resArg = @()
if ($rc -and (Test-Path "version.rc")) {
    Write-Host "Compiling version resource..."
    & rc.exe /nologo /fo version.res version.rc
    if ($LASTEXITCODE -ne 0) {
        throw "rc.exe failed with exit code $LASTEXITCODE"
    }
    $resArg = @("version.res")
}

Write-Host "Compiling sessiontape-capture-win.exe..."
& cl.exe /nologo /EHsc /O2 /W4 /MD /std:c++17 main.cpp @resArg /link ole32.lib oleaut32.lib mmdevapi.lib uuid.lib /OUT:sessiontape-capture-win.exe
if ($LASTEXITCODE -ne 0) {
    throw "Failed to compile sessiontape-capture-win.exe (exit code $LASTEXITCODE)"
}

if ($RunTests) {
    Write-Host "Compiling and running windows-capture unit tests..."
    & cl.exe /nologo /EHsc /O2 /W4 /MD /std:c++17 tests\test_pcm_convert.cpp /Fe:test_pcm_convert.exe
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to compile test_pcm_convert.exe (exit code $LASTEXITCODE)"
    }
    & .\test_pcm_convert.exe
    if ($LASTEXITCODE -ne 0) {
        throw "windows-capture unit tests failed (exit code $LASTEXITCODE)"
    }
    Remove-Item -Force -ErrorAction SilentlyContinue test_pcm_convert.exe, test_pcm_convert.obj
}

Remove-Item -Force -ErrorAction SilentlyContinue main.obj, version.res
Write-Host "Build successful."
