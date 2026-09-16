#!/usr/bin/env python3
"""Build ``dist/MoxaSerial-<version>.zip``, the portable release archive.

The archive holds

    MoxaSerial/          <- drop this whole folder into Fusion's AddIns folder
      MoxaSerial.py
      MoxaSerial.manifest
      moxaserial_loader.py
      moxaserial/ ...
      resources/ ...
      LICENSE  README.md
    install.py           <- optional: does the copy for you

The file list comes from :mod:`installers.payload`; the version comes from
``MoxaSerial.manifest``. The build is deterministic -- same inputs give a
byte-identical zip -- so release artifacts can be checksummed and compared:
entries are sorted, timestamps are pinned and modes are normalised, and no
``__pycache__`` ever gets in.

Usage::

    python3 tools/make_release_zip.py                 # -> dist/MoxaSerial-0.1.0.zip
    python3 tools/make_release_zip.py --out-dir build
    python3 tools/make_release_zip.py --print-version # just echo the version
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "installers"))

import payload  # noqa: E402  (needs the sys.path line above)

# Pinned mtime for every entry: 1 Jan 1980 is the earliest a zip can store.
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o644


def build_zip(root: Path, out_dir: Path, version: str | None = None) -> Path:
    """Write the release zip and return its path."""
    version = version or payload.read_version(root)
    files = payload.iter_payload(root)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{payload.ADDIN_NAME}-{version}.zip"
    if out_path.exists():
        out_path.unlink()

    entries: list[tuple[Path, str]] = [
        (src, f"{payload.ADDIN_NAME}/{rel}") for src, rel in files
    ]
    installer = root / "install.py"
    if installer.is_file():
        entries.append((installer, "install.py"))
    entries.sort(key=lambda pair: pair[1])

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for src, arcname in entries:
            info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (FILE_MODE & 0xFFFF) << 16
            info.create_system = 3  # Unix, so the mode above is honoured
            zf.writestr(info, src.read_bytes())

    return out_path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(REPO_ROOT), help="repository root (default: this checkout)")
    ap.add_argument("--out-dir", default=None, help="output directory (default: <root>/dist)")
    ap.add_argument("--print-version", action="store_true", help="print the manifest version and exit")
    ap.add_argument("--check-manifest", action="store_true",
                    help="also diff the include list against tools/public_manifest.txt")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    try:
        version = payload.read_version(root)
    except payload.PayloadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.print_version:
        print(version)
        return 0

    out_dir = Path(args.out_dir).resolve() if args.out_dir else root / "dist"
    try:
        out_path = build_zip(root, out_dir, version)
    except payload.PayloadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    with zipfile.ZipFile(out_path) as zf:
        count = len(zf.namelist())
    print(f"{out_path}  ({count} entries, {out_path.stat().st_size:,} bytes)")
    print(f"sha256  {sha256(out_path)}")

    if args.check_manifest:
        diffs = payload.verify_against_manifest(root)
        for d in diffs:
            print(f"warning: {d}", file=sys.stderr)
        if diffs:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
