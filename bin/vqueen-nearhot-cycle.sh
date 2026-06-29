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
  vqueen-nearhot-cycle.sh --help
  vqueen-nearhot-cycle.sh --show-plan
  vqueen-nearhot-cycle.sh --scheduled-cycle

The scheduled cycle is the only automation entrypoint:
  preflight -> container backup -> manifest verify -> restore proof ->
  verify-only -> LKG promotion only when full restore evidence exists
USAGE
}

required_cycle_token() {
  printf 'vqueen-v6.5.7-restore-tested-cycle-2026-06-28\n'
}

required_restore_test_token_for_cycle() {
  printf 'vqueen-v6.5.7-restore-proof-2026-06-26\n'
}

require_cycle_gate() {
  local expected
  expected="$(required_cycle_token)"
  log_info CycleRunner "checking scheduled cycle approval token"
  [ "${VQUEEN_NEARHOT_CYCLE_APPROVED:-}" = "$expected" ] || \
    die "--scheduled-cycle requires the explicit near-hot cycle approval token"
  log_notice CycleRunner "scheduled cycle approval token accepted"
}

runner_network_name() {
  printf '%s\n' "$CONTAINER_RUNNER_NETWORK"
}

show_plan() {
  validate_static_config
  cat <<EOF
CYCLE_ENTRYPOINT=bin/vqueen-nearhot-cycle.sh --scheduled-cycle
PIPELINE=preflight,container-backup,manifest-verify,restore-proof,verify-only,lkg-promotion-if-full-evidence
BACKUP_ROOT=$BACKUP_ROOT
DEV_RESTORE_ROOT=$DEV_RESTORE_ROOT
CONTAINER_RUNNER_IMAGE=$CONTAINER_RUNNER_IMAGE
CONTAINER_RUNNER_NETWORK=$(runner_network_name)
POSTGRES_PASSWORD_FILE=$POSTGRES_PASSWORD_FILE
OPERATION_LOCK=$OPERATION_LOCK
CYCLE_LOCK=$CYCLE_LOCK
KEEP_LAST_KNOWN_GOOD=$KEEP_LAST_KNOWN_GOOD
CANDIDATE_RETENTION_DAYS=$CANDIDATE_RETENTION_DAYS
FAILED_RETENTION_DAYS=$FAILED_RETENTION_DAYS
EOF
}

cycle_preflight() {
  log_info CycleRunner "cycle preflight started"
  validate_static_config
  check_required_tools_for_backup
  require_tool docker
  require_tool tee
  [ -n "${CONTAINER_RUNNER_IMAGE:-}" ] || die "CONTAINER_RUNNER_IMAGE is required"
  [ -n "${POSTGRES_PASSWORD_FILE:-}" ] || die "POSTGRES_PASSWORD_FILE is required"
  [ -f "$POSTGRES_PASSWORD_FILE" ] || die "POSTGRES_PASSWORD_FILE is missing"
  [ -d "$BACKUP_ROOT" ] || die "BACKUP_ROOT must already exist: $BACKUP_ROOT"
  [ -d "$DEV_RESTORE_ROOT" ] || die "DEV_RESTORE_ROOT must already exist: $DEV_RESTORE_ROOT"
  "${PROJECT_ROOT}/bin/vqueen-backup.sh" --preflight
  check_postgres_container_identity
  log_info CycleRunner "cycle preflight completed"
}

run_container_backup() {
  local run_id="$1"
  local log_file="$2"
  local output_file="$3"
  local container_name

  container_name="vqueen-nearhot-runner-${run_id}"
  log_notice CycleRunner "containerized backup start: $run_id"
  docker run --rm \
    --name "$container_name" \
    --read-only \
    --cap-drop=ALL \
    --cap-add=CHOWN \
    --cap-add=DAC_OVERRIDE \
    --cap-add=FOWNER \
    --security-opt no-new-privileges \
    --pids-limit 512 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=256m \
    --network "$(runner_network_name)" \
    -e "BACKUP_ROOT=/backup" \
    -e "NODE_DATA_SRC=/src/chain-node" \
    -e "NODEWORKER_DATA_SRC=/src/nodeworker" \
    -e "COLLECTOR_RUNTIME_SRC=/src/collector-runtime" \
    -e "POSTGRES_HOST=$POSTGRES_CONTAINER" \
    -e "POSTGRES_PORT=5432" \
    -e "POSTGRES_DB=$POSTGRES_DB" \
    -e "POSTGRES_USER=$POSTGRES_USER" \
    -e "POSTGRES_PASSWORD_FILE=/run/secrets/postgres-password" \
    -e "RSYNC_NICE=$RSYNC_NICE" \
    -e "RSYNC_IONICE_CLASS=$RSYNC_IONICE_CLASS" \
    -e "RSYNC_IONICE_LEVEL=$RSYNC_IONICE_LEVEL" \
    -e "RSYNC_BWLIMIT_KB=$RSYNC_BWLIMIT_KB" \
    -e "LOG_LEVEL=$LOG_LEVEL" \
    -e "LOG_TO_SYSLOG=0" \
    -v "${NODE_DATA_SRC}:/src/chain-node:ro" \
    -v "${NODEWORKER_DATA_SRC}:/src/nodeworker:ro" \
    -v "${COLLECTOR_RUNTIME_SRC}:/src/collector-runtime:ro" \
    -v "${BACKUP_ROOT}:/backup:rw" \
    -v "${POSTGRES_PASSWORD_FILE}:/run/secrets/postgres-password:ro" \
    "$CONTAINER_RUNNER_IMAGE" \
    --candidate "$run_id" | tee -a "$log_file" | tee "$output_file"
  log_notice CycleRunner "containerized backup completed: $run_id"
}

