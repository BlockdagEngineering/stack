#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
DOCKER=(docker)

say() { printf '\n==> %s\n' "$*"; }
warn() { printf '\nWARNING: %s\n' "$*" >&2; }

ask() {
  local prompt="$1" default="${2:-}" value
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default]: " value || true
    printf '%s\n' "${value:-$default}"
  else
    read -r -p "$prompt: " value || true
    printf '%s\n' "$value"
  fi
}

yes_no() {
  local prompt="$1" default="${2:-n}" value suffix="[y/N]"
  [[ "$default" == "y" ]] && suffix="[Y/n]"
  read -r -p "$prompt $suffix " value || true
  value="${value:-$default}"
  [[ "$value" =~ ^[Yy] ]]
}

need_sudo() {
  if [[ "$(id -u)" == "0" ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

compose_cmd() {
  if "${DOCKER[@]}" compose version >/dev/null 2>&1; then
    "${DOCKER[@]}" compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    return 127
  fi
}

init_docker_access() {
  if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
    export BDAG_DOCKER_USE_SUDO=0
    return 0
  fi
  if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    DOCKER=(sudo docker)
    export BDAG_DOCKER_USE_SUDO=1
    return 0
  fi
  echo "Docker is installed but this user cannot access it yet." >&2
  echo "Log out and back in, run 'newgrp docker', or rerun this installer with sudo." >&2
  exit 1
}

detect_arch() {
  case "$(uname -m)" in
    x86_64|amd64) printf 'amd64\n' ;;
    aarch64|arm64) printf 'arm64\n' ;;
    *) return 1 ;;
  esac
}

detect_lan_ip() {
  ip -o -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}'
}

default_cidr() {
  local ipaddr="$1"
  if [[ "$ipaddr" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)\.[0-9]+$ ]]; then
    printf '%s.%s.%s.0/24\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
  else
    printf '192.168.1.0/24\n'
  fi
}

random_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 16
  else
    tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32
  fi
}

set_env_value() {
  local file="$1" key="$2" value="$3"
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

normalize_node_mode() {
  local value="${1:-single}"
  value="${value,,}"
  case "$value" in
    1|single|single-node|one|one-node) printf 'single\n' ;;
    2|double|dual|dual-node|two|two-node) printf 'double\n' ;;
    *) printf 'single\n' ;;
  esac
}

configure_node_mode_env() {
  local mode="$1"
  if [[ "$mode" == "double" ]]; then
    set_env_value .env BDAG_NODE_MODE double
    set_env_value .env COMPOSE_PROFILES dual-node
    set_env_value .env BDAG_NODE_SERVICES "bdag-miner-node-1,bdag-miner-node-2"
    set_env_value .env BDAG_STACK_SERVICES "pool-db,bdag-miner-node-1,bdag-miner-node-2,rpc-failover,asic-pool"
    set_env_value .env POOL_RPC_BACKENDS "node1=http://bdag-miner-node-1:38131,node2=http://bdag-miner-node-2:38131"
    set_env_value .env WALLET_RPC_URL "http://bdag-miner-node-2:18545"
    set_env_value .env WALLET_RPC_URLS "http://bdag-miner-node-2:18545,http://bdag-miner-node-1:18545"
  else
    set_env_value .env BDAG_NODE_MODE single
    set_env_value .env COMPOSE_PROFILES ""
    set_env_value .env BDAG_NODE_SERVICES "bdag-miner-node-2"
    set_env_value .env BDAG_STACK_SERVICES "pool-db,bdag-miner-node-2,rpc-failover,asic-pool"
    set_env_value .env POOL_RPC_BACKENDS "node2=http://bdag-miner-node-2:38131"
    set_env_value .env WALLET_RPC_URL "http://bdag-miner-node-2:18545"
    set_env_value .env WALLET_RPC_URLS "http://bdag-miner-node-2:18545"
  fi
}

