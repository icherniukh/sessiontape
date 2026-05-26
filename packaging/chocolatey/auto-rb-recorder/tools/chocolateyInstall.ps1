$ErrorActionPreference = 'Stop'

$packageArgs = @{
  packageName    = 'auto-rb-recorder'
  fileType       = 'exe'
  url64bit       = 'https://github.com/icherniukh/auto-rb-recorder/releases/download/v1.2.0/auto-rb-recorder-setup.exe'
  softwareName   = 'auto-rb-recorder*'
  checksum64     = 'C020C239B2B4845FC802C4C962AD6AD380A729C3C62AA841FC4A7240069A025'
  checksumType64 = 'sha256'
  silentArgs     = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
  validExitCodes = @(0)
}

Install-ChocolateyPackage @packageArgs
