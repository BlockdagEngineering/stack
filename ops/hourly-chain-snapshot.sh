#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SNAPSHOT_DIR="${BDAG_SNAPSHOT_DIR:-$PROJECT_ROOT/data-restore/hourly}"
SNAPSHOT_STAGE_ROOT="${BDAG_SNAPSHOT_STAGE_ROOT:-$PROJECT_ROOT/data-restore/.hourly-stage}"
SNAPSHOT_RETAIN="${BDAG_SNAPSHOT_RETAIN:-12}"
LOCK_FILE="${BDAG_SNAPSHOT_LOCK:-$PROJECT_ROOT/ops/runtime/hourly-chain-snapshot.lock}"
STAGE_LOCK_FILE="${BDAG_SNAPSHOT_STAGE_LOCK:-$PROJECT_ROOT/ops/runtime/chain-snapshot-stage.lock}"
LOG_FILE="${BDAG_SNAPSHOT_LOG:-$PROJECT_ROOT/ops/runtime/logs/hourly-chain-snapshot.log}"
STATE_FILE="${BDAG_SNAPSHOT_STATE:-$PROJECT_ROOT/ops/runtime/hourly-chain-snapshot-state}"
SNAPSHOT_STOP_STATE_FILE="${BDAG_SNAPSHOT_STOP_STATE_FILE:-$PROJECT_ROOT/ops/runtime/snapshot-node-stop-state.json}"
ENV_FILE="${BDAG_ENV_FILE:-$PROJECT_ROOT/asic-pool/.env}"
COMPOSE_FILE="${BDAG_COMPOSE_FILE:-$PROJECT_ROOT/docker-compose.yml}"
SNAPSHOT_BACKOFF_BLOCKS="${BDAG_SNAPSHOT_BACKOFF_BLOCKS:-0}"
SNAPSHOT_MAX_BLOCK_LAG="${BDAG_SNAPSHOT_MAX_BLOCK_LAG:-5}"
SNAPSHOT_UNKNOWN_BACKOFF="${BDAG_SNAPSHOT_UNKNOWN_BACKOFF:-1}"
SNAPSHOT_COMPRESS="${BDAG_SNAPSHOT_COMPRESS:-0}"
SNAPSHOT_AVOID_RPC_PRIMARY="${BDAG_SNAPSHOT_AVOID_RPC_PRIMARY:-1}"
SNAPSHOT_RPC_RECOVERY_SECONDS="${BDAG_SNAPSHOT_RPC_RECOVERY_SECONDS:-180}"
SNAPSHOT_FINAL_STOP_SYNC="${BDAG_SNAPSHOT_FINAL_STOP_SYNC:-0}"
SNAPSHOT_FINAL_STOP_TIMEOUT_SECONDS="${BDAG_SNAPSHOT_FINAL_STOP_TIMEOUT_SECONDS:-45}"
SNAPSHOT_WARM_SYNC_TIMEOUT_SECONDS="${BDAG_SNAPSHOT_WARM_SYNC_TIMEOUT_SECONDS:-2700}"
SNAPSHOT_REQUIRE_SAFE_SOURCE="${BDAG_SNAPSHOT_REQUIRE_SAFE_SOURCE:-1}"
SNAPSHOT_ALWAYS_PUBLISH="${BDAG_SNAPSHOT_ALWAYS_PUBLISH:-0}"

source "$PROJECT_ROOT/ops/chain-snapshot-common.sh"

mkdir -p "$(dirname "$LOCK_FILE")" "$(dirname "$STAGE_LOCK_FILE")" "$(dirname "$LOG_FILE")" "$SNAPSHOT_DIR" "$SNAPSHOT_STAGE_ROOT"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[$(date -Is)] hourly snapshot already running" >> "$LOG_FILE"
  exit 0
fi

log() {
  echo "[$(date -Is)] $*" | tee -a "$LOG_FILE"
}

