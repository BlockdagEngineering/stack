#!/usr/bin/env bash
# Fix ownership of persisted paths on every container start. Docker volumes are
# often populated as root, which prevents bdagStack from opening chain data.
set -euo pipefail

timestamp_iso() {
  date -Is 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S%z'
}

log() {
  printf '[%s] node-entrypoint: %s\n' "$(timestamp_iso)" "$*" >&2
}

lower_ascii() {
  printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]'
}

ensure_owned_runtime_dirs() {
  mkdir -p /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack
  chown bdagStack:bdagStack /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack || true
}

fix_ownership_if_needed() {
  local mode="${BDAG_ENTRYPOINT_CHOWN_MODE:-needed}"
  local uid gid path mismatched
  case "$mode" in
    never|off|0|false)
      log "recursive ownership repair disabled by BDAG_ENTRYPOINT_CHOWN_MODE=$mode"
      return 0
      ;;
  esac

  uid="$(id -u bdagStack)"
  gid="$(id -g bdagStack)"
  for path in /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack; do
    [ -e "$path" ] || continue
    mismatched=""
    if [ "$(stat -c '%u:%g' "$path" 2>/dev/null || printf '')" != "$uid:$gid" ]; then
      mismatched="$path"
    elif [ "$mode" = "always" ]; then
      mismatched="$path"
    else
      mismatched="$(find "$path" \( ! -uid "$uid" -o ! -gid "$gid" \) -print -quit 2>/dev/null || true)"
    fi
    [ -n "$mismatched" ] || continue
    log "repairing ownership below $path due to ${mismatched#$path/}"
    chown -R bdagStack:bdagStack "$path" || true
  done
}

nodeworker_arg_present() {
  local key="$1"
  shift
  local arg
  for arg in "$@"; do
    case "$arg" in
      --"$key"|--"$key"=*)
        return 0
        ;;
    esac
  done
  return 1
}

node_arg_value() {
  local key="$1"
  local node_args="$2"
  local next=0
  local word
  for word in $node_args; do
    if [ "$next" = "1" ]; then
      printf '%s\n' "$word"
      return 0
    fi
    case "$word" in
      --"$key"=*)
        printf '%s\n' "${word#*=}"
        return 0
        ;;
      --"$key")
        next=1
        ;;
    esac
  done
  return 1
}

read_config_value() {
  local config_file="$1"
  local key="$2"
  [ -f "$config_file" ] || return 1
  awk -F= -v key="$key" '
    $1 == key {
      value = $0
      sub("^[^=]*=", "", value)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      print value
      exit
    }
  ' "$config_file"
}

node_args_from_argv() {
  local arg
  for arg in "$@"; do
    case "$arg" in
      --node-args=*)
        printf '%s\n' "${arg#*=}"
        return 0
        ;;
    esac
  done
  return 1
}

addpeer_values() {
  local node_args="$1"
  local word
  for word in $node_args; do
    case "$word" in
      --addpeer=*)
        printf '%s\n' "${word#*=}"
        ;;
    esac
  done
}

config_addpeer_values() {
  local config_file="$1"
  [ -f "$config_file" ] || return 0
  awk -F= '
    $1 ~ /^[[:space:]]*addpeer[[:space:]]*$/ {
      value = $0
      sub("^[^=]*=", "", value)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      if (value != "") print value
    }
  ' "$config_file"
}

ORDERED_FASTSYNC_SEEN=
append_unique_peer() {
  local bucket_name="$1"
  local peer="$2"
  local -n bucket="$bucket_name"

  [ -n "$peer" ] || return 0
  case "$peer" in
    none|null) return 0 ;;
  esac
  case "$ORDERED_FASTSYNC_SEEN" in
    *"|$peer|"*) return 0 ;;
  esac
  bucket+=("$peer")
  ORDERED_FASTSYNC_SEEN="${ORDERED_FASTSYNC_SEEN}|$peer|"
}

append_peer_list() {
  local bucket_name="$1"
  local raw="$2"
  local old_ifs="$IFS"
  local peer
  IFS=', '
  for peer in $raw; do
    peer_allowed_for_p2p "$peer" || continue
    append_unique_peer "$bucket_name" "$peer"
  done
  IFS="$old_ifs"
}

