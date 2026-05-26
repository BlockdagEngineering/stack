#!/usr/bin/env bash
# Fix ownership of persisted paths on every container start (named/bind volumes
# are often populated as root → bdagStack cannot open bdageth/chaindata/ancient/*).
set -euo pipefail

log() {
  printf '[%s] node-entrypoint: %s\n' "$(date -Is)" "$*" >&2
}

FASTSNAP_BOOTSTRAP_MUTATED=0

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

nodeworker_arg_value() {
  local key="$1"
  shift
  local arg
  for arg in "$@"; do
    case "$arg" in
      --"$key"=*)
        printf '%s\n' "${arg#*=}"
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
    append_unique_peer "$bucket_name" "$peer"
  done
  IFS="$old_ifs"
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
  local ordering="${BDAG_FASTSYNC_PEER_ORDERING:-latency}"
  local config_file config_peers generic_peers
  fastsync_peers=()
  ORDERED_FASTSYNC_SEEN=

  config_file="$(node_arg_value configfile "$node_args" || true)"
  config_file="${config_file:-/etc/bdagStack/node.conf}"
  config_peers="$(config_addpeer_values "$config_file" | paste -sd, - || true)"

  if [ "$ordering" = "legacy-buckets" ] || [ "$ordering" = "buckets" ]; then
    generic_peers="${BDAG_FASTSYNC_LAN_PEERS:-${BDAG_FASTSYNC_LOCAL_PEERS:-}} ${BDAG_FASTSYNC_VPN_PEERS:-${BDAG_FASTSYNC_PRIVATE_PEERS:-}} ${BDAG_FASTSYNC_PUBLIC_PEERS:-} ${BDAG_FASTSYNC_PEERS:-} ${BDAG_FASTSNAP_PEERS:-} ${BOOTSTRAP_PEER_ADDRESSES:-} $config_peers $(addpeer_values "$node_args" | paste -sd, - || true)"
  else
    generic_peers="${BDAG_FASTSYNC_PEERS:-} ${BDAG_FASTSNAP_PEERS:-} ${BOOTSTRAP_PEER_ADDRESSES:-} $config_peers $(addpeer_values "$node_args" | paste -sd, - || true) ${BDAG_FASTSYNC_LAN_PEERS:-${BDAG_FASTSYNC_LOCAL_PEERS:-}} ${BDAG_FASTSYNC_VPN_PEERS:-${BDAG_FASTSYNC_PRIVATE_PEERS:-}} ${BDAG_FASTSYNC_PUBLIC_PEERS:-}"
  fi
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

apply_ordered_fastsync_peers() {
  case "${BDAG_FASTSYNC_PEER_ORDERING:-latency}" in
    0|off|false|none) return 0 ;;
  esac

  local node_args ordered addpeer_args total_count ordering
  ordering="${BDAG_FASTSYNC_PEER_ORDERING:-latency}"
  node_args="$(node_args_from_argv "$@" || true)"
  ordered="$(ordered_fastsync_peers "$node_args")"
  [ -n "$ordered" ] || return 0

  export BDAG_FASTSNAP_PEERS="$ordered"
  total_count="$(printf '%s' "$ordered" | awk -F, '{print NF}')"
  if [ "$ordering" = "legacy-buckets" ] || [ "$ordering" = "buckets" ]; then
    log "legacy bucket FastSync candidates enabled; total=${total_count}"
  else
    log "latency-first FastSync candidates enabled; total=${total_count}"
  fi

  if [ "${BDAG_FASTSYNC_APPEND_ADDPEERS:-1}" = "1" ]; then
    addpeer_args="$(addpeer_args_from_csv "$ordered")"
    NODE_ARGS_APPEND="${addpeer_args}${NODE_ARGS_APPEND:+ $NODE_ARGS_APPEND}"
    export NODE_ARGS_APPEND
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

apply_default_fastsync_flags() {
  if [ "${BDAG_FASTARTIFACTSYNC_ENABLED:-1}" != "1" ]; then
    return 0
  fi

  local node_args
  node_args="$(node_args_from_argv "$@" || true)"
  append_node_arg_once "--fastartifactsync" "$node_args ${NODE_ARGS_APPEND:-}"
}

fastsnap_supports_directory_mode() {
  local fastsnap_bin="$1"
  "$fastsnap_bin" --help 2>&1 | grep -q -- "--dir-out"
}

