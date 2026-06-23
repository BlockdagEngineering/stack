#!/usr/bin/env bash
set -euo pipefail

INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="$(cd "$INSTALLER_DIR/.." && pwd)"
cd "$PACKAGE_ROOT"

OS_NAME="${BDAG_INSTALL_OS:-$(uname -s | tr '[:upper:]' '[:lower:]')}"
ARCH_NAME="${BDAG_INSTALL_ARCH:-$(uname -m)}"
PAYLOAD_METADATA_FILE="$PACKAGE_ROOT/release-payload.env"
BDAG_RELEASE_PAYLOAD_TARGET=""
BDAG_RELEASE_PAYLOAD_ARCH=""
BDAG_RELEASE_PAYLOAD_DOCKER_PLATFORM=""
INSTALL_MODE="${BDAG_INSTALL_MODE:-}"
DEPLOY_KIND="${BDAG_DEPLOY_KIND:-}"
CHAIN_MODE="${BDAG_CHAIN_MODE:-}"
BDAG_NODE_ARCHIVAL=0
BDAG_INSTALL_MIN_FREE_KB="${BDAG_INSTALL_MIN_FREE_KB:-10485760}"
BDAG_INSTALL_CHECK_PORTS="${BDAG_INSTALL_CHECK_PORTS:-3334 8080 9280 18545 18546 38131}"
BDAG_INSTALL_STRICT_PORTS="${BDAG_INSTALL_STRICT_PORTS:-0}"
BDAG_CLEAN_ORPHAN_CONTAINERS="${BDAG_CLEAN_ORPHAN_CONTAINERS:-0}"

echo "=== BlockDAG Pool Stack Installer (${OS_NAME}/${ARCH_NAME}) ==="
echo ""

require_command() {
    local name="$1"
    local hint="$2"
    if ! command -v "$name" >/dev/null 2>&1; then
        echo "Error: $name is required. $hint" >&2
        exit 1
    fi
}

read_payload_metadata() {
    [[ -f "$PAYLOAD_METADATA_FILE" ]] || return 0

    local key value
    while IFS='=' read -r key value || [[ -n "$key" ]]; do
        case "$key" in
            ''|\#*) continue ;;
            BDAG_RELEASE_PAYLOAD_TARGET) BDAG_RELEASE_PAYLOAD_TARGET="$value" ;;
            BDAG_RELEASE_PAYLOAD_ARCH) BDAG_RELEASE_PAYLOAD_ARCH="$value" ;;
            DOCKER_PLATFORM) BDAG_RELEASE_PAYLOAD_DOCKER_PLATFORM="$value" ;;
        esac
    done < "$PAYLOAD_METADATA_FILE"

    if [[ -z "$BDAG_RELEASE_PAYLOAD_ARCH" ]]; then
        case "$BDAG_RELEASE_PAYLOAD_TARGET" in
            linux-amd64) BDAG_RELEASE_PAYLOAD_ARCH=amd64 ;;
            linux-arm64) BDAG_RELEASE_PAYLOAD_ARCH=arm64 ;;
        esac
    fi
}

normalize_arch() {
    case "$1" in
        x86_64|amd64) printf '%s\n' amd64 ;;
        arm64|aarch64) printf '%s\n' arm64 ;;
        *)
            echo "Error: unsupported CPU architecture '${1}'." >&2
            exit 1
            ;;
    esac
}

resolve_docker_platform() {
    local payload_arch expected_platform
    read_payload_metadata
    payload_arch="${BDAG_RELEASE_PAYLOAD_ARCH:-$(normalize_arch "$ARCH_NAME")}"
    payload_arch="$(normalize_arch "$payload_arch")"
    expected_platform="linux/${payload_arch}"

    if [[ -n "$BDAG_RELEASE_PAYLOAD_DOCKER_PLATFORM" && "$BDAG_RELEASE_PAYLOAD_DOCKER_PLATFORM" != "$expected_platform" ]]; then
        echo "Error: release-payload.env has inconsistent DOCKER_PLATFORM=${BDAG_RELEASE_PAYLOAD_DOCKER_PLATFORM}; expected ${expected_platform}." >&2
        exit 1
    fi

    DOCKER_PLATFORM="$expected_platform"
}

DOCKER_PLATFORM=""
resolve_docker_platform
export DOCKER_PLATFORM

