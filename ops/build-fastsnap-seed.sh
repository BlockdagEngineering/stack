#!/usr/bin/env bash
set -Eeuo pipefail

# Build and publish a public FastSnap seed without letting the pool mine against
# the node being stopped. The pool must expose /admin/rpc-backend-maintenance
# from a build that includes the router maintenance-drain feature.

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${BDAG_ENV_FILE:-$PROJECT_ROOT/.env}"
COMPOSE_FILE="${BDAG_COMPOSE_FILE:-$PROJECT_ROOT/docker-compose.yml}"
POOL_ADMIN_URL="${BDAG_POOL_ADMIN_URL:-http://127.0.0.1:${POOL_METRICS_PORT:-${POOL_API_PORT:-9090}}}"
NODE_IMAGE="${BDAG_FASTSNAP_NODE_IMAGE:-${BLOCKDAG_NODE_IMAGE:-}}"
SEED_DIR="${BDAG_FASTSNAP_SEED_DIR:-$PROJECT_ROOT/data-restore/fastsnap}"
LOG_FILE="${BDAG_FASTSNAP_LOG:-$PROJECT_ROOT/ops/runtime/logs/fastsnap-seed-$(date +%Y%m%d).log}"
LOCK_FILE="${BDAG_FASTSNAP_LOCK:-$PROJECT_ROOT/ops/runtime/fastsnap-seed.lock}"
MAINTENANCE_LOCK_FILE="${BDAG_FASTSNAP_MAINTENANCE_LOCK:-$PROJECT_ROOT/ops/runtime/hourly-chain-snapshot.lock}"
MAINTENANCE_TTL="${BDAG_FASTSNAP_MAINTENANCE_TTL:-45m}"
EXPORT_BACKEND="${BDAG_FASTSNAP_EXPORT_BACKEND:-}"

mkdir -p "$SEED_DIR" "$(dirname "$LOG_FILE")" "$(dirname "$LOCK_FILE")" "$(dirname "$MAINTENANCE_LOCK_FILE")"

exec 9>"$LOCK_FILE"
flock -n 9 || {
  echo "[$(date -Is)] FastSnap seed build already running" | tee -a "$LOG_FILE"
  exit 0
}

STOPPED_UNITS=()
MAINTENANCE_BACKEND=""
EXPORT_SERVICE=""

log() {
  echo "[$(date -Is)] $*" | tee -a "$LOG_FILE"
}

compose() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

resolve_node_image() {
  if [[ -n "$NODE_IMAGE" ]]; then
    printf '%s\n' "$NODE_IMAGE"
    return
  fi
  local image_id
  image_id="$(compose images -q node 2>/dev/null | head -n1 || true)"
  if [[ -n "$image_id" ]]; then
    printf '%s\n' "$image_id"
    return
  fi
  log "set BDAG_FASTSNAP_NODE_IMAGE or BLOCKDAG_NODE_IMAGE, or build the compose node image first"
  return 1
}

run_low_priority() {
  local command=("$@")
  if command -v ionice >/dev/null 2>&1; then
    command=(ionice -c3 "${command[@]}")
  fi
  if command -v nice >/dev/null 2>&1; then
    command=(nice -n 19 "${command[@]}")
  fi
  "${command[@]}"
}

systemd_user_stop_if_active() {
  local unit="$1"
  if systemctl --user is-active --quiet "$unit" 2>/dev/null; then
    systemctl --user stop "$unit" || true
    STOPPED_UNITS+=("$unit")
    log "paused $unit for FastSnap maintenance"
  fi
}

restore_stopped_units() {
  local idx unit
  for ((idx=${#STOPPED_UNITS[@]}-1; idx>=0; idx--)); do
    unit="${STOPPED_UNITS[$idx]}"
    systemctl --user start "$unit" || true
    log "restored $unit after FastSnap maintenance"
  done
}

service_for_backend() {
  case "$1" in
    node1) printf '%s\n' "${BDAG_FASTSNAP_NODE1_SERVICE:-bdag-miner-node-1}" ;;
    node2) printf '%s\n' "${BDAG_FASTSNAP_NODE2_SERVICE:-bdag-miner-node-2}" ;;
    *) return 1 ;;
  esac
}

