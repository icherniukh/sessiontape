$ErrorActionPreference = "Stop"

$TaskName = "AutoRbRecorder"

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Task) {
    Write-Host "Stopping scheduled task..."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    
    Write-Host "Unregistering scheduled task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
} else {
    Write-Host "Scheduled task '$TaskName' not found."
}

$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\auto-rb-recorder"
if (Test-Path $InstallDir) {
    Write-Host "Removing installation directory..."
    # Ensure process is not running before deletion
    Get-Process auto-rb-recorder -ErrorAction SilentlyContinue | Stop-Process -Force
    Remove-Item -Path $InstallDir -Recurse -Force
}

Write-Host "Successfully uninstalled $TaskName."
