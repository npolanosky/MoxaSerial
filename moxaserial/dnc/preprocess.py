"""NC file preprocessing pipeline.

Pure functions only - no I/O, no threads, no globals - so the whole
pipeline is trivially unit-testable and can be previewed in the UI before
a single byte goes out on the wire.

Pipeline order (matching the order CIMCO's DNC applies them, so a shop
migrating settings gets the same bytes: skip/trigger -> omit -> filter
chars -> case -> whitespace -> line ending):

1. decode the file and split on any of CR, LF, CRLF
2. bound the program with the *start* / *end* triggers
   (``TRAN_STARTTRIG`` / ``TRAN_ENDTRIG``; the end-trigger line is *not* sent)
3. drop lines containing any *omit* character (``TRAN_OMMITLINES``)
4. per line: remove ASCII 0's -> remove characters -> tabs to spaces ->
   strip comments -> strip spaces -> uppercase
5. per line: prepend *leading chars*, append *trailing chars*
6. drop blank lines (evaluated *after* the strips above)
7. renumber lines (optional), replacing any existing ``N`` word
8. append the *EOB* string to every line
9. join with the configured line ending, wrapped in *start*/*end* chars

Character fields (``start_chars``, ``end_chars``, ``eob_chars``,
``leading_chars``, ``trailing_chars``, ``remove_chars``,
``omit_lines_containing``, ``line_ending_custom``) accept escapes:

* ``\\n \\r \\t \\0 \\\\`` and ``\\xNN`` - our own long-standing forms;
* ``\\NN`` **decimal**, which is CIMCO's convention, so an operator can
  type ``\\17``, ``\\19``, ``\\35`` or ``\\13 \\10`` exactly as they would
  in CIMCO Edit. One space directly after a decimal escape is treated as a
  separator and dropped (``\\13 \\10`` is CR+LF, not CR+space+LF), which is
  how CIMCO's own CR/LF combo entries read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

LINE_ENDINGS = {"LF": "\n", "CR": "\r", "CRLF": "\r\n"}

_SPLIT_RE = re.compile(r"\r\n|\r|\n")
_PAREN_COMMENT_RE = re.compile(r"\([^)]*\)?")
# Only the N-word itself is consumed - whatever separator followed it in the
# source is preserved, so renumbering never inserts or removes spaces.
_LINE_NUMBER_RE = re.compile(r"^\s*[Nn]\d+")
# ``\xNN`` hex, ``\NN`` decimal (CIMCO's convention, optionally followed by a
# single separating space), or one of the C-style letter escapes. ``\0`` is
# matched by the decimal branch and still yields NUL.
_ESCAPE_RE = re.compile(r"\\(?:x([0-9a-fA-F]{2})|(\d{1,3})\ ?|([nrt\\]))")


# --------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------

@dataclass
class PreprocessOptions:
    """Everything the pipeline needs, decoupled from the settings schema."""

    line_ending: str = "CRLF"
    line_ending_custom: str = ""
    uppercase: bool = True
    strip_comments: bool = False
    strip_blank_lines: bool = True
    strip_spaces: bool = False
    leading_chars: str = ""
    trailing_chars: str = ""
    start_chars: str = ""
    end_chars: str = ""
    eob_chars: str = ""
    line_numbers: bool = False
    line_number_start: int = 1
    line_number_increment: int = 1
    line_number_prefix: str = "N"
    line_number_digits: int = 0
    # -- CIMCO transmit filters ------------------------------------------
    start_trigger: str = ""
    end_trigger: str = ""
    omit_lines_containing: str = ""
    remove_chars: str = ""
    remove_nulls: bool = True
    tabs_to_spaces: bool = False
    encoding: str = "ascii"

    @classmethod
    def from_machine(cls, machine: dict[str, Any]) -> PreprocessOptions:
        s = dict(machine.get("send", {}))
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in s.items() if k in known})

    @property
    def eol(self) -> str:
        """The bytes that terminate every line.

        ``CUSTOM`` is CIMCO's editable CR/LF combo: any sequence, entered
        with the ``\\NN`` convention (``\\13 \\10 \\10``, ``\\10 \\13`` ...),
        which is also how "Send files with non-standard CR/LF" is done.
        """
        key = str(self.line_ending).upper()
        if key == "CUSTOM":
            return unescape(self.line_ending_custom)
        return LINE_ENDINGS.get(key, "\r\n")


@dataclass
class PreprocessResult:
    """Output of :func:`preprocess`."""

    lines: list[str] = field(default_factory=list)
    """Processed lines, without line endings (for the UI preview)."""

    payload: bytes = b""
    """Exactly the bytes that go on the wire, start/end chars included."""

    line_blobs: list[bytes] = field(default_factory=list)
    """Per-line byte blobs *including* the line ending, in send order."""

    prologue: bytes = b""
    """Start-of-file characters, sent before ``line_blobs``."""

    epilogue: bytes = b""
    """End-of-file characters, sent after ``line_blobs``."""

    source_lines: int = 0
    dropped_lines: int = 0

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def byte_count(self) -> int:
        return len(self.payload)


# --------------------------------------------------------------------------
# Small pure helpers - each is independently tested
# --------------------------------------------------------------------------

def unescape(text: str) -> str:
    """Expand ``\\n \\r \\t \\\\``, ``\\xNN`` hex and ``\\NN`` decimal.

    The decimal form is CIMCO's ("enter the ASCII value like this ``\\36``").
    Values above 255 are clamped. A single space immediately after a decimal
    escape separates two escapes rather than being literal, so CIMCO's
    ``\\13 \\10`` yields exactly CR LF.
    """
    if not text:
        return ""

    def _sub(m: re.Match[str]) -> str:
        hex_digits, dec_digits, letter = m.group(1), m.group(2), m.group(3)
        if hex_digits is not None:
            return chr(int(hex_digits, 16))
        if dec_digits is not None:
            return chr(min(int(dec_digits), 255))
        return {"n": "\n", "r": "\r", "t": "\t", "\\": "\\"}[letter]

    return _ESCAPE_RE.sub(_sub, text)


def char_set(text: str) -> set[str]:
    """A character *set* field (CIMCO ``TRAN_OMMITLINES`` / ``TRAN_REMCHAR``).

    Characters may be juxtaposed (``$%``) or written as escapes
    (``\\36 \\37``). *Literal* whitespace between entries is a separator and
    never joins the set; ``\\32`` is how you ask for an actual space.
    """
    if not text:
        return set()
    out: set[str] = set()
    pos = 0
    for m in _ESCAPE_RE.finditer(text):
        out.update(ch for ch in text[pos : m.start()] if not ch.isspace())
        out.add(unescape(m.group(0)))
        pos = m.end()
    out.update(ch for ch in text[pos:] if not ch.isspace())
    return out


def split_lines(text: str) -> list[str]:
    """Split on CR, LF or CRLF; a trailing newline does not add an empty line."""
    if text == "":
        return []
    parts = _SPLIT_RE.split(text)
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def strip_comments(line: str) -> str:
    """Remove ``( ... )`` comments and everything after a ``;``.

    An unterminated ``(`` swallows the rest of the line, which is what a
    control does. Semicolons inside parentheses are treated as comment
    text, not as a comment start.
    """
    out = _PAREN_COMMENT_RE.sub("", line)
    semi = out.find(";")
    if semi >= 0:
        out = out[:semi]
    return out


def strip_spaces(line: str) -> str:
    """Remove every space and tab (leading, trailing and interior)."""
    return line.replace(" ", "").replace("\t", "")


def renumber(lines: list[str], start: int, increment: int, prefix: str, digits: int) -> list[str]:
    """Replace/insert a leading line number on every non-empty line."""
    out: list[str] = []
    n = start
    for line in lines:
        body = _LINE_NUMBER_RE.sub("", line)
        if not body.strip():
            out.append(line)
            continue
        num = str(n).zfill(digits) if digits > 0 else str(n)
        out.append(f"{prefix}{num}{body}")
        n += increment
    return out


def apply_triggers(lines: list[str], start_trigger: str, end_trigger: str) -> list[str]:
    """Bound *lines* by CIMCO's transmit start / end triggers.

    The start trigger selects the **first line containing it**, and that
    line *is* sent. The end trigger selects the first line containing it at
    or after the start, and that line is **not** sent (CIMCO: "the line
    containing the end trigger is not transmitted"). A trigger that never
    matches is ignored rather than producing an empty transmission.
    """
    first = 0
    if start_trigger:
        for i, line in enumerate(lines):
            if start_trigger in line:
                first = i
                break
        else:
            return list(lines)
    last = len(lines)
    if end_trigger:
        for i in range(first, len(lines)):
            if end_trigger in lines[i]:
                last = i
                break
    return lines[first:last]


def transform_line(line: str, opts: PreprocessOptions) -> str:
    """Apply the per-line transforms in pipeline order."""
    out = line
    if opts.remove_nulls:
        out = out.replace("\0", "")
    removals = char_set(opts.remove_chars)
    if removals:
        out = "".join(ch for ch in out if ch not in removals)
    if opts.tabs_to_spaces:
        out = out.replace("\t", " ")
    if opts.strip_comments:
        out = strip_comments(out)
    out = strip_spaces(out) if opts.strip_spaces else out.rstrip(" \t")
    if opts.uppercase:
        out = out.upper()
    return out


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------

def preprocess(text: str, opts: PreprocessOptions) -> PreprocessResult:
    """Run the full pipeline over *text*."""
    raw = split_lines(text)
    lead = unescape(opts.leading_chars)
    trail = unescape(opts.trailing_chars)
    eob = unescape(opts.eob_chars)
    omit = char_set(opts.omit_lines_containing)

    bounded = apply_triggers(raw, opts.start_trigger, opts.end_trigger)
    dropped = len(raw) - len(bounded)

    processed: list[str] = []
    for line in bounded:
        if omit and any(ch in omit for ch in line):
            dropped += 1
            continue
        body = transform_line(line, opts)
        if opts.strip_blank_lines and not body.strip():
            dropped += 1
            continue
        processed.append(body)

    if opts.line_numbers:
        processed = renumber(
            processed,
            opts.line_number_start,
            max(1, opts.line_number_increment),
            opts.line_number_prefix,
            opts.line_number_digits,
        )

    processed = [f"{lead}{line}{trail}" for line in processed]

    eol = opts.eol
    enc = opts.encoding or "ascii"
    blobs = [(line + eob + eol).encode(enc, errors="replace") for line in processed]
    prologue = unescape(opts.start_chars).encode(enc, errors="replace")
    epilogue = unescape(opts.end_chars).encode(enc, errors="replace")
    # Never double a marker the program already carries. Fusion posts begin
    # and end with "%"; a Fanuc reads a second leading "%" as end-of-record
    # and stores an empty program.
    if prologue and blobs and blobs[0].strip() == prologue.strip():
        prologue = b""
    if epilogue and blobs and blobs[-1].strip() == epilogue.strip():
        epilogue = b""

    return PreprocessResult(
        lines=processed,
        payload=prologue + b"".join(blobs) + epilogue,
        line_blobs=blobs,
        prologue=prologue,
        epilogue=epilogue,
        source_lines=len(raw),
        dropped_lines=dropped,
    )


def preprocess_file(path: str, opts: PreprocessOptions) -> PreprocessResult:
    """Read *path* (tolerating stray high bytes) and run :func:`preprocess`."""
    with open(path, "rb") as fh:
        raw = fh.read()
    text = raw.decode("utf-8", errors="replace").lstrip("﻿")
    return preprocess(text, opts)


def preview(text: str, opts: PreprocessOptions, limit: int = 40) -> list[str]:
    """First *limit* processed lines - what the Send page shows before start."""
    return preprocess(text, opts).lines[:limit]


def program_name(text: str) -> str:
    """Best-effort program identifier from NC text (``O1234`` or first comment)."""
    for line in split_lines(text)[:40]:
        m = re.search(r"\bO\s*(\d{1,6})", line, re.IGNORECASE)
        if m:
            return f"O{m.group(1)}"
        m = re.search(r"\(([^)]{1,40})\)", line)
        if m:
            cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", m.group(1).strip())
            if cleaned:
                return cleaned
    return ""
