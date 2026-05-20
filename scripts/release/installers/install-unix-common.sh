#!/usr/bin/env bash
set -euo pipefail

INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_ROOT="$(cd "$INSTALLER_DIR/.." && pwd)"
cd "$PACKAGE_ROOT"

OS_NAME="${BDAG_INSTALL_OS:-$(uname -s | tr '[:upper:]' '[:lower:]')}"
ARCH_NAME="${BDAG_INSTALL_ARCH:-$(uname -m)}"
DOCKER_PLATFORM="linux/amd64"

echo "=== BlockDAG Pool Stack Installer (${OS_NAME}/${ARCH_NAME}) ==="
echo ""

if [[ "$ARCH_NAME" == "arm64" ]]; then
    echo "This release contains linux/amd64 service binaries."
    echo "Docker will run the stack with platform ${DOCKER_PLATFORM}; amd64 emulation must be enabled."
    echo ""
fi

require_command() {
    local name="$1"
    local hint="$2"
    if ! command -v "$name" >/dev/null 2>&1; then
        echo "Error: $name is required. $hint" >&2
        exit 1
    fi
}

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

SNAPSHOT_PATH="docker/no-snapshot.marker"
SNAPSHOT_FILE=""
if [[ -f latest.bdsnap ]]; then
    SNAPSHOT_FILE="latest.bdsnap"
else
    SNAPSHOT_FILE="$(find . -maxdepth 1 -type f -name '*.bdsnap' -print | head -n 1 || true)"
    if [[ -n "$SNAPSHOT_FILE" ]]; then
        SNAPSHOT_FILE="${SNAPSHOT_FILE#./}"
        mv -f "$SNAPSHOT_FILE" latest.bdsnap
        SNAPSHOT_FILE="latest.bdsnap"
    fi
fi

if [[ -n "$SNAPSHOT_FILE" ]]; then
    echo "Found snapshot: $SNAPSHOT_FILE"
    SNAPSHOT_PATH="./latest.bdsnap"
else
    echo "No local snapshot found. Downloading latest.bdsnap from snapshot.bdagdev.xyz."
    if curl -L --progress-bar -o latest.bdsnap "https://snapshot.bdagdev.xyz/latest.bdsnap"; then
        SNAPSHOT_PATH="./latest.bdsnap"
        echo "Snapshot downloaded."
    else
        echo "Warning: snapshot download failed. The node will sync from genesis/P2P."
        rm -f latest.bdsnap
    fi
fi

echo ""
echo "=== Configuration ==="
echo ""

while true; do
    read -rsp "Postgres password (required, hidden): " POSTGRES_PASSWORD
    echo ""
    [[ -n "$POSTGRES_PASSWORD" ]] && break
    echo "  Password cannot be empty. Try again."
done

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

export DOCKER_DEFAULT_PLATFORM="$DOCKER_PLATFORM"

echo ""
echo "=== Building Docker images (${DOCKER_PLATFORM}) ==="
echo ""
docker compose build

echo ""
echo "=== Starting services ==="
docker compose up -d

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
