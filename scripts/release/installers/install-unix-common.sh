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
SNAPSHOT_URL="${BDAG_SNAPSHOT_URL:-https://bdagstack.bdagdev.xyz/latest.bdsnap}"
SNAPSHOT_MIN_BYTES="${BDAG_SNAPSHOT_MIN_BYTES:-1048576}"
BDAG_REQUIRE_SNAPSHOT="${BDAG_REQUIRE_SNAPSHOT:-1}"
BDAG_RESET_NODE_DATA="${BDAG_RESET_NODE_DATA:-0}"
BDAG_SNAPSHOT_DOWNLOADER="${BDAG_SNAPSHOT_DOWNLOADER:-curl}"
BDAG_ARIA2_CONNECTIONS="${BDAG_ARIA2_CONNECTIONS:-8}"
BDAG_INSTALL_ARIA2="${BDAG_INSTALL_ARIA2:-0}"
BDAG_BROWSER_SNAPSHOT_FALLBACK="${BDAG_BROWSER_SNAPSHOT_FALLBACK:-0}"
BDAG_INSTALL_MIN_FREE_KB="${BDAG_INSTALL_MIN_FREE_KB:-10485760}"
BDAG_INSTALL_CHECK_PORTS="${BDAG_INSTALL_CHECK_PORTS:-3334 9280 18545 18546 38131}"
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

file_size_bytes() {
    if [[ "$OS_NAME" == "macos" ]]; then
        stat -f%z "$1"
    else
        stat -c%s "$1"
    fi
}

is_valid_snapshot() {
    local file="$1"
    local size
    [[ -f "$file" ]] || return 1
    size="$(file_size_bytes "$file" 2>/dev/null || echo 0)"
    [[ "$size" -ge "$SNAPSHOT_MIN_BYTES" ]]
}

html_escape() {
    printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g; s/"/\&quot;/g'
}

generate_postgres_password() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 32 | tr -d '\n'
        return 0
    fi

    od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
}

ensure_aria2c() {
    if command -v aria2c >/dev/null 2>&1; then
        return 0
    fi

    if [[ "$BDAG_INSTALL_ARIA2" != "1" ]]; then
        echo "Error: aria2c is required for snapshot downloads when BDAG_SNAPSHOT_DOWNLOADER=aria2c." >&2
        echo "Install it with: brew install aria2" >&2
        echo "Or use curl instead: BDAG_SNAPSHOT_DOWNLOADER=curl ./install.sh" >&2
        return 1
    fi

    if [[ "$OS_NAME" != "macos" ]]; then
        echo "Error: aria2c is required, and automatic aria2 installation is only enabled for macOS." >&2
        return 1
    fi

    if ! command -v brew >/dev/null 2>&1; then
        echo "Error: aria2c is missing and Homebrew is not installed." >&2
        echo "Install Homebrew from https://brew.sh, then re-run this installer." >&2
        echo "Or use curl instead: BDAG_SNAPSHOT_DOWNLOADER=curl ./install.sh" >&2
        return 1
    fi

    echo "aria2c is missing. Installing aria2 with Homebrew..."
    if ! brew install aria2; then
        echo "Error: brew install aria2 failed." >&2
        echo "Or use curl instead: BDAG_SNAPSHOT_DOWNLOADER=curl ./install.sh" >&2
        return 1
    fi

    command -v aria2c >/dev/null 2>&1
}

