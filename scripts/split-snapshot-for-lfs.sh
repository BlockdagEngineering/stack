#!/bin/bash
# Split a .bdsnap into chunks under 2 GiB for Git LFS servers with a 2GB object limit
# (e.g. error: Size must be less than or equal to 2147483648).
#
# Usage: ./scripts/split-snapshot-for-lfs.sh snapshots/exported-snapshot.bdsnap [stem]
#   stem defaults to basename without .bdsnap; chunks go to snapshots/lfs-parts/<stem>.000, .001, ...
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

INPUT="${1:?Usage: $0 <path/to/file.bdsnap> [stem]}"
STEM="${2:-$(basename "$INPUT" .bdsnap)}"
CHUNK_DIR="snapshots/lfs-parts"
MAX_CHUNK_BYTES=$((2000 * 1024 * 1024)) # 2000 MiB — safely under 2^31 server cap

if [[ ! -f "$INPUT" ]]; then
  echo "Not found: $INPUT" >&2
  exit 1
fi

mkdir -p "$CHUNK_DIR"
rm -f "$CHUNK_DIR/${STEM}".[0-9][0-9][0-9]

# GNU split: numeric suffixes, 3 digits (000..999) — enough for ~6 TB at 2GB/chunk
split -b "$MAX_CHUNK_BYTES" -d -a 3 "$INPUT" "$CHUNK_DIR/${STEM}."

echo "Created chunks under $CHUNK_DIR/${STEM}.*"
ls -lh "$CHUNK_DIR/${STEM}".*
echo ""
echo "Next:"
echo "  git rm --cached \"$INPUT\" 2>/dev/null || true   # stop tracking the huge single file"
echo "  rm -f \"$INPUT\"   # optional: remove local monolith after verifying chunks"
echo "  git add $CHUNK_DIR/${STEM}.*"
echo "  git commit -m \"Snapshot: LFS chunks for $STEM\""
echo "  git push"
