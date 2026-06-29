#!/usr/bin/env bash
set -Eeuo pipefail

PATH=/usr/sbin:/usr/bin:/sbin:/bin
BACKUP_ROOT="/opt/backups/blockchain/vqueen-v6.5.7-nearhot"
LOG_THREAD="ManifestWrapper"
LOG_SYSLOG_TAG="${LOG_SYSLOG_TAG:-vqueen-nearhot-manifest}"
VQUEEN_LOGGING_LIB="${VQUEEN_LOGGING_LIB:-/usr/local/lib/vqueen-nearhot-backup/vqueen-logging.sh}"

# shellcheck source=../lib/vqueen-logging.sh
. "$VQUEEN_LOGGING_LIB"

fail() {
  log_error ManifestWrapper "$*"
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

refuse_root_or_empty() {
  local path="${1:-}"
  [ -n "$path" ] || fail "empty path refused"
  [ "$path" != "/" ] || fail "root path refused"
}

refuse_symlink_path() {
  local path="$1"
  local current part
  local -a parts

  current="/"
  refuse_root_or_empty "$path"
  case "$path" in
    /*) ;;
    *) fail "path must be absolute: $path" ;;
  esac

  IFS='/' read -r -a parts <<< "${path#/}"
  for part in "${parts[@]}"; do
    [ -n "$part" ] || continue
    [ "$part" != "." ] || continue
    [ "$part" != ".." ] || fail "parent traversal refused: $path"
    current="${current%/}/$part"
    [ ! -L "$current" ] || fail "symlink component refused: $current"
  done
}

require_backup_run() {
  local run_dir="$1"

  case "$run_dir" in
    "${BACKUP_ROOT}"/runs/[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/vqueen-v6.5.7-*) ;;
    *) fail "run dir does not match approved v6.5.7 pattern: $run_dir" ;;
  esac
  refuse_symlink_path "$run_dir"
  [ -d "$run_dir/data" ] || fail "backup data dir missing: $run_dir/data"
}

mode="${1:-}"
run_dir="${2:-}"
[ "$#" -eq 2 ] || fail "usage: vqueen-nearhot-manifest <manifest|verify> <run-dir>"

run_dir="$(realpath -m -- "$run_dir")"
require_backup_run "$run_dir"
mkdir -p -- "$run_dir/manifests"

case "$mode" in
  manifest)
    log_info ManifestWrapper "manifest generation start: $run_dir"
    tmp_manifest="$(mktemp --tmpdir="$(dirname -- "$run_dir/manifests/file-manifest.sha256")" file-manifest.sha256.tmp.XXXXXX)"
    tmp_size="$(mktemp --tmpdir="$(dirname -- "$run_dir/manifests/data-size.txt")" data-size.txt.tmp.XXXXXX)"
    (
      cd "$run_dir/data"
      find . -type f -print0 | sort -z | xargs -0 sha256sum >"$tmp_manifest"
    )
    du -sh "$run_dir/data" >"$tmp_size"
    mv -- "$tmp_manifest" "$run_dir/manifests/file-manifest.sha256"
    mv -- "$tmp_size" "$run_dir/manifests/data-size.txt"
    chmod 0644 "$run_dir/manifests/file-manifest.sha256" "$run_dir/manifests/data-size.txt"
    log_info ManifestWrapper "manifest generation complete: $run_dir"
    ;;
  verify)
    log_info ManifestWrapper "manifest verification start: $run_dir"
    [ -f "$run_dir/manifests/file-manifest.sha256" ] || fail "manifest missing: $run_dir/manifests/file-manifest.sha256"
    (
      cd "$run_dir/data"
      sha256sum -c "$run_dir/manifests/file-manifest.sha256"
    ) >"$run_dir/manifests/file-manifest.verify.out"
    chmod 0644 "$run_dir/manifests/file-manifest.verify.out"
    log_info ManifestWrapper "manifest verification complete: $run_dir"
    ;;
  *)
    fail "unknown mode: $mode"
    ;;
esac