write_snapshot_manifest() {
  local published_path="$1"
  local manifest_path="$2"
  local latest_manifest_link="$3"
  local final_stop_sync="$4"
  local source_node_service="$5"
  local source_node_key="$6"
  local source_node_dir="$7"

  if PYTHONPATH="$PROJECT_ROOT/ops" python3 - "$PROJECT_ROOT" "$published_path" "$source_node_service" "$source_node_key" "$source_node_dir" "$final_stop_sync" > "$manifest_path.tmp" <<'PY'
import json
import sys
import time
from pathlib import Path

project_root = Path(sys.argv[1])
published_path = Path(sys.argv[2])
source_node_service = sys.argv[3]
source_node_key = sys.argv[4]
source_node_dir = sys.argv[5]
final_stop_sync = sys.argv[6] == "1"

try:
    from pool_ops import collect_status, now_iso
    status = collect_status(include_logs=False)
    generated_at = now_iso()
except Exception as exc:  # noqa: BLE001 - snapshot publication must not fail because metadata collection failed.
    status = {"metadata_error": str(exc)}
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")

nodes = status.get("nodes") if isinstance(status, dict) else {}
node_info = nodes.get(source_node_service, {}) if isinstance(nodes, dict) else {}
sync_progress = status.get("sync_progress") if isinstance(status, dict) else {}
node_heights = {
    name: info.get("best_main_order") or info.get("latest_block")
    for name, info in nodes.items()
    if isinstance(info, dict)
} if isinstance(nodes, dict) else {}
numeric_heights = []
for height in node_heights.values():
    try:
        numeric_heights.append(int(height))
    except (TypeError, ValueError):
        pass
source_latest_block = (node_info.get("best_main_order") or node_info.get("latest_block")) if isinstance(node_info, dict) else None
try:
    source_latest_numeric = int(source_latest_block)
except (TypeError, ValueError):
    source_latest_numeric = None
source_block_lag_to_best = (
    max(numeric_heights) - source_latest_numeric
    if numeric_heights and source_latest_numeric is not None
    else None
)
restore_safety_reasons = []
if status.get("overall") == "down" if isinstance(status, dict) else True:
    restore_safety_reasons.append("stack overall is down")
if sync_progress.get("remaining_blocks") not in (None, 0):
    restore_safety_reasons.append(f"sync remaining blocks={sync_progress.get('remaining_blocks')}")
if source_latest_numeric is None:
    restore_safety_reasons.append("source latest block unavailable")
elif source_block_lag_to_best is not None and source_block_lag_to_best > 5:
    restore_safety_reasons.append(f"source node is {source_block_lag_to_best} blocks behind best managed node")
if isinstance(node_info, dict) and node_info.get("template_probe_failing"):
    restore_safety_reasons.append("source template probe is failing")
payload = {
    "document_type": "bdag_chain_restore_manifest",
    "generated_at": generated_at,
    "project_root": str(project_root),
    "published_path": str(published_path),
    "source_node_service": source_node_service,
    "source_node_key": source_node_key,
    "source_node_dir": source_node_dir,
    "consistent_final_stopped_sync": final_stop_sync,
    "published_from_online_warm_copy": not final_stop_sync,
    "stack_overall": status.get("overall") if isinstance(status, dict) else None,
    "sync_status": sync_progress.get("status") if isinstance(sync_progress, dict) else None,
    "sync_remaining_blocks": sync_progress.get("remaining_blocks") if isinstance(sync_progress, dict) else None,
    "source_latest_block": source_latest_block,
    "source_importing": node_info.get("importing") if isinstance(node_info, dict) else None,
    "source_last_import_at": node_info.get("last_import_at") if isinstance(node_info, dict) else None,
    "source_template_probe_failing": node_info.get("template_probe_failing") if isinstance(node_info, dict) else None,
    "source_block_lag_to_best": source_block_lag_to_best,
    "node_heights": node_heights,
    "restore_safe": not restore_safety_reasons,
    "restore_safety_reasons": restore_safety_reasons,
    "restore_guidance": "Prefer the newest manifest with stack_overall=ok, sync_status=synced, and matching node heights. Preserve node identity files when cloning between nodes.",
}
print(json.dumps(payload, indent=2, sort_keys=True))
PY
  then
    mv "$manifest_path.tmp" "$manifest_path"
    ln -sfn "$latest_manifest_link" "$PROJECT_ROOT/data-restore/latest-hourly.manifest.json"
    log "wrote snapshot manifest: $manifest_path"
  else
    rm -f "$manifest_path.tmp"
    log "warning: failed to write snapshot manifest for $published_path"
  fi
}

