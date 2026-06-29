#!/usr/bin/env bash
set -Eeuo pipefail

PATH=/usr/sbin:/usr/bin:/sbin:/bin
BACKUP_ROOT="/opt/backups/blockchain/vqueen-v6.5.7-nearhot"
DEV_RESTORE_ROOT="/opt/backups/blockchain/dev-restore"
LOG_THREAD="RestoreWrapper"
LOG_SYSLOG_TAG="${LOG_SYSLOG_TAG:-vqueen-restore-proof}"
VQUEEN_LOGGING_LIB="${VQUEEN_LOGGING_LIB:-/usr/local/lib/vqueen-nearhot-backup/vqueen-logging.sh}"

# shellcheck source=../lib/vqueen-logging.sh
. "$VQUEEN_LOGGING_LIB"

fail() {
  log_error RestoreWrapper "$*"
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

require_paths() {
  backup_path="$(realpath -m -- "$backup_path")"
  restore_path="$(realpath -m -- "$restore_path")"

  case "$backup_path" in
    "${BACKUP_ROOT}"/runs/[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/vqueen-v6.5.7-*) ;;
    *) fail "backup path does not match approved v6.5.7 pattern: $backup_path" ;;
  esac
  case "${restore_path%/}/" in
    "${DEV_RESTORE_ROOT%/}/"*) ;;
    *) fail "restore path escapes dev restore root: $restore_path" ;;
  esac
  case "$restore_path" in
    "${DEV_RESTORE_ROOT}"/vqueen-v6.5.7-*) ;;
    *) fail "restore path does not match approved v6.5.7 pattern: $restore_path" ;;
  esac

  refuse_symlink_path "$backup_path"
  refuse_symlink_path "$restore_path"
  [ -d "$backup_path/data" ] || fail "backup data dir missing: $backup_path/data"
  [ -f "$backup_path/manifests/file-manifest.sha256" ] || fail "backup manifest missing"
}

mode="${1:-}"
backup_path="${2:-}"
restore_path="${3:-}"
[ "$#" -eq 3 ] || fail "usage: vqueen-nearhot-restore-proof <copy-verify> <backup-path> <restore-path>"
require_paths

case "$mode" in
  copy-verify)
    log_info RestoreWrapper "restore copy start: backup=$backup_path restore=$restore_path"
    if [ -e "$restore_path" ]; then
      [ -f "$restore_path/metadata/status.txt" ] || fail "existing restore path has no status: $restore_path"
      grep -qx copied "$restore_path/metadata/status.txt" || fail "existing restore path is not reusable: $restore_path"
      log_info RestoreWrapper "reusing copied restore path: $restore_path"
    else
      mkdir -p -- "$restore_path"
      rsync -aH --numeric-ids --delete --stats "${backup_path%/}/data/" "${restore_path%/}/data/"
      mkdir -p -- "$restore_path/manifests" "$restore_path/metadata"
      cp -a -- "$backup_path/manifests/file-manifest.sha256" "$restore_path/manifests/file-manifest.sha256"
      cp -a -- "$backup_path/manifests/data-size.txt" "$restore_path/manifests/source-data-size.txt"
      (
        cd "$restore_path/data"
        sha256sum -c "$restore_path/manifests/file-manifest.sha256"
      ) >"$restore_path/manifests/file-manifest.restore-verify.out"
      du -sh "$restore_path/data" >"$restore_path/manifests/restore-data-size.txt"
      printf 'copied\n' >"$restore_path/metadata/status.txt"
    fi
    install -d -o eddie -g eddie -m 0755 "$restore_path/evidence" "$restore_path/postgres-data"
    chmod -R u+rwX,go+rX "$restore_path/manifests" "$restore_path/metadata"
    log_info RestoreWrapper "restore copy verified: $restore_path"
    ;;
  *)
    fail "unknown mode: $mode"
    ;;
esac
