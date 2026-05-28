#!/usr/bin/env bash
set -eu

# Passive mining-host tuning. This is intentionally safe to reapply: it adjusts
# block-device queue/read-ahead, Docker weights, and process scheduler hints
# without changing chain data, node topology, ASIC configuration, or service
# state.
#
# Policy: paid block production wins local contention. The selected active
# mining-template node, pool, PostgreSQL, and RPC router get high work-conserving
# CPU/IO weights. The standby node remains protected, but lower than the active
# lane so active/passive routing does not lose the 11-13% efficiency previously
# observed from competing template lanes. Dashboard, observability, release
# seeding, browser, and maintenance work must yield under load.

ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

env_file_value() {
  key="$1"
  case "$key" in
    ""|*[!A-Za-z0-9_]*)
      return 0
      ;;
  esac
  [ -f "$ROOT/.env" ] || return 0
  sed -n -E "s/^[[:space:]]*$key[[:space:]]*=[[:space:]]*//p" "$ROOT/.env" |
    tail -n1 |
    sed -E "s/^['\"]//; s/['\"]$//"
}

setting() {
  key="$1"
  fallback="$2"
  value="$(printenv "$key" 2>/dev/null || true)"
  [ -n "$value" ] || value="$(env_file_value "$key")"
  printf '%s\n' "${value:-$fallback}"
}

read_ahead_kb="$(setting BDAG_BLOCK_READ_AHEAD_KB 1024)"
nr_requests="$(setting BDAG_BLOCK_NR_REQUESTS 256)"
vm_tuning_enabled="$(setting BDAG_VM_TUNING_ENABLED 1)"
vm_swappiness="$(setting BDAG_VM_SWAPPINESS 10)"
vm_vfs_cache_pressure="$(setting BDAG_VM_VFS_CACHE_PRESSURE 50)"
vm_dirty_background_bytes="$(setting BDAG_VM_DIRTY_BACKGROUND_BYTES 67108864)"
vm_dirty_bytes="$(setting BDAG_VM_DIRTY_BYTES 268435456)"
chain_data_paths="$(setting BDAG_CHAIN_DATA_PATHS "$ROOT/data/node1,$ROOT/data/node2,$ROOT/data/postgres")"
active_node_nice="$(setting BDAG_MINING_ACTIVE_NODE_NICE -8)"
pool_nice="$(setting BDAG_MINING_POOL_NICE -7)"
standby_node_nice="$(setting BDAG_MINING_STANDBY_NODE_NICE -2)"
rpc_nice="$(setting BDAG_MINING_RPC_NICE -4)"
observability_nice="$(setting BDAG_OBSERVABILITY_NICE 15)"
desktop_nice="$(setting BDAG_DESKTOP_BACKGROUND_NICE 19)"
pool_metrics_url="$(setting BDAG_POOL_METRICS_URL http://127.0.0.1:9092/metrics)"
sync_state_file="$(setting BDAG_SYNC_COORDINATOR_STATE_FILE "$ROOT/ops/runtime/sync-coordinator-state.json")"

log() {
  printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"
}

block_device_for_path() {
  source="$(findmnt -no SOURCE -T "$1" 2>/dev/null || true)"
  [ -n "$source" ] || return 0
  name="$(lsblk -no PKNAME "$source" 2>/dev/null | head -n1 || true)"
  if [ -z "$name" ]; then
    name="$(basename "$source" | sed -E 's/p?[0-9]+$//')"
  fi
  [ -n "$name" ] && printf '%s\n' "$name"
}

docker_root_dir() {
  command -v docker >/dev/null 2>&1 || return 0
  docker info --format '{{.DockerRootDir}}' 2>/dev/null || true
}

resolve_runtime_path() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s\n' "$ROOT/$1" ;;
  esac
}

tune_block_device() {
  queue="/sys/block/$1/queue"
  [ -d "$queue" ] || return 0
  [ -w "$queue/read_ahead_kb" ] && printf '%s\n' "$read_ahead_kb" > "$queue/read_ahead_kb" || true
  [ -w "$queue/nr_requests" ] && printf '%s\n' "$nr_requests" > "$queue/nr_requests" || true
  log "block_device=$1 read_ahead_kb=$(cat "$queue/read_ahead_kb" 2>/dev/null || echo unknown) nr_requests=$(cat "$queue/nr_requests" 2>/dev/null || echo unknown)"
}

numeric_setting() {
  case "$1" in
    ''|*[!0-9]*)
      return 1
      ;;
    *)
      return 0
      ;;
  esac
}

apply_sysctl_value() {
  key="$1"
  value="$2"
  numeric_setting "$value" || return 0
  command -v sysctl >/dev/null 2>&1 || return 0
  sysctl -w "$key=$value" >/dev/null 2>&1 || true
}

