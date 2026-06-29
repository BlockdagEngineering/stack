#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
export PROJECT_ROOT

# shellcheck source=../lib/vqueen-backup-lib.sh
. "${PROJECT_ROOT}/lib/vqueen-backup-lib.sh"
load_config

usage() {
  cat <<'USAGE'
Usage:
  vqueen-backup.sh --help
  vqueen-backup.sh --show-config
  vqueen-backup.sh --preflight
  vqueen-backup.sh --dry-run
  vqueen-backup.sh --backup
  vqueen-backup.sh --verify <run-path>
  vqueen-backup.sh --list

Current scaffold status:
  M4 dry-run is closed. Runtime write modes require an explicit active-gate
  approval token.
USAGE
}

show_config() {
  validate_static_config
  cat <<EOF
PROJECT_ROOT=$PROJECT_ROOT
BACKUP_ROOT=$BACKUP_ROOT
DEV_RESTORE_ROOT=$DEV_RESTORE_ROOT
BACKUP_LOG_DIR=$BACKUP_LOG_DIR
RESTORE_LOG_DIR=$RESTORE_LOG_DIR
LIVE_COMPOSE_PROJECT=$LIVE_COMPOSE_PROJECT
LIVE_RUNTIME_DIR=$LIVE_RUNTIME_DIR
NODE_DATA_SRC=$NODE_DATA_SRC
NODEWORKER_DATA_SRC=$NODEWORKER_DATA_SRC
COLLECTOR_RUNTIME_SRC=$COLLECTOR_RUNTIME_SRC
LIVE_READ_ACCESS=$LIVE_READ_ACCESS
SUDO_BIN=$SUDO_BIN
SUDO_FLAGS=$SUDO_FLAGS
POSTGRES_CONTAINER=$POSTGRES_CONTAINER
POSTGRES_COMPOSE_SERVICE=$POSTGRES_COMPOSE_SERVICE
POSTGRES_DB=$POSTGRES_DB
POSTGRES_USER=$POSTGRES_USER
MIN_FREE_BYTES=$MIN_FREE_BYTES
RSYNC_NICE=$RSYNC_NICE
RSYNC_IONICE_CLASS=$RSYNC_IONICE_CLASS
RSYNC_IONICE_LEVEL=$RSYNC_IONICE_LEVEL
RSYNC_BWLIMIT_KB=$RSYNC_BWLIMIT_KB
RSYNC_WRAPPER_BIN=$RSYNC_WRAPPER_BIN
LOG_LEVEL=$LOG_LEVEL
LOG_TO_SYSLOG=$LOG_TO_SYSLOG
LOG_SYSLOG_TAG=$LOG_SYSLOG_TAG
OPERATION_LOCK=$OPERATION_LOCK
BACKUP_LOCK=$BACKUP_LOCK
RESTORE_LOCK=$RESTORE_LOCK
EOF
}

preflight() {
  log_info MainThread "preflight started"
  validate_static_config
  validate_live_read_gate
  check_required_tools_for_preflight

  log_debug MainThread "checking live runtime dir: $LIVE_RUNTIME_DIR"
  [ -d "$LIVE_RUNTIME_DIR" ] || die "live runtime dir missing: $LIVE_RUNTIME_DIR"
  log_debug MainThread "checking live compose file: $LIVE_COMPOSE_FILE"
  [ -f "$LIVE_COMPOSE_FILE" ] || die "live compose file missing: $LIVE_COMPOSE_FILE"
  log_debug MainThread "checking node data source: $NODE_DATA_SRC"
  path_exists_dir "$NODE_DATA_SRC" || die "node data source missing: $NODE_DATA_SRC"
  log_debug MainThread "checking nodeworker source: $NODEWORKER_DATA_SRC"
  path_exists_dir "$NODEWORKER_DATA_SRC" || die "nodeworker source missing: $NODEWORKER_DATA_SRC"
  log_debug MainThread "checking collector runtime source: $COLLECTOR_RUNTIME_SRC"
  path_exists_dir "$COLLECTOR_RUNTIME_SRC" || die "collector runtime source missing: $COLLECTOR_RUNTIME_SRC"

  log_debug MainThread "checking live runtime path has no symlink components"
  refuse_symlink_path "$LIVE_RUNTIME_DIR"
  log_debug MainThread "checking node data path has no symlink components"
  refuse_symlink_path "$NODE_DATA_SRC"
  log_debug MainThread "checking nodeworker path has no symlink components"
  refuse_symlink_path "$NODEWORKER_DATA_SRC"
  log_debug MainThread "checking collector runtime path has no symlink components"
  refuse_symlink_path "$COLLECTOR_RUNTIME_SRC"

  log_info MainThread "preflight completed"
}