host_backup_path_from_container_report() {
  local run_path="$1"

  case "$run_path" in
    /backup/*) printf '%s/%s\n' "${BACKUP_ROOT%/}" "${run_path#/backup/}" ;;
    *) printf '%s\n' "$run_path" ;;
  esac
}

extract_backup_run() {
  local output_file="$1"
  local run_path host_run_path

  run_path="$(awk -F= '/^BACKUP_RUN=/{print $2}' "$output_file" | tail -n 1)"
  [ -n "$run_path" ] || die "container backup did not report BACKUP_RUN"
  host_run_path="$(host_backup_path_from_container_report "$run_path")"
  require_completed_backup_run "$host_run_path"
}

verify_backup_manifest() {
  local backup_run="$1"

  log_info CycleRunner "manifest verification start: $backup_run"
  case "${LIVE_READ_ACCESS:-}" in
    controlled-sudo)
      [ -n "${MANIFEST_WRAPPER_BIN:-}" ] || die "MANIFEST_WRAPPER_BIN is required"
      "$SUDO_BIN" $SUDO_FLAGS "$MANIFEST_WRAPPER_BIN" verify "$backup_run" || die "manifest verification wrapper failed"
      ;;
    direct)
      (
        cd "$backup_run/data"
        sha256sum -c "$backup_run/manifests/file-manifest.sha256"
      ) >"$backup_run/manifests/file-manifest.verify.out"
      ;;
    *)
      die "LIVE_READ_ACCESS must be controlled-sudo or direct"
      ;;
  esac
  log_info CycleRunner "manifest verification complete: $backup_run"
}

run_restore_proof_for_backup() {
  local backup_run="$1"
  local output_file="$2"
  local restore_path

  log_notice CycleRunner "restore proof start: $backup_run"
  if ! VQUEEN_RESTORE_TEST_APPROVED="$(required_restore_test_token_for_cycle)" \
    "${PROJECT_ROOT}/bin/vqueen-restore-test.sh" --restore --backup "$backup_run" | tee "$output_file"; then
    die "restore proof command failed"
  fi
  restore_path="$(awk -F= '/^RESTORE_PATH=/{print $2}' "$output_file" | tail -n 1)"
  [ -n "$restore_path" ] || die "restore proof did not report RESTORE_PATH"
  "${PROJECT_ROOT}/bin/vqueen-restore-test.sh" --verify-only "$restore_path" >/dev/null
  log_notice CycleRunner "restore proof verified: $restore_path"
  RESTORE_PROOF_PATH="$restore_path"
}

full_restore_evidence_ready() {
  local restore_path="$1"
  local summary

  summary="$restore_path/evidence/restore-proof-summary.txt"
  [ -f "$summary" ] || return 1
  grep -qx 'STATUS=restore-proven' "$summary" || return 1
  grep -qx 'RESTORE_MANIFEST_FAILED_LINES=0' "$summary" || return 1
  grep -Eq '^POSTGRES_TABLE_COUNT=[1-9][0-9]*$' "$summary" || return 1
  grep -qx 'POSTGRES_RESTORE_RC=0' "$summary" || return 1
  grep -qx 'POSTGRES_SCHEMA_ONLY_RC=0' "$summary" || return 1
  grep -qx 'NODE_RESTORE_STARTED=1' "$summary" || return 1
  grep -qx 'NODEWORKER_RESTORE_STARTED=1' "$summary" || return 1
  grep -qx 'RPC_HEALTH_OK=1' "$summary" || return 1
  grep -qx 'SYNC_CATCHUP_OK=1' "$summary" || return 1
  grep -qx 'FATAL_LOG_SCAN_OK=1' "$summary" || return 1
}

record_lkg_promotion_blocked() {
  local backup_run="$1"
  local restore_path="$2"
  local marker

  marker="$backup_run/metadata/lkg-promotion-blocked.txt"
  {
    printf 'BACKUP_RUN=%s\n' "$backup_run"
    printf 'RESTORE_PATH=%s\n' "$restore_path"
    printf 'BLOCKED_UTC=%s\n' "$(date -u +%Y/%m/%dT%H:%M:%SZ)"
    printf 'REASON=full restore evidence missing: node,nodeworker,rpc,sync,fatal-log\n'
  } >"$marker"
  printf 'lkg-promotion-blocked\n' >"$backup_run/metadata/cycle-state.txt"
  log_warning CycleRunner "LKG promotion blocked until full restore evidence exists: backup=$backup_run restore=$restore_path"
}

promote_last_known_good() {
  local backup_run="$1"
  local restore_path="$2"
  local lkg_dir link tmp_link marker

  lkg_dir="${BACKUP_ROOT}/last-known-good"
  link="${lkg_dir}/current"
  tmp_link="${lkg_dir}/.current.tmp.$$"
  marker="$backup_run/metadata/lkg-promoted.txt"
  require_completed_backup_run "$backup_run"
  require_restore_target_path "$restore_path"
  "${PROJECT_ROOT}/bin/vqueen-restore-test.sh" --verify-only "$restore_path" >/dev/null
  full_restore_evidence_ready "$restore_path" || die "LKG promotion blocked: full restore evidence is missing"

  mkdir -p -- "$lkg_dir"
  ln -s -- "$backup_run" "$tmp_link"
  mv -Tf -- "$tmp_link" "$link"
  {
    printf 'BACKUP_RUN=%s\n' "$backup_run"
    printf 'RESTORE_PATH=%s\n' "$restore_path"
    printf 'PROMOTED_UTC=%s\n' "$(date -u +%Y/%m/%dT%H:%M:%SZ)"
  } >"$marker"
  printf 'lkg-promoted\n' >"$backup_run/metadata/cycle-state.txt"
  log_notice CycleRunner "last-known-good promoted: $backup_run"
}

retention_after_promotion() {
  local current_target index run_dir
  local -a lkg_runs

  current_target="$(resolve_latest_lkg)"
  log_info CycleRunner "retention start: current=$current_target"
  find "$BACKUP_ROOT/runs" -mindepth 4 -maxdepth 4 -type d -name 'vqueen-v6.5.7-*' \
    -mtime +"$FAILED_RETENTION_DAYS" -exec sh -c '
      for run_dir do
        [ "$run_dir" != "$1" ] || continue
        [ -f "$run_dir/metadata/status.txt" ] || continue
        if grep -qx failed "$run_dir/metadata/status.txt"; then
          rm -rf -- "$run_dir"
        fi
      done
    ' sh "$current_target" {} +

  mapfile -t lkg_runs < <(find "$BACKUP_ROOT/runs" -mindepth 4 -maxdepth 4 -type f -path '*/metadata/lkg-promoted.txt' -printf '%T@ %h\n' 2>/dev/null | sort -rn | awk '{print $2}')
  index=0
  for run_dir in "${lkg_runs[@]}"; do
    index=$((index + 1))
    [ "$run_dir" != "$current_target" ] || continue
    if [ "$index" -gt "$KEEP_LAST_KNOWN_GOOD" ]; then
      log_info CycleRunner "retention removing old promoted backup: $run_dir"
      rm -rf -- "$run_dir"
    fi
  done
  log_info CycleRunner "retention complete"
}

