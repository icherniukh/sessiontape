# Chocolatey

This directory contains a staging Chocolatey community package for
`auto-rb-recorder`.

Build and test from Windows:

```powershell
choco pack packaging\chocolatey\auto-rb-recorder\auto-rb-recorder.nuspec
choco install auto-rb-recorder --source . --version 1.2.0 -y
choco uninstall auto-rb-recorder -y
```

Before submitting to the Chocolatey Community Repository:

1. Build and publish a signed Windows installer.
2. Update the package version, release URL, and SHA256 checksum.
3. Test install, upgrade, and uninstall in Windows Sandbox or a clean VM.

Chocolatey runs package scripts through PowerShell and often runs elevated. The
installer itself is per-user, so verify the resulting scheduled task and install
location under the account that runs `choco install`.
