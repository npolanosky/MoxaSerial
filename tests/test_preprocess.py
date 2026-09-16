"""Preprocessing pipeline - pure functions, no threads, no I/O."""

from __future__ import annotations

import pytest

from moxaserial.dnc.preprocess import (
    PreprocessOptions,
    preprocess,
    preprocess_file,
    program_name,
    renumber,
    split_lines,
    strip_comments,
    strip_spaces,
    transform_line,
    unescape,
)

# -- unescape ---------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ""),
        ("%", "%"),
        (r"\n", "\n"),
        (r"\r\n", "\r\n"),
        (r"\t", "\t"),
        (r"\0", "\0"),
        (r"\x12", "\x12"),
        (r"\xFF", "\xff"),
        (r"\\n", "\\n"),
        (r"%\n", "%\n"),
    ],
)
def test_unescape(raw, expected):
    assert unescape(raw) == expected


# -- split_lines ------------------------------------------------------------

def test_split_lines_handles_all_line_endings():
    assert split_lines("a\nb\r\nc\rd") == ["a", "b", "c", "d"]


def test_split_lines_trailing_newline_is_not_a_line():
    assert split_lines("a\nb\n") == ["a", "b"]
    assert split_lines("") == []


def test_split_lines_keeps_interior_blank_lines():
    assert split_lines("a\n\nb") == ["a", "", "b"]


# -- comments ---------------------------------------------------------------

@pytest.mark.parametrize(
    "line,expected",
    [
        ("G0 X1 (rapid) Y2", "G0 X1  Y2"),
        ("(header only)", ""),
        ("G0 X1 ; trailing", "G0 X1 "),
        ("G0 X1 (unterminated", "G0 X1 "),
        ("(a;b) G1", " G1"),
        ("no comment", "no comment"),
    ],
)
def test_strip_comments(line, expected):
    assert strip_comments(line) == expected


def test_strip_spaces_removes_tabs_too():
    assert strip_spaces("  G0\tX1 Y2 ") == "G0X1Y2"


# -- per-line transform -----------------------------------------------------

def test_transform_line_default_rstrips_only():
    opts = PreprocessOptions(uppercase=False)
    assert transform_line("  g0 x1   ", opts) == "  g0 x1"


def test_transform_line_uppercases():
    assert transform_line("g0 x1", PreprocessOptions(uppercase=True)) == "G0 X1"


def test_transform_line_order_comments_then_spaces_then_case():
    opts = PreprocessOptions(uppercase=True, strip_comments=True, strip_spaces=True)
    assert transform_line("g0 x1 (feed move) y2", opts) == "G0X1Y2"


# -- renumber ---------------------------------------------------------------

def test_renumber_replaces_existing_numbers():
    assert renumber(["N5 G0", "N99 G1"], 10, 10, "N", 0) == ["N10 G0", "N20 G1"]


def test_renumber_inserts_when_absent_and_pads():
    assert renumber(["G0", "G1"], 1, 1, "N", 4) == ["N0001G0", "N0002G1"]


def test_renumber_leaves_blank_lines_alone_and_does_not_consume_a_number():
    assert renumber(["G0", "", "G1"], 1, 1, "N", 0) == ["N1G0", "", "N2G1"]


# -- whole pipeline ---------------------------------------------------------

def test_pipeline_default_options():
    opts = PreprocessOptions(start_chars="", end_chars="")
    result = preprocess("g0 x1\n\ng1 y2\n", opts)
    assert result.lines == ["G0 X1", "G1 Y2"]
    assert result.payload == b"G0 X1\r\nG1 Y2\r\n"
    assert result.source_lines == 3
    assert result.dropped_lines == 1
    assert result.line_count == 2
    assert result.byte_count == len(result.payload)


def test_pipeline_line_endings():
    for ending, eol in (("LF", b"\n"), ("CR", b"\r"), ("CRLF", b"\r\n")):
        opts = PreprocessOptions(line_ending=ending, start_chars="", end_chars="")
        assert preprocess("G0", opts).payload == b"G0" + eol


