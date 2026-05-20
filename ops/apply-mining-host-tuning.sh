#!/usr/bin/env bash
set -eu

# Passive mining-host tuning. This is intentionally safe to reapply: it adjusts
# block-device queue/read-ahead, Docker weights, and process scheduler hints
# without changing chain data, node topology, ASIC configuration, or service
# state.

ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
read_ahead_kb="${BDAG_BLOCK_READ_AHEAD_KB:-1024}"
nr_requests="${BDAG_BLOCK_NR_REQUESTS:-256}"
critical_nice="${BDAG_MINING_CRITICAL_NICE:--5}"
desktop_nice="${BDAG_DESKTOP_BACKGROUND_NICE:-19}"

log() {
  printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"
}

block_device_for_path() {
  source="$(findmnt -no SOURCE -T "$1" 2>/dev/null || true)"
  [ -n "$source" ] || return 0
  name="$(lsblk -no PKNAME "$source" 2>/dev/null | head -n1 || true)"
  if [ -z "$name" ]; then
    name="$(basename "$source" | sed -E 's/p?[0-9]+$//')"
  fi
  [ -n "$name" ] && printf '%s\n' "$name"
}

tune_block_device() {
  queue="/sys/block/$1/queue"
  [ -d "$queue" ] || return 0
  [ -w "$queue/read_ahead_kb" ] && printf '%s\n' "$read_ahead_kb" > "$queue/read_ahead_kb" || true
  [ -w "$queue/nr_requests" ] && printf '%s\n' "$nr_requests" > "$queue/nr_requests" || true
  log "block_device=$1 read_ahead_kb=$(cat "$queue/read_ahead_kb" 2>/dev/null || echo unknown) nr_requests=$(cat "$queue/nr_requests" 2>/dev/null || echo unknown)"
}

renice_pids() {
  value="$1"
  shift
  for pid in "$@"; do
    [ -n "$pid" ] && renice -n "$value" -p "$pid" >/dev/null 2>&1 || true
  done
}

ionice_pids() {
  class="$1"
  priority="$2"
  shift 2
  command -v ionice >/dev/null 2>&1 || return 0
  for pid in "$@"; do
    [ -n "$pid" ] && ionice -c "$class" -n "$priority" -p "$pid" >/dev/null 2>&1 || true
  done
}

tune_processes() {
  critical_pids="$(pgrep -x bdag 2>/dev/null; pgrep -x nodeworker 2>/dev/null; pgrep -x asic-pool 2>/dev/null; pgrep -x postgres 2>/dev/null || true)"
  critical_pids="$(printf '%s\n' "$critical_pids" | awk 'NF' | sort -u)"
  if [ -n "$critical_pids" ]; then
    # shellcheck disable=SC2086
    renice_pids "$critical_nice" $critical_pids
    # shellcheck disable=SC2086
    ionice_pids 2 0 $critical_pids
  fi

  if [ "${BDAG_TUNE_DESKTOP_BACKGROUND:-1}" = "1" ]; then
    desktop_pids="$(pgrep -f '(/firefox|/chrome|/chromium|/code|Web Content|Socket Process|Utility Process)' 2>/dev/null || true)"
    if [ -n "$desktop_pids" ]; then
      # shellcheck disable=SC2086
      renice_pids "$desktop_nice" $desktop_pids
      # shellcheck disable=SC2086
      ionice_pids 3 7 $desktop_pids
    fi
  fi
}

tune_docker_weights() {
  command -v docker >/dev/null 2>&1 || return 0
  docker info >/dev/null 2>&1 || return 0
  docker update --cpu-shares 4096 --blkio-weight 1000 bdag-miner-node-1 bdag-miner-node-2 node >/dev/null 2>&1 || true
  docker update --cpu-shares 3072 --blkio-weight 900 asic-pool pool pool-db postgres >/dev/null 2>&1 || true
  docker update --cpu-shares 2048 --blkio-weight 800 rpc-failover >/dev/null 2>&1 || true
}

devices="$(
  {
    block_device_for_path "$ROOT"
    block_device_for_path /
  } | awk 'NF' | sort -u
)"
for dev in $devices; do
  tune_block_device "$dev"
done
tune_docker_weights
tune_processes
