#!/usr/bin/env bash
set -Eeuo pipefail

# Keep a low-priority local sidecar copy close to the live datadir. This is for
# single-node systems that cannot safely stop their only chain process for a
# long copy window. It does not publish an artifact by itself; use
# ops/build-rawdatadir-artifact.sh with BDAG_RAWDATADIR_SOURCE_DIR pointing at
# the sidecar after a guarded final sync window.

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
NETWORK="${BDAG_RAWDATADIR_NETWORK:-${BDAG_FASTSNAP_NETWORK:-mainnet}}"
ACTIVE_NODE_SERVICE="${BDAG_RAWDATADIR_SINGLE_NODE_SERVICE:-${BDAG_NODE_SERVICES%%,*}}"
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

mkdir -p "$(dirname "$LOCK_FILE")" "$(dirname "$LOG_FILE")" "$SIDECAR_DIR"

exec 9>"$LOCK_FILE"
flock -n 9 || {
  echo "[$(date -Is)] raw datadir sidecar sync already running" | tee -a "$LOG_FILE"
  exit 0
}

log() {
  echo "[$(date -Is)] $*" | tee -a "$LOG_FILE"
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

if [[ ! -d "$SOURCE_DIR/BdagChain" ]]; then
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

rsync_args=(
  -aH
  --numeric-ids
  --one-file-system
  --partial
  --partial-dir=.rsync-partial
  --delay-updates
  "--exclude=/network.key"
  "--exclude=/bdageth/nodekey"
  "--exclude=/keystore"
  "--exclude=/bdageth/keystore"
  "--exclude=/peerstore"
  "--exclude=/nodes"
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

log "syncing raw datadir sidecar source=$SOURCE_DIR target=$SIDECAR_DIR"
run_low_priority rsync "${rsync_args[@]}" "$SOURCE_DIR/" "$SIDECAR_DIR/" 2>&1 | tee -a "$LOG_FILE"
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