datadir_for_backend() {
  case "$1" in
    node1) printf '%s\n' "${BDAG_FASTSNAP_NODE1_DATADIR:-$PROJECT_ROOT/data/node1}" ;;
    node2) printf '%s\n' "${BDAG_FASTSNAP_NODE2_DATADIR:-$PROJECT_ROOT/data/node2}" ;;
    *) return 1 ;;
  esac
}

selected_backend() {
  curl -fsS "$POOL_ADMIN_URL/metrics" 2>/dev/null |
    awk -F'[{},]' '
      /^pool_rpc_backend_selected/ && $0 ~ /} 1$/ {
        for (i = 1; i <= NF; i++) {
          if ($i ~ /^backend=/) {
            gsub(/backend=|"/, "", $i)
            print $i
            exit
          }
        }
      }'
}

maintenance_metric() {
  local backend="$1"
  curl -fsS "$POOL_ADMIN_URL/metrics" 2>/dev/null |
    awk -v target="$backend" -F'[{},]' '
      /^pool_rpc_backend_maintenance/ && $0 ~ /} 1$/ {
        for (i = 1; i <= NF; i++) {
          if ($i ~ /^backend=/) {
            value=$i
            gsub(/backend=|"/, "", value)
            if (value == target) {
              print 1
              exit
            }
          }
        }
      }'
}

choose_export_backend() {
  local selected="$1"
  if [[ -n "$EXPORT_BACKEND" ]]; then
    printf '%s\n' "$EXPORT_BACKEND"
    return
  fi
  case "$selected" in
    node1) printf '%s\n' "node2" ;;
    node2) printf '%s\n' "node1" ;;
    *) return 1 ;;
  esac
}

admin_maintenance() {
  local backend="$1"
  local enabled="$2"
  local reason="$3"
  curl -fsS -X POST \
    "$POOL_ADMIN_URL/admin/rpc-backend-maintenance?backend=$backend&enabled=$enabled&ttl=$MAINTENANCE_TTL&reason=$reason"
}

wait_pool_selected_backend() {
  local expected="$1"
  local deadline=$((SECONDS + 30))
  local selected=""
  while ((SECONDS < deadline)); do
    selected="$(selected_backend || true)"
    if [[ "$selected" == "$expected" ]]; then
      return 0
    fi
    sleep 1
  done
  log "pool selected backend is ${selected:-unknown}; expected $expected"
  return 1
}

wait_backend_maintenance() {
  local backend="$1"
  local deadline=$((SECONDS + 15))
  while ((SECONDS < deadline)); do
    if [[ "$(maintenance_metric "$backend" || true)" == "1" ]]; then
      return 0
    fi
    sleep 1
  done
  log "pool did not expose maintenance=1 for backend=$backend"
  return 1
}

