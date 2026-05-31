#!/usr/bin/env bash
set -Eeuo pipefail

# Keep a low-priority local sidecar copy close to the live datadir. This is for
# single-node systems that cannot safely stop their only chain process for a
# long copy window. It does not publish an artifact by itself; use
# ops/build-rawdatadir-artifact.sh with BDAG_RAWDATADIR_SOURCE_DIR pointing at
# the sidecar after a guarded final sync window.

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
NETWORK="${BDAG_RAWDATADIR_NETWORK:-${BDAG_FASTSNAP_NETWORK:-mainnet}}"
NODE_SERVICES_CSV="${BDAG_NODE_SERVICES:-bdag-miner-node-1}"
ACTIVE_NODE_SERVICE="${BDAG_RAWDATADIR_SINGLE_NODE_SERVICE:-${NODE_SERVICES_CSV%%,*}}"
ACTIVE_NODE_SERVICE="${ACTIVE_NODE_SERVICE:-bdag-miner-node-1}"
case "$ACTIVE_NODE_SERVICE" in
  bdag-miner-node-1|node1) DEFAULT_NODE_DIR="${BDAG_NODE1_DATA_DIR:-$PROJECT_ROOT/data/node1}" ;;
  bdag-miner-node-2|node2) DEFAULT_NODE_DIR="${BDAG_NODE2_DATA_DIR:-$PROJECT_ROOT/data/node2}" ;;
  *) DEFAULT_NODE_DIR="${BDAG_NODE_DATA_DIR:-$PROJECT_ROOT/data/node}" ;;
esac
SOURCE_DIR="${BDAG_RAWDATADIR_SIDECAR_SOURCE:-$DEFAULT_NODE_DIR/$NETWORK}"
SIDECAR_DIR="${BDAG_RAWDATADIR_SIDECAR_DIR:-$PROJECT_ROOT/data-restore/rawdatadir-sidecar/$NETWORK}"
LOCK_FILE="${BDAG_RAWDATADIR_SIDECAR_LOCK:-$PROJECT_ROOT/ops/runtime/rawdatadir-sidecar.lock}"
LOG_FILE="${BDAG_RAWDATADIR_SIDECAR_LOG:-$PROJECT_ROOT/ops/runtime/logs/rawdatadir-sidecar-$(date +%Y%m%d).log}"
STATUS_FILE="${BDAG_RAWDATADIR_SOURCE_STATUS:-$PROJECT_ROOT/ops/runtime/rawdatadir-source-status.json}"
DELETE_MODE="${BDAG_RAWDATADIR_SIDECAR_DELETE:-1}"
BWLIMIT="${BDAG_RAWDATADIR_SIDECAR_RSYNC_BWLIMIT:-}"
USE_SUDO="${BDAG_RAWDATADIR_SIDECAR_USE_SUDO:-auto}"
CONTENT_MODE="${BDAG_RAWDATADIR_SIDECAR_CONTENT_MODE:-auto}"
CONTENT_SCRIPT="$PROJECT_ROOT/ops/seal_rawdatadir_sidecar_content.py"

mkdir -p "$(dirname "$LOCK_FILE")" "$(dirname "$LOG_FILE")" "$SIDECAR_DIR"

exec 9>"$LOCK_FILE"
flock -n 9 || {
  echo "[$(date -Is)] raw datadir sidecar sync already running" | tee -a "$LOG_FILE"
  exit 0
}

log() {
  echo "[$(date -Is)] $*" | tee -a "$LOG_FILE"
}

maintenance_backoff_reason() {
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PROJECT_ROOT/ops" BDAG_PROJECT_ROOT="$PROJECT_ROOT" python3 - "$1" <<'PY'
import sys

from pool_ops import background_maintenance_decision, collect_status_cached

decision = background_maintenance_decision(sys.argv[1], collect_status_cached(include_logs=False))
if not decision.get("allowed", True):
    print("; ".join(str(item) for item in decision.get("reasons", []) if item))
PY
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

source_datadir_exists() {
  if [[ -d "$SOURCE_DIR/BdagChain" ]]; then
    return 0
  fi
  case "${USE_SUDO,,}" in
    1|true|yes|on|auto)
      command -v sudo >/dev/null 2>&1 && sudo -n test -d "$SOURCE_DIR/BdagChain"
      return
      ;;
  esac
  return 1
}

if ! source_datadir_exists; then
  log "source dir does not look like a $NETWORK datadir: $SOURCE_DIR"
  exit 1
fi
if ! command -v rsync >/dev/null 2>&1; then
  log "rsync is required for raw datadir sidecar sync"
  exit 1
fi
if ! "$PROJECT_ROOT/ops/fastartifact_source_eligibility.py" --status-file "$STATUS_FILE" >/dev/null; then
  log "raw datadir source sidecar disabled by eligibility policy; see $STATUS_FILE"
  exit 0
fi

