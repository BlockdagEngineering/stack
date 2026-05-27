#!/usr/bin/env bash
set -Eeuo pipefail

# Keep a low-priority local sidecar copy close to the live datadir. This is for
# single-node systems that cannot safely stop their only chain process for a
# long copy window. It does not publish an artifact by itself; use
# ops/build-rawdatadir-artifact.sh with BDAG_RAWDATADIR_SOURCE_DIR pointing at
# the sidecar after a guarded final sync window.

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
NETWORK="${BDAG_RAWDATADIR_NETWORK:-${BDAG_FASTSNAP_NETWORK:-mainnet}}"
SOURCE_DIR="${BDAG_RAWDATADIR_SIDECAR_SOURCE:-$PROJECT_ROOT/data/node/$NETWORK}"
SIDECAR_DIR="${BDAG_RAWDATADIR_SIDECAR_DIR:-$PROJECT_ROOT/data-restore/rawdatadir-sidecar/$NETWORK}"
LOCK_FILE="${BDAG_RAWDATADIR_SIDECAR_LOCK:-$PROJECT_ROOT/ops/runtime/rawdatadir-sidecar.lock}"
LOG_FILE="${BDAG_RAWDATADIR_SIDECAR_LOG:-$PROJECT_ROOT/ops/runtime/logs/rawdatadir-sidecar-$(date +%Y%m%d).log}"
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

rsync_args=(
  -aH
  --numeric-ids
  --partial
  --inplace
  "--exclude=/network.key"
  "--exclude=/bdageth/nodekey"
  "--exclude=/keystore"
  "--exclude=/bdageth/keystore"
  "--exclude=/peerstore"
  "--exclude=/nodes"
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