maybe_fastsnap_bootstrap() {
  if [ "${BDAG_FASTSNAP_ENABLED:-1}" != "1" ]; then
    return 0
  fi

  local fastsnap_bin="${BDAG_FASTSNAP_BINARY:-/usr/local/bin/fastsnap}"
  [ -x "$fastsnap_bin" ] || {
    log "fastsnap binary missing; skipping P2P snapshot bootstrap"
    return 0
  }

  local node_binary
  node_binary="$(nodeworker_arg_value node-binary "$@" || true)"
  node_binary="${BDAG_FASTSNAP_NODE_BINARY:-${node_binary:-/usr/local/bin/blockdag-node}}"
  [ -x "$node_binary" ] || {
    log "node binary missing at $node_binary; skipping P2P snapshot bootstrap"
    return 0
  }

  local node_args
  node_args="$(node_args_from_argv "$@" || true)"
  local network="${BDAG_FASTSNAP_NETWORK:-mainnet}"
  local config_file data_parent data_dir archive min_tip timeout peers peer tmp_archive tmp_dir directory_mode
  config_file="$(node_arg_value configfile "$node_args" || true)"
  data_parent="${BDAG_FASTSNAP_DATADIR:-$(node_arg_value datadir "$node_args" || true)}"
  if [ -z "$data_parent" ] && [ -n "$config_file" ]; then
    data_parent="$(read_config_value "$config_file" datadir || true)"
  fi
  data_parent="${data_parent:-/var/lib/bdagStack/node}"
  data_dir="$(network_datadir "$data_parent" "$network")"

  if [ -d "$data_dir/BdagChain" ]; then
    return 0
  fi

  archive="$data_dir/snapshot.bdsnap"
  mkdir -p "$data_dir"
  if [ -s "$archive" ]; then
    log "importing existing P2P snapshot archive before node startup: $archive"
    FASTSNAP_BOOTSTRAP_MUTATED=1
    "$node_binary" snap import --datadir "$data_dir" --path "$archive"
    return 0
  fi

  peers="${BDAG_FASTSNAP_PEERS:-${BOOTSTRAP_PEER_ADDRESSES:-}}"
  if [ -z "$peers" ]; then
    peers="$(addpeer_values "$node_args" | paste -sd, -)"
  fi
  if [ -z "$peers" ]; then
    log "no P2P snapshot peers configured; normal FastSync/legacy sync will start"
    return 0
  fi

  min_tip="${BDAG_FASTSNAP_MIN_TIP:-0}"
  timeout="${BDAG_FASTSNAP_TIMEOUT:-90s}"
  tmp_archive="$archive.download.$$"
  directory_mode="${BDAG_FASTSNAP_DIRECTORY_MODE:-1}"
  if [ "$directory_mode" = "1" ] && ! fastsnap_supports_directory_mode "$fastsnap_bin"; then
    log "fastsnap binary does not support directory install flags; using V2 archive fallback"
    directory_mode=0
  fi
  tmp_dir="${BDAG_FASTSNAP_DIRECTORY_STAGING:-$data_parent/.fastsnap-directory-$network.$$}"
  rm -f "$tmp_archive" "$tmp_archive.manifest.json"
  rm -rf "$tmp_dir" "$tmp_dir.manifest.json"

  local fastsnap_args=(
    --out "$tmp_archive"
    --network "$network"
    --min-tip "$min_tip"
    --timeout "$timeout"
  )
  if [ "$directory_mode" = "1" ]; then
    fastsnap_args+=(--dir-out "$tmp_dir" --install-dir "$data_dir")
    if [ "${BDAG_FASTSNAP_DIRECTORY_REPLACE_EXISTING:-1}" = "1" ]; then
      fastsnap_args+=(--replace-existing)
    fi
    if [ "${BDAG_FASTSNAP_DIRECTORY_MOVE_STAGING:-1}" = "1" ]; then
      fastsnap_args+=(--move-staging)
    fi
  fi
  local old_ifs="$IFS"
  IFS=', '
  for peer in $peers; do
    [ -n "$peer" ] || continue
    fastsnap_args+=(--peer "$peer")
  done
  IFS="$old_ifs"

  if [ "${BDAG_FASTSNAP_ARTIFACT_V2:-1}" = "0" ]; then
    fastsnap_args+=(--artifact-v2=false)
  fi
  if [ "${BDAG_FASTSNAP_ALLOW_UNSIGNED:-0}" = "1" ]; then
    fastsnap_args+=(--allow-unsigned)
  fi
  if [ -n "${BDAG_FASTSNAP_PARALLELISM:-}" ]; then
    fastsnap_args+=(--parallelism "$BDAG_FASTSNAP_PARALLELISM")
  fi
  if [ -n "${BDAG_FASTSNAP_LEDGER:-}" ]; then
    fastsnap_args+=(--ledger "$BDAG_FASTSNAP_LEDGER")
  fi

  log "trying P2P snapshot bootstrap with libp2p latency-first peer selection"
  if "$fastsnap_bin" "${fastsnap_args[@]}"; then
    if [ -d "$data_dir/BdagChain" ]; then
      if [ -f "$tmp_dir.manifest.json" ]; then
        mv "$tmp_dir.manifest.json" "$data_dir/artifact.manifest.json"
      fi
      rm -f "$tmp_archive" "$tmp_archive.manifest.json"
      rm -rf "$tmp_dir"
      log "downloaded and installed P2P directory artifact before node startup"
      FASTSNAP_BOOTSTRAP_MUTATED=1
      return 0
    fi
    if [ ! -s "$tmp_archive" ]; then
      log "fastsnap completed but did not install chain data or produce an archive"
      rm -f "$tmp_archive" "$tmp_archive.manifest.json"
      rm -rf "$tmp_dir" "$tmp_dir.manifest.json"
      if [ "${BDAG_FASTSNAP_REQUIRED:-0}" = "1" ]; then
        log "required P2P snapshot bootstrap failed"
        exit 1
      fi
      log "P2P snapshot bootstrap unavailable; falling back to normal FastSync/legacy sync"
      return 0
    fi
    mv "$tmp_archive" "$archive"
    if [ -f "$tmp_archive.manifest.json" ]; then
      mv "$tmp_archive.manifest.json" "$archive.manifest.json"
    fi
    log "importing downloaded P2P snapshot before node startup"
    FASTSNAP_BOOTSTRAP_MUTATED=1
    "$node_binary" snap import --datadir "$data_dir" --path "$archive"
    rm -rf "$tmp_dir" "$tmp_dir.manifest.json"
    return 0
  fi
  rm -f "$tmp_archive" "$tmp_archive.manifest.json"
  rm -rf "$tmp_dir" "$tmp_dir.manifest.json"

  if [ "${BDAG_FASTSNAP_REQUIRED:-0}" = "1" ]; then
    log "required P2P snapshot bootstrap failed"
    exit 1
  fi
  log "P2P snapshot bootstrap unavailable; falling back to normal FastSync/legacy sync"
}

