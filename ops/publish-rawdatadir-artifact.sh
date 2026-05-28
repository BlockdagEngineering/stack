#!/usr/bin/env bash
set -Eeuo pipefail

# Refresh the raw-datadir sidecar and, when a safe finalized source is
# available, publish a signed immutable FastArtifact V2 generation. The default
# single-node mode does not stop the live node; set
# BDAG_RAWDATADIR_SINGLE_NODE_FINALIZE=1 only for an operator-approved
# finalization window.

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
NETWORK="${BDAG_RAWDATADIR_NETWORK:-${BDAG_FASTSNAP_NETWORK:-mainnet}}"
STATUS_FILE="${BDAG_RAWDATADIR_SOURCE_STATUS:-$PROJECT_ROOT/ops/runtime/rawdatadir-source-status.json}"
LOCK_FILE="${BDAG_RAWDATADIR_PUBLISH_LOCK:-$PROJECT_ROOT/ops/runtime/rawdatadir-publish.lock}"
LOG_FILE="${BDAG_RAWDATADIR_PUBLISH_LOG:-$PROJECT_ROOT/ops/runtime/logs/rawdatadir-publish-$(date +%Y%m%d).log}"
NODE_MODE="${BDAG_NODE_MODE:-single}"
FINALIZE_SINGLE_NODE="${BDAG_RAWDATADIR_SINGLE_NODE_FINALIZE:-0}"
ACTIVE_SERVICE="${BDAG_RAWDATADIR_SINGLE_NODE_SERVICE:-${BDAG_NODE_SERVICES%%,*}}"
ACTIVE_SERVICE="${ACTIVE_SERVICE:-bdag-miner-node-1}"
SIDECAR_DIR="${BDAG_RAWDATADIR_SIDECAR_DIR:-$PROJECT_ROOT/data-restore/rawdatadir-sidecar/$NETWORK}"

mkdir -p "$(dirname "$LOCK_FILE")" "$(dirname "$LOG_FILE")"

exec 9>"$LOCK_FILE"
flock -n 9 || {
  echo "[$(date -Is)] raw datadir publish already running" | tee -a "$LOG_FILE"
  exit 0
}

log() {
  echo "[$(date -Is)] $*" | tee -a "$LOG_FILE"
}

write_status_note() {
  local note="$1"
  python3 - "$STATUS_FILE" "$note" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
note = sys.argv[2]
payload = {}
if path.exists():
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = {}
payload["last_publish_note"] = note
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

run_eligibility() {
  if ! "$PROJECT_ROOT/ops/fastartifact_source_eligibility.py" --full --json --status-file "$STATUS_FILE" 2>&1 | tee -a "$LOG_FILE"; then
    log "raw datadir source eligibility denied; see $STATUS_FILE"
    exit 0
  fi
}

compose() {
  docker compose --env-file "${BDAG_ENV_FILE:-$PROJECT_ROOT/.env}" -f "${BDAG_COMPOSE_FILE:-$PROJECT_ROOT/docker-compose.yml}" "$@"
}

stop_active_node_for_final_sync() {
  if [[ "$NODE_MODE" != "single" ]]; then
    return 0
  fi
  if [[ "$FINALIZE_SINGLE_NODE" != "1" ]]; then
    log "single-node artifact publish requires BDAG_RAWDATADIR_SINGLE_NODE_FINALIZE=1; refreshed sidecar only"
    write_status_note "publish skipped: single-node finalization was not approved"
    exit 0
  fi
  log "operator-approved finalization: stopping $ACTIVE_SERVICE for final sidecar sync"
  compose stop "$ACTIVE_SERVICE" 2>&1 | tee -a "$LOG_FILE"
}

start_active_node_after_final_sync() {
  if [[ "$NODE_MODE" == "single" && "$FINALIZE_SINGLE_NODE" == "1" ]]; then
    log "restarting $ACTIVE_SERVICE after final sidecar sync"
    compose start "$ACTIVE_SERVICE" 2>&1 | tee -a "$LOG_FILE"
  fi
}

run_eligibility

log "refreshing raw datadir sidecar"
"$PROJECT_ROOT/ops/maintain-rawdatadir-sidecar.sh" 2>&1 | tee -a "$LOG_FILE"

stop_active_node_for_final_sync
trap start_active_node_after_final_sync EXIT INT TERM

if [[ "$NODE_MODE" == "single" && "$FINALIZE_SINGLE_NODE" == "1" ]]; then
  log "running final sidecar sync while $ACTIVE_SERVICE is stopped"
  "$PROJECT_ROOT/ops/maintain-rawdatadir-sidecar.sh" 2>&1 | tee -a "$LOG_FILE"
  start_active_node_after_final_sync
  trap - EXIT INT TERM
fi

if [[ ! -d "$SIDECAR_DIR/BdagChain" ]]; then
  log "sidecar does not look publishable: $SIDECAR_DIR"
  write_status_note "publish skipped: sidecar missing BdagChain"
  exit 1
fi

log "building raw datadir artifact from finalized sidecar $SIDECAR_DIR"
BDAG_RAWDATADIR_SOURCE_DIR="$SIDECAR_DIR" \
BDAG_RAWDATADIR_SOURCE_LABEL="${BDAG_RAWDATADIR_SOURCE_LABEL:-single-node-sidecar}" \
  "$PROJECT_ROOT/ops/build-rawdatadir-artifact.sh" 2>&1 | tee -a "$LOG_FILE"
write_status_note "publish complete"
