#!/usr/bin/env bash
set -euo pipefail

mode="source"
if [[ "${1:-}" == "--mode" ]]; then
  mode="${2:-source}"
  shift 2
elif [[ "${1:-}" == --mode=* ]]; then
  mode="${1#--mode=}"
  shift
fi
case "$mode" in
  source|live-runtime) ;;
  *) printf 'usage: %s [--mode source|live-runtime] [root]\n' "$0" >&2; exit 2 ;;
esac

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

validate_haproxy_semantics() {
  python3 - "$root/haproxy.cfg" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
lines = [
    line.strip()
    for line in path.read_text(encoding="utf-8").splitlines()
    if line.strip().startswith("server ")
]

def server_line(name: str, address: str) -> list[str]:
    for line in lines:
        tokens = line.split()
        if len(tokens) >= 3 and tokens[0] == "server" and tokens[1] == name and tokens[2] == address:
            return tokens
    raise SystemExit(f"missing HAProxy server {name} {address}")

node2 = server_line("node2", "bdag-miner-node-2:38131")
node1 = server_line("node1", "bdag-miner-node-1:38131")
if "backup" in node2:
    raise SystemExit("node2 must be the primary HAProxy backend")
if "backup" not in node1:
    raise SystemExit("node1 must be configured as the backup backend")
if node1[-1] != "backup":
    raise SystemExit("node1 backup token must be last to match stack-sentinel canonical form")
for name, tokens in (("node2", node2), ("node1", node1)):
    if "resolvers" not in tokens or "docker" not in tokens:
        raise SystemExit(f"{name} is missing docker resolver semantics")
    try:
        index = tokens.index("init-addr")
    except ValueError as exc:
        raise SystemExit(f"{name} is missing init-addr") from exc
    if index + 1 >= len(tokens) or tokens[index + 1] != "libc,none":
        raise SystemExit(f"{name} init-addr must include libc,none")
PY
}