scheduled_cycle_inner_locked() {
  local run_id log_file container_output restore_output backup_run restore_path

  RUN_ID="${RUN_ID:-$(make_run_id)}"
  export RUN_ID
  run_id="$RUN_ID"
  setup_log_dir "$BACKUP_LOG_DIR"
  log_file="$BACKUP_LOG_DIR/cycle-$run_id.log"
  container_output="$(mktemp)"
  restore_output="$(mktemp)"
  trap 'rm -f "$container_output" "$restore_output"' RETURN
  exec > >(tee -a "$log_file") 2>&1

  cycle_preflight
  run_container_backup "$run_id" "$log_file" "$container_output"
  if ! backup_run="$(extract_backup_run "$container_output")"; then
    die "container backup did not produce a usable BACKUP_RUN"
  fi
  verify_backup_manifest "$backup_run"
  run_restore_proof_for_backup "$backup_run" "$restore_output"
  restore_path="$RESTORE_PROOF_PATH"
  if full_restore_evidence_ready "$restore_path"; then
    promote_last_known_good "$backup_run" "$restore_path"
    retention_after_promotion
  else
    record_lkg_promotion_blocked "$backup_run" "$restore_path"
  fi
  log_notice CycleRunner "scheduled near-hot cycle complete: backup=$backup_run restore=$restore_path"
}

scheduled_cycle() {
  require_cycle_gate
  validate_static_config
  with_lock "$OPERATION_LOCK" scheduled_cycle_inner_locked
}

main() {
  case "${1:-}" in
    --help|-h) [ "$#" -eq 1 ] || die "$1 does not accept extra arguments"; usage ;;
    --show-plan) [ "$#" -eq 1 ] || die "--show-plan does not accept extra arguments"; show_plan ;;
    --scheduled-cycle) [ "$#" -eq 1 ] || die "--scheduled-cycle does not accept extra arguments"; scheduled_cycle ;;
    *) usage >&2; exit 2 ;;
  esac
}

main "$@"
