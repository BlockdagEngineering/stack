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
  vqueen-restore-test.sh --help
  vqueen-restore-test.sh --preflight --backup <backup-id|path|latest>
  vqueen-restore-test.sh --dry-run --backup <backup-id|path|latest>
  vqueen-restore-test.sh --restore --backup <backup-id|path|latest>
  vqueen-restore-test.sh --verify-only <restore-path>
  vqueen-restore-test.sh --cleanup <restore-path>

Current scaffold status:
  Runtime restore proof modes require an explicit active-gate approval token.
USAGE
}

backup_arg=""

parse_backup_arg() {
  log_debug MainThread "parsing restore backup argument"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --backup)
        shift
        [ -z "$backup_arg" ] || die "--backup may be specified only once"
        backup_arg="${1:-}"
        [ -n "$backup_arg" ] || die "--backup requires a value"
        ;;
      *)
        die "unknown restore argument: $1"
        ;;
    esac
    shift || true
  done
}

resolve_backup() {
  log_info MainThread "resolving backup selector: $1"
  resolve_completed_backup_run "$1"
}

resolve_restore_path_for_backup() {
  local backup_path="$1"
  local restore_path

  restore_path="${DEV_RESTORE_ROOT}/$(basename "$backup_path")"
  log_info MainThread "resolving restore path: $restore_path"
  require_restore_target_path "$restore_path"
  printf '%s\n' "$restore_path"
}

restore_preflight() {
  log_info MainThread "restore preflight started"
  validate_static_config
  check_required_tools_for_preflight
  [ -n "$backup_arg" ] || die "restore preflight requires --backup"
  local backup_path restore_path
  backup_path="$(resolve_backup "$backup_arg")"
  restore_path="$(resolve_restore_path_for_backup "$backup_path")"
  log_info MainThread "restore static path checks passed: $restore_path"
}

restore_dry_run() {
  log_info MainThread "restore dry-run started"
  restore_preflight
  local backup_path restore_path
  backup_path="$(resolve_backup "$backup_arg")"
  restore_path="$(resolve_restore_path_for_backup "$backup_path")"
  log_info MainThread "restore dry-run rendered plan: $restore_path"
  cat <<EOF
DRY_RUN=1
BACKUP_PATH=$backup_path
RESTORE_PATH=$restore_path
RESTORE_COMPOSE_PROJECT=${RESTORE_COMPOSE_PROJECT_PREFIX}-$(basename "$backup_path")
RESTORE_RPC=${RESTORE_RPC_HOST}:${RESTORE_RPC_PORT}
RESTORE_POSTGRES_PORT=$RESTORE_POSTGRES_PORT
OPERATION_LOCK=$OPERATION_LOCK
RESTORE_LOCK=$RESTORE_LOCK
RESTORE_EXPECT_NODE_DATA=$backup_path/data/chain/node
RESTORE_EXPECT_NODEWORKER_DATA=$backup_path/data/nodeworker
RESTORE_EXPECT_COLLECTOR_RUNTIME=$backup_path/data/collector-runtime
RESTORE_EXPECT_POSTGRES_DUMP=$backup_path/data/postgres
RESTORE_PROBES=node-container,nodeworker-container,collector-runtime-exists,rpc-health,sync-progress,fatal-log-scan
EOF
}

required_restore_test_token() {
  printf 'vqueen-v6.5.7-restore-proof-2026-06-26\n'
}

require_restore_gate() {
  local expected
  expected="$(required_restore_test_token)"
  log_info MainThread "checking restore proof approval token"
  [ "${VQUEEN_RESTORE_TEST_APPROVED:-}" = "$expected" ] || \
    die "--restore requires the explicit restore proof approval token for this run"
  log_notice MainThread "restore proof approval token accepted"
}

restore_container_name() {
  local restore_path="$1"
  printf 'vqueen-restore-postgres-%s\n' "$(basename -- "$restore_path" | sed -E 's/[^A-Za-z0-9_.-]+/-/g' | cut -c1-90)"
}

wait_for_restore_postgres() {
  local container="$1"
  local attempt

  for attempt in $(seq 1 60); do
    if docker exec "$container" pg_isready -h 127.0.0.1 -p 5432 -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
      log_info MainThread "restore postgres is ready: $container"
      return 0
    fi
    sleep 1
  done
  die "restore postgres did not become ready: $container"
}