rsync_source_to_dest() {
  local label="$1"
  local src="$2"
  local dest="$3"
  local run_dir="${4:-}"
  local wrapper_label="${5:-}"
  local -a rsync_args
  local rc

  mkdir -p -- "$dest"
  rsync_args=(-aH --numeric-ids --delete --stats)
  if [ -n "${RSYNC_BWLIMIT_KB:-}" ]; then
    rsync_args+=(--bwlimit="$RSYNC_BWLIMIT_KB")
  fi

  log_info RsyncWrapper "rsync start: $label"
  case "${LIVE_READ_ACCESS:-}" in
    controlled-sudo)
      [ -n "$run_dir" ] || die "run dir required for controlled-sudo rsync wrapper"
      [ -n "$wrapper_label" ] || die "source label required for controlled-sudo rsync wrapper"
      log_info RsyncWrapper "sudo wrapper invocation: label=$wrapper_label run_dir=$run_dir"
      nice -n "$RSYNC_NICE" ionice -c "$RSYNC_IONICE_CLASS" -n "$RSYNC_IONICE_LEVEL" \
        "$SUDO_BIN" $SUDO_FLAGS "$RSYNC_WRAPPER_BIN" "$wrapper_label" "$run_dir" || rc=$?
      ;;
    direct)
      log_info RsyncWrapper "direct rsync invocation: label=$label dest=$dest"
      nice -n "$RSYNC_NICE" ionice -c "$RSYNC_IONICE_CLASS" -n "$RSYNC_IONICE_LEVEL" \
        rsync "${rsync_args[@]}" "${src%/}/" "${dest%/}/" || rc=$?
      ;;
    *)
      die "LIVE_READ_ACCESS must be controlled-sudo or direct"
      ;;
  esac
  rc="${rc:-0}"
  if [ "$rc" -eq 24 ]; then
    log_warning RsyncWrapper "rsync live-source vanished files tolerated: $label"
  elif [ "$rc" -ne 0 ]; then
    die "rsync failed for $label with exit code $rc"
  fi
  log_info RsyncWrapper "rsync complete: $label"
}

write_backup_metadata() {
  local run_dir="$1"
  local metadata_dir="$run_dir/metadata"

  log_info MainThread "writing backup metadata: $metadata_dir"
  mkdir -p -- "$metadata_dir"
  log_debug MainThread "writing metadata show-config.txt"
  show_config >"$metadata_dir/show-config.txt"
  log_debug MainThread "writing metadata git-status-short.txt"
  git status --short >"$metadata_dir/git-status-short.txt"
  log_debug MainThread "writing metadata git-remote.txt"
  git remote -v >"$metadata_dir/git-remote.txt"
  log_debug MainThread "writing metadata docker-ps.txt"
  docker ps --format '{{.Names}} {{.Status}}' | sort >"$metadata_dir/docker-ps.txt"
  log_debug MainThread "writing metadata run.txt"
  {
    printf 'RUN_ID=%s\n' "$RUN_ID"
    printf 'RUN_DIR=%s\n' "$run_dir"
    printf 'UTC_TIME=%s\n' "$(date -u +%Y/%m/%dT%H:%M:%SZ)"
    printf 'HOST=%s\n' "$(hostname -f 2>/dev/null || hostname)"
  } >"$metadata_dir/run.txt"
  log_info MainThread "backup metadata written: $metadata_dir"
}

write_postgres_dumps() {
  local dump_dir="$1"

  check_postgres_container_identity
  mkdir -p -- "$dump_dir"
  log_info PostgresDump "postgres dump dir ready: $dump_dir"
  log_info PostgresDump "postgres custom dump start"
  docker exec "$POSTGRES_CONTAINER" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc \
    >"$dump_dir/${POSTGRES_DB}.custom.dump"
  log_info PostgresDump "postgres custom dump complete"
  log_info PostgresDump "postgres schema dump start"
  docker exec "$POSTGRES_CONTAINER" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --schema-only \
    >"$dump_dir/${POSTGRES_DB}.schema.sql"
  log_info PostgresDump "postgres schema dump complete"
}