write_snapshot_stop_marker() {
  local event="$1"
  local epoch
  epoch="$(date -u +%s)"
  cat > "$SNAPSHOT_STOP_STATE_FILE" <<EOF
{"node":"$node_service","node_key":"$node_key","event":"$event","written_at":"$(date -Is)","written_epoch":$epoch,"recovery_seconds":$SNAPSHOT_RPC_RECOVERY_SECONDS}
EOF
}

validate_source_restore_safe() {
  local source_service="$1"
  local max_lag="$2"
  PYTHONPATH="$PROJECT_ROOT/ops" python3 - "$PROJECT_ROOT" "$source_service" "$max_lag" <<'PY'
import sys

project_root = sys.argv[1]
source_service = sys.argv[2]
try:
    max_lag = int(sys.argv[3])
except ValueError:
    max_lag = 5

from pool_ops import collect_status  # noqa: E402

status = collect_status(include_logs=False)
if not isinstance(status, dict):
    print("status unavailable")
    raise SystemExit(1)
if status.get("overall") == "down":
    print("stack overall is down")
    raise SystemExit(1)
failures = status.get("failures") or []
if failures:
    print(f"stack failures present: {failures[:3]}")
    raise SystemExit(1)

nodes = status.get("nodes") if isinstance(status.get("nodes"), dict) else {}
source = nodes.get(source_service) if isinstance(nodes, dict) else None
if not isinstance(source, dict):
    print(f"source node missing from status: {source_service}")
    raise SystemExit(1)
try:
    source_height = int(source.get("latest_block"))
except (TypeError, ValueError):
    print(f"source node height unavailable: {source_service}")
    raise SystemExit(1)

managed = status.get("managed_node_services") or []
heights = []
for name in managed:
    row = nodes.get(name)
    if not isinstance(row, dict):
        continue
    try:
        heights.append(int(row.get("best_main_order") or row.get("latest_block")))
    except (TypeError, ValueError):
        continue
if heights:
    source_lag = max(heights) - source_height
    if source_lag > max_lag:
        print(f"source node {source_service} is {source_lag} blocks behind best managed node")
        raise SystemExit(1)

sync = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
remaining = sync.get("remaining_blocks")
if isinstance(remaining, int) and remaining > max_lag:
    print(f"stack sync remaining is {remaining} blocks")
    raise SystemExit(1)

if source.get("template_probe_failing"):
    print(f"source node template probe is failing: {source_service}")
    raise SystemExit(1)

print(f"source node {source_service} safe for restore publish at block {source_height}")
PY
}

exec 8>"$STAGE_LOCK_FILE"
log "waiting for exclusive snapshot staging lock"
flock 8

read -r sync_status sync_remaining sync_unknown sync_block_lag < <(snapshot_sync_summary "$PROJECT_ROOT" 2>>"$LOG_FILE" || printf 'unknown -1 1 -1\n')
if [[ "$SNAPSHOT_UNKNOWN_BACKOFF" == "1" && "$sync_unknown" =~ ^[0-9]+$ && "$sync_unknown" -gt 0 ]]; then
  if [[ "$SNAPSHOT_ALWAYS_PUBLISH" == "1" ]]; then
    log "continuing degraded hourly snapshot: sync state unknown for $sync_unknown node(s)"
  else
    log "skipping hourly snapshot: sync state unknown for $sync_unknown node(s), preserving node resources"
    exit 0
  fi
fi
if [[ "$sync_remaining" =~ ^[0-9]+$ ]] && (( sync_remaining > SNAPSHOT_BACKOFF_BLOCKS )); then
  if [[ "$SNAPSHOT_ALWAYS_PUBLISH" == "1" ]]; then
    log "continuing degraded hourly snapshot: chain catch-up status=$sync_status max_remaining=${sync_remaining} threshold=$SNAPSHOT_BACKOFF_BLOCKS"
  else
    log "skipping hourly snapshot: chain catch-up has priority status=$sync_status max_remaining=${sync_remaining} threshold=$SNAPSHOT_BACKOFF_BLOCKS"
    exit 0
  fi