if [[ -n "$BDAG_RELEASE_PAYLOAD_TARGET" ]]; then
    echo "Runtime payload: ${BDAG_RELEASE_PAYLOAD_TARGET} (${DOCKER_PLATFORM})"
    echo ""
fi

sed_escape() {
    printf '%s' "$1" | sed 's/[\/&|]/\\&/g'
}

inplace_sed() {
    if [[ "$OS_NAME" == "macos" ]]; then
        sed -i '' "$@"
    else
        sed -i "$@"
    fi
}

generate_postgres_password() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 32 | tr -d '\n'
        return 0
    fi

    od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
}

compose_project_name() {
    docker compose config --format json 2>/dev/null \
        | sed -n 's/^[[:space:]]*"name":[[:space:]]*"\([^"]*\)".*/\1/p' \
        | head -n 1
}

warn_or_fail_preflight() {
    local message="$1"
    if [[ "${BDAG_INSTALL_STRICT_PREFLIGHT:-0}" == "1" ]]; then
        echo "Error: $message" >&2
        exit 1
    fi
    echo "Warning: $message" >&2
}

port_in_use() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}$"
        return $?
    fi
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
        return $?
    fi
    return 1
}

run_release_preflight() {
    echo "=== Release preflight ==="

    case "$ARCH_NAME" in
        x86_64|amd64|arm64|aarch64) ;;
        *) warn_or_fail_preflight "unsupported CPU architecture '${ARCH_NAME}'." ;;
    esac

    local free_kb
    free_kb="$(df -Pk . 2>/dev/null | awk 'NR==2 {print $4}')"
    if [[ -n "$free_kb" && "$free_kb" -lt "$BDAG_INSTALL_MIN_FREE_KB" ]]; then
        warn_or_fail_preflight "free disk ${free_kb}KB is below BDAG_INSTALL_MIN_FREE_KB=${BDAG_INSTALL_MIN_FREE_KB}KB."
    fi

    local port busy_ports=()
    for port in $BDAG_INSTALL_CHECK_PORTS; do
        if port_in_use "$port"; then
            busy_ports+=("$port")
        fi
    done
    if [[ "${#busy_ports[@]}" -gt 0 ]]; then
        if [[ "$BDAG_INSTALL_STRICT_PORTS" == "1" ]]; then
            echo "Error: host ports already listening: ${busy_ports[*]}" >&2
            exit 1
        fi
        echo "Warning: host ports already listening: ${busy_ports[*]}. Existing stack services may be using them." >&2
    fi

    if command -v timedatectl >/dev/null 2>&1; then
        local ntp
        ntp="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)"
        [[ "$ntp" == "yes" ]] || warn_or_fail_preflight "system time is not NTP synchronized."
    fi

    if command -v jq >/dev/null 2>&1; then
        echo "jq found; release scripts do not require it for installer JSON parsing."
    else
        echo "jq not found; continuing because installer parsing avoids a jq dependency."
    fi

    echo ""
}

plan_orphan_container_cleanup() {
    local project
    project="$(compose_project_name || true)"
    [[ -n "$project" ]] || return 0

    local containers
    containers="$(docker ps -a --filter "label=com.docker.compose.project=${project}" --format '{{.Names}}\t{{.Status}}' 2>/dev/null || true)"
    [[ -n "$containers" ]] || return 0

    echo ""
    echo "Compose project '${project}' has existing containers:"
    printf '%s\n' "$containers" | sed 's/^/  /'
    if [[ "$BDAG_CLEAN_ORPHAN_CONTAINERS" == "1" ]]; then
        echo "BDAG_CLEAN_ORPHAN_CONTAINERS=1; running docker compose down --remove-orphans before start."
        docker compose down --remove-orphans || true
    else
        echo "Dry-run cleanup only. Set BDAG_CLEAN_ORPHAN_CONTAINERS=1 to remove old/orphan compose containers during install."
    fi
}

clean_build_context_metadata() {
    # OS metadata files appear on macOS/Windows/external-volume workflows and can
    # make Docker Desktop fail or unnecessarily pollute the build context.
    find . -name '._*' -type f -exec rm -f {} + 2>/dev/null || true
    find . -name '.DS_Store' -type f -exec rm -f {} + 2>/dev/null || true
    find . -iname 'Thumbs.db' -type f -exec rm -f {} + 2>/dev/null || true
    find . -iname 'desktop.ini' -type f -exec rm -f {} + 2>/dev/null || true
    find . -name '__MACOSX' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find . -name '$RECYCLE.BIN' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find . -name 'System Volume Information' -type d -prune -exec rm -rf {} + 2>/dev/null || true
}

