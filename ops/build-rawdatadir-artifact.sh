#!/usr/bin/env bash
set -Eeuo pipefail

# Build a signed raw-datadir FastArtifact V2 directory artifact from a stopped
# source datadir. Dual-node hosts drain and stop only the standby backend; a
# single-node host can point BDAG_RAWDATADIR_SOURCE_DIR at a pre-maintained
# sidecar copy instead.

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${BDAG_ENV_FILE:-$PROJECT_ROOT/.env}"
COMPOSE_FILE="${BDAG_COMPOSE_FILE:-$PROJECT_ROOT/docker-compose.yml}"
POOL_ADMIN_URL="${BDAG_POOL_ADMIN_URL:-http://127.0.0.1:${POOL_METRICS_PORT:-${POOL_API_PORT:-9092}}}"
ARTIFACT_BASE="${BDAG_RAWDATADIR_ARTIFACT_BASE:-$PROJECT_ROOT/data-restore/rawdatadir}"
ARTIFACT_KEEP="${BDAG_RAWDATADIR_ARTIFACT_KEEP:-3}"
NETWORK="${BDAG_RAWDATADIR_NETWORK:-${BDAG_FASTSNAP_NETWORK:-mainnet}}"
CHAIN_ID="${BDAG_RAWDATADIR_CHAIN_ID:-1404}"
NODE_IMAGE="${BDAG_RAWDATADIR_NODE_IMAGE:-${BDAG_FASTSNAP_NODE_IMAGE:-${BLOCKDAG_NODE_IMAGE:-}}}"
FASTSNAP_BIN="${BDAG_RAWDATADIR_FASTSNAP_BINARY:-}"
EXPORT_BACKEND="${BDAG_RAWDATADIR_EXPORT_BACKEND:-}"
SOURCE_DIR="${BDAG_RAWDATADIR_SOURCE_DIR:-}"
SOURCE_LABEL="${BDAG_RAWDATADIR_SOURCE_LABEL:-}"
LOCK_FILE="${BDAG_RAWDATADIR_LOCK:-$PROJECT_ROOT/ops/runtime/rawdatadir-artifact.lock}"
LOG_FILE="${BDAG_RAWDATADIR_LOG:-$PROJECT_ROOT/ops/runtime/logs/rawdatadir-artifact-$(date +%Y%m%d).log}"
MAINTENANCE_TTL="${BDAG_RAWDATADIR_MAINTENANCE_TTL:-45m}"
RESTORE_TIMEOUT_SECONDS="${BDAG_RAWDATADIR_RESTORE_TIMEOUT_SECONDS:-180}"
MAX_EXPORT_BACKEND_LAG="${BDAG_RAWDATADIR_MAX_EXPORT_BACKEND_LAG:-10000}"
REQUIRE_EXPORT_BACKEND_FRESH="${BDAG_RAWDATADIR_REQUIRE_EXPORT_BACKEND_FRESH:-1}"
REQUIRE_SIGNED="${BDAG_RAWDATADIR_REQUIRE_SIGNED:-1}"
REQUIRE_STATE_ROOT="${BDAG_RAWDATADIR_REQUIRE_STATE_ROOT:-1}"
ARCHIVE_USE_SUDO="${BDAG_RAWDATADIR_ARCHIVE_USE_SUDO:-auto}"
NODE_MODE="${BDAG_NODE_MODE:-single}"
STATUS_FILE="${BDAG_RAWDATADIR_SOURCE_STATUS:-$PROJECT_ROOT/ops/runtime/rawdatadir-source-status.json}"
DOCKER_CPU_SHARES="${BDAG_RAWDATADIR_DOCKER_CPU_SHARES:-128}"
DOCKER_BLKIO_WEIGHT="${BDAG_RAWDATADIR_DOCKER_BLKIO_WEIGHT:-10}"
DOCKER_CPUS="${BDAG_RAWDATADIR_DOCKER_CPUS:-1.5}"
NODE_METRICS_URLS="${BDAG_RAWDATADIR_NODE_METRICS_URLS:-node1=http://127.0.0.1:6061/debug/metrics/prometheus,node2=http://127.0.0.1:6062/debug/metrics/prometheus}"
ANCHOR_RPC_URL="${BDAG_RAWDATADIR_ANCHOR_RPC_URL:-${NODE_RPC_URL:-http://127.0.0.1:38131}}"
RPC_USER="${NODE_RPC_USER:-test}"
RPC_PASS="${NODE_RPC_PASS:-test}"

