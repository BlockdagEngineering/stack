#!/usr/bin/env bash
set -euo pipefail

root="${1:-.}"

fail() {
  printf 'restart hardening validation failed: %s\n' "$*" >&2
  exit 1
}

need_file() {
  local path="$root/$1"
  [[ -f "$path" ]] || fail "missing $1"
}

need_grep() {
  local pattern="$1"
  local file="$2"
  grep -Eq "$pattern" "$root/$file" || fail "$file does not match required pattern: $pattern"
}

reject_grep() {
  local pattern="$1"
  local file="$2"
  if grep -Eq "$pattern" "$root/$file"; then
    fail "$file still matches rejected pattern: $pattern"
  fi
}

need_file "ops/watchdog.py"
need_file "ops/sync_coordinator.py"
need_file "ops/latest_chain_candidate.py"
need_file "ops/update-local-peers.py"
need_file "ops/install-dashboard.sh"
need_file "ops/stack_sentinel.py"
need_file "ops/node_child_guard.py"
need_file "ops/incident_journal.py"
need_file "ops/incident_reporter.py"
need_file "ops/pool_ops.py"
need_file "ops/tests/test_miner_retirement_identity.py"
need_file "ops/dashboard.py"
need_file "ops/build-pi5-arm64-release.sh"
need_file "ops/release-install.sh"
need_file "ops/README.md"
need_file "ops/systemd/user-bdag-stack-sentinel.timer"
need_file "ops/systemd/user-bdag-node-child-guard.timer"
need_file "ops/systemd/user-bdag-incident-reporter.timer"
need_file "ops/systemd/user-bdag-hourly-snapshot.timer"
need_file "ops/systemd/user-bdag-chain-restore-guard.timer"
need_file "ops/systemd/user-bdag-fastsnap-seed.timer"
need_file "ops/build-fastsnap-seed.sh"
need_file "ops/fastartifact_sidecar.py"
need_file "ops/systemd/user-bdag-fastartifact-sidecar.service"
need_file "ops/systemd/user-bdag-fastartifact-sidecar.timer"
need_file "haproxy.cfg"
need_file "asic-pool/schema.sql"

if [[ -e "$root/ops/observability" ]]; then
  fail "retired ops/observability dashboard stack is present in RC dashboard path"
fi

need_grep 'BDAG_ENABLE_AUTOMATIC_CLEAN_RESTORE.*False' "ops/watchdog.py"
need_grep 'BDAG_BOOT_REPAIR_DIRTY_POLICY.*start' "ops/watchdog.py"
need_grep 'BDAG_BOOT_REPAIR_CRITICAL_POLICY.*restart' "ops/watchdog.py"
need_grep 'def boot_repair_mode' "ops/watchdog.py"
need_grep 'def run_boot_repair_mode' "ops/watchdog.py"
need_grep 'if not AUTOMATIC_CLEAN_RESTORE_ENABLED:' "ops/watchdog.py"
reject_grep 'boot-repair found dirty shutdown marker:.*run_repair\("clean"' "ops/watchdog.py"

need_grep 'BDAG_ENABLE_AUTOMATIC_CLEAN_RESTORE=0' "ops/install-dashboard.sh"
need_grep 'BDAG_BOOT_REPAIR_DIRTY_POLICY=start' "ops/install-dashboard.sh"
need_grep 'BDAG_BOOT_REPAIR_CRITICAL_POLICY=restart' "ops/install-dashboard.sh"
need_grep 'BDAG_INCIDENT_REPORT_ENABLED=0' "ops/install-dashboard.sh"
need_grep 'incident-reporter.timer' "ops/install-dashboard.sh"
need_grep 'BDAG_FASTSYNC_PREPROCESS_WORKERS 1' "ops/install-dashboard.sh"
need_grep 'OnCalendar=hourly' "ops/install-dashboard.sh"

