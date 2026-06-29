#!/usr/bin/env bash

VQUEEN_LOG_LEVEL_WIDTH="${VQUEEN_LOG_LEVEL_WIDTH:-8}"
VQUEEN_LOG_THREAD_WIDTH="${VQUEEN_LOG_THREAD_WIDTH:-22}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"
LOG_TO_SYSLOG="${LOG_TO_SYSLOG:-1}"
LOG_SYSLOG_TAG="${LOG_SYSLOG_TAG:-vqueen-nearhot-backup}"

log_level_value() {
  case "$1" in
    TRACE) printf '0\n' ;;
    DEBUG) printf '10\n' ;;
    INFO) printf '20\n' ;;
    NOTICE) printf '30\n' ;;
    WARNING) printf '40\n' ;;
    ERROR) printf '50\n' ;;
    CRITICAL) printf '60\n' ;;
    FATAL) printf '70\n' ;;
    *) return 2 ;;
  esac
}

log_syslog_priority() {
  case "$1" in
    TRACE|DEBUG) printf 'user.debug\n' ;;
    INFO) printf 'user.info\n' ;;
    NOTICE) printf 'user.notice\n' ;;
    WARNING) printf 'user.warning\n' ;;
    ERROR) printf 'user.err\n' ;;
    CRITICAL) printf 'user.crit\n' ;;
    FATAL) printf 'user.alert\n' ;;
    *) printf 'user.info\n' ;;
  esac
}

normalize_log_level() {
  case "$1" in
    TRACE|DEBUG|INFO|NOTICE|WARNING|ERROR|CRITICAL|FATAL) printf '%s\n' "$1" ;;
    *)
      printf 'invalid log level: %s\n' "$1" >&2
      return 2
      ;;
  esac
}

should_log_level() {
  local level="$1"
  local current="${LOG_LEVEL:-INFO}"
  local level_value current_value

  level_value="$(log_level_value "$level")" || return 2
  current="$(normalize_log_level "$current")" || return 2
  current_value="$(log_level_value "$current")" || return 2
  [ "$level_value" -ge "$current_value" ]
}

log_emit() {
  local level thread msg timestamp line pid priority
  level="$(normalize_log_level "$1")" || return 2
  thread="${2:-MainThread}"
  msg="$3"
  should_log_level "$level" || return 0
  timestamp="$(date '+%Y-%m-%d %H:%M:%S')"
  pid="${BASHPID:-$$}"
  printf -v line "%s: %-*s : %s %-*s : %s" \
    "$timestamp" "$VQUEEN_LOG_LEVEL_WIDTH" "$level" "$pid" "$VQUEEN_LOG_THREAD_WIDTH" "$thread" "$msg"
  printf '%s\n' "$line" >&2
  if [ "${LOG_TO_SYSLOG:-1}" = "1" ] && command -v logger >/dev/null 2>&1; then
    priority="$(log_syslog_priority "$level")"
    logger -t "$LOG_SYSLOG_TAG" -p "$priority" -- "$line"
  fi
}

log_trace() { log_emit TRACE "${1:-MainThread}" "${2:-}"; }
log_debug() { log_emit DEBUG "${1:-MainThread}" "${2:-}"; }
log_info() { log_emit INFO "${1:-MainThread}" "${2:-}"; }
log_notice() { log_emit NOTICE "${1:-MainThread}" "${2:-}"; }
log_warning() { log_emit WARNING "${1:-MainThread}" "${2:-}"; }
log_error() { log_emit ERROR "${1:-MainThread}" "${2:-}"; }
log_critical() { log_emit CRITICAL "${1:-MainThread}" "${2:-}"; }
log_fatal() { log_emit FATAL "${1:-MainThread}" "${2:-}"; }

log_line() {
  local level="$1"
  local thread="$2"
  local msg="$3"
  log_emit "$level" "$thread" "$msg"
}
