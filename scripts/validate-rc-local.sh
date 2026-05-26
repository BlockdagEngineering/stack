#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_RUNTIME="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_RUNTIME"
  find "$ROOT/ops" "$ROOT/scripts" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  find "$ROOT/ops" "$ROOT/scripts" -name '*.pyc' -delete 2>/dev/null || true
}
trap cleanup EXIT

remove_ignored_runtime_state() {
  local runtime="$ROOT/ops/runtime"
  [[ -e "$runtime" ]] || return 0
  if git -C "$ROOT" check-ignore -q ops/runtime; then
    rm -rf "$runtime"
    return 0
  fi
  printf 'refusing to remove non-ignored runtime state: %s\n' "$runtime" >&2
  return 1
}

cd "$ROOT"
remove_ignored_runtime_state
PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q ops scripts
BDAG_RUNTIME_DIR="$TMP_RUNTIME/runtime" PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s ops/tests -p 'test_*.py'
python3 scripts/check-doc-consistency.py
find "$ROOT/ops" "$ROOT/scripts" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find "$ROOT/ops" "$ROOT/scripts" -name '*.pyc' -delete 2>/dev/null || true
remove_ignored_runtime_state
bash scripts/validate-pi5-restart-hardening.sh --mode source .
