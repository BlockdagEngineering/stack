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
BDAG_INSTALL_MIN_FREE_KB="${BDAG_INSTALL_MIN_FREE_KB:-10485760}"
BDAG_INSTALL_CHECK_PORTS="${BDAG_INSTALL_CHECK_PORTS:-3334 8088 18545 18546 38131}"
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

package_path() {
    local raw="$1"
    raw="${raw:-./data/node}"
    case "$raw" in
        /*) printf '%s\n' "$raw" ;;
        ./*) printf '%s/%s\n' "$PACKAGE_ROOT" "${raw#./}" ;;
        *) printf '%s/%s\n' "$PACKAGE_ROOT" "$raw" ;;
    esac
}

detect_lan_ip() {
    local detected
    if [[ -n "${BDAG_POOL_HOST:-}" ]]; then
        printf '%s\n' "$BDAG_POOL_HOST"
        return 0
    fi
    if command -v ip >/dev/null 2>&1 && [[ -n "${BDAG_ASIC_LAN_INTERFACE:-}" ]]; then
        detected="$(ip -o -4 addr show dev "$BDAG_ASIC_LAN_INTERFACE" scope global 2>/dev/null \
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

validate_pool_lan_config() {
    local pool_host pool_url pool_url_host scan_target asic_cidrs allow_bridge
    pool_host="$(env_file_value .env BDAG_POOL_HOST)"
    pool_url="$(env_file_value .env BDAG_POOL_URL)"
    scan_target="$(env_file_value .env BDAG_MINER_SCAN_TARGET)"
    asic_cidrs="$(env_file_value .env BDAG_ASIC_LAN_CIDRS)"
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
}

prompt_with_default() {
    local prompt="$1" default_value="$2" value
    read -rp "$prompt [$default_value]: " value
    printf '%s\n' "${value:-$default_value}"
}

if [[ "${BDAG_INSTALL_TEST_WRITE_ENV_ONLY:-0}" == "1" ]]; then
    cp .env.example .env
    set_env_value .env DOCKER_PLATFORM "$DOCKER_PLATFORM"
    exit 0
fi

require_command docker "Install Docker Desktop or Docker Engine, then re-run this installer."
docker compose version >/dev/null 2>&1 || {
    echo "Error: Docker Compose v2 is required. Install/update Docker Desktop or the docker compose plugin." >&2
    exit 1
}
require_command curl "Install curl before running the installer."

if [[ ! -f .env.example || ! -f node.conf.example || ! -f docker-compose.yml ]]; then
    echo "Error: run this installer from the extracted pool-stack-docker release folder." >&2
    exit 1
fi

run_release_preflight
enforce_wired_route_policy

echo ""
echo "=== Configuration ==="
echo ""

if [[ -n "${POSTGRES_PASSWORD:-}" ]]; then
    echo "Using POSTGRES_PASSWORD from environment."
else
    POSTGRES_PASSWORD="$(generate_postgres_password)"
    echo "Generated Postgres password."
fi

read -rp "Mining/earnings wallet address (0x...): " MINING_ADDR
read -rsp "Pool operator private key (optional, hidden; press Enter to skip): " POOL_PRIVATE_KEY
echo ""

cp .env.example .env
DETECTED_POOL_LAN_IP="$(detect_lan_ip || true)"
POOL_LAN_IP="$(prompt_with_default "Pool LAN IP miners should connect to" "${BDAG_POOL_HOST:-${DETECTED_POOL_LAN_IP:-192.168.1.10}}")"
MINER_SCAN_TARGET="$(prompt_with_default "LAN scan range for ASIC discovery" "${BDAG_MINER_SCAN_TARGET:-${BDAG_ASIC_LAN_CIDRS:-$(default_cidr "$POOL_LAN_IP")}}")"
set_env_value .env POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
set_env_value .env MINING_POOL_ADDRESS "$MINING_ADDR"
set_env_value .env DOCKER_PLATFORM "$DOCKER_PLATFORM"
set_env_value .env BDAG_POOL_HOST "$POOL_LAN_IP"
set_env_value .env BDAG_POOL_URL "stratum+tcp://$POOL_LAN_IP:3334"
set_env_value .env BDAG_MINER_SCAN_TARGET "$MINER_SCAN_TARGET"
set_env_value .env BDAG_ASIC_LAN_CIDRS "$MINER_SCAN_TARGET"
validate_pool_lan_config
if [[ -n "$POOL_PRIVATE_KEY" ]]; then
    set_env_value .env POOL_PRIVATE_KEY "$POOL_PRIVATE_KEY"
fi

cp node.conf.example node.conf
if grep -q '^miningaddr=' node.conf; then
    inplace_sed "s|^miningaddr=.*|miningaddr=$(sed_escape "$MINING_ADDR")|" node.conf
else
    printf '\nminingaddr=%s\n' "$MINING_ADDR" >> node.conf
fi

echo ""
echo "Detecting external IP address..."
EXTERNAL_IP="$(curl -sf --max-time 5 https://api.ipify.org \
    || curl -sf --max-time 5 https://ifconfig.me \
    || curl -sf --max-time 5 https://icanhazip.com \
    || true)"
if [[ -n "$EXTERNAL_IP" ]]; then
    echo "  Detected: $EXTERNAL_IP"
    if grep -q '^# externalip=' node.conf; then
        inplace_sed "s|^# externalip=.*|externalip=$(sed_escape "$EXTERNAL_IP")|" node.conf
    elif grep -q '^externalip=' node.conf; then
        inplace_sed "s|^externalip=.*|externalip=$(sed_escape "$EXTERNAL_IP")|" node.conf
    else
        printf '\nexternalip=%s\n' "$EXTERNAL_IP" >> node.conf
    fi
else
    echo "  Warning: could not detect external IP. Node will operate outbound-only."
fi

mkdir -p dashboard/logs

clean_build_context_metadata
plan_orphan_container_cleanup

export DOCKER_DEFAULT_PLATFORM="$DOCKER_PLATFORM"

echo ""
echo "=== Building Docker images (${DOCKER_PLATFORM}) ==="
echo ""
if [[ -x ./scripts/bdag-low-io-build.sh ]]; then
    ./scripts/bdag-low-io-build.sh docker compose build
elif command -v ionice >/dev/null 2>&1; then
    ionice -c 3 nice -n 19 docker compose build
else
    nice -n 19 docker compose build
fi

echo ""
echo "=== Starting sync services ==="
python3 ops/automation_control.py ensure-normal \
    --owner release-installer \
    --owner-unit install-unix-common \
    --reason "Provision default automation control before sync-only first start" >/dev/null
docker compose up -d --no-build --pull never postgres node dashboard

cat <<'EOF'

=================================================
  BlockDAG Pool Stack sync services are running.
=================================================
  Dashboard:  http://localhost:8088
  Stratum:    starts after chain safety gates pass
  EVM RPC:    http://localhost:18545

  View logs:  docker compose logs -f
  Stop:       docker compose down
=================================================
EOF

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
