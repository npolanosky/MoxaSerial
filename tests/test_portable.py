"""Settings export / import (moxaserial/portable.py)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from moxaserial import portable  # noqa: E402
from moxaserial.config import ConfigStore, ValidationError, default_machine  # noqa: E402

HERE_WIN = os.name == "nt"


@pytest.fixture
def store(tmp_path):
    s = ConfigStore(tmp_path / "settings.json")
    m = default_machine("KIA lathe", "moxa")
    m["host"] = "192.0.2.5"
    m["receive"]["folder"] = str(Path.home() / "Documents" / "NC" / "in")
    m["send"]["default_folder"] = str(Path.home() / "Documents" / "NC" / "out")
    s.upsert_machine(m)
    s.update_globals({"theme": "light", "watch_folders": [str(Path.home() / "posted")]})
    return s


# -- paths --------------------------------------------------------------------
def test_path_style():
    assert portable.path_style(r"C:\Users\a\x") == "windows"
    assert portable.path_style(r"\\nas\share\nc") == "windows"
    assert portable.path_style("/Users/a/x") == "posix"
    assert portable.path_style("~/nc") == "home"
    assert portable.path_style("") == "relative"


def test_rehome_from_windows_home():
    got = portable.rehome(r"C:\Users\Alice\Documents\NC", r"C:\Users\Alice", True)
    assert got == str(Path.home() / "Documents" / "NC")
    assert portable.rehome(r"C:\Users\Alice", r"C:\Users\Alice", True) == str(Path.home())
    assert portable.rehome(r"D:\Jobs\NC", r"C:\Users\Alice", True) is None
    assert portable.rehome(r"C:\Users\Alice\..\Bob\x", r"C:\Users\Alice", True) is None


def test_rehome_from_posix_home_case_rules():
    assert portable.rehome("/Users/alice/nc", "/Users/alice", False) == str(Path.home() / "nc")
    # Linux homes are case-sensitive; macOS (APFS) folds case.
    assert portable.rehome("/Users/Alice/nc", "/Users/alice", False) is None
    assert portable.rehome("/Users/Alice/nc", "/Users/alice", False, fold_case=True) == str(Path.home() / "nc")
    notes: list[str] = []
    got = portable.convert_path("/Users/Alice/nc", "darwin", "/Users/alice", fallback="x", label="R", notes=notes)
    assert got == str(Path.home() / "nc")


def test_forward_slash_unc_is_a_windows_path():
    assert portable.path_style("//nas/share/nc") == "windows"
    notes: list[str] = []
    here = portable.current_platform()
    if HERE_WIN:
        assert portable.convert_path("//nas/share/nc", here, r"C:\Users\a", fallback="x", label="R", notes=notes) == "//nas/share/nc"
    else:
        assert portable.convert_path("//nas/share/nc", "win32", r"C:\Users\a", fallback="x", label="R", notes=notes) == "x"


def test_unknown_platform_is_inferred_from_the_home_path():
    notes: list[str] = []
    got = portable.convert_path(r"C:\Users\Alice\NC", "", r"C:\Users\alice", fallback="x", label="R", notes=notes)
    assert got == str(Path.home() / "NC")


def test_convert_path_cross_platform_falls_back():
    notes: list[str] = []
    foreign = "/Volumes/NAS/nc" if HERE_WIN else r"D:\Jobs\NC"
    src = "darwin" if HERE_WIN else "win32"
    got = portable.convert_path(foreign, src, "", fallback="/tmp/x", label="Receive", notes=notes)
    assert got == "/tmp/x" and notes and "another computer" in notes[0]
    notes.clear()
    got = portable.convert_path(foreign, src, "", fallback="", label="Watch", notes=notes)
    assert got == "" and "removed" in notes[0]


def test_convert_path_same_platform_outside_home_is_kept():
    notes: list[str] = []
    here = portable.current_platform()
    path = r"\\nas\share\nc" if HERE_WIN else "/Volumes/NAS/nc"
    assert portable.convert_path(path, here, "", fallback="x", label="R", notes=notes) == path
    assert notes == []


def test_convert_serial_device():
    notes: list[str] = []
    if HERE_WIN:
        assert portable.convert_serial_device("COM3", "win32", notes, "M") == "COM3"
        assert portable.convert_serial_device("/dev/cu.usbserial", "darwin", notes, "M") == ""
    else:
        assert portable.convert_serial_device("/dev/cu.usbserial", "darwin", notes, "M") == "/dev/cu.usbserial"
        assert portable.convert_serial_device("COM3", "win32", notes, "M") == ""
    assert len(notes) == 1 and "another computer" in notes[0]


# -- export ---------------------------------------------------------------------
def test_export_all_strips_update_bookkeeping(store):
    env = portable.export_all(store.data)
    assert env["format"] == portable.FORMAT and env["scope"] == "all"
    assert "last_check" not in env["settings"]["update"]
    assert env["settings"]["theme"] == "light"
    assert any(m["name"] == "KIA lathe" for m in env["settings"]["machines"])
    json.dumps(env)  # serialisable


def test_export_machine(store):
    m = next(m for m in store.machines() if m["name"] == "KIA lathe")
    env = portable.export_machine(m)
    assert env["scope"] == "machine" and env["machine"]["id"] == m["id"]
    assert portable.suggested_file_name("machine", m).startswith("MoxaSerial-KIA_lathe-")


# -- import ---------------------------------------------------------------------
def test_parse_envelope_rejects_junk():
    with pytest.raises(ValidationError):
        portable.parse_envelope([1, 2])
    with pytest.raises(ValidationError):
        portable.parse_envelope({"format": "other"})
    with pytest.raises(ValidationError):
        portable.parse_envelope({"format": portable.FORMAT, "format_version": 99, "scope": "all", "settings": {}})
    with pytest.raises(ValidationError):
        portable.parse_envelope({"format": portable.FORMAT, "scope": "all"})


def test_import_machine_updates_by_id_and_renames_a_name_clash(store):
    m = next(m for m in store.machines() if m["name"] == "KIA lathe")
    env = portable.export_machine(m)
    env["machine"]["host"] = "192.0.2.19"
    new, report = portable.apply_import(store.data, env)
    assert report.updated == ["KIA lathe"] and report.added == []
    assert next(x for x in new["machines"] if x["id"] == m["id"])["host"] == "192.0.2.19"

    env["machine"]["id"] = "abcdef123456"
    new, report = portable.apply_import(store.data, env)
    assert report.added == ["KIA lathe (2)"]
    assert any(x["name"] == "KIA lathe (2)" for x in new["machines"])


def test_import_all_merge_keeps_existing_and_copies_globals(store):
    other = ConfigStore(store.path.parent / "other.json")
    o = default_machine("Haas mill", "moxa")
    o["host"] = "192.0.2.7"
    other.upsert_machine(o)
    other.update_globals({"theme": "dark", "confirm_before_send": True})
    env = portable.export_all(other.data)

    new, report = portable.apply_import(store.data, env, "merge")
    names = {m["name"] for m in new["machines"]}
    assert {"KIA lathe", "Haas mill", "Simulator"} <= names
    assert report.added == ["Haas mill"]
    assert new["theme"] == "dark" and new["confirm_before_send"] is True
    assert str(Path.home() / "posted") in new["watch_folders"]
    # A settings file must not be able to redirect the updater.
    env["settings"]["update"]["repo"] = "evil/repo"
    env["settings"]["update"]["auto_install"] = True
    env["settings"]["update"]["include_prereleases"] = True
    new, _ = portable.apply_import(store.data, env, "merge")
    assert new["update"]["repo"] == store.data["update"]["repo"]
    assert new["update"]["auto_install"] is False
    assert new["update"]["include_prereleases"] is True

    new, report = portable.apply_import(store.data, env, "replace")
    names = {m["name"] for m in new["machines"]}
    assert "KIA lathe" not in names and "Haas mill" in names and "Simulator" in names


def test_import_rehomes_paths_from_another_computer(store):
    m = default_machine("Remote", "moxa")
    m["host"] = "192.0.2.11"
    env = portable.export_machine(m)
    if HERE_WIN:
        env["platform"], env["home"] = "darwin", "/Users/alice"
        env["machine"]["receive"]["folder"] = "/Users/alice/Documents/NC/in"
        env["machine"]["send"]["default_folder"] = "/Volumes/NAS/out"
        env["machine"]["serial_device"] = "/dev/cu.usbserial-1"
    else:
        env["platform"], env["home"] = "win32", r"C:\Users\Alice"
        env["machine"]["receive"]["folder"] = r"C:\Users\Alice\Documents\NC\in"
        env["machine"]["send"]["default_folder"] = r"D:\Jobs\out"
        env["machine"]["serial_device"] = "COM7"
    new, report = portable.apply_import(store.data, env)
    got = next(x for x in new["machines"] if x["name"] == "Remote")
    assert got["receive"]["folder"] == str(Path.home() / "Documents" / "NC" / "in")
    assert got["send"]["default_folder"] == ""
    assert got["serial_device"] == ""
    assert len(report.notes) == 3


def test_import_all_migrates_an_older_schema(store):
    env = portable.export_all(store.data)
    env["settings"]["schema_version"] = 1
    new, report = portable.apply_import(store.data, env, "merge")
    assert any("Migrated" in n for n in report.notes)
    assert new["schema_version"] == store.data["schema_version"]


def test_summarize(store):
    env = portable.export_all(store.data)
    s = portable.summarize(env)
    assert s["scope"] == "all" and s["machines"] == ["KIA lathe"] and s["crossPlatform"] is False
