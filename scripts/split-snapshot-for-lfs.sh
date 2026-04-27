#!/bin/bash
# Split a .bdsnap into chunks for Git LFS servers with a hard 2 GiB (2^31 byte)
# per-object cap, e.g.:
#   [oid] Size must be less than or equal to 2147483648: [422]
#
# Chunks must stay *strictly* below that ceiling: a 2.0G (2147483648 B) part is
# often rejected. Default is 1800 MiB; override with env LFS_CHUNK_BYTES.
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
# 1800 MiB = 1887436800 B — clear margin under 2147483648 (some remotes are strict)
MAX_CHUNK_BYTES="${LFS_CHUNK_BYTES:-$((1800 * 1024 * 1024))}"

if [[ ! -f "$INPUT" ]]; then
  echo "Not found: $INPUT" >&2
  exit 1
fi

mkdir -p "$CHUNK_DIR"
rm -f "$CHUNK_DIR/${STEM}".[0-9][0-9][0-9]

echo "Chunk size: $MAX_CHUNK_BYTES bytes (override with LFS_CHUNK_BYTES=... )" >&2

# GNU split: numeric suffixes, 3 digits (000..999) — enough for multi-TB at ~1.8GB/chunk
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
