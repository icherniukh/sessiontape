$ErrorActionPreference = 'Stop'

$packageArgs = @{
  packageName    = 'sessiontape'
  fileType       = 'msi'
  url64bit       = 'https://github.com/icherniukh/sessiontape/releases/download/v1.2.0/sessiontape.msi'
  softwareName   = 'SessionTape*'
  checksum64     = '0000000000000000000000000000000000000000000000000000000000000000'
  checksumType64 = 'sha256'
  silentArgs     = '/qn /norestart'
  validExitCodes = @(0, 3010, 1641)
}

Install-ChocolateyPackage @packageArgs