mkdir -p "$ARTIFACT_BASE/artifacts" "$(dirname "$LOCK_FILE")" "$(dirname "$LOG_FILE")"

exec 9>"$LOCK_FILE"
flock -n 9 || {
  echo "[$(date -Is)] raw datadir artifact build already running" | tee -a "$LOG_FILE"
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

pool_metric_value() {
  local metric="$1"
  curl -fsS "$POOL_ADMIN_URL/metrics" 2>/dev/null |
    awk -v metric="$metric" '$1 == metric || index($1, metric "{") == 1 { print $NF + 0; exit }'
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
  curl -fsS --max-time 3 "$url" 2>/dev/null |
    awk '
      $1 == "Blockdag_mainorder" { print int($2); found=1; exit }
      $1 == "chain_head_block" { fallback=int($2) }
      END { if (!found && fallback != "") print fallback }'
}

service_for_backend() {
  case "$1" in
    node1) printf '%s\n' "${BDAG_RAWDATADIR_NODE1_SERVICE:-bdag-miner-node-1}" ;;
    node2) printf '%s\n' "${BDAG_RAWDATADIR_NODE2_SERVICE:-bdag-miner-node-2}" ;;
    node) printf '%s\n' "${BDAG_RAWDATADIR_NODE_SERVICE:-node}" ;;
    *) return 1 ;;
  esac
}

datadir_for_backend() {
  case "$1" in
    node1) printf '%s\n' "${BDAG_RAWDATADIR_NODE1_DATADIR:-$PROJECT_ROOT/data/node1}" ;;
    node2) printf '%s\n' "${BDAG_RAWDATADIR_NODE2_DATADIR:-$PROJECT_ROOT/data/node2}" ;;
    node) printf '%s\n' "${BDAG_RAWDATADIR_NODE_DATADIR:-$PROJECT_ROOT/data/node}" ;;
    *) return 1 ;;
  esac
}

choose_export_backend() {
  local selected="$1"
  if [[ -n "$EXPORT_BACKEND" ]]; then
    printf '%s\n' "$EXPORT_BACKEND"
    return
  fi
  case "$selected" in
    node1) printf '%s\n' node2 ;;
    node2) printf '%s\n' node1 ;;
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
  log "pool jobs did not become ready ok=${ok:-unknown} ready=${ready:-unknown} authorized=${authorized:-unknown}"
  return 1
}

wait_db_lock_free() {
  local lock_path="$1"
  local deadline=$((SECONDS + 45))
  while ((SECONDS < deadline)); do
    if [[ -e "$lock_path" ]] && command -v fuser >/dev/null 2>&1 && fuser "$lock_path" >/dev/null 2>&1; then
      sleep 1
      continue
    fi
    return 0
  done
  return 1
}

assert_export_backend_fresh() {
  local active_backend="$1"
  local export_backend="$2"
  if [[ "$REQUIRE_EXPORT_BACKEND_FRESH" != "1" ]]; then
    log "skipping freshness gate because BDAG_RAWDATADIR_REQUIRE_EXPORT_BACKEND_FRESH=$REQUIRE_EXPORT_BACKEND_FRESH"
    return 0
  fi
  local active_order export_order lag
  active_order="$(backend_order_metric "$active_backend" || true)"
  export_order="$(backend_order_metric "$export_backend" || true)"
  if [[ -z "$active_order" || -z "$export_order" ]]; then
    log "refusing raw datadir export: could not read order metrics active=$active_backend($active_order) export=$export_backend($export_order)"
    return 1
  fi
  lag=$((active_order - export_order))
  if ((lag < 0)); then
    lag=0
  fi
  log "raw datadir freshness active=$active_backend order=$active_order export=$export_backend order=$export_order lag=$lag max=$MAX_EXPORT_BACKEND_LAG"
  if ((lag > MAX_EXPORT_BACKEND_LAG)); then
    log "refusing raw datadir export: standby node lag=$lag max=$MAX_EXPORT_BACKEND_LAG"
    return 1
  fi
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
  log "set BDAG_RAWDATADIR_NODE_IMAGE, BDAG_FASTSNAP_NODE_IMAGE, or BLOCKDAG_NODE_IMAGE"
  return 1
}

