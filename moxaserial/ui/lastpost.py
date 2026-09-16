"""Find the most recently posted NC file.

How "most recent posted program" is determined
----------------------------------------------
Three strategies, tried in order; the first that yields an existing file
on disk wins, and the winner is reported in the result's ``source`` so
the UI can say where the path came from.

1. **NC Programs in the active document** (preferred, verified live).
   ``cam.ncPrograms`` holds the document's persistent post deliverables.
   Each ``NCProgram.parameters`` carries, as confirmed against a running
   Fusion:

   ===============================  ================================
   ``nc_program_output_folder``     ``/path/to/nc/output``
   ``nc_program_filename``          ``1001`` (base name, no ext)
   ``nc_program_nc_extension``      ``.nc``
   ===============================  ================================

   (parameter values are read via ``param.value.value``.)

   The expected output path is therefore
   ``output_folder / (filename + nc_extension)``. Every NC program in the
   document is resolved this way and the one with the newest **file
   mtime** wins. This is what actually answers "what did I just post?" -
   the program that was posted most recently is the one whose file on
   disk is newest, regardless of browser order.

   ``nc_program_default_output_folder`` is used when the per-program
   folder is blank.

2. **Folder scan.** Every folder discovered in step 1 - plus any extra
   folders the user listed in settings under ``watch_folders`` - is
   scanned for the newest file with an NC-ish extension. This catches
   posts made by a different program name, posts whose parameters were
   edited afterwards, and files dropped there by other tooling.

3. **Fusion's default NC output folder** (``~/Documents/Fusion 360/NC
   Programs`` on macOS / ``%USERPROFILE%\\Documents\\...`` on Windows),
   scanned the same way, as a last resort.

Any Fusion API access is wrapped: with no Fusion (tests, dev server) the
module falls back to strategies 2 and 3 over the configured watch folders
alone, and returns an explanatory ``error`` if nothing is found.

UNVERIFIED: whether ``nc_program_nc_extension`` is always present on
older documents, and whether a post that fails still leaves the previous
file in place (a ``.failed`` stub is written next to it - those are
filtered out by extension here).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from moxaserial.log import get_logger

log = get_logger("lastpost")

#: Extensions treated as posted NC output when scanning a folder.
NC_EXTENSIONS = (
    ".nc", ".ncf", ".gcode", ".g", ".tap", ".cnc", ".eia", ".mpf", ".h", ".txt",
    ".min", ".iso", ".prg", ".anc", ".fnc", ".din", ".ptp", ".pim",
)

#: Never offered as "the last posted program".
IGNORED_SUFFIXES = (".failed", ".log", ".tmp", ".bak", ".dnc")

_P_FOLDER = "nc_program_output_folder"
_P_DEFAULT_FOLDER = "nc_program_default_output_folder"
_P_FILENAME = "nc_program_filename"
_P_EXTENSION = "nc_program_nc_extension"
_P_NAME = "nc_program_name"


# --------------------------------------------------------------------------
# Fusion-free helpers (unit-testable)
# --------------------------------------------------------------------------

def is_nc_file(path: Path) -> bool:
    name = path.name.lower()
    if name.startswith(".") or any(name.endswith(s) for s in IGNORED_SUFFIXES):
        return False
    return path.suffix.lower() in NC_EXTENSIONS


def newest_in_folders(folders: list[str], limit: int = 10) -> list[dict[str, Any]]:
    """Newest NC-ish files across *folders*, newest first."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for folder in folders:
        if not folder:
            continue
        d = Path(os.path.expanduser(folder))
        if not d.is_dir():
            continue
        try:
            entries = list(d.iterdir())
        except OSError as exc:
            log.debug("Cannot scan %s: %s", d, exc)
            continue
        for p in entries:
            try:
                if not p.is_file() or not is_nc_file(p):
                    continue
                key = str(p.resolve())
                if key in seen:
                    continue
                seen.add(key)
                found.append(_describe(p, source="folder-scan"))
            except OSError:
                continue
    found.sort(key=lambda f: f["mtime"], reverse=True)
    return found[:limit]


def fusion_default_nc_folder() -> str:
    """Fusion's out-of-the-box NC output folder for this platform."""
    return str(Path.home() / "Documents" / "Fusion 360" / "NC Programs")


def _describe(path: Path, source: str, program: str = "") -> dict[str, Any]:
    st = path.stat()
    return {
        "path": str(path),
        "name": path.name,
        "dir": str(path.parent),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "mtimeText": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
        "source": source,
        "program": program,
    }


# --------------------------------------------------------------------------
# Strategy 1 - the active document's NC programs
# --------------------------------------------------------------------------

def nc_program_candidates() -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve every NC program in the active document to its output file.

    Returns ``(existing_files, folders_seen)``. Both are empty outside
    Fusion.
    """
    files: list[dict[str, Any]] = []
    folders: list[str] = []
    try:
        import adsk.cam  # type: ignore
        import adsk.core  # type: ignore
    except ImportError:
        return files, folders

    try:
        app = adsk.core.Application.get()
        doc = app.activeDocument if app else None
        if doc is None:
            return files, folders
        cam = adsk.cam.CAM.cast(doc.products.itemByProductType("CAMProductType"))
        if cam is None:
            return files, folders
        programs = cam.ncPrograms
    except Exception as exc:  # noqa: BLE001
        log.debug("No CAM product available: %s", exc)
        return files, folders

    for i in range(programs.count):
        try:
            prog = programs.item(i)
            params = prog.parameters
            folder = _param(params, _P_FOLDER) or _param(params, _P_DEFAULT_FOLDER)
            filename = _param(params, _P_FILENAME)
            ext = _param(params, _P_EXTENSION) or ".nc"
            label = _param(params, _P_NAME) or filename
            if folder:
                folders.append(folder)
            if not folder or not filename:
                continue
            if not ext.startswith("."):
                ext = "." + ext
            candidate = Path(os.path.expanduser(folder)) / f"{filename}{ext}"
            if candidate.is_file():
                files.append(_describe(candidate, source="nc-program", program=str(label)))
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not resolve NC program %d: %s", i, exc)
            continue

    files.sort(key=lambda f: f["mtime"], reverse=True)
    return files, folders


def _param(params: Any, name: str) -> str:
    """Read a CAMParameter value as a string; "" when absent."""
    try:
        p = params.itemByName(name)
        if p is None:
            return ""
        value = p.value.value  # verified live: CAMParameter.value.value
        return "" if value is None else str(value)
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def find_last_posted(extra_folders: list[str] | None = None) -> dict[str, Any]:
    """Best guess at the most recently posted NC file.

    ``{"path", "name", "source", "candidates": [...], "error"?}``
    """
    extra = list(extra_folders or [])
    nc_files, nc_folders = nc_program_candidates()

    scan_folders = [*nc_folders, *extra, fusion_default_nc_folder()]
    scanned = newest_in_folders(scan_folders)

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*nc_files, *scanned]:
        if item["path"] in seen:
            continue
        seen.add(item["path"])
        merged.append(item)
    merged.sort(key=lambda f: f["mtime"], reverse=True)

    if not merged:
        where = ", ".join(dict.fromkeys(f for f in scan_folders if f)) or "(no folders known)"
        return {
            "path": "",
            "name": "",
            "source": "none",
            "candidates": [],
            "error": (
                "No posted NC file found. Post a program first, or add its output "
                f"folder under Machines -> watch folders. Looked in: {where}"
            ),
        }

    best = merged[0]
    log.debug("Last posted resolved to %s via %s", best["path"], best["source"])
    return {**best, "candidates": merged[:10]}
