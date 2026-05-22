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
  local config_file data_parent data_dir archive min_tip timeout peers peer tmp_archive
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
  IFS=', '
  for peer in $peers; do
    [ -n "$peer" ] || continue
    log "trying P2P snapshot bootstrap from $peer"
    if "$fastsnap_bin" --peer "$peer" --out "$tmp_archive" --network "$network" --min-tip "$min_tip" --timeout "$timeout"; then
      mv "$tmp_archive" "$archive"
      if [ -f "$tmp_archive.manifest.json" ]; then
        mv "$tmp_archive.manifest.json" "$archive.manifest.json"
      fi
      log "importing downloaded P2P snapshot before node startup"
      "$node_binary" snap import --datadir "$data_dir" --path "$archive"
      IFS="$old_ifs"
      return 0
    fi
    rm -f "$tmp_archive" "$tmp_archive.manifest.json"
  done
  IFS="$old_ifs"

  if [ "${BDAG_FASTSNAP_REQUIRED:-0}" = "1" ]; then
    log "required P2P snapshot bootstrap failed"
    exit 1
  fi
  log "P2P snapshot bootstrap unavailable; falling back to normal FastSync/legacy sync"
}

if [ "$(id -u)" = 0 ]; then
  mkdir -p /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack
  chown -R bdagStack:bdagStack /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack || true
  maybe_fastsnap_bootstrap "$@"
  chown -R bdagStack:bdagStack /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack || true
  exec runuser -u bdagStack -g bdagStack -- "$@"
fi
maybe_fastsnap_bootstrap "$@"
exec "$@"
