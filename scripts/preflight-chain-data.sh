#!/usr/bin/env bash
set -euo pipefail

ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${BDAG_ENV_FILE:-$ROOT/.env}"
NETWORK="${BDAG_NETWORK:-mainnet}"
FRESH_CHAIN_OK=0
ALLOW_NODE_DATA_DIR_OVERRIDE="${BDAG_ALLOW_NODE_DATA_DIR_OVERRIDE:-0}"
MIN_MATERIAL_BYTES="${BDAG_CHAIN_PREFLIGHT_MATERIAL_BYTES:-1073741824}"
SIZE_RATIO_NUMERATOR="${BDAG_CHAIN_PREFLIGHT_SIZE_RATIO_NUMERATOR:-2}"
DOCKER_HELPER_IMAGE="${BDAG_DOCKER_HELPER_IMAGE:-alpine:3.20}"
LEGACY_DOCKER_VOLUME_MOUNT=""
LEGACY_DOCKER_VOLUME_VALID=""
LEGACY_DOCKER_VOLUME_SIZE=""

usage() {
  cat <<'USAGE'
Usage: scripts/preflight-chain-data.sh [--fresh-chain-ok]

Fails closed when NODE_DATA_DIR would point at fresh/tiny chain data while a
better preserved chain dataset exists. NODE_DATA_DIR is the only canonical node
datadir variable; BDAG_NODE_DATA_DIR is obsolete and rejected.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fresh-chain-ok) FRESH_CHAIN_OK=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

fail() {
  echo "chain-data preflight failed: $*" >&2
  exit 1
}

info() {
  echo "chain-data preflight: $*" >&2
}

