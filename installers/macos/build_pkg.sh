#!/bin/bash
#
# Build dist/MoxaSerial-<version>.pkg -- a macOS installer that drops the
# add-in into the *current user's* Fusion AddIns folder:
#
#   ~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/MoxaSerial
#
# How it installs without admin rights
# ------------------------------------
# The component package is built with a *relative* --install-location, which
# makes it a home-relative package, and the Distribution restricts the install
# to the "current user home" domain:
#
#     <domains enable_anywhere="false"
#              enable_currentUserHome="true"
#              enable_localSystem="false"/>
#
# Installer.app then installs into $HOME with no password prompt, and
# `installer -pkg ... -target CurrentUserHomeDirectory` does the same from the
# command line without sudo. Nothing is ever written to /Library.
#
# Usage:
#   installers/macos/build_pkg.sh                 # version from the manifest
#   installers/macos/build_pkg.sh --version 1.2.3 # override
#   installers/macos/build_pkg.sh --out-dir build
#
# Signing (unsigned is fine for internal use; macOS will warn on download):
#   export MOXASERIAL_PKG_SIGN_ID="Developer ID Installer: Your Name (TEAMID)"
#   installers/macos/build_pkg.sh
# and to notarise afterwards:
#   xcrun notarytool submit dist/MoxaSerial-<v>.pkg \
#       --keychain-profile AC_PASSWORD --wait
#   xcrun stapler staple dist/MoxaSerial-<v>.pkg
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

IDENTIFIER="com.p3d.moxaserial"
ADDIN_NAME="MoxaSerial"
# Relative on purpose: this is what makes the package home-relative.
INSTALL_LOCATION="Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns"

VERSION=""
OUT_DIR="$REPO_ROOT/dist"

# Any Python 3.8+ will do; it is only used to read the manifest and copy the
# file list. Prefer whatever is on PATH (CI installs its own), fall back to the
# system one that every macOS with the command line tools has.
PYTHON="${PYTHON:-$(command -v python3 || true)}"
[ -x "$PYTHON" ] || PYTHON=/usr/bin/python3
[ -x "$PYTHON" ] || { echo "error: no python3 found" >&2; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --out-dir) OUT_DIR="$2"; shift 2 ;;
        -h|--help) sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ -z "$VERSION" ]; then
    VERSION="$("$PYTHON" "$REPO_ROOT/tools/make_release_zip.py" --print-version)"
fi
[ -n "$VERSION" ] || { echo "error: could not determine version" >&2; exit 2; }

echo "==> MoxaSerial $VERSION"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/moxaserial-pkg.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/root"
RESOURCES="$WORK/resources"
mkdir -p "$STAGE/$ADDIN_NAME" "$RESOURCES"

# ---------------------------------------------------------------------------
# Stage exactly the files installers/payload.py says ship, and nothing else.
# ---------------------------------------------------------------------------
echo "==> staging payload"
"$PYTHON" - "$REPO_ROOT" "$STAGE/$ADDIN_NAME" <<'PY'
import shutil, sys
from pathlib import Path

repo, stage = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(repo / "installers"))
import payload

for src, rel in payload.iter_payload(repo):
    dst = stage / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
print(f"    {len(payload.iter_payload(repo))} files")
PY

# Normalise permissions so the built pkg is reproducible regardless of umask.
/usr/bin/find "$STAGE" -type d -exec chmod 755 {} +
/usr/bin/find "$STAGE" -type f -exec chmod 644 {} +

# ---------------------------------------------------------------------------
# Component package
# ---------------------------------------------------------------------------
COMPONENT="$WORK/$ADDIN_NAME-component.pkg"
echo "==> pkgbuild"
/usr/bin/pkgbuild \
    --root "$STAGE" \
    --identifier "$IDENTIFIER" \
    --version "$VERSION" \
    --install-location "$INSTALL_LOCATION" \
    --scripts "$SCRIPT_DIR/scripts" \
    --ownership recommended \
    "$COMPONENT"

# ---------------------------------------------------------------------------
# Distribution wrapper: title, welcome/conclusion text, license, domain lock
# ---------------------------------------------------------------------------
echo "==> productbuild"
/usr/bin/sed "s/@VERSION@/$VERSION/g; s|@COMPONENT@|$ADDIN_NAME-component.pkg|g; s/@IDENTIFIER@/$IDENTIFIER/g" \
    "$SCRIPT_DIR/distribution.xml.in" > "$WORK/distribution.xml"

/usr/bin/sed "s/@VERSION@/$VERSION/g" "$SCRIPT_DIR/welcome.html" > "$RESOURCES/welcome.html"
/usr/bin/sed "s/@VERSION@/$VERSION/g" "$SCRIPT_DIR/conclusion.html" > "$RESOURCES/conclusion.html"
cp "$REPO_ROOT/LICENSE" "$RESOURCES/LICENSE.txt"

mkdir -p "$OUT_DIR"
OUT_PKG="$OUT_DIR/$ADDIN_NAME-$VERSION.pkg"
rm -f "$OUT_PKG"

SIGN_ARGS=()
if [ -n "${MOXASERIAL_PKG_SIGN_ID:-}" ]; then
    echo "    signing as: $MOXASERIAL_PKG_SIGN_ID"
    SIGN_ARGS=(--sign "$MOXASERIAL_PKG_SIGN_ID")
else
    echo "    (unsigned -- set MOXASERIAL_PKG_SIGN_ID to sign)"
fi

/usr/bin/productbuild \
    --distribution "$WORK/distribution.xml" \
    --package-path "$WORK" \
    --resources "$RESOURCES" \
    "${SIGN_ARGS[@]+"${SIGN_ARGS[@]}"}" \
    "$OUT_PKG"

echo
echo "==> $OUT_PKG"
/usr/bin/shasum -a 256 "$OUT_PKG"
echo
echo "Install without sudo:"
echo "    installer -pkg \"$OUT_PKG\" -target CurrentUserHomeDirectory"
echo "or just double-click it."
