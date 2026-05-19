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
need_file "ops/README.md"

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

need_grep 'MIN_TRUSTED_HEIGHT' "ops/sync_coordinator.py"
need_grep 'def remembered_highest_block' "ops/sync_coordinator.py"
need_grep 'network_highest = max\(current_network_highest, remembered_highest\)' "ops/sync_coordinator.py"
need_grep 'observed_highest_block' "ops/sync_coordinator.py"
need_grep 'refusing follower seed because leader is not proven near tip' "ops/sync_coordinator.py"
reject_grep 'network_highest = max\(current_network_highest, remembered_highest, highest_height\)' "ops/sync_coordinator.py"

need_grep 'prefer the newest chain data only after the manifest is restore-safe' "ops/latest_chain_candidate.py"
need_grep 'reject unsafe warm copies' "ops/latest_chain_candidate.py"
need_grep 'latest-chain-candidate-state.json' "ops/latest_chain_candidate.py"

need_grep 'def sort_public_peers_by_latency' "ops/update-local-peers.py"
need_grep 'def public_peer_assignment' "ops/update-local-peers.py"
need_grep 'paused_follower=' "ops/update-local-peers.py"

need_grep 'automatic clean restore is disabled' "ops/README.md"
need_grep 'preserves current node data' "ops/README.md"

printf 'restart hardening validation passed for %s\n' "$root"
