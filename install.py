#!/usr/bin/env python3
"""Install MoxaSerial into Autodesk Fusion's per-user add-ins folder.

Run it either from an unzipped release (next to the ``MoxaSerial/`` folder) or
from a source checkout::

    python3 install.py                 # install / upgrade
    python3 install.py --uninstall     # remove it again
    python3 install.py --dest DIR      # install into DIR instead (testing)
    python3 install.py --dry-run       # say what would happen, touch nothing

Fusion looks for add-ins in a per-user folder, so nothing here needs admin
rights and nothing is written outside your home directory:

    macOS    ~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/
    Windows  %APPDATA%\\Autodesk\\Autodesk Fusion 360\\API\\AddIns\\

An existing MoxaSerial folder is moved aside to ``MoxaSerial.bak-<timestamp>``
before the new one is written, so a bad upgrade is always one rename away from
being undone.

Standard library only, Python 3.8+ (the Python shipped with macOS and any
python.org build will do; Fusion's own interpreter is not used).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

ADDIN_NAME = "MoxaSerial"
DEST_ENV = "MOXASERIAL_ADDINS_DIR"
HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# Where Fusion keeps add-ins
# --------------------------------------------------------------------------

def addins_dir_candidates() -> list[Path]:
    """Every plausible AddIns folder for this OS, best guess first.

    Autodesk has shipped two product folder names ("Autodesk Fusion 360" and,
    on newer installs, "Autodesk Fusion"). We prefer whichever already exists.
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Autodesk"
        products = ["Autodesk Fusion 360", "Autodesk Fusion"]
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) / "Autodesk" if appdata else Path.home() / "AppData" / "Roaming" / "Autodesk"
        products = ["Autodesk Fusion 360", "Autodesk Fusion"]
    else:
        # Fusion has no Linux build; keep a sane path so --dest-less runs on a
        # CI box still produce something testable rather than crashing.
        base = Path.home() / ".config" / "Autodesk"
        products = ["Autodesk Fusion 360"]

    return [base / p / "API" / "AddIns" for p in products]


def resolve_addins_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get(DEST_ENV)
    if env:
        return Path(env).expanduser().resolve()

    candidates = addins_dir_candidates()
    for cand in candidates:
        if cand.is_dir():
            return cand
    for cand in candidates:  # Fusion installed but never opened: parent exists
        if cand.parent.parent.is_dir():
            return cand
    return candidates[0]


# --------------------------------------------------------------------------
# Where the files we are about to copy live
# --------------------------------------------------------------------------