resolve_path() {
  local raw="$1"
  if [[ "$raw" = /* ]]; then
    printf '%s\n' "$raw"
  else
    printf '%s\n' "$ROOT/${raw#./}"
  fi
}

size_bytes() {
  local path="$1"
  [[ -e "$path" ]] || { printf '0\n'; return 0; }
  local measured
  measured="$(du -sb "$path" 2>/dev/null | awk 'NR == 1 { print $1; exit }' || true)"
  if [[ "$measured" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$measured"
  else
    printf '0\n'
  fi
}

has_chain_markers() {
  local path="$1"
  if [[ -f "$path/$NETWORK/snapshot.bdsnap" ]]; then
    return 0
  fi
  [[ -d "$path/$NETWORK/BdagChain" ]] &&
    [[ -d "$path/$NETWORK/bdageth" ]] &&
    [[ -d "$path/$NETWORK/peerstore" ]] &&
    [[ -f "$path/$NETWORK/network.key" ]]
}

probe_legacy_docker_volume() {
  local mount="$1"
  [[ -n "$mount" ]] || return 0
  LEGACY_DOCKER_VOLUME_MOUNT="$(resolve_path "$mount")"
  local output
  output="$(
    docker run --rm -v stack_node-data:/candidate:ro "$DOCKER_HELPER_IMAGE" sh -lc "
      network=$(printf '%q' "$NETWORK")
      path=/candidate
      valid=0
      if [ -f \"\$path/\$network/snapshot.bdsnap\" ] || { [ -d \"\$path/\$network/BdagChain\" ] && [ -d \"\$path/\$network/bdageth\" ] && [ -d \"\$path/\$network/peerstore\" ] && [ -f \"\$path/\$network/network.key\" ]; }; then
        valid=1
      fi
      size=\$(du -sb \"\$path\" 2>/dev/null | awk '{print \$1}')
      printf 'valid=%s\n' \"\$valid\"
      printf 'size_bytes=%s\n' \"\${size:-0}\"
    " 2>/dev/null || true
  )"
  [[ -n "$output" ]] || return 0
  LEGACY_DOCKER_VOLUME_VALID="$(awk -F= '$1=="valid"{print $2; exit}' <<<"$output")"
  LEGACY_DOCKER_VOLUME_SIZE="$(awk -F= '$1=="size_bytes"{print $2; exit}' <<<"$output")"
}

candidate_has_chain_markers() {
  local path="$1"
  if [[ -n "$LEGACY_DOCKER_VOLUME_MOUNT" && "$path" == "$LEGACY_DOCKER_VOLUME_MOUNT" && -n "$LEGACY_DOCKER_VOLUME_VALID" ]]; then
    [[ "$LEGACY_DOCKER_VOLUME_VALID" == "1" ]]
    return
  fi
  has_chain_markers "$path"
}

candidate_size_bytes() {
  local path="$1"
  if [[ -n "$LEGACY_DOCKER_VOLUME_MOUNT" && "$path" == "$LEGACY_DOCKER_VOLUME_MOUNT" && -n "$LEGACY_DOCKER_VOLUME_SIZE" ]]; then
    printf '%s\n' "${LEGACY_DOCKER_VOLUME_SIZE:-0}"
    return 0
  fi
  size_bytes "$path"
}

candidate_label() {
  local path="$1"
  case "$path" in
    "$ROOT/data/node") printf '%s\n' canonical-host ;;
    /var/lib/docker/volumes/stack_node-data/_data) printf '%s\n' legacy-docker-volume ;;
    *) printf '%s\n' discovered ;;
  esac
}

declare -a CANDIDATES=()

add_candidate() {
  local path="$1"
  [[ -n "$path" ]] || return 0
  path="$(resolve_path "$path")"
  local existing
  for existing in "${CANDIDATES[@]:-}"; do
    [[ "$existing" == "$path" ]] && return 0
  done
  CANDIDATES+=("$path")
}

if [[ -f "$ENV_FILE" ]]; then
  if grep -Eq '^[[:space:]]*BDAG_NODE_DATA_DIR[[:space:]]*=' "$ENV_FILE"; then
    fail "$ENV_FILE still contains obsolete BDAG_NODE_DATA_DIR; migrate to NODE_DATA_DIR"
  fi
  # shellcheck disable=SC1090
  set -a; . "$ENV_FILE"; set +a
fi

if [[ -n "${BDAG_NODE_DATA_DIR:-}" ]]; then
  fail "environment still exports obsolete BDAG_NODE_DATA_DIR; migrate to NODE_DATA_DIR"
fi

[[ -n "${NODE_DATA_DIR:-}" ]] || fail "NODE_DATA_DIR is unset"

SELECTED="$(resolve_path "$NODE_DATA_DIR")"
CANONICAL="$(resolve_path "./data/node")"
if [[ "$SELECTED" != "$CANONICAL" && "$ALLOW_NODE_DATA_DIR_OVERRIDE" != "1" ]]; then
  fail "NODE_DATA_DIR resolves to $SELECTED, expected canonical $CANONICAL"
fi

add_candidate "$SELECTED"
add_candidate "$CANONICAL"

if command -v docker >/dev/null 2>&1; then
  volume_mount="$(docker volume inspect stack_node-data --format '{{ .Mountpoint }}' 2>/dev/null || true)"
  if [[ -n "$volume_mount" ]]; then
    probe_legacy_docker_volume "$volume_mount"
    add_candidate "$volume_mount"
  fi
fi

for path in \
  "$ROOT/chain-data/node" \
  "$ROOT/node-data" \
  "$HOME/Downloads/node" \
  "$HOME/Downloads/blockdag-chain/node"; do
  [[ -e "$path" ]] && add_candidate "$path"
done

for search_root in /mnt /media "/run/media/${USER:-}"; do
  [[ -d "$search_root" ]] || continue
  while IFS= read -r marker; do
    case "$marker" in
      */"$NETWORK"/BdagChain) add_candidate "${marker%/$NETWORK/BdagChain}" ;;
    esac
  done < <(find "$search_root" -maxdepth 6 -type d -name BdagChain 2>/dev/null | head -50)
done

selected_valid=0
candidate_has_chain_markers "$SELECTED" && selected_valid=1
selected_size="$(candidate_size_bytes "$SELECTED")"
best_path=""
best_size=0
valid_count=0

for candidate in "${CANDIDATES[@]:-}"; do
  if candidate_has_chain_markers "$candidate"; then
    candidate_size="$(candidate_size_bytes "$candidate")"
    valid_count=$((valid_count + 1))
    info "candidate $(candidate_label "$candidate") path=$candidate size_bytes=$candidate_size valid=1"
    if (( candidate_size > best_size )); then
      best_size="$candidate_size"
      best_path="$candidate"
    fi
  else
    info "candidate $(candidate_label "$candidate") path=$candidate size_bytes=$(candidate_size_bytes "$candidate") valid=0"
  fi
done

if (( selected_valid == 0 )); then
  if (( valid_count > 0 )); then
    fail "selected NODE_DATA_DIR $SELECTED is not valid, but preserved chain data exists at $best_path"
  fi
  if (( FRESH_CHAIN_OK == 1 )); then
    info "no preserved chain data found and --fresh-chain-ok was supplied"
    exit 0
  fi
  fail "selected NODE_DATA_DIR $SELECTED has no complete $NETWORK chain markers; use --fresh-chain-ok only for intentional genesis sync"
fi

if [[ -n "$best_path" && "$best_path" != "$SELECTED" ]]; then
  size_gap=$(( best_size - selected_size ))
  if (( size_gap > MIN_MATERIAL_BYTES && best_size >= selected_size * SIZE_RATIO_NUMERATOR )); then
    fail "selected NODE_DATA_DIR $SELECTED size_bytes=$selected_size is materially smaller than preserved candidate $best_path size_bytes=$best_size"
  fi
fi

info "selected NODE_DATA_DIR=$SELECTED size_bytes=$selected_size valid=1"
exit 0
