#!/bin/sh
# Fix ownership of persisted paths on every container start (named/bind volumes
# are often populated as root → bdagStack cannot open bdageth/chaindata/ancient/*).
set -eu

run_fastsnap_bootstrap() (
  set -eu
  case "${BDAG_FASTSNAP_ENABLED:-0}" in
    1|true|TRUE|yes|YES|on|ON) ;;
    *) exit 0 ;;
  esac

  fastsnap_required=0
  case "${BDAG_FASTSNAP_REQUIRED:-0}" in
    1|true|TRUE|yes|YES|on|ON) fastsnap_required=1 ;;
  esac

  peers="${BDAG_FASTSNAP_PEERS:-}"
  if [ -z "$peers" ] && [ -f "${BDAG_FASTSNAP_PEERS_FILE:-/etc/bdagStack/node.conf}" ]; then
    auto_max_peers="${BDAG_FASTSNAP_AUTO_MAX_PEERS:-3}"
    case "$auto_max_peers" in
      ''|*[!0-9]*) auto_max_peers=3 ;;
    esac
    peers="$(awk -F= -v max="$auto_max_peers" '
      /^[[:space:]]*addpeer[[:space:]]*=/ {
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2)
        print $2
        n++
        if (max > 0 && n >= max) exit
      }
    ' "${BDAG_FASTSNAP_PEERS_FILE:-/etc/bdagStack/node.conf}")"
  fi
  if [ -z "$peers" ]; then
    echo "Fast Artifact Sync V2 bootstrap skipped; no peers configured in BDAG_FASTSNAP_PEERS or node.conf"
    if [ "$fastsnap_required" = "1" ]; then
      exit 1
    fi
    exit 0
  fi

  network="${BDAG_FASTSNAP_NETWORK:-mainnet}"
  data_dir="${BDAG_FASTSNAP_DATADIR:-/var/lib/bdagStack/node/$network}"
  chain_current="$data_dir/BdagChain/CURRENT"
  archive="${BDAG_FASTSNAP_ARCHIVE:-$data_dir/fastsync-v2.bdsnap}"
  temp_archive="$archive.part"
  ledger="${BDAG_FASTSNAP_LEDGER:-$archive.artifact-ledger.json}"
  importing_marker="$data_dir/fastsync-v2.importing"
  complete_marker="$data_dir/fastsync-v2.import-complete"
  if [ -f "$chain_current" ]; then
    if [ -f "$importing_marker" ] && [ ! -f "$complete_marker" ]; then
      echo "Previous Fast Artifact Sync V2 import did not finish; removing partial chain/EVM DBs"
      rm -rf "$data_dir/BdagChain" "$data_dir/bdageth"
      rm -f "$complete_marker"
    else
      echo "Fast Artifact Sync V2 bootstrap skipped; existing chain DB found at $chain_current"
      exit 0
    fi
  fi

  parallelism="${BDAG_FASTSNAP_PARALLELISM:-4}"
  timeout="${BDAG_FASTSNAP_TIMEOUT:-180s}"
  min_tip="${BDAG_FASTSNAP_MIN_TIP:-0}"
  retries="${BDAG_FASTSNAP_RETRIES:-5}"
  retry_delay="${BDAG_FASTSNAP_RETRY_DELAY:-10s}"
  case "$retries" in
    ''|*[!0-9]*) retries=5 ;;
  esac
  if [ "$retries" -lt 1 ]; then
    retries=1
  fi

  mkdir -p "$data_dir"
  rm -f "$temp_archive"

  set -- /usr/local/bin/fastsnap \
    --artifact-v2=true \
    --network "$network" \
    --out "$temp_archive" \
    --ledger "$ledger" \
    --parallelism "$parallelism" \
    --timeout "$timeout"

  case "${BDAG_FASTSNAP_LEGACY_FALLBACK:-0}" in
    1|true|TRUE|yes|YES|on|ON) set -- "$@" --legacy-fallback=true ;;
    *) set -- "$@" --legacy-fallback=false ;;
  esac
  if [ "$min_tip" != "0" ]; then
    set -- "$@" --min-tip "$min_tip"
  fi
  if [ "${BDAG_FASTSNAP_ALLOW_UNSIGNED:-0}" = "1" ]; then
    set -- "$@" --allow-unsigned
  fi

  signer_list="${BDAG_FASTSNAP_TRUSTED_SIGNERS:-${BDAG_FASTSNAP_TRUSTED_SIGNER:-}}"
  for signer in $(printf '%s' "$signer_list" | tr ',\n\t' '    '); do
    [ -n "$signer" ] && set -- "$@" --trusted-signer "$signer"
  done
  for peer in $(printf '%s' "$peers" | tr ',\n\t' '    '); do
    [ -n "$peer" ] && set -- "$@" --peer "$peer"
  done

  echo "Fast Artifact Sync V2 bootstrap starting for $network into $data_dir"
  if [ ! -f "$archive" ]; then
    attempt=1
    while :; do
      "$@" && status=0 || status=$?
      if [ "$status" -eq 0 ]; then
        break
      fi
      if [ "$attempt" -ge "$retries" ]; then
        echo "Fast Artifact Sync V2 bootstrap failed after $attempt attempt(s)" >&2
        rm -f "$temp_archive"
        if [ "$fastsnap_required" = "1" ]; then
          exit "$status"
        fi
        echo "Fast Artifact Sync V2 bootstrap is not required; continuing with normal P2P sync" >&2
        exit 0
      fi
      rm -f "$temp_archive"
      attempt=$((attempt + 1))
      echo "Fast Artifact Sync V2 bootstrap attempt $((attempt - 1)) failed; retrying in $retry_delay ($attempt/$retries)" >&2
      sleep "$retry_delay"
    done
    mv -f "$temp_archive" "$archive"
  else
    echo "Fast Artifact Sync V2 archive already exists at $archive; reusing it for import"
  fi

  rm -f "$complete_marker"
  : > "$importing_marker"
  /usr/local/bin/blockdag-node snap import \
    --datadir "$data_dir" \
    --path "$archive"

  rm -f "$importing_marker"
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$complete_marker"
  if [ "${BDAG_FASTSNAP_KEEP_ARCHIVE:-0}" != "1" ]; then
    rm -f "$archive"
  fi
  echo "Fast Artifact Sync V2 bootstrap import finished"
)

if [ "$(id -u)" = 0 ]; then
  mkdir -p /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack
  chown -R bdagStack:bdagStack /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack || true
  run_fastsnap_bootstrap
  chown -R bdagStack:bdagStack /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack || true
  if [ -n "${NODE_ARGS_APPEND:-}" ]; then
    original_count=$#
    i=0
    while [ "$i" -lt "$original_count" ]; do
      arg="$1"
      shift
      case "$arg" in
        --node-args=*) arg="$arg $NODE_ARGS_APPEND" ;;
      esac
      set -- "$@" "$arg"
      i=$((i + 1))
    done
  fi
  exec runuser -u bdagStack -g bdagStack -- "$@"
fi
run_fastsnap_bootstrap
if [ -n "${NODE_ARGS_APPEND:-}" ]; then
  original_count=$#
  i=0
  while [ "$i" -lt "$original_count" ]; do
    arg="$1"
    shift
    case "$arg" in
      --node-args=*) arg="$arg $NODE_ARGS_APPEND" ;;
    esac
    set -- "$@" "$arg"
    i=$((i + 1))
  done
fi
exec "$@"