apply_vm_tuning() {
  [ "$vm_tuning_enabled" = "1" ] || return 0
  apply_sysctl_value vm.swappiness "$vm_swappiness"
  apply_sysctl_value vm.vfs_cache_pressure "$vm_vfs_cache_pressure"
  if numeric_setting "$vm_dirty_background_bytes" && numeric_setting "$vm_dirty_bytes" &&
    [ "$vm_dirty_background_bytes" -gt 0 ] && [ "$vm_dirty_bytes" -gt "$vm_dirty_background_bytes" ]; then
    apply_sysctl_value vm.dirty_background_bytes "$vm_dirty_background_bytes"
    apply_sysctl_value vm.dirty_bytes "$vm_dirty_bytes"
  fi
  log "vm_tuning swappiness=$(cat /proc/sys/vm/swappiness 2>/dev/null || echo unknown) vfs_cache_pressure=$(cat /proc/sys/vm/vfs_cache_pressure 2>/dev/null || echo unknown) dirty_background_bytes=$(cat /proc/sys/vm/dirty_background_bytes 2>/dev/null || echo unknown) dirty_bytes=$(cat /proc/sys/vm/dirty_bytes 2>/dev/null || echo unknown)"
}

renice_pids() {
  value="$1"
  shift
  for pid in "$@"; do
    [ -n "$pid" ] && renice -n "$value" -p "$pid" >/dev/null 2>&1 || true
  done
}

ionice_pids() {
  class="$1"
  priority="$2"
  shift 2
  command -v ionice >/dev/null 2>&1 || return 0
  for pid in "$@"; do
    [ -n "$pid" ] && ionice -c "$class" -n "$priority" -p "$pid" >/dev/null 2>&1 || true
  done
}

oom_score_pids() {
  score="$1"
  shift
  for pid in "$@"; do
    [ -n "$pid" ] || continue
    proc_file="/proc/$pid/oom_score_adj"
    [ -w "$proc_file" ] && printf '%s\n' "$score" > "$proc_file" || true
  done
}

tune_pids() {
  nice_value="$1"
  io_class="$2"
  io_priority="$3"
  oom_score="$4"
  shift 4
  [ "$#" -gt 0 ] || return 0
  renice_pids "$nice_value" "$@"
  ionice_pids "$io_class" "$io_priority" "$@"
  oom_score_pids "$oom_score" "$@"
}

docker_container_exists() {
  docker inspect "$1" >/dev/null 2>&1
}

docker_container_pids() {
  docker_container_exists "$1" || return 0
  docker top "$1" -eo pid 2>/dev/null | awk 'NR > 1 && $1 ~ /^[0-9]+$/ { print $1 }' || true
}

docker_update_one() {
  container="$1"
  cpu_shares="$2"
  blkio_weight="$3"
  docker_container_exists "$container" || return 0
  # Docker Compose owns OOMScoreAdj at container create time. docker update in
  # common distro builds does not support --oom-score-adj, so runtime tuning
  # only reapplies work-conserving CPU and block I/O weights.
  docker update \
    --cpu-shares "$cpu_shares" \
    --blkio-weight "$blkio_weight" \
    "$container" >/dev/null 2>&1 || true
}

selected_backend_from_metrics() {
  command -v curl >/dev/null 2>&1 || return 0
  curl -fsS --max-time 2 "$pool_metrics_url" 2>/dev/null |
    awk '
      $0 ~ /^pool_rpc_backend_selected/ && $0 ~ /} 1$/ {
        if (match($0, /backend="[^"]+"/)) {
          backend=substr($0, RSTART + 9, RLENGTH - 10)
          print backend
          exit
        }
      }'
}

selected_backend_from_env() {
  for env_file in "$ROOT/asic-pool/.env" "$ROOT/.env"; do
    [ -f "$env_file" ] || continue
    sed -n 's/^POOL_RPC_BACKENDS=//p' "$env_file" |
      awk -F'[=,]' 'NF { print $1; exit }'
  done
}

selected_backend() {
  backend="$(selected_backend_from_metrics || true)"
  if [ -z "$backend" ]; then
    backend="$(selected_backend_from_env || true)"
  fi
  case "$backend" in
    node1|bdag-miner-node-1) printf '%s\n' "node1" ;;
    node2|bdag-miner-node-2) printf '%s\n' "node2" ;;
    node) printf '%s\n' "node" ;;
    *) printf '%s\n' "node1" ;;
  esac
}

node_container_for_backend() {
  case "$1" in
    node1) printf '%s\n' "bdag-miner-node-1" ;;
    node2) printf '%s\n' "bdag-miner-node-2" ;;
    node) printf '%s\n' "node" ;;
  esac
}