configure_node_mining_env() {
  local enabled="$1" mining_address="$2"
  if [[ "$enabled" == "1" ]]; then
    set_env_value .env BDAG_ENABLE_NODE_MINING 1
    set_env_value .env BDAG_NODE_MODULES "Blockdag,miner"
    set_env_value .env BDAG_NODE_MINING_ARGS "'--allowminingwhennearlysynced --miner --miningaddr=${mining_address}'"
  else
    set_env_value .env BDAG_ENABLE_NODE_MINING 0
    set_env_value .env BDAG_NODE_MODULES "Blockdag"
    set_env_value .env BDAG_NODE_MINING_ARGS ""
  fi
}

install_packages() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    return 0
  fi
  say "Installing Docker and helper packages"
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "This installer expects Debian/Ubuntu with apt-get. Install Docker and rerun ./install.sh." >&2
    exit 1
  fi
  need_sudo apt-get update
  need_sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    docker.io docker-compose-plugin python3 curl jq rsync unzip zip zstd openssl iproute2
  if [[ "$(id -u)" != "0" ]]; then
    need_sudo usermod -aG docker "$USER" || true
    warn "If Docker permission fails, log out and back in, or rerun with: sudo ./install.sh"
  fi
}

configure_env() {
  say "Preparing configuration"
  [[ -f .env ]] || cp .env.example .env
  mkdir -p asic-pool data/node1 data/node2 data/postgres ops/runtime/logs

  local lan_ip scan_target mining_address node_mode node_mining_enabled mem_kb mem_gb
  lan_ip="$(detect_lan_ip)"
  lan_ip="$(ask "Pool LAN IP miners should connect to" "${lan_ip:-192.168.1.10}")"
  scan_target="$(ask "LAN scan range for ASIC discovery" "$(default_cidr "$lan_ip")")"
  mining_address="$(ask "Reward wallet address for this pool" "$(grep -E '^MINING_ADDRESS=' .env | cut -d= -f2-)")"
  if [[ -z "$mining_address" || "$mining_address" == "0x0000000000000000000000000000000000000000" ]]; then
    echo "A real reward wallet address is required." >&2
    exit 1
  fi
  node_mode="$(normalize_node_mode "$(ask "Backend node mode: single or double" "$(grep -E '^BDAG_NODE_MODE=' .env | cut -d= -f2- || printf 'single')")")"
  node_mining_enabled=0
  if yes_no "Enable node mining/template support now? Choose yes only when miners are attached" "n"; then
    node_mining_enabled=1
  fi

  local node_rpc_pass postgres_password postgres_user postgres_db
  node_rpc_pass="$(random_secret)"
  postgres_password="$(random_secret)"
  postgres_user="$(grep -E '^POSTGRES_USER=' .env | cut -d= -f2-)"
  postgres_db="$(grep -E '^POSTGRES_DB=' .env | cut -d= -f2-)"
  postgres_user="${postgres_user:-test}"
  postgres_db="${postgres_db:-pool}"

  set_env_value .env MINING_ADDRESS "$mining_address"
  set_env_value .env NODE_RPC_PASS "$node_rpc_pass"
  set_env_value .env POSTGRES_USER "$postgres_user"
  set_env_value .env POSTGRES_PASSWORD "$postgres_password"
  set_env_value .env POSTGRES_DB "$postgres_db"
  set_env_value .env PG_URL "postgres://${postgres_user}:${postgres_password}@pool-db:5432/${postgres_db}"
  set_env_value .env BDAG_POOL_HOST "$lan_ip"
  set_env_value .env BDAG_POOL_URL "stratum+tcp://$lan_ip:3334"
  set_env_value .env BDAG_MINER_SCAN_TARGET "$scan_target"
  set_env_value .env BDAG_FASTSYNC_PREPROCESS_WORKERS 1
  configure_node_mode_env "$node_mode"
  configure_node_mining_env "$node_mining_enabled" "$mining_address"

  mem_kb="$(awk '/MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
  mem_gb=$(( mem_kb / 1024 / 1024 ))
  if (( mem_gb > 0 && mem_gb <= 8 )); then
    say "Applying Pi/low-memory defaults"
    set_env_value .env BDAG_NODE_CACHE_MB 1024
    set_env_value .env NODE_MAX_PEERS 160
    set_env_value .env POSTGRES_SHARED_BUFFERS 256MB
    set_env_value .env POSTGRES_EFFECTIVE_CACHE_SIZE 1GB
  fi

  if yes_no "Expose the local dashboard on the LAN instead of only this machine?" "n"; then
    set_env_value .env BDAG_DASHBOARD_BIND "0.0.0.0"
  fi

  cp .env asic-pool/.env
}