need_grep 'MIN_TRUSTED_HEIGHT' "ops/sync_coordinator.py"
need_grep 'def remembered_highest_block' "ops/sync_coordinator.py"
need_grep 'network_highest = max\(current_network_highest, remembered_highest\)' "ops/sync_coordinator.py"
need_grep 'observed_highest_block' "ops/sync_coordinator.py"
need_grep 'refusing follower seed because leader is not proven near tip' "ops/sync_coordinator.py"
need_grep 'BDAG_SYNC_COORDINATOR_FAR_BEHIND_BLOCKS.*1000' "ops/sync_coordinator.py"
need_grep 'BDAG_SYNC_COORDINATOR_SEED_NEAR_TIP_BLOCKS.*5' "ops/sync_coordinator.py"
need_grep 'BDAG_SYNC_COORDINATOR_LEADER_CPU_SHARES.*8192' "ops/sync_coordinator.py"
need_grep 'apply_leader_catchup_resources' "ops/sync_coordinator.py"
need_grep 'action = "clear_pause_state"' "ops/sync_coordinator.py"
need_grep 'resume_allowed_now = safe_int\(decision.get\("target_remaining_blocks"\)' "ops/sync_coordinator.py"
reject_grep 'network_highest = max\(current_network_highest, remembered_highest, highest_height\)' "ops/sync_coordinator.py"

need_grep 'prefer the newest chain data only after the manifest is restore-safe' "ops/latest_chain_candidate.py"
need_grep 'reject unsafe warm copies' "ops/latest_chain_candidate.py"
need_grep 'latest-chain-candidate-state.json' "ops/latest_chain_candidate.py"

need_grep 'def sort_public_peers_by_latency' "ops/update-local-peers.py"
need_grep 'def public_peer_assignment' "ops/update-local-peers.py"
need_grep 'paused_follower=' "ops/update-local-peers.py"

need_grep 'automatic clean restore is disabled' "ops/README.md"
need_grep 'preserves current node data' "ops/README.md"

need_grep 'BDAG_FASTSYNC_PREPROCESS_WORKERS=1' ".env.example"
need_grep 'BDAG_FASTSNAP_SEED_TIMER_ENABLED=1' ".env.example"
need_grep 'BDAG_FASTSNAP_MAX_EXPORT_BACKEND_LAG=1000' ".env.example"
need_grep 'BDAG_FASTARTIFACT_SIDECAR_MAX_SEED_LAG=10000' ".env.example"
need_grep 'BDAG_FASTSNAP_DISCOVERY=1' ".env.example"
need_grep 'BDAG_FASTSYNC_ARTIFACT_MANIFEST_TTL=24h' ".env.example"
need_grep 'POOL_RPC_ROUTER_TEMPLATE_LANE_MODE=active-passive' ".env.example"
need_grep 'BDAG_STACK_SERVICES=pool-db,bdag-miner-node-2,rpc-failover,asic-pool' ".env.example"
need_grep 'OnCalendar=hourly' "ops/systemd/user-bdag-hourly-snapshot.timer"

need_grep '"status_version": 2' "ops/pool_ops.py"
need_grep '"mode": mode' "ops/pool_ops.py"
need_grep '"can_mine": can_mine' "ops/pool_ops.py"
need_grep '"can_accept_shares": can_accept_shares' "ops/pool_ops.py"
need_grep '"can_submit_blocks": can_submit_blocks' "ops/pool_ops.py"
need_grep '"truth_sources": truth_sources' "ops/pool_ops.py"
need_grep '"chain_block_count": "getBlockCount"' "ops/pool_ops.py"
need_grep 'MINER_RETIREMENTS_FILE' "ops/pool_ops.py"
need_grep 'def retired_miner_identity_decision' "ops/pool_ops.py"
need_grep 'MAC is the only permanent ASIC identity' "ops/pool_ops.py"
need_grep 'matched_by": "ip-observation"' "ops/pool_ops.py"
need_grep 'only MAC address can retire an ASIC' "ops/pool_ops.py"
need_grep 'def miner_health_count_summary' "ops/pool_ops.py"
need_grep 'managed_ok_count' "ops/pool_ops.py"
need_grep 'def build_pool_efficiency_loss_ledger' "ops/pool_ops.py"
need_grep 'def selected_backend_readiness_contract' "ops/pool_ops.py"
need_grep '"identity_basis": "mac"' "ops/pool_ops.py"
need_grep 'pool efficiency loss ledger' "ops/pool_ops.py"
need_grep 'lossLedgerStatusText' "ops/dashboard.py"
reject_grep 'matched_by": "worker"[,}]' "ops/pool_ops.py"
reject_grep 'matched_by": "ip"[,}]' "ops/pool_ops.py"
need_grep 'except OSError:' "ops/dashboard.py"

