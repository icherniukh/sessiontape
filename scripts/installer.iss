[Setup]
AppName=SessionTape
#ifndef AppVersion
  #define AppVersion "dev"
#endif
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\SessionTape
DefaultGroupName=SessionTape
UninstallDisplayIcon={app}\sessiontape.exe
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=sessiontape-setup
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

[Code]
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  // Try to stop the scheduled task and kill the process if it's running
  Exec('powershell.exe', '-WindowStyle Hidden -ExecutionPolicy Bypass -Command "Stop-ScheduledTask -TaskName ''SessionTape'' -ErrorAction SilentlyContinue; Get-Process sessiontape, sessiontape-capture-win -ErrorAction SilentlyContinue | Stop-Process -Force"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

[Files]
Source: "..\dist\sessiontape.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config.default.toml"; DestDir: "{userappdata}\sessiontape"; DestName: "config.toml"; Flags: onlyifdoesntexist

[Run]
Filename: "powershell.exe"; Parameters: "-WindowStyle Hidden -ExecutionPolicy Bypass -Command ""$Action = New-ScheduledTaskAction -Execute '{app}\sessiontape.exe'; $Trigger = New-ScheduledTaskTrigger -AtLogOn; $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0; $Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive; Register-ScheduledTask -TaskName 'SessionTape' -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null; Start-ScheduledTask -TaskName 'SessionTape'"""; Flags: runhidden

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-WindowStyle Hidden -ExecutionPolicy Bypass -Command ""Stop-ScheduledTask -TaskName 'SessionTape' -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName 'SessionTape' -Confirm:$false; Get-Process sessiontape, sessiontape-capture-win -ErrorAction SilentlyContinue | Stop-Process -Force"""; Flags: runhidden; RunOnceId: "CleanupScheduledTask"