sync_coordinator_leader_node() {
  [ -f "$sync_state_file" ] || return 0
  python3 - "$sync_state_file" <<'PY' 2>/dev/null || true
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        state = json.load(handle)
except Exception:
    raise SystemExit(0)

if state.get("mode") != "leader_catchup" or not state.get("paused_follower"):
    raise SystemExit(0)

leader = str(state.get("leader") or "")
mapping = {
    "node1": "bdag-miner-node-1",
    "node2": "bdag-miner-node-2",
    "bdag-miner-node-1": "bdag-miner-node-1",
    "bdag-miner-node-2": "bdag-miner-node-2",
    "node": "node",
}
if leader in mapping:
    print(mapping[leader])
PY
}

tune_processes() {
  active_backend="$(selected_backend)"
  active_node="$(node_container_for_backend "$active_backend")"
  catchup_node="$(sync_coordinator_leader_node || true)"
  if [ -n "$catchup_node" ]; then
    active_node="$catchup_node"
  fi

  for container in "$active_node"; do
    pids="$(docker_container_pids "$container")"
    [ -n "$pids" ] && tune_pids "$active_node_nice" 2 0 -950 $pids
  done

  for container in asic-pool pool pool-db postgres; do
    pids="$(docker_container_pids "$container")"
    [ -n "$pids" ] && tune_pids "$pool_nice" 2 0 -900 $pids
  done

  for container in rpc-failover; do
    pids="$(docker_container_pids "$container")"
    [ -n "$pids" ] && tune_pids "$rpc_nice" 2 1 -750 $pids
  done

  for container in bdag-miner-node-1 bdag-miner-node-2; do
    [ "$container" = "$active_node" ] && continue
    pids="$(docker_container_pids "$container")"
    [ -n "$pids" ] && tune_pids "$standby_node_nice" 2 2 -850 $pids
  done

  for container in \
    dashboard bdag-dashboard bdag-prometheus bdag-grafana bdag-loki \
    bdag-alertmanager bdag-cadvisor bdag-alloy bdag-blackbox-exporter \
    bdag-exporter bdag-node-exporter bdag-postgres-exporter; do
    pids="$(docker_container_pids "$container")"
    [ -n "$pids" ] && tune_pids "$observability_nice" 3 7 300 $pids
  done

  if [ "${BDAG_TUNE_DESKTOP_BACKGROUND:-1}" = "1" ]; then
    desktop_pids="$(pgrep -f '(/firefox|/chrome|/chromium|/code|Web Content|Socket Process|Utility Process|grafana|prometheus|loki|alloy|cadvisor|bdag_exporter.py)' 2>/dev/null || true)"
    if [ -n "$desktop_pids" ]; then
      # shellcheck disable=SC2086
      renice_pids "$desktop_nice" $desktop_pids
      # shellcheck disable=SC2086
      ionice_pids 3 7 $desktop_pids
    fi
  fi
}

tune_docker_weights() {
  command -v docker >/dev/null 2>&1 || return 0
  docker info >/dev/null 2>&1 || return 0
  active_backend="$(selected_backend)"
  active_node="$(node_container_for_backend "$active_backend")"
  catchup_node="$(sync_coordinator_leader_node || true)"
  if [ -n "$catchup_node" ]; then
    active_node="$catchup_node"
  fi

  if [ -n "$catchup_node" ]; then
    docker_update_one "$active_node" 8192 1000
  else
    docker_update_one "$active_node" 6144 1000
  fi
  for container in bdag-miner-node-1 bdag-miner-node-2; do
    [ "$container" = "$active_node" ] && continue
    docker_update_one "$container" 3072 800
  done
  docker_update_one node 6144 1000

  docker_update_one asic-pool 5120 950
  docker_update_one pool 5120 950
  docker_update_one pool-db 4096 950
  docker_update_one postgres 4096 950
  docker_update_one rpc-failover 3072 850

  for container in \
    dashboard bdag-dashboard bdag-prometheus bdag-grafana bdag-loki \
    bdag-alertmanager bdag-cadvisor bdag-alloy bdag-blackbox-exporter \
    bdag-exporter bdag-node-exporter bdag-postgres-exporter; do
    docker_update_one "$container" 128 100
  done

  if [ -n "$catchup_node" ]; then
    log "resource_policy=leader-catchup active_backend=$active_backend active_node=$active_node"
  else
    log "resource_policy=active-passive active_backend=$active_backend active_node=$active_node"
  fi
}

paths_for_block_tuning() {
  printf '%s\n' "$ROOT"
  printf '%s\n' "/"
  printf '%s\n' "$chain_data_paths" | tr ',;' '\n' | while IFS= read -r path; do
    [ -n "$path" ] && resolve_runtime_path "$path"
  done
  docker_root="$(docker_root_dir)"
  [ -n "$docker_root" ] && printf '%s\n' "$docker_root"
}

devices="$(
  paths_for_block_tuning | while IFS= read -r path; do
    [ -n "$path" ] && block_device_for_path "$path"
  done | awk 'NF' | sort -u
)"
apply_vm_tuning
for dev in $devices; do
  tune_block_device "$dev"
done
tune_docker_weights
tune_processes
