#!/usr/bin/env bash
# Export the gated public file set from this (private-history) checkout into
# the clean `public` branch and optionally push it to GitHub as `main`.
#
#   tools/publish_public.sh "MoxaSerial 0.2.0"          # commit only
#   tools/publish_public.sh "MoxaSerial 0.2.0" --push    # commit, push main, push tag v<version>
#
# The private `main` branch (which carries research/ in its history) is never
# pushed. The public branch is rebuilt from a file list produced by
# tools/check_public_tree.py, which also runs the leak scan first.
set -euo pipefail
MSG="${1:?commit message, e.g. \"MoxaSerial 0.2.0\"}"
PUSH="${2:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WT="${MOXA_PUBLIC_WORKTREE:-/tmp/moxa_public_wt}"
REMOTE="${MOXA_PUBLIC_REMOTE:-https://github.com/npolanosky/MoxaSerial.git}"
cd "$ROOT"
python3 tools/check_public_tree.py || { echo "public-tree gate failed; not publishing" >&2; exit 1; }
python3 tools/check_public_tree.py --list > /tmp/moxa_public_list.txt
VERSION="$(python3 -c 'import json;print(json.load(open("MoxaSerial.manifest"))["version"])')"
if [ ! -d "$WT/.git" ] && [ ! -f "$WT/.git" ]; then
  git worktree add -q "$WT" public 2>/dev/null || { git worktree add -q "$WT" --detach HEAD; (cd "$WT" && git checkout -q --orphan public && git rm -rfq --cached . ); }
fi
(cd "$WT" && git checkout -q public && find . -mindepth 1 -maxdepth 1 -not -name .git -exec rm -rf {} +)
while read -r f; do mkdir -p "$WT/$(dirname "$f")"; cp "$f" "$WT/$f"; done < /tmp/moxa_public_list.txt
cd "$WT"
python3 tools/check_public_tree.py || exit 1
git add -A
if git diff --cached --quiet; then echo "public branch already up to date"; else
  git commit -q -m "$MSG"
fi
git log --oneline -1
if [ "$PUSH" = "--push" ]; then
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "$REMOTE"
  git push origin public:main
  git tag -a "v$VERSION" -m "MoxaSerial $VERSION" 2>/dev/null && git push origin "v$VERSION" || echo "tag v$VERSION already exists"
fi
