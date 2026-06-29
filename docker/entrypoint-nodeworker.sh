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

file_size_bytes() {
  local path="$1" size
  size="$(stat -c%s "$path" 2>/dev/null || printf '0')"
  case "$size" in
    ''|*[!0-9]*) size=0 ;;
  esac
  printf '%s\n' "$size"
}

snapshot_progress_interval_seconds() {
  local value="${BDAG_SNAPSHOT_PROGRESS_INTERVAL_SECONDS:-30}"
  case "$value" in
    ''|*[!0-9]*) value=30 ;;
  esac
  printf '%s\n' "$value"
}

format_duration_compact() {
  local seconds="$1" hours minutes
  case "$seconds" in
    ''|*[!0-9]*) printf 'unknown\n'; return 0 ;;
  esac
  hours=$(( seconds / 3600 ))
  minutes=$(( (seconds % 3600) / 60 ))
  seconds=$(( seconds % 60 ))
  if [ "$hours" -gt 0 ]; then
    printf '%sh%sm%ss\n' "$hours" "$minutes" "$seconds"
  elif [ "$minutes" -gt 0 ]; then
    printf '%sm%ss\n' "$minutes" "$seconds"
  else
    printf '%ss\n' "$seconds"
  fi
}

snapshot_download_total_bytes() {
  local url="$1" value
  value="${BDAG_SNAPSHOT_TOTAL_BYTES:-${BDAG_SNAPSHOT_EXPECTED_BYTES:-}}"
  case "$value" in
    ''|*[!0-9]*) value="" ;;
  esac
  if [ -n "$value" ]; then
    printf '%s\n' "$value"
    return 0
  fi
  value="$(
    curl --fail --location --silent --show-error --head --connect-timeout 20 "$url" 2>/dev/null \
      | awk -F: 'tolower($1) == "content-length" {
          value = $2
          gsub(/^[[:space:]]+|[[:space:]\r]+$/, "", value)
        } END { print value }' || true
  )"
  case "$value" in
    ''|*[!0-9]*) return 1 ;;
  esac
  printf '%s\n' "$value"
}

log_snapshot_download_measurement() {
  local event="$1" path="$2" start="$3" total_bytes="${4:-}" exit_code="${5:-}"
  local now elapsed size rate remaining eta eta_text percent_milli percent_text total_text remaining_text
  now="$(date +%s 2>/dev/null || echo "$start")"
  elapsed=$(( now - start ))
  [ "$elapsed" -ge 0 ] || elapsed=0
  size="$(file_size_bytes "$path")"
  if [ "$elapsed" -gt 0 ]; then
    rate=$(( size / elapsed ))
  else
    rate=0
  fi

  total_text="unknown"
  remaining_text="unknown"
  eta="unknown"
  eta_text="unknown"
  percent_text="unknown"
  case "$total_bytes" in
    ''|*[!0-9]*) ;;
    *)
      if [ "$total_bytes" -gt 0 ]; then
        total_text="$total_bytes"
        if [ "$size" -lt "$total_bytes" ]; then
          remaining=$(( total_bytes - size ))
        else
          remaining=0
        fi
        remaining_text="$remaining"
        percent_milli=$(( size * 100000 / total_bytes ))
        percent_text="$(printf '%d.%03d' "$(( percent_milli / 1000 ))" "$(( percent_milli % 1000 ))")"
        if [ "$rate" -gt 0 ]; then
          eta=$(( (remaining + rate - 1) / rate ))
          eta_text="$(format_duration_compact "$eta")"
        fi
      fi
      ;;
  esac

  if [ -n "$exit_code" ]; then
    log "snapshot download ${event}: curl_exit=${exit_code} downloaded_bytes=${size} downloaded_mib=$(( size / 1048576 )) total_bytes=${total_text} percent=${percent_text} remaining_bytes=${remaining_text} rate_bytes_per_second=${rate} eta_seconds=${eta} eta_text=${eta_text} elapsed_seconds=${elapsed} path=${path}"
  else
    log "snapshot download ${event}: downloaded_bytes=${size} downloaded_mib=$(( size / 1048576 )) total_bytes=${total_text} percent=${percent_text} remaining_bytes=${remaining_text} rate_bytes_per_second=${rate} eta_seconds=${eta} eta_text=${eta_text} elapsed_seconds=${elapsed} path=${path}"
  fi
}

