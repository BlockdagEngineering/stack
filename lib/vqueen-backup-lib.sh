#!/usr/bin/env bash
set -Eeuo pipefail

VQUEEN_LIB_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=./vqueen-logging.sh
. "${VQUEEN_LIB_DIR}/vqueen-logging.sh"

die() {
  log_error "${LOG_THREAD:-MainThread}" "$*"
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

warn() {
  log_warning "${LOG_THREAD:-MainThread}" "$*"
  printf 'WARNING: %s\n' "$*" >&2
}

info() {
  log_info "${LOG_THREAD:-MainThread}" "$*"
  printf 'INFO: %s\n' "$*"
}

script_dir() {
  local src
  src="${BASH_SOURCE[0]}"
  while [ -L "$src" ]; do
    src="$(readlink "$src")"
  done
  cd -- "$(dirname -- "$src")" && pwd -P
}

project_root_from_lib() {
  local lib_dir
  lib_dir="$(script_dir)"
  cd -- "${lib_dir}/.." && pwd -P
}

require_tool() {
  command -v "$1" >/dev/null 2>&1 || die "required tool missing: $1"
}

canonical_path() {
  local path="$1"
  [ -n "$path" ] || die "empty path cannot be canonicalized"
  realpath -m -- "$path"
}

lexical_absolute_path() {
  local path="$1"
  local -a raw_parts clean_parts
  local part

  [ -n "$path" ] || die "empty path cannot be normalized"
  case "$path" in
    /*) ;;
    *) path="${PWD}/${path}" ;;
  esac

  IFS='/' read -r -a raw_parts <<< "$path"
  clean_parts=()
  for part in "${raw_parts[@]}"; do
    case "$part" in
      ""|.) ;;
      ..)
        if [ "${#clean_parts[@]}" -gt 0 ]; then
          clean_parts=("${clean_parts[@]:0:$((${#clean_parts[@]} - 1))}")
        fi
        ;;
      *) clean_parts+=("$part") ;;
    esac
  done

  if [ "${#clean_parts[@]}" -eq 0 ]; then
    printf '/\n'
    return 0
  fi

  printf '/%s' "${clean_parts[0]}"
  for part in "${clean_parts[@]:1}"; do
    printf '/%s' "$part"
  done
  printf '\n'
}

refuse_root_or_empty() {
  local path="${1:-}"
  [ -n "$path" ] || die "empty path refused"
  [ "$path" != "/" ] || die "root path refused"
}

require_under_root() {
  local candidate root c r
  candidate="$(canonical_path "$1")"
  root="$(canonical_path "$2")"
  refuse_root_or_empty "$candidate"
  refuse_root_or_empty "$root"
  c="${candidate%/}/"
  r="${root%/}/"
  case "$c" in
    "$r"*) ;;
    *) die "path escapes approved root: candidate=$candidate root=$root" ;;
  esac
}

require_under_existing_root() {
  local candidate="$1"
  local root="$2"

  [ -e "$candidate" ] || die "path does not exist: $candidate"
  require_under_root "$(realpath -- "$candidate")" "$root"
}

refuse_under_root() {
  local candidate root c r label
  candidate="$(canonical_path "$1")"
  root="$(canonical_path "$2")"
  label="${3:-root}"
  refuse_root_or_empty "$candidate"
  refuse_root_or_empty "$root"
  c="${candidate%/}/"
  r="${root%/}/"
  case "$c" in
    "$r"*) die "path overlaps forbidden ${label}: candidate=$candidate forbidden=$root" ;;
  esac
}

require_write_target_path() {
  local path="$1"
  local label="${2:-write target}"

  refuse_root_or_empty "$path"
  case "$path" in
    /*) ;;
    *) die "${label} must be absolute: $path" ;;
  esac
  refuse_symlink_path "$path"
}

require_write_root_path() {
  local path="$1"
  local label="${2:-write root}"

  require_write_target_path "$path" "$label"
  refuse_path_overlap "$path" "$PROJECT_ROOT" "tooling project"
  refuse_path_overlap "$path" "$LIVE_RUNTIME_DIR" "live runtime"
  refuse_path_overlap "$path" "$NODE_DATA_SRC" "live node data"
  refuse_path_overlap "$path" "$NODEWORKER_DATA_SRC" "live nodeworker data"
  refuse_path_overlap "$path" "$COLLECTOR_RUNTIME_SRC" "collector runtime"
}

require_restore_target_path() {
  local path="$1"

  require_write_target_path "$path" "restore target"
  require_under_root "$path" "$DEV_RESTORE_ROOT"
  refuse_path_overlap "$path" "$LIVE_RUNTIME_DIR" "live runtime"
  refuse_path_overlap "$path" "$NODE_DATA_SRC" "live node data"
  refuse_path_overlap "$path" "$NODEWORKER_DATA_SRC" "live nodeworker data"
  refuse_path_overlap "$path" "$COLLECTOR_RUNTIME_SRC" "collector runtime"
}

refuse_path_overlap() {
  local left="$1"
  local right="$2"
  local label="${3:-path}"

  refuse_under_root "$left" "$right" "$label"
  refuse_under_root "$right" "$left" "$label"
}

refuse_symlink_path() {
  local path="$1"
  local current part
  local -a parts
  current="/"
  refuse_root_or_empty "$path"
  case "$path" in
    /*) ;;
    *) path="${PWD}/${path}" ;;
  esac
  IFS='/' read -r -a parts <<< "${path#/}"
  for part in "${parts[@]}"; do
    [ -n "$part" ] || continue
    [ "$part" != "." ] || continue
    if [ "$part" = ".." ]; then
      current="$(dirname -- "$current")"
      continue
    fi
    current="${current%/}/$part"
    [ ! -L "$current" ] || die "symlink component refused: $current"
  done
}

require_resolved_under_root() {
  local candidate="$1"
  local root="$2"
  local resolved

  [ -e "$candidate" ] || die "path does not exist: $candidate"
  resolved="$(realpath -- "$candidate")"
  require_under_root "$resolved" "$root"
  printf '%s\n' "$resolved"
}

resolve_latest_lkg() {
  local link_path="${BACKUP_ROOT}/last-known-good/current"

  [ -L "$link_path" ] || die "latest LKG link missing"
  refuse_symlink_path "$(dirname -- "$link_path")"
  require_resolved_under_root "$link_path" "$BACKUP_ROOT/runs"
}

validate_backup_id_literal() {
  local value="$1"

  [ -n "$value" ] || die "backup id cannot be empty"
  case "$value" in
    */*|*'*'*|*'?'*|*'['*|*']'*) die "backup id must be a literal run id: $value" ;;
    vqueen-v6.5.7-*) ;;
    *) die "backup id must start with vqueen-v6.5.7-: $value" ;;
  esac
}

