$ErrorActionPreference = 'Stop'

$packageName = 'auto-rb-recorder'
$uninstallKeys = Get-UninstallRegistryKey -SoftwareName 'auto-rb-recorder*'

foreach ($key in $uninstallKeys) {
  if ($key.UninstallString -match '^(?:"?)(.+?unins\d+\.exe)(?:"?).*$') {
    $uninstaller = $Matches[1]
    if (Test-Path $uninstaller) {
      $packageArgs = @{
        packageName    = $packageName
        fileType       = 'exe'
        silentArgs     = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
        file           = $uninstaller
        validExitCodes = @(0)
      }
      Uninstall-ChocolateyPackage @packageArgs
      return
    }
  }
}

$fallbackUninstaller = Join-Path $env:LOCALAPPDATA 'Programs\auto-rb-recorder\unins000.exe'
if (Test-Path $fallbackUninstaller) {
  $packageArgs = @{
    packageName    = $packageName
    fileType       = 'exe'
    silentArgs     = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
    file           = $fallbackUninstaller
    validExitCodes = @(0)
  }
  Uninstall-ChocolateyPackage @packageArgs
}