snapshot_download_progress_monitor() {
  local path="$1" interval="$2" start="$3" total_bytes="${4:-}"
  [ "$interval" -gt 0 ] || return 0
  while true; do
    sleep "$interval" || return 0
    log_snapshot_download_measurement progress "$path" "$start" "$total_bytes"
  done
}

download_snapshot_with_progress() {
  local url="$1" out="$2" interval start curl_pid monitor_pid status total_bytes
  interval="$(snapshot_progress_interval_seconds)"
  total_bytes="$(snapshot_download_total_bytes "$url" || true)"
  start="$(date +%s 2>/dev/null || echo 0)"
  curl --fail --location --silent --show-error --connect-timeout 20 --retry 2 --retry-delay 2 -o "$out" "$url" &
  curl_pid="$!"
  monitor_pid=""
  if [ "$interval" -gt 0 ]; then
    snapshot_download_progress_monitor "$out" "$interval" "$start" "$total_bytes" &
    monitor_pid="$!"
  fi
  if wait "$curl_pid"; then
    status=0
  else
    status=$?
  fi
  if [ -n "$monitor_pid" ]; then
    kill "$monitor_pid" >/dev/null 2>&1 || true
    wait "$monitor_pid" >/dev/null 2>&1 || true
  fi
  if [ "$status" -eq 0 ]; then
    log_snapshot_download_measurement complete "$out" "$start" "$total_bytes"
  else
    log_snapshot_download_measurement failed "$out" "$start" "$total_bytes" "$status"
  fi
  return "$status"
}

tar_extract_archive_to_dir() {
  local path="$1" dest="$2"
  mkdir -p "$dest"
  if tar -xf "$path" -C "$dest" >/dev/null 2>&1; then
    return 0
  fi
  if tar -xzf "$path" -C "$dest" >/dev/null 2>&1; then
    return 0
  fi
  if command -v zstd >/dev/null 2>&1 && zstd -dc -- "$path" 2>/dev/null | tar -xf - -C "$dest" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

find_chain_datadir_payload_root() {
  local root="$1" candidate chain_dir
  for candidate in "$root" "$root/mainnet"; do
    if [ -d "$candidate/BdagChain" ] && [ -d "$candidate/bdageth" ] && [ -e "$candidate/metaData" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  while IFS= read -r chain_dir; do
    candidate="${chain_dir%/BdagChain}"
    if [ -d "$candidate/bdageth" ] && [ -e "$candidate/metaData" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done < <(find "$root" -maxdepth 4 -type d -name BdagChain -print 2>/dev/null || true)
  return 1
}

prepare_downloaded_chain_datadir_archive() {
  local path="$1" data_dir="$2" data_parent="$3" network="$4"
  local staging payload_root stamp quarantine item
  staging="${data_parent}/.http-chain-datadir-${network}.$$"
  rm -rf "$staging"
  if ! tar_extract_archive_to_dir "$path" "$staging"; then
    rm -rf "$staging"
    return 1
  fi
  payload_root="$(find_chain_datadir_payload_root "$staging" || true)"
  if [ -z "$payload_root" ]; then
    rm -rf "$staging"
    return 1
  fi

  log "downloaded snapshot is a chain datadir archive; preparing node datadir from ${path}"
  mkdir -p "$data_dir"
  stamp="$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null || date +%s)"
  quarantine="$data_dir/replaced-by-http-datadir-$stamp"
  for item in BdagChain bdageth metaData; do
    if [ -e "$data_dir/$item" ]; then
      mkdir -p "$quarantine"
      mv "$data_dir/$item" "$quarantine/$item" || {
        log "failed to quarantine existing $item before datadir archive restore"
        rm -rf "$staging"
        return 1
      }
    fi
  done
  for item in BdagChain bdageth metaData; do
    mv "$payload_root/$item" "$data_dir/$item" || {
      log "failed to install $item from downloaded datadir archive"
      rm -rf "$staging"
      return 1
    }
  done
  chown -R bdagStack:bdagStack "$data_dir" || true
  rm -rf "$staging"
  log "prepared downloaded chain datadir archive before node startup: data_dir=${data_dir}"
  return 0
}

json_string_value() {
  local key="$1"
  local file="$2"
  sed -n "s/.*\"$key\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" "$file" | head -n 1
}

node_start_guard() {
  [ "${BDAG_NODE_START_GUARD_ENABLED:-1}" = "1" ] || return 0
  local control_file state
  control_file="${BDAG_AUTOMATION_CONTROL_FILE:-/var/lib/bdagStack/runtime/automation-control.json}"
  if [ ! -r "$control_file" ]; then
    log "refusing to start node: automation control file is missing or unreadable at $control_file"
    exit 78
  fi
  state="$(json_string_value state "$control_file")"
  if [ "$state" != "normal" ]; then
    log "refusing to start node: automation control state is ${state:-unknown}"
    exit 78
  fi
}

lower_ascii() {
  printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]'
}

mainnet_only_network() {
  local requested="${1:-mainnet}"
  if [ -z "$requested" ]; then
    requested="mainnet"
  fi
  case "$(lower_ascii "$requested")" in
    mainnet)
      printf 'mainnet\n'
      ;;
    *)
      log "refusing non-mainnet snapshot network: $requested"
      exit 2
      ;;
  esac
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

network_datadir() {
  local data_parent="$1"
  local network="$2"
  case "$data_parent" in
    */"$network") printf '%s\n' "$data_parent" ;;
    *) printf '%s/%s\n' "$data_parent" "$network" ;;
  esac
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

