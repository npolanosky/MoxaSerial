"""Settings export / import between installs (GitHub issue #1).

An export is a small JSON envelope::

    {
      "format": "moxaserial-settings",
      "format_version": 1,
      "app_version": "0.2.1",
      "schema_version": 3,
      "exported_at": "2026-09-28T14:03:11",
      "platform": "darwin" | "win32" | "linux",
      "home": "<the exporting user's home folder>",
      "scope": "all" | "machine",
      "settings": { ...whole settings document... }      (scope "all")
      "machine":  { ...one machine... }                  (scope "machine")
    }

Credentials never appear here: they live in the OS keychain (``secrets.py``)
and are keyed by machine id, so a machine re-imported with the same id picks
its stored credentials back up on the same computer.

Paths are the one thing that does not travel. ``receive.folder``,
``send.default_folder`` and the global ``watch_folders`` are rewritten on
import:

* a path under the *exporting* user's home is re-homed under the importing
  user's home (``<their home>\\Documents\\NC`` -> ``<your home>/Documents/NC``),
  which covers the common case of the same layout on another computer;
* any other path in the other operating system's syntax is dropped in favour
  of a safe default (the receive folder) or removed (a watch or default send
  folder), and the report says so;
* a same-platform path outside the home is kept verbatim - it may well be a
  network share that exists on both machines.

``serial_device`` (``COM3`` vs ``/dev/cu.usbserial-…``) cannot be mapped at
all; it is cleared when it comes from the other platform.

``update.repo`` and ``update.auto_install`` are never imported (see
:func:`apply_import`): a settings file is not allowed to redirect the updater.

Pure functions; the bridge does the file I/O and the dialogs.
"""

from __future__ import annotations

import copy
import datetime as _dt
import os
import re
import sys
from pathlib import Path
from typing import Any

from moxaserial import __version__, paths
from moxaserial.config import (
    SCHEMA_VERSION,
    ValidationError,
    migrate,
    normalize_machine,
    normalize_settings,
    validate_machine,
)
from moxaserial.log import get_logger

log = get_logger("portable")

FORMAT = "moxaserial-settings"
FORMAT_VERSION = 1
SCOPES = ("all", "machine")
IMPORT_MODES = ("merge", "replace")

_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
# Both spellings: Fusion's dialog can hand back ``//server/share/...``.
_WIN_UNC = re.compile(r"^(?:\\\\[^\\]+\\|//[^/]+/)")
_COM_PORT = re.compile(r"^(\\\\\.\\)?COM\d+$", re.IGNORECASE)


# --------------------------------------------------------------------------
# Platform / path helpers
# --------------------------------------------------------------------------
def current_platform() -> str:
    if os.name == "nt":
        return "win32"
    return "darwin" if sys.platform == "darwin" else "linux"


def _is_windows_platform(platform: str) -> bool:
    return str(platform).lower().startswith("win")


def path_style(path: str) -> str:
    """``"windows"``, ``"posix"``, ``"home"`` (``~``-relative) or ``"relative"``."""
    p = str(path or "").strip()
    if not p:
        return "relative"
    if p.startswith("~"):
        return "home"
    if _WIN_DRIVE.match(p) or _WIN_UNC.match(p):
        return "windows"
    if p.startswith("/"):
        return "posix"
    return "relative"


def _source_is_windows(source_platform: str, source_home: str = "") -> bool:
    """Windows-ness of the exporting computer; the home path decides when
    the envelope does not say (a hand-edited file)."""
    plat = str(source_platform or "").lower()
    if plat.startswith("win"):
        return True
    if plat in ("darwin", "linux") or plat.startswith(("mac", "freebsd")):
        return False
    return path_style(source_home) == "windows"


def _folds_case(source_platform: str, source_windows: bool) -> bool:
    # NTFS and APFS/HFS+ are case-insensitive by default; Linux is not.
    return source_windows or str(source_platform or "").lower().startswith(("darwin", "mac"))


def _norm_for_compare(path: str, windows: bool, fold: bool) -> str:
    p = str(path).replace("\\", "/") if windows else str(path)
    p = p.rstrip("/")
    return p.lower() if fold else p


