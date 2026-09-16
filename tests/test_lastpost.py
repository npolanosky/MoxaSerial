"""Last-posted-file resolution (the Fusion-free half of moxaserial/ui/lastpost.py)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from moxaserial.ui import lastpost


@pytest.mark.parametrize(
    "name,expected",
    [
        ("prog.nc", True),
        ("prog.NC", True),
        ("prog.tap", True),
        ("prog.h", True),
        ("prog.cps", False),
        ("prog.nc.failed", False),
        ("prog.log", False),
        (".hidden.nc", False),
        ("prog", False),
    ],
)
def test_is_nc_file(tmp_path, name, expected):
    assert lastpost.is_nc_file(tmp_path / name) is expected


def test_newest_in_folders_sorts_newest_first(tmp_path):
    older = tmp_path / "old.nc"
    newer = tmp_path / "new.nc"
    older.write_text("a")
    newer.write_text("b")
    os.utime(older, (time.time() - 600, time.time() - 600))

    found = lastpost.newest_in_folders([str(tmp_path)])
    assert [f["name"] for f in found] == ["new.nc", "old.nc"]
    assert found[0]["source"] == "folder-scan"
    assert found[0]["mtimeText"]


def test_newest_in_folders_skips_missing_folders_and_non_nc_files(tmp_path):
    (tmp_path / "keep.nc").write_text("a")
    (tmp_path / "skip.cps").write_text("b")
    found = lastpost.newest_in_folders([str(tmp_path), "/definitely/not/a/folder"])
    assert [f["name"] for f in found] == ["keep.nc"]


def test_newest_in_folders_deduplicates_across_folders(tmp_path):
    (tmp_path / "one.nc").write_text("a")
    found = lastpost.newest_in_folders([str(tmp_path), str(tmp_path)])
    assert len(found) == 1


def test_newest_in_folders_respects_the_limit(tmp_path):
    for i in range(6):
        (tmp_path / f"p{i}.nc").write_text("x")
    assert len(lastpost.newest_in_folders([str(tmp_path)], limit=3)) == 3


def test_nc_program_candidates_is_empty_without_fusion():
    files, folders = lastpost.nc_program_candidates()
    assert files == []
    assert folders == []


def test_find_last_posted_uses_the_extra_folders(tmp_path):
    target = tmp_path / "posted"
    target.mkdir()
    (target / "1001.nc").write_text("%\nO1001\n%\n")
    result = lastpost.find_last_posted([str(target)])
    assert result["name"] == "1001.nc"
    assert result["source"] == "folder-scan"
    assert result["candidates"]


def test_find_last_posted_explains_itself_when_nothing_is_found(tmp_path):
    result = lastpost.find_last_posted([str(tmp_path / "empty")])
    assert result["path"] == ""
    assert "No posted NC file found" in result["error"]
    assert "Looked in" in result["error"]


def test_find_last_posted_picks_the_newest_across_folders(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "old.nc").write_text("x")
    (b / "new.nc").write_text("y")
    os.utime(a / "old.nc", (time.time() - 300, time.time() - 300))
    assert lastpost.find_last_posted([str(a), str(b)])["name"] == "new.nc"


def test_fusion_default_nc_folder_is_under_the_home_directory():
    folder = lastpost.fusion_default_nc_folder()
    assert str(Path.home()) in folder
    assert folder.endswith("NC Programs")