rewrite_node_args_configfile() {
  local node_args="$1"
  local config_file="$2"
  local rewritten="" word skip_next=0 saw_configfile=0
  for word in $node_args; do
    if [ "$skip_next" = "1" ]; then
      skip_next=0
      continue
    fi
    case "$word" in
      --configfile=*)
        rewritten="${rewritten:+$rewritten }--configfile=$config_file"
        saw_configfile=1
        ;;
      --configfile)
        rewritten="${rewritten:+$rewritten }--configfile=$config_file"
        skip_next=1
        saw_configfile=1
        ;;
      *)
        rewritten="${rewritten:+$rewritten }$word"
        ;;
    esac
  done
  if [ "$saw_configfile" != "1" ]; then
    rewritten="${rewritten:+$rewritten }--configfile=$config_file"
  fi
  printf '%s\n' "$rewritten"
}

prepare_runtime_configfile() {
  RUNTIME_CONFIGFILE_NODE_ARGS=
  [ "$(id -u)" = 0 ] || return 0

  local node_args config_file runtime_config runtime_dir
  node_args="$(node_args_from_argv "$@" || true)"
  config_file="$(node_arg_value configfile "$node_args" || true)"
  [ -n "$config_file" ] || return 0

  if runuser -u bdagStack -g bdagStack -- test -r "$config_file" 2>/dev/null; then
    return 0
  fi

  if [ ! -r "$config_file" ]; then
    log "node config file is not readable: $config_file"
    exit 1
  fi

  runtime_config="${BDAG_RUNTIME_CONFIGFILE:-${BDAG_EPHEMERAL_DIR:-/run/bdag-ephemeral}/node.conf}"
  runtime_dir="$(dirname "$runtime_config")"
  mkdir -p "$runtime_dir"
  cp "$config_file" "$runtime_config"
  chown bdagStack:bdagStack "$runtime_config"
  chmod 0600 "$runtime_config"
  RUNTIME_CONFIGFILE_NODE_ARGS="$(rewrite_node_args_configfile "$node_args" "$runtime_config")"
  log "prepared bdagStack-readable node config copy: $runtime_config"
}

