#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 /path/to/pool-stack-docker-<version>-linux-amd64.zip [target-dir]" >&2
  exit 2
fi

ZIP_PATH="$(realpath "$1")"
TARGET_DIR="${2:-/home/ben/repos/test-stack}"
CURRENT_STACK="${CURRENT_STACK:-/home/ben/repos/test-stack}"

[[ -f "$ZIP_PATH" ]] || { echo "zip not found: $ZIP_PATH" >&2; exit 1; }

is_docker_bridge_ip() {
  [[ "$1" =~ ^172\.(1[6-9]|2[0-9]|3[0-1])\. ]]
}

is_link_local_ip() {
  [[ "$1" =~ ^169\.254\. ]]
}

contains_docker_bridge_ip() {
  [[ "$1" =~ (^|[,[:space:]])172\.(1[6-9]|2[0-9]|3[0-1])\. ]]
}

default_cidr() {
  local ipaddr="$1"
  if [[ "$ipaddr" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)\.[0-9]+$ ]]; then
    printf '%s.%s.%s.0/24\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
  else
    printf '192.168.1.0/24\n'
  fi
}

env_file_value() {
  local file="$1"
  local key="$2"
  [[ -f "$file" ]] || return 0
  awk -F= -v key="$key" '$1 == key {value=substr($0, index($0, "=") + 1)} END {print value}' "$file"
}

configured_env_value() {
  local key="$1"
  if [[ ${!key+x} && -n "${!key}" ]]; then
    printf '%s\n' "${!key}"
    return 0
  fi
  local value
  value="$(env_file_value "$TARGET_DIR/.env" "$key")"
  if [[ -n "$value" ]]; then
    printf '%s\n' "$value"
    return 0
  fi
  env_file_value "$CURRENT_STACK/.env" "$key"
}

pool_url_host() {
  local value="$1"
  value="${value#*://}"
  value="${value%%:*}"
  printf '%s\n' "$value"
}

set_env_value() {
  local file="$1"
  local key="$2"
  local value="$3"
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$file"
  fi
}

detect_host_lan_ip() {
  local candidate=""
  candidate="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}' || true)"
  if [[ -n "$candidate" && "$candidate" != 127.* && "$candidate" != "0.0.0.0" ]] && ! is_docker_bridge_ip "$candidate"; then
    printf '%s\n' "$candidate"
    return 0
  fi
  while read -r candidate; do
    if [[ -n "$candidate" && "$candidate" != 127.* && "$candidate" != "0.0.0.0" ]] && ! is_docker_bridge_ip "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done < <(ip -4 -o addr show scope global 2>/dev/null | awk '{split($4, a, "/"); print a[1]}')
  return 1
}

node_conf_bootstrap_peers() {
  awk -F= '$1 == "addpeer" && $2 != "" {
    if (seen[$2]++) {
      next
    }
    if (out != "") {
      out = out "," $2
    } else {
      out = $2
    }
  } END { print out }' "$1"
}

mkdir -p "$TARGET_DIR"
TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

unzip -q "$ZIP_PATH" -d "$TMP_DIR"
ROOT="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -1)"
[[ -n "$ROOT" ]] || { echo "zip did not contain a package root" >&2; exit 1; }

rsync -a --delete \
  --exclude='.env' \
  --exclude='node.conf' \
  --exclude='node-data/' \
  --exclude='data/' \
  --exclude='ops/runtime/' \
  "$ROOT/" "$TARGET_DIR/"

[[ -f "$TARGET_DIR/node.conf.example" ]] || {
  echo "release payload is missing node.conf.example" >&2
  exit 1
}
cp "$TARGET_DIR/node.conf.example" "$TARGET_DIR/node.conf"

if [[ -f "$CURRENT_STACK/.env" && ! -f "$TARGET_DIR/.env" ]]; then
  cp "$CURRENT_STACK/.env" "$TARGET_DIR/.env"
fi
if [[ ! -f "$TARGET_DIR/.env" ]]; then
  cp "$TARGET_DIR/.env.example" "$TARGET_DIR/.env"
fi

