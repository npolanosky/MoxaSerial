"""Auto-update: watch GitHub Releases and replace the add-in folder in place.

Deliberately Fusion-free, like everything else below ``moxaserial/ui/``: the
whole update path (check, download, SHA verify, extract, swap, rollback) is
exercised by the test suite in a plain CPython interpreter with a mocked
``urlopen`` and real zip files in ``tmp_path``. Only the *restart* needs
Fusion, and that lives in :mod:`moxaserial.ui.updater`.

Release layout produced by CI (one GitHub release per version)::

    MoxaSerial-<version>.zip          top-level MoxaSerial/ folder, runtime files
    MoxaSerial-<version>.pkg          macOS installer
    MoxaSerial-<version>-Setup.exe    Windows installer
    SHA256SUMS                        "<sha256>  <filename>" per asset

The updater only ever consumes the ``.zip`` (it is the only asset that can be
applied without an elevated installer) plus ``SHA256SUMS``.

Three rules that shaped this module
-----------------------------------
1. **Nothing inside the add-in folder is preserved.** Settings and logs live
   in the per-user app-data directory (``moxaserial/paths.py``), so the folder
   holds only shipped files and the swap can be a plain directory rename.
2. **A development install is never touched.** If the add-in folder is a
   symlink (the usual "link the git checkout into Fusion's AddIns folder"
   dev setup) or contains a ``.git`` entry, :func:`apply_update` refuses with
   a sentence telling the operator to ``git pull`` instead. Silently
   replacing someone's working tree with a release zip would be unforgivable.
3. **The swap is reversible.** The live folder is *renamed* to
   ``MoxaSerial.bak-<version>`` rather than deleted, the staged folder is
   renamed into its place, and any failure renames the backup straight back.
   The two most recent backups are kept; older ones are removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from functools import cmp_to_key
from pathlib import Path
from typing import Any

from moxaserial import __version__
from moxaserial.config import DEFAULT_UPDATE_REPO
from moxaserial.log import get_logger

log = get_logger("update")

GITHUB_API = "https://api.github.com"
USER_AGENT = f"MoxaSerial/{__version__} (+https://github.com/{DEFAULT_UPDATE_REPO})"
ACCEPT_JSON = "application/vnd.github+json"

DEFAULT_TIMEOUT = 15.0
#: Read size while streaming a release asset to disk.
CHUNK = 64 * 1024
#: Refuse anything implausible for an add-in zip (guards a wrong URL).
MAX_ASSET_BYTES = 256 * 1024 * 1024
#: How many ``MoxaSerial.bak-*`` folders to keep beside the add-in.
KEEP_BACKUPS = 2

#: The one file whose presence proves a zip really is a MoxaSerial release.
MANIFEST_NAME = "MoxaSerial.manifest"
PAYLOAD_ROOT = "MoxaSerial"
MANIFEST_IN_ZIP = f"{PAYLOAD_ROOT}/{MANIFEST_NAME}"

#: Progress stages pushed to the UI as ``update.progress``.
STAGES = ("check", "download", "verify", "extract", "install", "restart", "done")

ProgressFn = Callable[[str, float, str], None]


class UpdateError(Exception):
    """Anything that stops an update, phrased for the operator."""


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"^\s*v?(?P<nums>\d+(?:\.\d+)*)(?:-(?P<rest>.*))?\s*$", re.IGNORECASE)


def parse_version(text: Any) -> tuple[tuple[int, ...], tuple[Any, ...]]:
    """Split ``"v1.2.3-beta.2"`` into ``((1, 2, 3), ("beta", 2))``.

    Unparseable input becomes ``((0,), ())`` rather than raising - a release
    someone tagged ``"latest"`` must not crash the check.
    """
    # Build metadata is not part of precedence (semver §10); drop it first so
    # "1.0.0+build9" and "1.0.0+build1" compare equal.
    raw = str(text or "").split("+", 1)[0]
    match = _VERSION_RE.match(raw)
    if not match:
        return (0,), ()
    nums = tuple(int(p) for p in match.group("nums").split("."))
    rest = match.group("rest") or ""
    pre: list[Any] = []
    for part in rest.split(".") if rest else []:
        if not part:
            continue
        pre.append(int(part) if part.isdigit() else part.lower())
    return nums, tuple(pre)


def _pad(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    size = max(len(a), len(b))
    return a + (0,) * (size - len(a)), b + (0,) * (size - len(b))


def compare_versions(left: Any, right: Any) -> int:
    """``-1`` / ``0`` / ``1`` for ``left`` <, ==, > ``right`` (semver order).

    ``1.2`` == ``1.2.0``; a pre-release sorts *below* the release it precedes
    (``1.2.0-rc.1`` < ``1.2.0``); numeric pre-release identifiers sort
    numerically and below alphanumeric ones.
    """
    lnums, lpre = parse_version(left)
    rnums, rpre = parse_version(right)
    lnums, rnums = _pad(lnums, rnums)
    if lnums != rnums:
        return -1 if lnums < rnums else 1
    if not lpre and not rpre:
        return 0
    if not lpre:
        return 1  # a release outranks its own pre-releases
    if not rpre:
        return -1
    for lp, rp in zip(lpre, rpre, strict=False):
        if lp == rp:
            continue
        l_num, r_num = isinstance(lp, int), isinstance(rp, int)
        if l_num and r_num:
            return -1 if lp < rp else 1
        if l_num != r_num:
            return -1 if l_num else 1  # numeric identifiers rank lower
        return -1 if str(lp) < str(rp) else 1
    if len(lpre) == len(rpre):
        return 0
    return -1 if len(lpre) < len(rpre) else 1


def is_newer(candidate: Any, current: Any) -> bool:
    """True when *candidate* is a strictly later version than *current*."""
    return compare_versions(candidate, current) > 0


# ---------------------------------------------------------------------------
# The installed version
# ---------------------------------------------------------------------------

def addin_dir() -> Path:
    """The add-in root - the folder holding ``MoxaSerial.manifest``.

    Deliberately **not** ``resolve()``d: resolving follows the very symlink
    that marks a development install, which would erase two of the three
    signals :func:`is_development_install` looks for and let the updater
    overwrite somebody's git checkout. ``ui/updater.addin_root()`` makes the
    same choice.
    """
    return Path(os.path.normpath(os.path.abspath(__file__))).parent.parent


def manifest_path(root: Path | str | None = None) -> Path:
    base = Path(root) if root else addin_dir()
    return base if base.name == MANIFEST_NAME else base / MANIFEST_NAME


def manifest_version(root: Path | str | None = None) -> str:
    """Version string from ``MoxaSerial.manifest``.

    The manifest - not ``moxaserial.__version__`` - is what Fusion shows in
    Scripts and Add-Ins, so it is the number the comparison must use. Falls
    back to the package version when the manifest is missing or malformed.
    """
    try:
        raw = manifest_path(root).read_text(encoding="utf-8")
        value = json.loads(raw).get("version")
        if value:
            return str(value).strip()
    except (OSError, ValueError):
        log.debug("Could not read the add-in manifest version", exc_info=True)
    return __version__


def is_development_install(root: Path | str | None = None) -> bool:
    """True when the add-in folder is a git checkout or a symlink to one.

    Two signals, either is enough:

    * the folder itself is a symlink - the standard dev setup is to link the
      checkout into Fusion's ``AddIns`` folder;
    * a ``.git`` entry sits inside it (a directory for a normal clone, a file
      for a worktree or submodule).
    """
    path = Path(root) if root else addin_dir()
    try:
        if path.is_symlink():
            return True
        if (path / ".git").exists():
            return True
        # A parent component may be the link (…/AddIns/link/MoxaSerial).
        if os.path.realpath(path) != os.path.abspath(path):
            return True
    except OSError:
        # Cannot tell - so refuse. An update that cannot classify the folder
        # it is about to rename must not proceed; "update with git pull" is a
        # recoverable wrong answer, overwriting a working tree is not.
        log.warning("Could not classify the add-in folder; treating it as a development install.",
                    exc_info=True)
        return True
    return False


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def _request(url: str, accept: str = ACCEPT_JSON) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,  # GitHub rejects requests without one
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _http_problem(exc: Exception, repo: str) -> str:
    """Turn a urllib failure into one sentence an operator can act on."""
    if isinstance(exc, urllib.error.HTTPError):
        remaining = ""
        try:
            remaining = exc.headers.get("X-RateLimit-Remaining", "") or ""
        except Exception:  # noqa: BLE001 - headers may be absent entirely
            remaining = ""
        if exc.code in (403, 429) and remaining == "0":
            reset = ""
            try:
                epoch = int(exc.headers.get("X-RateLimit-Reset", "") or 0)
                if epoch:
                    reset = " until " + time.strftime("%H:%M", time.localtime(epoch))
            except (TypeError, ValueError):
                reset = ""
            return (
                f"GitHub is rate-limiting this network{reset}. "
                "The check will run again later; nothing is wrong with the add-in."
            )
        if exc.code in (403, 429):
            return f"GitHub refused the request for '{repo}' (HTTP {exc.code})."
        if exc.code == 404:
            return (
                f"No published release found for '{repo}' "
                "(check the repository name on the About page)."
            )
        if exc.code == 401:
            return f"'{repo}' is private - the updater can only read public releases."
        return f"GitHub returned HTTP {exc.code} for '{repo}'."
    if isinstance(exc, urllib.error.URLError):
        return f"Could not reach GitHub ({exc.reason}). Is this machine offline?"
    if isinstance(exc, TimeoutError):
        return "The connection to GitHub timed out."
    return f"Update check failed: {exc}"


def _fetch_json(url: str, timeout: float) -> Any:
    with urllib.request.urlopen(_request(url), timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _pick_release(payload: Any, include_prereleases: bool) -> dict[str, Any] | None:
    """The newest usable release out of ``/releases/latest`` or ``/releases``."""
    releases = payload if isinstance(payload, list) else [payload]
    usable = [
        r
        for r in releases
        if isinstance(r, dict)
        and not r.get("draft")
        and (include_prereleases or not r.get("prerelease"))
    ]
    if not usable:
        return None
    # cmp_to_key, not the raw parse_version tuple: a pre-release tuple can mix
    # ints and strings ("1.0.0-1" vs "1.0.0-beta") and tuple comparison would
    # raise TypeError on that pair. compare_versions already orders them.
    usable.sort(
        key=cmp_to_key(
            lambda a, b: compare_versions(
                a.get("tag_name") or a.get("name"), b.get("tag_name") or b.get("name")
            )
        ),
        reverse=True,
    )
    return usable[0]


def _find_assets(release: dict[str, Any]) -> tuple[str, str]:
    """``(zip_url, sha_url)`` from a release payload; either may be ``""``."""
    zip_url = sha_url = ""
    for asset in release.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", ""))
        url = str(asset.get("browser_download_url", ""))
        if not url:
            continue
        low = name.lower()
        if low.endswith(".zip") and not zip_url:
            zip_url = url
        elif low == "sha256sums" and not sha_url:
            sha_url = url
    return zip_url, sha_url


def check_for_update(
    current_version: str | None = None,
    repo: str = DEFAULT_UPDATE_REPO,
    timeout: float = DEFAULT_TIMEOUT,
    include_prereleases: bool = False,
) -> dict[str, Any]:
    """Ask GitHub for the newest release. Never raises; reports in ``error``.

    Returns::

        {available, current, latest, notes, zip_url, sha_url, published,
         html_url, prerelease, error, checked}

    Every network failure - offline, DNS, 404, rate limit - comes back as
    ``available: False`` plus a human sentence in ``error``. The caller is a
    background timer thread; an exception there would be invisible.
    """
    current = str(current_version or manifest_version())
    repo = (repo or DEFAULT_UPDATE_REPO).strip("/")
    result: dict[str, Any] = {
        "available": False,
        "current": current,
        "latest": "",
        "notes": "",
        "zip_url": "",
        "sha_url": "",
        "published": "",
        "html_url": "",
        "prerelease": False,
        "error": "",
        "checked": time.time(),
    }
    url = (
        f"{GITHUB_API}/repos/{repo}/releases?per_page=20"
        if include_prereleases
        else f"{GITHUB_API}/repos/{repo}/releases/latest"
    )
    try:
        payload = _fetch_json(url, timeout)
    except Exception as exc:  # noqa: BLE001 - every failure is a reported string
        result["error"] = _http_problem(exc, repo)
        log.info("Update check failed: %s", result["error"])
        return result

    release = _pick_release(payload, include_prereleases)
    if release is None:
        result["error"] = f"'{repo}' has no published release yet."
        return result

    latest = str(release.get("tag_name") or release.get("name") or "").strip()
    zip_url, sha_url = _find_assets(release)
    result.update(
        {
            "latest": latest.lstrip("vV"),
            "notes": str(release.get("body") or "").strip(),
            "zip_url": zip_url,
            "sha_url": sha_url,
            "published": str(release.get("published_at") or ""),
            "html_url": str(release.get("html_url") or ""),
            "prerelease": bool(release.get("prerelease")),
            "available": is_newer(latest, current),
        }
    )
    if result["available"] and not zip_url:
        result["available"] = False
        result["error"] = (
            f"Release {latest} has no .zip asset, so it cannot be installed from here. "
            "Download it from the release page instead."
        )
    log.info(
        "Update check: installed %s, latest %s -> %s",
        current,
        result["latest"] or "?",
        "update available" if result["available"] else "up to date",
    )
    return result


# ---------------------------------------------------------------------------
# Download + verify
# ---------------------------------------------------------------------------

_SHA_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+)$")


def parse_sha256sums(text: str) -> dict[str, str]:
    """``SHA256SUMS`` -> ``{filename: lowercase digest}``.

    Accepts both coreutils spellings (``"<sum>  name"`` text mode and
    ``"<sum> *name"`` binary mode) and ignores anything else in the file.
    """
    sums: dict[str, str] = {}
    for line in text.splitlines():
        match = _SHA_LINE.match(line.strip())
        if match:
            sums[os.path.basename(match.group(2).strip())] = match.group(1).lower()
    return sums


def _asset_name(url: str) -> str:
    return os.path.basename(url.split("?", 1)[0].split("#", 1)[0]) or "MoxaSerial.zip"


def download_update(
    zip_url: str,
    sha_url: str = "",
    dest_dir: Path | str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    progress: ProgressFn | None = None,
) -> Path:
    """Stream the release zip to *dest_dir*, verify it, return its path.

    Three checks, in order, each fatal:

    1. **SHA256** against the ``SHA256SUMS`` asset - skipped with a warning
       when the release has no such asset, because an older release may not.
    2. The file is a readable zip.
    3. The zip contains ``MoxaSerial/MoxaSerial.manifest`` - i.e. it is a
       MoxaSerial release and not some other project's artifact.

    A failed check deletes the download and raises :class:`UpdateError`.
    """
    if not zip_url:
        raise UpdateError("This release has no downloadable zip.")
    dest = Path(dest_dir) if dest_dir else Path(_temp_root())
    dest.mkdir(parents=True, exist_ok=True)
    name = _asset_name(zip_url)
    target = dest / name

    def report(stage: str, percent: float, message: str) -> None:
        if progress is not None:
            try:
                progress(stage, percent, message)
            except Exception:  # noqa: BLE001 - a UI sink must never break a download
                log.debug("Update progress sink failed", exc_info=True)

    report("download", 0.0, f"Downloading {name}...")
    digest = hashlib.sha256()
    read = 0
    try:
        req = _request(zip_url, accept="application/octet-stream")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            total = int(resp.headers.get("Content-Length") or 0)
            if total and total > MAX_ASSET_BYTES:
                raise UpdateError(f"{name} is implausibly large ({total} bytes); refusing.")
            with open(target, "wb") as fh:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    read += len(chunk)
                    if read > MAX_ASSET_BYTES:
                        raise UpdateError(f"{name} exceeded {MAX_ASSET_BYTES} bytes; refusing.")
                    fh.write(chunk)
                    digest.update(chunk)
                    if total:
                        report(
                            "download",
                            min(99.0, read * 100.0 / total),
                            f"Downloading {name}... {read // 1024} kB",
                        )
    except UpdateError:
        _unlink(target)
        raise
    except Exception as exc:  # noqa: BLE001
        _unlink(target)
        raise UpdateError(_http_problem(exc, "the release asset")) from exc

    report("verify", 100.0, "Verifying the download...")
    expected = ""
    if sha_url:
        try:
            req = _request(sha_url, accept="text/plain")
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                sums = parse_sha256sums(resp.read().decode("utf-8", errors="replace"))
            expected = sums.get(name, "")
        except Exception:  # noqa: BLE001 - a missing checksum file is not fatal
            log.warning("Could not fetch SHA256SUMS; continuing without a checksum.")
    if expected:
        actual = digest.hexdigest()
        if actual != expected:
            _unlink(target)
            raise UpdateError(
                f"Checksum mismatch for {name}. The download was corrupted or tampered "
                "with and has been deleted."
            )
        log.info("SHA256 verified for %s.", name)
    else:
        log.warning("No SHA256 entry for %s; the download could not be verified.", name)

    try:
        with zipfile.ZipFile(target) as zf:
            names = zf.namelist()
            bad = zf.testzip()
            if bad is not None:
                raise UpdateError(f"{name} is a damaged zip (bad entry: {bad}).")
    except UpdateError:
        _unlink(target)
        raise
    except zipfile.BadZipFile as exc:
        _unlink(target)
        raise UpdateError(f"{name} is not a valid zip file.") from exc

    if MANIFEST_IN_ZIP not in {n.replace("\\", "/") for n in names}:
        _unlink(target)
        raise UpdateError(
            f"{name} does not contain {MANIFEST_IN_ZIP} - it is not a MoxaSerial release."
        )
    report("verify", 100.0, f"{name} verified.")
    return target


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _safe_extract(zip_path: Path, into: Path) -> None:
    """Extract *zip_path* into *into*, refusing any member that escapes it."""
    into.mkdir(parents=True, exist_ok=True)
    root = into.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            name = member.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                raise UpdateError(f"Refusing a zip entry that escapes the folder: {name}")
            target = (root / name).resolve()
            if not str(target).startswith(str(root) + os.sep) and target != root:
                raise UpdateError(f"Refusing a zip entry that escapes the folder: {name}")
            # A symlink member passes every name check above and then points
            # wherever it likes. CPython's extractall happens to write it as
            # a regular file rather than a link, so this is belt and braces -
            # but the add-in ships no symlinks, so anything claiming to be one
            # is a reason to stop rather than something to normalise.
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise UpdateError(f"Refusing a symlink inside the update archive: {name}")
        zf.extractall(root)


def _backup_name(root: Path, version: str) -> Path:
    base = root.parent / f"{root.name}.bak-{version or 'unknown'}"
    candidate = base
    n = 2
    while candidate.exists():
        candidate = Path(str(base) + f".{n}")
        n += 1
    return candidate


def prune_backups(root: Path, keep: int = KEEP_BACKUPS, protect: Path | None = None) -> list[str]:
    """Delete all but the *keep* newest ``<name>.bak-*`` folders. Returns paths removed.

    *protect* is never deleted whatever the mtimes say. The caller passes the
    backup it has just created, because "newest mtime wins" does not identify
    it: ``os.rename`` does not touch the renamed directory's mtime, so a fresh
    backup inherits the *old* release's timestamp, while a half-failed
    ``rmtree`` bumps an older one to now. Relying on ordering alone can delete
    the only rollback copy moments after the swap.
    """
    pattern = f"{root.name}.bak-"
    keep_path = None if protect is None else os.path.normpath(os.path.abspath(protect))
    found = [
        p
        for p in root.parent.iterdir()
        if p.name.startswith(pattern) and p.is_dir() and not p.is_symlink()
    ]

    def when(p: Path) -> tuple[int, float]:
        # The protected backup sorts first unconditionally, so it survives
        # without depending on a timestamp that rename() did not update. It
        # still counts against *keep* - it is one of the copies being kept.
        if keep_path is not None and os.path.normpath(os.path.abspath(p)) == keep_path:
            return (1, 0.0)
        try:
            return (0, p.stat().st_mtime)
        except OSError:      # vanished under us - treat as oldest
            return (0, 0.0)

    found.sort(key=when, reverse=True)
    removed: list[str] = []
    for old in found[max(0, keep):]:
        if keep_path is not None and os.path.normpath(os.path.abspath(old)) == keep_path:
            continue         # keep <= 0: still never delete the new backup
        try:
            shutil.rmtree(old)
            removed.append(str(old))
        except OSError:
            log.warning("Could not remove the old backup %s", old, exc_info=True)
    return removed


def apply_update(
    zip_path: Path | str,
    addin_dir: Path | str,
    keep_backups: int = KEEP_BACKUPS,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Replace the add-in folder with the contents of *zip_path*.

    Sequence, each step reversible until the last:

    1. refuse a development install (symlink or ``.git``);
    2. extract the zip into ``.MoxaSerial.staging-<pid>-<ts>`` **next to** the
       add-in folder, so the swap is a rename on the same filesystem;
    3. rename the live folder to ``MoxaSerial.bak-<installed version>``;
    4. rename the staged ``MoxaSerial`` folder into the live name;
    5. on any failure in 3-4, rename the backup straight back and re-raise.

    Nothing inside the add-in folder is preserved: settings and logs live in
    the app-data directory, and keeping stray files would let a removed
    module linger and shadow its replacement.

    Returns ``{from_version, to_version, backup, addin_dir, pruned}``.
    """
    zip_path = Path(zip_path)
    # Deliberately *not* resolve()d: resolving would silently follow the very
    # symlink that marks a development install, and the swap must happen at
    # the path Fusion actually loads.
    root = Path(os.path.abspath(addin_dir))

    def report(stage: str, percent: float, message: str) -> None:
        if progress is not None:
            try:
                progress(stage, percent, message)
            except Exception:  # noqa: BLE001
                log.debug("Update progress sink failed", exc_info=True)

    if is_development_install(root):
        raise UpdateError(
            "This is a development install (the add-in folder is a git checkout or a "
            "symlink to one) - update with git pull, not from here."
        )
    if not root.is_dir():
        raise UpdateError(f"The add-in folder does not exist: {root}")

    from_version = manifest_version(root)
    staging = root.parent / f".{root.name}.staging-{os.getpid()}-{int(time.time())}"
    report("extract", 0.0, "Extracting the update...")
    try:
        if staging.exists():
            shutil.rmtree(staging)
        _safe_extract(zip_path, staging)
        payload = staging / PAYLOAD_ROOT
        if not (payload / MANIFEST_NAME).is_file():
            raise UpdateError(
                f"The update zip has no {MANIFEST_IN_ZIP}; refusing to install it."
            )
        to_version = manifest_version(payload)
    except UpdateError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(f"Could not extract the update: {exc}") from exc

    backup = _backup_name(root, from_version)
    report("install", 50.0, f"Installing {to_version}...")
    try:
        os.rename(root, backup)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(
            f"Could not move the current add-in aside ({exc}). Nothing was changed - "
            "close anything using the add-in folder and try again."
        ) from exc

    try:
        os.rename(payload, root)
    except OSError as exc:
        # Put the old folder back before anyone notices it left.
        try:
            os.rename(backup, root)
            restored = "The previous version was restored."
        except OSError:
            restored = (
                f"The previous version is still at {backup} and must be renamed back "
                f"to {root.name} by hand."
            )
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(f"Could not install the update ({exc}). {restored}") from exc

    shutil.rmtree(staging, ignore_errors=True)
    # The swap has already succeeded, so housekeeping must never turn it into
    # a reported failure: the operator would be told the update failed while
    # running the new version.
    try:
        pruned = prune_backups(root, keep_backups, protect=backup)
    except OSError:
        log.warning("Could not prune old backups", exc_info=True)
        pruned = []
    log.info("Updated %s -> %s (backup at %s).", from_version, to_version, backup)
    report("install", 100.0, f"Installed {to_version}.")
    return {
        "from_version": from_version,
        "to_version": to_version,
        "backup": str(backup),
        "addin_dir": str(root),
        "pruned": pruned,
    }