apply_node_mining_runtime_args() {
  case "${BDAG_ENABLE_NODE_MINING:-0}" in
    1|true|TRUE|yes|YES|on|ON) ;;
    *) return 0 ;;
  esac

  local node_args modules word mining_args
  node_args="$(node_args_from_argv "$@" || true)"
  modules="${BDAG_NODE_MODULES:-}"
  if [ -n "$modules" ]; then
    modules="$(printf '%s' "$modules" | tr ',' ' ')"
    for word in $modules; do
      [ -n "$word" ] || continue
      append_node_arg_once "--modules=${word}" "$node_args ${NODE_ARGS_APPEND:-}"
    done
  fi
  mining_args="${BDAG_NODE_MINING_ARGS:-}"
  if [ -z "$mining_args" ]; then
    local mining_addr
    if ! mining_addr="$(select_node_mining_address)"; then
      log "BDAG_ENABLE_NODE_MINING=1 but no valid non-zero mining address is configured; refusing to start mining template support"
      exit 1
    fi
    mining_args="--miner --miningaddr=${mining_addr}"
  fi
  for word in $mining_args; do
    case "$word" in
      --miningaddr=*) append_node_arg_prefix_once "$word" "$node_args ${NODE_ARGS_APPEND:-}" ;;
      --*) append_node_arg_once "$word" "$node_args ${NODE_ARGS_APPEND:-}" ;;
    esac
  done
  if env_value_true "${BDAG_NODE_MINING_NO_PENDING_TX:-1}"; then
    append_node_arg_once "--miningnopendingtx" "$node_args ${NODE_ARGS_APPEND:-}"
  fi
}

is_nonzero_eth_address() {
  local value lower zero
  value="${1:-}"
  [[ "$value" =~ ^0[xX][0-9a-fA-F]{40}$ ]] || return 1
  lower="$(lower_ascii "$value")"
  zero="0x0000000000000000000000000000000000000000"
  [[ "$lower" != "$zero" ]]
}

select_node_mining_address() {
  local key value
  for key in POOL_COINBASE_ADDRESS MINING_POOL_ADDRESS MINING_ADDRESS; do
    value="${!key-}"
    if is_nonzero_eth_address "$value"; then
      printf '%s\n' "$value"
      return 0
    fi
  done
  return 1
}

env_value_true() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON|enabled|ENABLED) return 0 ;;
  esac
  return 1
}

apply_archival_flag() {
  case "${BDAG_NODE_ARCHIVAL:-0}" in
    1|true|True|yes) ;;
    *) return 0 ;;
  esac
  local node_args node_binary
  node_args="$(node_args_from_argv "$@" || true)"
  node_binary="$(node_binary_from_argv "$@" || true)"
  if [ -n "$node_binary" ] && [ -x "$node_binary" ] && ! "$node_binary" --help 2>&1 | grep -q -- '--archival'; then
    log "native archival flag requested but unsupported by $node_binary; relying on EVM --gcmode=archive"
    return 0
  fi
  append_node_arg_once "--archival" "$node_args ${NODE_ARGS_APPEND:-}"
  log "archival mode enabled; node keeps full block history (--archival)"
}

strip_wrapping_quotes() {
  local value="${1:-}"
  case "$value" in
    \"*\")
      value="${value#\"}"
      value="${value%\"}"
      ;;
    \'*\')
      value="${value#\'}"
      value="${value%\'}"
      ;;
  esac
  printf '%s\n' "$value"
}

evm_args_without_gcmode() {
  local evm_args="$1" word skip_next=0 rewritten=""
  for word in $evm_args; do
    if [ "$skip_next" = "1" ]; then
      skip_next=0
      continue
    fi
    case "$word" in
      --gcmode)
        skip_next=1
        ;;
      --gcmode=*)
        ;;
      *)
        rewritten="${rewritten:+$rewritten }$word"
        ;;
    esac
  done
  printf '%s\n' "$rewritten"
}

