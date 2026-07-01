#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"

# shellcheck source=../lib/vqueen-logging.sh
. "${PROJECT_ROOT}/lib/vqueen-logging.sh"

usage() {
  cat <<'USAGE'
Usage:
  vqueen-container-backup.sh --help
  vqueen-container-backup.sh --candidate <run-id>

Runs inside the locked-down near-hot backup container. The host cycle
orchestrator owns Docker, restore proof, known-good marking, and retention.
USAGE
}

die() {
  log_error ContainerBackup "$*"
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_env() {
  local name="$1"
  [ -n "${!name:-}" ] || die "required environment variable missing: $name"
}

require_dir() {
  local path="$1"
  [ -d "$path" ] || die "required directory missing: $path"
}

validate_run_id() {
  local value="$1"
  case "$value" in
    vqueen-v6.5.7-*) ;;
    *) die "run id must start with vqueen-v6.5.7-: $value" ;;
  esac
  case "$value" in
    */*|*'*'*|*'?'*|*'['*|*']'*) die "run id must be a literal id: $value" ;;
  esac
}

utc_day_path() {
  date -u +%Y/%m/%d
}

copy_source() {
  local label="$1"
  local src="$2"
  local dest="$3"
  local -a args
  local rc

  require_dir "$src"
  mkdir -p -- "$dest"
  args=(-aH --numeric-ids --delete --stats)
  if [ -n "${RSYNC_BWLIMIT_KB:-}" ]; then
    args+=(--bwlimit="$RSYNC_BWLIMIT_KB")
  fi

  log_info ContainerBackup "rsync start: $label"
  nice -n "${RSYNC_NICE:-15}" ionice -c "${RSYNC_IONICE_CLASS:-2}" -n "${RSYNC_IONICE_LEVEL:-7}" \
    rsync "${args[@]}" "${src%/}/" "${dest%/}/" || rc=$?
  rc="${rc:-0}"
  if [ "$rc" -eq 24 ]; then
    log_warning ContainerBackup "rsync live-source vanished files tolerated: $label"
  elif [ "$rc" -ne 0 ]; then
    die "rsync failed for $label with exit code $rc"
  fi
  log_info ContainerBackup "rsync complete: $label"
}

postgres_password() {
  require_env POSTGRES_PASSWORD_FILE
  [ -f "$POSTGRES_PASSWORD_FILE" ] || die "postgres password file missing"
  sed -n '1p' "$POSTGRES_PASSWORD_FILE"
}

write_postgres_dumps() {
  local dump_dir="$1"
  local password

  require_env POSTGRES_HOST
  require_env POSTGRES_PORT
  require_env POSTGRES_DB
  require_env POSTGRES_USER
  password="$(postgres_password)"
  [ -n "$password" ] || die "postgres password file is empty"

  mkdir -p -- "$dump_dir"
  log_info ContainerBackup "postgres custom dump start"
  PGPASSWORD="$password" pg_dump -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc \
    >"$dump_dir/${POSTGRES_DB}.custom.dump"
  log_info ContainerBackup "postgres custom dump complete"

  log_info ContainerBackup "postgres schema dump start"
  PGPASSWORD="$password" pg_dump -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" --schema-only \
    >"$dump_dir/${POSTGRES_DB}.schema.sql"
  log_info ContainerBackup "postgres schema dump complete"
}

write_manifest_and_verify() {
  local run_dir="$1"

  mkdir -p -- "$run_dir/manifests"
  log_info ContainerBackup "manifest generation start"
  (
    cd "$run_dir/data"
    find . -type f -print0 | sort -z | xargs -0 sha256sum >"$run_dir/manifests/file-manifest.sha256"
  )
  du -sh "$run_dir/data" >"$run_dir/manifests/data-size.txt"
  (
    cd "$run_dir/data"
    sha256sum -c "$run_dir/manifests/file-manifest.sha256"
  ) >"$run_dir/manifests/file-manifest.verify.out"
  log_info ContainerBackup "manifest generation and verification complete"
}

write_metadata() {
  local run_dir="$1"

  mkdir -p -- "$run_dir/metadata"
  {
    printf 'RUN_ID=%s\n' "$RUN_ID"
    printf 'RUN_DIR=%s\n' "$run_dir"
    printf 'UTC_TIME=%s\n' "$(date -u +%Y/%m/%dT%H:%M:%SZ)"
    printf 'CONTAINERIZED_BACKUP=1\n'
    printf 'CONTAINER_HOSTNAME=%s\n' "$(hostname)"
  } >"$run_dir/metadata/run.txt"
}

run_candidate() {
  local run_id="$1"
  local run_dir staging_dir

  RUN_ID="$run_id"
  export RUN_ID
  validate_run_id "$RUN_ID"
  require_env BACKUP_ROOT
  require_env NODE_DATA_SRC
  require_env NODEWORKER_DATA_SRC
  require_env COLLECTOR_RUNTIME_SRC

  run_dir="${BACKUP_ROOT%/}/runs/$(utc_day_path)/${RUN_ID}"
  staging_dir="$run_dir/staging"
  [ ! -e "$run_dir" ] || die "run dir already exists: $run_dir"

  mkdir -p -- "$run_dir/metadata" "$staging_dir/data"
  printf 'running\n' >"$run_dir/metadata/status.txt"
  trap 'rc=$?; if [ "$rc" -ne 0 ] && [ -n "${run_dir:-}" ] && [ -d "$run_dir/metadata" ]; then log_error ContainerBackup "container backup failed with exit code $rc"; printf "failed\n" >"$run_dir/metadata/status.txt"; fi' EXIT

  write_metadata "$run_dir"
  copy_source "node pass 1" "$NODE_DATA_SRC" "$staging_dir/data/chain/node"
  copy_source "nodeworker pass 1" "$NODEWORKER_DATA_SRC" "$staging_dir/data/nodeworker"
  copy_source "collector runtime pass 1" "$COLLECTOR_RUNTIME_SRC" "$staging_dir/data/collector-runtime"
  write_postgres_dumps "$staging_dir/data/postgres"
  copy_source "node pass 2" "$NODE_DATA_SRC" "$staging_dir/data/chain/node"
  copy_source "nodeworker pass 2" "$NODEWORKER_DATA_SRC" "$staging_dir/data/nodeworker"
  copy_source "collector runtime pass 2" "$COLLECTOR_RUNTIME_SRC" "$staging_dir/data/collector-runtime"

  mv -- "$staging_dir/data" "$run_dir/data"
  rmdir -- "$staging_dir"
  write_manifest_and_verify "$run_dir"
  printf 'backup-complete\n' >"$run_dir/metadata/cycle-state.txt"
  printf 'complete\n' >"$run_dir/metadata/status.txt"
  log_notice ContainerBackup "container backup complete: $run_dir"
  printf 'BACKUP_RUN=%s\n' "$run_dir"
  trap - EXIT
}

main() {
  case "${1:-}" in
    --help|-h) [ "$#" -eq 1 ] || die "$1 does not accept extra arguments"; usage ;;
    --candidate) [ "$#" -eq 2 ] || die "--candidate requires exactly one run id"; run_candidate "$2" ;;
    *) usage >&2; exit 2 ;;
  esac
}

main "$@"