need_grep 'def node_chain_rpc_snapshot' "ops/pool_ops.py"
need_grep 'getBlockCount' "ops/pool_ops.py"
need_grep 'getMainChainHeight' "ops/pool_ops.py"
need_grep 'chain_block_count' "ops/pool_ops.py"
need_grep 'current_block_source": "getBlockCount"' "ops/pool_ops.py"
need_grep 'nodeBlockHeight' "ops/dashboard.py"
need_grep 'firstPresent\(syncNode\?\.chain_block_count, node\?\.chain_block_count, null\)' "ops/dashboard.py"

need_grep 'BDAG_NODE_MODE=single' "ops/build-pi5-arm64-release.sh"
need_grep 'COMPOSE_PROFILES dual-node' "ops/release-install.sh"
need_grep 'profiles:' "ops/build-pi5-arm64-release.sh"
need_grep 'dual-node' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_NODE_MINING_ARGS' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_ENABLE_NODE_MINING=0' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_FASTSYNC_PREPROCESS_WORKERS=1' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_FASTSYNC_LAN_PEERS' "ops/build-pi5-arm64-release.sh"
need_grep 'private/VPN second, public last' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_FASTSNAP_DISCOVERY=1' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_FASTSNAP_TRUSTED_SIGNERS' "ops/build-pi5-arm64-release.sh"
need_grep 'bdag-fastartifact-sidecar.timer' "ops/install-p2p-services.sh"

need_grep 'stack-sentinel.timer' "ops/install-dashboard.sh"
need_grep 'stack_sentinel.py' "ops/install-dashboard.sh"
need_grep 'chain_restore_guard.py' "ops/install-dashboard.sh"
need_grep 'update-local-peers.py --apply' "ops/install-dashboard.sh"
need_grep 'pool-db,bdag-miner-node-2,rpc-failover,asic-pool' "ops/install-dashboard.sh"

need_grep 'server node2 bdag-miner-node-2:38131' "haproxy.cfg"
need_grep 'server node1 bdag-miner-node-1:38131.*backup.*init-addr libc,none' "haproxy.cfg"

need_grep 'BDAG_INCIDENT_REPORT_REPO' "ops/incident_reporter.py"
need_grep 'BDAG_INCIDENT_REPORT_ENABLED' "ops/incident_reporter.py"
need_grep 'def redact' "ops/incident_reporter.py"

python3 - "$root/ops/dashboard.py" <<'PY'
from pathlib import Path
import re
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r"function nodeBlockHeight\(.*?\n    \}", text, re.S)
if not match:
    raise SystemExit("dashboard nodeBlockHeight function not found")
body = match.group(0)
for forbidden in ("template_probe_last_height", "latest_block", "best_main_order", "fan_in", "highest_block"):
    if forbidden in body:
        raise SystemExit(f"dashboard nodeBlockHeight still uses non-chain source: {forbidden}")
if "chain_block_count" not in body:
    raise SystemExit("dashboard nodeBlockHeight does not use chain_block_count")
PY

if [[ -e "$root/ops/runtime" || -n "$(find "$root/ops" -path '*/__pycache__*' -o -name '*.pyc' -print -quit)" ]]; then
  fail "ops bundle contains runtime state or Python bytecode"
fi

printf 'restart hardening validation passed for %s\n' "$root"
