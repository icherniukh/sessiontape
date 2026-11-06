# Windows Package Manager

This directory contains a staging winget manifest for `sessiontape`.

The manifest is meant to be validated locally before copying it into a PR for
the upstream `microsoft/winget-pkgs` repository:

```powershell
winget validate packaging\winget\manifests\s\SessionTape\SessionTape\1.2.0
winget install --manifest packaging\winget\manifests\s\SessionTape\SessionTape\1.2.0
winget uninstall SessionTape.SessionTape
```

Before submitting upstream:

1. Build and publish a signed Windows MSI installer.
2. Update `PackageVersion`, `ReleaseDate`, `InstallerUrl`, and
   `InstallerSha256`.
3. Validate install, upgrade, and uninstall in Windows Sandbox or a clean VM.

The package targets the MSI installer as the canonical Windows package-manager
artifact. A parallel `.exe` installer can still be shipped as an alternative
download surface.