restore_export_backend() {
  local changed=0
  if [[ -n "$EXPORT_SERVICE" ]]; then
    log "starting exported backend service=$EXPORT_SERVICE"
    compose start "$EXPORT_SERVICE" 2>&1 | tee -a "$LOG_FILE"
    wait_container_running "$EXPORT_SERVICE"
    EXPORT_SERVICE=""
    changed=1
  fi
  if [[ -n "$MAINTENANCE_BACKEND" ]]; then
    log "clearing pool maintenance backend=$MAINTENANCE_BACKEND"
    admin_maintenance "$MAINTENANCE_BACKEND" false rawdatadir-restore | tee -a "$LOG_FILE" >/dev/null
    MAINTENANCE_BACKEND=""
    changed=1
  fi
  if [[ "$changed" == "1" ]]; then
    wait_pool_jobs_ready "$RESTORE_TIMEOUT_SECONDS" || true
  fi
}

cleanup() {
  local rc=$?
  if [[ "$CLEANUP_DONE" == "1" ]]; then
    exit "$rc"
  fi
  CLEANUP_DONE=1
  restore_export_backend >/dev/null 2>&1 || true
  exit "$rc"
}
trap cleanup EXIT INT TERM

collect_anchor_env() {
  PYTHONDONTWRITEBYTECODE=1 python3 - "$ANCHOR_RPC_URL" "$RPC_USER" "$RPC_PASS" "$REQUIRE_STATE_ROOT" <<'PY'
import base64
import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.request

url, user, password, require_state_root = sys.argv[1:5]
require_state_root = require_state_root.lower() not in {"0", "false", "no", "off"}

def rpc(method, params=None):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    if user or password:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=5) as resp:
        decoded = json.loads(resp.read().decode())
    if decoded.get("error"):
        raise RuntimeError(f"{method}: {decoded['error']}")
    return decoded.get("result")

def quantity(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.lower().startswith("0x") else int(value)
    raise ValueError(value)

def env(name, value):
    print(f"{name}={shlex.quote(str(value))}")

configured_block_total = os.getenv("BDAG_RAWDATADIR_BLOCK_TOTAL")
configured_tip_order = os.getenv("BDAG_RAWDATADIR_TIP_ORDER")
configured_tip_hash = os.getenv("BDAG_RAWDATADIR_TIP_HASH")
configured_state_root = os.getenv("BDAG_RAWDATADIR_STATE_ROOT")
genesis_hash = os.getenv("BDAG_RAWDATADIR_GENESIS_HASH", "")
zero = "0x" + ("0" * 64)

def zero_hash(value):
    return not value or str(value).lower() in {"0x" + ("0" * 64), "0" * 64}

for attempt in range(24):
    block_total = configured_block_total
    tip_order = configured_tip_order
    tip_hash = configured_tip_hash
    state_root = configured_state_root
    if not block_total:
        for method in ("getBlockTotal", "getBlockCount"):
            try:
                block_total = str(quantity(rpc(method)))
                break
            except Exception:
                pass
    if not tip_order:
        try:
            tip_order = str(quantity(rpc("getMainChainHeight")))
        except Exception:
            pass
    if not tip_hash and tip_order:
        for method, params in (("getBlockhash", [int(tip_order)]), ("getBestBlockHash", [])):
            try:
                tip_hash = str(rpc(method, params))
                break
            except Exception:
                pass
    if not state_root and tip_hash:
        for method, params in (("getBlockHeader", [tip_hash, True]), ("getStateRoot", [int(tip_order or 0), False])):
            try:
                result = rpc(method, params)
                if isinstance(result, dict):
                    state_root = result.get("stateRoot") or result.get("stateroot") or result.get("StateRoot")
                elif isinstance(result, str):
                    state_root = result
                if state_root:
                    break
            except Exception:
                pass
    missing = []
    try:
        if not block_total or quantity(block_total) <= 1:
            missing.append("block_total")
    except Exception:
        missing.append("block_total")
    try:
        if not tip_order or quantity(tip_order) <= 1:
            missing.append("tip_order")
    except Exception:
        missing.append("tip_order")
    if zero_hash(tip_hash):
        missing.append("tip_hash")
    if require_state_root and zero_hash(state_root):
        missing.append("state_root")
    if not missing:
        break
    if attempt == 23:
        raise SystemExit("raw datadir anchor unavailable from live RPC: " + ",".join(missing))
    time.sleep(5)

if not genesis_hash:
    for _ in range(3):
        try:
            genesis_hash = str(rpc("getBlockhash", [0]))
            break
        except Exception:
            time.sleep(1)

env("RAW_BLOCK_TOTAL", block_total)
env("RAW_TIP_ORDER", tip_order)
env("RAW_TIP_HASH", tip_hash)
env("RAW_STATE_ROOT", state_root or zero)
env("RAW_GENESIS_HASH", genesis_hash)
PY
}

