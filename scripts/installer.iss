[Setup]
AppName=auto-rb-recorder
AppVersion=1.0.0
DefaultDirName={localappdata}\Programs\auto-rb-recorder
DefaultGroupName=auto-rb-recorder
UninstallDisplayIcon={app}\rb-recorder-bin.exe
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=auto-rb-recorder-setup
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64

[Files]
Source: "..\dist\rb-recorder-bin.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config.default.toml"; DestDir: "{userappdata}\rb-recorder"; DestName: "config.toml"; Flags: onlyifdoesntexist

[Run]
Filename: "powershell.exe"; Parameters: "-WindowStyle Hidden -ExecutionPolicy Bypass -Command ""$Action = New-ScheduledTaskAction -Execute '{app}\rb-recorder-bin.exe'; $Trigger = New-ScheduledTaskTrigger -AtLogOn; $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0; $Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive; Register-ScheduledTask -TaskName 'AutoRbRecorder' -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null; Start-ScheduledTask -TaskName 'AutoRbRecorder'"""; Flags: runhidden

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-WindowStyle Hidden -ExecutionPolicy Bypass -Command ""Stop-ScheduledTask -TaskName 'AutoRbRecorder' -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName 'AutoRbRecorder' -Confirm:$false; Get-Process auto-rb-recorder -ErrorAction SilentlyContinue | Stop-Process -Force"""; Flags: runhidden