def rehome(
    path: str, source_home: str, source_windows: bool, fold_case: bool | None = None
) -> str | None:
    """*path* re-rooted under this user's home if it was under *source_home*.

    Returns ``None`` when the path is not under the source home. Compared
    case-insensitively for Windows (and, via *fold_case*, macOS) homes.
    """
    if not path or not source_home:
        return None
    fold = source_windows if fold_case is None else fold_case
    src = _norm_for_compare(source_home, source_windows, fold)
    cand = _norm_for_compare(path, source_windows, fold)
    if not src or not (cand == src or cand.startswith(src + "/")):
        return None
    rel = str(path).replace("\\", "/") if source_windows else str(path)
    rel = rel[len(str(source_home).rstrip("\\/")):].lstrip("\\/")
    parts = [seg for seg in rel.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        return None
    return str(Path.home().joinpath(*parts)) if parts else str(Path.home())


def convert_path(
    path: str,
    source_platform: str,
    source_home: str,
    *,
    fallback: str,
    label: str,
    notes: list[str],
) -> str:
    """One folder path from the export, made safe for this computer."""
    p = str(path or "").strip()
    if not p:
        return p
    src_win = _source_is_windows(source_platform, source_home)
    here_win = _is_windows_platform(current_platform())
    style = path_style(p)

    if style in ("home", "relative"):
        # Expands the same everywhere; leave it to whoever uses it.
        return p

    rehomed = rehome(p, source_home, src_win, _folds_case(source_platform, src_win))
    if rehomed is not None:
        if rehomed != p:
            notes.append(f"{label}: '{p}' moved under this user's home -> '{rehomed}'.")
        return rehomed

    foreign = (style == "windows") != here_win
    if not foreign:
        return p  # same OS, outside the home: a share, a second drive - keep it
    if fallback:
        notes.append(f"{label}: '{p}' is a {style} path from another computer; using '{fallback}'.")
    else:
        notes.append(f"{label}: '{p}' is a {style} path from another computer; removed.")
    return fallback


def convert_serial_device(device: str, source_platform: str, notes: list[str], label: str) -> str:
    d = str(device or "").strip()
    if not d:
        return d
    here_win = _is_windows_platform(current_platform())
    if _COM_PORT.match(d):
        foreign = not here_win
    elif d.startswith("/dev/"):
        foreign = here_win
    else:
        foreign = _source_is_windows(source_platform) != here_win
    if foreign:
        notes.append(f"{label}: serial port '{d}' belongs to another computer; choose one here.")
        return ""
    return d


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------
def _envelope(scope: str) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "app_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "exported_at": _dt.datetime.now().replace(microsecond=0).isoformat(),
        "platform": current_platform(),
        "home": str(Path.home()),
        "scope": scope,
    }


def export_all(settings: dict[str, Any]) -> dict[str, Any]:
    """The whole settings document, minus per-install bookkeeping."""
    data = copy.deepcopy(settings)
    upd = data.get("update")
    if isinstance(upd, dict):
        for key in ("last_check", "last_seen_version", "last_error"):
            upd.pop(key, None)
    env = _envelope("all")
    env["settings"] = data
    return env


def export_machine(machine: dict[str, Any]) -> dict[str, Any]:
    env = _envelope("machine")
    env["machine"] = copy.deepcopy(machine)
    return env


def suggested_file_name(scope: str, machine: dict[str, Any] | None = None) -> str:
    stamp = _dt.date.today().isoformat()
    if scope == "machine" and machine:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(machine.get("name", "machine"))).strip("_") or "machine"
        return f"MoxaSerial-{safe}-{stamp}.json"
    return f"MoxaSerial-settings-{stamp}.json"


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------
class ImportReport:
    """What an import did (or, for a dry run, would do)."""

    def __init__(self) -> None:
        self.scope = ""
        self.source_platform = ""
        self.source_app_version = ""
        self.exported_at = ""
        self.added: list[str] = []
        self.updated: list[str] = []
        self.notes: list[str] = []
        self.warnings: list[str] = []
        self.globals_replaced = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "sourcePlatform": self.source_platform,
            "sourceAppVersion": self.source_app_version,
            "exportedAt": self.exported_at,
            "added": list(self.added),
            "updated": list(self.updated),
            "notes": list(self.notes),
            "warnings": list(self.warnings),
            "globalsReplaced": self.globals_replaced,
        }


def parse_envelope(raw: Any) -> dict[str, Any]:
    """Validate the outer shape. Raises :class:`ValidationError`."""
    if not isinstance(raw, dict):
        raise ValidationError("Not a MoxaSerial settings file (expected a JSON object).")
    if raw.get("format") != FORMAT:
        raise ValidationError("Not a MoxaSerial settings file (missing format marker).")
    try:
        fmt = int(raw.get("format_version", 0))
    except (TypeError, ValueError):
        fmt = 0
    if fmt > FORMAT_VERSION:
        raise ValidationError(
            f"This file was written by a newer MoxaSerial (format {fmt}); update first."
        )
    scope = str(raw.get("scope", ""))
    if scope not in SCOPES:
        raise ValidationError(f"Unknown export scope '{scope}'.")
    body = raw.get("settings") if scope == "all" else raw.get("machine")
    if not isinstance(body, dict):
        raise ValidationError("The file has no settings in it.")
    return raw


def _convert_machine(
    machine: dict[str, Any], platform: str, home: str, report: ImportReport
) -> dict[str, Any]:
    m = normalize_machine(machine)
    label = f"Machine '{m['name']}'"
    m["receive"]["folder"] = convert_path(
        m["receive"].get("folder", ""), platform, home,
        fallback=str(paths.default_receive_dir()), label=f"{label} receive folder",
        notes=report.notes,
    ) or str(paths.default_receive_dir())
    m["send"]["default_folder"] = convert_path(
        m["send"].get("default_folder", ""), platform, home,
        fallback="", label=f"{label} default send folder", notes=report.notes,
    )
    m["serial_device"] = convert_serial_device(
        m.get("serial_device", ""), platform, report.notes, label
    )
    for problem in validate_machine(m):
        report.warnings.append(f"{label}: {problem}")
    return m