archive_source_datadir() {
  local source_mainnet="$1"
  local archive="$2"
  local tmp="$archive.tmp"
  rm -f "$tmp"
  local tar_args=(
    --xattrs
    --numeric-owner
    --one-file-system
    --zstd
    -cpf "$tmp"
    -C "$source_mainnet"
    "--exclude=./network.key*"
    "--exclude=./bdageth/nodekey*"
    "--exclude=./keystore*"
    "--exclude=./bdageth/keystore*"
    "--exclude=./bdageth/nodes*"
    "--exclude=./peerstore*"
    "--exclude=./nodes*"
    "--exclude=./geth.ipc"
    "--exclude=./bdag.ipc"
    "--exclude=*.ipc"
    "--exclude=*.sock"
    .
  )
  local tar_command=(tar)
  case "${ARCHIVE_USE_SUDO,,}" in
    1|true|yes|on)
      if ! command -v sudo >/dev/null 2>&1 || ! sudo -n true 2>/dev/null; then
        log "BDAG_RAWDATADIR_ARCHIVE_USE_SUDO is enabled, but passwordless sudo is unavailable"
        return 1
      fi
      tar_command=(sudo -n tar)
      log "archiving raw datadir with sudo tar"
      ;;
    auto)
      if [[ "$(id -u)" != "0" ]] && command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        tar_command=(sudo -n tar)
        log "archiving raw datadir with sudo tar"
      fi
      ;;
    0|false|no|off)
      ;;
    *)
      log "invalid BDAG_RAWDATADIR_ARCHIVE_USE_SUDO=$ARCHIVE_USE_SUDO"
      return 1
      ;;
  esac

  if "${tar_command[@]}" "${tar_args[@]}" 2>>"$LOG_FILE"; then
    if [[ "${tar_command[0]}" == "sudo" ]]; then
      sudo chown "$(id -u):$(id -g)" "$tmp"
    fi
    mv -f "$tmp" "$archive"
    return 0
  fi
  if [[ "${tar_command[0]}" != "sudo" ]] && command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    log "retrying raw datadir archive with sudo because ordinary tar failed"
    sudo -n tar "${tar_args[@]}" 2>>"$LOG_FILE"
    sudo chown "$(id -u):$(id -g)" "$tmp"
    mv -f "$tmp" "$archive"
    return 0
  fi
  return 1
}

run_manifest_builder() {
  local stage="$1"
  local manifest="$2"
  shift 2
  if [[ "$REQUIRE_SIGNED" == "1" && -z "${BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_HEX:-}" ]]; then
    log "refusing unsigned raw datadir artifact: set BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_HEX or BDAG_RAWDATADIR_REQUIRE_SIGNED=0"
    return 1
  fi
  if [[ -n "$FASTSNAP_BIN" ]]; then
    "$FASTSNAP_BIN" --build-directory-manifest --artifact-root-dir "$stage" --manifest-out "$manifest" "$@"
    return
  fi
  if command -v fastsnap >/dev/null 2>&1; then
    fastsnap --build-directory-manifest --artifact-root-dir "$stage" --manifest-out "$manifest" "$@"
    return
  fi
  local image
  image="$(resolve_node_image)"
  docker_run_low_priority \
    --entrypoint /usr/local/bin/fastsnap \
    -e BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_ID="${BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_ID:-}" \
    -e BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_HEX="${BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_HEX:-}" \
    -v "$stage":/artifact \
    "$image" \
    --build-directory-manifest --artifact-root-dir /artifact --manifest-out /artifact/manifest.json "$@"
}

