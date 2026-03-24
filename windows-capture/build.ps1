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
        $vsPath = & $vswhere -latest -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        if ($vsPath) {
            $DevCmd = "$vsPath\Common7\Tools\VsDevCmd.bat"
            if (Test-Path $DevCmd) {
                # Setup environment variables using VsDevCmd.bat
                cmd.exe /c "call `"$DevCmd`" -arch=x64 && set" | ForEach-Object {
                    if ($_ -match "^(.*?)=(.*)$") {
                        Set-Item -Force -Path "ENV:\$($matches[1])" -Value $matches[2]
                    }
                }
            }
        }
    }
}

$cl = Get-Command "cl.exe" -ErrorAction SilentlyContinue
if (-Not $cl) {
    Write-Error "Could not find MSVC compiler (cl.exe). Please run this script from a x64 Native Tools Command Prompt for VS."
}

Write-Host "Compiling rb-capture-win.exe..."
cl.exe /EHsc /O2 /W3 /MD /std:c++17 main.cpp /link ole32.lib oleaut32.lib mmdevapi.lib /OUT:rb-capture-win.exe

if ($LASTEXITCODE -eq 0) {
    Write-Host "Build successful."
} else {
    Write-Error "Build failed."
}