browser_snapshot_download() {
    if [[ "$OS_NAME" != "macos" ]]; then
        echo "Error: browser snapshot download helper is only supported on macOS." >&2
        return 1
    fi

    local link_file="download-latest-bdsnap.html"
    local escaped_url escaped_dir answer
    escaped_url="$(html_escape "$SNAPSHOT_URL")"
    escaped_dir="$(html_escape "$PACKAGE_ROOT")"

    cat > "$link_file" <<EOF
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Download latest.bdsnap</title>
  </head>
  <body>
    <p><a href="${escaped_url}" download="latest.bdsnap">Download latest.bdsnap</a></p>
    <p>Save or move the completed file to:</p>
    <pre>${escaped_dir}/latest.bdsnap</pre>
  </body>
</html>
EOF

    echo ""
    echo "Opening a browser download link and Finder at this installer folder:"
    echo "  ${PACKAGE_ROOT}"
    echo ""
    echo "Browsers do not let shell scripts force the download folder."
    echo "If your browser asks where to save, choose this folder and save as latest.bdsnap."
    echo "Otherwise, move latest.bdsnap here after the browser download finishes."
    echo ""

    open "$link_file" >/dev/null 2>&1 || true
    open "$PACKAGE_ROOT" >/dev/null 2>&1 || true

    while true; do
        if is_valid_snapshot latest.bdsnap; then
            echo "Found snapshot: latest.bdsnap ($(file_size_bytes latest.bdsnap) bytes)"
            return 0
        fi

        read -rp "Press Enter after latest.bdsnap is in this folder, or type 'skip' to stop waiting: " answer
        if [[ "$answer" == "skip" ]]; then
            return 1
        fi
    done
}

download_snapshot() {
    local tmp="latest.bdsnap.part"

    echo "No local snapshot found. Downloading latest.bdsnap from ${SNAPSHOT_URL}."
    if [[ "$BDAG_SNAPSHOT_DOWNLOADER" == "aria2c" ]]; then
        if ! ensure_aria2c; then
            return 1
        fi

        echo "Using aria2c with ${BDAG_ARIA2_CONNECTIONS} connections."
        if ! aria2c \
            --allow-overwrite=true \
            --auto-file-renaming=false \
            --continue=true \
            --connect-timeout=20 \
            --dir=. \
            --file-allocation=none \
            --max-connection-per-server="$BDAG_ARIA2_CONNECTIONS" \
            --max-tries=3 \
            --min-split-size=64M \
            --out "$tmp" \
            --retry-wait=2 \
            --split="$BDAG_ARIA2_CONNECTIONS" \
            --timeout=60 \
            "$SNAPSHOT_URL"; then
            return 1
        fi
    elif [[ "$BDAG_SNAPSHOT_DOWNLOADER" == "curl" ]]; then
        rm -f "$tmp"
        if ! curl --fail --location --show-error --progress-bar --connect-timeout 20 --retry 2 --retry-delay 2 -o "$tmp" "$SNAPSHOT_URL"; then
            return 1
        fi
    elif [[ "$BDAG_SNAPSHOT_DOWNLOADER" == "browser" ]]; then
        browser_snapshot_download
        return $?
    else
        echo "Error: unsupported BDAG_SNAPSHOT_DOWNLOADER '${BDAG_SNAPSHOT_DOWNLOADER}'. Use aria2c, curl, or browser." >&2
        return 1
    fi

    if [[ -f "$tmp" ]]; then
        if is_valid_snapshot "$tmp"; then
            mv -f "$tmp" latest.bdsnap
            echo "Snapshot downloaded ($(file_size_bytes latest.bdsnap) bytes)."
            return 0
        fi

        echo "Warning: downloaded snapshot is too small to be valid ($(file_size_bytes "$tmp" 2>/dev/null || echo 0) bytes)." >&2
    fi

    if [[ "$BDAG_SNAPSHOT_DOWNLOADER" != "aria2c" ]]; then
        rm -f "$tmp"
    fi
    return 1
}

