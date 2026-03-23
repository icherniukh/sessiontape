$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Split-Path -Parent $ScriptDir

$ExePath = Join-Path $ProjectRoot "dist\auto-rb-recorder.exe"
if (-Not (Test-Path $ExePath)) {
    Write-Error "Executable not found at $ExePath. Please run scripts\build-windows.ps1 first."
}

$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\auto-rb-recorder"
if (-Not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir | Out-Null
}

$TargetExe = Join-Path $InstallDir "auto-rb-recorder.exe"
Copy-Item -Path $ExePath -Destination $TargetExe -Force
Write-Host "Copied executable to $TargetExe"

$TaskName = "AutoRbRecorder"
$Action = New-ScheduledTaskAction -Execute $TargetExe
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0

# Create task running as current user
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null

Write-Host "Successfully installed and scheduled $TaskName to run at logon."
Write-Host "You can start it manually now by running: Start-ScheduledTask -TaskName $TaskName"
