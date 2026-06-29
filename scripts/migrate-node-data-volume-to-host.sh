#!/usr/bin/env bash
set -euo pipefail

ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${BDAG_ENV_FILE:-$ROOT/.env}"
NETWORK="${BDAG_NETWORK:-mainnet}"
SOURCE=""
FORCE=0
NO_STOP=0
STAMP="${BDAG_MIGRATION_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
USE_SUDO="${BDAG_MIGRATION_USE_SUDO:-auto}"

usage() {
  cat <<'USAGE'
Usage: scripts/migrate-node-data-volume-to-host.sh [--source PATH] [--force] [--no-stop]

Migrates a legacy preserved node datadir into canonical NODE_DATA_DIR=./node-data.
The source is preserved; the previous target is quarantined before copy.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="${2:-}"; shift ;;
    --force) FORCE=1 ;;
    --no-stop) NO_STOP=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

fail() {
  echo "chain-data migration failed: $*" >&2
  exit 1
}

say() {
  echo "chain-data migration: $*" >&2
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
  du -sb "$path" 2>/dev/null | awk '{print $1}' || printf '0\n'
}

has_chain_markers() {
  local path="$1"
  [[ -d "$path/$NETWORK/BdagChain" ]] &&
    [[ -d "$path/$NETWORK/bdageth" ]] &&
    [[ -d "$path/$NETWORK/peerstore" ]] &&
    [[ -f "$path/$NETWORK/network.key" ]]
}

run_root() {
  if [[ "$(id -u)" -eq 0 || "$USE_SUDO" == "0" ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

has_chain_markers_root() {
  local path="$1"
  if run_root test -f "$path/$NETWORK/snapshot.bdsnap"; then
    return 0
  fi
  run_root test -d "$path/$NETWORK/BdagChain" &&
    run_root test -d "$path/$NETWORK/bdageth" &&
    run_root test -d "$path/$NETWORK/peerstore" &&
    run_root test -f "$path/$NETWORK/network.key"
}

size_bytes_root() {
  local path="$1"
  run_root du -sb "$path" 2>/dev/null | awk '{print $1}' || printf '0\n'
}

if [[ -f "$ENV_FILE" ]]; then
  if grep -Eq '^[[:space:]]*BDAG_NODE_DATA_DIR[[:space:]]*=' "$ENV_FILE"; then
    fail "$ENV_FILE still contains obsolete BDAG_NODE_DATA_DIR; migrate config to NODE_DATA_DIR first"
  fi
  # shellcheck disable=SC1090
  set -a; . "$ENV_FILE"; set +a
fi

if [[ -n "${BDAG_NODE_DATA_DIR:-}" ]]; then
  fail "environment still exports obsolete BDAG_NODE_DATA_DIR; migrate to NODE_DATA_DIR first"
fi

[[ -n "${NODE_DATA_DIR:-}" ]] || fail "NODE_DATA_DIR is unset"
TARGET="$(resolve_path "$NODE_DATA_DIR")"
CANONICAL="$(resolve_path "./node-data")"
[[ "$TARGET" == "$CANONICAL" ]] || fail "NODE_DATA_DIR resolves to $TARGET, expected canonical $CANONICAL"

if [[ -z "$SOURCE" ]]; then
  if command -v docker >/dev/null 2>&1; then
    SOURCE="$(docker volume inspect stack_node-data --format '{{ .Mountpoint }}' 2>/dev/null || true)"
  fi
fi
[[ -n "$SOURCE" ]] || fail "no source supplied and legacy Docker volume stack_node-data was not found"
SOURCE="$(resolve_path "$SOURCE")"
has_chain_markers_root "$SOURCE" || fail "source $SOURCE does not contain complete $NETWORK chain markers"

if [[ -e "$TARGET" && "$FORCE" != "1" ]]; then
  if has_chain_markers "$TARGET"; then
    source_size="$(size_bytes_root "$SOURCE/$NETWORK")"
    target_size="$(size_bytes "$TARGET/$NETWORK")"
    if (( target_size >= source_size )); then
      say "target already has valid chain data at least as large as source; use --force to replace"
      exit 0
    fi
  fi
fi

if [[ "$NO_STOP" != "1" ]] && command -v docker >/dev/null 2>&1 && [[ -f "$ROOT/docker-compose.yml" ]]; then
  say "stopping node before datadir migration"
  (cd "$ROOT" && docker compose stop node >/dev/null)
fi

quarantine="$ROOT/data/quarantine/node.pre-migration-$STAMP"
run_root mkdir -p "$ROOT/data/quarantine"
if [[ -e "$TARGET" ]]; then
  run_root mv "$TARGET" "$quarantine"
fi
run_root mkdir -p "$TARGET"

say "copying active $NETWORK data from $SOURCE to $TARGET"
run_root rsync -aHAX --numeric-ids "$SOURCE/$NETWORK" "$TARGET/"
for file in rpc.cert rpc.key; do
  [[ -f "$SOURCE/$file" ]] && run_root rsync -aHAX --numeric-ids "$SOURCE/$file" "$TARGET/"
done

has_chain_markers_root "$TARGET" || fail "target $TARGET did not validate after copy"

manifest_dir="$ROOT/ops/runtime"
manifest="$manifest_dir/chain-data-migration-$STAMP.json"
run_root mkdir -p "$manifest_dir"
source_size="$(size_bytes_root "$SOURCE/$NETWORK")"
target_size="$(size_bytes_root "$TARGET/$NETWORK")"
quarantine_size="$(size_bytes_root "$quarantine")"
tmp_manifest="$(mktemp)"
cat > "$tmp_manifest" <<EOF
{
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "source": "legacy-node-data",
  "source_path": "$SOURCE",
  "source_active_path": "$SOURCE/$NETWORK",
  "target_path": "$TARGET",
  "quarantined_previous_target": "$quarantine",
  "network": "$NETWORK",
  "migration_reason": "legacy-volume-to-canonical-host-node-data",
  "source_size_bytes": $source_size,
  "target_size_bytes": $target_size,
  "quarantine_size_bytes": $quarantine_size,
  "source_height": null,
  "target_height": null,
  "rollback_source_preserved": true,
  "copied_paths": ["$NETWORK", "rpc.cert", "rpc.key"],
  "excluded_legacy_paths": ["failed-node-data", "data", "logs", "build"]
}
EOF
run_root mv "$tmp_manifest" "$manifest"
run_root chown "$(id -u):$(id -g)" "$manifest" 2>/dev/null || true
say "wrote $manifest"
