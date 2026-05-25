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
VERIFY_EXISTING="${BDAG_FASTSNAP_VERIFY_EXISTING:-0}"
VERIFY_AFTER_EXPORT="${BDAG_FASTSNAP_VERIFY_AFTER_EXPORT:-1}"
RESTORE_TIMEOUT_SECONDS="${BDAG_FASTSNAP_RESTORE_TIMEOUT_SECONDS:-180}"
REQUIRE_BOTH_BACKENDS_FOR_VERIFY="${BDAG_FASTSNAP_REQUIRE_BOTH_BACKENDS_FOR_VERIFY:-1}"
DOCKER_CPU_SHARES="${BDAG_FASTSNAP_DOCKER_CPU_SHARES:-128}"
DOCKER_BLKIO_WEIGHT="${BDAG_FASTSNAP_DOCKER_BLKIO_WEIGHT:-10}"
DOCKER_CPUS="${BDAG_FASTSNAP_DOCKER_CPUS:-}"
MAX_EXPORT_BACKEND_LAG="${BDAG_FASTSNAP_MAX_EXPORT_BACKEND_LAG:-1000}"
REQUIRE_EXPORT_BACKEND_FRESH="${BDAG_FASTSNAP_REQUIRE_EXPORT_BACKEND_FRESH:-1}"
METRICS_TIMEOUT="${BDAG_FASTSNAP_METRICS_TIMEOUT:-3}"
NODE_METRICS_URLS="${BDAG_FASTSNAP_NODE_METRICS_URLS:-node1=http://127.0.0.1:6061/debug/metrics/prometheus,node2=http://127.0.0.1:6062/debug/metrics/prometheus}"

mkdir -p "$SEED_DIR" "$(dirname "$LOG_FILE")" "$(dirname "$LOCK_FILE")" "$(dirname "$MAINTENANCE_LOCK_FILE")"

exec 9>"$LOCK_FILE"
flock -n 9 || {
  echo "[$(date -Is)] FastSnap seed build already running" | tee -a "$LOG_FILE"
  exit 0
}

STOPPED_UNITS=()
MAINTENANCE_BACKEND=""
EXPORT_SERVICE=""
CLEANUP_DONE=0

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