apply_evm_runtime_args() {
  local node_args config_file evm_args gcmode
  node_args="$(node_args_from_argv "$@" || true)"
  config_file="$(node_arg_value configfile "$node_args" || true)"
  evm_args="${BDAG_EVM_ARGS:-${BDAG_NODE_EVM_ARGS:-}}"
  if [ -z "$evm_args" ] && [ -n "$config_file" ]; then
    evm_args="$(read_config_value "$config_file" evmenv || true)"
  fi
  evm_args="$(strip_wrapping_quotes "$evm_args")"

  gcmode="${BDAG_EVM_GCMODE:-}"
  if [ -z "$gcmode" ] && env_value_true "${BDAG_NODE_ARCHIVAL:-0}"; then
    gcmode="archive"
  fi
  if [ -n "$gcmode" ]; then
    case "$(lower_ascii "$gcmode")" in
      archive|full) gcmode="$(lower_ascii "$gcmode")" ;;
      *)
        log "refusing invalid BDAG_EVM_GCMODE=$gcmode; expected archive or full"
        exit 1
        ;;
    esac
    evm_args="$(evm_args_without_gcmode "$evm_args")"
    evm_args="${evm_args:+$evm_args }--gcmode=$gcmode"
  fi

  [ -n "$evm_args" ] || return 0
  case "$evm_args" in
    *\"*|*$'\n'*|*$'\r'*)
      log "refusing unsafe EVM args containing quotes or newlines"
      exit 1
      ;;
  esac
  append_node_arg_prefix_once "--evmenv=\"$evm_args\"" "$node_args ${NODE_ARGS_APPEND:-}"
  if [ "$gcmode" = "archive" ]; then
    log "EVM archive mode enabled; evmenv includes --gcmode=archive"
  fi
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

chain_datadir_complete() {
  local data_dir="$1"
  [ -d "$data_dir/BdagChain" ] && [ -d "$data_dir/bdageth" ] && [ -e "$data_dir/metaData" ]
}

chain_datadir_has_any_payload() {
  local data_dir="$1"
  [ -e "$data_dir/BdagChain" ] || [ -e "$data_dir/bdageth" ] || [ -e "$data_dir/metaData" ]
}

validate_chain_datadir_state() {
  local data_dir="$1" network="$2"
  if [ -d "$data_dir/$network/BdagChain" ] || [ -d "$data_dir/$network/bdageth" ]; then
    log "refusing nested chain datadir at $data_dir/$network; NODE_DATA_DIR must mount the parent that directly contains $network"
    exit 78
  fi
  if chain_datadir_complete "$data_dir"; then
    return 0
  fi
  if ! chain_datadir_has_any_payload "$data_dir"; then
    return 0
  fi
  if env_value_true "${BDAG_ALLOW_PARTIAL_CHAIN_DATADIR_BOOTSTRAP:-0}"; then
    log "partial chain datadir allowed by BDAG_ALLOW_PARTIAL_CHAIN_DATADIR_BOOTSTRAP=1: data_dir=$data_dir"
    return 0
  fi
  log "refusing partial chain datadir: data_dir=$data_dir requires BdagChain, bdageth, and metaData together; quarantine or repair it before node startup"
  exit 78
}