def test_pipeline_start_and_end_chars_are_unescaped_and_wrap_the_file():
    opts = PreprocessOptions(start_chars=r"%\n", end_chars=r"%\n", line_ending="LF")
    result = preprocess("G0", opts)
    assert result.payload == b"%\nG0\n%\n"
    assert result.prologue == b"%\n"
    assert result.epilogue == b"%\n"
    # The wrapper is not part of the previewed line list.
    assert result.lines == ["G0"]


def test_pipeline_leading_trailing_and_eob_apply_per_line():
    opts = PreprocessOptions(
        leading_chars=">", trailing_chars="<", eob_chars=r"\x17",
        line_ending="LF", start_chars="", end_chars="",
    )
    result = preprocess("A\nB", opts)
    assert result.lines == [">A<", ">B<"]
    assert result.payload == b">A<\x17\n>B<\x17\n"


def test_pipeline_blank_line_removal_happens_after_comment_stripping():
    opts = PreprocessOptions(
        strip_comments=True, strip_blank_lines=True, start_chars="", end_chars=""
    )
    result = preprocess("G0\n(just a comment)\nG1", opts)
    assert result.lines == ["G0", "G1"]
    assert result.dropped_lines == 1


def test_pipeline_keeps_blank_lines_when_asked():
    opts = PreprocessOptions(strip_blank_lines=False, start_chars="", end_chars="")
    assert preprocess("A\n\nB", opts).lines == ["A", "", "B"]


def test_pipeline_line_blobs_line_up_with_lines():
    opts = PreprocessOptions(start_chars="%", end_chars="%", line_ending="LF")
    result = preprocess("A\nB\nC", opts)
    assert len(result.line_blobs) == len(result.lines) == 3
    assert b"".join(result.line_blobs) == b"A\nB\nC\n"
    assert result.payload == result.prologue + b"".join(result.line_blobs) + result.epilogue


def test_pipeline_renumbering_end_to_end():
    opts = PreprocessOptions(
        line_numbers=True, line_number_start=100, line_number_increment=5,
        line_number_prefix="N", start_chars="", end_chars="",
    )
    assert preprocess("N1 G0\nG1", opts).lines == ["N100 G0", "N105G1"]


def test_pipeline_non_ascii_is_replaced_not_fatal():
    opts = PreprocessOptions(start_chars="", end_chars="")
    result = preprocess("G0 (café)", opts)
    assert result.byte_count > 0  # encodes with errors="replace"


def test_from_machine_reads_the_send_section():
    machine = {"send": {"uppercase": False, "line_ending": "LF", "unknown_key": 1}}
    opts = PreprocessOptions.from_machine(machine)
    assert opts.uppercase is False
    assert opts.line_ending == "LF"


def test_preprocess_file_strips_bom(tmp_path):
    path = tmp_path / "bom.nc"
    path.write_bytes("﻿G0 X1\n".encode())
    opts = PreprocessOptions(start_chars="", end_chars="")
    assert preprocess_file(str(path), opts).lines == ["G0 X1"]


# -- program_name -----------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("%\nO1234 (PART)\n", "O1234"),
        ("(BRACKET OP1)\nG0\n", "BRACKET_OP1"),
        ("G0 X1\nG1 Y2\n", ""),
    ],
)
def test_program_name(text, expected):
    assert program_name(text) == expected


def test_start_and_end_markers_are_not_doubled():
    """Fusion posts already begin and end with '%'; a second leading '%' is
    end-of-record to a Fanuc and yields an empty program."""
    from moxaserial.dnc.preprocess import PreprocessOptions, preprocess

    opts = PreprocessOptions(start_chars="%\\n", end_chars="%\\n", line_ending="LF")
    posted = preprocess("%\nO1042\nG0 X0\nM30\n%\n", opts)
    assert posted.payload == b"%\nO1042\nG0 X0\nM30\n%\n"
    assert posted.prologue == b"" and posted.epilogue == b""
    bare = preprocess("O1042\nG0 X0\nM30\n", opts)
    assert bare.payload == b"%\nO1042\nG0 X0\nM30\n%\n"
    assert bare.prologue == b"%\n" and bare.epilogue == b"%\n"