require_completed_backup_run() {
  local run_path="$1"
  local resolved

  [ -n "$run_path" ] || die "backup run path cannot be empty"
  resolved="$(require_resolved_under_root "$run_path" "$BACKUP_ROOT/runs")"
  require_under_root "$resolved" "$BACKUP_ROOT/runs"
  validate_backup_id_literal "$(basename -- "$resolved")"
  refuse_symlink_path "$resolved"
  [ -f "$resolved/metadata/status.txt" ] || die "backup status missing: $resolved"
  grep -qx complete "$resolved/metadata/status.txt" || die "backup run is not complete: $resolved"
  [ -d "$resolved/data" ] || die "backup data dir missing: $resolved"
  [ -f "$resolved/manifests/file-manifest.sha256" ] || die "backup file manifest missing: $resolved"
  printf '%s\n' "$resolved"
}

resolve_completed_backup_run() {
  local value="$1"
  local found count candidate

  [ -n "$value" ] || die "backup selector cannot be empty"
  case "$value" in
    latest)
      require_completed_backup_run "$(resolve_latest_lkg)"
      ;;
    /*)
      require_completed_backup_run "$value"
      ;;
    *)
      validate_backup_id_literal "$value"
      found=""
      count=0
      while IFS= read -r candidate; do
        if [ "$(basename -- "$candidate")" = "$value" ]; then
          found="$candidate"
          count=$((count + 1))
        fi
      done < <(find "$BACKUP_ROOT/runs" -mindepth 4 -maxdepth 4 -type d -print 2>/dev/null || true)
      [ "$count" -le 1 ] || die "backup id matched more than one run: $value"
      [ -n "$found" ] || die "backup id not found under runs: $value"
      require_completed_backup_run "$found"
      ;;
  esac
}

require_uint() {
  local value="$1"
  local name="$2"

  case "$value" in
    ""|*[!0-9]*) die "${name} must be a non-negative integer" ;;
  esac
}

require_uint_range() {
  local value="$1"
  local name="$2"
  local min="$3"
  local max="$4"

  require_uint "$value" "$name"
  [ "$value" -ge "$min" ] || die "${name} must be >= ${min}"
  [ "$value" -le "$max" ] || die "${name} must be <= ${max}"
}

validate_resource_config() {
  require_uint "${MIN_FREE_BYTES:-}" "MIN_FREE_BYTES"
  require_uint "${KEEP_LAST_KNOWN_GOOD:-}" "KEEP_LAST_KNOWN_GOOD"
  require_uint "${CANDIDATE_RETENTION_DAYS:-}" "CANDIDATE_RETENTION_DAYS"
  require_uint "${FAILED_RETENTION_DAYS:-}" "FAILED_RETENTION_DAYS"
  require_uint_range "${RSYNC_NICE:-}" "RSYNC_NICE" 0 19
  require_uint_range "${RSYNC_IONICE_CLASS:-}" "RSYNC_IONICE_CLASS" 2 3
  require_uint_range "${RSYNC_IONICE_LEVEL:-}" "RSYNC_IONICE_LEVEL" 0 7
  if [ -n "${RSYNC_BWLIMIT_KB:-}" ]; then
    require_uint "$RSYNC_BWLIMIT_KB" "RSYNC_BWLIMIT_KB"
  fi
}

validate_controlled_sudo_config() {
  case "${LIVE_READ_ACCESS:-}" in
    controlled-sudo)
      [ "${SUDO_BIN:-}" = "sudo" ] || die "SUDO_BIN must be sudo for controlled sudo"
      [ "${SUDO_FLAGS:-}" = "-n" ] || die "SUDO_FLAGS must be -n for controlled sudo"
      ;;
    direct)
      ;;
    *)
      die "LIVE_READ_ACCESS must be controlled-sudo or direct"
      ;;
  esac
}

validate_live_read_gate() {
  [ "${LIVE_READ_ACCESS:-}" = "controlled-sudo" ] || return 0
  log_debug MainThread "validating controlled sudo helper availability"
  require_tool "$SUDO_BIN"
  log_debug MainThread "controlled sudo helper available"
}

validate_lock_paths() {
  log_debug MainThread "validating lock paths"
  require_under_root "$OPERATION_LOCK" "$PROJECT_STATE_DIR"
  require_under_root "$BACKUP_LOCK" "$PROJECT_STATE_DIR"
  require_under_root "$RESTORE_LOCK" "$PROJECT_STATE_DIR"
  require_under_root "$CYCLE_LOCK" "$PROJECT_STATE_DIR"
  require_write_target_path "$PROJECT_STATE_DIR" "project state dir"
  require_write_target_path "$OPERATION_LOCK" "operation lock"
  require_write_target_path "$BACKUP_LOCK" "backup lock"
  require_write_target_path "$RESTORE_LOCK" "restore lock"
  require_write_target_path "$CYCLE_LOCK" "cycle lock"
  refuse_path_overlap "$OPERATION_LOCK" "$BACKUP_LOCK" "lock file"
  refuse_path_overlap "$OPERATION_LOCK" "$RESTORE_LOCK" "lock file"
  refuse_path_overlap "$OPERATION_LOCK" "$CYCLE_LOCK" "lock file"
  refuse_path_overlap "$BACKUP_LOCK" "$RESTORE_LOCK" "lock file"
  refuse_path_overlap "$BACKUP_LOCK" "$CYCLE_LOCK" "lock file"
  refuse_path_overlap "$RESTORE_LOCK" "$CYCLE_LOCK" "lock file"
  log_debug MainThread "lock paths validated"
}

operation_lock_already_held() {
  local fd="${VQUEEN_OPERATION_LOCK_FD:-}"
  local lock_path="${VQUEEN_OPERATION_LOCK_PATH:-${OPERATION_LOCK:-}}"
  local fd_path fd_inode lock_inode

  [ -n "$fd" ] || return 1
  case "$fd" in
    ""|*[!0-9]*) return 1 ;;
  esac
  [ -n "$lock_path" ] || return 1
  [ "${OPERATION_LOCK:-}" = "$lock_path" ] || return 1
  fd_path="/proc/$$/fd/$fd"
  [ -e "$fd_path" ] || return 1
  [ -e "$lock_path" ] || return 1
  fd_inode="$(stat -Lc '%d:%i' "$fd_path" 2>/dev/null)" || return 1
  lock_inode="$(stat -Lc '%d:%i' "$lock_path" 2>/dev/null)" || return 1
  [ "$fd_inode" = "$lock_inode" ] || return 1
  flock -n "$fd" || return 1
}

with_lock() {
  local lock_path="$1"
  local lock_fd rc restore_operation_lock_env=0
  local old_operation_lock_fd="" old_operation_lock_path=""
  local had_operation_lock_fd=0 had_operation_lock_path=0
  shift

  require_tool flock
  mkdir -p -- "$(dirname -- "$lock_path")"
  log_info MainThread "acquiring lock: $lock_path"
  exec {lock_fd}>"$lock_path"
  flock -n "$lock_fd" || die "lock already held: $lock_path"
  log_info MainThread "lock acquired: $lock_path"
  if [ "${OPERATION_LOCK:-}" = "$lock_path" ]; then
    restore_operation_lock_env=1
    if [ "${VQUEEN_OPERATION_LOCK_FD+x}" ]; then
      had_operation_lock_fd=1
      old_operation_lock_fd="$VQUEEN_OPERATION_LOCK_FD"
    fi
    if [ "${VQUEEN_OPERATION_LOCK_PATH+x}" ]; then
      had_operation_lock_path=1
      old_operation_lock_path="$VQUEEN_OPERATION_LOCK_PATH"
    fi
    export VQUEEN_OPERATION_LOCK_FD="$lock_fd"
    export VQUEEN_OPERATION_LOCK_PATH="$lock_path"
  fi
  "$@" || rc=$?
  if [ "$restore_operation_lock_env" -eq 1 ]; then
    if [ "$had_operation_lock_fd" -eq 1 ]; then
      export VQUEEN_OPERATION_LOCK_FD="$old_operation_lock_fd"
    else
      unset VQUEEN_OPERATION_LOCK_FD
    fi
    if [ "$had_operation_lock_path" -eq 1 ]; then
      export VQUEEN_OPERATION_LOCK_PATH="$old_operation_lock_path"
    else
      unset VQUEEN_OPERATION_LOCK_PATH
    fi
  fi
  log_info MainThread "releasing lock: $lock_path"
  flock -u "$lock_fd" || true
  eval "exec ${lock_fd}>&-"
  log_info MainThread "lock released: $lock_path"
  return "${rc:-0}"
}

load_config() {
  PROJECT_ROOT="${PROJECT_ROOT:-$(project_root_from_lib)}"
  export PROJECT_ROOT

  local conf
  log_debug MainThread "loading backup config"
  conf="${VQUEEN_BACKUP_CONF:-${PROJECT_ROOT}/etc/vqueen-backup.conf}"
  if [ ! -f "$conf" ]; then
    log_debug MainThread "active config missing, falling back to example: $conf"
    conf="${PROJECT_ROOT}/etc/vqueen-backup.conf.example"
  fi
  [ -f "$conf" ] || die "config not found: $conf"
  # shellcheck source=/dev/null
  . "$conf"
  RSYNC_WRAPPER_BIN="${RSYNC_WRAPPER_BIN:-/usr/local/sbin/vqueen-nearhot-rsync}"
  MANIFEST_WRAPPER_BIN="${MANIFEST_WRAPPER_BIN:-/usr/local/sbin/vqueen-nearhot-manifest}"
  RESTORE_WRAPPER_BIN="${RESTORE_WRAPPER_BIN:-/usr/local/sbin/vqueen-nearhot-restore-proof}"
  OPERATION_LOCK="${OPERATION_LOCK:-${PROJECT_STATE_DIR}/vqueen-v657-nearhot-operation.lock}"
  BACKUP_LOCK="${BACKUP_LOCK:-${PROJECT_STATE_DIR}/vqueen-v657-nearhot-backup.lock}"
  RESTORE_LOCK="${RESTORE_LOCK:-${PROJECT_STATE_DIR}/vqueen-v657-restore-test.lock}"
  CYCLE_LOCK="${CYCLE_LOCK:-${PROJECT_STATE_DIR}/vqueen-v657-nearhot-cycle.lock}"
  CANDIDATE_RETENTION_DAYS="${CANDIDATE_RETENTION_DAYS:-14}"
  FAILED_RETENTION_DAYS="${FAILED_RETENTION_DAYS:-14}"
  CONTAINER_RUNNER_NETWORK="${CONTAINER_RUNNER_NETWORK:-${LIVE_COMPOSE_PROJECT}_default}"
  log_info MainThread "config loaded: $conf"
}

make_run_id() {
  local host rand
  host="$(hostname -s | tr -cd '[:alnum:]-' | tr '[:upper:]' '[:lower:]')"
  rand="$(LC_ALL=C tr -dc 'a-f0-9' </dev/urandom | head -c 6 || true)"
  printf 'vqueen-v6.5.7-%s-%s-%s\n' "$(date -u +%Y%m%dT%H%M%SZ)" "${host:-host}" "${rand:-000000}"
}

validate_static_config() {
  log_debug MainThread "static config validation start"
  load_config

  [ "${PROJECT_ROOT}" = "$(canonical_path "$PROJECT_ROOT")" ] || PROJECT_ROOT="$(canonical_path "$PROJECT_ROOT")"
  validate_resource_config
  validate_controlled_sudo_config
  require_under_root "${PROJECT_ROOT}/bin" "$PROJECT_ROOT"
  require_under_root "${PROJECT_ROOT}/lib" "$PROJECT_ROOT"
  require_under_root "${PROJECT_ROOT}/etc" "$PROJECT_ROOT"
  require_under_root "$PROJECT_STATE_DIR" "$PROJECT_ROOT"
  validate_lock_paths

  refuse_path_overlap "$BACKUP_ROOT" "$PROJECT_ROOT" "tooling project"
  refuse_path_overlap "$DEV_RESTORE_ROOT" "$PROJECT_ROOT" "tooling project"
  refuse_path_overlap "$BACKUP_LOG_DIR" "$PROJECT_ROOT" "tooling project"
  refuse_path_overlap "$RESTORE_LOG_DIR" "$PROJECT_ROOT" "tooling project"

  refuse_path_overlap "$DEV_RESTORE_ROOT" "$BACKUP_ROOT" "backup payload"
  refuse_path_overlap "$BACKUP_LOG_DIR" "$RESTORE_LOG_DIR" "log root"

  require_write_root_path "$BACKUP_ROOT" "backup root"
  require_write_root_path "$DEV_RESTORE_ROOT" "dev restore root"
  require_write_root_path "$BACKUP_LOG_DIR" "backup log dir"
  require_write_root_path "$RESTORE_LOG_DIR" "restore log dir"

  refuse_path_overlap "$BACKUP_ROOT" "$LIVE_RUNTIME_DIR" "live runtime"
  refuse_path_overlap "$BACKUP_ROOT" "$NODE_DATA_SRC" "live node data"
  refuse_path_overlap "$BACKUP_ROOT" "$NODEWORKER_DATA_SRC" "live nodeworker data"
  refuse_path_overlap "$BACKUP_ROOT" "$COLLECTOR_RUNTIME_SRC" "collector runtime"

  refuse_path_overlap "$DEV_RESTORE_ROOT" "$LIVE_RUNTIME_DIR" "live runtime"
  refuse_path_overlap "$DEV_RESTORE_ROOT" "$NODE_DATA_SRC" "live node data"
  refuse_path_overlap "$DEV_RESTORE_ROOT" "$NODEWORKER_DATA_SRC" "live nodeworker data"
  refuse_path_overlap "$DEV_RESTORE_ROOT" "$COLLECTOR_RUNTIME_SRC" "collector runtime"

  validate_postgres_static_config
  validate_cycle_static_config
  log_debug MainThread "static config validation complete"
}

validate_cycle_static_config() {
  [ -n "${CONTAINER_RUNNER_IMAGE:-}" ] || die "CONTAINER_RUNNER_IMAGE is required"
  [ -n "${CONTAINER_RUNNER_NETWORK:-}" ] || die "CONTAINER_RUNNER_NETWORK is required"
  [ -n "${POSTGRES_PASSWORD_FILE:-}" ] || die "POSTGRES_PASSWORD_FILE is required"
  require_write_target_path "$POSTGRES_PASSWORD_FILE" "postgres password file"
  refuse_path_overlap "$POSTGRES_PASSWORD_FILE" "$PROJECT_ROOT" "tooling project"
  refuse_path_overlap "$POSTGRES_PASSWORD_FILE" "$LIVE_RUNTIME_DIR" "live runtime"
  refuse_path_overlap "$POSTGRES_PASSWORD_FILE" "$NODE_DATA_SRC" "live node data"
  refuse_path_overlap "$POSTGRES_PASSWORD_FILE" "$NODEWORKER_DATA_SRC" "live nodeworker data"
  refuse_path_overlap "$POSTGRES_PASSWORD_FILE" "$COLLECTOR_RUNTIME_SRC" "collector runtime"
}

check_required_tools_for_preflight() {
  log_debug MainThread "checking required preflight tools"
  require_tool bash
  require_tool find
  require_tool flock
  require_tool git
  require_tool ionice
  require_tool nice
  require_tool rsync
  require_tool sha256sum
  require_tool stat
  require_tool realpath
  require_tool docker
  if [ "${LIVE_READ_ACCESS:-}" = "controlled-sudo" ]; then
    require_tool "$SUDO_BIN"
  fi
  log_debug MainThread "required preflight tools present"
}

check_required_tools_for_backup() {
  log_debug MainThread "checking required backup tools"
  check_required_tools_for_preflight
  require_tool du
  require_tool mv
  require_tool tee
  log_debug MainThread "required backup tools present"
}

path_exists_dir() {
  local path="$1"

  case "${LIVE_READ_ACCESS:-}" in
    controlled-sudo)
      log_debug MainThread "checking source dir through controlled sudo: $path"
      "$SUDO_BIN" $SUDO_FLAGS test -d "$path"
      ;;
    direct)
      log_debug MainThread "checking source dir directly: $path"
      [ -d "$path" ]
      ;;
    *)
      die "LIVE_READ_ACCESS must be controlled-sudo or direct"
      ;;
  esac
}

validate_postgres_static_config() {
  [ -n "${POSTGRES_CONTAINER:-}" ] || die "POSTGRES_CONTAINER is required"
  [ -n "${POSTGRES_COMPOSE_SERVICE:-}" ] || die "POSTGRES_COMPOSE_SERVICE is required"
  [ -n "${POSTGRES_IMAGE_FAMILY:-}" ] || die "POSTGRES_IMAGE_FAMILY is required"
}

check_postgres_container_identity() {
  local name project service image

  log_info PostgresDump "checking postgres container identity"
  validate_postgres_static_config
  require_tool docker
  name="$(docker inspect -f '{{.Name}}' "$POSTGRES_CONTAINER" 2>/dev/null | sed 's#^/##')" \
    || die "postgres container not inspectable: $POSTGRES_CONTAINER"
  project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$POSTGRES_CONTAINER" 2>/dev/null || true)"
  service="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "$POSTGRES_CONTAINER" 2>/dev/null || true)"
  image="$(docker inspect -f '{{.Config.Image}}' "$POSTGRES_CONTAINER" 2>/dev/null || true)"
  [ "$name" = "$POSTGRES_CONTAINER" ] || die "postgres container name mismatch: $name"
  [ "$project" = "$LIVE_COMPOSE_PROJECT" ] || die "postgres compose project mismatch: $project"
  [ "$service" = "$POSTGRES_COMPOSE_SERVICE" ] || die "postgres compose service mismatch: $service"
  case "$image" in
    "$POSTGRES_IMAGE_FAMILY"|"$POSTGRES_IMAGE_FAMILY"@*) ;;
    *) die "postgres image mismatch: $image" ;;
  esac
  log_info PostgresDump "postgres container identity accepted: $POSTGRES_CONTAINER"
}

run_with_live_read_access() {
  case "${LIVE_READ_ACCESS:-}" in
    controlled-sudo)
      die "generic sudo dispatch is refused; use reviewed live-read helpers"
      ;;
    direct)
      "$@"
      ;;
    *)
      die "LIVE_READ_ACCESS must be controlled-sudo or direct"
      ;;
  esac
}

setup_log_dir() {
  local dir="$1"

  if [ -d "$dir" ] && [ -w "$dir" ]; then
    log_debug MainThread "log dir already writable: $dir"
    return 0
  fi

  case "${LIVE_READ_ACCESS:-}" in
    controlled-sudo)
      die "backup log dir must already exist and be writable: $dir"
      ;;
    direct)
      log_info MainThread "creating log dir: $dir"
      mkdir -p -- "$dir"
      ;;
    *)
      die "LIVE_READ_ACCESS must be controlled-sudo or direct"
      ;;
  esac
}

free_bytes_for_path() {
  local path="$1"
  df -B1 --output=avail "$path" | awk 'NR == 2 { print $1 }'
}

print_boundary_notice() {
  cat <<'NOTICE'
Current scaffold boundary:
- preflight, dry-run, and backup may run only when approved for the active gate
- restore and cleanup may run only when separately approved for the active gate
- no timers
- no production service lifecycle action
- no push
NOTICE
}