fi
if [[ "$sync_block_lag" =~ ^[0-9]+$ ]] && (( sync_block_lag > SNAPSHOT_MAX_BLOCK_LAG )); then
  if [[ "$SNAPSHOT_ALWAYS_PUBLISH" == "1" ]]; then
    log "continuing degraded hourly snapshot: node block lag=${sync_block_lag} threshold=$SNAPSHOT_MAX_BLOCK_LAG"
  else
    log "skipping hourly snapshot: node block lag has priority block_lag=${sync_block_lag} threshold=$SNAPSHOT_MAX_BLOCK_LAG"
    exit 0
  fi
fi

cleanup_stale_temps() {
  find "$SNAPSHOT_DIR" -maxdepth 1 -type f -name '.bdag-node*-hourly-*.tar.gz.tmp' -mmin +30 -delete
  find "$SNAPSHOT_DIR" -maxdepth 1 -type d -name '.bdag-node*-hourly-*.tmp' -mmin +30 -exec rm -rf {} +
}

prune_orphan_snapshot_manifests() {
  local manifest base
  find "$SNAPSHOT_DIR" -maxdepth 1 -type f -name 'bdag-node*-hourly-*.manifest.json' -print0 |
    while IFS= read -r -d '' manifest; do
      base="${manifest%.manifest.json}"
      if [[ ! -d "$base" && ! -f "$base.tar.gz" ]]; then
        rm -f "$manifest"
      fi
    done
}

prune_directory_snapshots() {
  local old_names=()
  mapfile -t old_names < <(
    find "$SNAPSHOT_DIR" -maxdepth 1 -type d -name 'bdag-node*-hourly-*' -printf '%f\n' |
      sed -E 's/^(.*-hourly-)([0-9]{8}T[0-9]{6}Z)(.*)$/\2 \0/' |
      sort -r |
      awk -v keep="$SNAPSHOT_RETAIN" 'NR > keep {print (NF > 1 ? $2 : $1)}'
  )
  if (( ${#old_names[@]} > 0 )); then
    local name
    for name in "${old_names[@]}"; do
      rm -rf "$SNAPSHOT_DIR/$name"
      rm -f "$SNAPSHOT_DIR/$name.manifest.json"
    done
  fi
  prune_orphan_snapshot_manifests
}

prune_compressed_snapshots() {
  local old_names=()
  mapfile -t old_names < <(
    find "$SNAPSHOT_DIR" -maxdepth 1 -type f -name 'bdag-node*-hourly-*.tar.gz' -printf '%f\n' |
      sed 's/\.tar\.gz$//' |
      sed -E 's/^(.*-hourly-)([0-9]{8}T[0-9]{6}Z)(.*)$/\2 \0/' |
      sort -r |
      awk -v keep="$SNAPSHOT_RETAIN" 'NR > keep {print (NF > 1 ? $2 : $1)}'
  )
  if (( ${#old_names[@]} > 0 )); then
    local name
    for name in "${old_names[@]}"; do
      rm -f "$SNAPSHOT_DIR/$name.tar.gz"
      rm -f "$SNAPSHOT_DIR/$name.tar.gz.manifest.json"
      rm -f "$SNAPSHOT_DIR/$name.manifest.json"
    done
  fi
  prune_orphan_snapshot_manifests
}

compose() {
  if [[ -f "$ENV_FILE" ]]; then
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
  else
    docker compose -f "$COMPOSE_FILE" "$@"
  fi
}

choose_node() {
  local previous next
  previous="$(cat "$STATE_FILE" 2>/dev/null || true)"
  if [[ "$previous" == "node1" ]]; then
    next="node2"
  else
    next="node1"
  fi
  printf '%s\n' "$next"
}

service_for_node_key() {
  case "$1" in
    node1) printf '%s\n' "bdag-miner-node-1" ;;
    node2) printf '%s\n' "bdag-miner-node-2" ;;
    *) return 1 ;;
  esac
}

dir_for_node_key() {
  case "$1" in
    node1) printf '%s\n' "node1" ;;
    node2) printf '%s\n' "node2" ;;
    *) return 1 ;;
  esac
}