validate_runtime_compose() {
  need_file "docker-compose.yml"
  if [[ "$mode" != "live-runtime" ]]; then
    return 0
  fi
  need_grep '^# BDAG_GENERATED_PI5_RUNTIME_COMPOSE=1$' "docker-compose.yml"
  reject_grep '^[[:space:]]*(build|dockerfile):' "docker-compose.yml"
  need_grep 'container_name: pool-db' "docker-compose.yml"
  need_grep 'container_name: bdag-miner-node-2' "docker-compose.yml"
  need_grep 'container_name: rpc-failover' "docker-compose.yml"
  need_grep 'container_name: asic-pool' "docker-compose.yml"
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
need_file "ops/optimization_measurement.py"
need_file "ops/status_sampler.py"
need_file "ops/tests/test_miner_retirement_identity.py"
need_file "ops/tests/test_optimization_measurement.py"
need_file "ops/dashboard.py"
need_file "ops/build-pi5-arm64-release.sh"
need_file "ops/deploy-live-runtime-update.sh"
need_file "ops/release-install.sh"
need_file "ops/compose_migrations.py"
need_file "ops/README.md"
need_file "docs/platform-adaptive-runtime.md"
need_file "docs/t430-single-node-appliance-hardening.md"
need_file "docs/five-asic-template-conversion-guard.html"
need_file "docs/fastsnap-maintenance-resource-guard.html"
need_file "scripts/mining-appliance-preflight.py"
need_file "docker-compose.yml"
need_file "ops/systemd/user-bdag-stack-sentinel.timer"
need_file "ops/systemd/user-bdag-status-sampler.service"
need_file "ops/systemd/user-bdag-node-child-guard.timer"
need_file "ops/systemd/user-bdag-incident-reporter.timer"
need_file "ops/systemd/user-bdag-hourly-snapshot.timer"
need_file "ops/systemd/user-bdag-chain-restore-guard.timer"
need_file "ops/systemd/user-bdag-fastsnap-seed.timer"
need_file "ops/build-fastsnap-seed.sh"
need_file "haproxy.cfg"
need_file "asic-pool/schema.sql"
validate_runtime_compose

if [[ "$mode" == "source" ]]; then
  need_file ".env.cpu.example"
  need_file ".github/workflows/rc-hardening.yml"
  need_file ".github/workflows/build.yml"
  need_file ".github/workflows/build-cpu.yml"
  need_file "scripts/check-doc-consistency.py"
  need_file "scripts/release/installers/install-unix-common.sh"
  need_file "scripts/release/installers/install-windows.ps1"
  need_file "scripts/validate-rc-local.sh"
  need_file "scripts/verify-release-architecture.py"
  need_file "ops/monitor-fastsync-peers.sh"
  need_file "ops/tests/test_no_miner_collect_status.py"
  need_file "ops/tests/test_mining_appliance_preflight.py"
  need_file "ops/tests/test_compose_migrations.py"
  need_file "ops/tests/test_sync_coordinator_fast_catchup.py"
  need_file "ops/tests/test_deployment_portability.py"
  need_file "ops/tests/test_watchdog_miner_source_counts.py"
  need_file "ops/tests/test_earnings_onchain_sources.py"
  need_grep 'copy_source_tree' "scripts/validate-rc-local.sh"
  need_grep 'ls-files.*--cached.*--others.*--exclude-standard' "scripts/validate-rc-local.sh"
fi

if [[ "$mode" == "source" && -e "$root/ops/observability" ]]; then
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
need_grep 'BDAG_SHARED_STATUS_CACHE_ENABLED 1' "ops/install-dashboard.sh"
need_grep 'BDAG_SHARED_STATUS_CACHE_SECONDS 3.0' "ops/install-dashboard.sh"
need_grep 'BDAG_HOST_PROFILE auto' "ops/install-dashboard.sh"
need_grep 'BDAG_ADAPTIVE_CONCURRENCY_ENABLED 1' "ops/install-dashboard.sh"
need_grep 'BDAG_ADAPTIVE_CHAIN_RPC_WARN_MS 1000' "ops/install-dashboard.sh"
need_grep 'BDAG_GLOBAL_RPC_WORKERS 24' "ops/install-dashboard.sh"
need_grep 'BDAG_MINER_SCAN_WORKERS 64' "ops/install-dashboard.sh"
need_grep 'BDAG_MINER_HASHRATE_PROBE_WORKERS 8' "ops/install-dashboard.sh"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_BACKOFF_ENABLED 1' "ops/install-dashboard.sh"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_SYNC_BACKOFF_BLOCKS 0' "ops/install-dashboard.sh"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_IOWAIT_WARN_PERCENT 25' "ops/install-dashboard.sh"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_IO_SOME_AVG10_WARN 20.0' "ops/install-dashboard.sh"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_CPU_SOME_AVG10_WARN 80.0' "ops/install-dashboard.sh"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_CHAIN_RPC_WARN_MS 1000' "ops/install-dashboard.sh"
need_grep 'BDAG_GLOBAL_HISTORY_COMPACT_MULTIPLIER 2' "ops/install-dashboard.sh"
need_grep 'BDAG_ENTRYPOINT_CHOWN_MODE needed' "ops/install-dashboard.sh"
need_grep 'OnCalendar=hourly' "ops/install-dashboard.sh"

need_grep 'run_appliance_preflight' "ops/release-install.sh"
need_grep 'BDAG_APPLIANCE_PREFLIGHT_STRICT' "ops/release-install.sh"
need_grep 'mining-appliance-preflight.py' "ops/release-install.sh"
need_grep 'def check_storage' "scripts/mining-appliance-preflight.py"
need_grep 'single_node_duplicate_data' "scripts/mining-appliance-preflight.py"
need_grep 'chain_data_filesystem' "scripts/mining-appliance-preflight.py"
need_grep 'live_node_child' "scripts/mining-appliance-preflight.py"
need_grep 'Docker root' "scripts/mining-appliance-preflight.py"
need_grep 'block_submissions' "scripts/mining-appliance-preflight.py"

need_grep 'MIN_TRUSTED_HEIGHT' "ops/sync_coordinator.py"
need_grep 'def remembered_highest_block' "ops/sync_coordinator.py"
need_grep 'network_highest = max\(current_network_highest, remembered_highest\)' "ops/sync_coordinator.py"
need_grep 'observed_highest_block' "ops/sync_coordinator.py"
need_grep 'refusing follower seed because leader is not proven near tip' "ops/sync_coordinator.py"
need_grep 'BDAG_SYNC_COORDINATOR_FAR_BEHIND_BLOCKS.*1000' "ops/sync_coordinator.py"
need_grep 'BDAG_SYNC_COORDINATOR_SEED_NEAR_TIP_BLOCKS.*5' "ops/sync_coordinator.py"
need_grep 'BDAG_SYNC_COORDINATOR_LEADER_CPU_SHARES.*8192' "ops/sync_coordinator.py"
need_grep 'BDAG_SYNC_COORDINATOR_FAST_RESTART_COOLDOWN_SECONDS.*900' "ops/sync_coordinator.py"
need_grep 'FAST_CATCHUP_REQUIRED_NODE_FLAG = "--fastartifactsync"' "ops/sync_coordinator.py"
need_grep 'apply_leader_catchup_resources' "ops/sync_coordinator.py"
need_grep 'action = "accelerate_leader_catchup"' "ops/sync_coordinator.py"
need_grep 'maybe_restart_leader_for_fast_sync' "ops/sync_coordinator.py"
need_grep 'action = "clear_pause_state"' "ops/sync_coordinator.py"
need_grep 'resume_allowed_now = safe_int\(decision.get\("target_remaining_blocks"\)' "ops/sync_coordinator.py"
reject_grep 'network_highest = max\(current_network_highest, remembered_highest, highest_height\)' "ops/sync_coordinator.py"

need_grep 'prefer the newest chain data only after the manifest is restore-safe' "ops/latest_chain_candidate.py"
need_grep 'reject unsafe warm copies' "ops/latest_chain_candidate.py"
need_grep 'latest-chain-candidate-state.json' "ops/latest_chain_candidate.py"

need_grep 'def sort_public_peers_by_latency' "ops/update-local-peers.py"
need_grep 'def detect_network_topology' "ops/update-local-peers.py"
need_grep 'def tiered_peer_addresses' "ops/update-local-peers.py"
need_grep 'def public_peer_assignment' "ops/update-local-peers.py"
need_grep 'paused_follower=' "ops/update-local-peers.py"
need_grep 'def active_mining_recreate_guard_reason' "ops/update-local-peers.py"
need_grep 'BDAG_LOCAL_PEERS_DEFER_NODE_RECREATE_WHILE_MINING' "ops/update-local-peers.py"
need_grep 'def parse_args' "ops/sync_coordinator.py"
need_grep 'restart-lagging-follower' "ops/sync_coordinator.py"

need_grep 'automatic clean restore is disabled' "ops/README.md"
need_grep 'preserves current node data' "ops/README.md"

need_grep 'BDAG_FASTSYNC_PREPROCESS_WORKERS=1' ".env.example"
need_grep 'BDAG_FASTSNAP_DIRECTORY_MODE=1' ".env.example"
need_grep 'BDAG_FASTSYNC_ARTIFACT_DIRECTORY=' ".env.example"
need_grep 'BDAG_FASTSYNC_ARTIFACT_MANIFEST=' ".env.example"
need_grep 'BDAG_FASTARTIFACTSYNC_ENABLED=1' ".env.example"
need_grep 'BDAG_SYNC_COORDINATOR_ACCELERATE_FASTSYNC=1' ".env.example"
need_grep 'BDAG_SYNC_COORDINATOR_FAST_RESTART_COOLDOWN_SECONDS=900' ".env.example"
need_grep 'BDAG_FASTSNAP_SEED_TIMER_ENABLED=1' ".env.example"
need_grep 'BDAG_FASTSNAP_MAX_EXPORT_BACKEND_LAG=1000' ".env.example"
need_grep 'BDAG_NODE_CHAIN_RPC_TIMEOUT=8.0' ".env.example"
need_grep 'BDAG_NODE_CHAIN_RPC_RETRIES=2' ".env.example"
need_grep 'BDAG_POOL_RPC_REFUSED_WARN_SECONDS=120' ".env.example"
need_grep 'BDAG_HOST_PRESSURE_IOWAIT_WARN_PERCENT=25' ".env.example"
need_grep 'BDAG_SHARED_STATUS_CACHE_ENABLED=1' ".env.example"
need_grep 'BDAG_SHARED_STATUS_CACHE_SECONDS=3.0' ".env.example"
need_grep 'BDAG_HOST_PROFILE=auto' ".env.example"
need_grep 'BDAG_ADAPTIVE_CONCURRENCY_ENABLED=1' ".env.example"
need_grep 'BDAG_ADAPTIVE_CHAIN_RPC_WARN_MS=1000' ".env.example"
need_grep 'BDAG_STATUS_SAMPLER_ENABLED=1' ".env.example"
need_grep 'BDAG_STATUS_SAMPLER_INTERVAL_SECONDS=10' ".env.example"
need_grep 'BDAG_STATUS_SAMPLER_MAX_AGE_SECONDS=12' ".env.example"
need_grep 'BDAG_GLOBAL_RPC_WORKERS=24' ".env.example"
need_grep 'BDAG_MINER_SCAN_WORKERS=64' ".env.example"
need_grep 'BDAG_MINER_HASHRATE_PROBE_WORKERS=8' ".env.example"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_BACKOFF_ENABLED=1' ".env.example"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_SYNC_BACKOFF_BLOCKS=0' ".env.example"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_IOWAIT_WARN_PERCENT=25' ".env.example"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_IO_SOME_AVG10_WARN=20.0' ".env.example"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_CPU_SOME_AVG10_WARN=80.0' ".env.example"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_CHAIN_RPC_WARN_MS=1000' ".env.example"
need_grep 'BDAG_GLOBAL_HISTORY_COMPACT_MULTIPLIER=2' ".env.example"
need_grep 'BDAG_ENTRYPOINT_CHOWN_MODE=needed' ".env.example"
need_grep 'POOL_RPC_ROUTER_TEMPLATE_LANE_MODE=active-passive' ".env.example"
need_grep 'POOL_RPC_ROUTER_NODE_HEALTH_PROBE_SECONDS=1' ".env.example"
need_grep 'POOL_RPC_ROUTER_NODE_HEALTH_UNREADY_THRESHOLD=1' ".env.example"
need_grep 'POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT=true' ".env.example"
need_grep 'POOL_SUBMIT_STALE_BLOCK_CANDIDATES=false' ".env.example"
need_grep 'POOL_MAX_BLOCK_CANDIDATE_JOB_AGE_MS=2500' ".env.example"
need_grep 'POOL_MAX_BLOCK_CANDIDATE_TEMPLATE_LAG_BLOCKS=16' ".env.example"
need_grep 'POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED=true' ".env.example"
need_grep 'POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD=1' ".env.example"
need_grep 'POOL_STALE_RACE_RECOVERY_COOLDOWN_SECONDS=1' ".env.example"
need_grep 'POOL_ALLOW_MULTIPLE_BLOCK_CANDIDATES_PER_JOB=true' ".env.example"
need_grep 'POOL_PREEMPTIVE_BLOCK_CANDIDATE_REFRESH_ENABLED=true' ".env.example"
need_grep 'NODE_RPC_URLS=http://node:38131' ".env.example"
need_grep 'POOL_SUBMIT_RPC_URLS=' ".env.example"
need_grep 'apply_default_fastsync_flags' "docker/entrypoint-nodeworker.sh"
need_grep 'BDAG_FASTARTIFACTSYNC_ENABLED' "docker/entrypoint-nodeworker.sh"
need_grep 'fastsnap_supports_directory_mode' "docker/entrypoint-nodeworker.sh"
need_grep 'apply_default_fastsync_flags' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_FASTARTIFACTSYNC_ENABLED' "ops/build-pi5-arm64-release.sh"
need_grep 'fastsnap_supports_directory_mode' "ops/build-pi5-arm64-release.sh"
if [[ "$mode" == "live-runtime" ]]; then
  need_grep 'NODE_RPC_URLS: .*http://(node|rpc-failover):38131' "docker-compose.yml"
else
  need_grep 'NODE_RPC_URLS: .*http://node:38131' "docker-compose.yml"
  need_grep 'POOL_SUBMIT_RPC_URLS: .*POOL_SUBMIT_RPC_URLS' "docker-compose.yml"
fi
need_grep 'POOL_SUBMIT_RPC_URLS: .*POOL_SUBMIT_RPC_URLS' "ops/build-pi5-arm64-release.sh"
need_grep 'POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT: .*POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT' "docker-compose.yml"
need_grep 'POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT: .*POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT' "ops/build-pi5-arm64-release.sh"
need_grep 'POOL_SUBMIT_STALE_BLOCK_CANDIDATES: .*POOL_SUBMIT_STALE_BLOCK_CANDIDATES' "docker-compose.yml"
need_grep 'POOL_MAX_BLOCK_CANDIDATE_TEMPLATE_LAG_BLOCKS: .*POOL_MAX_BLOCK_CANDIDATE_TEMPLATE_LAG_BLOCKS' "docker-compose.yml"
need_grep 'POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED: .*POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED' "docker-compose.yml"
need_grep 'POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD: .*POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD' "docker-compose.yml"
need_grep 'POOL_SUBMIT_STALE_BLOCK_CANDIDATES=false' "ops/build-pi5-arm64-release.sh"
need_grep 'POOL_MAX_BLOCK_CANDIDATE_JOB_AGE_MS=2500' "ops/build-pi5-arm64-release.sh"
need_grep 'POOL_MAX_BLOCK_CANDIDATE_TEMPLATE_LAG_BLOCKS=16' "ops/build-pi5-arm64-release.sh"
need_grep 'POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED=true' "ops/build-pi5-arm64-release.sh"
need_grep 'POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD=1' "ops/build-pi5-arm64-release.sh"
need_grep 'POOL_STALE_RACE_RECOVERY_COOLDOWN_SECONDS=1' "ops/build-pi5-arm64-release.sh"
need_grep 'POOL_ALLOW_MULTIPLE_BLOCK_CANDIDATES_PER_JOB=true' "ops/build-pi5-arm64-release.sh"
need_grep 'POOL_PREEMPTIVE_BLOCK_CANDIDATE_REFRESH_ENABLED=true' "ops/build-pi5-arm64-release.sh"
need_grep 'METRICS_ADDR: .*0.0.0.0:9090' "ops/build-pi5-arm64-release.sh"
need_grep 'METRICS_ADDR=0.0.0.0:9090' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_STACK_SERVICES=pool-db,bdag-miner-node-2,rpc-failover,asic-pool' ".env.example"
need_grep 'OnCalendar=hourly' "ops/systemd/user-bdag-hourly-snapshot.timer"
need_grep 'RandomizedDelaySec=10s' "ops/systemd/user-bdag-node-child-guard.timer"
need_grep 'RandomizedDelaySec=20s' "ops/systemd/user-bdag-sync-coordinator.timer"
need_grep 'RandomizedDelaySec=2m' "ops/systemd/user-bdag-incident-reporter.timer"
need_grep 'RandomizedDelaySec=10s' "host/mining-appliance/bdag-runtime-priority.timer"

need_grep '"status_version": 2' "ops/pool_ops.py"
need_grep 'blockdag-node' "ops/pool_ops.py"
need_grep 'urllib.request.Request' "ops/pool_ops.py"
reject_grep 'command = \["curl"' "ops/pool_ops.py"
need_grep '"mode": mode' "ops/pool_ops.py"
need_grep '"can_mine": can_mine' "ops/pool_ops.py"
need_grep '"can_accept_shares": can_accept_shares' "ops/pool_ops.py"
need_grep '"can_submit_blocks": can_submit_blocks' "ops/pool_ops.py"
need_grep '"truth_sources": truth_sources' "ops/pool_ops.py"
need_grep '"chain_block_count": "eth_blockNumber/getBlockCount fallback"' "ops/pool_ops.py"
need_grep 'MINER_RETIREMENTS_FILE' "ops/pool_ops.py"
need_grep 'def retired_miner_identity_decision' "ops/pool_ops.py"
need_grep 'MAC is the only permanent ASIC identity' "ops/pool_ops.py"
need_grep 'matched_by": "ip-observation"' "ops/pool_ops.py"
need_grep 'only MAC address can retire an ASIC' "ops/pool_ops.py"
need_grep 'def miner_health_count_summary' "ops/pool_ops.py"
need_grep 'managed_ok_count' "ops/pool_ops.py"
need_grep 'def build_pool_efficiency_loss_ledger' "ops/pool_ops.py"
need_grep 'def selected_backend_readiness_contract' "ops/pool_ops.py"
need_grep 'def read_status_sampler_payload' "ops/pool_ops.py"
need_grep 'def write_status_sampler_payload' "ops/pool_ops.py"
need_grep 'POOL_ACTIVITY_BOOTSTRAP_LOG_LINES' "ops/pool_ops.py"
need_grep 'def note_legacy_extranonce_client' "ops/pool_ops.py"
need_grep 'last_pool_job_extranonces' "ops/pool_ops.py"
need_grep 'global_evm_rpc_urls\(\)' "ops/pool_ops.py"
need_grep '"source_truth": "on-chain balance window reconciled with native transfers"' "ops/pool_ops.py"
need_grep '"fallback_used": False' "ops/pool_ops.py"
reject_grep '"source": onchain_24h.get\("source"\) if onchain_24h.get\("status"\) == "ok" else "pool-db-credits-24h"' "ops/pool_ops.py"
need_grep 'STATUS_SAMPLER_FILE' "ops/status_sampler.py"
need_grep '"identity_basis": "mac"' "ops/pool_ops.py"
need_grep 'pool efficiency loss ledger' "ops/pool_ops.py"
need_grep 'lossLedgerStatusText' "ops/dashboard.py"
reject_grep 'matched_by": "worker"[,}]' "ops/pool_ops.py"
reject_grep 'matched_by": "ip"[,}]' "ops/pool_ops.py"
need_grep 'except OSError:' "ops/dashboard.py"

need_grep 'def node_chain_rpc_snapshot' "ops/pool_ops.py"
need_grep 'BDAG_NODE_CHAIN_RPC_TIMEOUT' "ops/pool_ops.py"
need_grep 'BDAG_NODE_CHAIN_RPC_RETRIES' "ops/pool_ops.py"
need_grep 'BDAG_POOL_RPC_REFUSED_WARN_SECONDS' "ops/pool_ops.py"
need_grep 'def is_no_miner_sync_noise' "ops/pool_ops.py"
need_grep 'miner_demand_present' "ops/pool_ops.py"
need_grep 'suppressed_for_no_miners' "ops/pool_ops.py"
need_grep 'def collect_status_cached' "ops/pool_ops.py"
need_grep 'SHARED_STATUS_CACHE_FILE' "ops/pool_ops.py"
need_grep 'collect_status_cached' "ops/watchdog.py"
need_grep 'collect_status_cached' "ops/sync_coordinator.py"
need_grep 'collect_status_cached' "ops/p2p_guard.py"
need_grep 'collect_status_cached' "ops/dashboard.py"
need_grep 'def collect_host_pressure' "ops/pool_ops.py"
need_grep 'iowait_percent' "ops/pool_ops.py"
need_grep 'host_pressure_warning_messages' "ops/pool_ops.py"
need_grep 'iowait_warning_active' "ops/pool_ops.py"
need_grep 'def host_runtime_profile' "ops/pool_ops.py"
need_grep 'def adaptive_worker_count' "ops/pool_ops.py"
need_grep 'adaptive_concurrency' "ops/pool_ops.py"
need_grep 'host_profile' "ops/pool_ops.py"
need_grep 'chain_rpc_latency_ms' "ops/pool_ops.py"
need_grep 'def background_maintenance_decision' "ops/pool_ops.py"
need_grep 'maintenance_deferred' "ops/pool_ops.py"
need_grep 'global blockchain scan deferred' "ops/pool_ops.py"
need_grep 'background maintenance backoff active' "ops/hourly-chain-snapshot.sh"
need_grep 'background maintenance backoff active' "ops/build-fastsnap-seed.sh"
need_grep 'iowait=' "ops/dashboard.py"
need_grep 'dashboard_url' "ops/dashboard.py"
need_grep 'getBlockCount' "ops/pool_ops.py"
need_grep 'eth_blockNumber' "ops/pool_ops.py"
need_grep 'getMainChainHeight' "ops/pool_ops.py"
need_grep 'chain_block_count' "ops/pool_ops.py"
need_grep 'current_block_source": chain.get\("chain_rpc_source"\)' "ops/pool_ops.py"
need_grep 'nodeBlockHeight' "ops/dashboard.py"
need_grep 'firstPresent\(syncNode\?\.chain_block_count, node\?\.chain_block_count, null\)' "ops/dashboard.py"
need_grep 'def append_jsonl_file' "ops/pool_ops.py"
need_grep 'GLOBAL_HISTORY_COMPACT_MULTIPLIER' "ops/pool_ops.py"
need_grep 'append_jsonl_file\(GLOBAL_HISTORY_FILE' "ops/pool_ops.py"
need_grep 'BDAG_ENTRYPOINT_CHOWN_MODE' "docker/entrypoint-nodeworker.sh"
need_grep 'FASTSNAP_BOOTSTRAP_MUTATED' "docker/entrypoint-nodeworker.sh"
need_grep 'dir-out' "docker/entrypoint-nodeworker.sh"
need_grep 'install-dir' "docker/entrypoint-nodeworker.sh"
need_grep 'configure_directory_artifact_serving' "docker/entrypoint-nodeworker.sh"
need_grep 'using archive/legacy serving fallback' "docker/entrypoint-nodeworker.sh"
need_grep 'using archive/legacy serving fallback' "ops/build-pi5-arm64-release.sh"
need_grep 'fix_ownership_if_needed' "docker/entrypoint-nodeworker.sh"
need_grep 'find "\$path" .* -print -quit' "docker/entrypoint-nodeworker.sh"
need_grep 'fcntl.LOCK_EX \| fcntl.LOCK_NB' "ops/stack_sentinel.py"
need_grep 'def acquire_run_lock' "ops/stack_sentinel.py"
need_grep '"--no-build", "--pull", "never"' "ops/stack_sentinel.py"

need_grep 'BDAG_NODE_MODE=single' "ops/build-pi5-arm64-release.sh"
need_grep 'COMPOSE_PROFILES dual-node' "ops/release-install.sh"
need_grep 'profiles:' "ops/build-pi5-arm64-release.sh"
need_grep 'dual-node' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_NODE_MINING_ARGS' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_ENABLE_NODE_MINING=0' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_NODE_CHAIN_RPC_TIMEOUT=8.0' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_NODE_CHAIN_RPC_RETRIES=2' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_POOL_RPC_REFUSED_WARN_SECONDS=120' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_HOST_PRESSURE_IOWAIT_WARN_PERCENT=25' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_SHARED_STATUS_CACHE_ENABLED=1' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_SHARED_STATUS_CACHE_SECONDS=3.0' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_HOST_PROFILE=auto' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_ADAPTIVE_CONCURRENCY_ENABLED=1' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_ADAPTIVE_CHAIN_RPC_WARN_MS=1000' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_GLOBAL_RPC_WORKERS=24' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_MINER_SCAN_WORKERS=64' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_MINER_HASHRATE_PROBE_WORKERS=8' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_BACKOFF_ENABLED=1' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_SYNC_BACKOFF_BLOCKS=0' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_IOWAIT_WARN_PERCENT=25' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_IO_SOME_AVG10_WARN=20.0' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_CPU_SOME_AVG10_WARN=80.0' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_BACKGROUND_MAINTENANCE_CHAIN_RPC_WARN_MS=1000' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_GLOBAL_HISTORY_COMPACT_MULTIPLIER=2' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_ENTRYPOINT_CHOWN_MODE=needed' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_FASTSYNC_PREPROCESS_WORKERS=1' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_NETWORK_TOPOLOGY=auto' ".env.example"
need_grep 'BDAG_ASIC_LAN_CIDRS=192.168.50.0/24' ".env.example"
need_grep 'BDAG_ALLOW_ASIC_LAN_P2P=0' ".env.example"
need_grep 'BDAG_FASTSYNC_PEER_ORDERING=tiered-latency' ".env.example"
need_grep 'BDAG_FASTSYNC_PEER_ORDERING=tiered-latency' "ops/build-pi5-arm64-release.sh"
need_grep 'BDAG_FASTSYNC_LAN_PEERS' "ops/build-pi5-arm64-release.sh"
need_grep 'tiered latency FastSync candidates enabled' "ops/build-pi5-arm64-release.sh"
need_grep 'tiered latency FastSync candidates enabled' "docker/entrypoint-nodeworker.sh"
need_grep 'single-node-asic-router' "ops/update-local-peers.py"
need_grep 'BDAG_ALLOW_ASIC_LAN_P2P' "docker/entrypoint-nodeworker.sh"
if [[ "$mode" == "source" ]]; then
  need_grep 'BDAG_FASTSYNC_PEER_ORDERING=tiered-latency' ".env.cpu.example"
fi
need_grep 'BDAG_FASTSNAP_DOCKER_CPUS:-1.5' "ops/build-fastsnap-seed.sh"
need_grep 'five-asic-template-conversion-guard.html' "AGENTS.md"
need_grep 'five-asic-template-conversion-guard.html' "README.md"
need_grep 'fastsnap-maintenance-resource-guard.html' "README.md"
need_grep 'BDAG_GENERATED_PI5_RUNTIME_COMPOSE=1' "ops/build-pi5-arm64-release.sh"
need_grep 'guard_release_compose' "ops/build-pi5-arm64-release.sh"
need_grep 'deploy-live-runtime-update.sh --target' "README.md"
need_grep 'preflight_copy_contract' "ops/deploy-live-runtime-update.sh"
need_grep 'runtime_compose_guard' "ops/deploy-live-runtime-update.sh"
need_grep 'rollback_from_backup' "ops/deploy-live-runtime-update.sh"
need_grep 'post_deploy_health_check' "ops/deploy-live-runtime-update.sh"
need_grep 'dashboard_api_ready' "ops/deploy-live-runtime-update.sh"
need_grep 'critical_containers_ready' "ops/deploy-live-runtime-update.sh"
need_grep 'watchdog_state_fresh' "ops/deploy-live-runtime-update.sh"
need_grep 'guard_runtime_compose' "ops/release-install.sh"
need_grep 'compose_cmd up -d --no-build --pull never' "ops/release-install.sh"
need_grep 'Fresh installs assume zero miner sources' "AGENTS.md"
need_grep 'Fresh installs assume zero miner sources' "README.md"
need_grep 'configure discovered miner sources now\?" "n"' "ops/release-install.sh"
need_grep 'expected_lane_count == 0 or imbalanced_count == 0' "ops/watchdog.py"
need_grep 'no-window-work' "ops/watchdog.py"
need_grep 'active miner source\(s\) are connected/submitting' "ops/watchdog.py"
need_grep 'verify-release-architecture.py' "ops/build-pi5-arm64-release.sh"
if [[ "$mode" == "source" ]]; then
  need_grep 'BDAG_RESET_NODE_DATA="\${BDAG_RESET_NODE_DATA:-0}"' "scripts/release/installers/install-unix-common.sh"
  need_grep 'BDAG_RESET_NODE_DATA -eq' "scripts/release/installers/install-windows.ps1"
  need_grep 'run_release_preflight' "scripts/release/installers/install-unix-common.sh"
  need_grep 'Invoke-ReleasePreflight' "scripts/release/installers/install-windows.ps1"
  need_grep 'BDAG_CLEAN_ORPHAN_CONTAINERS' "scripts/release/installers/install-unix-common.sh"
  need_grep 'BDAG_CLEAN_ORPHAN_CONTAINERS' "scripts/release/installers/install-windows.ps1"
  need_grep 'check-release-archive.py' ".github/workflows/build.yml"
  need_grep 'check-release-archive.py' ".github/workflows/build-cpu.yml"
  need_grep 'GOFLAGS: "-buildvcs=false"' ".github/workflows/build.yml"
  need_grep 'GOFLAGS: "-buildvcs=false"' ".github/workflows/build-cpu.yml"
  need_grep 'e10dd5d8fb57aefa73a682ff69c94eac5c2b50f4' ".github/workflows/build.yml"
  need_grep '61b231c0501b32338f4ad47561a09e03e5933adc' ".github/workflows/build.yml"
  need_grep 'if ! command -v jq' "ops/monitor-fastsync-peers.sh"
fi

if [[ "$mode" == "source" ]]; then
  need_grep 'validate-pi5-restart-hardening.sh --mode source' ".github/workflows/rc-hardening.yml"
  need_grep 'check-doc-consistency.py' ".github/workflows/rc-hardening.yml"
  python3 "$root/scripts/check-doc-consistency.py"
fi

need_grep 'stack-sentinel.timer' "ops/install-dashboard.sh"
need_grep 'stack_sentinel.py' "ops/install-dashboard.sh"
need_grep 'chain_restore_guard.py' "ops/install-dashboard.sh"
need_grep 'update-local-peers.py --apply' "ops/install-dashboard.sh"
need_grep 'pool-db,bdag-miner-node-2,rpc-failover,asic-pool' "ops/install-dashboard.sh"

validate_haproxy_semantics

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

if [[ "$mode" == "source" && ( -e "$root/ops/runtime" || -n "$(find "$root/ops" \( -path '*/__pycache__*' -o -name '*.pyc' \) -print -quit)" ) ]]; then
  fail "ops bundle contains runtime state or Python bytecode"
fi

printf 'restart hardening validation passed for %s (%s mode)\n' "$root" "$mode"