wait_container_stopped() {
  local service="$1"
  local deadline=$((SECONDS + 60))
  while ((SECONDS < deadline)); do
    if ! docker inspect -f '{{.State.Running}}' "$service" 2>/dev/null | grep -qx true; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_db_lock_free() {
  local lock_path="$1"
  local deadline=$((SECONDS + 45))
  while ((SECONDS < deadline)); do
    if command -v fuser >/dev/null 2>&1 && fuser "$lock_path" >/dev/null 2>&1; then
      sleep 1
      continue
    fi
    return 0
  done
  return 1
}

install_snapshot_links() {
  local final_archive="$1"
  local final_manifest="$2"
  local node_dir
  for node_dir in \
    "${BDAG_FASTSNAP_NODE1_DATADIR:-$PROJECT_ROOT/data/node1}/mainnet" \
    "${BDAG_FASTSNAP_NODE2_DATADIR:-$PROJECT_ROOT/data/node2}/mainnet"; do
    if [[ -d "$node_dir" ]]; then
      ln -f "$final_archive" "$node_dir/snapshot.bdsnap"
      ln -f "$final_manifest" "$node_dir/snapshot.bdsnap.manifest.json"
      log "installed hardlinked FastSnap seed into $node_dir"
    fi
  done
}

cleanup() {
  local rc=$?
  if [[ -n "$EXPORT_SERVICE" ]]; then
    compose start "$EXPORT_SERVICE" >/dev/null 2>&1 || true
  fi
  if [[ -n "$MAINTENANCE_BACKEND" ]]; then
    admin_maintenance "$MAINTENANCE_BACKEND" false fastsnap-clear >/dev/null 2>&1 || true
  fi
  restore_stopped_units
  exit "$rc"
}
trap cleanup EXIT INT TERM

SNAP_FINAL="$SEED_DIR/snapshot.bdsnap"
MANIFEST_FINAL="$SNAP_FINAL.manifest.json"
SNAP_TMP="$SEED_DIR/snapshot.bdsnap.tmp"
MANIFEST_TMP="$SNAP_TMP.manifest.json"

exec 8>"$MAINTENANCE_LOCK_FILE"
flock 8

systemd_user_stop_if_active bdag-stack-sentinel.timer
systemd_user_stop_if_active bdag-stack-sentinel.service
systemd_user_stop_if_active bdag-watchdog-guard.timer
systemd_user_stop_if_active bdag-watchdog.service
systemd_user_stop_if_active bdag-miner-15min-supervisor.timer
systemd_user_stop_if_active bdag-sync-coordinator.timer
systemd_user_stop_if_active bdag-codex-intelligent-supervisor.timer
systemd_user_stop_if_active bdag-hourly-snapshot.timer
systemd_user_stop_if_active bdag-hourly-snapshot.service

rm -f "$SNAP_TMP" "$MANIFEST_TMP"

ACTIVE_BACKEND="$(selected_backend || true)"
if [[ -z "$ACTIVE_BACKEND" ]]; then
  log "pool router has no selected backend; refusing to stop a node for FastSnap export"
  exit 1
fi
EXPORT_BACKEND="$(choose_export_backend "$ACTIVE_BACKEND")"
EXPORT_SERVICE="$(service_for_backend "$EXPORT_BACKEND")"
EXPORT_NODE_DIR="$(datadir_for_backend "$EXPORT_BACKEND")"

if [[ "$EXPORT_BACKEND" == "$ACTIVE_BACKEND" ]]; then
  log "export backend equals active backend ($ACTIVE_BACKEND); refusing unsafe FastSnap export"
  exit 1
fi
if [[ ! -d "$EXPORT_NODE_DIR/mainnet/BdagChain" ]]; then
  log "missing export datadir: $EXPORT_NODE_DIR/mainnet/BdagChain"
  exit 1
fi

log "requesting pool maintenance drain backend=$EXPORT_BACKEND active=$ACTIVE_BACKEND"
admin_maintenance "$EXPORT_BACKEND" true fastsnap | tee -a "$LOG_FILE" >/dev/null
MAINTENANCE_BACKEND="$EXPORT_BACKEND"
wait_pool_selected_backend "$ACTIVE_BACKEND"
wait_backend_maintenance "$EXPORT_BACKEND"

log "stopping drained backend service=$EXPORT_SERVICE"
compose stop "$EXPORT_SERVICE" 2>&1 | tee -a "$LOG_FILE"
wait_container_stopped "$EXPORT_SERVICE"
wait_pool_selected_backend "$ACTIVE_BACKEND"
wait_db_lock_free "$EXPORT_NODE_DIR/mainnet/BdagChain/LOCK"

log "exporting FastSnap archive from $EXPORT_BACKEND datadir=$EXPORT_NODE_DIR"
NODE_IMAGE="$(resolve_node_image)"
run_low_priority docker run --rm \
  --entrypoint /usr/local/bin/bdag \
  -v "$EXPORT_NODE_DIR":/snapshot-source \
  -v "$SEED_DIR":/out \
  "$NODE_IMAGE" \
  snap export --datadir /snapshot-source/mainnet --path /out/snapshot.bdsnap.tmp 2>&1 | tee -a "$LOG_FILE"

if [[ ! -s "$SNAP_TMP" || ! -s "$MANIFEST_TMP" ]]; then
  log "FastSnap export did not create expected archive and manifest"
  exit 1
fi

log "verifying exported FastSnap archive"
run_low_priority docker run --rm \
  --entrypoint /usr/local/bin/bdag \
  -v "$SEED_DIR":/out:ro \
  "$NODE_IMAGE" \
  snap verify --path /out/snapshot.bdsnap.tmp 2>&1 | tee -a "$LOG_FILE"

mv -f "$SNAP_TMP" "$SNAP_FINAL"
mv -f "$MANIFEST_TMP" "$MANIFEST_FINAL"
install_snapshot_links "$SNAP_FINAL" "$MANIFEST_FINAL"

log "FastSnap seed ready: $SNAP_FINAL"
