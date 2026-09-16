"""Auto-update: version compare, GitHub check, download/verify, apply/rollback.

No network. ``urllib.request.urlopen`` is monkeypatched with a fake that
serves canned JSON / bytes, and every filesystem case uses real zip files in
``tmp_path`` so the rename dance and its rollback are genuinely exercised.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import zipfile
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from moxaserial import update
from moxaserial.update import (
    UpdateError,
    UpdateService,
    apply_update,
    check_for_update,
    compare_versions,
    download_update,
    is_development_install,
    is_newer,
    manifest_version,
    parse_sha256sums,
    parse_version,
    prune_backups,
)

# ---------------------------------------------------------------------------
# Fake HTTP
# ---------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, headers: dict | None = None) -> None:
        super().__init__(body)
        self.headers = Message()
        for k, v in (headers or {}).items():
            self.headers[k] = str(v)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def http_error(code: int, headers: dict | None = None) -> HTTPError:
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = str(v)
    return HTTPError("https://api.github.com/x", code, "boom", msg, None)


def serve(monkeypatch, routes: dict):
    """Route by URL substring. A value may be bytes, a dict (JSON) or an exception."""
    seen: list[str] = []

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        url = req.full_url if hasattr(req, "full_url") else str(req)
        seen.append(url)
        assert req.headers.get("User-agent"), "GitHub rejects requests with no User-Agent"
        for fragment, value in routes.items():
            if fragment in url:
                if isinstance(value, Exception):
                    raise value
                if isinstance(value, bytes):
                    return FakeResponse(value, {"Content-Length": len(value)})
                body = json.dumps(value).encode()
                return FakeResponse(body, {"Content-Length": len(body)})
        raise AssertionError(f"unrouted URL: {url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def release(version="0.2.0", prerelease=False, assets=True, draft=False):
    out = {
        "tag_name": f"v{version}",
        "name": f"MoxaSerial {version}",
        "body": f"Release notes for {version}.",
        "published_at": "2026-09-01T12:00:00Z",
        "html_url": f"https://github.com/o/r/releases/tag/v{version}",
        "prerelease": prerelease,
        "draft": draft,
        "assets": [],
    }
    if assets:
        out["assets"] = [
            {
                "name": f"MoxaSerial-{version}.zip",
                "browser_download_url": f"https://dl/MoxaSerial-{version}.zip",
            },
            {
                "name": f"MoxaSerial-{version}.pkg",
                "browser_download_url": f"https://dl/MoxaSerial-{version}.pkg",
            },
            {
                "name": f"MoxaSerial-{version}-Setup.exe",
                "browser_download_url": f"https://dl/MoxaSerial-{version}-Setup.exe",
            },
            {"name": "SHA256SUMS", "browser_download_url": "https://dl/SHA256SUMS"},
        ]
    return out


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def write_zip(path: Path, version: str = "0.2.0", manifest_at: str | None = None) -> Path:
    entry = manifest_at or "MoxaSerial/MoxaSerial.manifest"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(entry, json.dumps({"type": "addin", "version": version}))
        zf.writestr("MoxaSerial/MoxaSerial.py", "# the new entry point\n")
        zf.writestr("MoxaSerial/moxaserial/__init__.py", f'__version__ = "{version}"\n')
    return path


def write_addin(root: Path, version: str = "0.1.0") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "MoxaSerial.manifest").write_text(json.dumps({"type": "addin", "version": version}))
    (root / "MoxaSerial.py").write_text("# the old entry point\n")
    (root / "moxaserial").mkdir(exist_ok=True)
    (root / "moxaserial" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    return root


@pytest.fixture
def installed(tmp_path):
    """An 'AddIns' folder holding a normal (non-dev) MoxaSerial install."""
    addins = tmp_path / "AddIns"
    addins.mkdir()
    return write_addin(addins / "MoxaSerial", "0.1.0")


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "left,right,expected",
    [
        ("0.1.0", "0.1.0", 0),
        ("v0.1.0", "0.1.0", 0),          # the tag's leading v is noise
        ("1.2", "1.2.0", 0),             # missing components are zeros
        ("0.2.0", "0.1.9", 1),
        ("0.10.0", "0.9.0", 1),          # not a string compare
        ("1.0.0", "0.99.99", 1),
        ("0.1.0", "0.1.1", -1),
        ("1.0.0-rc.1", "1.0.0", -1),     # a pre-release precedes its release
        ("1.0.0", "1.0.0-rc.1", 1),
        ("1.0.0-alpha", "1.0.0-beta", -1),
        ("1.0.0-alpha.1", "1.0.0-alpha.2", -1),
        ("1.0.0-alpha.9", "1.0.0-alpha.10", -1),   # numeric, not lexical
        ("1.0.0-1", "1.0.0-alpha", -1),            # numeric ranks below alnum
        ("1.0.0-alpha", "1.0.0-alpha.1", -1),      # fewer fields rank lower
        ("1.0.0+build9", "1.0.0+build1", 0),       # build metadata is ignored
        ("garbage", "0.0.1", -1),                  # unparseable sorts last
        ("2.0.0", "garbage", 1),
    ],
)
def test_compare_versions(left, right, expected):
    assert compare_versions(left, right) == expected
    assert compare_versions(right, left) == -expected


def test_parse_version_never_raises():
    assert parse_version(None) == ((0,), ())
    assert parse_version("") == ((0,), ())
    assert parse_version("latest") == ((0,), ())
    assert parse_version("v1.2.3-beta.2") == ((1, 2, 3), ("beta", 2))


def test_is_newer():
    assert is_newer("0.2.0", "0.1.0")
    assert not is_newer("0.1.0", "0.1.0")
    assert not is_newer("0.0.9", "0.1.0")


# ---------------------------------------------------------------------------
# check_for_update
# ---------------------------------------------------------------------------


def test_check_reports_an_available_update(monkeypatch):
    seen = serve(monkeypatch, {"/releases/latest": release("0.2.0")})
    result = check_for_update("0.1.0", repo="o/r")
    assert result["available"] is True
    assert result["latest"] == "0.2.0"          # the tag's "v" is stripped
    assert result["zip_url"].endswith("MoxaSerial-0.2.0.zip")
    assert result["sha_url"].endswith("SHA256SUMS")
    assert "Release notes" in result["notes"]
    assert result["published"] and result["html_url"]
    assert result["error"] == ""
    assert "api.github.com/repos/o/r/releases/latest" in seen[0]


def test_check_says_up_to_date_when_the_versions_match(monkeypatch):
    serve(monkeypatch, {"/releases/latest": release("0.1.0")})
    result = check_for_update("0.1.0", repo="o/r")
    assert result["available"] is False
    assert result["latest"] == "0.1.0"
    assert result["error"] == ""


def test_check_does_not_downgrade(monkeypatch):
    serve(monkeypatch, {"/releases/latest": release("0.1.0")})
    assert check_for_update("0.9.0", repo="o/r")["available"] is False


def test_check_picks_the_zip_and_ignores_the_installers(monkeypatch):
    serve(monkeypatch, {"/releases/latest": release("0.2.0")})
    result = check_for_update("0.1.0", repo="o/r")
    assert result["zip_url"].endswith(".zip")
    assert ".pkg" not in result["zip_url"] and ".exe" not in result["zip_url"]


def test_check_refuses_a_release_with_no_zip(monkeypatch):
    serve(monkeypatch, {"/releases/latest": release("0.2.0", assets=False)})
    result = check_for_update("0.1.0", repo="o/r")
    assert result["available"] is False
    assert "no .zip asset" in result["error"]
    assert result["latest"] == "0.2.0"  # still reported so the UI can link to it


def test_check_skips_prereleases_by_default(monkeypatch):
    serve(monkeypatch, {"/releases": [release("0.3.0-rc.1", prerelease=True), release("0.2.0")]})
    result = check_for_update("0.1.0", repo="o/r", include_prereleases=True)
    assert result["latest"] == "0.3.0-rc.1"
    assert result["prerelease"] is True


def test_check_with_prereleases_uses_the_list_endpoint(monkeypatch):
    seen = serve(monkeypatch, {"/releases": [release("0.2.0")]})
    check_for_update("0.1.0", repo="o/r", include_prereleases=True)
    assert "/releases?per_page=" in seen[0]
    assert "/releases/latest" not in seen[0]


def test_check_ignores_drafts_and_prereleases_in_the_list(monkeypatch):
    serve(
        monkeypatch,
        {
            "/releases": [
                release("0.9.0", draft=True),
                release("0.4.0", prerelease=True),
                release("0.2.0"),
            ]
        },
    )
    result = check_for_update("0.1.0", repo="o/r", include_prereleases=False)
    assert result["latest"] == "0.2.0"


def test_check_sorts_the_list_by_version_not_by_order(monkeypatch):
    serve(monkeypatch, {"/releases": [release("0.2.0"), release("0.10.0"), release("0.9.0")]})
    result = check_for_update("0.1.0", repo="o/r", include_prereleases=True)
    assert result["latest"] == "0.10.0"


def test_check_sorts_mixed_prerelease_identifiers(monkeypatch):
    """('beta',) vs (1,) would raise TypeError under a plain tuple sort."""
    serve(
        monkeypatch,
        {"/releases": [release("1.0.0-1", prerelease=True), release("1.0.0-beta", prerelease=True)]},
    )
    result = check_for_update("0.1.0", repo="o/r", include_prereleases=True)
    assert result["latest"] == "1.0.0-beta"   # alphanumeric outranks numeric
    assert result["error"] == ""


def test_check_handles_a_rate_limit(monkeypatch):
    serve(
        monkeypatch,
        {
            "/releases/latest": http_error(
                403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1800000000"}
            )
        },
    )
    result = check_for_update("0.1.0", repo="o/r")
    assert result["available"] is False
    assert "rate-limiting" in result["error"]


def test_check_handles_404(monkeypatch):
    serve(monkeypatch, {"/releases/latest": http_error(404)})
    result = check_for_update("0.1.0", repo="o/typo")
    assert result["available"] is False
    assert "No published release" in result["error"]
    assert "o/typo" in result["error"]


def test_check_handles_a_private_repo(monkeypatch):
    serve(monkeypatch, {"/releases/latest": http_error(401)})
    assert "private" in check_for_update("0.1.0", repo="o/r")["error"]


def test_check_handles_being_offline(monkeypatch):
    serve(monkeypatch, {"/releases/latest": URLError("Name or service not known")})
    result = check_for_update("0.1.0", repo="o/r")
    assert result["available"] is False
    assert "offline" in result["error"]


def test_check_handles_garbage_json(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=None: FakeResponse(b"<html>nope</html>")
    )
    result = check_for_update("0.1.0", repo="o/r")
    assert result["available"] is False
    assert result["error"]


def test_check_reports_an_empty_repo(monkeypatch):
    serve(monkeypatch, {"/releases": []})
    result = check_for_update("0.1.0", repo="o/r", include_prereleases=True)
    assert "no published release" in result["error"]


def test_check_defaults_to_the_manifest_version(monkeypatch, tmp_path):
    serve(monkeypatch, {"/releases/latest": release("0.2.0")})
    result = check_for_update(None, repo="o/r")
    assert result["current"] == manifest_version()


# ---------------------------------------------------------------------------
# SHA256SUMS
# ---------------------------------------------------------------------------


def test_parse_sha256sums_accepts_both_coreutils_spellings():
    digest = "a" * 64
    text = (
        f"{digest}  MoxaSerial-0.2.0.zip\n"
        f"{'b' * 64} *MoxaSerial-0.2.0.pkg\n"
        "not a checksum line\n"
        f"{'c' * 64}  dist/MoxaSerial-0.2.0-Setup.exe\n"
    )
    sums = parse_sha256sums(text)
    assert sums["MoxaSerial-0.2.0.zip"] == digest
    assert sums["MoxaSerial-0.2.0.pkg"] == "b" * 64
    assert sums["MoxaSerial-0.2.0-Setup.exe"] == "c" * 64  # path stripped
    assert len(sums) == 3


# ---------------------------------------------------------------------------
# download_update
# ---------------------------------------------------------------------------


def _served_zip(tmp_path, version="0.2.0", manifest_at=None) -> bytes:
    src = write_zip(tmp_path / "src.zip", version, manifest_at)
    return src.read_bytes()


def test_download_verifies_the_checksum(monkeypatch, tmp_path):
    blob = _served_zip(tmp_path)
    digest = hashlib.sha256(blob).hexdigest()
    serve(
        monkeypatch,
        {
            "MoxaSerial-0.2.0.zip": blob,
            "SHA256SUMS": f"{digest}  MoxaSerial-0.2.0.zip\n".encode(),
        },
    )
    stages = []
    out = download_update(
        "https://dl/MoxaSerial-0.2.0.zip",
        "https://dl/SHA256SUMS",
        dest_dir=tmp_path / "dl",
        progress=lambda s, p, m: stages.append(s),
    )
    assert out.exists() and out.name == "MoxaSerial-0.2.0.zip"
    assert out.read_bytes() == blob
    assert "download" in stages and "verify" in stages


def test_download_rejects_a_checksum_mismatch(monkeypatch, tmp_path):
    blob = _served_zip(tmp_path)
    serve(
        monkeypatch,
        {
            "MoxaSerial-0.2.0.zip": blob,
            "SHA256SUMS": f"{'0' * 64}  MoxaSerial-0.2.0.zip\n".encode(),
        },
    )
    dest = tmp_path / "dl"
    with pytest.raises(UpdateError, match="Checksum mismatch"):
        download_update("https://dl/MoxaSerial-0.2.0.zip", "https://dl/SHA256SUMS", dest_dir=dest)
    assert list(dest.iterdir()) == [], "a corrupt download must not be left on disk"


def test_download_continues_when_there_is_no_sha_file(monkeypatch, tmp_path):
    blob = _served_zip(tmp_path)
    serve(monkeypatch, {"MoxaSerial-0.2.0.zip": blob})
    out = download_update("https://dl/MoxaSerial-0.2.0.zip", "", dest_dir=tmp_path / "dl")
    assert out.exists()


def test_download_survives_an_unreachable_sha_file(monkeypatch, tmp_path):
    blob = _served_zip(tmp_path)
    serve(
        monkeypatch,
        {"MoxaSerial-0.2.0.zip": blob, "SHA256SUMS": URLError("gone")},
    )
    out = download_update(
        "https://dl/MoxaSerial-0.2.0.zip", "https://dl/SHA256SUMS", dest_dir=tmp_path / "dl"
    )
    assert out.exists()


def test_download_rejects_a_zip_without_our_manifest(monkeypatch, tmp_path):
    blob = _served_zip(tmp_path, manifest_at="SomethingElse/SomethingElse.manifest")
    serve(monkeypatch, {"MoxaSerial-0.2.0.zip": blob})
    dest = tmp_path / "dl"
    with pytest.raises(UpdateError, match="not a MoxaSerial release"):
        download_update("https://dl/MoxaSerial-0.2.0.zip", "", dest_dir=dest)
    assert list(dest.iterdir()) == []


def test_download_rejects_a_non_zip(monkeypatch, tmp_path):
    serve(monkeypatch, {"MoxaSerial-0.2.0.zip": b"404: Not Found"})
    dest = tmp_path / "dl"
    with pytest.raises(UpdateError, match="not a valid zip"):
        download_update("https://dl/MoxaSerial-0.2.0.zip", "", dest_dir=dest)
    assert list(dest.iterdir()) == []


def test_download_reports_a_dead_url(monkeypatch, tmp_path):
    serve(monkeypatch, {"MoxaSerial-0.2.0.zip": http_error(404)})
    with pytest.raises(UpdateError):
        download_update("https://dl/MoxaSerial-0.2.0.zip", "", dest_dir=tmp_path / "dl")


def test_download_refuses_an_absurdly_large_asset(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "MAX_ASSET_BYTES", 10)
    serve(monkeypatch, {"MoxaSerial-0.2.0.zip": b"x" * 500})
    with pytest.raises(UpdateError, match="implausibly large|exceeded"):
        download_update("https://dl/MoxaSerial-0.2.0.zip", "", dest_dir=tmp_path / "dl")


def test_download_needs_a_url():
    with pytest.raises(UpdateError, match="no downloadable zip"):
        download_update("", "")


# ---------------------------------------------------------------------------
# apply_update
# ---------------------------------------------------------------------------


def test_apply_replaces_the_folder_and_keeps_a_backup(tmp_path, installed):
    zip_path = write_zip(tmp_path / "MoxaSerial-0.2.0.zip", "0.2.0")
    (installed / "stale_module.py").write_text("# removed in 0.2.0\n")

    outcome = apply_update(zip_path, installed)

    assert outcome["from_version"] == "0.1.0"
    assert outcome["to_version"] == "0.2.0"
    assert manifest_version(installed) == "0.2.0"
    assert (installed / "MoxaSerial.py").read_text() == "# the new entry point\n"
    # Nothing is preserved from inside the old folder - settings and logs
    # live in the app-data dir, so a removed module must not linger.
    assert not (installed / "stale_module.py").exists()

    backup = Path(outcome["backup"])
    assert backup.name == "MoxaSerial.bak-0.1.0"
    assert backup.parent == installed.parent
    assert manifest_version(backup) == "0.1.0"
    assert (backup / "stale_module.py").exists()
    # No staging directory left behind.
    assert not any(p.name.startswith(".MoxaSerial.staging") for p in installed.parent.iterdir())


def test_apply_leaves_the_app_data_dir_alone(tmp_path, installed, isolated_app_data):
    """Settings live outside the add-in folder, which is what makes the swap safe."""
    from moxaserial.config import ConfigStore

    store = ConfigStore()
    store.update_globals({"theme": "light"})
    settings_file = store.path
    assert settings_file.exists()
    assert installed not in settings_file.parents

    apply_update(write_zip(tmp_path / "u.zip", "0.2.0"), installed)

    assert settings_file.exists()
    assert json.loads(settings_file.read_text())["theme"] == "light"


def test_apply_rolls_back_when_the_swap_fails(tmp_path, installed, monkeypatch):
    zip_path = write_zip(tmp_path / "u.zip", "0.2.0")
    real_rename = os.rename
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # 1 = move the old folder aside, 2 = move the new one in
            raise OSError("access denied")
        return real_rename(src, dst)

    monkeypatch.setattr(update.os, "rename", flaky)
    with pytest.raises(UpdateError, match="previous version was restored"):
        apply_update(zip_path, installed)

    assert installed.is_dir()
    assert manifest_version(installed) == "0.1.0"
    assert (installed / "MoxaSerial.py").read_text() == "# the old entry point\n"
    assert not list(installed.parent.glob("MoxaSerial.bak-*"))
    assert not list(installed.parent.glob(".MoxaSerial.staging-*"))


def test_apply_changes_nothing_when_the_folder_cannot_be_moved(tmp_path, installed, monkeypatch):
    zip_path = write_zip(tmp_path / "u.zip", "0.2.0")

    def refuse(src, dst):
        raise OSError("in use")

    monkeypatch.setattr(update.os, "rename", refuse)
    with pytest.raises(UpdateError, match="Nothing was changed"):
        apply_update(zip_path, installed)
    assert manifest_version(installed) == "0.1.0"
    assert not list(installed.parent.glob(".MoxaSerial.staging-*"))


def test_apply_rejects_a_zip_without_the_payload_folder(tmp_path, installed):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("MoxaSerial.manifest", json.dumps({"version": "0.2.0"}))  # no top folder
    with pytest.raises(UpdateError, match="MoxaSerial/MoxaSerial.manifest"):
        apply_update(bad, installed)
    assert manifest_version(installed) == "0.1.0"


def test_apply_refuses_a_zip_that_escapes_the_folder(tmp_path, installed):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("MoxaSerial/MoxaSerial.manifest", json.dumps({"version": "0.2.0"}))
        zf.writestr("../../pwned.txt", "no")
    with pytest.raises(UpdateError, match="escapes the folder"):
        apply_update(evil, installed)
    assert manifest_version(installed) == "0.1.0"


def test_apply_refuses_a_missing_folder(tmp_path):
    zip_path = write_zip(tmp_path / "u.zip", "0.2.0")
    with pytest.raises(UpdateError, match="does not exist"):
        apply_update(zip_path, tmp_path / "nope")


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_apply_refuses_a_symlinked_development_install(tmp_path):
    checkout = write_addin(tmp_path / "checkout", "0.1.0")
    (checkout / ".git").mkdir()
    addins = tmp_path / "AddIns"
    addins.mkdir()
    link = addins / "MoxaSerial"
    link.symlink_to(checkout, target_is_directory=True)

    assert is_development_install(link) is True
    with pytest.raises(UpdateError, match="development install"):
        apply_update(write_zip(tmp_path / "u.zip", "0.2.0"), link)
    assert link.is_symlink()
    assert manifest_version(checkout) == "0.1.0"


def test_apply_refuses_a_git_checkout(tmp_path, installed):
    (installed / ".git").mkdir()
    assert is_development_install(installed) is True
    with pytest.raises(UpdateError, match="git pull"):
        apply_update(write_zip(tmp_path / "u.zip", "0.2.0"), installed)
    assert manifest_version(installed) == "0.1.0"


def test_apply_refuses_a_git_worktree_file(tmp_path, installed):
    (installed / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    with pytest.raises(UpdateError, match="development install"):
        apply_update(write_zip(tmp_path / "u.zip", "0.2.0"), installed)


def test_a_plain_install_is_not_a_development_install(installed):
    assert is_development_install(installed) is False


def test_apply_keeps_only_the_two_newest_backups(tmp_path, installed):
    for i in range(1, 4):
        apply_update(write_zip(tmp_path / f"u{i}.zip", f"0.{i + 1}.0"), installed)
    backups = sorted(p.name for p in installed.parent.glob("MoxaSerial.bak-*"))
    assert len(backups) == 2, backups
    assert "MoxaSerial.bak-0.1.0" not in backups   # the oldest went
    assert "MoxaSerial.bak-0.3.0" in backups
    assert manifest_version(installed) == "0.4.0"


def test_backup_names_do_not_collide(tmp_path, installed):
    # Two updates from the same installed version (a re-run of the same release).
    apply_update(write_zip(tmp_path / "a.zip", "0.2.0"), installed)
    write_addin(installed, "0.1.0")  # pretend we rolled back by hand
    apply_update(write_zip(tmp_path / "b.zip", "0.2.0"), installed)
    names = sorted(p.name for p in installed.parent.glob("MoxaSerial.bak-0.1.0*"))
    assert names == ["MoxaSerial.bak-0.1.0", "MoxaSerial.bak-0.1.0.2"]


def test_a_folder_that_cannot_be_classified_is_treated_as_a_dev_install(monkeypatch, installed):
    """Unknown must mean refuse. Overwriting a working tree is not recoverable."""
    monkeypatch.setattr(
        update.os.path, "realpath", lambda *a, **k: (_ for _ in ()).throw(OSError("EACCES"))
    )
    assert is_development_install(installed) is True


def test_a_symlink_inside_the_archive_is_refused(tmp_path, installed):
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("MoxaSerial/MoxaSerial.manifest", json.dumps({"version": "0.2.0"}))
        info = zipfile.ZipInfo("MoxaSerial/sneaky")
        info.create_system = 3                       # Unix
        info.external_attr = (0o120777 << 16)        # S_IFLNK
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(UpdateError, match="symlink"):
        apply_update(zip_path, installed)
    assert manifest_version(installed) == "0.1.0"    # nothing was applied


def test_the_new_backup_survives_pruning_even_when_it_looks_oldest(tmp_path, installed):
    """rename() does not touch the renamed folder's mtime, so the fresh backup
    can carry the *oldest* timestamp. Ordering alone must not decide."""
    fresh = installed.parent / "MoxaSerial.bak-0.1.0"
    fresh.mkdir()
    os.utime(fresh, (1, 1))                          # far older than the others
    for name in ("MoxaSerial.bak-0.0.1", "MoxaSerial.bak-0.0.2"):
        (installed.parent / name).mkdir()
    removed = prune_backups(installed, keep=2, protect=fresh)
    assert fresh.is_dir()
    assert str(fresh) not in removed
    assert len(list(installed.parent.glob("MoxaSerial.bak-*"))) == 2


def test_the_new_backup_survives_even_with_keep_zero(installed):
    fresh = installed.parent / "MoxaSerial.bak-0.1.0"
    fresh.mkdir()
    assert prune_backups(installed, keep=0, protect=fresh) == []
    assert fresh.is_dir()


def test_a_failure_to_prune_does_not_fail_a_completed_update(monkeypatch, tmp_path, installed):
    """The swap already happened; housekeeping must not report it as failed."""
    monkeypatch.setattr(
        update.Path, "iterdir", lambda self: (_ for _ in ()).throw(OSError("gone"))
    )
    result = apply_update(write_zip(tmp_path / "u.zip", "0.2.0"), installed)
    assert result["to_version"] == "0.2.0"
    assert result["pruned"] == []
    assert manifest_version(installed) == "0.2.0"


def test_prune_backups_ignores_unrelated_folders(tmp_path, installed):
    (installed.parent / "OtherAddin").mkdir()
    (installed.parent / "MoxaSerial.bak-0.0.1").mkdir()
    removed = prune_backups(installed, keep=0)
    assert removed == [str(installed.parent / "MoxaSerial.bak-0.0.1")]
    assert (installed.parent / "OtherAddin").is_dir()
    assert installed.is_dir()


def test_manifest_version_falls_back_when_unreadable(tmp_path):
    from moxaserial import __version__

    empty = tmp_path / "empty"
    empty.mkdir()
    assert manifest_version(empty) == __version__
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "MoxaSerial.manifest").write_text("{not json")
    assert manifest_version(broken) == __version__


# ---------------------------------------------------------------------------
# UpdateService
# ---------------------------------------------------------------------------


class FakeHost:
    def __init__(self, root: Path, restart_ok: bool = True) -> None:
        self.root = root
        self.restart_ok = restart_ok
        self.toasts: list[tuple[str, str]] = []
        self.restarts = 0

    def addin_dir(self) -> str:
        return str(self.root)

    def restart_addin(self) -> bool:
        self.restarts += 1
        return self.restart_ok

    def toast(self, message: str, level: str = "info", title: str = "MoxaSerial") -> None:
        self.toasts.append((level, message))


@pytest.fixture
def service(installed):
    from moxaserial.config import ConfigStore

    pushes: list[tuple[str, dict]] = []
    host = FakeHost(installed)
    svc = UpdateService(
        store=ConfigStore(), push=lambda a, p: pushes.append((a, p)), host=host
    )
    svc.pushes = pushes  # type: ignore[attr-defined]
    svc.host_obj = host  # type: ignore[attr-defined]
    return svc


def test_service_reads_the_installed_manifest(service):
    assert service.current_version() == "0.1.0"


def test_service_defaults(service):
    cfg = service.settings()
    assert cfg["auto_check"] is True
    assert cfg["auto_install"] is False
    assert cfg["include_prereleases"] is False
    assert cfg["check_interval_hours"] == 24
    assert cfg["repo"] == "npolanosky/MoxaSerial"


def test_service_due_respects_the_interval(service):
    import time

    assert service.due() is True  # never checked
    service.store.update_globals({"update": {"last_check": time.time()}})
    assert service.due() is False
    service.store.update_globals({"update": {"check_interval_hours": 0}})
    assert service.due() is True  # 0 means every start


def test_service_check_persists_bookkeeping_and_pushes(monkeypatch, service):
    serve(monkeypatch, {"/releases/latest": release("0.2.0")})
    service.store.update_globals({"update": {"repo": "o/r"}})

    result = service.check(force=True)

    assert result["available"] is True
    cfg = service.settings()
    assert cfg["last_check"] > 0
    assert cfg["last_seen_version"] == "0.2.0"
    assert cfg["last_error"] == ""
    assert cfg["repo"] == "o/r"  # the partial save did not reset it
    actions = [a for a, _ in service.pushes]
    assert "update.checked" in actions and "update.available" in actions
    assert any("0.2.0" in m for _, m in service.host_obj.toasts)


def test_service_check_records_an_error(monkeypatch, service):
    serve(monkeypatch, {"/releases/latest": URLError("offline")})
    service.store.update_globals({"update": {"repo": "o/r"}})
    service.check(force=True)
    assert "offline" in service.settings()["last_error"]
    assert [a for a, _ in service.pushes] == ["update.checked"]


def test_service_check_honours_auto_check_off(service):
    service.store.update_globals({"update": {"auto_check": False}})
    # No urlopen patch at all: reaching the network here would be the failure.
    assert service.check(force=False)["skipped"] == "auto-check is off"


def test_service_does_not_schedule_a_startup_check_when_disabled(service):
    service.store.update_globals({"update": {"auto_check": False}})
    assert service.schedule_startup_check(delay=0.0) is False


def test_service_does_not_schedule_when_not_due(service):
    import time

    service.store.update_globals({"update": {"last_check": time.time()}})
    assert service.schedule_startup_check(delay=0.0) is False


def test_service_status_reports_the_development_install(service, installed):
    assert service.status()["developmentInstall"] is False
    (installed / ".git").mkdir()
    assert service.status()["developmentInstall"] is True


def test_service_install_end_to_end(monkeypatch, service, tmp_path, installed):
    blob = _served_zip(tmp_path)
    digest = hashlib.sha256(blob).hexdigest()
    serve(
        monkeypatch,
        {
            "/releases/latest": release("0.2.0"),
            "MoxaSerial-0.2.0.zip": blob,
            "SHA256SUMS": f"{digest}  MoxaSerial-0.2.0.zip\n".encode(),
        },
    )
    service.store.update_globals({"update": {"repo": "o/r"}})
    result = service.check(force=True)

    service.install_now(result)

    assert manifest_version(installed) == "0.2.0"
    actions = [a for a, _ in service.pushes]
    assert "update.done" in actions
    assert "update.error" not in actions
    stages = [p["stage"] for a, p in service.pushes if a == "update.progress"]
    assert "download" in stages and "install" in stages and "restart" in stages
    assert service.host_obj.restarts == 1


def test_service_install_warns_when_the_restart_is_unavailable(monkeypatch, service, tmp_path):
    blob = _served_zip(tmp_path)
    serve(
        monkeypatch,
        {"/releases/latest": release("0.2.0"), "MoxaSerial-0.2.0.zip": blob},
    )
    service.host_obj.restart_ok = False
    service.store.update_globals({"update": {"repo": "o/r"}})
    service.install_now(service.check(force=True))
    assert any("Restart Fusion" in m for _, m in service.host_obj.toasts)
    restart = [p for a, p in service.pushes if a == "update.restart"]
    assert restart and restart[0]["restarted"] is False


def test_service_install_reports_a_failure_instead_of_raising(monkeypatch, service, tmp_path):
    serve(
        monkeypatch,
        {"/releases/latest": release("0.2.0"), "MoxaSerial-0.2.0.zip": b"not a zip"},
    )
    service.store.update_globals({"update": {"repo": "o/r"}})
    service.install_now(service.check(force=True))
    errors = [p["message"] for a, p in service.pushes if a == "update.error"]
    assert errors and "zip" in errors[0]
    assert service.host_obj.restarts == 0


def test_service_install_refuses_a_development_install(monkeypatch, service, installed, tmp_path):
    (installed / ".git").mkdir()
    serve(
        monkeypatch,
        {"/releases/latest": release("0.2.0"), "MoxaSerial-0.2.0.zip": _served_zip(tmp_path)},
    )
    service.store.update_globals({"update": {"repo": "o/r"}})
    service.install_now(service.check(force=True))
    errors = [p["message"] for a, p in service.pushes if a == "update.error"]
    assert errors and "git pull" in errors[0]
    assert manifest_version(installed) == "0.1.0"


def test_service_install_says_so_when_there_is_nothing_to_install(monkeypatch, service):
    serve(monkeypatch, {"/releases/latest": release("0.1.0")})
    service.store.update_globals({"update": {"repo": "o/r"}})
    service.install_now(None)
    errors = [p["message"] for a, p in service.pushes if a == "update.error"]
    assert errors and "No update is available" in errors[0]