continue_without_snapshot_or_exit() {
    if [[ "$BDAG_REQUIRE_SNAPSHOT" != "0" ]]; then
        echo "Error: snapshot download/import is required, but no valid snapshot is available." >&2
        echo "Set BDAG_REQUIRE_SNAPSHOT=0 to continue without a snapshot and sync from P2P." >&2
        exit 1
    fi

    echo "Warning: BDAG_REQUIRE_SNAPSHOT=0; continuing without a snapshot. The node will sync from genesis/P2P." >&2
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

    curl --fail --location --head --silent --show-error --connect-timeout 10 "$SNAPSHOT_URL" >/dev/null \
        || warn_or_fail_preflight "could not reach snapshot seed URL ${SNAPSHOT_URL}; P2P sync may still work if BDAG_REQUIRE_SNAPSHOT=0."
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

prepare_node_volume_for_snapshot() {
    [[ "$SNAPSHOT_PATH" == "./latest.bdsnap" ]] || return 0

    local project node_volume nodeworker_volume answer
    project="$(compose_project_name || true)"
    [[ -n "$project" ]] || return 0

    node_volume="${project}_node-data"
    nodeworker_volume="${project}_nodeworker-data"

    if ! docker volume inspect "$node_volume" >/dev/null 2>&1; then
        return 0
    fi

    echo ""
    echo "Existing Docker node volume detected: ${node_volume}"
    echo "Snapshot import happens when the node image is built. If this existing volume is kept,"
    echo "Docker will continue using its current chain data instead of the newly imported snapshot."

    if [[ "$BDAG_RESET_NODE_DATA" != "0" ]]; then
        answer="yes"
    else
        answer="no"
    fi

    case "$answer" in
        y|Y|yes|YES)
            echo "Stopping existing stack and removing node data volumes..."
            docker compose down || true
            docker volume rm "$node_volume" "$nodeworker_volume" >/dev/null 2>&1 || true
            ;;
        *)
            echo "BDAG_RESET_NODE_DATA=0; keeping existing node data. The downloaded snapshot will not replace this volume."
            ;;
    esac
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
require_command curl "Install curl or place latest.bdsnap in this folder before running the installer."

if [[ ! -f .env.example || ! -f node.conf.example || ! -f docker-compose.yml ]]; then
    echo "Error: run this installer from the extracted pool-stack-docker release folder." >&2
    exit 1
fi

run_release_preflight

SNAPSHOT_PATH="docker/no-snapshot.marker"
SNAPSHOT_FILE=""
if [[ -f latest.bdsnap ]] && is_valid_snapshot latest.bdsnap; then
    SNAPSHOT_FILE="latest.bdsnap"
else
    SNAPSHOT_FILE="$(find . -maxdepth 1 -type f -name '*.bdsnap' -print | head -n 1 || true)"
    if [[ -n "$SNAPSHOT_FILE" ]]; then
        SNAPSHOT_FILE="${SNAPSHOT_FILE#./}"
        if is_valid_snapshot "$SNAPSHOT_FILE"; then
            mv -f "$SNAPSHOT_FILE" latest.bdsnap
            SNAPSHOT_FILE="latest.bdsnap"
        else
            echo "Ignoring invalid snapshot file: $SNAPSHOT_FILE ($(file_size_bytes "$SNAPSHOT_FILE" 2>/dev/null || echo 0) bytes)"
            SNAPSHOT_FILE=""
        fi
    fi
fi

if [[ -n "$SNAPSHOT_FILE" ]]; then
    echo "Found snapshot: $SNAPSHOT_FILE ($(file_size_bytes "$SNAPSHOT_FILE") bytes)"
    SNAPSHOT_PATH="./latest.bdsnap"
else
    if download_snapshot; then
        SNAPSHOT_PATH="./latest.bdsnap"
    elif [[ "$BDAG_BROWSER_SNAPSHOT_FALLBACK" == "1" ]] && browser_snapshot_download; then
        SNAPSHOT_PATH="./latest.bdsnap"
    else
        rm -f latest.bdsnap
        echo "Warning: snapshot download failed. The node will sync from genesis/P2P."
        continue_without_snapshot_or_exit
    fi
fi

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
set_env_value .env POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
set_env_value .env MINING_POOL_ADDRESS "$MINING_ADDR"
set_env_value .env DOCKER_PLATFORM "$DOCKER_PLATFORM"
set_env_value .env SNAPSHOT_PATH "$SNAPSHOT_PATH"
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
prepare_node_volume_for_snapshot
plan_orphan_container_cleanup

export DOCKER_DEFAULT_PLATFORM="$DOCKER_PLATFORM"

echo ""
echo "=== Building Docker images (${DOCKER_PLATFORM}) ==="
echo ""
docker compose build

echo ""
echo "=== Starting services ==="
docker compose up -d --no-build --pull never

cat <<'EOF'

=================================================
  BlockDAG Pool Stack is running.
=================================================
  Dashboard:  http://localhost:9280
  Stratum:    stratum+tcp://localhost:3334
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