run_postgres_restore_proof() {
  local restore_path="$1"
  local container
  local password
  local dump_path
  local schema_path
  local schema_probe_db
  local evidence_dir
  local pg_restore_rc psql_rc table_count

  container="$(restore_container_name "$restore_path")"
  password="restore-proof-${container}"
  dump_path="$restore_path/data/postgres/${POSTGRES_DB}.custom.dump"
  schema_path="$restore_path/data/postgres/${POSTGRES_DB}.schema.sql"
  schema_probe_db="${POSTGRES_DB}_schema_probe"
  evidence_dir="$restore_path/evidence"

  [ -f "$dump_path" ] || die "postgres custom dump missing: $dump_path"
  [ -f "$schema_path" ] || die "postgres schema dump missing: $schema_path"
  mkdir -p -- "$restore_path/postgres-data" "$evidence_dir"

  log_info MainThread "removing stale restore postgres container if present: $container"
  docker rm -f "$container" >/dev/null 2>&1 || true

  log_info MainThread "starting isolated restore postgres container: $container"
  docker run -d --name "$container" \
    -p "${RESTORE_RPC_HOST}:${RESTORE_POSTGRES_PORT}:5432" \
    -e "POSTGRES_USER=$POSTGRES_USER" \
    -e "POSTGRES_DB=$POSTGRES_DB" \
    -e "POSTGRES_PASSWORD=$password" \
    -v "$restore_path/postgres-data:/var/lib/postgresql/data" \
    -v "$restore_path/data/postgres:/restore-postgres:ro" \
    "$POSTGRES_IMAGE_FAMILY" >"$evidence_dir/postgres-container-id.txt"

  wait_for_restore_postgres "$container"

  log_info MainThread "capturing pg_restore list"
  docker exec "$container" pg_restore -l "/restore-postgres/${POSTGRES_DB}.custom.dump" \
    >"$evidence_dir/pg-restore-list.txt"

  log_info MainThread "restoring postgres custom dump into isolated container"
  set +e
  docker exec -e "PGPASSWORD=$password" "$container" \
    pg_restore --exit-on-error --single-transaction -h 127.0.0.1 -p 5432 \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" "/restore-postgres/${POSTGRES_DB}.custom.dump" \
    >"$evidence_dir/pg-restore.out" 2>"$evidence_dir/pg-restore.err"
  pg_restore_rc=$?
  set -e
  printf '%s\n' "$pg_restore_rc" >"$evidence_dir/pg-restore.rc"
  [ "$pg_restore_rc" -eq 0 ] || die "postgres custom restore failed: rc=$pg_restore_rc"
  [ ! -s "$evidence_dir/pg-restore.err" ] || die "postgres custom restore wrote stderr"

  log_info MainThread "restoring schema-only dump into isolated probe database"
  docker exec -e "PGPASSWORD=$password" "$container" \
    createdb -h 127.0.0.1 -p 5432 -U "$POSTGRES_USER" "$schema_probe_db" \
    >"$evidence_dir/pg-schema-createdb.out" 2>"$evidence_dir/pg-schema-createdb.err"
  set +e
  docker exec -i -e "PGPASSWORD=$password" "$container" \
    psql -h 127.0.0.1 -p 5432 -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$schema_probe_db" --single-transaction \
    >"$evidence_dir/pg-schema-only.out" 2>"$evidence_dir/pg-schema-only.err" \
    <"$schema_path"
  psql_rc=$?
  set -e
  printf '%s\n' "$psql_rc" >"$evidence_dir/pg-schema-only.rc"
  [ "$psql_rc" -eq 0 ] || die "postgres schema-only restore failed: rc=$psql_rc"
  [ ! -s "$evidence_dir/pg-schema-only.err" ] || die "postgres schema-only restore wrote stderr"

  log_info MainThread "running postgres restore smoke queries"
  docker exec -e "PGPASSWORD=$password" "$container" \
    psql -h 127.0.0.1 -p 5432 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc \
    "select count(*) from information_schema.tables where table_schema not in ('pg_catalog','information_schema');" \
    >"$evidence_dir/postgres-user-table-count.txt"
  table_count="$(cat "$evidence_dir/postgres-user-table-count.txt")"
  case "$table_count" in
    ""|*[!0-9]*) die "postgres user table count is not numeric: $table_count" ;;
    0) die "postgres user table count is zero" ;;
  esac
  docker exec -e "PGPASSWORD=$password" "$container" \
    psql -h 127.0.0.1 -p 5432 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select current_database();" \
    >"$evidence_dir/postgres-current-database.txt"

  log_info MainThread "stopping isolated restore postgres container: $container"
  docker rm -f "$container" >"$evidence_dir/postgres-container-removed.txt"
}

write_restore_summary() {
  local backup_path="$1"
  local restore_path="$2"
  local verify_lines failed_lines table_count

  verify_lines="$(wc -l <"$restore_path/manifests/file-manifest.restore-verify.out")"
  failed_lines="$(grep -c -v ': OK$' "$restore_path/manifests/file-manifest.restore-verify.out" || true)"
  table_count="$(cat "$restore_path/evidence/postgres-user-table-count.txt")"

  cat >"$restore_path/evidence/restore-proof-summary.txt" <<EOF
BACKUP_PATH=$backup_path
RESTORE_PATH=$restore_path
STATUS=restore-proven
RESTORE_MANIFEST_LINES=$verify_lines
RESTORE_MANIFEST_FAILED_LINES=$failed_lines
POSTGRES_TABLE_COUNT=$table_count
POSTGRES_RESTORE_RC=$(cat "$restore_path/evidence/pg-restore.rc")
POSTGRES_SCHEMA_ONLY_RC=$(cat "$restore_path/evidence/pg-schema-only.rc")
POSTGRES_DUMP=$restore_path/data/postgres/${POSTGRES_DB}.custom.dump
EOF
}