def _temp_root() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="moxaserial-update-")


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# The scheduled service the bridge owns
# ---------------------------------------------------------------------------

class UpdateService:
    """Check / download / install, on worker threads, with UI pushes.

    Threading: every network and filesystem step runs on a daemon worker so
    Fusion's UI thread never blocks on a socket. The only main-thread work is
    the restart, which the host performs through its own custom-event hop
    (:mod:`moxaserial.ui.updater`); this class never imports ``adsk``.
    """

    def __init__(
        self,
        store: Any,
        push: Callable[[str, dict[str, Any]], None] | None = None,
        host: Any = None,
    ) -> None:
        self.store = store
        self._push = push or (lambda action, payload: None)
        self.host = host
        self._lock = threading.RLock()
        self._busy = False
        self._timer: threading.Timer | None = None
        self._last: dict[str, Any] = {}

    # -- settings ------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        from moxaserial.config import normalize_update

        return normalize_update(self.store.get("update"))

    def _remember(self, **values: Any) -> None:
        try:
            self.store.update_globals({"update": values})
        except Exception:  # noqa: BLE001 - a settings write must not kill a check
            log.debug("Could not persist update bookkeeping", exc_info=True)

    def root(self) -> Path:
        folder = ""
        if self.host is not None:
            try:
                folder = self.host.addin_dir() or ""
            except Exception:  # noqa: BLE001
                folder = ""
        return Path(folder) if folder else addin_dir()

    def current_version(self) -> str:
        return manifest_version(self.root())

    def status(self) -> dict[str, Any]:
        """What the About page renders. Cheap; never touches the network."""
        cfg = self.settings()
        return {
            "settings": cfg,
            "current": self.current_version(),
            "busy": self._busy,
            "developmentInstall": is_development_install(self.root()),
            "addinDir": str(self.root()),
            "lastCheck": cfg.get("last_check", 0.0),
            "lastError": cfg.get("last_error", ""),
            "result": self._last,
        }

    # -- checking ------------------------------------------------------
    def due(self, now: float | None = None) -> bool:
        """Whether ``check_interval_hours`` has elapsed since the last check."""
        cfg = self.settings()
        try:
            interval = float(cfg.get("check_interval_hours", 24) or 0)
        except (TypeError, ValueError):
            interval = 24.0
        if interval <= 0:
            return True
        last = float(cfg.get("last_check", 0.0) or 0.0)
        return (now or time.time()) - last >= interval * 3600.0

    def check(self, force: bool = False) -> dict[str, Any]:
        """Blocking check. Call from a worker thread (or a test)."""
        cfg = self.settings()
        if not force and not cfg.get("auto_check", True):
            return {"available": False, "error": "", "skipped": "auto-check is off"}
        result = check_for_update(
            current_version=self.current_version(),
            repo=str(cfg.get("repo") or DEFAULT_UPDATE_REPO),
            include_prereleases=bool(cfg.get("include_prereleases", False)),
        )
        self._last = result
        self._remember(
            last_check=result.get("checked", time.time()),
            last_seen_version=result.get("latest", ""),
            last_error=result.get("error", ""),
        )
        self._push("update.checked", result)
        if result.get("available"):
            self._push("update.available", result)
            self._toast(
                f"MoxaSerial {result.get('latest', '')} is available "
                f"(you have {result.get('current', '')}).",
                "info",
            )
        return result

    def check_async(self, force: bool = True) -> None:
        threading.Thread(
            target=self._check_worker, args=(force,), name="moxa-update-check", daemon=True
        ).start()

    def _check_worker(self, force: bool) -> None:
        try:
            result = self.check(force=force)
        except Exception:  # noqa: BLE001 - a daemon thread must not die loudly
            log.exception("Update check thread failed")
            return
        if result.get("available") and self.settings().get("auto_install", False):
            if is_development_install(self.root()):
                log.info("Auto-install skipped: this is a development install.")
                return
            self.install_async(result)

    def schedule_startup_check(self, delay: float = 6.0) -> bool:
        """Arm the delayed start-up check. False when it will not run.

        Delayed so the add-in's own start-up (palette, toolbar, config) is
        finished and Fusion's first document load is not competing with a TLS
        handshake. The timer thread does nothing but call :meth:`check`.
        """
        cfg = self.settings()
        if not cfg.get("auto_check", True):
            log.debug("Automatic update checks are disabled.")
            return False
        if not self.due():
            log.debug("Update check not due yet.")
            return False
        self.cancel()
        timer = threading.Timer(max(0.0, delay), self._check_worker, args=(False,))
        timer.daemon = True
        timer.name = "moxa-update-startup"
        self._timer = timer
        timer.start()
        return True

    def cancel(self) -> None:
        timer = self._timer
        if timer is not None:
            timer.cancel()
        self._timer = None

    # -- installing ----------------------------------------------------
    def install_async(self, result: dict[str, Any] | None = None) -> dict[str, Any]:
        """Kick off download + apply + restart. Returns immediately."""
        with self._lock:
            if self._busy:
                return {"started": False, "error": "An update is already in progress."}
            self._busy = True
        payload = result or self._last
        threading.Thread(
            target=self._install_worker, args=(payload,), name="moxa-update-install", daemon=True
        ).start()
        return {"started": True}

    def _install_worker(self, result: dict[str, Any] | None) -> None:
        try:
            self.install_now(result)
        except Exception:  # noqa: BLE001 - a daemon thread must not die loudly
            log.exception("Update install thread failed")
        finally:
            with self._lock:
                self._busy = False

    def install_now(self, result: dict[str, Any] | None = None) -> None:
        """Download, verify, apply, restart. Blocking; never raises.

        Every failure is reported as an ``update.error`` push plus a toast -
        the caller is a worker thread with nowhere to propagate to.
        """

        def progress(stage: str, percent: float, message: str) -> None:
            self._push(
                "update.progress", {"stage": stage, "percent": round(percent, 1), "message": message}
            )

        try:
            if not result or not result.get("zip_url"):
                progress("check", 0.0, "Looking for the newest release...")
                result = self.check(force=True)
            if not result.get("available"):
                raise UpdateError(result.get("error") or "No update is available.")
            root = self.root()
            if is_development_install(root):
                raise UpdateError(
                    "This is a development install - update with git pull, not from here."
                )
            zip_path = download_update(
                result["zip_url"], result.get("sha_url", ""), progress=progress
            )
            outcome = apply_update(zip_path, root, progress=progress)
            shutil.rmtree(zip_path.parent, ignore_errors=True)
        except UpdateError as exc:
            log.warning("Update failed: %s", exc)
            self._push("update.error", {"message": str(exc)})
            self._toast(str(exc), "error")
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("Update failed")
            self._push("update.error", {"message": str(exc)})
            self._toast(f"Update failed: {exc}", "error")
            return

        self._push("update.done", outcome)
        self._toast(
            f"MoxaSerial {outcome['to_version']} installed. Reloading the add-in...", "success"
        )
        progress("restart", 100.0, "Reloading the add-in...")
        self._restart()

    def _restart(self) -> None:
        restarted = False
        if self.host is not None:
            try:
                restarted = bool(self.host.restart_addin())
            except Exception:  # noqa: BLE001
                log.exception("Add-in restart failed")
                restarted = False
        if not restarted:
            self._toast(
                "Update installed. Restart Fusion (or toggle the add-in in Scripts and "
                "Add-Ins) to load it.",
                "warning",
            )
        self._push("update.restart", {"restarted": restarted})

    # -- helpers -------------------------------------------------------
    def _toast(self, message: str, level: str = "info") -> None:
        if self.host is None:
            return
        try:
            self.host.toast(message, level=level)
        except Exception:  # noqa: BLE001
            log.debug("Update toast failed", exc_info=True)