write_manifest() {
  local run_dir="$1"

  log_info MainThread "manifest generation start: $run_dir"
  case "${LIVE_READ_ACCESS:-}" in
    controlled-sudo)
      [ -n "${MANIFEST_WRAPPER_BIN:-}" ] || die "MANIFEST_WRAPPER_BIN is required for controlled-sudo manifest generation"
      "$SUDO_BIN" $SUDO_FLAGS "$MANIFEST_WRAPPER_BIN" manifest "$run_dir" || die "manifest wrapper failed"
      ;;
    direct)
      mkdir -p -- "$run_dir/manifests"
      (
        cd "$run_dir/data"
        find . -type f -print0 | sort -z | xargs -0 sha256sum >"$run_dir/manifests/file-manifest.sha256"
      )
      du -sh "$run_dir/data" >"$run_dir/manifests/data-size.txt"
      ;;
    *)
      die "LIVE_READ_ACCESS must be controlled-sudo or direct"
      ;;
  esac
  log_info MainThread "manifest generation complete: $run_dir/manifests"
}

dry_run() {
  log_info MainThread "dry-run started"
  preflight
  RUN_ID="${RUN_ID:-$(make_run_id)}"
  export RUN_ID
  local run_dir
  run_dir="${BACKUP_ROOT}/runs/$(date -u +%Y/%m/%d)/${RUN_ID}"
  require_under_root "$run_dir" "$BACKUP_ROOT"
  log_info MainThread "dry-run rendered plan for run dir: $run_dir"

  cat <<EOF
DRY_RUN=1
RUN_ID=$RUN_ID
RUN_DIR=$run_dir
LIVE_READ_ACCESS=$LIVE_READ_ACCESS
SUDO_COMMAND=$SUDO_BIN $SUDO_FLAGS
MIN_FREE_BYTES=$MIN_FREE_BYTES
RSYNC_NICE=$RSYNC_NICE
RSYNC_IONICE_CLASS=$RSYNC_IONICE_CLASS
RSYNC_IONICE_LEVEL=$RSYNC_IONICE_LEVEL
RSYNC_BWLIMIT_KB=$RSYNC_BWLIMIT_KB
BACKUP_LOCK=$BACKUP_LOCK
OPERATION_LOCK=$OPERATION_LOCK
RSYNC_PASS_1_NODE_SRC=$NODE_DATA_SRC
RSYNC_PASS_1_NODE_LABEL=chain-node
RSYNC_PASS_1_NODE_DEST=$run_dir/staging/data/chain/node/
RSYNC_PASS_1_NODEWORKER_SRC=$NODEWORKER_DATA_SRC
RSYNC_PASS_1_NODEWORKER_LABEL=nodeworker
RSYNC_PASS_1_NODEWORKER_DEST=$run_dir/staging/data/nodeworker/
RSYNC_PASS_1_COLLECTOR_RUNTIME_SRC=$COLLECTOR_RUNTIME_SRC
RSYNC_PASS_1_COLLECTOR_RUNTIME_LABEL=collector-runtime
RSYNC_PASS_1_COLLECTOR_RUNTIME_DEST=$run_dir/staging/data/collector-runtime/
POSTGRES_DUMP_DEST=$run_dir/staging/data/postgres/
RSYNC_PASS_2_NODE_SRC=$NODE_DATA_SRC
RSYNC_PASS_2_NODEWORKER_SRC=$NODEWORKER_DATA_SRC
RSYNC_PASS_2_COLLECTOR_RUNTIME_SRC=$COLLECTOR_RUNTIME_SRC
RSYNC_WRAPPER_BIN=$RSYNC_WRAPPER_BIN
BACKUP_LOG=$BACKUP_LOG_DIR/backup-$RUN_ID.log
EOF
}

required_m5_backup_token() {
  printf 'vqueen-v6.5.7-first-backup-2026-06-26\n'
}

require_m5_backup_gate() {
  local expected
  expected="$(required_m5_backup_token)"
  log_info MainThread "checking M5 backup approval token"
  if [ "${VQUEEN_M5_BACKUP_APPROVED:-}" = "$expected" ]; then
    log_notice MainThread "M5 backup approval token accepted"
    return 0
  fi
  die "--backup requires an explicit backup approval token for this run"
}