promote_current() {
  local stage="$1"
  local current="$ARTIFACT_BASE/current"
  local target
  target="$(realpath --relative-to "$ARTIFACT_BASE" "$stage" 2>/dev/null || printf '%s\n' "$stage")"
  ln -sfn "$target" "$current.tmp"
  mv -Tf "$current.tmp" "$current"
  log "raw datadir artifact current -> $target"
}

prune_old_artifacts() {
  [[ "$ARTIFACT_KEEP" =~ ^[0-9]+$ ]] || return 0
  ((ARTIFACT_KEEP > 0)) || return 0
  mapfile -t dirs < <(find "$ARTIFACT_BASE/artifacts" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -n | awk '{print $2}')
  local remove_count=$((${#dirs[@]} - ARTIFACT_KEEP))
  if ((remove_count <= 0)); then
    return 0
  fi
  local i
  for ((i=0; i<remove_count; i++)); do
    rm -rf "${dirs[$i]}"
    log "removed old raw datadir artifact ${dirs[$i]}"
  done
}

if [[ -n "$SOURCE_DIR" ]]; then
  SOURCE_MAINNET="$SOURCE_DIR"
  SOURCE_LABEL="${SOURCE_LABEL:-manual-source}"
  if [[ ! -d "$SOURCE_MAINNET/BdagChain" ]]; then
    log "source dir does not look like a $NETWORK datadir: $SOURCE_MAINNET"
    exit 1
  fi
  LIVE_NODE1_MAINNET="$(readlink -m "${BDAG_RAWDATADIR_NODE1_DATADIR:-$PROJECT_ROOT/data/node1}/$NETWORK")"
  LIVE_NODE2_MAINNET="$(readlink -m "${BDAG_RAWDATADIR_NODE2_DATADIR:-$PROJECT_ROOT/data/node2}/$NETWORK")"
  SOURCE_MAINNET_REAL="$(readlink -m "$SOURCE_MAINNET")"
  if [[ "${BDAG_RAWDATADIR_ALLOW_LIVE_SOURCE:-0}" != "1" && ( "$SOURCE_MAINNET_REAL" == "$LIVE_NODE1_MAINNET" || "$SOURCE_MAINNET_REAL" == "$LIVE_NODE2_MAINNET" ) ]]; then
    log "refusing raw datadir artifact from live node datadir: $SOURCE_MAINNET_REAL"
    log "use ops/publish-rawdatadir-artifact.sh to refresh/finalize a sidecar first"
    exit 1
  fi
  wait_db_lock_free "$SOURCE_MAINNET/BdagChain/LOCK" || {
    log "source datadir lock is still held: $SOURCE_MAINNET/BdagChain/LOCK"
    exit 1
  }
else
  if [[ "$NODE_MODE" == "single" ]]; then
    log "refusing standby export in BDAG_NODE_MODE=single; set BDAG_RAWDATADIR_SOURCE_DIR to a finalized sidecar"
    exit 1
  fi
  ACTIVE_BACKEND="$(selected_backend || true)"
  if [[ -z "$ACTIVE_BACKEND" ]]; then
    log "pool router has no selected backend; refusing to stop a node for raw datadir export"
    exit 1
  fi
  EXPORT_BACKEND="$(choose_export_backend "$ACTIVE_BACKEND")"
  EXPORT_SERVICE="$(service_for_backend "$EXPORT_BACKEND")"
  EXPORT_NODE_DIR="$(datadir_for_backend "$EXPORT_BACKEND")"
  SOURCE_MAINNET="$EXPORT_NODE_DIR/$NETWORK"
  SOURCE_LABEL="$EXPORT_BACKEND"
  if [[ "$EXPORT_BACKEND" == "$ACTIVE_BACKEND" ]]; then
    log "export backend equals active backend ($ACTIVE_BACKEND); refusing unsafe raw datadir export"
    exit 1
  fi
  if [[ ! -d "$SOURCE_MAINNET/BdagChain" ]]; then
    log "missing export datadir: $SOURCE_MAINNET/BdagChain"
    exit 1
  fi
  wait_pool_jobs_ready "$RESTORE_TIMEOUT_SECONDS"
  assert_export_backend_fresh "$ACTIVE_BACKEND" "$EXPORT_BACKEND"
  log "requesting pool maintenance drain backend=$EXPORT_BACKEND active=$ACTIVE_BACKEND"
  admin_maintenance "$EXPORT_BACKEND" true rawdatadir | tee -a "$LOG_FILE" >/dev/null
  MAINTENANCE_BACKEND="$EXPORT_BACKEND"
  wait_pool_selected_backend "$ACTIVE_BACKEND"
  wait_backend_maintenance "$EXPORT_BACKEND"
  log "stopping drained backend service=$EXPORT_SERVICE"
  compose stop "$EXPORT_SERVICE" 2>&1 | tee -a "$LOG_FILE"
  wait_container_stopped "$EXPORT_SERVICE"
  wait_pool_selected_backend "$ACTIVE_BACKEND"
  wait_db_lock_free "$SOURCE_MAINNET/BdagChain/LOCK" || {
    log "source datadir lock is still held: $SOURCE_MAINNET/BdagChain/LOCK"
    exit 1
  }
fi

STAMP="$(date +%Y%m%d-%H%M%S%Z)"
STAGE="$ARTIFACT_BASE/artifacts/rawdatadir-$STAMP"
ARCHIVE="$STAGE/node-datadir-$NETWORK-no-private-keys.tar.zst"
MANIFEST="$STAGE/manifest.json"
mkdir -p "$STAGE"

ANCHOR_FILE="$STAGE/anchor.env"
collect_anchor_env > "$ANCHOR_FILE"
source "$ANCHOR_FILE"

log "archiving raw datadir source=$SOURCE_LABEL path=$SOURCE_MAINNET"
archive_source_datadir "$SOURCE_MAINNET" "$ARCHIVE"

restore_export_backend

if [[ ! -s "$ARCHIVE" ]]; then
  log "raw datadir archive was not created: $ARCHIVE"
  exit 1
fi

(
  cd "$STAGE"
  sha256sum "$(basename "$ARCHIVE")" > SHA256SUMS
  tar --zstd -tf "$(basename "$ARCHIVE")" >/dev/null
)

cat > "$STAGE/README-RAWDATADIR.txt" <<EOF
BlockDAG raw datadir artifact

Created: $(date -Is)
Network: $NETWORK
Chain ID: $CHAIN_ID
Source: $SOURCE_LABEL
Tip order: $RAW_TIP_ORDER
Tip hash: $RAW_TIP_HASH
State root: $RAW_STATE_ROOT

Excluded identity/secret material:
- network.key variants
- bdageth/nodekey variants
- keystore and bdageth/keystore variants
- peerstore, nodes, and bdageth/nodes variants
- IPC/socket files

Fetch with ops/fetch-rawdatadir-artifact.sh or fastsnap --artifact-type raw_datadir_checkpoint.
EOF

rm -f "$MANIFEST"
log "building raw datadir FastArtifact manifest"
run_manifest_builder "$STAGE" "$MANIFEST" \
  --artifact-type raw_datadir_checkpoint \
  --network "$NETWORK" \
  --chain-id "$CHAIN_ID" \
  --genesis-hash "$RAW_GENESIS_HASH" \
  --tip-order "$RAW_TIP_ORDER" \
  --tip-hash "$RAW_TIP_HASH" \
  --block-total "$RAW_BLOCK_TOTAL" \
  --state-root "$RAW_STATE_ROOT" \
  --metadata "raw_datadir_source=$SOURCE_LABEL" \
  --metadata "raw_datadir_archive=$(basename "$ARCHIVE")"

promote_current "$STAGE"
prune_old_artifacts

log "raw datadir artifact ready: $STAGE"
log "serve with BDAG_FASTSYNC_ARTIFACT_DIRECTORY=$ARTIFACT_BASE/current and BDAG_FASTSYNC_ARTIFACT_MANIFEST=$ARTIFACT_BASE/current/manifest.json"
python3 - "$STATUS_FILE" "$STAGE" "$MANIFEST" "$RAW_TIP_ORDER" "$RAW_TIP_HASH" <<'PY'
import json
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
payload = {}
if path.exists():
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {}
payload.update({
    "last_publish_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "last_publish_dir": sys.argv[2],
    "last_manifest": sys.argv[3],
    "artifact_tip_order": sys.argv[4],
    "artifact_tip_hash": sys.argv[5],
    "serving_directory_hint": str(Path(sys.argv[2]).parents[1] / "current"),
})
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