alternate_node_key() {
  case "$1" in
    node1) printf '%s\n' "node2" ;;
    node2) printf '%s\n' "node1" ;;
    *) return 1 ;;
  esac
}

current_rpc_primary() {
  awk '
    $1 == "server" && $3 ~ /^bdag-miner-node-[12]:38131$/ {
      is_backup = 0
      for (i = 4; i <= NF; i++) {
        if ($i == "backup") {
          is_backup = 1
        }
      }
      if (!is_backup) {
        sub(":38131", "", $3)
        print $3
        exit
      }
    }
  ' "$PROJECT_ROOT/haproxy.cfg" 2>/dev/null || true
}

node_key="$(choose_node)"
node_service="$(service_for_node_key "$node_key")"
node_dir="$(dir_for_node_key "$node_key")"

if [[ "$SNAPSHOT_AVOID_RPC_PRIMARY" == "1" ]]; then
  rpc_primary="$(current_rpc_primary)"
  if [[ -n "$rpc_primary" && "$node_service" == "$rpc_primary" ]]; then
    alternate_key="$(alternate_node_key "$node_key")"
    alternate_service="$(service_for_node_key "$alternate_key")"
    alternate_dir="$(dir_for_node_key "$alternate_key")"
    if [[ -d "$PROJECT_ROOT/data/$alternate_dir" ]]; then
      log "avoiding snapshot stop of active RPC primary $node_service; using $alternate_service instead"
      node_key="$alternate_key"
      node_service="$alternate_service"
      node_dir="$alternate_dir"
    else
      log "skipping hourly snapshot: selected node is active RPC primary $node_service and alternate source is missing"
      exit 0
    fi
  fi
fi

SNAPSHOT_SOURCE="$PROJECT_ROOT/data/$node_dir"
SNAPSHOT_STAGE="$SNAPSHOT_STAGE_ROOT/$node_dir"
mkdir -p "$SNAPSHOT_STAGE"

node_stopped=0
restart_node_if_needed() {
  if [[ "$node_stopped" == "1" ]]; then
    log "restarting $node_service after interrupted snapshot"
    compose start "$node_service" >> "$LOG_FILE" 2>&1 || true
    write_snapshot_stop_marker "restarted-after-interrupted-snapshot"
  fi
}
trap restart_node_if_needed EXIT

