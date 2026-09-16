# Packaging and installers

Everything needed to turn a checkout into something a machinist can
double-click. Three artifacts, one file list, one version.

| Artifact | Built by | Installs to | Needs admin? |
|---|---|---|---|
| `MoxaSerial-<v>.zip` | `tools/make_release_zip.py` | wherever you unzip it (plus `install.py`) | no |
| `MoxaSerial-<v>.pkg` | `installers/macos/build_pkg.sh` | `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/MoxaSerial` | **no** |
| `MoxaSerial-<v>-setup.exe` | `installers/windows/MoxaSerial.iss` (Inno Setup 6) | `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\MoxaSerial` | **no** |

Everything lands in `dist/`, which is gitignored.

---

## The two things that must stay in one place

**The file list.** [`payload.py`](payload.py) is the single source of truth for
what ships: `MoxaSerial.py`, `MoxaSerial.manifest`, `moxaserial_loader.py`,
`moxaserial/**`, `resources/**`, `LICENSE`, `README.md` — and nothing else. No
tests, tools, research or `.devdata`; no `__pycache__`.

`tools/make_release_zip.py`, `install.py` (in a source checkout) and
`build_pkg.sh` all read the list from there. Two places restate it because
they cannot import Python at build time:

* `installers/windows/MoxaSerial.iss` — the `[Files]` section
* `installers/windows/install.ps1` — `$IncludeFiles` / `$IncludeTrees`

**If you add a runtime file, change all three.** There is also
`tools/public_manifest.txt`, maintained separately for the public source drop;
`payload.verify_against_manifest()` diffs the two, and
`make_release_zip.py --check-manifest` (which CI runs) reports any drift as a
warning.

**The version.** `MoxaSerial.manifest`'s `version` field, full stop. Change it
with

```bash
python3 tools/bump_version.py 0.2.0
```

which edits the manifest (and a `CHANGELOG.md` heading if you keep one) and
prints the commit/tag commands. Everything else reads the version from there:
the zip name, `pkgbuild --version`, the Inno `MyAppVersion` define, and the
release job's tag check.

---

## Building locally

```bash
# portable zip -- deterministic, same bytes every time
python3 tools/make_release_zip.py            # -> dist/MoxaSerial-0.1.0.zip
python3 tools/make_release_zip.py --check-manifest

# macOS installer package
installers/macos/build_pkg.sh                # -> dist/MoxaSerial-0.1.0.pkg
installers/macos/build_pkg.sh --version 1.2.3 --out-dir build

# Windows installer (needs Inno Setup 6; ISCC.exe on PATH), from the repo root
iscc /DMyAppVersion=0.1.0 installers\windows\MoxaSerial.iss
```

Testing an installer without touching a real Fusion install:

```bash
python3 install.py --dest /tmp/addins            # then check /tmp/addins/MoxaSerial
python3 install.py --uninstall --dest /tmp/addins
python3 install.py --dry-run                     # say what would happen, change nothing

# same for the PowerShell fallback
powershell -ExecutionPolicy Bypass -File installers\windows\install.ps1 -Dest C:\Temp\addins
```

`install.py` also honours `$MOXASERIAL_ADDINS_DIR`, and `install.ps1` honours
`$env:MOXASERIAL_ADDINS_DIR`.

---

## How the macOS package installs into `$HOME` without sudo

This is the part worth understanding before changing anything.

`pkgbuild` is given a **relative** `--install-location`:

```
Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns
```

A relative install location makes a *home-relative* package, and
`distribution.xml.in` then restricts where it may be installed:

```xml
<domains enable_anywhere="false" enable_currentUserHome="true" enable_localSystem="false"/>
```

With only the current-user-home domain enabled, Installer.app installs into the
user's home directory and never asks for an administrator password, and from a
terminal:

```bash
installer -pkg dist/MoxaSerial-0.1.0.pkg -target CurrentUserHomeDirectory   # no sudo
```

`installer` reports `Installing at base path /Users/<you>`, and the files land
in `~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/MoxaSerial`.
Nothing is ever written to `/Library`. `relocatable="false"` is already implied
(there are no bundles in the payload), and `PackageInfo` confirms it.

### The scripts

`scripts/preinstall` runs first and moves any existing install aside to
`MoxaSerial.bak-<timestamp>`. This matters for two reasons:

1. A package **overlays** files rather than replacing a directory, so without
   it an upgrade would leave behind modules the new version deleted — and a
   stale `.py` is still importable.
2. If the AddIns entry is a **symlink to a source checkout** (how developers
   run the add-in), laying the payload down would write *through* the link and
   overwrite the working tree. The script tests `-L` as well as `-e`, so both
   live and broken links are renamed rather than followed.

`scripts/postinstall` verifies `MoxaSerial.manifest` arrived, normalises modes,
and (only if it somehow runs as root) hands the tree back to the home
directory's owner. It exits non-zero if the payload is not where it should be,
so a silent mis-install shows up as a failed install.

Both scripts resolve the home directory from installer argument `$3`, then
`$HOME`, then the console user via `stat -f %Su /dev/console` + `dscl`, so they
behave the same however they are invoked.

### Signing and notarising (not done yet)

The package is currently **unsigned**, so Gatekeeper warns on a downloaded
`.pkg` (right-click → *Open* gets past it). To sign, get a *Developer ID
Installer* certificate into the keychain and set one environment variable:

```bash
export MOXASERIAL_PKG_SIGN_ID="Developer ID Installer: Your Name (TEAMID)"
installers/macos/build_pkg.sh
```

`build_pkg.sh` passes it to `productbuild --sign`. Then notarise and staple:

```bash
xcrun notarytool submit dist/MoxaSerial-<v>.pkg --keychain-profile AC_PASSWORD --wait
xcrun stapler staple dist/MoxaSerial-<v>.pkg
```

In CI, import the certificate into a temporary keychain (e.g. with
`apple-actions/import-codesign-certs`) and set `MOXASERIAL_PKG_SIGN_ID` from a
repository secret; the `macos` job needs no other change.

---

## The Windows installer

`MoxaSerial.iss` is an Inno Setup 6 script. The important directives:

* `PrivilegesRequired=lowest` — Setup never elevates, and the uninstall entry
  is registered under `HKCU`. `PrivilegesRequiredOverridesAllowed` is left
  empty, so nothing can talk it into elevating.
* `DefaultDirName={userappdata}\Autodesk\Autodesk Fusion 360\API\AddIns\MoxaSerial`
  with `DisableDirPage=yes` — Fusion finds add-ins by folder name, so there is
  nothing useful to choose.
* `SourceDir=..\..` — every `Source:` path, plus `LicenseFile` and `OutputDir`,
  is then relative to the repository root.
* `AppId` is a fixed GUID. **Never change it**; it is what ties an upgrade to
  the previous install.
* `[Code] CurStepChanged` renames an existing install to
  `MoxaSerial.bak-<timestamp>` at `ssInstall`, for the same two reasons the
  macOS preinstall does.
* `[UninstallDelete]` removes the whole `{app}` folder, because Fusion writes
  `__pycache__` folders inside it at runtime that Setup did not install and
  would otherwise leave behind.

The version must be passed in:

```
iscc /DMyAppVersion=0.1.0 installers\windows\MoxaSerial.iss
```

The script `#error`s without it rather than quietly building `0.0.0`.

Line continuations are deliberately **not** used in `[Files]`: backslash line
spanning is an ISPP preprocessor feature and is only documented for
preprocessor directives, so every entry is on one line.

### `install.ps1`, the no-Inno fallback

Does exactly what `install.py` does, for someone with a zip or a clone but no
Python on `PATH`. Same backup semantics, same reparse-point (junction/symlink)
handling, `-Uninstall`, `-DryRun` and `-Dest`. Written for Windows PowerShell
5.1, which is what ships with Windows.

### Signing (not done yet)

Unsigned, so SmartScreen warns (*More info → Run anyway*). To sign, add a
signtool definition on the ISCC command line and uncomment `SignTool=byparam`
and `SignedUninstaller=yes` in `[Setup]`:

```
iscc /DMyAppVersion=0.1.0 ^
     "/Sbyparam=signtool.exe sign /f cert.pfx /p $p /tr http://timestamp.digicert.com /td sha256 /fd sha256 $f" ^
     installers\windows\MoxaSerial.iss
```

An EV or OV code-signing certificate is what actually clears SmartScreen; a
self-signed certificate does not.

---

## What CI does

### `.github/workflows/ci.yml` — every push and PR to `main`

* **test**: Ubuntu, Python 3.12, `ruff check .`, `pytest -q`, then
  `make_release_zip.py --check-manifest` and a dry-run `install.py`.
* **installers**: Ubuntu + macOS + Windows. Installs with `install.py` into a
  scratch folder, runs it a second time to prove it backs up rather than
  merges, then uninstalls. On Windows it also parses `install.ps1` (a parse
  error is the one failure we cannot reproduce anywhere else) and runs it. On
  macOS it builds the `.pkg` and installs it with `installer -target
  CurrentUserHomeDirectory`, asserting the files land in `$HOME`.

### `.github/workflows/release.yml` — on a `v*` tag

1. **version** — reads `MoxaSerial.manifest` and fails immediately if the tag
   does not match it. This is the guard against a `v0.2.0` tag shipping a
   `0.1.9` manifest.
2. **test** — ruff + pytest, as above.
3. **zip** (Ubuntu) — builds the archive, unzips it, installs from it, and
   fails if any `__pycache__`/`.pyc` got in.
4. **macos** — builds the `.pkg` and installs it into the runner's home,
   asserting nothing appeared under `/Library`.
5. **windows** — `choco install innosetup`, runs `ISCC` with the version,
   then installs the result `/VERYSILENT` and checks `%APPDATA%`.
6. **release** — downloads all artifacts, writes `SHA256SUMS`, and publishes a
   GitHub Release with `softprops/action-gh-release`. The body is the top
   entry of `CHANGELOG.md` if there is one, followed by a generated install
   table and checksum instructions.

Only the release job has `contents: write`.

To rehearse without publishing, run the workflow manually
(**Actions → Release → Run workflow**): every build and verification step runs,
the release job is skipped.

---

## Cutting a release

```bash
python3 tools/bump_version.py 0.2.0
$EDITOR CHANGELOG.md                 # optional; becomes the release body
git add -A && git commit -m "release: v0.2.0"
git tag -a v0.2.0 -m "MoxaSerial v0.2.0"
git push origin HEAD --follow-tags
```

Then watch the **Release** workflow.