set_env_value() {
    local file="$1"
    local key="$2"
    local value="$3"
    local escaped
    escaped="$(sed_escape "$value")"
    if grep -q "^${key}=" "$file"; then
        inplace_sed "s|^${key}=.*|${key}=${escaped}|" "$file"
    else
        printf '\n%s=%s\n' "$key" "$value" >> "$file"
    fi
}

env_file_value() {
    local file="$1" key="$2" value
    value="$(grep -E "^${key}=" "$file" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    printf '%s\n' "$value"
}

absolute_package_path() {
    local path="$1"
    if [[ "$path" == /* ]]; then
        printf '%s\n' "$path"
    else
        printf '%s\n' "$PACKAGE_ROOT/${path#./}"
    fi
}

ensure_node_datadir_bind_mount() {
    local node_data_dir node_data_path
    node_data_dir="$(env_file_value .env NODE_DATA_DIR)"
    if [[ -z "$node_data_dir" ]]; then
        node_data_dir="./node-data"
    fi
    node_data_path="$(absolute_package_path "$node_data_dir")"
    if [[ -e "$node_data_path" && ! -d "$node_data_path" ]]; then
        echo "Error: NODE_DATA_DIR points at a non-directory: $node_data_path" >&2
        exit 1
    fi
    mkdir -p "$node_data_path"
    set_env_value .env NODE_DATA_DIR "$node_data_dir"
    set_env_value .env BDAG_NODE_DATA_DIR "$node_data_dir"
    echo "Node data bind mount: $node_data_path"
}

apply_chain_db_archive_env_overrides() {
    local key
    for key in \
        BDAG_CHAIN_DB_ARCHIVE_URL \
        BDAG_CHAIN_DB_ARCHIVE_MIN_BYTES \
        BDAG_CHAIN_DB_ARCHIVE_KEEP \
        BDAG_CHAIN_DB_ARCHIVE_FORCE \
        BDAG_CHAIN_DB_IMPORT_BINARY \
        BDAG_CHAIN_DB_IMPORT_BATCH_BYTES \
        BDAG_CHAIN_DB_IMPORT_NO_NETWORK_CHECK; do
        if [[ -n "${!key:-}" ]]; then
            set_env_value .env "$key" "${!key}"
        fi
    done
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
        ip -o -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}' || true
    fi
}

wired_route_policy_script() {
    local candidate
    for candidate in \
        "$PACKAGE_ROOT/scripts/validate-network-route-policy.py" \
        "$PACKAGE_ROOT/../scripts/validate-network-route-policy.py" \
        "$PACKAGE_ROOT/validate-network-route-policy.py"; do
        if [[ -f "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

enforce_wired_route_policy() {
    if [[ "$OS_NAME" != "linux" ]]; then
        return 0
    fi
    if [[ "${BDAG_ENFORCE_WIRED_ROUTE_POLICY:-1}" != "1" ]]; then
        echo "Skipping wired-first route policy because BDAG_ENFORCE_WIRED_ROUTE_POLICY=${BDAG_ENFORCE_WIRED_ROUTE_POLICY:-unset}."
        return 0
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        echo "Warning: python3 is missing; cannot validate or apply wired-first route policy." >&2
        return 0
    fi
    local script
    script="$(wired_route_policy_script || true)"
    if [[ -z "$script" ]]; then
        echo "Warning: wired-first route policy script is missing from this package." >&2
        return 0
    fi
    echo "=== Applying wired-first route policy ==="
    if ! python3 "$script" --apply --warn-only; then
        echo "Warning: wired-first route policy application failed; continuing so later checks can report the remaining network state." >&2
    fi
    echo ""
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

run_privileged() {
    if [[ "$(id -u)" == "0" ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        echo "Error: $* requires root privileges. Re-run this installer with sudo or install sudo." >&2
        return 1
    fi
}

validate_pool_lan_config() {
    local pool_host pool_url pool_url_host scan_target asic_cidrs asic_iface allow_bridge
    pool_host="$(env_file_value .env BDAG_POOL_HOST)"
    pool_url="$(env_file_value .env BDAG_POOL_URL)"
    scan_target="$(env_file_value .env BDAG_MINER_SCAN_TARGET)"
    asic_cidrs="$(env_file_value .env BDAG_ASIC_LAN_CIDRS)"
    asic_iface="$(env_file_value .env BDAG_ASIC_LAN_INTERFACE)"
    allow_bridge="$(env_file_value .env BDAG_ALLOW_DOCKER_BRIDGE_ASIC_IPS)"
    allow_bridge="${allow_bridge:-0}"
    pool_url_host="${pool_url#*://}"
    pool_url_host="${pool_url_host%%:*}"
    if [[ -z "$pool_host" || -z "$pool_url" || -z "$scan_target" || -z "$asic_cidrs" ]]; then
        echo "Error: pool LAN configuration is incomplete. Set BDAG_POOL_HOST, BDAG_POOL_URL, BDAG_MINER_SCAN_TARGET, and BDAG_ASIC_LAN_CIDRS." >&2
        exit 1
    fi
    if [[ "$allow_bridge" != "1" && "$allow_bridge" != "true" && "$allow_bridge" != "True" ]]; then
        if is_default_docker_bridge_address "$pool_host" || is_default_docker_bridge_address "$pool_url_host"; then
            echo "Error: refusing Docker bridge pool endpoint '$pool_url'. Use the host-facing ASIC LAN IP, not a 172.16.0.0/12 container address." >&2
            exit 1
        fi
        if [[ "$scan_target" =~ (^|[,[:space:]])172\.(1[6-9]|2[0-9]|3[0-1])\. || "$asic_cidrs" =~ (^|[,[:space:]])172\.(1[6-9]|2[0-9]|3[0-1])\. ]]; then
            echo "Error: refusing Docker bridge ASIC scan scope '$asic_cidrs'. Set BDAG_ASIC_LAN_CIDRS to the physical ASIC LAN." >&2
            exit 1
        fi
    fi
    if is_link_local_ipv4 "$pool_host" || is_link_local_ipv4 "$pool_url_host"; then
        if [[ -z "$asic_iface" ]]; then
            echo "Error: link-local ASIC pool endpoint '$pool_url' requires BDAG_ASIC_LAN_INTERFACE so the host can own $pool_host on the miner cable." >&2
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
        echo "Error: the ip command is required to bind link-local ASIC pool address $pool_host on $asic_iface." >&2
        exit 1
    fi
    if ! ip link show "$asic_iface" >/dev/null 2>&1; then
        echo "Error: ASIC LAN interface '$asic_iface' was not found; set BDAG_ASIC_LAN_INTERFACE to the cabled miner interface." >&2
        exit 1
    fi
    if ip -o -4 addr show dev "$asic_iface" 2>/dev/null \
        | awk -v want="$pool_host" '{split($4,a,"/"); if (a[1] == want) found=1} END {exit found ? 0 : 1}'; then
        return 0
    fi
    echo "Adding link-local ASIC pool address $pool_host/16 to $asic_iface."
    run_privileged ip addr replace "$pool_host/16" dev "$asic_iface" scope link
}

prompt_with_default() {
    local prompt="$1" default_value="$2" value
    read -rp "$prompt [$default_value]: " value
    printf '%s\n' "${value:-$default_value}"
}

normalize_deploy_kind() {
    case "$1" in
        1|pool|pool-stack) printf 'pool\n' ;;
        2|node|standalone|standalone-node) printf 'node\n' ;;
        *) return 1 ;;
    esac
}

normalize_chain_mode() {
    case "$1" in
        1|non-archive|nonarchive|pruned) printf 'non-archive\n' ;;
        2|archive|full) printf 'archive\n' ;;
        *) return 1 ;;
    esac
}

# Legacy combined override. BDAG_INSTALL_MODE pre-seeds both dimensions so older
# non-interactive callers keep working; explicit BDAG_DEPLOY_KIND/BDAG_CHAIN_MODE
# take precedence over whatever the legacy value would imply.
seed_dimensions_from_install_mode() {
    [[ -n "$INSTALL_MODE" ]] || return 0
    case "$INSTALL_MODE" in
        pool|pool-stack)
            DEPLOY_KIND="${DEPLOY_KIND:-pool}"
            ;;
        archive-node)
            DEPLOY_KIND="${DEPLOY_KIND:-node}"
            CHAIN_MODE="${CHAIN_MODE:-archive}"
            ;;
        node|non-archive-node)
            DEPLOY_KIND="${DEPLOY_KIND:-node}"
            CHAIN_MODE="${CHAIN_MODE:-non-archive}"
            ;;
        *)
            echo "Error: invalid BDAG_INSTALL_MODE '${INSTALL_MODE}'. Use pool, archive-node, or node." >&2
            exit 1
            ;;
    esac
}

# Step 1: pool stack vs standalone node.
select_deploy_kind() {
    if [[ -n "$DEPLOY_KIND" ]]; then
        if ! DEPLOY_KIND="$(normalize_deploy_kind "$DEPLOY_KIND")"; then
            echo "Error: invalid deployment '${DEPLOY_KIND}'. Use pool or node." >&2
            exit 1
        fi
        echo "Deployment: ${DEPLOY_KIND} (preselected)"
        return 0
    fi

    echo "Step 1/2 - Select what to install:"
    echo "  1) Mining pool stack with dashboard (default)"
    echo "  2) Standalone node only"
    local choice
    while true; do
        read -rp "Choice [1]: " choice
        if DEPLOY_KIND="$(normalize_deploy_kind "${choice:-1}")"; then
            break
        fi
        echo "Please enter 1 or 2."
    done
    echo ""
}

# Step 2: archive vs non-archive chain data (applies to both deployments).
select_chain_mode() {
    if [[ -n "$CHAIN_MODE" ]]; then
        if ! CHAIN_MODE="$(normalize_chain_mode "$CHAIN_MODE")"; then
            echo "Error: invalid chain mode '${CHAIN_MODE}'. Use archive or non-archive." >&2
            exit 1
        fi
        echo "Chain data: ${CHAIN_MODE} (preselected)"
        echo ""
        return 0
    fi

    echo "Step 2/2 - Select chain data type:"
    echo "  1) Non-archive (pruned chain data, default)"
    echo "  2) Archive (keeps full block history, no pruning)"
    local choice
    while true; do
        read -rp "Choice [1]: " choice
        if CHAIN_MODE="$(normalize_chain_mode "${choice:-1}")"; then
            break
        fi
        echo "Please enter 1 or 2."
    done
    echo ""
}

# Resolves the archival flag for the selected chain mode. Chain DB snapshot
# download/import is handled by ops/release-install.sh after .env is
# written, using BDAG_CHAIN_DB_ARCHIVE_URL from .env.example by default.
resolve_mode_settings() {
    if [[ "$CHAIN_MODE" == "archive" ]]; then
        BDAG_NODE_ARCHIVAL=1
    else
        BDAG_NODE_ARCHIVAL=0
    fi
    echo "Chain snapshot source: BDAG_CHAIN_DB_ARCHIVE_URL from .env"
    echo ""
}

install_mode_is_node_only() {
    [[ "$DEPLOY_KIND" == "node" ]]
}

if [[ "${BDAG_INSTALL_TEST_WRITE_ENV_ONLY:-0}" == "1" ]]; then
    cp .env.example .env
    set_env_value .env DOCKER_PLATFORM "$DOCKER_PLATFORM"
    apply_chain_db_archive_env_overrides
    ensure_node_datadir_bind_mount
    exit 0
fi

print_docker_install_instructions() {
    cat >&2 <<'DOCKER_INSTRUCTIONS'

Install Docker Engine first, then re-run this installer.

Quick install (most Linux distros):

  curl -fsSL https://get.docker.com | sh

Then enable the daemon and let your user run docker without sudo:

  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER"
  newgrp docker   # or log out and back in

Verify everything works:

  docker run --rm hello-world
  docker compose version

Notes:
  - Avoid your distro's docker.io package; it is often outdated.
  - Membership in the docker group is root-equivalent on this host. On a
    multi-admin box, skip the usermod step and run the installer with a
    user that can sudo docker instead.
DOCKER_INSTRUCTIONS
}

if ! command -v docker >/dev/null 2>&1; then
    echo "Error: Docker is not installed." >&2
    print_docker_install_instructions
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "Error: Docker is installed but the Docker Compose v2 plugin is missing." >&2
    print_docker_install_instructions
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    echo "Error: Docker is installed but this user cannot reach the Docker daemon." >&2
    cat >&2 <<'DOCKER_ACCESS'

Fix daemon access, then re-run this installer:

  sudo systemctl enable --now docker     # make sure the daemon is running
  sudo usermod -aG docker "$USER"        # allow docker without sudo
  newgrp docker                          # or log out and back in
DOCKER_ACCESS
    exit 1
fi
require_command curl "Install curl so the host-side chain snapshot importer can download the node database."

if [[ ! -f .env.example || ! -f node.conf.example || ! -f docker-compose.yml ]]; then
    echo "Error: run this installer from the extracted pool-stack-docker release folder." >&2
    exit 1
fi

seed_dimensions_from_install_mode
select_deploy_kind
select_chain_mode
resolve_mode_settings

run_release_preflight
enforce_wired_route_policy

echo ""
echo "=== Configuration ==="
echo ""

if [[ -n "${POSTGRES_PASSWORD:-}" ]]; then
    echo "Using POSTGRES_PASSWORD from environment."
else
    # Always set; docker-compose interpolation requires a value even when the
    # pool database service is not started (node-only installs).
    POSTGRES_PASSWORD="$(generate_postgres_password)"
    echo "Generated Postgres password."
fi

cp .env.example .env
set_env_value .env POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
set_env_value .env DOCKER_PLATFORM "$DOCKER_PLATFORM"
set_env_value .env BDAG_NODE_ARCHIVAL "$BDAG_NODE_ARCHIVAL"
apply_chain_db_archive_env_overrides
ensure_node_datadir_bind_mount

if install_mode_is_node_only; then
    echo "Node-only install: skipping pool, dashboard, and ASIC configuration."
else
    read -rp "Mining/earnings wallet address (0x...): " MINING_ADDR
    read -rsp "Pool operator private key (optional, hidden; press Enter to skip): " POOL_PRIVATE_KEY
    echo ""

    DETECTED_POOL_LAN_IP="$(detect_lan_ip || true)"
    POOL_LAN_IP="$(prompt_with_default "Pool LAN IP miners should connect to" "${BDAG_POOL_HOST:-${DETECTED_POOL_LAN_IP:-192.168.1.10}}")"
    ASIC_LAN_INTERFACE="${BDAG_ASIC_LAN_INTERFACE:-}"
    if is_link_local_ipv4 "$POOL_LAN_IP"; then
        ASIC_LAN_INTERFACE="$(prompt_with_default "Host interface for link-local ASIC LAN" "$ASIC_LAN_INTERFACE")"
    fi
    MINER_SCAN_TARGET="$(prompt_with_default "LAN scan range for ASIC discovery" "${BDAG_MINER_SCAN_TARGET:-${BDAG_ASIC_LAN_CIDRS:-$(default_cidr "$POOL_LAN_IP")}}")"
    set_env_value .env MINING_POOL_ADDRESS "$MINING_ADDR"
    set_env_value .env BDAG_POOL_HOST "$POOL_LAN_IP"
    set_env_value .env BDAG_POOL_URL "stratum+tcp://$POOL_LAN_IP:3334"
    set_env_value .env BDAG_ASIC_LAN_INTERFACE "$ASIC_LAN_INTERFACE"
    set_env_value .env BDAG_MINER_SCAN_TARGET "$MINER_SCAN_TARGET"
    set_env_value .env BDAG_ASIC_LAN_CIDRS "$MINER_SCAN_TARGET"
    validate_pool_lan_config
    ensure_asic_lan_address "$POOL_LAN_IP" "$ASIC_LAN_INTERFACE"
    if [[ -n "$POOL_PRIVATE_KEY" ]]; then
        set_env_value .env POOL_PRIVATE_KEY "$POOL_PRIVATE_KEY"
    fi
fi

cp node.conf.example node.conf
if ! install_mode_is_node_only; then
    if grep -q '^miningaddr=' node.conf; then
        inplace_sed "s|^miningaddr=.*|miningaddr=$(sed_escape "$MINING_ADDR")|" node.conf
    else
        printf '\nminingaddr=%s\n' "$MINING_ADDR" >> node.conf
    fi
fi

if [[ -n "${BDAG_P2P_ADVERTISE_IP:-}" ]]; then
    echo ""
    echo "Using explicit P2P advertised IP: ${BDAG_P2P_ADVERTISE_IP}"
    if grep -q '^# externalip=' node.conf; then
        inplace_sed "s|^# externalip=.*|externalip=$(sed_escape "$BDAG_P2P_ADVERTISE_IP")|" node.conf
    elif grep -q '^externalip=' node.conf; then
        inplace_sed "s|^externalip=.*|externalip=$(sed_escape "$BDAG_P2P_ADVERTISE_IP")|" node.conf
    else
        printf '\nexternalip=%s\n' "$BDAG_P2P_ADVERTISE_IP" >> node.conf
    fi
    set_env_value .env BDAG_P2P_ADVERTISE_IP "$BDAG_P2P_ADVERTISE_IP"
else
    echo ""
    echo "No BDAG_P2P_ADVERTISE_IP set; leaving node externalip unset for outbound P2P sync."
    sed -i '/^externalip=/d' node.conf
fi

if ! install_mode_is_node_only; then
    mkdir -p collector/logs
fi

clean_build_context_metadata
plan_orphan_container_cleanup

export DOCKER_DEFAULT_PLATFORM="$DOCKER_PLATFORM"

# Intentionally unquoted below: empty for full-stack builds, one service name
# for node-only builds.
BUILD_SERVICES=""
if install_mode_is_node_only; then
    BUILD_SERVICES="node"
fi

echo ""
echo "=== Building Docker images (${DOCKER_PLATFORM}) ==="
echo ""
if [[ -x ./scripts/bdag-low-io-build.sh ]]; then
    ./scripts/bdag-low-io-build.sh docker compose build $BUILD_SERVICES
elif command -v ionice >/dev/null 2>&1; then
    ionice -c 3 nice -n 19 docker compose build $BUILD_SERVICES
else
    nice -n 19 docker compose build $BUILD_SERVICES
fi

bootstrap_chain_db_archive() {
    local archive_url
    archive_url="$(env_file_value .env BDAG_CHAIN_DB_ARCHIVE_URL)"
    if [[ -z "$archive_url" ]]; then
        echo ""
        echo "No BDAG_CHAIN_DB_ARCHIVE_URL set; node will sync from peers."
        return 0
    fi
    if [[ ! -f ops/release-install.sh ]]; then
        echo "Error: ops/release-install.sh is missing; cannot bootstrap node data before start." >&2
        exit 1
    fi

    echo ""
    echo "=== Bootstrapping node data ==="
    BDAG_RELEASE_INSTALL_CHAIN_DB_ONLY=1 bash ops/release-install.sh --chain-db-only
}

wait_for_node_sync() {
    echo ""
    echo "=== Waiting for node sync ==="
    python3 ops/wait_for_node_sync.py
}

bootstrap_chain_db_archive

if install_mode_is_node_only; then
    echo ""
    echo "=== Starting node ==="
    docker compose up -d --no-build --pull never node
else
    echo ""
    echo "=== Starting node for initial sync ==="
    python3 ops/automation_control.py ensure-normal \
        --owner release-installer \
        --owner-unit install-unix-common \
        --reason "Provision default automation control before sync-only first start" >/dev/null
    docker compose up -d --no-build --pull never node
fi

wait_for_node_sync

if install_mode_is_node_only; then
    NODE_KIND="non-archive"
    if [[ "$BDAG_NODE_ARCHIVAL" == "1" ]]; then
        NODE_KIND="archive"
    fi
    cat <<EOF

=================================================
  BlockDAG ${NODE_KIND} node is running.
=================================================
  P2P:        port 8150
  Chain RPC:  http://localhost:38131
  EVM RPC:    http://localhost:18545

  View logs:  docker compose logs -f node
  Stop:       docker compose down
=================================================
EOF
else
    echo ""
    echo "=== Starting remaining services ==="
    docker compose up -d --no-build --pull never pool-db pool collector dashboard

    cat <<'EOF'

=================================================
  BlockDAG node sync complete and stack services are starting.
=================================================
  Dashboard:  docker compose up -d pool-db pool dashboard
  Collector:  docker compose up -d collector
  Stratum:    docker compose up -d pool-db pool
  EVM RPC:    http://localhost:18545

  View logs:  docker compose logs -f node
  Stop:       docker compose down
=================================================
EOF
fi

if [[ "$OS_NAME" == "macos" ]]; then
    open -a Terminal "$PACKAGE_ROOT" 2>/dev/null || true
elif [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]]; then
    for term in gnome-terminal konsole xfce4-terminal mate-terminal lxterminal xterm; do
        if command -v "$term" >/dev/null 2>&1; then
            case "$term" in
                gnome-terminal) gnome-terminal --working-directory="$PACKAGE_ROOT" & ;;
                konsole) konsole --workdir "$PACKAGE_ROOT" & ;;
                xterm) xterm -e "cd '$PACKAGE_ROOT' && exec bash" & ;;
                *) "$term" --working-directory="$PACKAGE_ROOT" & ;;
            esac
            break
        fi
    done
fi