bounded_final_sync() {
  local source_dir="$1"
  local stage_dir="$2"

  if command -v timeout >/dev/null 2>&1 && [[ "$SNAPSHOT_FINAL_STOP_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] && (( SNAPSHOT_FINAL_STOP_TIMEOUT_SECONDS > 0 )); then
    export BDAG_SNAPSHOT_RSYNC_BWLIMIT_KB
    export -f run_low_priority snapshot_rsync_node
    timeout --kill-after=10s "${SNAPSHOT_FINAL_STOP_TIMEOUT_SECONDS}s" \
      bash -c 'snapshot_rsync_node "$1" "$2"' _ "$source_dir" "$stage_dir"
  else
    snapshot_rsync_node "$source_dir" "$stage_dir"
  fi
}

bounded_warm_sync() {
  local source_dir="$1"
  local stage_dir="$2"

  if command -v timeout >/dev/null 2>&1 && [[ "$SNAPSHOT_WARM_SYNC_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] && (( SNAPSHOT_WARM_SYNC_TIMEOUT_SECONDS > 0 )); then
    export BDAG_SNAPSHOT_RSYNC_BWLIMIT_KB
    export -f run_low_priority snapshot_rsync_node
    timeout --kill-after=10s "${SNAPSHOT_WARM_SYNC_TIMEOUT_SECONDS}s" \
      bash -c 'snapshot_rsync_node "$1" "$2"' _ "$source_dir" "$stage_dir"
  else
    snapshot_rsync_node "$source_dir" "$stage_dir"
  fi
}

if [[ ! -d "$SNAPSHOT_SOURCE" ]]; then
  log "snapshot source missing: $SNAPSHOT_SOURCE"
  exit 1
fi
if [[ "$SNAPSHOT_REQUIRE_SAFE_SOURCE" == "1" ]]; then
  if ! safe_reason="$(validate_source_restore_safe "$node_service" "$SNAPSHOT_MAX_BLOCK_LAG" 2>&1)"; then
    if [[ "$SNAPSHOT_ALWAYS_PUBLISH" == "1" ]]; then
      log "continuing degraded hourly snapshot: source restore safety failed: $safe_reason"
    else
      log "skipping hourly snapshot: source restore safety failed: $safe_reason"
      exit 0
    fi
  fi
  if [[ -n "${safe_reason:-}" ]]; then
    log "$safe_reason"
  fi
fi
printf '%s\n' "$node_key" > "$STATE_FILE"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
snapshot_name="bdag-$node_dir-hourly-$stamp"
snapshot_tmp="$SNAPSHOT_DIR/.$snapshot_name.tmp"
snapshot_path="$SNAPSHOT_DIR/$snapshot_name"
archive_name="$snapshot_name.tar.gz"
archive_tmp="$SNAPSHOT_DIR/.$archive_name.tmp"
archive_path="$SNAPSHOT_DIR/$archive_name"

if [[ "$SNAPSHOT_COMPRESS" == "1" ]]; then
  log "starting hourly chain snapshot for $node_service: $archive_path"
else
  log "starting hourly chain snapshot for $node_service: $snapshot_path"
fi
cleanup_stale_temps
log "refreshing warm copy for $node_dir while stack remains online"
warm_copy_ok=0
if bounded_warm_sync "$SNAPSHOT_SOURCE" "$SNAPSHOT_STAGE" >> "$LOG_FILE" 2>&1; then
  warm_copy_ok=1
  log "warm copy complete for $node_dir"
else
  log "warm copy partial for $node_dir; will only publish if final stopped sync succeeds"
fi

read -r sync_status sync_remaining sync_unknown sync_block_lag < <(snapshot_sync_summary "$PROJECT_ROOT" 2>>"$LOG_FILE" || printf 'unknown -1 1 -1\n')
if [[ "$SNAPSHOT_UNKNOWN_BACKOFF" == "1" && "$sync_unknown" =~ ^[0-9]+$ && "$sync_unknown" -gt 0 ]]; then
  if [[ "$SNAPSHOT_ALWAYS_PUBLISH" == "1" ]]; then
    log "continuing degraded publish: sync state became unknown for $sync_unknown node(s)"
  else
    log "skipping final stopped sync: sync state became unknown for $sync_unknown node(s)"
    exit 0
  fi
fi
if [[ "$sync_remaining" =~ ^[0-9]+$ ]] && (( sync_remaining > SNAPSHOT_BACKOFF_BLOCKS )); then
  if [[ "$SNAPSHOT_ALWAYS_PUBLISH" == "1" ]]; then
    log "continuing degraded publish: chain catch-up status=$sync_status max_remaining=${sync_remaining} threshold=$SNAPSHOT_BACKOFF_BLOCKS"
  else
    log "skipping final stopped sync: chain catch-up has priority status=$sync_status max_remaining=${sync_remaining} threshold=$SNAPSHOT_BACKOFF_BLOCKS"
    exit 0
  fi
fi
if [[ "$sync_block_lag" =~ ^[0-9]+$ ]] && (( sync_block_lag > SNAPSHOT_MAX_BLOCK_LAG )); then
  if [[ "$SNAPSHOT_ALWAYS_PUBLISH" == "1" ]]; then
    log "continuing degraded publish: node block lag=${sync_block_lag} threshold=$SNAPSHOT_MAX_BLOCK_LAG"
  else
    log "skipping final stopped sync: node block lag has priority block_lag=${sync_block_lag} threshold=$SNAPSHOT_MAX_BLOCK_LAG"
    exit 0
  fi
fi

if [[ "$SNAPSHOT_FINAL_STOP_SYNC" == "1" ]]; then
  log "stopping only $node_service for final consistent sync"
  write_snapshot_stop_marker "stopping-for-final-sync"
  compose stop "$node_service" >> "$LOG_FILE" 2>&1
  node_stopped=1

  log "final sync while $node_service is stopped, timeout=${SNAPSHOT_FINAL_STOP_TIMEOUT_SECONDS}s"
  if ! bounded_final_sync "$SNAPSHOT_SOURCE" "$SNAPSHOT_STAGE" >> "$LOG_FILE" 2>&1; then
    log "final stopped sync failed or timed out; restarting $node_service and skipping snapshot publish"
    compose start "$node_service" >> "$LOG_FILE" 2>&1 || true
    write_snapshot_stop_marker "started-after-final-sync-failed"
    node_stopped=0
    exit 1
  fi

  log "starting $node_service before publishing restore point"
  compose start "$node_service" >> "$LOG_FILE" 2>&1
  write_snapshot_stop_marker "started-after-final-sync"
  node_stopped=0
else
  if [[ "$warm_copy_ok" != "1" ]]; then
    log "not publishing online warm copy because warm rsync did not complete successfully"
    exit 1
  fi
  log "skipping stopped final sync because BDAG_SNAPSHOT_FINAL_STOP_SYNC=${SNAPSHOT_FINAL_STOP_SYNC}; publishing online warm copy"
  write_snapshot_stop_marker "published-online-warm-copy"
fi

if [[ "$SNAPSHOT_COMPRESS" == "1" ]]; then
  if [[ "$SNAPSHOT_REQUIRE_SAFE_SOURCE" == "1" ]]; then
    if ! safe_reason="$(validate_source_restore_safe "$node_service" "$SNAPSHOT_MAX_BLOCK_LAG" 2>&1)"; then
      if [[ "$SNAPSHOT_ALWAYS_PUBLISH" == "1" ]]; then
        log "publishing degraded compressed restore point: source restore safety failed after sync: $safe_reason"
      else
        log "not publishing compressed restore point: source restore safety failed after sync: $safe_reason"
        exit 0
      fi
    fi
  fi
  log "compressing staged snapshot"
  run_low_priority tar -C "$SNAPSHOT_STAGE" -czf "$archive_tmp" .
  mv "$archive_tmp" "$archive_path"
  ln -sfn "hourly/$archive_name" "$PROJECT_ROOT/data-restore/latest-hourly.tar.gz"
  write_snapshot_manifest "$archive_path" "$archive_path.manifest.json" "hourly/$archive_name.manifest.json" "$SNAPSHOT_FINAL_STOP_SYNC" "$node_service" "$node_key" "$node_dir"
  log "pruning old compressed hourly snapshots, keeping $SNAPSHOT_RETAIN"
  prune_compressed_snapshots
  log "hourly chain snapshot complete: $archive_path"
else
  if [[ "$SNAPSHOT_REQUIRE_SAFE_SOURCE" == "1" ]]; then
    if ! safe_reason="$(validate_source_restore_safe "$node_service" "$SNAPSHOT_MAX_BLOCK_LAG" 2>&1)"; then
      if [[ "$SNAPSHOT_ALWAYS_PUBLISH" == "1" ]]; then
        log "publishing degraded directory restore point: source restore safety failed after sync: $safe_reason"
      else
        log "not publishing directory restore point: source restore safety failed after sync: $safe_reason"
        exit 0
      fi
    fi
  fi
  log "publishing hardlinked restore directory without compression"
  rm -rf "$snapshot_tmp"
  mkdir -p "$snapshot_tmp"
  run_low_priority cp -al "$SNAPSHOT_STAGE"/. "$snapshot_tmp"/
  mv "$snapshot_tmp" "$snapshot_path"
  ln -sfn "hourly/$snapshot_name" "$PROJECT_ROOT/data-restore/latest-hourly"
  write_snapshot_manifest "$snapshot_path" "$snapshot_path.manifest.json" "hourly/$snapshot_name.manifest.json" "$SNAPSHOT_FINAL_STOP_SYNC" "$node_service" "$node_key" "$node_dir"

  log "pruning old directory hourly snapshots, keeping $SNAPSHOT_RETAIN"
  prune_directory_snapshots
  log "hourly chain snapshot complete: $snapshot_path"
fi