configure_directory_artifact_serving() {
  if [ -n "${BDAG_FASTSYNC_ARTIFACT_DIRECTORY:-}" ] || [ -n "${BDAG_FASTSYNC_ARTIFACT_MANIFEST:-}" ]; then
    return 0
  fi
  local node_args network config_file data_parent data_dir manifest
  node_args="$(node_args_from_argv "$@" || true)"
  network="${BDAG_FASTSNAP_NETWORK:-mainnet}"
  config_file="$(node_arg_value configfile "$node_args" || true)"
  data_parent="${BDAG_FASTSNAP_DATADIR:-$(node_arg_value datadir "$node_args" || true)}"
  if [ -z "$data_parent" ] && [ -n "$config_file" ]; then
    data_parent="$(read_config_value "$config_file" datadir || true)"
  fi
  data_parent="${data_parent:-/var/lib/bdagStack/node}"
  data_dir="$(network_datadir "$data_parent" "$network")"
  manifest="$data_dir/artifact.manifest.json"
  if [ -s "$manifest" ] && [ -d "$data_dir/BdagChain" ]; then
    export BDAG_FASTSYNC_ARTIFACT_DIRECTORY="$data_dir"
    export BDAG_FASTSYNC_ARTIFACT_MANIFEST="$manifest"
    log "enabled Fast Artifact Sync V2 directory serving from $data_dir"
  else
    log "Fast Artifact Sync V2 directory manifest unavailable at $manifest; using archive/legacy serving fallback"
  fi
}

apply_ordered_fastsync_peers "$@"
apply_default_fastsync_flags "$@"

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

if [ "${BDAG_FASTSYNC_PRINT_ORDERED_PEERS:-0}" = "1" ]; then
  printf '%s\n' "${BDAG_FASTSNAP_PEERS:-}"
  exit 0
fi

if [ "$(id -u)" = 0 ]; then
  ensure_owned_runtime_dirs
  fix_ownership_if_needed
  maybe_fastsnap_bootstrap "$@"
  configure_directory_artifact_serving "$@"
  if [ "$FASTSNAP_BOOTSTRAP_MUTATED" = "1" ]; then
    fix_ownership_if_needed
  fi
  exec runuser -u bdagStack -g bdagStack -- "$@"
fi
maybe_fastsnap_bootstrap "$@"
configure_directory_artifact_serving "$@"
exec "$@"
