#!/usr/bin/env python3
"""Regenerate the toolbar PNGs in resources/<command>/.

Stdlib only - no Pillow - because the add-in must stay dependency-free and
the icons are simple enough to rasterise by hand. Each icon is described as
a function of normalised (0..1) coordinates, so one drawing renders at 16,
32 and 64 px without resampling artefacts.

    python3 tools/make_icons.py
"""

import pathlib
import struct
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent / "resources"
ACCENT = (0x2E, 0x7D, 0xB8, 255)
DARK   = (0x33, 0x38, 0x3D, 255)
GREEN  = (0x3D, 0x9A, 0x66, 255)
AMBER  = (0xC8, 0x8A, 0x2A, 255)


def png(path, size, draw):
    rows = []
    for y in range(size):
        row = bytearray([0])  # filter type 0
        for x in range(size):
            row += bytes(draw(x, y, size))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    blob = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)


def scaled(x, y, size):
    """Normalised 0..1 coordinates so one drawing works at any size."""
    return x / size, y / size


def send_icon(color=ACCENT):
    def draw(x, y, size):
        u, v = scaled(x, y, size)
        # A connector body on the left, an arrow flying right.
        if 0.06 <= u <= 0.30 and 0.22 <= v <= 0.78:
            return color
        if 0.30 <= u <= 0.40 and 0.36 <= v <= 0.64:
            return color
        # arrow shaft
        if 0.44 <= u <= 0.78 and 0.44 <= v <= 0.56:
            return color
        # arrow head (triangle)
        if 0.72 <= u <= 0.94:
            half = (0.94 - u) * 1.4
            if abs(v - 0.5) <= half:
                return color
        return (0, 0, 0, 0)
    return draw


def panel_icon(color=ACCENT):
    def draw(x, y, size):
        u, v = scaled(x, y, size)
        border = 0.06
        # window frame
        inside = border <= u <= 1 - border and border <= v <= 1 - border
        if not inside:
            return (0, 0, 0, 0)
        edge = (u <= border + 0.07 or u >= 1 - border - 0.07
                or v <= border + 0.07 or v >= 1 - border - 0.07)
        if edge:
            return color
        if v <= 0.30:            # title bar
            return color
        if 0.40 <= v <= 0.48 and 0.22 <= u <= 0.78:
            return DARK
        if 0.56 <= v <= 0.64 and 0.22 <= u <= 0.62:
            return DARK
        if 0.72 <= v <= 0.80 and 0.22 <= u <= 0.70:
            return DARK
        return (0, 0, 0, 0)
    return draw


def receive_icon(color=GREEN):
    def draw(x, y, size):
        u, v = scaled(x, y, size)
        # connector on the right, arrow flying left into it
        if 0.70 <= u <= 0.94 and 0.22 <= v <= 0.78:
            return color
        if 0.60 <= u <= 0.70 and 0.36 <= v <= 0.64:
            return color
        if 0.22 <= u <= 0.56 and 0.44 <= v <= 0.56:
            return color
        if 0.06 <= u <= 0.28:
            half = (u - 0.06) * 1.4
            if abs(v - 0.5) <= half:
                return color
        return (0, 0, 0, 0)
    return draw


ICONS = {"send": send_icon(), "panel": panel_icon(), "receive": receive_icon()}
for name, draw in ICONS.items():
    for size in (16, 32, 64):
        png(ROOT / name / f"{size}x{size}.png", size, draw)
    (ROOT / name / "README.md").write_text(
        f"# {name} icon\n\nToolbar icons for the MoxaSerial `{name}` command.\n"
        "Fusion loads `16x16.png` and `32x32.png` from this folder by path;\n"
        "`64x64.png` is here for high-DPI displays.\n\n"
        "Regenerate with `tools/make_icons.py`.\n", encoding="utf-8")
print("icons written")