def find_source() -> tuple[Path, list[tuple[Path, str]]]:
    """Return ``(description root, [(src, rel), ...])`` for the payload.

    Two layouts are supported:

    * **Unzipped release** -- a ready-made ``MoxaSerial/`` folder sits next to
      this script. It was filtered when the zip was built, so it is copied
      as-is.
    * **Source checkout** -- this script sits in the repo root. The file list
      then comes from ``installers/payload.py``, the one place the shipping
      file list is defined.
    """
    staged = HERE / ADDIN_NAME
    if (staged / "MoxaSerial.manifest").is_file():
        files = []
        for path in sorted(staged.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.name != ".DS_Store":
                files.append((path, path.relative_to(staged).as_posix()))
        return staged, files

    if (HERE / "MoxaSerial.manifest").is_file():
        sys.path.insert(0, str(HERE / "installers"))
        try:
            import payload  # type: ignore
        except ImportError:
            die(
                "this looks like a source checkout but installers/payload.py is missing;\n"
                "       run tools/make_release_zip.py from a complete checkout, or use a release zip"
            )
        try:
            return HERE, payload.iter_payload(HERE)
        except payload.PayloadError as exc:
            die(str(exc))

    die(
        f"nothing to install: expected a '{ADDIN_NAME}/' folder next to {Path(__file__).name}\n"
        f"       (unzipped release) or a MoxaSerial.manifest in {HERE} (source checkout)"
    )
    raise AssertionError("unreachable")


def read_version(src_root: Path) -> str:
    import json

    try:
        return str(json.loads((src_root / "MoxaSerial.manifest").read_text(encoding="utf-8"))["version"])
    except Exception:
        return "?"


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(2)


def backup_existing(target: Path, dry_run: bool) -> Path | None:
    """Move any existing install aside; return where it went.

    ``is_symlink()`` matters as much as ``exists()`` here. Developers often
    symlink the AddIns entry at a live source checkout, and a broken symlink is
    invisible to ``exists()`` -- in both cases copying into ``target/...`` would
    write *through* the link into the linked tree, so the link is renamed first.
    """
    if not target.exists() and not target.is_symlink():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(f"{target.name}.bak-{stamp}")
    n = 1
    while backup.exists() or backup.is_symlink():
        n += 1
        backup = target.with_name(f"{target.name}.bak-{stamp}-{n}")
    if target.is_symlink():
        print(f"  {target.name} is a symlink to {os.readlink(target)}")
        print(f"  moving the symlink aside -> {backup.name}  (rename it back to undo)")
    else:
        print(f"  backing up existing install -> {backup.name}")
    if not dry_run:
        target.rename(backup)
    return backup


def install(addins: Path, dry_run: bool) -> int:
    src_root, files = find_source()
    version = read_version(src_root)
    target = addins / ADDIN_NAME

    print(f"MoxaSerial {version}")
    print(f"  source      {src_root}")
    print(f"  destination {target}")
    print(f"  files       {len(files)}")
    if dry_run:
        print("  (dry run -- nothing written)")

    if not addins.exists():
        print(f"  creating {addins}")
        if not dry_run:
            try:
                addins.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                die(f"cannot create {addins}: {exc}")

    backup = backup_existing(target, dry_run)

    if not dry_run:
        try:
            for src, rel in files:
                dst = target / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                shutil.copymode(src, dst)
        except OSError as exc:
            # Put the old install back rather than leaving a half-written one.
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if backup is not None and backup.exists():
                backup.rename(target)
                print("  restored the previous install", file=sys.stderr)
            die(f"copy failed: {exc}")

    print()
    print("Installed. Next, in Fusion:")
    print("  1. Utilities tab -> ADD-INS -> Scripts and Add-Ins  (or Shift+S)")
    print("  2. Add-Ins tab -> MoxaSerial -> Run")
    print("  3. Tick 'Run on Startup' so it comes back with Fusion")
    print()
    print("The buttons appear in the Manufacture workspace: a 'Moxa DNC' panel on the")
    print("P3DTools tab, plus 'Send Last Program' beside Post Process.")
    if backup is not None:
        print()
        print(f"Your previous install is at {backup} -- delete it once the new one works.")
    print()
    print("If Fusion was already running, restart it (or stop and re-run the add-in).")
    return 0


def uninstall(addins: Path, dry_run: bool, keep_backups: bool) -> int:
    target = addins / ADDIN_NAME
    removed = False

    if target.is_symlink():
        # Only the link goes; whatever it points at is someone's checkout.
        print(f"  removing symlink {target} -> {os.readlink(target)}")
        print("  (the linked folder itself is left alone)")
        if not dry_run:
            target.unlink()
        removed = True
    elif target.exists():
        print(f"  removing {target}")
        if not dry_run:
            shutil.rmtree(target)
        removed = True
    else:
        print(f"  nothing installed at {target}")

    if not keep_backups:
        for path in sorted(addins.glob(f"{ADDIN_NAME}.bak-*")):
            print(f"  removing backup {path.name}")
            if not dry_run:
                if path.is_symlink():
                    path.unlink()
                else:
                    shutil.rmtree(path, ignore_errors=True)
            removed = True

    if removed:
        print()
        print("Removed. Your settings and logs were NOT touched; they live in")
        if sys.platform == "darwin":
            print("  ~/Library/Application Support/MoxaSerial/")
        elif os.name == "nt":
            print("  %APPDATA%\\MoxaSerial\\")
        else:
            print("  ~/.config/MoxaSerial/")
        print("Delete that folder too if you want a completely clean slate.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="install.py",
        description="Install the MoxaSerial add-in into Autodesk Fusion's per-user AddIns folder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"The destination can also be set with ${DEST_ENV}.",
    )
    ap.add_argument("--dest", metavar="DIR", help="AddIns folder to install into (default: auto-detect)")
    ap.add_argument("--uninstall", action="store_true", help="remove an installed MoxaSerial")
    ap.add_argument("--keep-backups", action="store_true", help="with --uninstall, leave MoxaSerial.bak-* alone")
    ap.add_argument("--dry-run", action="store_true", help="print what would happen, change nothing")
    args = ap.parse_args(argv)

    # Not the add-in's own floor: this script runs on whatever Python the user
    # happens to have, not on Fusion's embedded 3.12, so keep the check.
    if sys.version_info < (3, 8):  # noqa: UP036
        die(f"Python 3.8 or newer required, this is {sys.version.split()[0]}")

    addins = resolve_addins_dir(args.dest)
    if args.uninstall:
        return uninstall(addins, args.dry_run, args.keep_backups)
    return install(addins, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