def _machine_names(machines: list[dict[str, Any]]) -> set[str]:
    return {str(m.get("name", "")) for m in machines}


def _merge_machine(
    target: list[dict[str, Any]], incoming: dict[str, Any], report: ImportReport
) -> None:
    for i, existing in enumerate(target):
        if existing["id"] == incoming["id"]:
            target[i] = incoming
            report.updated.append(incoming["name"])
            return
    # Same name, different id: this is a second copy, not the same machine.
    names = _machine_names(target)
    if incoming["name"] in names:
        base = incoming["name"]
        n = 2
        while f"{base} ({n})" in names:
            n += 1
        incoming["name"] = f"{base} ({n})"
        report.notes.append(f"A machine named '{base}' already exists; imported as '{incoming['name']}'.")
    target.append(incoming)
    report.added.append(incoming["name"])


def apply_import(
    current: dict[str, Any], envelope: dict[str, Any], mode: str = "merge"
) -> tuple[dict[str, Any], ImportReport]:
    """Return ``(new_settings, report)``. Does not touch disk.

    *mode* ``merge`` (default) adds or updates machines by id and copies the
    global options over; ``replace`` throws the current machines away first.
    A single-machine file always merges.
    """
    env = parse_envelope(envelope)
    if mode not in IMPORT_MODES:
        raise ValidationError(f"Unknown import mode '{mode}'.")
    report = ImportReport()
    report.scope = str(env["scope"])
    report.source_platform = str(env.get("platform", "") or "unknown")
    report.source_app_version = str(env.get("app_version", "") or "")
    report.exported_at = str(env.get("exported_at", "") or "")
    home = str(env.get("home", "") or "")
    platform = report.source_platform

    out = copy.deepcopy(current)
    machines: list[dict[str, Any]] = list(out.get("machines", []))

    if report.scope == "machine":
        incoming = _convert_machine(env["machine"], platform, home, report)
        _merge_machine(machines, incoming, report)
        out["machines"] = machines
        return normalize_settings(out), report

    # scope == "all": bring the file up to this schema first.
    src, notes = migrate(dict(env["settings"]))
    report.notes.extend(notes)
    src = normalize_settings(src)

    if mode == "replace":
        machines = []
    for raw in src.get("machines", []):
        incoming = _convert_machine(raw, platform, home, report)
        if incoming["id"] == "simulator":
            # The built-in one is always present; its settings still travel.
            for i, existing in enumerate(machines):
                if existing["id"] == "simulator":
                    machines[i] = incoming
                    break
            else:
                machines.insert(0, incoming)
            continue
        _merge_machine(machines, incoming, report)
    out["machines"] = machines

    watch: list[str] = []
    for folder in src.get("watch_folders", []):
        conv = convert_path(
            folder, platform, home, fallback="", label="Watch folder", notes=report.notes
        )
        if conv and conv not in watch:
            watch.append(conv)
    if mode == "merge":
        for folder in out.get("watch_folders", []):
            if folder not in watch:
                watch.append(folder)
    out["watch_folders"] = watch

    for key in ("machine_selection", "log_level", "theme", "confirm_before_send"):
        if key in src:
            out[key] = src[key]
    # Deliberately NOT imported: ``update.repo`` and ``update.auto_install``.
    # A settings file from someone else must not be able to point the updater
    # at another GitHub repository with automatic install switched on - that
    # would be arbitrary code in Fusion on the next check. Those two stay as
    # they are on this computer.
    upd_in = src.get("update") or {}
    upd_out = dict(out.get("update") or {})
    for key in ("auto_check", "include_prereleases", "check_interval_hours"):
        if key in upd_in:
            upd_out[key] = upd_in[key]
    out["update"] = upd_out
    ids = {m["id"] for m in machines}
    for key in ("default_machine_id", "last_used_machine_id"):
        if src.get(key) in ids:
            out[key] = src[key]
    report.globals_replaced = True
    return normalize_settings(out), report


def summarize(envelope: dict[str, Any]) -> dict[str, Any]:
    """A cheap description of an export for the confirmation step."""
    env = parse_envelope(envelope)
    if env["scope"] == "machine":
        names = [str(env["machine"].get("name", "?"))]
    else:
        names = [
            str(m.get("name", "?"))
            for m in env["settings"].get("machines", [])
            if isinstance(m, dict) and m.get("id") != "simulator"
        ]
    return {
        "scope": env["scope"],
        "platform": str(env.get("platform", "") or "unknown"),
        "appVersion": str(env.get("app_version", "") or ""),
        "exportedAt": str(env.get("exported_at", "") or ""),
        "machines": names,
        "crossPlatform": _source_is_windows(str(env.get("platform", "")), str(env.get("home", "")))
        != _is_windows_platform(current_platform()),
    }


__all__ = [
    "FORMAT",
    "FORMAT_VERSION",
    "ImportReport",
    "apply_import",
    "convert_path",
    "convert_serial_device",
    "export_all",
    "export_machine",
    "parse_envelope",
    "path_style",
    "rehome",
    "suggested_file_name",
    "summarize",
]
