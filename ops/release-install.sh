#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/docker-compose.yml" && -f "$SCRIPT_DIR/.env.example" ]]; then
  ROOT="$SCRIPT_DIR"
elif [[ -f "$SCRIPT_DIR/../docker-compose.yml" && -f "$SCRIPT_DIR/../.env.example" ]]; then
  ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
else
  ROOT="$SCRIPT_DIR"
fi
cd "$ROOT"
if [[ -z "${BDAG_STACK_DEFAULTS_FILE:-}" ]]; then
  if [[ -f "$ROOT/ops/config/stack-defaults.env" ]]; then
    BDAG_STACK_DEFAULTS_FILE="$ROOT/ops/config/stack-defaults.env"
  else
    BDAG_STACK_DEFAULTS_FILE="$ROOT/config/stack-defaults.env"
  fi
fi
if [[ -f "$BDAG_STACK_DEFAULTS_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$BDAG_STACK_DEFAULTS_FILE"
  set +a
fi
DOCKER=(docker)

say() { printf '\n==> %s\n' "$*"; }
warn() { printf '\nWARNING: %s\n' "$*" >&2; }

stack_default() {
  local key="$1" fallback="${2:-}"
  if [[ ${!key+x} ]]; then
    printf '%s' "${!key}"
  else
    printf '%s' "$fallback"
  fi
}

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

docker_socket_gid() {
  if [[ -S /var/run/docker.sock ]]; then
    stat -c '%g' /var/run/docker.sock 2>/dev/null || printf '0\n'
  else
    printf '0\n'
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
  local detected
  if [[ -n "${BDAG_POOL_HOST:-}" ]]; then
    printf '%s\n' "$BDAG_POOL_HOST"
    return 0
  fi
  if command -v ip >/dev/null 2>&1 && [[ -n "${BDAG_ASIC_LAN_INTERFACE:-}" ]]; then
    detected="$(ip -o -4 addr show dev "$BDAG_ASIC_LAN_INTERFACE" 2>/dev/null \
      | awk '{split($4,a,"/"); if (a[1] != "") {print a[1]; exit}}' || true)"
    if [[ -n "$detected" ]]; then
      printf '%s\n' "$detected"
      return 0
    fi
  fi
  if command -v ip >/dev/null 2>&1; then
    detected="$(ip -o -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}' || true)"
    if [[ -n "$detected" && ! "$detected" =~ ^127\. && ! "$detected" =~ ^169\.254\. && ! "$detected" =~ ^172\.(1[6-9]|2[0-9]|3[0-1])\. ]]; then
      printf '%s\n' "$detected"
      return 0
    fi
    detected="$(ip -o -4 addr show scope global 2>/dev/null \
      | awk '
          $2 !~ /^(docker|br-|veth|zt|wg|tun|tap|tailscale)/ {
            split($4,a,"/")
            if (a[1] !~ /^127\./ && a[1] !~ /^169\.254\./ && a[1] !~ /^172\.(1[6-9]|2[0-9]|3[0-1])\./) {
              print a[1]
              exit
            }
          }' || true)"
    if [[ -n "$detected" ]]; then
      printf '%s\n' "$detected"
      return 0
    fi
  fi
  return 0
}

wired_route_policy_script() {
  local candidate
  for candidate in \
    "$ROOT/scripts/validate-network-route-policy.py" \
    "$ROOT/../scripts/validate-network-route-policy.py" \
    "$(cd "$ROOT/.." 2>/dev/null && pwd)/scripts/validate-network-route-policy.py"; do
    if [[ -f "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

enforce_wired_route_policy() {
  if [[ "$(uname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')" != "linux" ]]; then
    return 0
  fi
  if [[ "${BDAG_ENFORCE_WIRED_ROUTE_POLICY:-1}" != "1" ]]; then
    warn "Skipping wired-first route policy because BDAG_ENFORCE_WIRED_ROUTE_POLICY=${BDAG_ENFORCE_WIRED_ROUTE_POLICY:-unset}."
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    warn "python3 is missing; cannot validate or apply wired-first route policy."
    return 0
  fi
  local script
  script="$(wired_route_policy_script || true)"
  if [[ -z "$script" ]]; then
    warn "Wired-first route policy script is missing from this package."
    return 0
  fi
  say "Applying wired-first route policy"
  if ! python3 "$script" --apply --warn-only; then
    warn "Wired-first route policy application failed; continuing so preflight can report the remaining network state."
  fi
}

default_cidr() {
  local ipaddr="$1"
  if [[ "$ipaddr" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)\.[0-9]+$ ]]; then
    printf '%s.%s.%s.0/24\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
  else
    printf '192.168.1.0/24\n'
  fi
}

is_default_docker_bridge_address() {
  [[ "$1" =~ ^172\.(1[6-9]|2[0-9]|3[0-1])\. ]]
}

is_link_local_ipv4() {
  [[ "$1" =~ ^169\.254\. ]]
}

validate_pool_lan_config() {
  local pool_host pool_url pool_url_host scan_target asic_cidrs asic_iface allow_bridge
  pool_host="$(grep -E '^BDAG_POOL_HOST=' .env | tail -n 1 | cut -d= -f2- || true)"
  pool_url="$(grep -E '^BDAG_POOL_URL=' .env | tail -n 1 | cut -d= -f2- || true)"
  scan_target="$(grep -E '^BDAG_MINER_SCAN_TARGET=' .env | tail -n 1 | cut -d= -f2- || true)"
  asic_cidrs="$(grep -E '^BDAG_ASIC_LAN_CIDRS=' .env | tail -n 1 | cut -d= -f2- || true)"
  asic_iface="$(grep -E '^BDAG_ASIC_LAN_INTERFACE=' .env | tail -n 1 | cut -d= -f2- || true)"
  allow_bridge="$(grep -E '^BDAG_ALLOW_DOCKER_BRIDGE_ASIC_IPS=' .env | tail -n 1 | cut -d= -f2- || true)"
  pool_host="${pool_host%\"}"; pool_host="${pool_host#\"}"
  pool_url="${pool_url%\"}"; pool_url="${pool_url#\"}"
  scan_target="${scan_target%\"}"; scan_target="${scan_target#\"}"
  asic_cidrs="${asic_cidrs%\"}"; asic_cidrs="${asic_cidrs#\"}"
  asic_iface="${asic_iface%\"}"; asic_iface="${asic_iface#\"}"
  allow_bridge="${allow_bridge:-0}"
  pool_url_host="${pool_url#*://}"
  pool_url_host="${pool_url_host%%:*}"
  if [[ -z "$pool_host" || -z "$pool_url" || -z "$scan_target" || -z "$asic_cidrs" ]]; then
    echo "Pool LAN configuration is incomplete. Set BDAG_POOL_HOST, BDAG_POOL_URL, BDAG_MINER_SCAN_TARGET, and BDAG_ASIC_LAN_CIDRS." >&2
    exit 1
  fi
  if [[ "$allow_bridge" != "1" && "$allow_bridge" != "true" && "$allow_bridge" != "True" ]]; then
    if is_default_docker_bridge_address "$pool_host" || is_default_docker_bridge_address "$pool_url_host"; then
      echo "Refusing Docker bridge pool endpoint '$pool_url'. Use the host-facing ASIC LAN IP, not a 172.16.0.0/12 container address." >&2
      exit 1
    fi
    if [[ "$scan_target" =~ (^|[,[:space:]])172\.(1[6-9]|2[0-9]|3[0-1])\. || "$asic_cidrs" =~ (^|[,[:space:]])172\.(1[6-9]|2[0-9]|3[0-1])\. ]]; then
      echo "Refusing Docker bridge ASIC scan scope '$asic_cidrs'. Set BDAG_ASIC_LAN_CIDRS to the physical ASIC LAN." >&2
      exit 1
    fi
  fi
  if is_link_local_ipv4 "$pool_host" || is_link_local_ipv4 "$pool_url_host"; then
    if [[ -z "$asic_iface" ]]; then
      echo "Link-local ASIC pool endpoint '$pool_url' requires BDAG_ASIC_LAN_INTERFACE so the host can own $pool_host on the miner cable." >&2
      exit 1
    fi
  fi
}

ensure_asic_lan_address() {
  local pool_host="$1" asic_iface="$2"
  if ! is_link_local_ipv4 "$pool_host"; then
    return 0
  fi
  if [[ -z "$asic_iface" ]]; then
    return 0
  fi
  if ! command -v ip >/dev/null 2>&1; then
    echo "The ip command is required to bind link-local ASIC pool address $pool_host on $asic_iface." >&2
    exit 1
  fi
  if ! ip link show "$asic_iface" >/dev/null 2>&1; then
    echo "ASIC LAN interface '$asic_iface' was not found; set BDAG_ASIC_LAN_INTERFACE to the cabled miner interface." >&2
    exit 1
  fi
  if ip -o -4 addr show dev "$asic_iface" 2>/dev/null \
    | awk -v want="$pool_host" '{split($4,a,"/"); if (a[1] == want) found=1} END {exit found ? 0 : 1}'; then
    return 0
  fi
  say "Adding link-local ASIC pool address $pool_host/16 to $asic_iface"
  need_sudo ip addr replace "$pool_host/16" dev "$asic_iface" scope link
}

detect_zerotier_interface() {
  ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | grep -m1 '^zt' || true
}

configure_miner_network() {
  # Miners may live on the host LAN (no extra routing) or on a remote LAN
  # reached through a routed gateway such as a ZeroTier peer. In the remote
  # case the miner-route compose service keeps a host route alive so the
  # watchdog can reach the miner HTTP API for health checks and restarts.
  miner_route_subnet=""
  miner_route_gateway=""
  miner_route_dev=""
  if yes_no "Are any miners on a remote LAN reached through a routed gateway (e.g. a ZeroTier peer)?" "n"; then
    miner_route_subnet="$(ask "Remote miner subnet (CIDR)" "$(env_value BDAG_MINER_ROUTE_SUBNET)")"
    miner_route_gateway="$(ask "Gateway IP that forwards to that subnet (e.g. the ZeroTier peer)" "$(env_value BDAG_MINER_ROUTE_GATEWAY)")"
    miner_route_dev="$(ask "Host interface for the route (blank lets the kernel pick)" "$(env_value BDAG_MINER_ROUTE_DEV "$(detect_zerotier_interface)")")"
    if [[ -n "$miner_route_subnet" && ",$scan_target," != *",$miner_route_subnet,"* ]]; then
      scan_target="$scan_target,$miner_route_subnet"
    fi
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

set_env_value_literal() {
  local file="$1" key="$2" value="$3" tmp replaced=0 line
  tmp="$(mktemp)"
  if [[ -f "$file" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" == "$key="* ]]; then
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
        replaced=1
      else
        printf '%s\n' "$line" >> "$tmp"
      fi
    done < "$file"
  fi
  if (( replaced == 0 )); then
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  mv "$tmp" "$file"
}

single_quote_env_value() {
  local value="$1"
  printf "'%s'" "$(printf '%s' "$value" | sed "s/'/'\\\\''/g")"
}

set_stack_default_env_value() {
  local file="$1" key="$2" fallback="${3:-}"
  set_env_value "$file" "$key" "$(stack_default "$key" "$fallback")"
}

apply_stack_defaults_env() {
  local file="$1" line key value
  [[ -f "$BDAG_STACK_DEFAULTS_FILE" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    key="${line%%=*}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    value="$(stack_default "$key" "${line#*=}")"
    set_env_value "$file" "$key" "$value"
  done < "$BDAG_STACK_DEFAULTS_FILE"
}

configure_active_node_env() {
  set_env_value .env COMPOSE_PROFILES ""
  set_stack_default_env_value .env BDAG_POOL_CONTAINER pool
  set_stack_default_env_value .env BDAG_POOL_CONTAINERS pool
  set_stack_default_env_value .env BDAG_POOL_DB_CONTAINER postgres
  set_stack_default_env_value .env BDAG_NODE_SERVICES node
  set_stack_default_env_value .env BDAG_STACK_SERVICES "postgres,node,pool"
  set_env_value .env POOL_RPC_BACKENDS "node=http://node:38131"
  set_env_value .env POOL_SUBMIT_RPC_URLS ""
  set_env_value .env WALLET_RPC_URL "http://node:18545"
  set_env_value .env WALLET_RPC_URLS "http://node:18545"
  set_stack_default_env_value .env POOL_GBT_MIN_INTERVAL_MS
  set_stack_default_env_value .env POOL_GBT_PRESSURE_INTERVAL_MS
  set_stack_default_env_value .env POOL_GBT_PRESSURE_WINDOW_SECONDS
  set_stack_default_env_value .env POOL_RPC_ROUTER_NODE_HEALTH_ENABLED
  set_stack_default_env_value .env POOL_RPC_ROUTER_NODE_HEALTH_PROBE_SECONDS
  set_stack_default_env_value .env POOL_RPC_ROUTER_NODE_HEALTH_MAX_AGE_SECONDS
}

configure_node_mining_env() {
  local enabled="$1" mining_address="$2"
  if [[ "$enabled" == "1" ]]; then
    set_env_value .env BDAG_ENABLE_NODE_MINING 1
    set_env_value .env BDAG_NODE_MODULES "Blockdag,miner"
    set_env_value .env BDAG_NODE_MINING_ARGS "--miner --miningaddr=${mining_address} --maxinbound=1"
  else
    set_env_value .env BDAG_ENABLE_NODE_MINING 0
    set_env_value .env BDAG_NODE_MODULES "Blockdag,miner"
    set_env_value .env BDAG_NODE_MINING_ARGS ""
  fi
}

env_value() {
  local key="$1" fallback="${2:-}" value
  value="$(grep -E "^${key}=" .env 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
  printf '%s\n' "${value:-$fallback}"
}

env_value_unquoted() {
  local value
  value="$(env_value "$@")"
  case "$value" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
  esac
  printf '%s\n' "$value"
}

env_file_value_unquoted() {
  local file="$1" key="$2" fallback="${3:-}" value
  value="$(grep -E "^${key}=" "$file" 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
  value="${value:-$fallback}"
  case "$value" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
  esac
  printf '%s\n' "$value"
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
  local chain_base runtime_base node_dir postgres_dir runtime_dir profile existing_profile
  chain_base="$(absolute_path "$(select_chain_data_base)")"
  runtime_base="$(absolute_path "$(select_runtime_data_base "$chain_base")")"
  existing_profile="$(env_value BDAG_STORAGE_PROFILE auto)"
  if [[ "$existing_profile" == "auto" || -z "$existing_profile" ]]; then
    node_dir="$(env_path_value_for_auto_profile BDAG_NODE_DATA_DIR "$chain_base/node" "./data/node")"
    postgres_dir="$(env_path_value_for_auto_profile BDAG_POSTGRES_DATA_DIR "$runtime_base/postgres" "./data/postgres")"
    runtime_dir="$(env_path_value_for_auto_profile BDAG_RUNTIME_DIR "$runtime_base/ops-runtime" "./ops/runtime")"
  else
    node_dir="$(env_path_value BDAG_NODE_DATA_DIR "$chain_base/node")"
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
  set_env_value .env BDAG_NODE_DATA_DIR "$node_dir"
  set_env_value .env NODE_DATA_DIR "$node_dir"
  set_env_value .env BDAG_POSTGRES_DATA_DIR "$postgres_dir"
  set_env_value .env BDAG_RUNTIME_DIR "$runtime_dir"
  set_env_value .env BDAG_OPS_UID "$(id -u)"
  set_env_value .env BDAG_OPS_GID "$(id -g)"
  set_env_value .env BDAG_DOCKER_SOCKET_GID "$(docker_socket_gid)"
  set_env_value .env BDAG_STORAGE_MIN_CHAIN_FREE_GIB "$(env_value BDAG_STORAGE_MIN_CHAIN_FREE_GIB "${BDAG_STORAGE_MIN_CHAIN_FREE_GIB:-50}")"
  set_env_value .env BDAG_STORAGE_MIN_RUNTIME_FREE_GIB "$(env_value BDAG_STORAGE_MIN_RUNTIME_FREE_GIB "${BDAG_STORAGE_MIN_RUNTIME_FREE_GIB:-4}")"
  set_env_value .env BDAG_NODE_CPU_SHARES "$(env_value BDAG_NODE_CPU_SHARES 6144)"
  set_env_value .env BDAG_POOL_CPU_SHARES "$(env_value BDAG_POOL_CPU_SHARES 5120)"
  set_env_value .env BDAG_POOL_DB_CPU_SHARES "$(env_value BDAG_POOL_DB_CPU_SHARES 4096)"
  set_env_value .env BDAG_DASHBOARD_CPU_SHARES "$(env_value BDAG_DASHBOARD_CPU_SHARES 128)"
  set_env_value .env BDAG_NODE_MEMORY_LOW "$(env_value BDAG_NODE_MEMORY_LOW 768M)"
  set_env_value .env BDAG_POOL_MEMORY_LOW "$(env_value BDAG_POOL_MEMORY_LOW 256M)"
  set_env_value .env BDAG_POOL_DB_MEMORY_LOW "$(env_value BDAG_POOL_DB_MEMORY_LOW 512M)"
  set_env_value .env BDAG_DASHBOARD_MEMORY_LOW "$(env_value BDAG_DASHBOARD_MEMORY_LOW 64M)"
  set_env_value .env BDAG_TUNE_NET_QDISC "$(env_value BDAG_TUNE_NET_QDISC 1)"

  mkdir -p "$node_dir" "$postgres_dir" "$runtime_dir/logs"
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
  set_env_value .env BDAG_NODE_TMPFS_SIZE "$(env_value BDAG_NODE_TMPFS_SIZE 512m)"

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

  local lan_ip scan_target asic_lan_interface mining_address node_mining_enabled mem_kb mem_gb
  local miner_route_subnet miner_route_gateway miner_route_dev
  local p2p_advertise_ip
  lan_ip="$(detect_lan_ip)"
  lan_ip="$(ask "Pool LAN IP miners should connect to" "${lan_ip:-192.168.1.10}")"
  asic_lan_interface="$(env_value BDAG_ASIC_LAN_INTERFACE "${BDAG_ASIC_LAN_INTERFACE:-}")"
  if is_link_local_ipv4 "$lan_ip"; then
    asic_lan_interface="$(ask "Host interface for link-local ASIC LAN" "$asic_lan_interface")"
  fi
  scan_target="$(ask "LAN scan range for ASIC discovery" "$(default_cidr "$lan_ip")")"
  configure_miner_network
  mining_address="$(ask "Reward wallet address for this pool" "$(grep -E '^MINING_ADDRESS=' .env | cut -d= -f2-)")"
  if [[ -z "$mining_address" || "$mining_address" == "0x0000000000000000000000000000000000000000" ]]; then
    echo "A real reward wallet address is required." >&2
    exit 1
  fi
  node_mining_enabled=0
  if yes_no "Enable node mining/template support now? Choose yes only when miners are attached" "n"; then
    node_mining_enabled=1
  fi

  local node_rpc_pass postgres_password postgres_user postgres_db
  node_rpc_pass="$(env_value_unquoted NODE_RPC_PASS "")"
  if [[ -z "$node_rpc_pass" || "$node_rpc_pass" == "test" ]]; then
    node_rpc_pass="$(random_secret)"
  fi
  postgres_password="$(env_value_unquoted POSTGRES_PASSWORD "")"
  if [[ -n "${POSTGRES_PASSWORD:-}" ]]; then
    postgres_password="$POSTGRES_PASSWORD"
  elif [[ -z "$postgres_password" || "$postgres_password" == "change_me_to_a_strong_secret" ]]; then
    postgres_password="$(random_secret)"
  else
    say "Reusing POSTGRES_PASSWORD from existing .env"
  fi
  postgres_user="$(grep -E '^POSTGRES_USER=' .env | cut -d= -f2-)"
  postgres_db="$(grep -E '^POSTGRES_DB=' .env | cut -d= -f2-)"
  postgres_user="${postgres_user:-bdag_pool}"
  postgres_db="${postgres_db:-bdagpool}"

  set_env_value .env MINING_ADDRESS "$mining_address"
  set_env_value .env NODE_RPC_PASS "$node_rpc_pass"
  set_env_value .env POSTGRES_USER "$postgres_user"
  set_env_value .env POSTGRES_PASSWORD "$postgres_password"
  set_env_value .env POSTGRES_DB "$postgres_db"
  set_env_value .env PG_URL "postgres://${postgres_user}:${postgres_password}@postgres:5432/${postgres_db}"
  set_env_value .env BDAG_POOL_HOST "$lan_ip"
  set_env_value .env BDAG_POOL_URL "stratum+tcp://$lan_ip:3334"
  set_env_value .env BDAG_ASIC_LAN_INTERFACE "$asic_lan_interface"
  set_env_value .env BDAG_MINER_SCAN_TARGET "$scan_target"
  set_env_value .env BDAG_ASIC_LAN_CIDRS "$scan_target"
  set_env_value .env BDAG_MINER_ROUTE_SUBNET "$miner_route_subnet"
  set_env_value .env BDAG_MINER_ROUTE_GATEWAY "$miner_route_gateway"
  set_env_value .env BDAG_MINER_ROUTE_DEV "$miner_route_dev"
  validate_pool_lan_config
  ensure_asic_lan_address "$lan_ip" "$asic_lan_interface"
  p2p_advertise_ip="$(env_value BDAG_P2P_ADVERTISE_IP "")"
  if [[ -n "$p2p_advertise_ip" ]]; then
    set_env_value .env BDAG_P2P_ADVERTISE_IP "$p2p_advertise_ip"
    if grep -q '^externalip=' node.conf 2>/dev/null; then
      sed -i "s|^externalip=.*|externalip=${p2p_advertise_ip}|" node.conf
    else
      printf '\nexternalip=%s\n' "$p2p_advertise_ip" >> node.conf
    fi
  else
    set_env_value .env BDAG_P2P_ADVERTISE_IP ""
    sed -i '/^externalip=/d' node.conf 2>/dev/null || true
  fi
  apply_stack_defaults_env .env
  set_stack_default_env_value .env BDAG_FASTSYNC_RANGE_BLOCKS
  set_stack_default_env_value .env BDAG_FASTSYNC_PREPROCESS_WORKERS
  set_stack_default_env_value .env BDAG_CHAIN_PEERSTORE_PEER_EXTRACTION_ENABLED
  set_stack_default_env_value .env BDAG_CHAIN_PEERSTORE_LOG_TAIL
  if ! grep -q '^BDAG_CHAIN_DB_ARCHIVE_URL=' .env; then
    local default_chain_archive_url
    default_chain_archive_url="${BDAG_CHAIN_DB_ARCHIVE_URL:-$(env_file_value_unquoted .env.example BDAG_CHAIN_DB_ARCHIVE_URL "")}"
    if [[ -n "$default_chain_archive_url" ]]; then
      set_env_value_literal .env BDAG_CHAIN_DB_ARCHIVE_URL "$(single_quote_env_value "$default_chain_archive_url")"
    fi
  fi
  set_env_value .env BDAG_CHAIN_DB_ARCHIVE_MIN_BYTES "$(env_value_unquoted BDAG_CHAIN_DB_ARCHIVE_MIN_BYTES 1048576)"
  set_env_value .env BDAG_CHAIN_DB_ARCHIVE_KEEP "$(env_value_unquoted BDAG_CHAIN_DB_ARCHIVE_KEEP 1)"
  set_env_value .env BDAG_CHAIN_DB_ARCHIVE_FORCE "$(env_value_unquoted BDAG_CHAIN_DB_ARCHIVE_FORCE 0)"
  set_env_value .env BDAG_CHAIN_DB_IMPORT_BINARY "$(env_value_unquoted BDAG_CHAIN_DB_IMPORT_BINARY "")"
  set_env_value .env BDAG_CHAIN_DB_IMPORT_BATCH_BYTES "$(env_value_unquoted BDAG_CHAIN_DB_IMPORT_BATCH_BYTES 16777216)"
  set_env_value .env BDAG_CHAIN_DB_IMPORT_NO_NETWORK_CHECK "$(env_value_unquoted BDAG_CHAIN_DB_IMPORT_NO_NETWORK_CHECK 0)"
  set_env_value .env BDAG_INSTALL_APPLIANCE_HOST_PROFILE "$(env_value BDAG_INSTALL_APPLIANCE_HOST_PROFILE 1)"
  set_env_value .env BDAG_INSTALL_APPLIANCE_PROFILE_DISABLE_SERVICES "$(env_value BDAG_INSTALL_APPLIANCE_PROFILE_DISABLE_SERVICES 0)"
  set_env_value .env BDAG_INSTALL_APPLIANCE_PROFILE_RELOAD_DOCKER "$(env_value BDAG_INSTALL_APPLIANCE_PROFILE_RELOAD_DOCKER 1)"
  set_env_value .env BDAG_INSTALL_APPLIANCE_PROFILE_STRICT "$(env_value BDAG_INSTALL_APPLIANCE_PROFILE_STRICT 0)"
  set_env_value .env BDAG_INSTALL_STACK_SUPPORT_SERVICES "$(env_value BDAG_INSTALL_STACK_SUPPORT_SERVICES 1)"
  set_env_value .env BDAG_INSTALL_STACK_SUPPORT_SERVICES_STRICT "$(env_value BDAG_INSTALL_STACK_SUPPORT_SERVICES_STRICT 0)"
  set_env_value .env NODE_ARGS_APPEND ""
  set_stack_default_env_value .env BDAG_INSTALL_REBUILD_DASHBOARD_PLOTS
  set_stack_default_env_value .env BDAG_INSTALL_REBUILD_DASHBOARD_PLOT_HOURS
  set_stack_default_env_value .env BDAG_INSTALL_REBUILD_DASHBOARD_PLOT_WINDOW_BLOCKS
  set_stack_default_env_value .env BDAG_INSTALL_REBUILD_DASHBOARD_PLOT_WORKERS
  set_stack_default_env_value .env BDAG_DASHBOARD_HISTORY_REBUILD_PRESERVE_ASIC_HISTORY
  set_stack_default_env_value .env BDAG_SYNC_COORDINATOR_ACCELERATE_FASTSYNC
  set_stack_default_env_value .env BDAG_SYNC_COORDINATOR_FAST_RESTART_COOLDOWN_SECONDS
  set_stack_default_env_value .env BDAG_SYNC_COORDINATOR_RESTART_ON_STALE_IMPORT
  set_stack_default_env_value .env BDAG_CATCHUP_PAUSE_ENABLED
  set_stack_default_env_value .env BDAG_CATCHUP_PAUSE_THRESHOLD_BLOCKS
  set_stack_default_env_value .env BDAG_CATCHUP_IO_PRESSURE_PAUSE_ENABLED
  set_stack_default_env_value .env BDAG_CATCHUP_IO_PRESSURE_MIN_LAG_BLOCKS
  set_stack_default_env_value .env BDAG_CATCHUP_IOWAIT_WARN_PERCENT
  set_stack_default_env_value .env BDAG_CATCHUP_IO_SOME_AVG10_WARN
  set_stack_default_env_value .env BDAG_CATCHUP_IO_FULL_AVG10_WARN
  set_stack_default_env_value .env BDAG_CATCHUP_NODE_RECREATE_ENABLED
  set_stack_default_env_value .env BDAG_CATCHUP_NODE_CACHE_MB
  set_stack_default_env_value .env BDAG_CATCHUP_NODE_CACHE_MIN_MB
  set_stack_default_env_value .env BDAG_CATCHUP_NODE_CACHE_MEMORY_PERCENT
  configure_active_node_env
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

}

install_appliance_host_profile() {
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
  case "${BDAG_INSTALL_APPLIANCE_HOST_PROFILE:-1}" in
    0|false|False|no|No|off|Off)
      warn "Skipping appliance host profile because BDAG_INSTALL_APPLIANCE_HOST_PROFILE=${BDAG_INSTALL_APPLIANCE_HOST_PROFILE:-0}."
      return 0
      ;;
  esac
  if [[ ! -f scripts/install-mining-appliance-profile.sh ]]; then
    warn "Cannot install appliance host profile: scripts/install-mining-appliance-profile.sh is missing."
    return 0
  fi
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "Cannot install appliance host profile: systemctl is not available on this host."
    return 0
  fi

  local args=()
  if [[ "${BDAG_INSTALL_APPLIANCE_PROFILE_DISABLE_SERVICES:-0}" != "1" ]]; then
    args+=(--no-disable-services)
  fi
  if [[ "${BDAG_INSTALL_APPLIANCE_PROFILE_RELOAD_DOCKER:-1}" != "1" ]]; then
    args+=(--no-docker-reload)
  fi

  say "Installing non-destructive mining appliance host profile"
  if bash scripts/install-mining-appliance-profile.sh "${args[@]}"; then
    return 0
  fi
  if [[ "${BDAG_INSTALL_APPLIANCE_PROFILE_STRICT:-0}" == "1" ]]; then
    echo "Appliance host profile installation failed and strict mode is enabled." >&2
    exit 1
  fi
  warn "Appliance host profile installation failed. Continuing because BDAG_INSTALL_APPLIANCE_PROFILE_STRICT=0."
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

chain_db_archive_basename() {
  local url="$1" clean
  clean="${url%%\?*}"
  clean="${clean##*/}"
  if [[ -z "$clean" || "$clean" == */* ]]; then
    clean="chain-db-archive.bdsnap"
  fi
  printf '%s\n' "$clean"
}

chain_db_archive_url_is_local() {
  case "$1" in
    file://*|/*|./*|../*) return 0 ;;
    *) return 1 ;;
  esac
}

chain_db_archive_local_path() {
  local url="$1" path
  case "$url" in
    file://localhost/*) path="/${url#file://localhost/}" ;;
    file://*) path="${url#file://}" ;;
    /*) path="$url" ;;
    ./*|../*) path="$(absolute_path "$url")" ;;
    *) return 1 ;;
  esac
  [[ -f "$path" ]] || return 1
  printf '%s\n' "$path"
}

file_size_bytes() {
  if stat -c%s "$1" >/dev/null 2>&1; then
    stat -c%s "$1"
  elif stat -f%z "$1" >/dev/null 2>&1; then
    stat -f%z "$1"
  else
    wc -c < "$1" | tr -d '[:space:]'
  fi
}

download_chain_db_archive() {
  local url="$1" cache_dir="$2" min_bytes="$3"
  local archive part size local_archive
  archive="$cache_dir/$(chain_db_archive_basename "$url")"
  part="${archive}.part"
  mkdir -p "$cache_dir"

  if [[ -s "$archive" ]]; then
    size="$(file_size_bytes "$archive" 2>/dev/null || echo 0)"
    if (( size >= min_bytes )); then
      say "Using cached chain DB snapshot archive: $archive" >&2
    else
      warn "Cached chain DB snapshot archive is too small ($size bytes < $min_bytes); redownloading."
      rm -f "$archive"
    fi
  fi

  if [[ ! -s "$archive" ]]; then
    if chain_db_archive_url_is_local "$url"; then
      if ! local_archive="$(chain_db_archive_local_path "$url")"; then
        echo "Configured BDAG_CHAIN_DB_ARCHIVE_URL local archive was not found: $url" >&2
        exit 1
      fi
      say "Copying local chain DB snapshot archive" >&2
      echo "Snapshot source: $local_archive" >&2
      echo "Snapshot cache: $archive" >&2
      cp "$local_archive" "$part"
    else
      command -v curl >/dev/null 2>&1 || { echo "curl is required to download BDAG_CHAIN_DB_ARCHIVE_URL." >&2; exit 1; }
      say "Downloading chain DB snapshot archive" >&2
      echo "Snapshot cache: $archive" >&2
      if ! curl --fail --location --show-error \
        --connect-timeout 30 \
        --retry 8 \
        --retry-all-errors \
        --retry-delay 5 \
        --continue-at - \
        --output "$part" \
        "$url"; then
        echo "Chain DB snapshot archive download failed." >&2
        exit 1
      fi
    fi
    size="$(file_size_bytes "$part" 2>/dev/null || echo 0)"
    if (( size < min_bytes )); then
      echo "Downloaded chain DB snapshot archive is too small ($size bytes < $min_bytes)." >&2
      exit 1
    fi
    mv "$part" "$archive"
  fi

  printf '%s\n' "$archive"
}

extract_chain_db_datadir_archive() {
  local archive="$1" staging="$2"
  local archive_base extract_dir bdag_chain_dir source_root
  archive_base="$(basename "$archive")"
  extract_dir="${staging}.extract"
  rm -rf "$extract_dir"
  mkdir -p "$extract_dir"

  case "$archive_base" in
    *.tar.zst|*.tzst)
      if tar --help 2>/dev/null | grep -q -- '--zstd'; then
        tar --zstd -xf "$archive" -C "$extract_dir"
      elif command -v zstd >/dev/null 2>&1; then
        zstd -dc "$archive" | tar -xf - -C "$extract_dir"
      else
        echo "Extracting $archive_base requires tar with --zstd support or the zstd command." >&2
        return 1
      fi
      ;;
    *.tar.gz|*.tgz)
      tar -xzf "$archive" -C "$extract_dir"
      ;;
    *.tar)
      tar -xf "$archive" -C "$extract_dir"
      ;;
    *)
      echo "Unsupported chain DB datadir archive type: $archive_base" >&2
      return 1
      ;;
  esac

  bdag_chain_dir="$(find "$extract_dir" -type d -path '*/mainnet/BdagChain' -print -quit)"
  if [[ -n "$bdag_chain_dir" ]]; then
    source_root="$(dirname "$(dirname "$bdag_chain_dir")")"
    cp -a "$source_root/." "$staging/"
  elif [[ -d "$extract_dir/BdagChain" ]]; then
    mkdir -p "$staging/mainnet"
    cp -a "$extract_dir/." "$staging/mainnet/"
  else
    echo "Chain DB datadir archive did not contain mainnet/BdagChain." >&2
    return 1
  fi

  rm -rf "$extract_dir"
}

chain_db_import_binary() {
  local configured arch candidate
  configured="$(env_value_unquoted BDAG_CHAIN_DB_IMPORT_BINARY "")"
  if [[ -n "$configured" ]]; then
    if [[ "$configured" == */* ]]; then
      candidate="$(absolute_path "$configured")"
      if [[ -x "$candidate" ]]; then
        printf '%s\n' "$candidate"
        return 0
      fi
    elif command -v "$configured" >/dev/null 2>&1; then
      command -v "$configured"
      return 0
    fi
    echo "Configured BDAG_CHAIN_DB_IMPORT_BINARY is not executable: $configured" >&2
    return 2
  fi

  if [[ "$(uname -s)" != "Linux" ]]; then
    return 1
  fi

  for candidate in "$ROOT/bin/blockdag-node" "$ROOT/bin/bdag"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  if arch="$(detect_arch 2>/dev/null)"; then
    for candidate in \
      "$ROOT/artifacts/binaries/linux-$arch/blockdag-node" \
      "$ROOT/artifacts/binaries/linux-$arch/bdag"; do
      if [[ -x "$candidate" ]]; then
        printf '%s\n' "$candidate"
        return 0
      fi
    done
  fi

  if command -v blockdag-node >/dev/null 2>&1; then
    command -v blockdag-node
    return 0
  fi
  if command -v bdag >/dev/null 2>&1; then
    command -v bdag
    return 0
  fi
  return 1
}

chain_db_import_args() {
  local batch no_network_check
  batch="$(env_value_unquoted BDAG_CHAIN_DB_IMPORT_BATCH_BYTES 16777216)"
  no_network_check="$(env_value_unquoted BDAG_CHAIN_DB_IMPORT_NO_NETWORK_CHECK 0)"
  if [[ -n "$batch" ]]; then
    printf '%s\0%s\0' "--batch-size" "$batch"
  fi
  case "$no_network_check" in
    1|true|True|TRUE|yes|Yes|YES|on|On|ON)
      printf '%s\0' "--no-network-check"
      ;;
  esac
}

import_chain_db_archive() {
  local archive="$1" network_dir="$2"
  local binary archive_dir archive_base staging_root
  local binary_status
  local import_args=()

  mkdir -p "$network_dir"
  while IFS= read -r -d '' arg; do
    import_args+=("$arg")
  done < <(chain_db_import_args)

  if binary="$(chain_db_import_binary)"; then
    say "Importing chain DB .bdsnap with $binary"
    "$binary" snap import --datadir "$network_dir" --path "$archive" "${import_args[@]}"
    return 0
  else
    binary_status=$?
    if (( binary_status == 2 )); then
      return 1
    fi
  fi

  if "${DOCKER[@]}" image inspect bdag-release/node:local >/dev/null 2>&1; then
    say "Importing chain DB .bdsnap with bdag-release/node:local"
    staging_root="$(dirname "$network_dir")"
    archive_dir="$(dirname "$archive")"
    archive_base="$(basename "$archive")"
    "${DOCKER[@]}" run --rm \
      --entrypoint /usr/local/bin/blockdag-node \
      -v "$staging_root:/import-node" \
      -v "$archive_dir:/snapshots:ro" \
      bdag-release/node:local \
      snap import --datadir /import-node/mainnet --path "/snapshots/$archive_base" "${import_args[@]}"
    return 0
  fi

  echo "Cannot import chain DB snapshot: no usable blockdag-node/bdag binary or bdag-release/node:local image was found." >&2
  return 1
}

stop_node_for_chain_db_replace() {
  local container
  container="$(compose_cmd ps -q node 2>/dev/null || true)"
  if [[ -n "$container" ]]; then
    say "Stopping node before replacing chain DB"
    compose_cmd stop node
    return 0
  fi
  if "${DOCKER[@]}" ps --format '{{.Names}}' 2>/dev/null | grep -qx 'node'; then
    say "Stopping node container before replacing chain DB"
    "${DOCKER[@]}" stop node >/dev/null
  fi
}

install_chain_db_archive() {
  local url chain_base node_dir cache_dir min_bytes keep force archive
  local parent stamp staging backup has_chain has_anything network_dir
  url="$(env_value_unquoted BDAG_CHAIN_DB_ARCHIVE_URL "")"
  if [[ -z "$url" ]]; then
    warn "BDAG_CHAIN_DB_ARCHIVE_URL is not set. The node will sync from configured peers."
    return 0
  fi

  chain_base="$(env_path_value BDAG_CHAIN_DATA_DIR data)"
  node_dir="$(env_path_value BDAG_NODE_DATA_DIR "$chain_base/node")"
  parent="$(dirname "$node_dir")"
  cache_dir="$(absolute_path "$(env_value_unquoted BDAG_CHAIN_DB_ARCHIVE_CACHE_DIR "$parent/chain-db-archives")")"
  min_bytes="$(env_value_unquoted BDAG_CHAIN_DB_ARCHIVE_MIN_BYTES 1048576)"
  keep="$(env_value_unquoted BDAG_CHAIN_DB_ARCHIVE_KEEP 1)"
  force="$(env_value_unquoted BDAG_CHAIN_DB_ARCHIVE_FORCE 0)"

  has_chain=0
  [[ -d "$node_dir/mainnet/BdagChain" ]] && has_chain=1
  has_anything=0
  if [[ -d "$node_dir" ]] && find "$node_dir" -mindepth 1 -print -quit 2>/dev/null | grep -q .; then
    has_anything=1
  fi

  if (( has_chain == 1 )) && [[ "$force" != "1" ]]; then
    say "Existing node chain data was found in $node_dir/mainnet/BdagChain; skipping chain DB download."
    return 0
  elif (( has_anything == 1 && has_chain == 0 )); then
    warn "Node data directory exists but does not contain mainnet/BdagChain; it will be parked before snapshot import."
  fi

  archive="$(download_chain_db_archive "$url" "$cache_dir" "$min_bytes")"

  if (( has_anything == 1 )); then
    stop_node_for_chain_db_replace
  fi

  mkdir -p "$parent"
  stamp="$(date +%Y%m%d-%H%M%S)"
  staging="$parent/.node-chain-db-install-$stamp"
  backup="$node_dir.backup.$stamp"
  rm -rf "$staging"
  mkdir -p "$staging"
  network_dir="$staging/mainnet"

  case "$(basename "$archive")" in
    *.tar.zst|*.tzst|*.tar.gz|*.tgz|*.tar)
      say "Extracting chain DB datadir archive into staged node data"
      if ! extract_chain_db_datadir_archive "$archive" "$staging"; then
        rm -rf "$staging" "${staging}.extract"
        echo "Chain DB datadir archive extraction failed." >&2
        exit 1
      fi
      ;;
    *.bdsnap)
      say "Importing chain DB snapshot archive into staged node data"
      if ! import_chain_db_archive "$archive" "$network_dir"; then
        rm -rf "$staging"
        echo "Chain DB snapshot import failed." >&2
        exit 1
      fi
      ;;
    *)
      warn "Downloaded chain DB snapshot archive does not use a .tar or .bdsnap filename; trying .bdsnap import validation."
      if ! import_chain_db_archive "$archive" "$network_dir"; then
        rm -rf "$staging"
        echo "Chain DB snapshot import failed." >&2
        exit 1
      fi
      ;;
  esac

  if [[ ! -d "$network_dir/BdagChain" ]]; then
    rm -rf "$staging"
    echo "Chain DB snapshot import did not create expected layout: mainnet/BdagChain" >&2
    exit 1
  fi

  if [[ -e "$node_dir" && "$has_anything" == "1" ]]; then
    mv "$node_dir" "$backup"
    echo "Previous node data parked at: $backup"
  elif [[ -d "$node_dir" ]]; then
    rmdir "$node_dir" 2>/dev/null || true
  fi
  mv "$staging" "$node_dir"
  echo "Imported chain DB snapshot into: $node_dir"

  if [[ "$keep" == "0" ]]; then
    rm -f "$archive"
  fi
}

start_stack() {
  say "Starting BlockDAG node for initial sync"
  guard_runtime_compose
  python3 ops/automation_control.py ensure-normal \
    --owner release-installer \
    --owner-unit release-install \
    --reason "Provision default automation control before sync-only first start" >/dev/null
  if [[ "${BDAG_RELEASE_PULL_BASE_IMAGES:-0}" == "1" ]]; then
    compose_cmd pull postgres || true
  else
    warn "Skipping implicit image pulls. Set BDAG_RELEASE_PULL_BASE_IMAGES=1 for an explicit base-image refresh."
  fi
  compose_cmd up -d --no-build --pull never node
  compose_cmd ps
}

wait_for_node_sync() {
  say "Waiting for node sync to complete"
  python3 ops/wait_for_node_sync.py
}

install_stack_support_services() {
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
  case "${BDAG_INSTALL_STACK_SUPPORT_SERVICES:-1}" in
    0|false|False|no|No|off|Off)
      warn "Skipping P2P/mining-host support services because BDAG_INSTALL_STACK_SUPPORT_SERVICES=${BDAG_INSTALL_STACK_SUPPORT_SERVICES:-0}."
      return 0
      ;;
  esac
  if [[ ! -f ops/install-p2p-services.sh ]]; then
    warn "Cannot install P2P/mining-host support services: ops/install-p2p-services.sh is missing."
    return 0
  fi

  say "Installing P2P and mining-host tuning services"
  if bash ops/install-p2p-services.sh; then
    return 0
  fi
  if [[ "${BDAG_INSTALL_STACK_SUPPORT_SERVICES_STRICT:-0}" == "1" ]]; then
    echo "Support service installation failed and strict mode is enabled." >&2
    exit 1
  fi
  warn "Support service installation failed. Continuing because BDAG_INSTALL_STACK_SUPPORT_SERVICES_STRICT=0."
}

discover_preserved_chain_peers() {
  local runtime_dir manifest
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
  if [[ "${BDAG_CHAIN_PEERSTORE_PEER_EXTRACTION_ENABLED:-1}" == "0" ]]; then
    warn "Preserved chain peerstore extraction disabled by BDAG_CHAIN_PEERSTORE_PEER_EXTRACTION_ENABLED=0."
    return 0
  fi
  if [[ ! -f ops/update-local-peers.py ]]; then
    warn "Cannot extract preserved chain peers: missing ops/update-local-peers.py."
    return 0
  fi
  runtime_dir="$(env_path_value BDAG_RUNTIME_DIR "ops/runtime")"
  manifest="$runtime_dir/peer-discovery-current.json"

  say "Extracting sync peer candidates from preserved chain evidence"
  if python3 ops/update-local-peers.py --env-file "$ROOT/.env" --force-apply; then
    echo "Peer discovery manifest: $manifest"
    if [[ -s "$runtime_dir/live-peers-current.txt" ]]; then
      echo "TCP-open peer candidates:"
      sed 's/^/  /' "$runtime_dir/live-peers-current.txt"
    else
      warn "No TCP-open peer candidates were discovered. Continue only if normal sync readiness later proves the node has peers and fresh templates."
    fi
  else
    warn "Peer discovery from preserved chain evidence failed. Continue only after validating peer connectivity, sync freshness, and template health."
  fi
}

rebuild_dashboard_plot_data() {
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
  if [[ "${BDAG_INSTALL_REBUILD_DASHBOARD_PLOTS:-1}" == "0" ]]; then
    warn "Dashboard plot rebuild disabled by BDAG_INSTALL_REBUILD_DASHBOARD_PLOTS=0."
    return 0
  fi
  if [[ ! -f ops/rebuild_dashboard_plot_history.py ]]; then
    warn "Cannot rebuild dashboard plot data: missing ops/rebuild_dashboard_plot_history.py."
    return 0
  fi

  local runtime_dir log_file hours window_blocks workers
  runtime_dir="$(env_path_value BDAG_RUNTIME_DIR "ops/runtime")"
  mkdir -p "$runtime_dir/logs"
  log_file="$runtime_dir/logs/dashboard-rpc-history-rebuild-install.log"
  hours="${BDAG_INSTALL_REBUILD_DASHBOARD_PLOT_HOURS:-720}"
  window_blocks="${BDAG_INSTALL_REBUILD_DASHBOARD_PLOT_WINDOW_BLOCKS:-64}"
  workers="${BDAG_INSTALL_REBUILD_DASHBOARD_PLOT_WORKERS:-12}"

  say "Rebuilding dashboard Global and Wallet plot data from local chain RPC"
  local cmd=(
    python3 ops/rebuild_dashboard_plot_history.py
    --install
    --write-report
    --hours "$hours"
    --window-blocks "$window_blocks"
    --workers "$workers"
  )
  if command -v ionice >/dev/null 2>&1; then
    cmd=(ionice -c 3 nice -n 19 "${cmd[@]}")
  else
    cmd=(nice -n 19 "${cmd[@]}")
  fi
  if BDAG_DASHBOARD_HISTORY_REBUILD_LOG_FILE="$log_file" "${cmd[@]}" >"$log_file" 2>&1; then
    echo "Dashboard plot rebuild complete: $log_file"
  else
    warn "Dashboard plot rebuild did not finish cleanly. See $log_file."
    tail -n 40 "$log_file" >&2 || true
  fi
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
  enforce_wired_route_policy
  configure_env
  install_appliance_host_profile
  run_appliance_preflight
  load_or_build_images "$arch"
  install_chain_db_archive
  start_stack
  wait_for_node_sync
  install_stack_support_services
  discover_preserved_chain_peers
  rebuild_dashboard_plot_data
  install_dashboard
  configure_miners
  say "Install complete"
  echo "Stratum: ${BDAG_POOL_URL:-$(grep '^BDAG_POOL_URL=' .env | cut -d= -f2-)}"
  echo "Dashboard: http://${BDAG_DASHBOARD_BIND:-127.0.0.1}:${BDAG_DASHBOARD_PORT:-8088}"
  echo "Run ./tools/status.sh for a status check."
}

if [[ "${BDAG_RELEASE_INSTALL_CHAIN_DB_ONLY:-0}" == "1" || "${1:-}" == "--chain-db-only" ]]; then
  init_docker_access
  install_chain_db_archive
  exit 0
fi

main "$@"