load_or_build_images() {
  local arch="$1"
  say "Loading BlockDAG images for linux/$arch"
  local image_dir="artifacts/images/linux-$arch"
  local loaded=0

  if compgen -G "$image_dir/*.tar.zst" >/dev/null; then
    for image in "$image_dir"/*.tar.zst; do
      echo "Loading $image"
      zstd -dc "$image" | "${DOCKER[@]}" load
      loaded=1
    done
  fi

  if (( loaded == 0 )); then
    say "No prebuilt image archives found; building local images from bundled binaries"
    src/build-images.sh "$arch" "bundle"
  fi

  if "${DOCKER[@]}" image inspect "bdag-release/asic-pool:bundle-$arch" >/dev/null 2>&1; then
    "${DOCKER[@]}" tag "bdag-release/asic-pool:bundle-$arch" bdag-release/asic-pool:local
  fi
  if "${DOCKER[@]}" image inspect "bdag-release/node:bundle-$arch" >/dev/null 2>&1; then
    "${DOCKER[@]}" tag "bdag-release/node:bundle-$arch" bdag-release/node:local
  fi
}

find_or_extract_chain_seed() {
  if [[ -f chain-data/chain-data-seed.zip ]]; then
    printf '%s\n' "chain-data/chain-data-seed.zip"
    return 0
  fi

  local candidate
  for candidate in "$ROOT"/*chain-data*.zip "$ROOT"/../*chain-data*.zip; do
    [[ -f "$candidate" ]] || continue
    if unzip -l "$candidate" 'chain-data/chain-data-seed.zip' >/dev/null 2>&1; then
      say "Extracting chain seed from separate data package: $candidate"
      unzip -qo "$candidate" 'chain-data/chain-data-seed.zip' -d "$ROOT"
      printf '%s\n' "chain-data/chain-data-seed.zip"
      return 0
    fi
    if unzip -l "$candidate" 'mainnet/*' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

seed_chain_data() {
  local seed
  if ! seed="$(find_or_extract_chain_seed)"; then
    warn "No separate chain-data seed found. Nodes will sync from public peers."
    warn "If you received chain-data parts, reassemble them first, then rerun ./install.sh."
    return 0
  fi

  if [[ -d data/node1/mainnet/BdagChain || -d data/node2/mainnet/BdagChain ]]; then
    if ! yes_no "Existing node chain data was found. Replace it from the chain seed?" "n"; then
      return 0
    fi
    mv data/node1 "data/node1.backup.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    mv data/node2 "data/node2.backup.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    mkdir -p data/node1 data/node2
  fi

  say "Unpacking one chain seed and copying it to configured node datadirs"
  rm -rf data/chain-template
  mkdir -p data/chain-template
  unzip -q "$seed" -d data/chain-template
  if [[ -d data/chain-template/chain-data ]]; then
    rsync -a data/chain-template/chain-data/ data/node2/
    if [[ "$(grep -E '^BDAG_NODE_MODE=' .env | cut -d= -f2-)" == "double" ]]; then
      rsync -a data/chain-template/chain-data/ data/node1/
    fi
  else
    rsync -a data/chain-template/ data/node2/
    if [[ "$(grep -E '^BDAG_NODE_MODE=' .env | cut -d= -f2-)" == "double" ]]; then
      rsync -a data/chain-template/ data/node1/
    fi
  fi
}

publish_p2p_snapshot_archive() {
  local arch="$1"
  local bdag_bin="artifacts/binaries/linux-$arch/bdag"
  local source_datadir="data/node2/mainnet"
  local target_datadir="data/node2/mainnet"
  local source_archive="$source_datadir/snapshot.bdsnap"
  local target_archive="$target_datadir/snapshot.bdsnap"
  local force="${BDAG_P2P_SNAPSHOT_FORCE:-0}"

  if [[ "${BDAG_P2P_SNAPSHOT_PUBLISH:-1}" != "1" ]]; then
    warn "P2P snapshot archive publication disabled by BDAG_P2P_SNAPSHOT_PUBLISH=0."
    return 0
  fi
  if [[ ! -x "$bdag_bin" ]]; then
    warn "Cannot publish P2P snapshot archive: missing executable $bdag_bin."
    return 0
  fi
  if [[ ! -d "$source_datadir/BdagChain" ]]; then
    warn "No seeded node2 chain DB found; nodes will sync first, then create snapshot.bdsnap from a stopped synced node before relying on P2P snapshots."
    return 0
  fi

  if [[ ! -s "$source_archive" || "$force" == "1" ]]; then
    say "Publishing P2P snapshot archive for node datadirs"
    rm -f "$source_archive.tmp" "$source_archive.tmp.manifest.json"
    "$bdag_bin" snap export --datadir "$source_datadir" --path "$source_archive.tmp"
    mv "$source_archive.tmp" "$source_archive"
    if [[ -f "$source_archive.tmp.manifest.json" ]]; then
      mv "$source_archive.tmp.manifest.json" "$source_archive.manifest.json"
    fi
  else
    say "Existing node2 P2P snapshot archive found: $source_archive"
  fi

  target_datadir="data/node1/mainnet"
  target_archive="$target_datadir/snapshot.bdsnap"
  if [[ "$(grep -E '^BDAG_NODE_MODE=' .env | cut -d= -f2-)" == "double" && -d "$target_datadir/BdagChain" ]]; then
    mkdir -p "$target_datadir"
    rm -f "$target_archive" "$target_archive.manifest.json"
    ln "$source_archive" "$target_archive" 2>/dev/null || cp -f "$source_archive" "$target_archive"
    if [[ -f "$source_archive.manifest.json" ]]; then
      ln "$source_archive.manifest.json" "$target_archive.manifest.json" 2>/dev/null || cp -f "$source_archive.manifest.json" "$target_archive.manifest.json"
    fi
    say "P2P snapshot archive available to node1 and node2"
  else
    warn "node1 is not enabled or has no chain DB; only node2 can serve a P2P snapshot."
  fi
}

start_stack() {
  say "Starting BlockDAG pool stack"
  compose_cmd pull pool-db rpc-failover || true
  compose_cmd up -d
  compose_cmd ps
}

install_dashboard() {
  if yes_no "Install the local dashboard/watchdog service?" "y"; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
    ops/install-dashboard.sh --bind "${BDAG_DASHBOARD_BIND:-127.0.0.1}" --port "${BDAG_DASHBOARD_PORT:-8088}" || true
  fi
}

configure_miners() {
  if yes_no "Scan the LAN and optionally configure discovered ASICs now?" "y"; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
    python3 tools/miner_wizard.py \
      --scan-target "${BDAG_MINER_SCAN_TARGET:-}" \
      --pool-url "${BDAG_POOL_URL:-stratum+tcp://${BDAG_POOL_HOST:-127.0.0.1}:3334}" \
      --worker "$MINING_ADDRESS"
  fi
}

main() {
  local arch
  arch="$(detect_arch)" || { echo "Unsupported architecture: $(uname -m)" >&2; exit 2; }
  install_packages
  init_docker_access
  configure_env
  load_or_build_images "$arch"
  seed_chain_data
  publish_p2p_snapshot_archive "$arch"
  start_stack
  install_dashboard
  configure_miners
  say "Install complete"
  echo "Stratum: ${BDAG_POOL_URL:-$(grep '^BDAG_POOL_URL=' .env | cut -d= -f2-)}"
  echo "Dashboard: http://${BDAG_DASHBOARD_BIND:-127.0.0.1}:${BDAG_DASHBOARD_PORT:-8088}"
  echo "Run ./tools/status.sh for a status check."
}

main "$@"
