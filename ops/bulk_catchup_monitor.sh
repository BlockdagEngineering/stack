#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="${BDAG_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
RUNTIME_DIR="$STACK_DIR/ops/runtime"
STATE_FILE="$RUNTIME_DIR/bulk-catchup-mode.json"
LOG_FILE="$RUNTIME_DIR/logs/bulk-catchup-monitor.log"
THRESHOLD="${BDAG_BULK_CATCHUP_RESUME_BLOCKS:-50}"
METRICS_URL="${BDAG_BULK_CATCHUP_METRICS_URL:-http://127.0.0.1:6061/debug/metrics/prometheus}"

mkdir -p "$RUNTIME_DIR/logs"

metrics="$(curl -fsS --max-time 5 "$METRICS_URL" || true)"
if [[ -z "$metrics" ]]; then
  printf '{"updated_at":"%s","mode":"bulk_catchup","status":"waiting_for_node_metrics"}\n' "$(date -Is)" >"$STATE_FILE"
  echo "$(date -Is) waiting_for_node_metrics" >>"$LOG_FILE"
  exit 0
fi

metric_value() {
  awk -v name="$1" '$1 == name {print int($2); found=1} END {if (!found) print 0}' <<<"$metrics"
}

main_order="$(metric_value Blockdag_mainorder)"
head_block="$(metric_value chain_head_block)"
lead="$(metric_value p2p_miningFreshness_bestPeerLeadBlocks)"
best_peer="$(metric_value p2p_miningFreshness_bestPeerMainOrder)"
sync_peer="$(metric_value p2p_miningFreshness_syncPeerPresent)"
fresh_peer="$(metric_value p2p_miningFreshness_syncPeerFresh)"
metric_lead="$lead"
computed_lead=0
if [[ "$best_peer" -gt "$main_order" ]]; then
  computed_lead=$((best_peer - main_order))
fi
if [[ "$computed_lead" -gt 0 ]]; then
  lead="$computed_lead"
fi

if [[ "$sync_peer" -ge 1 && "$fresh_peer" -ge 1 && "$lead" -le "$THRESHOLD" ]]; then
  {
    printf '{"updated_at":"%s","mode":"resume_pool","status":"starting_pool","main_order":%s,"head_block":%s,"lead_blocks":%s,"best_peer_main_order":%s,"threshold":%s}\n' \
      "$(date -Is)" "$main_order" "$head_block" "$lead" "$best_peer" "$THRESHOLD"
  } >"$STATE_FILE"
  echo "$(date -Is) resume_pool lead=$lead metric_lead=$metric_lead threshold=$THRESHOLD main_order=$main_order best_peer=$best_peer" >>"$LOG_FILE"
  cd "$STACK_DIR"
  docker compose --env-file asic-pool/.env up -d pool-db asic-pool
  systemctl --user start \
    bdag-watchdog.service \
    bdag-mining-30min-check.timer \
    bdag-fastsync-peer-monitor.timer \
    bdag-local-peers.timer \
    bdag-p2p-guard.service || true
  systemctl --user stop bdag-bulk-catchup-monitor.timer || true
  exit 0
fi

printf '{"updated_at":"%s","mode":"bulk_catchup","status":"waiting","main_order":%s,"head_block":%s,"lead_blocks":%s,"metric_lead_blocks":%s,"best_peer_main_order":%s,"sync_peer_present":%s,"sync_peer_fresh":%s,"threshold":%s}\n' \
  "$(date -Is)" "$main_order" "$head_block" "$lead" "$metric_lead" "$best_peer" "$sync_peer" "$fresh_peer" "$THRESHOLD" >"$STATE_FILE"
echo "$(date -Is) waiting lead=$lead metric_lead=$metric_lead threshold=$THRESHOLD main_order=$main_order best_peer=$best_peer" >>"$LOG_FILE"