# Bootstrap chain data from an HTTP(S) snapshot link before node startup.
# Order of precedence on an empty datadir: locally staged snapshot.bdsnap,
# then BDAG_SNAPSHOT_URL download, then normal node sync.
maybe_http_snapshot_bootstrap() {
  if [ "${BDAG_ENTRYPOINT_PRINT_NODE_FLAGS:-0}" = "1" ]; then
    return 0
  fi

  local node_binary
  node_binary="$(node_binary_from_argv "$@" || true)"
  node_binary="${BDAG_SNAPSHOT_NODE_BINARY:-${node_binary:-/usr/local/bin/blockdag-node}}"
  [ -x "$node_binary" ] || {
    log "node binary missing at $node_binary; skipping snapshot bootstrap"
    return 0
  }

  local node_args network config_file data_parent data_dir archive tmp min_bytes size
  node_args="$(node_args_from_argv "$@" || true)"
  network="$(mainnet_only_network "${BDAG_SNAPSHOT_NETWORK:-mainnet}")"
  config_file="$(node_arg_value configfile "$node_args" || true)"
  data_parent="${BDAG_SNAPSHOT_DATADIR:-$(node_arg_value datadir "$node_args" || true)}"
  if [ -z "$data_parent" ] && [ -n "$config_file" ]; then
    data_parent="$(read_config_value "$config_file" datadir || true)"
  fi
  data_parent="${data_parent:-/var/lib/bdagStack/node}"
  data_dir="$(network_datadir "$data_parent" "$network")"

  validate_chain_datadir_state "$data_dir" "$network"
  if chain_datadir_complete "$data_dir"; then
    return 0
  fi

  archive="$data_dir/snapshot.bdsnap"
  mkdir -p "$data_dir"
  if [ -s "$archive" ]; then
    log "importing staged snapshot before node startup: $archive"
    if ! "$node_binary" snap import --datadir "$data_dir" --path "$archive"; then
      log "staged snapshot import failed; continuing with normal sync"
    fi
    return 0
  fi

  [ -n "${BDAG_SNAPSHOT_URL:-}" ] || return 0
  command -v curl >/dev/null 2>&1 || {
    log "curl missing; skipping HTTP snapshot download"
    return 0
  }

  min_bytes="${BDAG_SNAPSHOT_MIN_BYTES:-1048576}"
  tmp="$archive.download.$$"
  log "no chain data found; downloading snapshot from ${BDAG_SNAPSHOT_URL}"
  if ! download_snapshot_with_progress "$BDAG_SNAPSHOT_URL" "$tmp"; then
    rm -f "$tmp"
    log "snapshot download failed; continuing with normal node sync"
    return 0
  fi
  size="$(file_size_bytes "$tmp")"
  if [ "$size" -lt "$min_bytes" ]; then
    rm -f "$tmp"
    log "downloaded snapshot too small ($size bytes < $min_bytes); continuing with normal node sync"
    return 0
  fi
  if prepare_downloaded_chain_datadir_archive "$tmp" "$data_dir" "$data_parent" "$network"; then
    rm -f "$tmp"
    return 0
  fi
  mv "$tmp" "$archive"
  log "importing downloaded snapshot before node startup ($size bytes)"
  if ! "$node_binary" snap import --datadir "$data_dir" --path "$archive"; then
    rm -f "$archive"
    log "downloaded snapshot import failed; continuing with normal sync"
  fi
}

node_start_guard
apply_node_metrics_runtime_args "$@"
apply_node_mining_runtime_args "$@"
apply_archival_flag "$@"
apply_evm_runtime_args "$@"
maybe_http_snapshot_bootstrap "$@"

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

if [ "$(basename "${1:-}")" = "nodeworker" ] && ! nodeworker_arg_present "health.probe-interval" "$@"; then
  args=("$@")
  args+=("--health.probe-interval=${BDAG_NODEWORKER_HEALTH_PROBE_INTERVAL:-2s}")
  set -- "${args[@]}"
fi

if [ "$(basename "${1:-}")" = "nodeworker" ] && ! nodeworker_arg_present "health.mining-readiness-timeout" "$@"; then
  args=("$@")
  args+=("--health.mining-readiness-timeout=${BDAG_NODEWORKER_MINING_READINESS_TIMEOUT:-30m}")
  set -- "${args[@]}"
fi

if [ "$(basename "${1:-}")" = "nodeworker" ] && ! nodeworker_arg_present "health.mining-readiness-grace" "$@"; then
  args=("$@")
  args+=("--health.mining-readiness-grace=${BDAG_NODEWORKER_MINING_READINESS_GRACE:-20m}")
  set -- "${args[@]}"
fi

if [ "${BDAG_ENTRYPOINT_PRINT_NODE_FLAGS:-0}" = "1" ]; then
  printf 'NODE_ARGS_APPEND=%s\n' "${NODE_ARGS_APPEND:-}"
  printf 'NODEWORKER_ARGS=%s\n' "$*"
  exit 0
fi

if [ "$(id -u)" = 0 ]; then
  ensure_owned_runtime_dirs
  fix_ownership_if_needed
  prepare_runtime_configfile "$@"
  if [ -n "${RUNTIME_CONFIGFILE_NODE_ARGS:-}" ]; then
    args=("$@")
    for i in "${!args[@]}"; do
      if [[ "${args[$i]}" == --node-args=* ]]; then
        args[$i]="--node-args=${RUNTIME_CONFIGFILE_NODE_ARGS}"
        set -- "${args[@]}"
        break
      fi
    done
  fi
  exec runuser -u bdagStack -g bdagStack -- "$@"
fi
exec "$@"