peer_allowed_for_p2p() {
  local peer="$1"
  case "$peer" in
    */p2p/*) return 0 ;;
  esac
  return 1
}

join_peer_array() {
  local old_ifs="$IFS"
  local joined
  IFS=,
  joined="${fastsync_peers[*]:-}"
  IFS="$old_ifs"
  printf '%s\n' "$joined"
}

ordered_fastsync_peers() {
  local node_args="$1"
  local ordering="${BDAG_FASTSYNC_PEER_ORDERING:-p2p-latency}"
  local config_file config_peers generic_peers
  fastsync_peers=()
  ORDERED_FASTSYNC_SEEN=

  config_file="$(node_arg_value configfile "$node_args" || true)"
  config_file="${config_file:-/etc/bdagStack/node.conf}"
  config_peers="$(config_addpeer_values "$config_file" | paste -sd, - || true)"

  case "$ordering" in
    p2p-latency|p2p|latency|flat-latency|flat|tiered-latency|legacy-buckets|buckets) ;;
    *) log "unknown BDAG_FASTSYNC_PEER_ORDERING=$ordering; using p2p-latency" ;;
  esac
  generic_peers="${BDAG_FASTSYNC_PEERS:-} ${BOOTSTRAP_PEER_ADDRESSES:-} $config_peers $(addpeer_values "$node_args" | paste -sd, - || true)"
  append_peer_list fastsync_peers "$generic_peers"

  join_peer_array
}

addpeer_args_from_csv() {
  local csv="$1"
  local old_ifs="$IFS"
  local peer
  IFS=,
  for peer in $csv; do
    [ -n "$peer" ] && printf ' --addpeer=%s' "$peer"
  done
  IFS="$old_ifs"
}

bootstrapnode_arg_from_csv() {
  local csv="$1"
  local old_ifs="$IFS"
  local peer
  IFS=,
  for peer in $csv; do
    if [ -n "$peer" ]; then
      printf '%s' "--bootstrapnode=$peer"
      IFS="$old_ifs"
      return 0
    fi
  done
  IFS="$old_ifs"
}

apply_ordered_fastsync_peers() {
  case "${BDAG_FASTSYNC_PEER_ORDERING:-p2p-latency}" in
    0|off|false|none) return 0 ;;
  esac

  local node_args ordered addpeer_args bootstrapnode_arg total_count ordering
  ordering="${BDAG_FASTSYNC_PEER_ORDERING:-p2p-latency}"
  node_args="$(node_args_from_argv "$@" || true)"
  ordered="$(ordered_fastsync_peers "$node_args")"
  [ -n "$ordered" ] || return 0

  total_count="$(printf '%s' "$ordered" | awk -F, '{print NF}')"
  log "P2P peer candidates enabled; total=${total_count}"

  if [ "${BDAG_FASTSYNC_APPEND_ADDPEERS:-1}" = "1" ]; then
    addpeer_args="$(addpeer_args_from_csv "$ordered")"
    NODE_ARGS_APPEND="${addpeer_args}${NODE_ARGS_APPEND:+ $NODE_ARGS_APPEND}"
    export NODE_ARGS_APPEND
  fi

  if [ "${BDAG_FASTSYNC_APPEND_BOOTSTRAPNODE:-1}" = "1" ]; then
    bootstrapnode_arg="$(bootstrapnode_arg_from_csv "$ordered")"
    if [ -n "$bootstrapnode_arg" ] && ! node_args_contains_prefix "$node_args ${NODE_ARGS_APPEND:-}" "--bootstrapnode"; then
      NODE_ARGS_APPEND="${NODE_ARGS_APPEND:+$NODE_ARGS_APPEND }$bootstrapnode_arg"
      export NODE_ARGS_APPEND
    fi
  fi
}

node_args_contains_word() {
  local node_args="$1"
  local needle="$2"
  local word
  for word in $node_args; do
    [ "$word" = "$needle" ] && return 0
  done
  return 1
}

append_node_arg_once() {
  local flag="$1"
  local node_args="$2"
  if node_args_contains_word "$node_args" "$flag"; then
    return 0
  fi
  NODE_ARGS_APPEND="${NODE_ARGS_APPEND:+$NODE_ARGS_APPEND }$flag"
  export NODE_ARGS_APPEND
}

remove_node_arg_prefix() {
  local prefix="$1"
  local filtered="" word
  for word in ${NODE_ARGS_APPEND:-}; do
    case "$word" in
      "$prefix"|"$prefix"=*) continue ;;
    esac
    filtered="${filtered:+$filtered }$word"
  done
  NODE_ARGS_APPEND="$filtered"
  export NODE_ARGS_APPEND
}

node_args_contains_prefix() {
  local node_args="$1"
  local prefix="$2"
  local word
  for word in $node_args; do
    case "$word" in
      "$prefix"|"$prefix"=*) return 0 ;;
    esac
  done
  return 1
}

append_node_arg_prefix_once() {
  local flag="$1"
  local node_args="$2"
  local prefix="${flag%%=*}"
  if node_args_contains_prefix "$node_args" "$prefix"; then
    return 0
  fi
  NODE_ARGS_APPEND="${NODE_ARGS_APPEND:+$NODE_ARGS_APPEND }$flag"
  export NODE_ARGS_APPEND
}

apply_node_metrics_runtime_args() {
  local node_args
  node_args="$(node_args_from_argv "$@" || true)"
  append_node_arg_prefix_once "--metrics" "$node_args ${NODE_ARGS_APPEND:-}"
}

apply_node_mining_runtime_args() {
  case "${BDAG_ENABLE_NODE_MINING:-0}" in
    1|true|TRUE|yes|YES|on|ON) ;;
    *) return 0 ;;
  esac

  local node_args modules word
  node_args="$(node_args_from_argv "$@" || true)"
  modules="${BDAG_NODE_MODULES:-}"
  if [ -n "$modules" ]; then
    modules="$(printf '%s' "$modules" | tr ',' ' ')"
    for word in $modules; do
      [ -n "$word" ] || continue
      append_node_arg_once "--modules=${word}" "$node_args ${NODE_ARGS_APPEND:-}"
    done
  fi
  for word in ${BDAG_NODE_MINING_ARGS:-}; do
    case "$word" in
      --miningaddr=*) append_node_arg_prefix_once "$word" "$node_args ${NODE_ARGS_APPEND:-}" ;;
      --*) append_node_arg_once "$word" "$node_args ${NODE_ARGS_APPEND:-}" ;;
    esac
  done
}

mount_source_for_path() {
  local path="$1" real best_src="" best_target="" src target fstype rest
  real="$(readlink -m "$path" 2>/dev/null || printf '%s' "$path")"
  [ -r /proc/mounts ] || {
    printf '\n'
    return 0
  }
  while read -r src target fstype rest; do
    target="${target//\\040/ }"
    if [[ "$real" == "$target" || "$real" == "$target"/* ]]; then
      if [ "${#target}" -gt "${#best_target}" ]; then
        best_target="$target"
        best_src="$src"
      fi
    fi
  done < /proc/mounts
  printf '%s\n' "$best_src"
}

block_device_from_source() {
  local source="$1" base
  case "$source" in
    /dev/*) ;;
    *) return 1 ;;
  esac
  base="$(basename "$source")"
  case "$base" in
    nvme*n*p*) printf '%s\n' "${base%p[0-9]*}" ;;
    mmcblk*p*) printf '%s\n' "${base%p[0-9]*}" ;;
    *) printf '%s\n' "${base%%[0-9]*}" ;;
  esac
}

path_is_usb_backed() {
  local path="$1" source block device_path
  source="$(mount_source_for_path "$path")"
  block="$(block_device_from_source "$source" 2>/dev/null || true)"
  [ -n "$block" ] || return 1
  device_path="$(readlink -f "/sys/block/$block/device" 2>/dev/null || true)"
  case "$device_path" in
    *usb*) return 0 ;;
    *) return 1 ;;
  esac
}

env_value_true() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON|enabled|ENABLED) return 0 ;;
  esac
  return 1
}

env_value_false() {
  case "${1:-}" in
    0|false|FALSE|no|NO|off|OFF|disabled|DISABLED) return 0 ;;
  esac
  return 1
}

node_data_parent_from_args() {
  local node_args config_file data_parent
  node_args="$(node_args_from_argv "$@" || true)"
  config_file="$(node_arg_value configfile "$node_args" || true)"
  data_parent="$(node_arg_value datadir "$node_args" || true)"
  if [ -z "$data_parent" ] && [ -n "$config_file" ]; then
    data_parent="$(read_config_value "$config_file" datadir || true)"
  fi
  printf '%s\n' "${data_parent:-/var/lib/bdagStack/node}"
}

fastsync_serving_disable_reason() {
  local no_serve="${BDAG_NO_FASTSYNC_SERVE:-auto}"
  if env_value_true "$no_serve"; then
    printf 'BDAG_NO_FASTSYNC_SERVE=%s\n' "$no_serve"
    return 0
  fi
  if env_value_false "$no_serve"; then
    return 1
  fi

  local storage_profile="${BDAG_STORAGE_PROFILE:-}"
  storage_profile="$(lower_ascii "$storage_profile")"
  case "$storage_profile" in
    usb-chain-internal-runtime|single-usb-constrained)
      printf 'BDAG_STORAGE_PROFILE=%s\n' "$storage_profile"
      return 0
      ;;
  esac

  local data_parent
  data_parent="$(node_data_parent_from_args "$@")"
  if path_is_usb_backed "$data_parent"; then
    printf 'usb_backed_datadir=%s\n' "$data_parent"
    return 0
  fi

  return 1
}

node_binary_from_argv() {
  local arg
  if [ -n "${BDAG_NODE_BINARY:-}" ]; then
    printf '%s\n' "$BDAG_NODE_BINARY"
    return 0
  fi
  for arg in "$@"; do
    case "$arg" in
      --node-binary=*)
        printf '%s\n' "${arg#*=}"
        return 0
        ;;
    esac
  done
  if [ "$#" -gt 0 ]; then
    printf '%s\n' "$1"
    return 0
  fi
  return 1
}

node_binary_supports_arg() {
  local flag="$1" binary
  shift
  binary="$(node_binary_from_argv "$@" || true)"
  [ -n "$binary" ] || return 1
  if [ ! -x "$binary" ]; then
    binary="$(command -v "$binary" 2>/dev/null || true)"
  fi
  [ -n "$binary" ] || return 1
  "$binary" --help 2>&1 | grep -q -- "$flag"
}

apply_no_fastsync_serve_guard() {
  local disable_reason
  disable_reason="$(fastsync_serving_disable_reason "$@" || true)"
  if [ -z "$disable_reason" ]; then
    return 0
  fi

  local node_args
  node_args="$(node_args_from_argv "$@" || true)"
  if node_binary_supports_arg "--nofastsyncserve" "$@"; then
    append_node_arg_once "--nofastsyncserve" "$node_args ${NODE_ARGS_APPEND:-}"
    log "Bulk sync serving guard active ($disable_reason); keeping normal outbound sync and block relay."
  else
    log "Bulk sync serving guard active ($disable_reason); selected node binary does not support --nofastsyncserve."
  fi
}

apply_archival_flag() {
  case "${BDAG_NODE_ARCHIVAL:-0}" in
    1|true|True|yes) ;;
    *) return 0 ;;
  esac
  local node_args
  node_args="$(node_args_from_argv "$@" || true)"
  append_node_arg_once "--archival" "$node_args ${NODE_ARGS_APPEND:-}"
  log "archival mode enabled; node keeps full block history (--archival)"
}

start_node_metrics_exporter() {
  case "${BDAG_NODE_METRICS_EXPORTER_ENABLED:-1}" in
    0|false|FALSE|no|NO|off|OFF|disabled|DISABLED) return 0 ;;
  esac
  if [ "${BDAG_ENTRYPOINT_PRINT_NODE_FLAGS:-0}" = "1" ]; then
    return 0
  fi
  local exporter="/usr/local/bin/node-metrics-exporter.py"
  if [ ! -x "$exporter" ]; then
    log "node metrics exporter missing at $exporter"
    return 0
  fi
  log "starting node metrics exporter on ${BDAG_NODE_METRICS_EXPORTER_ADDR:-0.0.0.0}:${BDAG_NODE_METRICS_EXPORTER_PORT:-6060}"
  if [ "$(id -u)" = 0 ]; then
    runuser -u bdagStack -g bdagStack -- "$exporter" &
  else
    "$exporter" &
  fi
}

apply_ordered_fastsync_peers "$@"
apply_no_fastsync_serve_guard "$@"
apply_node_metrics_runtime_args "$@"
apply_node_mining_runtime_args "$@"
apply_archival_flag "$@"

if [ -n "${NODE_ARGS_APPEND:-}" ]; then
  args=("$@")
  appended=0
  for i in "${!args[@]}"; do
    if [[ "${args[$i]}" == --node-args=* ]]; then
      args[$i]="${args[$i]} ${NODE_ARGS_APPEND}"
      appended=1
      break
    fi
  done
  if [ "${appended}" -eq 0 ]; then
    args+=("--node-args=${NODE_ARGS_APPEND}")
  fi
  set -- "${args[@]}"
fi

if [ "$(basename "${1:-}")" = "nodeworker" ] && ! nodeworker_arg_present "health.liveness-timeout" "$@"; then
  args=("$@")
  args+=("--health.liveness-timeout=${BDAG_NODEWORKER_LIVENESS_TIMEOUT:-5m}")
  set -- "${args[@]}"
fi

if [ "${BDAG_ENTRYPOINT_PRINT_NODE_FLAGS:-0}" = "1" ]; then
  printf 'NODE_ARGS_APPEND=%s\n' "${NODE_ARGS_APPEND:-}"
  exit 0
fi

start_node_metrics_exporter "$@"

if [ "$(id -u)" = 0 ]; then
  ensure_owned_runtime_dirs
  fix_ownership_if_needed
  exec runuser -u bdagStack -g bdagStack -- "$@"
fi
exec "$@"