if [[ -f "$TARGET_DIR/.env" ]]; then
  set_env_value "$TARGET_DIR/.env" "NODE_DATA_DIR" "./node-data"
  set_env_value "$TARGET_DIR/.env" "BDAG_NODE_DATA_DIR" "./node-data"
  mkdir -p "$TARGET_DIR/node-data"

  set_env_value "$TARGET_DIR/.env" "BDAG_STACK_HOST_ROOT" "$TARGET_DIR"

  asic_lan_interface="$(configured_env_value BDAG_ASIC_LAN_INTERFACE)"
  if [[ -n "$asic_lan_interface" ]]; then
    set_env_value "$TARGET_DIR/.env" "BDAG_ASIC_LAN_INTERFACE" "$asic_lan_interface"
  fi

  detected_pool_host="$(configured_env_value BDAG_POOL_HOST)"
  if [[ -z "$detected_pool_host" ]] || is_docker_bridge_ip "$detected_pool_host"; then
    detected_pool_host="$(detect_host_lan_ip || true)"
  fi
  current_pool_host="$(configured_env_value BDAG_POOL_HOST)"
  if [[ -z "$current_pool_host" || "$current_pool_host" == 127.* || "$current_pool_host" == "0.0.0.0" ]] || is_docker_bridge_ip "$current_pool_host"; then
    if [[ -z "$detected_pool_host" ]]; then
      echo "could not detect a host LAN IP for BDAG_POOL_HOST; set BDAG_POOL_HOST and rerun" >&2
      exit 1
    fi
    set_env_value "$TARGET_DIR/.env" "BDAG_POOL_HOST" "$detected_pool_host"
    current_pool_host="$detected_pool_host"
  else
    set_env_value "$TARGET_DIR/.env" "BDAG_POOL_HOST" "$current_pool_host"
  fi
  if is_link_local_ip "$current_pool_host" && [[ -z "$asic_lan_interface" ]]; then
    echo "BDAG_POOL_HOST=$current_pool_host is link-local; set BDAG_ASIC_LAN_INTERFACE to the miner cable interface and rerun" >&2
    exit 1
  fi

  pool_url="$(configured_env_value BDAG_POOL_URL)"
  pool_url_current_host="$(pool_url_host "$pool_url")"
  if [[ -z "$pool_url" || "$pool_url_current_host" != "$current_pool_host" || "$pool_url_current_host" == 127.* || "$pool_url_current_host" == "0.0.0.0" ]] || is_docker_bridge_ip "$pool_url_current_host"; then
    pool_url="stratum+tcp://$current_pool_host:3334"
  fi
  set_env_value "$TARGET_DIR/.env" "BDAG_POOL_URL" "$pool_url"

  asic_cidrs="$(configured_env_value BDAG_ASIC_LAN_CIDRS)"
  miner_scan_target="$(configured_env_value BDAG_MINER_SCAN_TARGET)"
  if [[ -z "$miner_scan_target" ]] || contains_docker_bridge_ip "$miner_scan_target"; then
    miner_scan_target="${asic_cidrs:-$(default_cidr "$current_pool_host")}"
  fi
  if [[ -z "$asic_cidrs" ]] || contains_docker_bridge_ip "$asic_cidrs"; then
    asic_cidrs="$miner_scan_target"
  fi
  set_env_value "$TARGET_DIR/.env" "BDAG_MINER_SCAN_TARGET" "$miner_scan_target"
  set_env_value "$TARGET_DIR/.env" "BDAG_ASIC_LAN_CIDRS" "$asic_cidrs"

  if grep -q '^BDAG_OPS_UID=' "$TARGET_DIR/.env"; then
    sed -i "s|^BDAG_OPS_UID=.*|BDAG_OPS_UID=$(id -u)|" "$TARGET_DIR/.env"
  else
    printf '\nBDAG_OPS_UID=%s\n' "$(id -u)" >> "$TARGET_DIR/.env"
  fi
  if grep -q '^BDAG_OPS_GID=' "$TARGET_DIR/.env"; then
    sed -i "s|^BDAG_OPS_GID=.*|BDAG_OPS_GID=$(id -g)|" "$TARGET_DIR/.env"
  else
    printf 'BDAG_OPS_GID=%s\n' "$(id -g)" >> "$TARGET_DIR/.env"
  fi

  docker_socket_gid="0"
  if [[ -S /var/run/docker.sock ]]; then
    docker_socket_gid="$(stat -c '%g' /var/run/docker.sock 2>/dev/null || printf '0')"
  fi
  if grep -q '^BDAG_DOCKER_SOCKET_GID=' "$TARGET_DIR/.env"; then
    sed -i "s|^BDAG_DOCKER_SOCKET_GID=.*|BDAG_DOCKER_SOCKET_GID=${docker_socket_gid}|" "$TARGET_DIR/.env"
  else
    printf 'BDAG_DOCKER_SOCKET_GID=%s\n' "$docker_socket_gid" >> "$TARGET_DIR/.env"
  fi

  p2p_advertise_ip="$(awk -F= '$1 == "BDAG_P2P_ADVERTISE_IP" {print $2; exit}' "$TARGET_DIR/.env")"
  mining_pool_address="$(awk -F= '$1 == "MINING_POOL_ADDRESS" {print $2; exit}' "$TARGET_DIR/.env")"
  if [[ -f "$TARGET_DIR/node.conf" ]]; then
    bootstrap_peer_addresses="$(node_conf_bootstrap_peers "$TARGET_DIR/node.conf")"
    if [[ -n "$bootstrap_peer_addresses" ]]; then
      set_env_value "$TARGET_DIR/.env" "BOOTSTRAP_PEER_ADDRESSES" "$bootstrap_peer_addresses"
    fi
    if [[ -n "$mining_pool_address" ]]; then
      if grep -q '^miningaddr=' "$TARGET_DIR/node.conf"; then
        sed -i "s|^miningaddr=.*|miningaddr=${mining_pool_address}|" "$TARGET_DIR/node.conf"
      else
        printf '\nminingaddr=%s\n' "$mining_pool_address" >> "$TARGET_DIR/node.conf"
      fi
    fi
    if [[ -n "$p2p_advertise_ip" ]]; then
      if grep -q '^externalip=' "$TARGET_DIR/node.conf"; then
        sed -i "s|^externalip=.*|externalip=${p2p_advertise_ip}|" "$TARGET_DIR/node.conf"
      else
        printf '\nexternalip=%s\n' "$p2p_advertise_ip" >> "$TARGET_DIR/node.conf"
      fi
    else
      sed -i '/^externalip=/d' "$TARGET_DIR/node.conf"
    fi
  fi
fi

cat <<EOF
Deployed test release files to:
  $TARGET_DIR

Validate:
  cd "$TARGET_DIR"
  BDAG_STACK_HOST_ROOT="$TARGET_DIR" docker compose --env-file .env config >/tmp/stack-test-compose.yml

This script does not start containers because the compose file uses fixed
container_name values that would conflict with the running stack.
EOF
