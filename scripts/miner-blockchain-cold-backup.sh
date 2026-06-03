#!/usr/bin/env bash
set -euo pipefail

# Miner single-node cold blockchain backup.
# Takes a mostly-preseeded copy, then briefly stops the live node/pool/dashboard
# for a final consistent rsync of BDAG node volumes. Postgres stays running and
# is dumped after the node/pool are stopped.

ROOT="${ROOT:-/opt/miner/pool-stack-docker}"
PROJECT="${PROJECT:-bdagminer}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose-miner.yml}"
ENV_FILE="${ENV_FILE:-.env}"
BACKUP_ROOT="${BACKUP_ROOT:-/opt/miner/backups/blockchain}"
STATE_ROOT="${STATE_ROOT:-/opt/miner/backup-state}"
LOG_DIR="${LOG_DIR:-/opt/miner/logs}"
LOG="${LOG:-$LOG_DIR/blockchain-backup.log}"
LOCK_FILE="${LOCK_FILE:-$STATE_ROOT/blockchain-backup.lock}"
STATE_FILE="${STATE_FILE:-$STATE_ROOT/blockchain-backup-state.json}"
REPORT_DIR="${REPORT_DIR:-$STATE_ROOT/backup-reports}"
PENDING_FILE="${PENDING_FILE:-$STATE_ROOT/blockchain-backup.pending}"
MANIFEST_FILE_NAME="${MANIFEST_FILE_NAME:-RESTORE-MANIFEST.json}"
KEEP="${KEEP:-6}"

NODE_VOLUME="${NODE_VOLUME:-bdagminer_node-data}"
NODEWORKER_VOLUME="${NODEWORKER_VOLUME:-bdagminer_nodeworker-data}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-bdagminer-postgres-1}"
POSTGRES_USER="${POSTGRES_USER:-bdag_pool}"
POSTGRES_DB="${POSTGRES_DB:-bdagpool}"

HEALTH_DB_MAX_AGE_SECONDS="${HEALTH_DB_MAX_AGE_SECONDS:-1200}"
HEALTH_LOG_WINDOW="${HEALTH_LOG_WINDOW:-20m}"
HEALTH_MIN_POOL_EVENTS="${HEALTH_MIN_POOL_EVENTS:-1}"

STOP_SERVICES=(pool dashboard node)
START_SERVICES=(node pool dashboard)
LIVE_SERVICES_STOPPED=0
RSYNC_COMMON_ARGS=(-aH --delete --exclude LOCK)

NODE_EVM_RPC="${NODE_EVM_RPC:-http://127.0.0.1:18545}"
NODE_RPC_MAX_TIME="${NODE_RPC_MAX_TIME:-5}"

mkdir -p "$BACKUP_ROOT/archive" "$BACKUP_ROOT/current" "$STATE_ROOT" "$REPORT_DIR" "$LOG_DIR"
cd "$ROOT"

ts="$(date +%Y%m%d-%H%M%S)"
dated="$BACKUP_ROOT/archive/$ts"
staging="$BACKUP_ROOT/staging/$ts"

log() { printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*" | tee -a "$LOG"; }
json_string() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "${1:-}"; }
compose() { docker compose -p "$PROJECT" -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

write_state() {
  local status="$1"
  cat > "$STATE_FILE" <<JSON
{
  "active": true,
  "status": $(json_string "$status"),
  "updated_at": $(json_string "$(date -Is)"),
  "backup_root": $(json_string "$BACKUP_ROOT"),
  "staging_dir": $(json_string "$staging"),
  "archive_dir": $(json_string "$dated")
}
JSON
}

clear_state() {
  cat > "$STATE_FILE" <<JSON
{
  "active": false,
  "finished_at": $(json_string "$(date -Is)"),
  "last_archive_dir": $(json_string "$dated")
}
JSON
}

cleanup_failed_staging() {
  if [[ -n "${staging:-}" && "$staging" == "$BACKUP_ROOT/staging/"* && -d "$staging" ]]; then
    log "Removing failed staging directory: $staging"
    rm -rf "$staging"
  fi
}

write_failure_report() {
  local reason="$1" report_file
  report_file="$REPORT_DIR/blockchain-backup-failure-$(date +%Y%m%d-%H%M%S).json"
  cat > "$report_file" <<JSON
{
  "type": "miner_cold_blockchain_backup_failure",
  "created_at": $(json_string "$(date -Is)"),
  "reason": $(json_string "$reason"),
  "backup_root": $(json_string "$BACKUP_ROOT"),
  "state_file": $(json_string "$STATE_FILE"),
  "report_file": $(json_string "$report_file")
}
JSON
  log "Wrote failure report: $report_file"
}

write_skip_report() {
  local reason="$1" report_file
  report_file="$REPORT_DIR/blockchain-backup-skipped-$(date +%Y%m%d-%H%M%S).json"
  cat > "$report_file" <<JSON
{
  "type": "miner_cold_blockchain_backup_skipped",
  "created_at": $(json_string "$(date -Is)"),
  "reason": $(json_string "$reason"),
  "pending_file": $(json_string "$PENDING_FILE"),
  "backup_root": $(json_string "$BACKUP_ROOT"),
  "report_file": $(json_string "$report_file")
}
JSON
  log "Wrote skip report: $report_file"
}

mark_backup_pending() {
  local reason="$1"
  cat > "$PENDING_FILE" <<JSON
{
  "pending": true,
  "created_at": $(json_string "$(date -Is)"),
  "reason": $(json_string "$reason"),
  "retry_policy": "retry when health gate passes"
}
JSON
  log "Marked backup pending: $PENDING_FILE"
}

clear_backup_pending() {
  if [[ -f "$PENDING_FILE" ]]; then
    rm -f "$PENDING_FILE"
    log "Cleared pending backup marker"
  fi
}

volume_mountpoint() {
  docker volume inspect -f '{{.Mountpoint}}' "$1"
}

container_running() {
  [[ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || true)" == "true" ]]
}

snapshot_metadata() {
  local target="$1/metadata"
  mkdir -p "$target"
  docker ps -a > "$target/docker-ps-a.txt" 2>&1 || true
  compose ps > "$target/docker-compose-ps.txt" 2>&1 || true
  docker inspect bdagminer-node-1 > "$target/docker-inspect-bdagminer-node-1.json" 2>&1 || true
  docker inspect bdagminer-pool-1 > "$target/docker-inspect-bdagminer-pool-1.json" 2>&1 || true
  docker inspect "$POSTGRES_CONTAINER" > "$target/docker-inspect-$POSTGRES_CONTAINER.json" 2>&1 || true
  docker logs --timestamps --since 24h bdagminer-node-1 > "$target/docker-logs-bdagminer-node-1.log" 2>&1 || true
  docker logs --timestamps --since 24h bdagminer-pool-1 > "$target/docker-logs-bdagminer-pool-1.log" 2>&1 || true
  docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc \
    "select 'latest_block='||coalesce(max(created_at)::text,''); select 'blocks='||count(*) from blocks; select 'credits='||count(*) from credits;" \
    > "$target/db-summary.txt" 2>&1 || true
}

rsync_volume() {
  local volume="$1" target="$2" src
  src="$(volume_mountpoint "$volume")"
  mkdir -p "$target"
  rsync "${RSYNC_COMMON_ARGS[@]}" "$src/" "$target/"
}

rsync_volume_live_presync() {
  local volume="$1" target="$2" src rc
  src="$(volume_mountpoint "$volume")"
  mkdir -p "$target"

  set +e
  rsync "${RSYNC_COMMON_ARGS[@]}" "$src/" "$target/"
  rc=$?
  set -e

  if [[ "$rc" -eq 0 ]]; then
    return 0
  fi

  if [[ "$rc" -eq 24 ]]; then
    log "Live pre-sync warning: rsync saw vanished files for volume=$volume target=$target; continuing because final offline sync will verify consistency"
    return 0
  fi

  return "$rc"
}

postgres_healthy() {
  [[ "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$POSTGRES_CONTAINER" 2>/dev/null || true)" == "healthy" ]]
}

db_age_seconds() {
  local table="$1"
  docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atq -c     "select coalesce(extract(epoch from now() - max(created_at))::int,999999) from $table;"     2>/dev/null | tr -d '\r' | head -1
}

recent_log_count() {
  local container="$1" pattern="$2"
  docker logs --since "$HEALTH_LOG_WINDOW" "$container" 2>&1 | grep -Eci "$pattern" || true
}

node_rpc_json() {
  local method="$1"
  curl -fsS --max-time "$NODE_RPC_MAX_TIME" \
    -H 'Content-Type: application/json' \
    --data "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$method\",\"params\":[]}" \
    "$NODE_EVM_RPC"
}

node_block_number() {
  node_rpc_json eth_blockNumber \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("result", ""))'
}

node_is_syncing() {
  local result
  result="$(node_rpc_json eth_syncing | python3 -c 'import json,sys; print(json.load(sys.stdin).get("result"))')"
  [[ "$result" != "False" && "$result" != "false" ]]
}

health_gate() {
  local block_age credit_age pool_events node_bad pool_bad evm_block
  log "Running pre-backup health gate"
  write_state "health_gate"

  container_running bdagminer-node-1 || { write_failure_report "health gate failed: node container not running"; return 1; }
  container_running bdagminer-pool-1 || { write_failure_report "health gate failed: pool container not running"; return 1; }
  container_running bdagminer-dashboard-1 || { write_failure_report "health gate failed: dashboard container not running"; return 1; }
  container_running "$POSTGRES_CONTAINER" || { write_failure_report "health gate failed: postgres container not running"; return 1; }
  postgres_healthy || { write_failure_report "health gate failed: postgres container not healthy"; return 1; }

  evm_block="$(node_block_number 2>/dev/null || true)"
  [[ "$evm_block" =~ ^0x[0-9a-fA-F]+$ ]] || {
    write_failure_report "health gate failed: node EVM RPC unavailable or invalid block number"
    return 1
  }

  if node_is_syncing; then
    write_failure_report "health gate failed: node reports eth_syncing"
    return 1
  fi

  block_age="$(db_age_seconds blocks)"
  credit_age="$(db_age_seconds credits)"
  [[ "$block_age" =~ ^[0-9]+$ ]] || { write_failure_report "health gate failed: could not read block age"; return 1; }
  [[ "$credit_age" =~ ^[0-9]+$ ]] || { write_failure_report "health gate failed: could not read credit age"; return 1; }
  if (( block_age > HEALTH_DB_MAX_AGE_SECONDS || credit_age > HEALTH_DB_MAX_AGE_SECONDS )); then
    write_failure_report "health gate failed: stale DB production block_age=${block_age}s credit_age=${credit_age}s max=${HEALTH_DB_MAX_AGE_SECONDS}s"
    return 1
  fi

  node_bad="$(recent_log_count bdagminer-node-1 'fatal|panic|Illegal withdrawal|Head block is not reachable|database corruption|corrupt|invalid chunk|unknown ancestor|Failed to truncate|Node is Syncing|initial download|downloading blocks')"
  pool_bad="$(recent_log_count bdagminer-pool-1 'fatal|panic|wallet.*disconnect|Wallet.*Disconnected|database corruption|corrupt|Block Template Request Rejected : Node is Syncing')"
  if (( node_bad > 0 || pool_bad > 0 )); then
    write_failure_report "health gate failed: bad recent log signs node_bad=${node_bad} pool_bad=${pool_bad} window=${HEALTH_LOG_WINDOW}"
    return 1
  fi

  pool_events="$(recent_log_count bdagminer-pool-1 'valid share accepted|BLOCK FOUND|Block submitted successfully|Block saved and credited')"
  if (( pool_events < HEALTH_MIN_POOL_EVENTS )); then
    write_failure_report "health gate failed: insufficient recent pool activity events=${pool_events} min=${HEALTH_MIN_POOL_EVENTS} window=${HEALTH_LOG_WINDOW}"
    return 1
  fi

  log "Health gate passed: evm_block=${evm_block} block_age=${block_age}s credit_age=${credit_age}s pool_events=${pool_events} node_bad=${node_bad} pool_bad=${pool_bad}"
}

post_restore_health_gate() {
  local block_age credit_age
  log "Running post-backup health check"
  write_state "post_restore_health_gate"
  verify_live_restored
  postgres_healthy || { write_failure_report "post-backup health failed: postgres not healthy"; return 1; }
  block_age="$(db_age_seconds blocks)"
  credit_age="$(db_age_seconds credits)"
  [[ "$block_age" =~ ^[0-9]+$ ]] || { write_failure_report "post-backup health failed: could not read block age"; return 1; }
  [[ "$credit_age" =~ ^[0-9]+$ ]] || { write_failure_report "post-backup health failed: could not read credit age"; return 1; }
  if (( block_age > HEALTH_DB_MAX_AGE_SECONDS || credit_age > HEALTH_DB_MAX_AGE_SECONDS )); then
    write_failure_report "post-backup health failed: stale DB production block_age=${block_age}s credit_age=${credit_age}s max=${HEALTH_DB_MAX_AGE_SECONDS}s"
    return 1
  fi
  log "Post-backup health passed: block_age=${block_age}s credit_age=${credit_age}s"
}

pre_sync() {
  log "Pre-syncing live node volumes to reduce offline time"
  write_state "pre_syncing_live_volumes"
  mkdir -p "$staging/volumes"
  rsync_volume_live_presync "$NODE_VOLUME" "$staging/volumes/node-data"
  rsync_volume_live_presync "$NODEWORKER_VOLUME" "$staging/volumes/nodeworker-data"
}

stop_live_services() {
  log "Stopping live single-node services for final consistent copy: ${STOP_SERVICES[*]}"
  write_state "stopping_live_services"
  compose stop "${STOP_SERVICES[@]}"
  LIVE_SERVICES_STOPPED=1
}

start_live_services() {
  log "Starting live services: ${START_SERVICES[*]}"
  write_state "starting_live_services"
  compose up -d --no-build "${START_SERVICES[@]}"
  LIVE_SERVICES_STOPPED=0
}

final_sync_offline() {
  log "Final offline sync of node volumes"
  write_state "final_offline_sync"
  rsync_volume "$NODE_VOLUME" "$staging/volumes/node-data"
  rsync_volume "$NODEWORKER_VOLUME" "$staging/volumes/nodeworker-data"
}

dump_database() {
  log "Dumping Miner pool DB"
  write_state "dumping_database"
  mkdir -p "$staging/database"
  container_running "$POSTGRES_CONTAINER" || { write_failure_report "Postgres container is not running: $POSTGRES_CONTAINER"; return 1; }
  docker exec "$POSTGRES_CONTAINER" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" | gzip -9 > "$staging/database/pool.sql.gz"
  gzip -t "$staging/database/pool.sql.gz"
}

write_restore_manifest() {
  local target="$1" node_mp worker_mp bdag_sst evm_sst evm_block
  node_mp="$(volume_mountpoint "$NODE_VOLUME")"
  worker_mp="$(volume_mountpoint "$NODEWORKER_VOLUME")"
  bdag_sst="$(find "$target/volumes/node-data/mainnet/BdagChain" -maxdepth 1 -type f -name '*.sst' 2>/dev/null | wc -l)"
  evm_sst="$(find "$target/volumes/node-data/mainnet/bdageth/chaindata" -maxdepth 1 -type f -name '*.sst' 2>/dev/null | wc -l)"
  evm_block="$(node_block_number 2>/dev/null || true)"

  cat > "$target/$MANIFEST_FILE_NAME" <<JSON
{
  "type": "bdag_miner_blockchain_backup",
  "created_at": $(json_string "$(date -Is)"),
  "backup_dir": $(json_string "$target"),
  "source": {
    "node_volume": $(json_string "$NODE_VOLUME"),
    "node_mountpoint": $(json_string "$node_mp"),
    "nodeworker_volume": $(json_string "$NODEWORKER_VOLUME"),
    "nodeworker_mountpoint": $(json_string "$worker_mp")
  },
  "capture_policy": {
    "live_presync_rsync_24_allowed": true,
    "final_offline_sync_strict": true,
    "lock_files_excluded": true,
    "expected_restored_ownership": "999:999",
    "final_stopped_services": $(json_string "${STOP_SERVICES[*]}")
  },
  "chain_inventory": {
    "evm_block_number": $(json_string "$evm_block"),
    "bdagchain_sst_files": $bdag_sst,
    "evm_chaindata_sst_files": $evm_sst
  },
  "restore_order": [
    "stop all miner containers",
    "restore fresh from backup/source",
    "remove only LOCK files",
    "chown -R 999:999 node and nodeworker volumes",
    "start node only",
    "verify node RPC",
    "start pool and dashboard"
  ]
}
JSON
}

archive_backup() {
  log "Archiving backup to $dated"
  write_state "archiving"
  mkdir -p "$dated"
  cp -a --reflink=auto "$staging/volumes" "$dated/"
  cp -a --reflink=auto "$staging/database" "$dated/"
  snapshot_metadata "$dated"
  write_restore_manifest "$dated"
  (cd "$dated" && find volumes database metadata "$MANIFEST_FILE_NAME" -type f -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
  du -sh "$dated" > "$dated/SIZE.txt"
  rm -f "$BACKUP_ROOT/current/latest"
  ln -s "../archive/$ts" "$BACKUP_ROOT/current/latest"
}

apply_retention() {
  log "Keeping latest $KEEP dated backups"
  write_state "retention"
  find "$BACKUP_ROOT/archive" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
    | sort -rn \
    | tail -n +$((KEEP + 1)) \
    | cut -d' ' -f2- \
    | while read -r old; do
        [[ -n "$old" ]] || continue
        log "Removing old backup: $old"
        rm -rf "$old"
      done
}

verify_live_restored() {
  log "Verifying live services are back up"
  write_state "verifying_live_services"
  container_running bdagminer-node-1 || { write_failure_report "node did not restart"; return 1; }
  container_running bdagminer-pool-1 || { write_failure_report "pool did not restart"; return 1; }
  container_running bdagminer-dashboard-1 || { write_failure_report "dashboard did not restart"; return 1; }
}

main() {
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    log "Another blockchain backup is already running: $LOCK_FILE"
    exit 1
  fi

  write_state "starting"
  mkdir -p "$staging"
  snapshot_metadata "$staging"
  if ! health_gate; then
    local reason="scheduled backup skipped because health gate failed; retry when node is healthy"
    write_skip_report "$reason"
    mark_backup_pending "$reason"
    cleanup_failed_staging || true
    clear_state
    log "$reason"
    exit 0
  fi
  pre_sync
  stop_live_services
  final_sync_offline
  dump_database
  start_live_services
  post_restore_health_gate
  archive_backup
  apply_retention
  rm -rf "$staging"
  clear_backup_pending
  clear_state
  log "Cold backup complete: $dated"
}

retry_pending() {
  if [[ ! -f "$PENDING_FILE" ]]; then
    log "No pending blockchain backup marker; retry check exits"
    exit 0
  fi

  log "Pending backup marker exists; running backup if health gate passes"
  main
}

if [[ "${1:-}" == "--health-check" ]]; then
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    log "Another blockchain backup/check is already running: $LOCK_FILE"
    exit 1
  fi
  write_state "manual_health_check"
  health_gate
  clear_state
  exit 0
fi

if [[ "${1:-}" == "--retry-pending" ]]; then
  trap 'rc=$?; if [[ $rc -ne 0 ]]; then write_failure_report "backup retry exited with status $rc"; if [[ "$LIVE_SERVICES_STOPPED" == "1" ]]; then start_live_services || true; fi; cleanup_failed_staging || true; clear_state; fi; exit $rc' EXIT
  retry_pending
  exit 0
fi

trap 'rc=$?; if [[ $rc -ne 0 ]]; then write_failure_report "backup exited with status $rc"; if [[ "$LIVE_SERVICES_STOPPED" == "1" ]]; then start_live_services || true; fi; cleanup_failed_staging || true; clear_state; fi; exit $rc' EXIT
main "$@"
