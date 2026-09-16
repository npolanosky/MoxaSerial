#!/usr/bin/env python3
"""Gate for making this repository public.

Two checks, both driven by ``tools/public_manifest.txt``:

1. **Nothing unexpected would ship.** Every file in the working tree (and
   every file git tracks) must match either a public pattern or an explicit
   ``!private`` pattern. A file matching neither is a hole in the manifest
   and fails the run, so a new folder can never leak by being forgotten.

2. **Nothing private is written inside a public file.** The public files are
   scanned for private IP addresses, home-directory paths, e-mail addresses,
   credentials and customer / job names.

    python3 tools/check_public_tree.py          # check
    python3 tools/check_public_tree.py --list   # print the public file list

Exit status is non-zero on any hit. Stdlib only, like the add-in itself.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "tools" / "public_manifest.txt"

# Directories never walked: git internals and the private trees are matched
# by the manifest anyway, but walking 1 MB of help extracts is pointless.
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", ".pytest_cache", ".ruff_cache",
             "node_modules", ".devdata", ".claude"}

TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".html", ".css", ".js", ".toml",
                 ".ini", ".cfg", ".yml", ".yaml", ".manifest", ".sh", ".bat", ".ps1",
                 ".iss", ".plist", ".gitignore", ""}


# --------------------------------------------------------------------------
# Leak patterns
# --------------------------------------------------------------------------

# Substrings that are allowed to match a rule above. Each is a documented
# exception, not a blanket mute.
ALLOWED = (
    # The documented example network. Prose, docstrings and form placeholders
    # say "type your NPort's address here" with a 192.168.1.x address on
    # purpose, so that no real device address has to appear in a public file.
    "192.168.1.",
)

RULES: list[tuple[str, str]] = [
    ("private IPv4 (10.0.0.0/8)", r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    ("private IPv4 (192.168.0.0/16)", r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
    ("private IPv4 (172.16.0.0/12)", r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"),
    ("macOS home path", r"/Users/[A-Za-z0-9._-]+"),
    ("Linux home path", r"/home/[A-Za-z0-9._-]+"),
    ("Windows user path", r"[A-Za-z]:\\+Users\\+[A-Za-z0-9._-]+"),
    ("mounted volume path", r"/Volumes/[A-Za-z0-9._-]+"),
    ("e-mail address", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}\b"),
    # "no admin password" is installer prose and `password: str = ""` is a
    # signature, so an assignment-ish context is not enough on its own: the
    # value has to be a non-empty *literal*. That still catches
    # `password = "hunter2"` and `"password": "hunter2"`, while ignoring
    # annotations, keyword forwarding (`password=password`) and expressions
    # (`password = str(payload.get(...))`). Whitespace in the value means
    # prose — an error message keyed "password" is not a password.
    ("password", r"""(?i)\bpass(?:word|wd|phrase)\b["']?\s*[:=]\s*["'][^"'\s]+["']"""),
    # "repository secret" in CI docs is prose; flag only assignments.
    ("secret", r"(?i)\bsecret\b\s*[:=]\s*\S"),
    # "token" is a protocol noun here (the ASPP POLLING token, the receive
    # overwrite token), so only flag it when it reads like a credential.
    ("credential token",
     r"(?i)\b(?:api|access|auth|bearer|refresh|private|secret)[_-]?token\b"
     r"|\btoken\s*[:=]\s*[\"'][A-Za-z0-9_\-]{12,}[\"']"),
    ("API key", r"(?i)\b(?:api[_-]?key|client[_-]?secret|aws_[a-z_]*key)\b"),
    ("private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("scratch / temp working path", r"/private/tmp/|/var/folders/|\bscratchpad\b"),
    # A public file must not point at a file that never ships.
    ("reference to a non-public path",
     r"research/[A-Za-z0-9._/-]+|prompt\+requirements|Cimco-for-reference"),
    # Customer, job and shop-specific names that must not reach the public repo.
    ("customer / job name", r"(?i)\b(?:xometry|protolabs|fictiv)\b"),
]

