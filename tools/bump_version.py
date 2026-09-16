#!/usr/bin/env python3
"""Set the release version in the one place that defines it.

``MoxaSerial.manifest`` is the single source of truth: the zip builder, the
macOS pkg, the Windows installer and the release workflow all read the version
from there. This script edits that one field (and the CHANGELOG heading, if you
keep a CHANGELOG.md), then prints the git commands to tag the release.

Usage::

    python3 tools/bump_version.py 0.2.0
    python3 tools/bump_version.py 0.2.0 --dry-run
    python3 tools/bump_version.py --show          # print the current version

Pushing the printed ``v<version>`` tag is what triggers
``.github/workflows/release.yml`` to build the zip, the .pkg and the .exe and
publish them as a GitHub Release.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "MoxaSerial.manifest"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

# Matches the manifest's version line without reformatting the rest of the
# JSON -- a json.load/json.dump round trip would reflow the whole file.
VERSION_LINE = re.compile(r'^(?P<pre>\s*"version"\s*:\s*")(?P<version>[^"]*)(?P<post>".*)$', re.MULTILINE)


def current_version() -> str:
    text = MANIFEST.read_text(encoding="utf-8")
    m = VERSION_LINE.search(text)
    if not m:
        raise SystemExit(f"error: no \"version\" line found in {MANIFEST}")
    return m.group("version")


def set_manifest_version(new: str, dry_run: bool) -> str:
    text = MANIFEST.read_text(encoding="utf-8")
    m = VERSION_LINE.search(text)
    if not m:
        raise SystemExit(f'error: no "version" line found in {MANIFEST}')
    old = m.group("version")
    updated = text[: m.start()] + m.group("pre") + new + m.group("post") + text[m.end():]

    if not dry_run:
        MANIFEST.write_text(updated, encoding="utf-8")
    print(f"  {MANIFEST.relative_to(REPO_ROOT)}: {old} -> {new}")
    return old


def update_changelog(new: str, dry_run: bool) -> None:
    """Best-effort CHANGELOG heading update; skipped when there is no CHANGELOG."""
    if not CHANGELOG.is_file():
        print("  CHANGELOG.md: not present, skipped")
        return

    today = _dt.date.today().isoformat()
    text = CHANGELOG.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    heading = re.compile(r"^##\s+")
    unreleased = re.compile(r"^##\s+\[?unreleased\]?", re.IGNORECASE)
    already = re.compile(rf"^##\s+\[?{re.escape(new)}\]?\b")

    for i, line in enumerate(lines):
        if not heading.match(line):
            continue
        if already.match(line):
            print(f"  CHANGELOG.md: already has a {new} heading, left alone")
            return
        if unreleased.match(line):
            lines[i] = f"## {new} - {today}\n"
            print(f"  CHANGELOG.md: 'Unreleased' -> '{new} - {today}'")
            break
        # First heading is some other version: insert a new section above it.
        lines.insert(i, f"## {new} - {today}\n\n")
        print(f"  CHANGELOG.md: inserted '{new} - {today}' above '{line.strip()}'")
        break
    else:
        lines.append(f"\n## {new} - {today}\n")
        print(f"  CHANGELOG.md: appended '{new} - {today}'")

    if not dry_run:
        CHANGELOG.write_text("".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("version", nargs="?", help="new version, x.y.z")
    ap.add_argument("--show", action="store_true", help="print the current version and exit")
    ap.add_argument("--dry-run", action="store_true", help="print the changes without writing them")
    args = ap.parse_args(argv)

    if args.show or not args.version:
        print(current_version())
        return 0 if args.show else 2

    new = args.version.lstrip("v")
    if not SEMVER.match(new):
        print(f"error: {args.version!r} is not x.y.z", file=sys.stderr)
        return 2

    old = current_version()
    if new == old:
        print(f"error: already at {new}", file=sys.stderr)
        return 2

    print(f"MoxaSerial {old} -> {new}" + ("  (dry run)" if args.dry_run else ""))
    set_manifest_version(new, args.dry_run)
    update_changelog(new, args.dry_run)

    print()
    print("Now commit and tag:")
    print(f"    git add MoxaSerial.manifest{' CHANGELOG.md' if CHANGELOG.is_file() else ''}")
    print(f'    git commit -m "release: v{new}"')
    print(f'    git tag -a v{new} -m "MoxaSerial v{new}"')
    print("    git push origin HEAD --follow-tags")
    print()
    print(f"Pushing the v{new} tag runs .github/workflows/release.yml, which builds")
    print("the zip, the macOS .pkg and the Windows .exe and publishes the release.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