if ! pressure_reason="$(maintenance_backoff_reason rawdatadir_sidecar 2>>"$LOG_FILE")"; then
  log "skipping raw datadir sidecar sync: background maintenance gate unavailable"
  exit 0
fi
if [[ -n "$pressure_reason" ]]; then
  log "skipping raw datadir sidecar sync: background maintenance backoff active: $pressure_reason"
  exit 0
fi

rsync_args=(
  -aH
  --numeric-ids
  --one-file-system
  --partial
  --partial-dir=.rsync-partial
  --delay-updates
  "--exclude=/network.key*"
  "--exclude=/bdageth/nodekey*"
  "--exclude=/keystore*"
  "--exclude=/bdageth/keystore*"
  "--exclude=/bdageth/nodes*"
  "--exclude=/peerstore*"
  "--exclude=/nodes*"
  "--exclude=/.rsync-partial"
  "--exclude=/snapshot.bdsnap"
  "--exclude=/artifact.manifest.json"
  "--exclude=/LOCK"
  "--exclude=/BdagChain/LOCK"
  "--exclude=*.ipc"
  "--exclude=*.sock"
)
if [[ "$DELETE_MODE" == "1" ]]; then
  rsync_args+=(--delete --delete-excluded)
fi
if [[ -n "$BWLIMIT" ]]; then
  rsync_args+=(--bwlimit "$BWLIMIT")
fi

rsync_command=(rsync)
case "${USE_SUDO,,}" in
  1|true|yes|on)
    if ! command -v sudo >/dev/null 2>&1 || ! sudo -n true 2>/dev/null; then
      log "BDAG_RAWDATADIR_SIDECAR_USE_SUDO is enabled, but passwordless sudo is unavailable"
      exit 1
    fi
    rsync_command=(sudo -n rsync)
    ;;
  auto)
    if [[ "$(id -u)" != "0" ]] && command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
      rsync_command=(sudo -n rsync)
    fi
    ;;
  0|false|no|off)
    ;;
  *)
    log "invalid BDAG_RAWDATADIR_SIDECAR_USE_SUDO=$USE_SUDO"
    exit 1
    ;;
esac

log "syncing raw datadir sidecar source=$SOURCE_DIR target=$SIDECAR_DIR"
set +e
run_low_priority "${rsync_command[@]}" "${rsync_args[@]}" "$SOURCE_DIR/" "$SIDECAR_DIR/" 2>&1 | tee -a "$LOG_FILE"
rsync_status="${PIPESTATUS[0]}"
set -e
case "$rsync_status" in
  0)
    ;;
  24)
    log "raw datadir sidecar sync saw vanished hot-db files; continuing with best-effort hot sidecar seal"
    ;;
  *)
    log "raw datadir sidecar sync failed rc=$rsync_status"
    exit "$rsync_status"
    ;;
esac
log "raw datadir sidecar sync complete"
python3 - "$STATUS_FILE" "$SOURCE_DIR" "$SIDECAR_DIR" <<'PY'
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
    "last_sidecar_sync_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "last_sidecar_source": sys.argv[2],
    "last_sidecar_dir": sys.argv[3],
})
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

case "${CONTENT_MODE,,}" in
  0|false|no|off|disabled)
    log "raw datadir sidecar content sealing disabled by BDAG_RAWDATADIR_SIDECAR_CONTENT_MODE=$CONTENT_MODE"
    ;;
  *)
    if [[ -x "$CONTENT_SCRIPT" ]]; then
      log "sealing raw datadir sidecar content artifact"
      seal_env=(
        "BDAG_PROJECT_ROOT=$PROJECT_ROOT"
        "BDAG_ENV_FILE=${BDAG_ENV_FILE:-$PROJECT_ROOT/.env}"
        "BDAG_RAWDATADIR_NETWORK=$NETWORK"
        "BDAG_RAWDATADIR_SIDECAR_DIR=$SIDECAR_DIR"
        "BDAG_RAWDATADIR_SOURCE_STATUS=$STATUS_FILE"
        "BDAG_RAWDATADIR_SIDECAR_CONTENT_MODE=$CONTENT_MODE"
        "BDAG_RAWDATADIR_SIDECAR_CONTENT_OWNER_UID=$(id -u)"
        "BDAG_RAWDATADIR_SIDECAR_CONTENT_OWNER_GID=$(id -g)"
      )
      if [[ "${rsync_command[0]}" == "sudo" ]]; then
        if ! run_low_priority sudo -n env "${seal_env[@]}" python3 "$CONTENT_SCRIPT" 2>&1 | tee -a "$LOG_FILE"; then
          log "raw datadir sidecar content sealing failed; see status file"
        fi
      else
        if ! run_low_priority env "${seal_env[@]}" python3 "$CONTENT_SCRIPT" 2>&1 | tee -a "$LOG_FILE"; then
          log "raw datadir sidecar content sealing failed; see status file"
        fi
      fi
    else
      log "raw datadir sidecar content sealing skipped: missing $CONTENT_SCRIPT"
    fi
    ;;
esac