backup_run_inner_locked() {
  RUN_ID="${RUN_ID:-$(make_run_id)}"
  export RUN_ID

  local run_dir staging_dir free_bytes log_file
  run_dir="${BACKUP_ROOT}/runs/$(date -u +%Y/%m/%d)/${RUN_ID}"
  staging_dir="$run_dir/staging"
  setup_log_dir "$BACKUP_LOG_DIR"
  log_file="$BACKUP_LOG_DIR/backup-$RUN_ID.log"
  exec > >(tee -a "$log_file") 2>&1
  log_info MainThread "backup log attached: $log_file"

  preflight
  check_required_tools_for_backup

  log_info MainThread "validating backup run paths: $run_dir"
  require_under_root "$run_dir" "$BACKUP_ROOT"
  require_under_root "$run_dir" "$BACKUP_ROOT/runs"
  require_write_target_path "$run_dir" "backup run dir"
  require_write_target_path "$staging_dir" "backup staging dir"
  [ ! -e "$run_dir" ] || die "run dir already exists: $run_dir"

  log_info MainThread "creating backup run and staging directories"
  mkdir -p -- "$BACKUP_ROOT/runs" "$run_dir/metadata" "$staging_dir/data"
  log_info MainThread "writing backup status: running"
  printf 'running\n' >"$run_dir/metadata/status.txt"
  trap 'rc=$?; if [ "$rc" -ne 0 ] && [ -n "${run_dir:-}" ] && [ -d "$run_dir/metadata" ]; then log_error MainThread "backup failed with exit code $rc; writing failed status"; printf "failed\n" >"$run_dir/metadata/status.txt"; fi' EXIT

  free_bytes="$(free_bytes_for_path "$BACKUP_ROOT")"
  log_info MainThread "backup target free bytes: $free_bytes"
  require_uint "$free_bytes" "backup target free bytes"
  [ "$free_bytes" -ge "$MIN_FREE_BYTES" ] || die "backup target free bytes below MIN_FREE_BYTES: $free_bytes"

  log_notice MainThread "backup started"
  write_backup_metadata "$run_dir"

  rsync_source_to_dest "node pass 1" "$NODE_DATA_SRC" "$staging_dir/data/chain/node" "$run_dir" "chain-node"
  rsync_source_to_dest "nodeworker pass 1" "$NODEWORKER_DATA_SRC" "$staging_dir/data/nodeworker" "$run_dir" "nodeworker"
  rsync_source_to_dest "collector runtime pass 1" "$COLLECTOR_RUNTIME_SRC" "$staging_dir/data/collector-runtime" "$run_dir" "collector-runtime"
  write_postgres_dumps "$staging_dir/data/postgres"
  rsync_source_to_dest "node pass 2" "$NODE_DATA_SRC" "$staging_dir/data/chain/node" "$run_dir" "chain-node"
  rsync_source_to_dest "nodeworker pass 2" "$NODEWORKER_DATA_SRC" "$staging_dir/data/nodeworker" "$run_dir" "nodeworker"
  rsync_source_to_dest "collector runtime pass 2" "$COLLECTOR_RUNTIME_SRC" "$staging_dir/data/collector-runtime" "$run_dir" "collector-runtime"

  log_info MainThread "promoting staging data into final data path"
  mv -- "$staging_dir/data" "$run_dir/data"
  log_info MainThread "removing empty staging dir"
  rmdir -- "$staging_dir"
  write_manifest "$run_dir"
  log_info MainThread "writing backup status: complete"
  printf 'complete\n' >"$run_dir/metadata/status.txt"

  log_notice MainThread "backup complete: $run_dir"
  printf 'BACKUP_RUN=%s\n' "$run_dir"
  printf 'BACKUP_LOG=%s\n' "$log_file"
  trap - EXIT
}

backup_run() {
  validate_static_config
  require_m5_backup_gate
  with_lock "$OPERATION_LOCK" backup_run_inner_locked
}

verify_run() {
  local run_path="${1:-}"
  local resolved_run

  validate_static_config
  [ -n "$run_path" ] || die "--verify requires a run path"
  resolved_run="$(resolve_completed_backup_run "$run_path")"
  print_boundary_notice >&2
  die "--verify is not approved in the current scaffold gate: $resolved_run"
}

list_runs() {
  validate_static_config
  [ -d "$BACKUP_ROOT/runs" ] || return 0
  while IFS= read -r candidate; do
    resolve_completed_backup_run "$candidate"
  done < <(find "$BACKUP_ROOT/runs" -mindepth 4 -maxdepth 4 -type d -name 'vqueen-v6.5.7-*' -print 2>/dev/null | sort)
}

main() {
  case "${1:-}" in
    --help|-h) [ "$#" -eq 1 ] || die "${1:-} does not accept extra arguments"; usage ;;
    --show-config) [ "$#" -eq 1 ] || die "--show-config does not accept extra arguments"; show_config ;;
    --preflight) [ "$#" -eq 1 ] || die "--preflight does not accept extra arguments"; preflight ;;
    --dry-run) [ "$#" -eq 1 ] || die "--dry-run does not accept extra arguments"; dry_run ;;
    --backup) [ "$#" -eq 1 ] || die "--backup does not accept extra arguments"; backup_run ;;
    --verify) [ "$#" -eq 2 ] || die "--verify requires exactly one run path"; shift; verify_run "${1:-}" ;;
    --list) [ "$#" -eq 1 ] || die "--list does not accept extra arguments"; list_runs ;;
    *) usage >&2; exit 2 ;;
  esac
}

main "$@"