COMPILED = [(name, re.compile(pat)) for name, pat in RULES]

# Files whose whole job is to name the private material, per rule. Scoped to
# one file and one rule each, never to a shipped module: application code is
# always scanned in full.
RULE_EXEMPT: dict[str, set[str]] = {
    ".gitignore": {"reference to a non-public path"},
    # These pin datagrams captured byte-for-byte from a real NPort, and the
    # console tests replay a captured login page. The addresses, the CSRF
    # token and the passwords in them are test fixtures, not credentials.
    "tests/test_discovery.py": {
        "private IPv4 (10.0.0.0/8)",
        "private IPv4 (192.168.0.0/16)",
    },
    "tests/test_bridge_discovery.py": {"private IPv4 (10.0.0.0/8)", "password"},
    "tests/test_nport_console.py": {
        "private IPv4 (192.168.0.0/16)",
        "password",
        "credential token",
    },
    "tests/test_secrets.py": {"password"},
}


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

def load_manifest() -> tuple[list[str], list[str]]:
    public: list[str] = []
    private: list[str] = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        (private if line.startswith("!") else public).append(line.lstrip("!"))
    return public, private


def matches(path: str, patterns: list[str]) -> bool:
    """fnmatch, plus the usual "``**`` also means zero directories" rule.

    fnmatch's ``*`` already spans ``/``, so ``a/**/*.py`` matches
    ``a/b/c.py``; the extra variant makes it match ``a/c.py`` too.
    """
    for pat in patterns:
        variants = {pat}
        if "/**/" in pat:
            variants.add(pat.replace("/**/", "/", 1))
        if pat.endswith("/**"):
            variants.add(pat[:-3])
        for v in variants:
            if fnmatch.fnmatch(path, v) or path.startswith(v.rstrip("*") + "/"):
                return True
    return False


def walk_tree() -> list[str]:
    out: list[str] = []
    for p in ROOT.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(ROOT)
        if SKIP_DIRS & set(rel.parts[:-1]):
            continue
        if rel.parts[0] in SKIP_DIRS:
            continue
        out.append(rel.as_posix())
    return sorted(out)


def tracked_files() -> list[str]:
    try:
        res = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                             capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return []
    return sorted(f for f in res.stdout.splitlines() if f)


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

def redact(line: str) -> str:
    line = line.strip()
    return line if len(line) <= 160 else line[:157] + "..."


def scan(rel: str) -> list[str]:
    # These two files *are* the pattern list and the private-path list, so
    # scanning them only ever finds themselves.
    if rel in ("tools/check_public_tree.py", "tools/public_manifest.txt"):
        return []
    path = ROOT / rel
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    exempt = RULE_EXEMPT.get(rel, set())
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line
        for ok in ALLOWED:
            stripped = stripped.replace(ok, "")
        for name, rx in COMPILED:
            if name in exempt:
                continue
            if rx.search(stripped):
                hits.append(f"{rel}:{lineno}: {name}: {redact(line)}")
                break
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="print the public file list and exit")
    args = ap.parse_args()

    public_pats, private_pats = load_manifest()

    present = walk_tree()
    known = set(present) | set(tracked_files())

    public, unclassified = [], []
    for rel in sorted(known):
        if matches(rel, private_pats):
            continue
        if matches(rel, public_pats):
            public.append(rel)
        else:
            unclassified.append(rel)

    if args.list:
        print("\n".join(public))
        return 0

    problems = 0

    if unclassified:
        problems += len(unclassified)
        print("Files matching neither a public nor a private pattern "
              "(add them to tools/public_manifest.txt):")
        for rel in unclassified:
            print(f"  {rel}")
        print()

    leaks = [h for rel in public for h in scan(rel)]
    if leaks:
        problems += len(leaks)
        print("Private data found inside public files:")
        for h in leaks:
            print(f"  {h}")
        print()

    if problems:
        print(f"FAIL: {problems} problem(s). The tree is not ready to publish.")
        return 1

    print(f"OK: {len(public)} public files, no unclassified files, no leaks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
