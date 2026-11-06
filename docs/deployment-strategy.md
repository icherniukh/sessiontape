# SessionTape Deployment Strategy

## Strategy

GitHub Releases are the canonical release surface. A tagged release builds the
signed macOS binary package and Windows installers, publishes checksums, and
updates Homebrew automatically. Windows package-manager publication stays gated:
winget and Chocolatey manifests are validated in CI, then submitted manually
after the release installer has been checked on a clean Windows machine.

This keeps release automation useful without letting package-manager submission
outrun installer signing, permissions UX, or clean-machine validation.

For Windows, the long-term direction is:

- MSI is the canonical installer for package managers and release validation.
- The `.exe` installer can continue to be built and shipped in parallel as an
  extra download surface, but it should not define the package-manager contract.
- Public metadata, QA, and release checks should converge on one primary Windows
  artifact so versioning and checksum updates do not drift.

## Release Pipeline

1. Run the test gate.
2. Build the macOS release on `macos-latest`.
3. Codesign the macOS executable and upload `sessiontape-macos-universal.zip`
   plus its SHA256 checksum.
4. Build the Windows release on `windows-latest`.
5. Upload `sessiontape.msi`, its SHA256 checksum, and any secondary Windows
   installers that are still intentionally supported.
6. Update the Homebrew tap from the macOS release asset.

The release workflow is tag-driven for `v*` tags and can also be run manually
from GitHub Actions.

## Package Managers

Package-manager metadata is staged in this repository so it can be reviewed
before upstream submission.

- Homebrew: automated by the release workflow after the macOS asset is uploaded.
- winget: staged under `packaging/winget`; submit to `microsoft/winget-pkgs`
  after the matching signed MSI is smoke-tested.
- Chocolatey: staged under `packaging/chocolatey`; submit to the Chocolatey
  Community Repository after MSI install, upgrade, and uninstall tests pass.

The staged package-manager validation workflow checks that the winget and
Chocolatey metadata agree on version, installer URL, and SHA256 checksum. When
enabled, it also runs `winget validate` when the hosted runner has the `winget`
CLI available, and it builds/smoke-checks the Chocolatey package metadata.

## Release Gates

Before cutting a public package-manager release:

1. Confirm the release tag matches the package metadata version.
2. Confirm the canonical Windows installer for this release is signed and
   published.
3. Update winget and Chocolatey URL/checksum fields from that exact release
   asset.
4. Validate install, upgrade, and uninstall in Windows Sandbox or a clean VM.
5. Verify the scheduled task, install location, and config path are created for
   the user account running the installer.
6. Only then submit upstream winget and Chocolatey packages.

## Current Blockers

- `.github/workflows/release.yml` is moving to an MSI-first contract, but the
  repo still needs a real signed MSI release before package-manager metadata can
  be refreshed from production artifacts.
- `.github/workflows/package-managers.yml.disabled` should stay disabled until a
  real signed MSI exists. It is useful for consistency checks, but it still
  cannot prove that the referenced GitHub release asset exists, is signed, or
  installs cleanly. The current staged Chocolatey checksum is also only 63
  characters long, so enabling the workflow before regenerating real hashes
  would just create a known-failing CI loop.
- The staged Windows package-manager metadata still carries placeholder release
  values, so winget and Chocolatey remain staging surfaces until their MSI URL
  and SHA256 values are refreshed from a real SessionTape release artifact.

## Team Loadout

1. Mara (release-pipeline-owner)
   Owns `.github/workflows/release.yml` and keeps the tagged release path
   honest. Her job is to make the artifact contract explicit: the canonical
   Windows installer must always be produced, and any secondary installer must
   be treated as optional on purpose.
2. Dmytro (windows-packaging-owner)
   Owns `scripts/build-windows.ps1`, the WiX/MSI path, any remaining Inno
   compatibility build, and the installer outputs. His job is to make MSI the
   reliable primary artifact without blocking an optional parallel `.exe` build.
3. Priya (package-manager-curator)
   Owns `.github/workflows/package-managers.yml.disabled`, `packaging/winget`, and
   `packaging/chocolatey`. Her job is to migrate staged metadata from the old
   Inno assumptions to MSI and refresh version, URL, and SHA256 only from the
   real public release asset.
4. Elena (distribution-qa)
   Owns the clean-machine install, upgrade, uninstall, and scheduled-task
   checks. Her job is to verify what non-technical DJs will actually experience
   on a fresh Windows box before any upstream package-manager submission.
5. Roman (signing-admin-coordinator)
   Owns SignPath and public release identity alignment. His job is to keep the
   signing/admin metadata, repo URL, and product name aligned to `SessionTape`
   before Windows distribution expands.

## Migration Strategy

1. Clean and checkpoint the current CI slice.
   Land the current MSI-first release and identity changes first. Keep the
   package-manager validation workflow disabled until the branch has real
   release-derived hashes.
2. Make MSI the declared source of truth.
   Mara and Dmytro change the release contract, docs, and QA target so MSI is
   the primary Windows installer. If the `.exe` stays, keep it as a secondary
   artifact instead of the packaging baseline.
3. Align signing and public identity.
   Roman makes sure the signing/admin path uses the SessionTape name, repo URL,
   and download surface before the Windows release becomes the public install
   path.
4. Migrate staged package metadata.
   Priya updates winget installer type, installer URL, checksum handling, and
   Chocolatey install/uninstall behavior so they target MSI instead of the Inno
   uninstaller path.
5. Update user-facing docs and commands.
   Public install instructions, release notes, and build docs stop describing
   the `.exe` as the only Windows path.
6. Run clean-machine Windows validation.
   Elena validates install, upgrade, uninstall, scheduled task registration,
   install location, and config-path behavior in Windows Sandbox or a clean VM.
7. Publish in channel order.
   GitHub Releases and Homebrew can ship first. winget and Chocolatey stay
   manual follow-on channels after the Windows artifact, metadata, and QA gates
   are all satisfied.

## Drift Gates

- No public winget or Chocolatey submission before a tagged release produces the
  exact Windows installer referenced by the manifests.
- No checksum updates unless they are regenerated from the matching public
  release asset.
- No "Windows release ready" claim until clean-machine install, upgrade, and
  uninstall have been checked.
- No more Inno-specific package-manager assumptions after MSI becomes the
  declared primary Windows installer.

## Not Automated Yet

- macOS notarization is not wired into CI.
- winget and Chocolatey upstream submissions are manual.
- Clean-VM install/upgrade/uninstall checks are manual.
- LaunchAgent login-time behavior still needs real verification before broad
  macOS packaging expansion.
