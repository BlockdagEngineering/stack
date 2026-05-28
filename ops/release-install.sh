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
    set_env_value .env POOL_SUBMIT_RPC_URLS "node1=http://bdag-miner-node-1:38131,node2=http://bdag-miner-node-2:38131"
    set_env_value .env POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT true
    set_env_value .env WALLET_RPC_URL "http://bdag-miner-node-2:18545"
    set_env_value .env WALLET_RPC_URLS "http://bdag-miner-node-2:18545,http://bdag-miner-node-1:18545"
  else
    set_env_value .env BDAG_NODE_MODE single
    set_env_value .env COMPOSE_PROFILES ""
    set_env_value .env BDAG_NODE_SERVICES "bdag-miner-node-1"
    set_env_value .env BDAG_STACK_SERVICES "pool-db,bdag-miner-node-1,rpc-failover,asic-pool"
    set_env_value .env POOL_RPC_BACKENDS "node1=http://bdag-miner-node-1:38131"
    set_env_value .env POOL_SUBMIT_RPC_URLS ""
    set_env_value .env POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT true
    set_env_value .env WALLET_RPC_URL "http://bdag-miner-node-1:18545"
    set_env_value .env WALLET_RPC_URLS "http://bdag-miner-node-1:18545"
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

env_value() {
  local key="$1" fallback="${2:-}" value
  value="$(grep -E "^${key}=" .env 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
  printf '%s\n' "${value:-$fallback}"
}