docker_run_low_priority() {
  local command=(docker run --rm --cpu-shares "$DOCKER_CPU_SHARES" --blkio-weight "$DOCKER_BLKIO_WEIGHT")
  if [[ -n "$DOCKER_CPUS" ]]; then
    command+=(--cpus "$DOCKER_CPUS")
  fi
  command+=("$@")
  run_low_priority "${command[@]}"
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
  STOPPED_UNITS=()
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

backend_healthy_metric() {
  local backend="$1"
  curl -fsS "$POOL_ADMIN_URL/metrics" 2>/dev/null |
    awk -v target="$backend" -F'[{},]' '
      /^pool_rpc_backend_healthy/ {
        for (i = 1; i <= NF; i++) {
          if ($i ~ /^backend=/) {
            value=$i
            gsub(/backend=|"/, "", value)
            if (value == target) {
              print $NF + 0
              exit
            }
          }
        }
      }'
}

pool_metric_value() {
  local metric="$1"
  curl -fsS "$POOL_ADMIN_URL/metrics" 2>/dev/null |
    awk -v metric="$metric" '$1 == metric || index($1, metric "{") == 1 { print $NF + 0; exit }'
}

backend_metrics_url() {
  local backend="$1"
  printf '%s\n' "$NODE_METRICS_URLS" |
    tr ',' '\n' |
    awk -F= -v target="$backend" '$1 == target { sub(/^[^=]*=/, ""); print; exit }'
}

backend_order_metric() {
  local backend="$1"
  local url
  url="$(backend_metrics_url "$backend")"
  [[ -n "$url" ]] || return 1
  curl -fsS --max-time "$METRICS_TIMEOUT" "$url" 2>/dev/null |
    awk '
      $1 == "Blockdag_mainorder" { print int($2); found=1; exit }
      $1 == "chain_head_block" { fallback=int($2) }
      END { if (!found && fallback != "") print fallback }'
}

assert_export_backend_fresh() {
  local active_backend="$1"
  local export_backend="$2"
  if [[ "$REQUIRE_EXPORT_BACKEND_FRESH" != "1" ]]; then
    log "skipping export backend freshness gate because BDAG_FASTSNAP_REQUIRE_EXPORT_BACKEND_FRESH=$REQUIRE_EXPORT_BACKEND_FRESH"
    return 0
  fi

  local active_order export_order lag
  active_order="$(backend_order_metric "$active_backend" || true)"
  export_order="$(backend_order_metric "$export_backend" || true)"
  if [[ -z "$active_order" || -z "$export_order" ]]; then
    log "refusing FastSnap export: could not read node order metrics active=$active_backend($active_order) export=$export_backend($export_order)"
    return 1
  fi

  lag=$((active_order - export_order))
  if ((lag < 0)); then
    lag=0
  fi
  log "FastSnap export freshness active=$active_backend order=$active_order export=$export_backend order=$export_order lag=$lag max=$MAX_EXPORT_BACKEND_LAG"
  if ((lag > MAX_EXPORT_BACKEND_LAG)); then
    log "refusing FastSnap export: backup node is too far behind for a public seed lag=$lag max=$MAX_EXPORT_BACKEND_LAG"
    return 1
  fi
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

wait_container_running() {
  local service="$1"
  local deadline=$((SECONDS + 60))
  while ((SECONDS < deadline)); do
    if docker inspect -f '{{.State.Running}}' "$service" 2>/dev/null | grep -qx true; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_pool_jobs_ready() {
  local timeout="${1:-$RESTORE_TIMEOUT_SECONDS}"
  local deadline=$((SECONDS + timeout))
  local ok="" ready="" authorized=""
  while ((SECONDS < deadline)); do
    ok="$(pool_metric_value pool_job_health_ok || true)"
    ready="$(pool_metric_value pool_job_health_ready_miners || true)"
    authorized="$(pool_metric_value pool_job_health_authorized_miners || true)"
    if [[ "${ok:-0}" == "1" && "${authorized:-0}" -gt 0 && "${ready:-0}" -eq "${authorized:-0}" ]]; then
      return 0
    fi
    sleep 2
  done
  log "pool jobs did not become ready after FastSnap restore ok=${ok:-unknown} ready=${ready:-unknown} authorized=${authorized:-unknown}"
  return 1
}

wait_both_backends_healthy() {
  local timeout="${1:-$RESTORE_TIMEOUT_SECONDS}"
  local deadline=$((SECONDS + timeout))
  local node1="" node2=""
  while ((SECONDS < deadline)); do
    node1="$(backend_healthy_metric node1 || true)"
    node2="$(backend_healthy_metric node2 || true)"
    if [[ "${node1:-0}" == "1" && "${node2:-0}" == "1" ]]; then
      return 0
    fi
    sleep 2
  done
  log "both backends did not become healthy after FastSnap restore node1=${node1:-unknown} node2=${node2:-unknown}"
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

host_own_snapshot_files() {
  local files=()
  local path
  for path in "$@"; do
    if [[ -e "$path" ]]; then
      files+=("$path")
    fi
  done
  if [[ "${#files[@]}" -eq 0 ]]; then
    return 0
  fi
  if chown "$(id -u):$(id -g)" "${files[@]}" 2>/dev/null; then
    return 0
  fi
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    sudo chown "$(id -u):$(id -g)" "${files[@]}" || true
  fi
}

restore_export_backend_before_verify() {
  if [[ -n "$EXPORT_SERVICE" ]]; then
    log "starting exported backend service=$EXPORT_SERVICE before verification"
    compose start "$EXPORT_SERVICE" 2>&1 | tee -a "$LOG_FILE"
    wait_container_running "$EXPORT_SERVICE"
  fi
  if [[ -n "$MAINTENANCE_BACKEND" ]]; then
    log "clearing pool maintenance backend=$MAINTENANCE_BACKEND before verification"
    admin_maintenance "$MAINTENANCE_BACKEND" false fastsnap-restore | tee -a "$LOG_FILE" >/dev/null
    MAINTENANCE_BACKEND=""
  fi
  wait_pool_jobs_ready "$RESTORE_TIMEOUT_SECONDS"
  if [[ "$REQUIRE_BOTH_BACKENDS_FOR_VERIFY" == "1" ]]; then
    wait_both_backends_healthy "$RESTORE_TIMEOUT_SECONDS"
  fi
}

install_snapshot_links() {
  local final_archive="$1"
  local final_manifest="$2"
  local node_dir
  local target_archive target_manifest
  for node_dir in \
    "${BDAG_FASTSNAP_NODE1_DATADIR:-$PROJECT_ROOT/data/node1}/mainnet" \
    "${BDAG_FASTSNAP_NODE2_DATADIR:-$PROJECT_ROOT/data/node2}/mainnet"; do
    if [[ -d "$node_dir" ]]; then
      target_archive="$node_dir/snapshot.bdsnap"
      target_manifest="$node_dir/snapshot.bdsnap.manifest.json"
      if ! ln -f "$final_archive" "$target_archive" 2>/dev/null; then
        if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
          sudo ln -f "$final_archive" "$target_archive"
        else
          log "failed to install FastSnap archive link into $node_dir; sudo is unavailable"
          return 1
        fi
      fi
      if ! ln -f "$final_manifest" "$target_manifest" 2>/dev/null; then
        if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
          sudo ln -f "$final_manifest" "$target_manifest"
        else
          log "failed to install FastSnap manifest link into $node_dir; sudo is unavailable"
          return 1
        fi
      fi
      log "installed hardlinked FastSnap seed into $node_dir"
    fi
  done
}

verify_existing_snapshot() {
  if [[ ! -s "$SNAP_TMP" || ! -s "$MANIFEST_TMP" ]]; then
    log "missing existing temporary FastSnap archive or manifest in $SEED_DIR"
    return 1
  fi

  wait_pool_jobs_ready "$RESTORE_TIMEOUT_SECONDS"
  if [[ "$REQUIRE_BOTH_BACKENDS_FOR_VERIFY" == "1" ]]; then
    wait_both_backends_healthy "$RESTORE_TIMEOUT_SECONDS"
  fi

  log "verifying existing exported FastSnap archive"
  NODE_IMAGE="$(resolve_node_image)"
  docker_run_low_priority \
    --entrypoint /usr/local/bin/bdag \
    -v "$SEED_DIR":/out:ro \
    "$NODE_IMAGE" \
    snap verify --path /out/snapshot.bdsnap.tmp 2>&1 | tee -a "$LOG_FILE"

  host_own_snapshot_files "$SNAP_TMP" "$MANIFEST_TMP"
  mv -f "$SNAP_TMP" "$SNAP_FINAL"
  mv -f "$MANIFEST_TMP" "$MANIFEST_FINAL"
  host_own_snapshot_files "$SNAP_FINAL" "$MANIFEST_FINAL"
  install_snapshot_links "$SNAP_FINAL" "$MANIFEST_FINAL"
  log "FastSnap seed ready: $SNAP_FINAL"
}

cleanup() {
  local rc=$?
  if [[ "$CLEANUP_DONE" == "1" ]]; then
    exit "$rc"
  fi
  CLEANUP_DONE=1
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

if [[ "$VERIFY_EXISTING" == "1" ]]; then
  verify_existing_snapshot
  exit 0
fi

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
wait_pool_jobs_ready "$RESTORE_TIMEOUT_SECONDS"
assert_export_backend_fresh "$ACTIVE_BACKEND" "$EXPORT_BACKEND"

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
docker_run_low_priority \
  --entrypoint /usr/local/bin/bdag \
  -v "$EXPORT_NODE_DIR":/snapshot-source \
  -v "$SEED_DIR":/out \
  "$NODE_IMAGE" \
  snap export --datadir /snapshot-source/mainnet --path /out/snapshot.bdsnap.tmp 2>&1 | tee -a "$LOG_FILE"

if [[ ! -s "$SNAP_TMP" || ! -s "$MANIFEST_TMP" ]]; then
  log "FastSnap export did not create expected archive and manifest"
  exit 1
fi
host_own_snapshot_files "$SNAP_TMP" "$MANIFEST_TMP"

# Verification is a heavy sequential read. Restore pool redundancy before it so
# a slow verify cannot leave mining dependent on one already-stressed backend.
restore_export_backend_before_verify

if [[ "$VERIFY_AFTER_EXPORT" != "1" ]]; then
  log "FastSnap archive exported but not verified/promoted; leaving temporary files in $SEED_DIR"
  log "set BDAG_FASTSNAP_VERIFY_AFTER_EXPORT=1 and rerun verification before serving this seed publicly"
  exit 0
fi

log "verifying exported FastSnap archive"
docker_run_low_priority \
  --entrypoint /usr/local/bin/bdag \
  -v "$SEED_DIR":/out:ro \
  "$NODE_IMAGE" \
  snap verify --path /out/snapshot.bdsnap.tmp 2>&1 | tee -a "$LOG_FILE"

mv -f "$SNAP_TMP" "$SNAP_FINAL"
mv -f "$MANIFEST_TMP" "$MANIFEST_FINAL"
host_own_snapshot_files "$SNAP_FINAL" "$MANIFEST_FINAL"
install_snapshot_links "$SNAP_FINAL" "$MANIFEST_FINAL"

log "FastSnap seed ready: $SNAP_FINAL"
