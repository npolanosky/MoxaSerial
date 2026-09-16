"""The single, authoritative list of files that ship in a MoxaSerial release.

Everything that packages the add-in -- ``tools/make_release_zip.py``, the root
``install.py``, ``installers/macos/build_pkg.sh`` (via the zip builder) and the
Windows ``install.ps1`` -- resolves what to copy through this module. Change the
include list here and every installer follows.

Only the Python standard library is used, and only modules that ship with the
CPython 3.12 that Fusion embeds, so this file can also be imported from inside
an unzipped release.

.. note::
   ``tools/public_manifest.txt`` (maintained separately, for the public source
   drop) covers the same set of runtime files. The two are intentionally
   independent, but they must agree -- if you add a runtime file, add it to
   both. ``verify_against_manifest()`` below will diff them for you when that
   file exists.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

#: Folder name Fusion must see under ``API/AddIns``. Fusion matches the folder
#: name against the manifest/script name, so this is not a free choice.
ADDIN_NAME = "MoxaSerial"

#: Individual files copied from the repo root, verbatim.
INCLUDE_FILES: tuple[str, ...] = (
    "MoxaSerial.py",
    "MoxaSerial.manifest",
    "moxaserial_loader.py",
    "LICENSE",
    "README.md",
)

#: Directories copied whole, minus :data:`EXCLUDE_DIR_NAMES` /
#: :data:`EXCLUDE_SUFFIXES` / :data:`EXCLUDE_FILE_NAMES`.
INCLUDE_TREES: tuple[str, ...] = (
    "moxaserial",
    "resources",
)

#: Directory names pruned anywhere inside an included tree.
EXCLUDE_DIR_NAMES: frozenset[str] = frozenset(
    {"__pycache__", ".git", ".pytest_cache", ".ruff_cache", ".devdata", ".venv", "venv"}
)

#: File suffixes never shipped.
EXCLUDE_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo", ".pyd", ".log", ".orig", ".rej")

#: Exact file names never shipped.
EXCLUDE_FILE_NAMES: frozenset[str] = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})

#: Paths that must exist in the payload for it to be usable by Fusion.
REQUIRED: tuple[str, ...] = (
    "MoxaSerial.py",
    "MoxaSerial.manifest",
    "moxaserial_loader.py",
    "moxaserial/__init__.py",
    "moxaserial/ui/app.py",
    "resources/palette/index.html",
)


class PayloadError(RuntimeError):
    """Raised when the repo does not contain a complete, shippable payload."""


def _excluded(name: str) -> bool:
    return name in EXCLUDE_FILE_NAMES or name.endswith(EXCLUDE_SUFFIXES)


def iter_payload(root: str | os.PathLike[str]) -> list[tuple[Path, str]]:
    """Return ``[(absolute source path, posix relative path), ...]``, sorted.

    *root* is the repository root (or any directory laid out like one). The
    relative paths are what goes inside the ``MoxaSerial/`` folder, using ``/``
    separators on every platform so archives are identical everywhere.
    """
    root = Path(root).resolve()
    out: list[tuple[Path, str]] = []

    for name in INCLUDE_FILES:
        src = root / name
        if not src.is_file():
            raise PayloadError(f"missing payload file: {name} (looked in {root})")
        out.append((src, name))

    for tree in INCLUDE_TREES:
        base = root / tree
        if not base.is_dir():
            raise PayloadError(f"missing payload directory: {tree} (looked in {root})")
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIR_NAMES)
            for fn in sorted(filenames):
                if _excluded(fn):
                    continue
                src = Path(dirpath) / fn
                out.append((src, src.relative_to(root).as_posix()))

    out.sort(key=lambda pair: pair[1])

    have = {rel for _, rel in out}
    missing = [r for r in REQUIRED if r not in have]
    if missing:
        raise PayloadError("payload is incomplete, missing: " + ", ".join(missing))
    return out


_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([.-][0-9A-Za-z.]+)?$")


def read_version(root: str | os.PathLike[str]) -> str:
    """Read the version from ``MoxaSerial.manifest`` -- the single source of truth."""
    manifest = Path(root) / "MoxaSerial.manifest"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PayloadError(f"no manifest at {manifest}") from None
    except json.JSONDecodeError as exc:
        raise PayloadError(f"{manifest} is not valid JSON: {exc}") from None

    version = str(data.get("version", "")).strip()
    if not version:
        raise PayloadError(f"{manifest} has no 'version'")
    if not _VERSION_RE.match(version):
        raise PayloadError(f"{manifest} version {version!r} is not x.y.z")
    return version


def verify_against_manifest(root: str | os.PathLike[str]) -> list[str]:
    """Check that every shipped file is a *public* file.

    ``tools/public_manifest.txt`` lists the repo's public globs (and
    ``!excluded`` globs). The payload is the runtime subset of the public
    tree, so the check is one-directional: a shipped file that no public
    glob covers, or that an exclusion covers, is a leak. Returns
    human-readable problems (empty when fine or when the manifest is absent).
    """
    import fnmatch

    listing = Path(root) / "tools" / "public_manifest.txt"
    if not listing.is_file():
        return []
    public: list[str] = []
    private: list[str] = []
    for raw in listing.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("!"):
            private.append(line[1:].lstrip("./"))
        else:
            public.append(line.lstrip("./"))

    def matches(rel: str, patterns: list[str]) -> bool:
        for pat in patterns:
            variants = {pat, pat.replace("/**/", "/"), pat.replace("**/", "")}
            for v in variants:
                if fnmatch.fnmatch(rel, v) or rel == v.rstrip("/") or rel.startswith(v.rstrip("*") + "/") and v.endswith("/**"):
                    return True
        return False

    diffs: list[str] = []
    for _, rel in iter_payload(root):
        if matches(rel, private):
            diffs.append(f"shipped file is excluded by public_manifest.txt: {rel}")
        elif not matches(rel, public):
            diffs.append(f"shipped file is not covered by public_manifest.txt: {rel}")
    return diffs


if __name__ == "__main__":  # pragma: no cover - convenience for eyeballing
    here = Path(__file__).resolve().parent.parent
    print(f"MoxaSerial {read_version(here)} payload from {here}:")
    for _src, rel in iter_payload(here):
        print("  " + rel)
    for diff in verify_against_manifest(here):
        print("  ! " + diff)