absolute_path() {
  local path="$1"
  if [[ "$path" == /* ]]; then
    printf '%s\n' "$path"
  else
    printf '%s\n' "$ROOT/${path#./}"
  fi
}

env_path_value() {
  local key="$1" fallback="$2"
  absolute_path "$(env_value "$key" "$fallback")"
}

env_path_value_for_auto_profile() {
  local key="$1" fallback="$2" shipped_default="$3" value
  value="$(env_value "$key" "")"
  if [[ -z "$value" || "$value" == "auto" || "$value" == "$shipped_default" || "$value" == "${shipped_default#./}" ]]; then
    absolute_path "$fallback"
  else
    absolute_path "$value"
  fi
}

existing_parent() {
  local path="$1"
  while [[ ! -e "$path" && "$path" != "/" ]]; do
    path="$(dirname "$path")"
  done
  printf '%s\n' "$path"
}

path_free_gib() {
  local path parent
  path="$1"
  parent="$(existing_parent "$path")"
  df -Pk "$parent" 2>/dev/null | awk 'NR == 2 {printf "%d", $4 / 1048576}'
}

mount_source_for_path() {
  local path parent
  path="$1"
  parent="$(existing_parent "$path")"
  findmnt -rn -T "$parent" -o SOURCE 2>/dev/null | sed 's/\[.*//'
}

same_mount_device() {
  local left="$1" right="$2" left_source right_source
  left_source="$(mount_source_for_path "$left")"
  right_source="$(mount_source_for_path "$right")"
  [[ -n "$left_source" && "$left_source" == "$right_source" ]]
}

path_is_usb() {
  local source block tran
  source="$(mount_source_for_path "$1")"
  [[ "$source" == /dev/* ]] || return 1
  tran="$(lsblk -no TRAN "$source" 2>/dev/null | head -n1 || true)"
  if [[ "$tran" == "usb" ]]; then
    return 0
  fi
  block="$(lsblk -no PKNAME "$source" 2>/dev/null | head -n1 || true)"
  [[ -n "$block" ]] || block="$(basename "$source")"
  tran="$(lsblk -dn -o TRAN "/dev/$block" 2>/dev/null | head -n1 || true)"
  [[ "$tran" == "usb" ]]
}

select_chain_data_base() {
  local configured target fstype source free_gib score best="" best_score=-1 profile min_chain_gib
  configured="$(env_value BDAG_CHAIN_DATA_DIR "")"
  profile="$(env_value BDAG_STORAGE_PROFILE auto)"
  if [[ -n "$configured" && "$configured" != "auto" && ! ( "$profile" == "auto" && ( "$configured" == "./data" || "$configured" == "data" ) ) ]]; then
    absolute_path "$configured"
    return 0
  fi
  min_chain_gib="$(env_value BDAG_STORAGE_MIN_CHAIN_FREE_GIB "${BDAG_STORAGE_MIN_CHAIN_FREE_GIB:-50}")"

  while read -r target fstype source; do
    case "$target" in
      /|/boot*|/dev*|/proc*|/run*|/sys*|/snap*|/var/lib/docker*|/var/lib/snapd*) continue ;;
    esac
    case "$fstype" in
      tmpfs|devtmpfs|overlay|squashfs|proc|sysfs|cgroup*|devpts|securityfs|tracefs|debugfs|fusectl|configfs) continue ;;
    esac
    free_gib="$(path_free_gib "$target")"
    free_gib="${free_gib:-0}"
    (( free_gib >= min_chain_gib )) || continue
    score="$free_gib"
    if path_is_usb "$target"; then
      score=$(( score + 100000 ))
    fi
    if (( score > best_score )); then
      best="$target/blockdag-chain"
      best_score="$score"
    fi
  done < <(findmnt -rn -o TARGET,FSTYPE,SOURCE)

  if [[ -n "$best" ]]; then
    printf '%s\n' "$best"
  else
    printf '%s\n' "$ROOT/data"
  fi
}

select_runtime_data_base() {
  local chain_base="$1" configured runtime_free min_runtime_gib
  configured="$(env_value BDAG_RUNTIME_DATA_DIR "")"
  if [[ -n "$configured" && "$configured" != "auto" ]]; then
    absolute_path "$configured"
    return 0
  fi
  min_runtime_gib="$(env_value BDAG_STORAGE_MIN_RUNTIME_FREE_GIB "${BDAG_STORAGE_MIN_RUNTIME_FREE_GIB:-4}")"
  runtime_free="$(path_free_gib "$ROOT")"
  runtime_free="${runtime_free:-0}"
  if ! same_mount_device "$ROOT" "$chain_base" && (( runtime_free >= min_runtime_gib )); then
    printf '%s\n' "$ROOT/runtime-data"
  else
    printf '%s\n' "$chain_base/runtime"
  fi
}

configure_storage_profile() {
  local chain_base runtime_base node1_dir node2_dir postgres_dir runtime_dir profile existing_profile
  chain_base="$(absolute_path "$(select_chain_data_base)")"
  runtime_base="$(absolute_path "$(select_runtime_data_base "$chain_base")")"
  existing_profile="$(env_value BDAG_STORAGE_PROFILE auto)"
  if [[ "$existing_profile" == "auto" || -z "$existing_profile" ]]; then
    node1_dir="$(env_path_value_for_auto_profile BDAG_NODE1_DATA_DIR "$chain_base/node1" "./data/node1")"
    node2_dir="$(env_path_value_for_auto_profile BDAG_NODE2_DATA_DIR "$chain_base/node2" "./data/node2")"
    postgres_dir="$(env_path_value_for_auto_profile BDAG_POSTGRES_DATA_DIR "$runtime_base/postgres" "./data/postgres")"
    runtime_dir="$(env_path_value_for_auto_profile BDAG_RUNTIME_DIR "$runtime_base/ops-runtime" "./ops/runtime")"
  else
    node1_dir="$(env_path_value BDAG_NODE1_DATA_DIR "$chain_base/node1")"
    node2_dir="$(env_path_value BDAG_NODE2_DATA_DIR "$chain_base/node2")"
    postgres_dir="$(env_path_value BDAG_POSTGRES_DATA_DIR "$runtime_base/postgres")"
    runtime_dir="$(env_path_value BDAG_RUNTIME_DIR "$runtime_base/ops-runtime")"
  fi
  if [[ "$existing_profile" == "auto" || -z "$existing_profile" ]]; then
    if path_is_usb "$chain_base" && ! same_mount_device "$chain_base" "$runtime_base"; then
      profile="usb-chain-internal-runtime"
    elif path_is_usb "$chain_base"; then
      profile="single-usb-constrained"
    elif ! same_mount_device "$chain_base" "$runtime_base"; then
      profile="split-ssd"
    else
      profile="single-device"
    fi
  else
    profile="$existing_profile"
  fi

  set_env_value .env BDAG_STORAGE_PROFILE "$profile"
  set_env_value .env BDAG_CHAIN_DATA_DIR "$chain_base"
  set_env_value .env BDAG_DATA_DIR "$chain_base"
  set_env_value .env BDAG_NODE1_DATA_DIR "$node1_dir"
  set_env_value .env BDAG_NODE2_DATA_DIR "$node2_dir"
  set_env_value .env BDAG_POSTGRES_DATA_DIR "$postgres_dir"
  set_env_value .env BDAG_RUNTIME_DIR "$runtime_dir"
  set_env_value .env BDAG_STORAGE_MIN_CHAIN_FREE_GIB "$(env_value BDAG_STORAGE_MIN_CHAIN_FREE_GIB "${BDAG_STORAGE_MIN_CHAIN_FREE_GIB:-50}")"
  set_env_value .env BDAG_STORAGE_MIN_RUNTIME_FREE_GIB "$(env_value BDAG_STORAGE_MIN_RUNTIME_FREE_GIB "${BDAG_STORAGE_MIN_RUNTIME_FREE_GIB:-4}")"

  mkdir -p asic-pool "$node1_dir" "$node2_dir" "$postgres_dir" "$runtime_dir/logs"
  say "Storage profile: $profile"
  echo "Chain data: $chain_base"
  echo "Postgres data: $postgres_dir"
  echo "Runtime/dashboard state: $runtime_dir"
}

configure_ephemeral_storage() {
  local enabled ephemeral_dir tmpfs_size mem_kb mem_gb
  enabled="$(env_value BDAG_EPHEMERAL_TMPFS_ENABLED 1)"
  ephemeral_dir="$(env_path_value BDAG_EPHEMERAL_DIR /run/bdag-pool)"
  tmpfs_size="$(env_value BDAG_CONTAINER_TMPFS_SIZE "")"
  if [[ -z "$tmpfs_size" ]]; then
    mem_kb="$(awk '/MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
    mem_gb=$(( mem_kb / 1024 / 1024 ))
    if (( mem_gb > 0 && mem_gb <= 4 )); then
      tmpfs_size="64m"
    else
      tmpfs_size="128m"
    fi
  fi

  set_env_value .env BDAG_EPHEMERAL_TMPFS_ENABLED "$enabled"
  set_env_value .env BDAG_EPHEMERAL_DIR "$ephemeral_dir"
  set_env_value .env BDAG_HOST_TMPDIR "$ephemeral_dir/tmp"
  set_env_value .env BDAG_CONTAINER_TMPFS_SIZE "$tmpfs_size"

  if [[ "$enabled" == "1" ]]; then
    if ! need_sudo mkdir -p "$ephemeral_dir/tmp" ||
      ! need_sudo chmod 0755 "$ephemeral_dir" ||
      ! need_sudo chmod 1777 "$ephemeral_dir/tmp"; then
      warn "Could not create $ephemeral_dir. Container tmpfs mounts will still protect in-container scratch; create the host ephemeral dir during host-profile install."
    fi
  fi
}

guard_runtime_compose() {
  if [[ ! -f docker-compose.yml ]]; then
    echo "Missing docker-compose.yml in release root." >&2
    exit 1
  fi
  if ! grep -q '^# BDAG_GENERATED_PI5_RUNTIME_COMPOSE=1$' docker-compose.yml; then
    echo "This installer requires the generated Pi5 runtime compose. Refusing to start an unmarked compose file." >&2
    exit 1
  fi
  if grep -Eq '^[[:space:]]*(build|dockerfile):' docker-compose.yml; then
    echo "Runtime compose contains build/dockerfile entries. Refusing to overwrite the deployed image set." >&2
    exit 1
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
  configure_storage_profile
  configure_ephemeral_storage

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
  set_env_value .env BDAG_FASTARTIFACTSYNC_ENABLED 1
  set_env_value .env BDAG_FASTSNAP_SEED_TIMER_ENABLED 0
  set_env_value .env BDAG_RAWDATADIR_SOURCE_MODE auto
  set_env_value .env BDAG_RAWDATADIR_ARTIFACT_BASE "./data-restore/rawdatadir"
  set_env_value .env BDAG_RAWDATADIR_MAX_EXPORT_BACKEND_LAG 10000
  set_env_value .env BDAG_RAWDATADIR_SINGLE_NODE_FINALIZE 0
  set_env_value .env BDAG_RAWDATADIR_PEERS ""
  set_env_value .env BDAG_RAWDATADIR_TRUSTED_SIGNERS ""
  set_env_value .env BDAG_SYNC_COORDINATOR_ACCELERATE_FASTSYNC 1
  set_env_value .env BDAG_SYNC_COORDINATOR_FAST_RESTART_COOLDOWN_SECONDS 900
  set_env_value .env BDAG_SYNC_COORDINATOR_RESTART_ON_MISSING_FASTARTIFACT 1
  set_env_value .env BDAG_SYNC_COORDINATOR_RESTART_ON_STALE_IMPORT 1
  set_env_value .env BDAG_FAST_CATCHUP_ARTIFACT_MODE auto
  set_env_value .env BDAG_FAST_CATCHUP_ARTIFACT_RETRY_SECONDS 300
  set_env_value .env BDAG_FAST_CATCHUP_ARTIFACT_MIN_BEHIND_BLOCKS 1000
  set_env_value .env BDAG_FAST_CATCHUP_ARTIFACT_MIN_GAIN_BLOCKS 1000
  set_env_value .env BDAG_FAST_CATCHUP_ARTIFACT_TRUST_ON_FIRST_SIGNED 1
  set_env_value .env BDAG_FAST_CATCHUP_ALLOW_UNSIGNED_ARTIFACTS 0
  set_env_value .env BDAG_FAST_CATCHUP_ARTIFACT_TIMEOUT 7200s
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

run_appliance_preflight() {
  if [[ "${BDAG_APPLIANCE_PREFLIGHT:-1}" != "1" ]]; then
    warn "Skipping mining appliance preflight because BDAG_APPLIANCE_PREFLIGHT=0."
    return 0
  fi
  if [[ ! -f scripts/mining-appliance-preflight.py ]]; then
    warn "Mining appliance preflight script is missing from this package."
    return 0
  fi

  say "Running mining appliance preflight"
  if [[ "${BDAG_APPLIANCE_PREFLIGHT_STRICT:-0}" == "1" ]]; then
    python3 scripts/mining-appliance-preflight.py --root "$ROOT" --env-file "$ROOT/.env"
  else
    python3 scripts/mining-appliance-preflight.py --root "$ROOT" --env-file "$ROOT/.env" --warn-only
  fi
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
    if command -v ionice >/dev/null 2>&1; then
      ionice -c 3 nice -n 19 src/build-images.sh "$arch" "bundle"
    else
      nice -n 19 src/build-images.sh "$arch" "bundle"
    fi
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
  local seed chain_base node1_dir node2_dir template_dir node_mode
  if ! seed="$(find_or_extract_chain_seed)"; then
    warn "No separate chain-data seed found. Nodes will sync from public peers."
    warn "If you received chain-data parts, reassemble them first, then rerun ./install.sh."
    return 0
  fi

  chain_base="$(env_path_value BDAG_CHAIN_DATA_DIR data)"
  node1_dir="$(env_path_value BDAG_NODE1_DATA_DIR "$chain_base/node1")"
  node2_dir="$(env_path_value BDAG_NODE2_DATA_DIR "$chain_base/node2")"
  template_dir="$chain_base/chain-template"
  node_mode="$(env_value BDAG_NODE_MODE single)"
  if [[ -z "$template_dir" || "$template_dir" == "/" ]]; then
    echo "Refusing unsafe chain template directory: $template_dir" >&2
    exit 1
  fi

  if [[ -d "$node1_dir/mainnet/BdagChain" || -d "$node2_dir/mainnet/BdagChain" ]]; then
    if ! yes_no "Existing node chain data was found. Replace it from the chain seed?" "n"; then
      return 0
    fi
    mv "$node1_dir" "$node1_dir.backup.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    mv "$node2_dir" "$node2_dir.backup.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    mkdir -p "$node1_dir" "$node2_dir"
  fi

  say "Unpacking one chain seed and copying it to configured node datadirs"
  rm -rf "$template_dir"
  mkdir -p "$template_dir" "$node1_dir" "$node2_dir"
  unzip -q "$seed" -d "$template_dir"
  if [[ -d "$template_dir/chain-data" ]]; then
    rsync -a "$template_dir/chain-data/" "$node1_dir/"
    if [[ "$node_mode" == "double" ]]; then
      rsync -a "$template_dir/chain-data/" "$node2_dir/"
    fi
  else
    rsync -a "$template_dir/" "$node1_dir/"
    if [[ "$node_mode" == "double" ]]; then
      rsync -a "$template_dir/" "$node2_dir/"
    fi
  fi
}

publish_p2p_snapshot_archive() {
  local arch="$1"
  local bdag_bin="artifacts/binaries/linux-$arch/bdag"
  local node1_dir node2_dir source_datadir target_datadir
  node1_dir="$(env_path_value BDAG_NODE1_DATA_DIR data/node1)"
  node2_dir="$(env_path_value BDAG_NODE2_DATA_DIR data/node2)"
  source_datadir="$node1_dir/mainnet"
  target_datadir="$node1_dir/mainnet"
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
    warn "No seeded node1 chain DB found; nodes will sync first, then use raw-datadir FastArtifact source serving after a finalized sidecar publish."
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
    say "Existing node1 P2P snapshot archive found: $source_archive"
  fi

  target_datadir="$node2_dir/mainnet"
  target_archive="$target_datadir/snapshot.bdsnap"
  if [[ "$(env_value BDAG_NODE_MODE single)" == "double" && -d "$target_datadir/BdagChain" ]]; then
    mkdir -p "$target_datadir"
    rm -f "$target_archive" "$target_archive.manifest.json"
    ln "$source_archive" "$target_archive" 2>/dev/null || cp -f "$source_archive" "$target_archive"
    if [[ -f "$source_archive.manifest.json" ]]; then
      ln "$source_archive.manifest.json" "$target_archive.manifest.json" 2>/dev/null || cp -f "$source_archive.manifest.json" "$target_archive.manifest.json"
    fi
    say "P2P snapshot archive available to node1 and node2"
  else
    warn "node2 is not enabled or has no chain DB; only node1 can serve a P2P snapshot."
  fi
}

start_stack() {
  say "Starting BlockDAG pool stack"
  guard_runtime_compose
  if [[ "${BDAG_RELEASE_PULL_BASE_IMAGES:-0}" == "1" ]]; then
    compose_cmd pull pool-db rpc-failover || true
  else
    warn "Skipping implicit image pulls. Set BDAG_RELEASE_PULL_BASE_IMAGES=1 for an explicit base-image refresh."
  fi
  compose_cmd up -d --no-build --pull never
  compose_cmd ps
}

install_dashboard() {
  if yes_no "Install the local dashboard/watchdog service?" "y"; then
    local runtime_dir
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
    runtime_dir="$(env_path_value BDAG_RUNTIME_DIR "ops/runtime")"
    ops/install-dashboard.sh --bind "${BDAG_DASHBOARD_BIND:-127.0.0.1}" --port "${BDAG_DASHBOARD_PORT:-8088}" --runtime-dir "$runtime_dir" || true
  fi
}

configure_miners() {
  if yes_no "After initial sync, scan the LAN and optionally configure discovered miner sources now?" "n"; then
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
  run_appliance_preflight
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
