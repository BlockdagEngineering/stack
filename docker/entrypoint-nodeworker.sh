#!/usr/bin/env bash
# Fix ownership of persisted paths on every container start (named/bind volumes
# are often populated as root → bdagStack cannot open bdageth/chaindata/ancient/*).
set -euo pipefail

log() {
  printf '[%s] node-entrypoint: %s\n' "$(date -Is)" "$*" >&2
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

peer_host() {
  local peer="$1"
  case "$peer" in
    /ip4/*)
      peer="${peer#/ip4/}"
      printf '%s\n' "${peer%%/*}"
      ;;
    *)
      return 1
      ;;
  esac
}

host_matches_prefixes() {
  local host="$1"
  local prefixes="$2"
  local old_ifs="$IFS"
  local prefix
  IFS=', '
  for prefix in $prefixes; do
    [ -n "$prefix" ] || continue
    case "$host" in
      "$prefix"*) IFS="$old_ifs"; return 0 ;;
    esac
  done
  IFS="$old_ifs"
  return 1
}

host_is_private_or_vpn() {
  local host="$1"
  case "$host" in
    10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*|100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*)
      return 0
      ;;
  esac
  return 1
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

classify_peer_list() {
  local raw="$1"
  local old_ifs="$IFS"
  local peer host
  IFS=', '
  for peer in $raw; do
    [ -n "$peer" ] || continue
    host="$(peer_host "$peer" || true)"
    if [ -n "$host" ] && host_matches_prefixes "$host" "${BDAG_FASTSYNC_LAN_PREFIXES:-192.168.}"; then
      append_unique_peer fastsync_lan_peers "$peer"
    elif [ -n "$host" ] && host_is_private_or_vpn "$host"; then
      append_unique_peer fastsync_vpn_peers "$peer"
    else
      append_unique_peer fastsync_public_peers "$peer"
    fi
  done
  IFS="$old_ifs"
}

join_peer_arrays() {
  local old_ifs="$IFS"
  local joined
  IFS=,
  joined="${fastsync_lan_peers[*]:-}"
  if [ "${#fastsync_vpn_peers[@]}" -gt 0 ]; then
    joined="${joined:+$joined,}${fastsync_vpn_peers[*]}"
  fi
  if [ "${#fastsync_public_peers[@]}" -gt 0 ]; then
    joined="${joined:+$joined,}${fastsync_public_peers[*]}"
  fi
  IFS="$old_ifs"
  printf '%s\n' "$joined"
}

ordered_fastsync_peers() {
  local node_args="$1"
  local config_file config_peers generic_peers
  fastsync_lan_peers=()
  fastsync_vpn_peers=()
  fastsync_public_peers=()
  ORDERED_FASTSYNC_SEEN=

  config_file="$(node_arg_value configfile "$node_args" || true)"
  config_file="${config_file:-/etc/bdagStack/node.conf}"
  config_peers="$(config_addpeer_values "$config_file" | paste -sd, - || true)"

  append_peer_list fastsync_lan_peers "${BDAG_FASTSYNC_LAN_PEERS:-${BDAG_FASTSYNC_LOCAL_PEERS:-}}"
  append_peer_list fastsync_vpn_peers "${BDAG_FASTSYNC_VPN_PEERS:-${BDAG_FASTSYNC_PRIVATE_PEERS:-}}"
  append_peer_list fastsync_public_peers "${BDAG_FASTSYNC_PUBLIC_PEERS:-}"
  generic_peers="${BDAG_FASTSYNC_PEERS:-} ${BDAG_FASTSNAP_PEERS:-} ${BOOTSTRAP_PEER_ADDRESSES:-} $config_peers $(addpeer_values "$node_args" | paste -sd, - || true)"
  classify_peer_list "$generic_peers"

  join_peer_arrays
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
  if [ "${BDAG_FASTSYNC_PEER_ORDERING:-1}" != "1" ]; then
    return 0
  fi

  local node_args ordered addpeer_args total_count
  node_args="$(node_args_from_argv "$@" || true)"
  ordered="$(ordered_fastsync_peers "$node_args")"
  [ -n "$ordered" ] || return 0

  export BDAG_FASTSNAP_PEERS="$ordered"
  total_count="$(printf '%s' "$ordered" | awk -F, '{print NF}')"
  log "ordered FastSync candidates enabled: LAN first, private/VPN second, public last; total=${total_count}"

  if [ "${BDAG_FASTSYNC_APPEND_ADDPEERS:-1}" = "1" ]; then
    addpeer_args="$(addpeer_args_from_csv "$ordered")"
    NODE_ARGS_APPEND="${addpeer_args}${NODE_ARGS_APPEND:+ $NODE_ARGS_APPEND}"
    export NODE_ARGS_APPEND
  fi
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
  local config_file data_parent data_dir archive min_tip timeout peers peer tmp_archive ledger
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
  rm -f "$tmp_archive" "$tmp_archive.manifest.json"

  local old_ifs="$IFS"
  local fastsnap_args signer peer_count
  fastsnap_args=(
    --out "$tmp_archive"
    --network "$network"
    --min-tip "$min_tip"
    --timeout "$timeout"
  )
  peer_count=0
  IFS=', '
  for peer in $peers; do
    [ -n "$peer" ] || continue
    fastsnap_args+=(--peer "$peer")
    peer_count=$((peer_count + 1))
  done
  IFS="$old_ifs"
  if [ "$peer_count" -eq 0 ]; then
    log "no valid P2P snapshot peers configured; normal FastSync/legacy sync will start"
    return 0
  fi
  log "trying P2P snapshot bootstrap from $peer_count ordered peer candidate(s)"

  if [ "${BDAG_FASTSNAP_ARTIFACT_V2:-1}" = "0" ]; then
    fastsnap_args+=(--artifact-v2=false)
  fi
  if [ "${BDAG_FASTSNAP_ALLOW_UNSIGNED:-0}" = "1" ]; then
    fastsnap_args+=(--allow-unsigned)
  fi
  if [ "${BDAG_FASTSNAP_DISCOVERY:-1}" = "1" ]; then
    fastsnap_args+=(--discover)
    if [ -n "${BDAG_FASTSNAP_DISCOVERY_LIMIT:-}" ]; then
      fastsnap_args+=(--discover-limit "$BDAG_FASTSNAP_DISCOVERY_LIMIT")
    fi
    if [ -n "${BDAG_FASTSNAP_DISCOVERY_TIMEOUT:-}" ]; then
      fastsnap_args+=(--discover-timeout "$BDAG_FASTSNAP_DISCOVERY_TIMEOUT")
    fi
  fi
  if [ -n "${BDAG_FASTSNAP_PARALLELISM:-}" ]; then
    fastsnap_args+=(--parallelism "$BDAG_FASTSNAP_PARALLELISM")
  fi
  ledger="${BDAG_FASTSNAP_LEDGER:-$archive.artifact-ledger.json}"
  fastsnap_args+=(--ledger "$ledger")
  IFS=', '
  for signer in ${BDAG_FASTSNAP_TRUSTED_SIGNERS:-}; do
    [ -n "$signer" ] && fastsnap_args+=(--trusted-signer "$signer")
  done
  IFS="$old_ifs"

  if "$fastsnap_bin" "${fastsnap_args[@]}"; then
    mv "$tmp_archive" "$archive"
    if [ -f "$tmp_archive.manifest.json" ]; then
      mv "$tmp_archive.manifest.json" "$archive.manifest.json"
    fi
    log "importing downloaded P2P snapshot before node startup"
    "$node_binary" snap import --datadir "$data_dir" --path "$archive"
    return 0
  fi
  rm -f "$tmp_archive" "$tmp_archive.manifest.json"

  if [ "${BDAG_FASTSNAP_REQUIRED:-0}" = "1" ]; then
    log "required P2P snapshot bootstrap failed"
    exit 1
  fi
  log "P2P snapshot bootstrap unavailable; falling back to normal FastSync/legacy sync"
}

apply_ordered_fastsync_peers "$@"

NODE_ARGS_APPEND_COMBINED="${NODE_ARGS_APPEND:-}"
if [ -n "${BDAG_NODE_FASTARTIFACT_ARGS:---fastartifactsync}" ]; then
  NODE_ARGS_APPEND_COMBINED="${NODE_ARGS_APPEND_COMBINED:+$NODE_ARGS_APPEND_COMBINED }${BDAG_NODE_FASTARTIFACT_ARGS:---fastartifactsync}"
fi
if [ -n "${BDAG_NODE_P2P_ADVERTISE_ARGS:-}" ]; then
  NODE_ARGS_APPEND_COMBINED="${NODE_ARGS_APPEND_COMBINED:+$NODE_ARGS_APPEND_COMBINED }${BDAG_NODE_P2P_ADVERTISE_ARGS}"
fi

if [ -n "$NODE_ARGS_APPEND_COMBINED" ]; then
  args=("$@")
  appended=0
  for i in "${!args[@]}"; do
    if [[ "${args[$i]}" == --node-args=* ]]; then
      args[$i]="${args[$i]} ${NODE_ARGS_APPEND_COMBINED}"
      appended=1
      break
    fi
  done
  if [ "${appended}" -eq 0 ]; then
    args+=("--node-args=${NODE_ARGS_APPEND_COMBINED}")
  fi
  set -- "${args[@]}"
fi

if [ "${BDAG_FASTSYNC_PRINT_ORDERED_PEERS:-0}" = "1" ]; then
  printf '%s\n' "${BDAG_FASTSNAP_PEERS:-}"
  exit 0
fi

if [ "$(id -u)" = 0 ]; then
  mkdir -p /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack
  chown -R bdagStack:bdagStack /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack || true
  maybe_fastsnap_bootstrap "$@"
  chown -R bdagStack:bdagStack /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack || true
  exec runuser -u bdagStack -g bdagStack -- "$@"
fi
maybe_fastsnap_bootstrap "$@"
exec "$@"
