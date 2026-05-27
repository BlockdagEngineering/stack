#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${BDAG_ENV_FILE:-$PROJECT_ROOT/.env}"
COMPOSE_FILE="${BDAG_COMPOSE_FILE:-$PROJECT_ROOT/docker-compose.yml}"
RUNTIME_DIR="${BDAG_RUNTIME_DIR:-$PROJECT_ROOT/ops/runtime}"
LOG_FILE="${BDAG_SELF_HEAL_LOG:-$RUNTIME_DIR/logs/portable-self-heal.log}"
INTERVAL="${BDAG_SELF_HEAL_INTERVAL:-60}"
STATE_FILE="${BDAG_SYNC_COORDINATOR_STATE_FILE:-$RUNTIME_DIR/sync-coordinator-state.json}"
LOCK_DIR="$RUNTIME_DIR/portable-self-heal.lock"

usage() {
  cat <<'USAGE'
Usage: ops/portable-self-heal.sh [--once|--loop]

Portable host-level self-healing for release installs. This portable self-healing
fallback is intentionally
conservative: it only asks Docker Compose to bring configured services back up
and never deletes volumes, restores snapshots, or rewrites chain data.
USAGE
}

mode="once"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --once)
      mode="once"
      shift
      ;;
    --loop)
      mode="loop"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

mkdir -p "$RUNTIME_DIR/logs"

log() {
  printf '[%s] portable-self-heal: %s\n' "$(date -Is)" "$*" | tee -a "$LOG_FILE" >&2
}

env_value() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 1
  awk -F= -v key="$key" '
    $1 == key {
      value = $0
      sub("^[^=]*=", "", value)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      gsub(/^["'\'']|["'\'']$/, "", value)
      print value
      exit
    }
  ' "$ENV_FILE"
}

compose() {
  local args=(compose)
  if [[ -f "$ENV_FILE" ]]; then
    args+=(--env-file "$ENV_FILE")
  fi
  args+=(-f "$COMPOSE_FILE")
  docker "${args[@]}" "$@"
}

available_services() {
  compose config --services 2>/dev/null | sed '/^[[:space:]]*$/d' || true
}

configured_service_csv() {
  if [[ -n "${BDAG_SELF_HEAL_SERVICES:-}" ]]; then
    printf '%s\n' "$BDAG_SELF_HEAL_SERVICES"
    return 0
  fi
  if [[ -n "${BDAG_STACK_SERVICES:-}" ]]; then
    printf '%s\n' "$BDAG_STACK_SERVICES"
    return 0
  fi
  env_value BDAG_SELF_HEAL_SERVICES || env_value BDAG_STACK_SERVICES || true
}

planned_paused_follower() {
  [[ -f "$STATE_FILE" ]] || return 0
  grep -Eq '"mode"[[:space:]]*:[[:space:]]*"leader_catchup"' "$STATE_FILE" || return 0
  sed -n 's/.*"paused_follower"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$STATE_FILE" | head -n 1
}

service_list() {
  local available configured paused item
  available="$(available_services)"
  configured="$(configured_service_csv)"
  paused="$(planned_paused_follower)"

  if [[ -z "$available" ]]; then
    return 0
  fi

  if [[ -z "$configured" ]]; then
    configured="$(printf '%s\n' "$available" | paste -sd, -)"
  fi

  local selected=()
  local old_ifs="$IFS"
  IFS=', '
  for item in $configured; do
    [[ -n "$item" ]] || continue
    [[ "$item" == "$paused" ]] && continue
    if printf '%s\n' "$available" | grep -Fxq "$item"; then
      selected+=("$item")
    fi
  done
  IFS="$old_ifs"

  if (( ${#selected[@]} == 0 )); then
    while IFS= read -r item; do
      [[ -n "$item" && "$item" != "$paused" ]] && selected+=("$item")
    done <<<"$available"
  fi

  printf '%s\n' "${selected[@]}"
}

run_once() {
  if ! docker info >/dev/null 2>&1; then
    log "Docker is not reachable; scheduler will retry"
    return 0
  fi
  if [[ ! -f "$COMPOSE_FILE" ]]; then
    log "compose file missing: $COMPOSE_FILE"
    return 0
  fi

  mapfile -t services < <(service_list)
  if (( ${#services[@]} == 0 )); then
    log "no compose services resolved; skipping"
    return 0
  fi

  log "ensuring compose services are up: ${services[*]}"
  if compose up -d "${services[@]}" >>"$LOG_FILE" 2>&1; then
    log "compose self-heal pass completed"
  else
    log "compose self-heal pass failed; see log above"
  fi
}

if mkdir "$LOCK_DIR" 2>/dev/null; then
  trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
else
  log "another portable self-heal worker is already running"
  exit 0
fi

log "started mode=$mode project=$PROJECT_ROOT"
run_once

if [[ "$mode" == "loop" ]]; then
  while true; do
    sleep "$INTERVAL"
    run_once
  done
fi