verify_restore_evidence() {
  local restore_path="$1"
  local summary

  summary="$restore_path/evidence/restore-proof-summary.txt"
  [ -f "$summary" ] || die "restore proof summary missing: $summary"
  grep -qx 'STATUS=restore-proven' "$summary" || die "restore summary status is not restore-proven"
  grep -qx 'RESTORE_MANIFEST_FAILED_LINES=0' "$summary" || die "restore manifest verification has failures"
  grep -Eq '^POSTGRES_TABLE_COUNT=[1-9][0-9]*$' "$summary" || die "restore postgres table count is empty"
  grep -qx 'POSTGRES_RESTORE_RC=0' "$summary" || die "restore postgres rc is not zero"
  grep -qx 'POSTGRES_SCHEMA_ONLY_RC=0' "$summary" || die "restore schema-only rc is not zero"
  [ -f "$restore_path/evidence/pg-restore.err" ] || die "restore stderr evidence missing"
  [ ! -s "$restore_path/evidence/pg-restore.err" ] || die "restore stderr is not empty"
  [ -f "$restore_path/evidence/pg-schema-only.err" ] || die "schema-only stderr evidence missing"
  [ ! -s "$restore_path/evidence/pg-schema-only.err" ] || die "schema-only stderr is not empty"
}

restore_run_inner_locked() {
  local backup_path restore_path log_file

  [ -n "$backup_arg" ] || die "restore requires --backup"
  backup_path="$(resolve_backup "$backup_arg")"
  restore_path="$(resolve_restore_path_for_backup "$backup_path")"

  setup_log_dir "$RESTORE_LOG_DIR"
  log_file="$RESTORE_LOG_DIR/restore-$(basename -- "$backup_path").log"
  exec > >(tee -a "$log_file") 2>&1
  log_info MainThread "restore proof log attached: $log_file"
  log_info MainThread "restore proof started: backup=$backup_path restore=$restore_path"

  validate_static_config
  check_required_tools_for_backup
  require_restore_gate
  [ -n "${RESTORE_WRAPPER_BIN:-}" ] || die "RESTORE_WRAPPER_BIN is required"
  "$SUDO_BIN" $SUDO_FLAGS "$RESTORE_WRAPPER_BIN" copy-verify "$backup_path" "$restore_path" || die "restore wrapper failed"
  run_postgres_restore_proof "$restore_path"
  write_restore_summary "$backup_path" "$restore_path"
  verify_restore_evidence "$restore_path"
  printf 'restore-proven\n' >"$restore_path/metadata/status.txt"

  log_notice MainThread "restore proof complete: $restore_path"
  printf 'RESTORE_PATH=%s\n' "$restore_path"
  printf 'RESTORE_LOG=%s\n' "$log_file"
}

restore_run() {
  validate_static_config
  if operation_lock_already_held; then
    log_info MainThread "using inherited operation lock: $OPERATION_LOCK"
    restore_run_inner_locked
    return
  fi
  with_lock "$OPERATION_LOCK" restore_run_inner_locked
}

verify_only() {
  local restore_path="${1:-}"
  local summary

  log_info MainThread "verify-only started"
  validate_static_config
  [ -n "$restore_path" ] || die "--verify-only requires restore path"
  require_restore_target_path "$restore_path"
  [ -d "$restore_path" ] || die "restore path missing: $restore_path"
  [ -f "$restore_path/metadata/status.txt" ] || die "restore status missing: $restore_path"
  if ! grep -Eqx 'complete|restore-proven' "$restore_path/metadata/status.txt"; then
    die "restore status is not complete or restore-proven: $restore_path"
  fi
  summary="$restore_path/evidence/restore-proof-summary.txt"
  verify_restore_evidence "$restore_path"
  cat "$summary"
  log_notice MainThread "verify-only complete: $restore_path"
}

cleanup_refuse_until_gate() {
  local restore_path="${1:-}"
  log_info MainThread "cleanup gate check started"
  validate_static_config
  [ -n "$restore_path" ] || die "--cleanup requires restore path"
  require_restore_target_path "$restore_path"
  print_boundary_notice >&2
  die "--cleanup is not approved in the current scaffold gate: $restore_path"
}

main() {
  local mode="${1:-}"
  case "$mode" in
    --help|-h) [ "$#" -eq 1 ] || die "$mode does not accept extra arguments"; usage ;;
    --preflight) shift; parse_backup_arg "$@"; restore_preflight ;;
    --dry-run) shift; parse_backup_arg "$@"; restore_dry_run ;;
    --restore) shift; parse_backup_arg "$@"; restore_run ;;
    --verify-only) [ "$#" -eq 2 ] || die "--verify-only requires exactly one restore path"; shift; verify_only "${1:-}" ;;
    --cleanup) [ "$#" -eq 2 ] || die "--cleanup requires exactly one restore path"; shift; cleanup_refuse_until_gate "${1:-}" ;;
    *) usage >&2; exit 2 ;;
  esac
}

main "$@"
