#!/usr/bin/env bash
set -Eeuo pipefail

PATH=/usr/sbin:/usr/bin:/sbin:/bin
BACKUP_ROOT="/opt/backups/blockchain/vqueen-v6.5.7-nearhot"
RSYNC_BWLIMIT_KB="65536"
LOG_THREAD="RsyncWrapper"
LOG_SYSLOG_TAG="${LOG_SYSLOG_TAG:-vqueen-nearhot-rsync}"
VQUEEN_LOGGING_LIB="${VQUEEN_LOGGING_LIB:-/usr/local/lib/vqueen-nearhot-backup/vqueen-logging.sh}"

# shellcheck source=../lib/vqueen-logging.sh
. "$VQUEEN_LOGGING_LIB"

fail() {
  log_error RsyncWrapper "$*"
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

require_under_backup_runs() {
  local path="$1"
  local root="${BACKUP_ROOT}/runs"
  case "${path%/}/" in
    "${root%/}/"*) ;;
    *) fail "path escapes backup runs: $path" ;;
  esac
}

label="${1:-}"
run_dir="${2:-}"
[ "$#" -eq 2 ] || fail "usage: vqueen-nearhot-rsync <chain-node|nodeworker|collector-runtime> <run-dir>"
log_info RsyncWrapper "wrapper invoked: label=$label"

case "$label" in
  chain-node)
    src="/home/eddie/blockdag-pool-20260615-nvme/blockdag-chain/node"
    dest_sub="chain/node"
    ;;
  nodeworker)
    src="/var/lib/docker/volumes/pool-stack-docker-pool-v657-linux-amd64_nodeworker-data/_data"
    dest_sub="nodeworker"
    ;;
  collector-runtime)
    src="/var/lib/docker/volumes/pool-stack-docker-pool-v657-linux-amd64_collector-runtime/_data"
    dest_sub="collector-runtime"
    ;;
  *) fail "unknown source label: $label" ;;
esac
log_info RsyncWrapper "source label accepted: $label"

run_dir="$(realpath -m -- "$run_dir")"
log_info RsyncWrapper "run dir resolved: $run_dir"
case "$run_dir" in
  "${BACKUP_ROOT}"/runs/[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/vqueen-v6.5.7-*) ;;
  *) fail "run dir does not match approved v6.5.7 pattern: $run_dir" ;;
esac
require_under_backup_runs "$run_dir"
log_info RsyncWrapper "run dir accepted"

src="$(realpath -m -- "$src")"
dest="$(realpath -m -- "$run_dir/staging/data/$dest_sub")"

[ -d "$src" ] || fail "source missing: $src"
[ -d "$dest" ] || fail "destination missing: $dest"
log_info RsyncWrapper "source and destination directories exist: label=$label dest_sub=$dest_sub"
refuse_symlink_path "$src"
refuse_symlink_path "$dest"
require_under_backup_runs "$dest"
log_info RsyncWrapper "path validation complete: label=$label"

log_info RsyncWrapper "rsync exec start: label=$label bwlimit_kb=$RSYNC_BWLIMIT_KB"
exec /usr/bin/rsync -aH --numeric-ids --delete --stats --bwlimit="$RSYNC_BWLIMIT_KB" \
  "${src%/}/" "${dest%/}/"
