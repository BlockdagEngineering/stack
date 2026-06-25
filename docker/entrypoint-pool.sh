#!/usr/bin/env sh
set -eu

log() {
  printf '[%s] pool-entrypoint: %s\n' "$(date -Iseconds 2>/dev/null || date)" "$*" >&2
}

json_string_value() {
  key="$1"
  file="$2"
  sed -n "s/.*\"$key\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" "$file" | head -n 1
}

controller_allows_actor() {
  controller="$1"
  actor="$2"
  normalized="$(printf '%s' "$controller" | tr ';' ',' | tr -d '[:space:]')"
  case ",$normalized," in
    *,any,*|*,all,*|*,\*,*) return 0 ;;
    *,"$actor",*) return 0 ;;
  esac
  return 1
}

if [ "${BDAG_POOL_START_GUARD_ENABLED:-1}" = "1" ]; then
  control_file="${BDAG_AUTOMATION_CONTROL_FILE:-/var/lib/bdagStack/runtime/automation-control.json}"
  lease_file="${BDAG_POOL_START_LEASE_FILE:-/var/lib/bdagStack/runtime/pool-start-lease.env}"
  max_age="${BDAG_POOL_START_LEASE_MAX_AGE_SECONDS:-120}"

  if [ ! -r "$control_file" ]; then
    log "refusing to start mining pool: automation control file is missing or unreadable at $control_file"
    exit 78
  fi
  state="$(json_string_value state "$control_file")"
  controller="$(json_string_value high_risk_controller "$control_file")"
  controller="${controller:-watchdog}"
  if [ "$state" != "normal" ]; then
    log "refusing to start mining pool: automation control state is $state"
    exit 78
  fi

  if [ ! -r "$lease_file" ]; then
    log "refusing to start mining pool: start lease is missing or unreadable at $lease_file"
    exit 78
  fi
  # shellcheck disable=SC1090
  . "$lease_file"
  now="$(date +%s)"
  lease_epoch="${BDAG_POOL_START_LEASE_EPOCH:-0}"
  lease_expires="${BDAG_POOL_START_LEASE_EXPIRES:-0}"
  lease_actor="${BDAG_POOL_START_LEASE_ACTOR:-}"
  case "$lease_epoch:$lease_expires" in
    *[!0-9:]*|:|0:*) log "refusing to start mining pool: start lease has invalid timestamps"; exit 78 ;;
  esac
  if [ "$lease_expires" -lt "$now" ]; then
    log "refusing to start mining pool: start lease expired at $lease_expires"
    exit 78
  fi
  if [ $((now - lease_epoch)) -gt "$max_age" ]; then
    log "refusing to start mining pool: start lease is older than ${max_age}s"
    exit 78
  fi
  if ! controller_allows_actor "$controller" "$lease_actor"; then
    log "refusing to start mining pool: lease actor $lease_actor is not controller $controller"
    exit 78
  fi
  log "start lease accepted actor=$lease_actor controller=$controller"
fi

exec "$@"
