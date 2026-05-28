#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SOURCE_ROOT="${BDAG_SOURCE_ROOT:-/home/jeremy/blockdag-source}"
RELEASE_ROOT="${BDAG_RELEASE_ROOT:-/home/jeremy/blockdag-releases}"
STAMP="${BDAG_RELEASE_STAMP:-$(date +%Y%m%d-%H%M%S)}"
RELEASE_NAME="${BDAG_RELEASE_NAME:-blockdag-pool-pi5-arm64-release-$STAMP}"
RELEASE_DIR="$RELEASE_ROOT/$RELEASE_NAME"
UNPACKED_DIR="$RELEASE_DIR/unpacked"
PACKAGE_DIR="$UNPACKED_DIR/$RELEASE_NAME"
ARCHIVES_DIR="$RELEASE_DIR/archives"
HELPERS_DIR="$RELEASE_DIR/helpers"
SHARE_DIR="$RELEASE_DIR/share-to-user"
BUILD_ROOT="$RELEASE_ROOT/.build/$RELEASE_NAME"
PART_SIZE="${BDAG_RELEASE_PART_SIZE:-1800M}"
CHAIN_SOURCE="${BDAG_RELEASE_CHAIN_SOURCE:-$PROJECT_ROOT/data-restore/latest-hourly}"

POOL_REPO="${BDAG_POOL_REPO:-$SOURCE_ROOT/pool}"
NODE_REPO="${BDAG_NODE_REPO:-$SOURCE_ROOT/blockdag-corechain}"
POOL_COMMIT="${BDAG_POOL_COMMIT:-56fe111b46c0f5fe4e8f007078fcd69c1a53d588}"
NODE_COMMIT="${BDAG_NODE_COMMIT:-c74f88b9c1b4fbf4213e15272d3bf1f63943e839}"

say() { printf '\n==> %s\n' "$*"; }
warn() { printf '\nWARNING: %s\n' "$*" >&2; }

need_tool() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Required tool missing: $1" >&2
    exit 1
  }
}

base_package() {
  local latest
  latest="$(readlink -f "$RELEASE_ROOT/latest-blockdag-pool" 2>/dev/null || true)"
  if [[ -n "$latest" && -d "$latest/unpacked" ]]; then
    find "$latest/unpacked" -mindepth 1 -maxdepth 1 -type d | head -n1
    return 0
  fi
  echo "$PROJECT_ROOT"
}

extract_peer_value() {
  local key="$1" value
  local peer_env_file="${BDAG_PEER_ENV_FILE:-$PROJECT_ROOT/asic-pool/.env}"
  if [[ ! -f "$peer_env_file" ]]; then
    peer_env_file="$PROJECT_ROOT/.env.example"
  fi
  value="$(
    python3 "$PROJECT_ROOT/ops/multinode_peer_sets.py" --nodes 2 --env-file "$peer_env_file" --print-values 2>/dev/null |
      awk -v k="$key" 'index($0, k "=") == 1 {sub(k "=", ""); print; exit}'
  )"
  if [[ -z "$value" ]]; then
    warn "Could not generate $key from $peer_env_file; installer will rely on BOOTSTRAP_PEER_ADDRESSES/BDAG_FASTSYNC_PEERS"
    printf '\n'
    return 0
  fi
  printf '%s\n' "$value"
}

write_release_compose() {
  cat > "$PACKAGE_DIR/docker-compose.yml" <<'EOF'
# BDAG_GENERATED_PI5_RUNTIME_COMPOSE=1
# Generated Pi5 runtime compose. Do not replace with the source/dev compose file.
x-mining-logging: &mining-logging
  driver: local
  options:
    max-size: "10m"
    max-file: "2"

services:
  asic-pool:
    image: ${POOL_IMAGE:-bdag-release/asic-pool:local}
    container_name: asic-pool
    restart: unless-stopped
    logging: *mining-logging
    tmpfs:
      - /tmp:size=${BDAG_CONTAINER_TMPFS_SIZE:-128m},mode=1777
    cpu_shares: 3072
    blkio_config:
      weight: 900
    oom_score_adj: -800
    ulimits:
      nofile:
        soft: 1048576
        hard: 1048576
    env_file:
      - ./asic-pool/.env
    working_dir: /data/asic-pool
    volumes:
      - ./asic-pool:/data/asic-pool:ro
    environment:
      POOL_PORT: ${POOL_PORT:-3334}
      NODE_RPC_URL: http://rpc-failover:38131
      NODE_RPC_URLS: ${NODE_RPC_URLS:-http://rpc-failover:38131}
      POOL_SUBMIT_RPC_URLS: ${POOL_SUBMIT_RPC_URLS:-}
      POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT: ${POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT:-true}
      POOL_SUBMIT_STALE_BLOCK_CANDIDATES: ${POOL_SUBMIT_STALE_BLOCK_CANDIDATES:-false}
      POOL_MAX_BLOCK_CANDIDATE_JOB_AGE_MS: ${POOL_MAX_BLOCK_CANDIDATE_JOB_AGE_MS:-800}
      POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED: ${POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED:-true}
      POOL_STALE_RACE_REJECT_WINDOW_SECONDS: ${POOL_STALE_RACE_REJECT_WINDOW_SECONDS:-10}
      POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD: ${POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD:-1}
      POOL_STALE_RACE_RECOVERY_COOLDOWN_SECONDS: ${POOL_STALE_RACE_RECOVERY_COOLDOWN_SECONDS:-1}
      POOL_ALLOW_MULTIPLE_BLOCK_CANDIDATES_PER_JOB: ${POOL_ALLOW_MULTIPLE_BLOCK_CANDIDATES_PER_JOB:-true}
      POOL_PREEMPTIVE_BLOCK_CANDIDATE_REFRESH_ENABLED: ${POOL_PREEMPTIVE_BLOCK_CANDIDATE_REFRESH_ENABLED:-true}
      NODE_RPC_USER: ${NODE_RPC_USER:-test}
      NODE_RPC_PASS: ${NODE_RPC_PASS:-test}
      WALLET_RPC_URL: ${WALLET_RPC_URL:-http://bdag-miner-node-1:18545}
      WALLET_RPC_URLS: ${WALLET_RPC_URLS:-http://bdag-miner-node-1:18545}
      PPLNS_N_WORK: ${PPLNS_N_WORK:-1000}
      POOL_BLOCK_MATURITY: ${POOL_BLOCK_MATURITY:-10}
      POOL_PAYOUT_MATURITY: ${POOL_PAYOUT_MATURITY:-9999999999}
      POOL_FEE_PERCENTAGE: ${POOL_FEE_PERCENTAGE:-0.0}
      PG_URL: ${PG_URL:-postgres://test:test@pool-db:5432/pool}
      POOL_PRIVATE_KEY: ${POOL_PRIVATE_KEY:-}
      METRICS_ADDR: ${METRICS_ADDR:-0.0.0.0:9090}
    ports:
      - "${POOL_PORT:-3334}:3334"
      - "127.0.0.1:${POOL_METRICS_PORT:-9092}:9090"
    networks:
      - pool-net
    depends_on:
      pool-db:
        condition: service_started
      rpc-failover:
        condition: service_started

  rpc-failover:
    image: haproxy:2.9-alpine
    container_name: rpc-failover
    restart: unless-stopped
    logging: *mining-logging
    tmpfs:
      - /tmp:size=${BDAG_CONTAINER_TMPFS_SIZE:-128m},mode=1777
    cpu_shares: 2048
    blkio_config:
      weight: 800
    oom_score_adj: -700
    ulimits:
      nofile:
        soft: 262144
        hard: 262144
    networks:
      - pool-net
    ports:
      - "38131:38131"
    volumes:
      - ./haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro
    depends_on:
      bdag-miner-node-1:
        condition: service_started

  bdag-miner-node-1:
    image: ${BLOCKDAG_NODE_IMAGE:-bdag-release/node:local}
    container_name: bdag-miner-node-1
    restart: unless-stopped
    logging: *mining-logging
    cpu_shares: 4096
    blkio_config:
      weight: 1000
    oom_score_adj: -900
    ulimits:
      nofile:
        soft: 1048576
        hard: 1048576
    volumes:
      - ${BDAG_NODE1_DATA_DIR:-./data/node1}:/data
      - ${BDAG_RAWDATADIR_ARTIFACT_BASE:-./data-restore/rawdatadir}:/fastartifact/rawdatadir:ro
    environment:
      ROLLOUT_WINDOW: 30m
      HEALTH_MIN_PEERS: 1
      RPC_URL: "ws://127.0.0.1:18546"
      BOOTSTRAP_PEER_ADDRESSES: ${BOOTSTRAP_PEER_ADDRESSES:-}
      BDAG_FASTSNAP_ENABLED: ${BDAG_FASTSNAP_ENABLED:-1}
      BDAG_FASTSNAP_REQUIRED: ${BDAG_FASTSNAP_REQUIRED:-0}
      BDAG_FASTSNAP_PEERS: ${BDAG_FASTSNAP_PEERS:-}
      BDAG_FASTSNAP_MIN_TIP: ${BDAG_FASTSNAP_MIN_TIP:-0}
      BDAG_FASTSNAP_TIMEOUT: ${BDAG_FASTSNAP_TIMEOUT:-90s}
      BDAG_FASTSNAP_ARTIFACT_V2: ${BDAG_FASTSNAP_ARTIFACT_V2:-1}
      BDAG_FASTSNAP_DIRECTORY_MODE: ${BDAG_FASTSNAP_DIRECTORY_MODE:-1}
      BDAG_FASTSNAP_DIRECTORY_STAGING: ${BDAG_FASTSNAP_DIRECTORY_STAGING:-}
      BDAG_FASTSNAP_DIRECTORY_REPLACE_EXISTING: ${BDAG_FASTSNAP_DIRECTORY_REPLACE_EXISTING:-1}
      BDAG_FASTSNAP_DIRECTORY_MOVE_STAGING: ${BDAG_FASTSNAP_DIRECTORY_MOVE_STAGING:-1}
      BDAG_FASTSNAP_ALLOW_UNSIGNED: ${BDAG_FASTSNAP_ALLOW_UNSIGNED:-0}
      BDAG_FASTSNAP_PARALLELISM: ${BDAG_FASTSNAP_PARALLELISM:-4}
      BDAG_FASTSNAP_LEDGER: ${BDAG_FASTSNAP_LEDGER:-}
      BDAG_FASTSYNC_ARTIFACT_DIRECTORY: ${BDAG_FASTSYNC_ARTIFACT_DIRECTORY:-}
      BDAG_FASTSYNC_ARTIFACT_MANIFEST: ${BDAG_FASTSYNC_ARTIFACT_MANIFEST:-}
      BDAG_NETWORK_TOPOLOGY: ${BDAG_NETWORK_TOPOLOGY:-auto}
      BDAG_DETECTED_NETWORK_TOPOLOGY: ${BDAG_DETECTED_NETWORK_TOPOLOGY:-}
      BDAG_ASIC_LAN_INTERFACE: ${BDAG_ASIC_LAN_INTERFACE:-eth0}
      BDAG_ASIC_LAN_CIDRS: ${BDAG_ASIC_LAN_CIDRS:-192.168.50.0/24}
      BDAG_ALLOW_ASIC_LAN_P2P: ${BDAG_ALLOW_ASIC_LAN_P2P:-0}
      BDAG_P2P_ADVERTISE_IP: ${BDAG_P2P_ADVERTISE_IP:-}
      BDAG_P2P_INTERFACE: ${BDAG_P2P_INTERFACE:-}
      BDAG_P2P_LAN_PEERS: ${BDAG_P2P_LAN_PEERS:-}
      BDAG_P2P_VPN_PEERS: ${BDAG_P2P_VPN_PEERS:-}
      BDAG_P2P_PUBLIC_PEERS: ${BDAG_P2P_PUBLIC_PEERS:-}
      LAN_PEER_ADDRESSES: ${LAN_PEER_ADDRESSES:-}
      VPN_PEER_ADDRESSES: ${VPN_PEER_ADDRESSES:-}
      ZEROTIER_PEER_ADDRESSES: ${ZEROTIER_PEER_ADDRESSES:-}
      BDAG_FASTSYNC_PEER_ORDERING: ${BDAG_FASTSYNC_PEER_ORDERING:-tiered-latency}
      BDAG_FASTSYNC_APPEND_ADDPEERS: ${BDAG_FASTSYNC_APPEND_ADDPEERS:-1}
      BDAG_FASTARTIFACTSYNC_ENABLED: ${BDAG_FASTARTIFACTSYNC_ENABLED:-1}
      BDAG_FASTSYNC_LAN_PREFIXES: ${BDAG_FASTSYNC_LAN_PREFIXES:-}
      BDAG_FASTSYNC_LAN_PEERS: ${BDAG_FASTSYNC_LAN_PEERS:-}
      BDAG_FASTSYNC_VPN_PEERS: ${BDAG_FASTSYNC_VPN_PEERS:-}
      BDAG_FASTSYNC_PUBLIC_PEERS: ${BDAG_FASTSYNC_PUBLIC_PEERS:-}
      BDAG_FASTSYNC_PEERS: ${BDAG_FASTSYNC_PEERS:-}
      BDAG_FASTSYNC_PREPROCESS_WORKERS: ${BDAG_FASTSYNC_PREPROCESS_WORKERS:-1}
      BDAG_ENTRYPOINT_CHOWN_MODE: ${BDAG_ENTRYPOINT_CHOWN_MODE:-needed}
      NODE_ARGS: >
        --p2ptcpport=8151
        --listen=0.0.0.0
        --addpeer=${NODE1_PEER_ADDRESSES}
        --rpclisten=0.0.0.0:38131
        --evm.http.port=18545
        --evm.http.addr=0.0.0.0
        --datadir=/data
        --cache=${BDAG_NODE_CACHE_MB:-4096}
        --cache.database=${BDAG_NODE_CACHE_DATABASE_PERCENT:-50}
        --cache.snapshot=${BDAG_NODE_CACHE_SNAPSHOT_PERCENT:-35}
        --bdcachesize=${BDAG_NODE_BD_CACHE_SIZE:-8192}
        --dagcachesize=${BDAG_NODE_DAG_CACHE_SIZE:-8192}
        --debuglevel=${BDAG_NODE_DEBUG_LEVEL:-warn}
        --evmtrietimeout=${BDAG_EVM_TRIE_TIMEOUT_SECONDS:-7200}
        --nofilelogging
        --notls
        --rpcuser=${NODE_RPC_USER:-test}
        --rpcpass=${NODE_RPC_PASS:-test}
        --rpcmaxclients=100
        --evm.http.api=eth,net,web3,txpool,debug
        --http.writetimeout=45s
        --http.idletimeout=45s
        --acctmode
        --modules=${BDAG_NODE_MODULES:-Blockdag}
        --evmenv="--cache=${BDAG_EVM_CACHE_MB:-8192} --cache.database=${BDAG_EVM_CACHE_DATABASE_PERCENT:-80} --cache.snapshot=${BDAG_EVM_CACHE_SNAPSHOT_PERCENT:-1} --rpc.allow-unprotected-txs --metrics --metrics.addr 0.0.0.0 --metrics.port 6060"
        --metrics
        --rpcmaxconcurrentreqs=500
        --maxpeers=${NODE_MAX_PEERS:-160}
        --http.ratelimit=1800
        --http.rateburst=3200
        --evm.ws.port=18546
        --evm.ws.addr=0.0.0.0
        ${BDAG_NODE_MINING_ARGS:-}
    networks:
      - pool-net
    ports:
      - "8151:8151"
      - "6061:6060"

  bdag-miner-node-2:
    profiles:
      - dual-node
    image: ${BLOCKDAG_NODE_IMAGE:-bdag-release/node:local}
    container_name: bdag-miner-node-2
    restart: unless-stopped
    logging: *mining-logging
    cpu_shares: 4096
    blkio_config:
      weight: 1000
    oom_score_adj: -900
    ulimits:
      nofile:
        soft: 1048576
        hard: 1048576
    volumes:
      - ${BDAG_NODE2_DATA_DIR:-./data/node2}:/data
      - ${BDAG_RAWDATADIR_ARTIFACT_BASE:-./data-restore/rawdatadir}:/fastartifact/rawdatadir:ro
    environment:
      ROLLOUT_WINDOW: 30m
      HEALTH_MIN_PEERS: 1
      RPC_URL: "ws://127.0.0.1:18546"
      BOOTSTRAP_PEER_ADDRESSES: ${BOOTSTRAP_PEER_ADDRESSES:-}
      BDAG_FASTSNAP_ENABLED: ${BDAG_FASTSNAP_ENABLED:-1}
      BDAG_FASTSNAP_REQUIRED: ${BDAG_FASTSNAP_REQUIRED:-0}
      BDAG_FASTSNAP_PEERS: ${BDAG_FASTSNAP_PEERS:-}
      BDAG_FASTSNAP_MIN_TIP: ${BDAG_FASTSNAP_MIN_TIP:-0}
      BDAG_FASTSNAP_TIMEOUT: ${BDAG_FASTSNAP_TIMEOUT:-90s}
      BDAG_FASTSNAP_ARTIFACT_V2: ${BDAG_FASTSNAP_ARTIFACT_V2:-1}
      BDAG_FASTSNAP_DIRECTORY_MODE: ${BDAG_FASTSNAP_DIRECTORY_MODE:-1}
      BDAG_FASTSNAP_DIRECTORY_STAGING: ${BDAG_FASTSNAP_DIRECTORY_STAGING:-}
      BDAG_FASTSNAP_DIRECTORY_REPLACE_EXISTING: ${BDAG_FASTSNAP_DIRECTORY_REPLACE_EXISTING:-1}
      BDAG_FASTSNAP_DIRECTORY_MOVE_STAGING: ${BDAG_FASTSNAP_DIRECTORY_MOVE_STAGING:-1}
      BDAG_FASTSNAP_ALLOW_UNSIGNED: ${BDAG_FASTSNAP_ALLOW_UNSIGNED:-0}
      BDAG_FASTSNAP_PARALLELISM: ${BDAG_FASTSNAP_PARALLELISM:-4}
      BDAG_FASTSNAP_LEDGER: ${BDAG_FASTSNAP_LEDGER:-}
      BDAG_FASTSYNC_ARTIFACT_DIRECTORY: ${BDAG_FASTSYNC_ARTIFACT_DIRECTORY:-}
      BDAG_FASTSYNC_ARTIFACT_MANIFEST: ${BDAG_FASTSYNC_ARTIFACT_MANIFEST:-}
      BDAG_NETWORK_TOPOLOGY: ${BDAG_NETWORK_TOPOLOGY:-auto}
      BDAG_DETECTED_NETWORK_TOPOLOGY: ${BDAG_DETECTED_NETWORK_TOPOLOGY:-}
      BDAG_ASIC_LAN_INTERFACE: ${BDAG_ASIC_LAN_INTERFACE:-eth0}
      BDAG_ASIC_LAN_CIDRS: ${BDAG_ASIC_LAN_CIDRS:-192.168.50.0/24}
      BDAG_ALLOW_ASIC_LAN_P2P: ${BDAG_ALLOW_ASIC_LAN_P2P:-0}
      BDAG_P2P_ADVERTISE_IP: ${BDAG_P2P_ADVERTISE_IP:-}
      BDAG_P2P_INTERFACE: ${BDAG_P2P_INTERFACE:-}
      BDAG_P2P_LAN_PEERS: ${BDAG_P2P_LAN_PEERS:-}
      BDAG_P2P_VPN_PEERS: ${BDAG_P2P_VPN_PEERS:-}
      BDAG_P2P_PUBLIC_PEERS: ${BDAG_P2P_PUBLIC_PEERS:-}
      LAN_PEER_ADDRESSES: ${LAN_PEER_ADDRESSES:-}
      VPN_PEER_ADDRESSES: ${VPN_PEER_ADDRESSES:-}
      ZEROTIER_PEER_ADDRESSES: ${ZEROTIER_PEER_ADDRESSES:-}
      BDAG_FASTSYNC_PEER_ORDERING: ${BDAG_FASTSYNC_PEER_ORDERING:-tiered-latency}
      BDAG_FASTSYNC_APPEND_ADDPEERS: ${BDAG_FASTSYNC_APPEND_ADDPEERS:-1}
      BDAG_FASTARTIFACTSYNC_ENABLED: ${BDAG_FASTARTIFACTSYNC_ENABLED:-1}
      BDAG_FASTSYNC_LAN_PREFIXES: ${BDAG_FASTSYNC_LAN_PREFIXES:-}
      BDAG_FASTSYNC_LAN_PEERS: ${BDAG_FASTSYNC_LAN_PEERS:-}
      BDAG_FASTSYNC_VPN_PEERS: ${BDAG_FASTSYNC_VPN_PEERS:-}
      BDAG_FASTSYNC_PUBLIC_PEERS: ${BDAG_FASTSYNC_PUBLIC_PEERS:-}
      BDAG_FASTSYNC_PEERS: ${BDAG_FASTSYNC_PEERS:-}
      BDAG_FASTSYNC_PREPROCESS_WORKERS: ${BDAG_FASTSYNC_PREPROCESS_WORKERS:-1}
      BDAG_ENTRYPOINT_CHOWN_MODE: ${BDAG_ENTRYPOINT_CHOWN_MODE:-needed}
      NODE_ARGS: >
        --p2ptcpport=8152
        --listen=0.0.0.0
        --addpeer=${NODE2_PEER_ADDRESSES}
        --rpclisten=0.0.0.0:38131
        --evm.http.port=18545
        --evm.http.addr=0.0.0.0
        --datadir=/data
        --cache=${BDAG_NODE_CACHE_MB:-4096}
        --cache.database=${BDAG_NODE_CACHE_DATABASE_PERCENT:-50}
        --cache.snapshot=${BDAG_NODE_CACHE_SNAPSHOT_PERCENT:-35}
        --bdcachesize=${BDAG_NODE_BD_CACHE_SIZE:-8192}
        --dagcachesize=${BDAG_NODE_DAG_CACHE_SIZE:-8192}
        --debuglevel=${BDAG_NODE_DEBUG_LEVEL:-warn}
        --evmtrietimeout=${BDAG_EVM_TRIE_TIMEOUT_SECONDS:-7200}
        --nofilelogging
        --notls
        --rpcuser=${NODE_RPC_USER:-test}
        --rpcpass=${NODE_RPC_PASS:-test}
        --rpcmaxclients=100
        --evm.http.api=eth,net,web3,txpool,debug
        --http.writetimeout=45s
        --http.idletimeout=45s
        --acctmode
        --modules=${BDAG_NODE_MODULES:-Blockdag}
        --evmenv="--cache=${BDAG_EVM_CACHE_MB:-8192} --cache.database=${BDAG_EVM_CACHE_DATABASE_PERCENT:-80} --cache.snapshot=${BDAG_EVM_CACHE_SNAPSHOT_PERCENT:-1} --rpc.allow-unprotected-txs --metrics --metrics.addr 0.0.0.0 --metrics.port 6060"
        --metrics
        --rpcmaxconcurrentreqs=500
        --maxpeers=${NODE_MAX_PEERS:-160}
        --http.ratelimit=1800
        --http.rateburst=3200
        --evm.ws.port=18546
        --evm.ws.addr=0.0.0.0
        ${BDAG_NODE_MINING_ARGS:-}
    networks:
      - pool-net
    ports:
      - "8152:8152"
      - "6062:6060"

  pool-db:
    image: postgres:15-alpine
    container_name: pool-db
    restart: unless-stopped
    logging: *mining-logging
    tmpfs:
      - /tmp:size=${BDAG_CONTAINER_TMPFS_SIZE:-128m},mode=1777
    command:
      - postgres
      - -c
      - shared_buffers=${POSTGRES_SHARED_BUFFERS:-256MB}
      - -c
      - effective_cache_size=${POSTGRES_EFFECTIVE_CACHE_SIZE:-1GB}
      - -c
      - max_wal_size=${POSTGRES_MAX_WAL_SIZE:-1GB}
      - -c
      - checkpoint_timeout=${POSTGRES_CHECKPOINT_TIMEOUT:-15min}
    cpu_shares: 3072
    blkio_config:
      weight: 900
    oom_score_adj: -800
    ulimits:
      nofile:
        soft: 262144
        hard: 262144
    env_file:
      - ./asic-pool/.env
    volumes:
      - ${BDAG_POSTGRES_DATA_DIR:-./data/postgres}:/var/lib/postgresql/data
      - ./asic-pool/schema.sql:/docker-entrypoint-initdb.d/schema.sql:ro
    networks:
      - pool-net

networks:
  pool-net:
    driver: bridge
EOF
}

guard_release_compose() {
  local compose="$PACKAGE_DIR/docker-compose.yml"
  if ! grep -q '^# BDAG_GENERATED_PI5_RUNTIME_COMPOSE=1$' "$compose"; then
    echo "Generated runtime compose marker missing from $compose" >&2
    exit 1
  fi
  if grep -Eq '^[[:space:]]*(build|dockerfile):' "$compose"; then
    echo "Generated runtime compose must not contain build/dockerfile entries: $compose" >&2
    exit 1
  fi
}

sanitize_release_tree() {
  rm -rf "$PACKAGE_DIR/ops/observability/testdata"
  rm -f "$PACKAGE_DIR/ops/observability/.env"
  rm -f "$PACKAGE_DIR/ops/observability/docs/zerotier-access.md"
  rm -f "$PACKAGE_DIR/ops/stack_ab_test.py"

  python3 - "$PACKAGE_DIR/ops/dashboard.py" "$PACKAGE_DIR/ops/pool_ops.py" <<'PY'
from pathlib import Path
import re
import sys

dashboard = Path(sys.argv[1])
pool_ops = Path(sys.argv[2])

if dashboard.exists():
    text = dashboard.read_text(encoding="utf-8")
    text = re.sub(
        r"const globalPoolNames = \{\n(?:      \"0x[a-f0-9]{40}\": \"[^\"]+\",\n)*    \};",
        "const globalPoolNames = {};",
        text,
        flags=re.IGNORECASE,
    )
    dashboard.write_text(text, encoding="utf-8")

if pool_ops.exists():
    text = pool_ops.read_text(encoding="utf-8")
    text = re.sub(
        r"DEFAULT_GLOBAL_POOL_LABELS = \{\n(?:    \"0x[a-f0-9]{40}\": \"[^\"]+\",\n)*\}",
        "DEFAULT_GLOBAL_POOL_LABELS = {}",
        text,
        flags=re.IGNORECASE,
    )
    pool_ops.write_text(text, encoding="utf-8")
PY
}

write_env_examples() {
  local node1_peers="$1" node2_peers="$2"
  cat > "$PACKAGE_DIR/.env.example" <<EOF
# BlockDAG Pi5 ARM64 pool release configuration.
# The installer copies this file to .env, asks for your LAN IP and wallet,
# then writes fresh local passwords before starting Docker Compose.

POOL_IMAGE=bdag-release/asic-pool:local
BLOCKDAG_NODE_IMAGE=bdag-release/node:local
POOL_PORT=3334
POOL_METRICS_PORT=9092
METRICS_ADDR=0.0.0.0:9090
POOL_STARTING_PDIFF=0.06

# You will be prompted for this during install. Leave this placeholder here.
MINING_ADDRESS=0x0000000000000000000000000000000000000000
BDAG_POOL_HOST=192.168.1.10
BDAG_POOL_URL=stratum+tcp://192.168.1.10:3334
BDAG_MINER_SCAN_TARGET=192.168.1.0/24
BDAG_DASHBOARD_BIND=127.0.0.1
BDAG_DASHBOARD_PORT=8088

# Storage placement. The installer resolves auto into concrete paths so large,
# growing chain data can live on capacity storage while small frequent writes
# stay on internal storage when the host has enough free space.
BDAG_STORAGE_PROFILE=auto
BDAG_CHAIN_DATA_DIR=./data
BDAG_DATA_DIR=./data
BDAG_NODE1_DATA_DIR=./data/node1
BDAG_NODE2_DATA_DIR=./data/node2
BDAG_POSTGRES_DATA_DIR=./data/postgres
BDAG_RUNTIME_DIR=./ops/runtime
BDAG_STORAGE_MIN_CHAIN_FREE_GIB=50
BDAG_STORAGE_MIN_RUNTIME_FREE_GIB=4

# Ephemeral scratch policy. Small temporary files belong on RAM-backed tmpfs so
# they do not add avoidable writes to the USB chain device. Large snapshot and
# chain-artifact staging must stay on capacity storage unless explicitly sized.
BDAG_EPHEMERAL_TMPFS_ENABLED=1
BDAG_EPHEMERAL_DIR=/run/bdag-pool
BDAG_HOST_TMPDIR=/run/bdag-pool/tmp
BDAG_CONTAINER_TMPFS_SIZE=128m

# Single-node is the safe default for Pi5 USB power and catch-up stability.
# Set BDAG_NODE_MODE=double and COMPOSE_PROFILES=dual-node to run both backend
# nodes. Nodes stay sync-only until BDAG_NODE_MINING_ARGS is explicitly set.
BDAG_NODE_MODE=single
COMPOSE_PROFILES=
BDAG_NODE_SERVICES=bdag-miner-node-1
BDAG_STACK_SERVICES=pool-db,bdag-miner-node-1,rpc-failover,asic-pool
BDAG_ENABLE_NODE_MINING=0
BDAG_NODE_MODULES=Blockdag
BDAG_NODE_MINING_ARGS=
BDAG_NODE_CHAIN_RPC_TIMEOUT=8.0
BDAG_NODE_CHAIN_RPC_RETRIES=2
BDAG_POOL_RPC_REFUSED_WARN_SECONDS=120
BDAG_HOST_PRESSURE_IOWAIT_WARN_PERCENT=25
BDAG_HOST_PRESSURE_IOWAIT_WARN_SAMPLES=3
BDAG_HOST_PRESSURE_HISTORY_SAMPLES=6
BDAG_SHARED_STATUS_CACHE_ENABLED=1
BDAG_SHARED_STATUS_CACHE_SECONDS=3.0
BDAG_HOST_PROFILE=auto
BDAG_ADAPTIVE_CONCURRENCY_ENABLED=1
BDAG_ADAPTIVE_IOWAIT_WARN_PERCENT=25
BDAG_ADAPTIVE_IO_SOME_AVG10_WARN=20.0
BDAG_ADAPTIVE_CPU_SOME_AVG10_WARN=80.0
BDAG_ADAPTIVE_CHAIN_RPC_WARN_MS=1000
BDAG_STATUS_SAMPLER_ENABLED=1
BDAG_STATUS_SAMPLER_INTERVAL_SECONDS=10
BDAG_STATUS_SAMPLER_MAX_AGE_SECONDS=12
BDAG_GLOBAL_RPC_WORKERS=24
BDAG_MINER_SCAN_WORKERS=64
BDAG_MINER_HASHRATE_PROBE_WORKERS=8
BDAG_BACKGROUND_MAINTENANCE_BACKOFF_ENABLED=1
BDAG_BACKGROUND_MAINTENANCE_SYNC_BACKOFF_BLOCKS=0
BDAG_BACKGROUND_MAINTENANCE_IOWAIT_WARN_PERCENT=25
BDAG_BACKGROUND_MAINTENANCE_IO_SOME_AVG10_WARN=20.0
BDAG_BACKGROUND_MAINTENANCE_CPU_SOME_AVG10_WARN=80.0
BDAG_BACKGROUND_MAINTENANCE_CHAIN_RPC_WARN_MS=1000
BDAG_GLOBAL_HISTORY_COMPACT_MULTIPLIER=2
BDAG_ENTRYPOINT_CHOWN_MODE=needed
BDAG_NODE_CACHE_MB=4096
BDAG_NODE_CACHE_DATABASE_PERCENT=50
BDAG_NODE_CACHE_SNAPSHOT_PERCENT=35
BDAG_NODE_BD_CACHE_SIZE=8192
BDAG_NODE_DAG_CACHE_SIZE=8192
BDAG_EVM_CACHE_MB=8192
BDAG_EVM_CACHE_DATABASE_PERCENT=80
BDAG_EVM_CACHE_SNAPSHOT_PERCENT=1
BDAG_EVM_TRIE_TIMEOUT_SECONDS=7200
BDAG_NODE_DEBUG_LEVEL=warn

NODE_RPC_USER=test
NODE_RPC_PASS=change-me-at-install
POSTGRES_USER=test
POSTGRES_PASSWORD=change-me-at-install
POSTGRES_DB=pool
PG_URL=postgres://test:change-me-at-install@pool-db:5432/pool

POOL_RPC_ROUTER_ENABLED=true
POOL_RPC_ROUTER_NODE_HEALTH_ENABLED=true
POOL_RPC_BACKENDS=node1=http://bdag-miner-node-1:38131
POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT=true
POOL_TEMPLATE_FANIN_ENABLED=false
POOL_TEMPLATE_FANIN_MAX_BACKENDS=2
POOL_TEMPLATE_FANIN_REJECT_LAG_BLOCKS=0
POOL_TEMPLATE_FANIN_ACCEPT_SAME_HEIGHT=false
POOL_TEMPLATE_FANIN_ALT_TAKEOVER_MIN_AGE_MS=0
POOL_TEMPLATE_FANIN_ALT_TAKEOVER_LEAD_BLOCKS=0
POOL_RPC_ROUTER_MIN_HEALTHY_SECONDS=2
POOL_RPC_ROUTER_SWITCH_COOLDOWN_SECONDS=10
POOL_RPC_ROUTER_TEMPLATE_MAX_AGE_SECONDS=5
POOL_RPC_ROUTER_FREEZE_FAILOVER_ENABLED=true
POOL_RPC_ROUTER_FREEZE_FAILOVER_AFTER_SECONDS=45
POOL_RPC_ROUTER_FREEZE_MAX_TEMPLATE_AGE_SECONDS=90
POOL_RPC_ROUTER_RECOVERY_PROBE_SECONDS=15
POOL_RPC_ROUTER_NODE_HEALTH_PROBE_SECONDS=1
POOL_RPC_ROUTER_NODE_HEALTH_MAX_AGE_SECONDS=3
POOL_RPC_ROUTER_NODE_HEALTH_UNREADY_THRESHOLD=1
POOL_RPC_ROUTER_NODE_HEALTH_ERROR_THRESHOLD=2
POOL_RPC_ROUTER_NODE_HEALTH_FRESH_TEMPLATE_GRACE_SECONDS=5

NODE1_PEER_ADDRESSES=$node1_peers
NODE2_PEER_ADDRESSES=$node2_peers
BDAG_NETWORK_TOPOLOGY=auto
BDAG_DETECTED_NETWORK_TOPOLOGY=
BDAG_ASIC_LAN_INTERFACE=eth0
BDAG_ASIC_LAN_CIDRS=192.168.50.0/24
BDAG_ALLOW_ASIC_LAN_P2P=0
BDAG_P2P_ADVERTISE_IP=
BDAG_P2P_INTERFACE=
BDAG_P2P_LAN_PEERS=
BDAG_P2P_VPN_PEERS=
BDAG_P2P_PUBLIC_PEERS=
LAN_PEER_ADDRESSES=
VPN_PEER_ADDRESSES=
ZEROTIER_PEER_ADDRESSES=
BDAG_FASTSNAP_ENABLED=1
BDAG_FASTSNAP_REQUIRED=0
BDAG_FASTSNAP_PEERS=
BDAG_FASTSNAP_MIN_TIP=0
BDAG_FASTSNAP_TIMEOUT=90s
BDAG_FASTSNAP_ARTIFACT_V2=1
BDAG_FASTSNAP_DIRECTORY_MODE=1
BDAG_FASTSNAP_DIRECTORY_STAGING=
BDAG_FASTSNAP_DIRECTORY_REPLACE_EXISTING=1
BDAG_FASTSNAP_DIRECTORY_MOVE_STAGING=1
BDAG_FASTSNAP_ALLOW_UNSIGNED=0
BDAG_FASTSNAP_PARALLELISM=4
BDAG_FASTSNAP_LEDGER=
BDAG_FASTSNAP_SEED_TIMER_ENABLED=0
BDAG_FASTSNAP_MAX_EXPORT_BACKEND_LAG=10000
BDAG_RAWDATADIR_SOURCE_MODE=auto
BDAG_RAWDATADIR_ARTIFACT_BASE=./data-restore/rawdatadir
BDAG_RAWDATADIR_ARTIFACT_KEEP=3
BDAG_RAWDATADIR_REQUIRE_SIGNED=1
BDAG_RAWDATADIR_MAX_EXPORT_BACKEND_LAG=10000
BDAG_RAWDATADIR_SIDECAR_SOURCE=
BDAG_RAWDATADIR_SIDECAR_DIR=
BDAG_RAWDATADIR_SINGLE_NODE_FINALIZE=0
BDAG_RAWDATADIR_MIN_FREE_GIB=100
BDAG_RAWDATADIR_FREE_SPACE_MULTIPLIER=2.5
BDAG_RAWDATADIR_REQUIRE_STATE_ROOT=1
BDAG_RAWDATADIR_SIDECAR_USE_SUDO=auto
BDAG_RAWDATADIR_ARCHIVE_USE_SUDO=auto
BDAG_FASTSYNC_ARTIFACT_DIRECTORY=
BDAG_FASTSYNC_ARTIFACT_MANIFEST=
BDAG_FASTSYNC_PEER_ORDERING=tiered-latency
BDAG_FASTSYNC_APPEND_ADDPEERS=1
BDAG_FASTARTIFACTSYNC_ENABLED=1
BDAG_FASTSYNC_LAN_PREFIXES=
BDAG_FASTSYNC_LAN_PEERS=
BDAG_FASTSYNC_VPN_PEERS=
BDAG_FASTSYNC_PUBLIC_PEERS=
BDAG_FASTSYNC_PEERS=
BDAG_FASTSYNC_PREPROCESS_WORKERS=1
BDAG_ENTRYPOINT_CHOWN_MODE=needed

NODE_RPC_URL=http://rpc-failover:38131
NODE_RPC_URLS=http://rpc-failover:38131
POOL_SUBMIT_RPC_URLS=
WALLET_RPC_URL=http://bdag-miner-node-1:18545
WALLET_RPC_URLS=http://bdag-miner-node-1:18545
PPLNS_N_WORK=1000
POOL_BLOCK_MATURITY=10
POOL_PAYOUT_MATURITY=9999999999
POOL_FEE_PERCENTAGE=0.0
POOL_PRIVATE_KEY=

POOL_SUBMIT_STALE_BLOCK_CANDIDATES=false
POOL_GLOBAL_BLOCK_DEDUPE_ENABLED=true
POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED=true
POOL_STALE_RACE_REJECT_WINDOW_SECONDS=10
POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD=1
POOL_STALE_RACE_RECOVERY_COOLDOWN_SECONDS=1
POOL_SUBMIT_STALL_RECOVERY_ENABLED=true
POOL_SUBMIT_STALL_RECOVERY_WINDOW_SECONDS=60
POOL_SUBMIT_STALL_RECOVERY_MIN_AGE_SECONDS=30
POOL_SUBMIT_STALL_RECOVERY_FAILURE_THRESHOLD=8
POOL_SUBMIT_STALL_RECOVERY_STALE_THRESHOLD=3
POOL_SUBMIT_STALL_RECOVERY_DUPLICATE_THRESHOLD=6
POOL_SUBMIT_STALL_RECOVERY_COOLDOWN_SECONDS=20
POOL_LOW_DIFF_FLOOD_DISCONNECT_ENABLED=true
POOL_LOW_DIFF_REJECT_WINDOW_SECONDS=10
POOL_LOW_DIFF_REJECT_DISCONNECT_THRESHOLD=100
POOL_IDLE_CLIENT_RECONNECT_ENABLED=true
POOL_IDLE_CLIENT_RECONNECT_SECONDS=120
POOL_REFRESH_LOG_INTERVAL_SECONDS=5
POOL_DB_SUCCESS_LOG_INTERVAL_SECONDS=60
POOL_DIFFICULTY_PUSH_LOG_INTERVAL_SECONDS=300
POOL_SKIP_UNCHANGED_DIFFICULTY_PUSHES=true
POOL_WS_UNSUPPORTED_BACKOFF_SECONDS=300
POOL_GBT_MIN_INTERVAL_MS=100
POOL_GBT_PRESSURE_INTERVAL_MS=100
POOL_GBT_PRESSURE_WINDOW_SECONDS=30
POOL_MAX_BLOCK_CANDIDATE_JOB_AGE_MS=800
POOL_ALLOW_MULTIPLE_BLOCK_CANDIDATES_PER_JOB=true
POOL_PREEMPTIVE_BLOCK_CANDIDATE_REFRESH_ENABLED=true
EOF
  mkdir -p "$PACKAGE_DIR/asic-pool"
  cp "$PACKAGE_DIR/.env.example" "$PACKAGE_DIR/asic-pool/.env.example"
}

write_image_build_files() {
  mkdir -p "$PACKAGE_DIR/src/docker"
  cat > "$PACKAGE_DIR/src/docker/pool.Dockerfile" <<'EOF'
FROM arm64v8/ubuntu:24.04

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd -r bdagStack && useradd -r -g bdagStack -d /var/lib/bdagStack -m bdagStack \
 && mkdir -p /var/lib/bdagStack/pool /var/log/bdagStack /etc/bdagStack \
 && chown -R bdagStack:bdagStack /var/lib/bdagStack /var/log/bdagStack /etc/bdagStack

COPY pool /usr/local/bin/pool
COPY dashboard-api /usr/local/bin/dashboard-api
COPY schema.sql /var/lib/bdagStack/pool/schema.sql

RUN chmod +x /usr/local/bin/pool /usr/local/bin/dashboard-api \
 && chown bdagStack:bdagStack /var/lib/bdagStack/pool/schema.sql

USER bdagStack
WORKDIR /var/lib/bdagStack/pool
EXPOSE 3334 8080 9090
ENTRYPOINT ["/usr/local/bin/pool"]
EOF

  cat > "$PACKAGE_DIR/src/docker/node.Dockerfile" <<'EOF'
FROM arm64v8/ubuntu:24.04

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY bdag /usr/local/bin/bdag
COPY nodeworker /usr/local/bin/nodeworker
COPY fastsnap /usr/local/bin/fastsnap
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/bdag /usr/local/bin/nodeworker /usr/local/bin/fastsnap /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/bin/sh", "/usr/local/bin/entrypoint.sh"]
EOF

  cat > "$PACKAGE_DIR/src/docker/entrypoint.sh" <<'EOF'
#!/bin/sh
set -e

BIN=/usr/local/bin/bdag
[ -x /opt/bdag/bdag ] && BIN=/opt/bdag/bdag

log() {
  echo "[$(date -Is)] node-entrypoint: $*" >&2
}

csv_append_unique() {
  name="$1"
  value="$2"
  [ -n "$value" ] || return 0
  case ",$(eval "printf '%s' \"\${$name:-}\"")," in
    *",$value,"*) return 0 ;;
  esac
  current="$(eval "printf '%s' \"\${$name:-}\"")"
  if [ -n "$current" ]; then
    eval "$name=\"\$current,\$value\""
  else
    eval "$name=\"\$value\""
  fi
}

peer_host() {
  case "$1" in
    /ip4/*) peer="${1#/ip4/}"; echo "${peer%%/*}" ;;
    *) return 1 ;;
  esac
}

host_matches_prefixes() {
  host="$1"
  prefixes="$2"
  old_ifs="$IFS"
  IFS=', '
  for prefix in $prefixes; do
    [ -n "$prefix" ] || continue
    case "$host" in "$prefix"*) IFS="$old_ifs"; return 0 ;; esac
  done
  IFS="$old_ifs"
  return 1
}

host_is_private_or_vpn() {
  case "$1" in
    10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*|100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*) return 0 ;;
  esac
  return 1
}

host_matches_cidr_prefix() {
  host="$1"
  cidr="$2"
  case "$cidr" in
    */*)
      base="${cidr%/*}"
      mask="${cidr#*/}"
      ;;
    *)
      case "$host" in "$cidr"*) return 0 ;; esac
      return 1
      ;;
  esac
  case "$mask" in
    8) prefix="${base%%.*}." ;;
    16) prefix="$(printf '%s\n' "$base" | awk -F. '{print $1 "." $2 "."}')" ;;
    24) prefix="$(printf '%s\n' "$base" | awk -F. '{print $1 "." $2 "." $3 "."}')" ;;
    32) [ "$host" = "$base" ] && return 0; return 1 ;;
    *) return 1 ;;
  esac
  case "$host" in "$prefix"*) return 0 ;; esac
  return 1
}

host_is_excluded_asic_lan() {
  host="$1"
  topology="${BDAG_DETECTED_NETWORK_TOPOLOGY:-${BDAG_NETWORK_TOPOLOGY:-auto}}"
  [ "${BDAG_ALLOW_ASIC_LAN_P2P:-0}" = "1" ] && return 1
  [ "$topology" = "single-node-asic-router" ] || return 1
  old_ifs="$IFS"
  IFS=', '
  for cidr in ${BDAG_ASIC_LAN_CIDRS:-192.168.50.0/24}; do
    [ -n "$cidr" ] || continue
    if host_matches_cidr_prefix "$host" "$cidr"; then
      IFS="$old_ifs"
      return 0
    fi
  done
  IFS="$old_ifs"
  return 1
}

peer_allowed_for_p2p() {
  peer="$1"
  host="$(peer_host "$peer" || true)"
  [ -n "$host" ] || return 0
  ! host_is_excluded_asic_lan "$host"
}

classify_peer_csv() {
  raw="$1"
  old_ifs="$IFS"
  IFS=', '
  for peer in $raw; do
    [ -n "$peer" ] || continue
    peer_allowed_for_p2p "$peer" || continue
    host="$(peer_host "$peer" || true)"
    if [ -n "$host" ] && [ -n "${BDAG_FASTSYNC_LAN_PREFIXES:-}" ] && host_matches_prefixes "$host" "${BDAG_FASTSYNC_LAN_PREFIXES:-}"; then
      csv_append_unique fastsync_lan_peers "$peer"
    elif [ -n "$host" ] && host_is_private_or_vpn "$host"; then
      csv_append_unique fastsync_vpn_peers "$peer"
    else
      csv_append_unique fastsync_public_peers "$peer"
    fi
  done
  IFS="$old_ifs"
}

append_latency_peer_csv() {
  raw="$1"
  old_ifs="$IFS"
  IFS=', '
  for peer in $raw; do
    peer_allowed_for_p2p "$peer" || continue
    csv_append_unique fastsync_public_peers "$peer"
  done
  IFS="$old_ifs"
}

addpeer_values() {
  for word in ${NODE_ARGS:-}; do
    case "$word" in
      --addpeer=*) echo "${word#*=}" ;;
    esac
  done
}

apply_ordered_fastsync_peers() {
  case "${BDAG_FASTSYNC_PEER_ORDERING:-tiered-latency}" in
    0|off|false|none) return 0 ;;
  esac
  fastsync_lan_peers=""
  fastsync_vpn_peers=""
  fastsync_public_peers=""

  ordering="${BDAG_FASTSYNC_PEER_ORDERING:-tiered-latency}"
  if [ "$ordering" = "flat-latency" ] || [ "$ordering" = "flat" ]; then
    append_latency_peer_csv "${BDAG_FASTSYNC_PEERS:-}"
    append_latency_peer_csv "${BDAG_FASTSNAP_PEERS:-}"
    append_latency_peer_csv "${BOOTSTRAP_PEER_ADDRESSES:-}"
    append_latency_peer_csv "$(addpeer_values | paste -sd, - || true)"
    append_latency_peer_csv "${BDAG_FASTSYNC_LAN_PEERS:-${BDAG_FASTSYNC_LOCAL_PEERS:-}}"
    append_latency_peer_csv "${BDAG_FASTSYNC_VPN_PEERS:-${BDAG_FASTSYNC_PRIVATE_PEERS:-}}"
    append_latency_peer_csv "${BDAG_FASTSYNC_PUBLIC_PEERS:-}"
  else
    classify_peer_csv "${BDAG_P2P_LAN_PEERS:-} ${LAN_PEER_ADDRESSES:-} ${BDAG_FASTSYNC_LAN_PEERS:-${BDAG_FASTSYNC_LOCAL_PEERS:-}}"
    classify_peer_csv "${BDAG_P2P_VPN_PEERS:-} ${VPN_PEER_ADDRESSES:-} ${ZEROTIER_PEER_ADDRESSES:-} ${BDAG_FASTSYNC_VPN_PEERS:-${BDAG_FASTSYNC_PRIVATE_PEERS:-}}"
    classify_peer_csv "${BDAG_P2P_PUBLIC_PEERS:-} ${BDAG_FASTSYNC_PUBLIC_PEERS:-}"
    classify_peer_csv "${BDAG_FASTSYNC_PEERS:-} ${BDAG_FASTSNAP_PEERS:-} ${BOOTSTRAP_PEER_ADDRESSES:-} $(addpeer_values | paste -sd, - || true)"
  fi

  ordered="$fastsync_lan_peers"
  [ -n "$fastsync_vpn_peers" ] && ordered="${ordered:+$ordered,}$fastsync_vpn_peers"
  [ -n "$fastsync_public_peers" ] && ordered="${ordered:+$ordered,}$fastsync_public_peers"
  [ -n "$ordered" ] || return 0

  export BDAG_FASTSNAP_PEERS="$ordered"
  count="$(printf '%s' "$ordered" | awk -F, '{print NF}')"
  if [ "$ordering" = "flat-latency" ] || [ "$ordering" = "flat" ]; then
    log "flat latency FastSync candidates enabled; total=$count"
  else
    log "tiered latency FastSync candidates enabled: LAN first, private/VPN second, public last; total=$count"
  fi
  if [ "${BDAG_FASTSYNC_APPEND_ADDPEERS:-1}" = "1" ]; then
    old_ifs="$IFS"
    IFS=,
    for peer in $ordered; do
      NODE_ARGS="${NODE_ARGS:-} --addpeer=$peer"
    done
    IFS="$old_ifs"
    export NODE_ARGS
  fi
}

node_args_contains_word() {
  needle="$1"
  for word in ${NODE_ARGS:-}; do
    [ "$word" = "$needle" ] && return 0
  done
  return 1
}

append_node_arg_once() {
  flag="$1"
  node_args_contains_word "$flag" && return 0
  NODE_ARGS="${NODE_ARGS:-} $flag"
  export NODE_ARGS
}

apply_default_fastsync_flags() {
  [ "${BDAG_FASTARTIFACTSYNC_ENABLED:-1}" = "1" ] || return 0
  append_node_arg_once "--fastartifactsync"
}

fastsnap_supports_directory_mode() {
  "$1" --help 2>&1 | grep -q -- "--dir-out"
}

node_arg_value() {
  key="$1"
  next=0
  for word in ${NODE_ARGS:-}; do
    if [ "$next" = "1" ]; then
      echo "$word"
      return 0
    fi
    case "$word" in
      --"$key"=*) echo "${word#*=}"; return 0 ;;
      --"$key") next=1 ;;
    esac
  done
  return 1
}

network_datadir() {
  data_parent="$1"
  network="$2"
  case "$data_parent" in
    */"$network") echo "$data_parent" ;;
    *) echo "$data_parent/$network" ;;
  esac
}

maybe_fastsnap_bootstrap() {
  [ "${BDAG_FASTSNAP_ENABLED:-1}" = "1" ] || return 0
  FASTSNAP="${BDAG_FASTSNAP_BINARY:-/usr/local/bin/fastsnap}"
  [ -x "$FASTSNAP" ] || {
    log "fastsnap binary missing; skipping P2P snapshot bootstrap"
    return 0
  }

  network="${BDAG_FASTSNAP_NETWORK:-mainnet}"
  data_parent="${BDAG_FASTSNAP_DATADIR:-$(node_arg_value datadir || true)}"
  data_parent="${data_parent:-/data}"
  data_dir="$(network_datadir "$data_parent" "$network")"
  if [ -d "$data_dir/BdagChain" ]; then
    return 0
  fi

  archive="$data_dir/snapshot.bdsnap"
  mkdir -p "$data_dir"
  if [ -s "$archive" ]; then
    log "importing existing P2P snapshot archive before node startup: $archive"
    "$BIN" snap import --datadir "$data_dir" --path "$archive"
    return 0
  fi

  peers="${BDAG_FASTSNAP_PEERS:-${BOOTSTRAP_PEER_ADDRESSES:-}}"
  if [ -z "$peers" ]; then
    peers="$(addpeer_values | paste -sd, -)"
  fi
  if [ -z "$peers" ]; then
    log "no P2P snapshot peers configured; normal FastSync/legacy sync will start"
    return 0
  fi

  min_tip="${BDAG_FASTSNAP_MIN_TIP:-0}"
  timeout="${BDAG_FASTSNAP_TIMEOUT:-90s}"
  tmp_archive="$archive.download.$$"
  directory_mode="${BDAG_FASTSNAP_DIRECTORY_MODE:-1}"
  if [ "$directory_mode" = "1" ] && ! fastsnap_supports_directory_mode "$fastsnap_bin"; then
    log "fastsnap binary does not support directory install flags; using V2 archive fallback"
    directory_mode=0
  fi
  tmp_dir="${BDAG_FASTSNAP_DIRECTORY_STAGING:-$data_parent/.fastsnap-directory-$network.$$}"
  rm -f "$tmp_archive" "$tmp_archive.manifest.json"
  rm -rf "$tmp_dir" "$tmp_dir.manifest.json"

  old_ifs="$IFS"
  IFS=', '
  for peer in $peers; do
    [ -n "$peer" ] || continue
    log "trying P2P snapshot bootstrap from $peer"
    args="--peer $peer --out $tmp_archive --network $network --min-tip $min_tip --timeout $timeout"
    if [ "$directory_mode" = "1" ]; then
      args="$args --dir-out $tmp_dir --install-dir $data_dir"
      [ "${BDAG_FASTSNAP_DIRECTORY_REPLACE_EXISTING:-1}" = "1" ] && args="$args --replace-existing"
      [ "${BDAG_FASTSNAP_DIRECTORY_MOVE_STAGING:-1}" = "1" ] && args="$args --move-staging"
    fi
    [ "${BDAG_FASTSNAP_ARTIFACT_V2:-1}" = "0" ] && args="$args --artifact-v2=false"
    [ "${BDAG_FASTSNAP_ALLOW_UNSIGNED:-0}" = "1" ] && args="$args --allow-unsigned"
    [ -n "${BDAG_FASTSNAP_PARALLELISM:-}" ] && args="$args --parallelism ${BDAG_FASTSNAP_PARALLELISM}"
    [ -n "${BDAG_FASTSNAP_LEDGER:-}" ] && args="$args --ledger ${BDAG_FASTSNAP_LEDGER}"
    # shellcheck disable=SC2086
    if "$FASTSNAP" $args; then
      if [ -d "$data_dir/BdagChain" ]; then
        if [ -f "$tmp_dir.manifest.json" ]; then
          mv "$tmp_dir.manifest.json" "$data_dir/artifact.manifest.json"
        fi
        rm -f "$tmp_archive" "$tmp_archive.manifest.json"
        rm -rf "$tmp_dir"
        log "downloaded and installed P2P directory artifact before node startup"
        IFS="$old_ifs"
        return 0
      fi
      if [ ! -s "$tmp_archive" ]; then
        log "fastsnap completed but did not install chain data or produce an archive"
        rm -f "$tmp_archive" "$tmp_archive.manifest.json"
        rm -rf "$tmp_dir" "$tmp_dir.manifest.json"
        continue
      fi
      mv "$tmp_archive" "$archive"
      if [ -f "$tmp_archive.manifest.json" ]; then
        mv "$tmp_archive.manifest.json" "$archive.manifest.json"
      fi
      log "importing downloaded P2P snapshot before node startup"
      "$BIN" snap import --datadir "$data_dir" --path "$archive"
      rm -rf "$tmp_dir" "$tmp_dir.manifest.json"
      IFS="$old_ifs"
      return 0
    fi
    rm -f "$tmp_archive" "$tmp_archive.manifest.json"
    rm -rf "$tmp_dir" "$tmp_dir.manifest.json"
  done
  IFS="$old_ifs"

  if [ "${BDAG_FASTSNAP_REQUIRED:-0}" = "1" ]; then
    log "required P2P snapshot bootstrap failed"
    exit 1
  fi
  log "P2P snapshot bootstrap unavailable; falling back to normal FastSync/legacy sync"
}

configure_directory_artifact_serving() {
  if [ -n "${BDAG_FASTSYNC_ARTIFACT_DIRECTORY:-}" ] || [ -n "${BDAG_FASTSYNC_ARTIFACT_MANIFEST:-}" ]; then
    return 0
  fi
  network="${BDAG_FASTSNAP_NETWORK:-mainnet}"
  data_parent="${BDAG_FASTSNAP_DATADIR:-$(node_arg_value datadir || true)}"
  data_parent="${data_parent:-/data}"
  data_dir="$(network_datadir "$data_parent" "$network")"
  manifest="$data_dir/artifact.manifest.json"
  if [ -s "$manifest" ] && [ -d "$data_dir/BdagChain" ]; then
    export BDAG_FASTSYNC_ARTIFACT_DIRECTORY="$data_dir"
    export BDAG_FASTSYNC_ARTIFACT_MANIFEST="$manifest"
    log "enabled Fast Artifact Sync V2 directory serving from $data_dir"
  else
    log "Fast Artifact Sync V2 directory manifest unavailable at $manifest; using archive/legacy serving fallback"
  fi
}

echo "Using node binary: $BIN"
apply_ordered_fastsync_peers
apply_default_fastsync_flags
maybe_fastsnap_bootstrap
configure_directory_artifact_serving

exec nodeworker \
  --node-binary="$BIN" \
  --node-args="${NODE_ARGS:-}" \
  --rpc-url="${RPC_URL:-}" \
  --rollout-window="${ROLLOUT_WINDOW:-0s}" \
  --persist-root="${PERSIST_ROOT:-/opt/bdag}" \
  --health-min-peers="${HEALTH_MIN_PEERS:-0}"
EOF
  chmod +x "$PACKAGE_DIR/src/docker/entrypoint.sh"

  cat > "$PACKAGE_DIR/src/build-images.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCH="${1:-arm64}"
TAG_SUFFIX="${2:-bundle}"
DOCKER=(docker)
if [[ "${BDAG_DOCKER_USE_SUDO:-0}" == "1" ]]; then
  DOCKER=(sudo docker)
fi

case "$ARCH" in
  arm64|aarch64) ARCH=arm64; PLATFORM=linux/arm64 ;;
  *) echo "This release only includes Raspberry Pi 5 / linux-arm64 artifacts." >&2; exit 2 ;;
esac

BIN_DIR="$ROOT/artifacts/binaries/linux-$ARCH"
BUILD_DIR="$ROOT/.image-build/$ARCH"
mkdir -p "$BUILD_DIR/pool" "$BUILD_DIR/node"

test -x "$BIN_DIR/pool" || { echo "Missing $BIN_DIR/pool" >&2; exit 1; }
test -x "$BIN_DIR/dashboard-api" || { echo "Missing $BIN_DIR/dashboard-api" >&2; exit 1; }
test -x "$BIN_DIR/bdag" || { echo "Missing $BIN_DIR/bdag" >&2; exit 1; }
test -x "$BIN_DIR/nodeworker" || { echo "Missing $BIN_DIR/nodeworker" >&2; exit 1; }
test -x "$BIN_DIR/fastsnap" || { echo "Missing $BIN_DIR/fastsnap" >&2; exit 1; }

if [[ -f "$ROOT/scripts/verify-release-architecture.py" ]]; then
  python3 "$ROOT/scripts/verify-release-architecture.py" --target "linux-$ARCH" \
    "$BIN_DIR/pool" "$BIN_DIR/dashboard-api" "$BIN_DIR/bdag" "$BIN_DIR/nodeworker" "$BIN_DIR/fastsnap"
fi

cp "$BIN_DIR/pool" "$BUILD_DIR/pool/pool"
cp "$BIN_DIR/dashboard-api" "$BUILD_DIR/pool/dashboard-api"
cp "$ROOT/asic-pool/schema.sql" "$BUILD_DIR/pool/schema.sql"
cp "$ROOT/src/docker/pool.Dockerfile" "$BUILD_DIR/pool/Dockerfile"
cp "$BIN_DIR/bdag" "$BUILD_DIR/node/bdag"
cp "$BIN_DIR/nodeworker" "$BUILD_DIR/node/nodeworker"
cp "$BIN_DIR/fastsnap" "$BUILD_DIR/node/fastsnap"
cp "$ROOT/src/docker/entrypoint.sh" "$BUILD_DIR/node/entrypoint.sh"
cp "$ROOT/src/docker/node.Dockerfile" "$BUILD_DIR/node/Dockerfile"
chmod +x "$BUILD_DIR/pool/pool" "$BUILD_DIR/pool/dashboard-api" "$BUILD_DIR/node/bdag" "$BUILD_DIR/node/nodeworker" "$BUILD_DIR/node/fastsnap" "$BUILD_DIR/node/entrypoint.sh"

"${DOCKER[@]}" build -t "bdag-release/asic-pool:$TAG_SUFFIX-$ARCH" "$BUILD_DIR/pool"
"${DOCKER[@]}" build -t "bdag-release/node:$TAG_SUFFIX-$ARCH" "$BUILD_DIR/node"
"${DOCKER[@]}" tag "bdag-release/asic-pool:$TAG_SUFFIX-$ARCH" bdag-release/asic-pool:local
"${DOCKER[@]}" tag "bdag-release/node:$TAG_SUFFIX-$ARCH" bdag-release/node:local

echo "Built and tagged linux/$ARCH images:"
echo "  bdag-release/asic-pool:$TAG_SUFFIX-$ARCH -> bdag-release/asic-pool:local"
echo "  bdag-release/node:$TAG_SUFFIX-$ARCH -> bdag-release/node:local"
EOF
  chmod +x "$PACKAGE_DIR/src/build-images.sh"
}

write_reassemble_helpers() {
  local data_zip="$1"
  cat > "$HELPERS_DIR/reassemble-blockdag-chain-data-linux-mac.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
BASE="$data_zip"
cd "\$(dirname "\$0")"
echo "BlockDAG chain-data reassembly"
echo "================================"
mapfile -t parts < <(ls -1 "\$BASE".part-* 2>/dev/null | sort)
if (( \${#parts[@]} == 0 )); then
  echo "No chain-data part files found next to this script."
  exit 1
fi
cat "\${parts[@]}" > "\$BASE"
if [[ -f "\$BASE.sha256" ]] && command -v sha256sum >/dev/null 2>&1; then
  sha256sum -c "\$BASE.sha256"
elif [[ -f "\$BASE.parts.sha256" ]] && command -v sha256sum >/dev/null 2>&1; then
  sha256sum -c "\$BASE.parts.sha256"
fi
if command -v unzip >/dev/null 2>&1; then
  unzip -tq "\$BASE"
fi
echo "Created: \$BASE"
echo "Put this zip next to the stack installer folder before running ./install.sh."
EOF
  chmod +x "$HELPERS_DIR/reassemble-blockdag-chain-data-linux-mac.sh"

  cat > "$HELPERS_DIR/install-on-pi5-linux.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "\$(dirname "\$0")"

STACK_ZIP="$RELEASE_NAME-stack.zip"
DATA_ZIP="$RELEASE_NAME-chain-data.zip"

echo "BlockDAG Pi5 installer helper"
echo "============================="

if [[ ! -d "$RELEASE_NAME" ]]; then
  test -f "\$STACK_ZIP" || { echo "Missing \$STACK_ZIP"; exit 1; }
  unzip -q "\$STACK_ZIP"
fi

if [[ ! -f "\$DATA_ZIP" ]] && compgen -G "\$DATA_ZIP.part-*" >/dev/null; then
  bash ./reassemble-blockdag-chain-data-linux-mac.sh
fi

if [[ -f "\$DATA_ZIP" ]]; then
  mv -f "\$DATA_ZIP" "$RELEASE_NAME/"
fi

cd "$RELEASE_NAME"
chmod +x ./install.sh
./install.sh
EOF
  chmod +x "$HELPERS_DIR/install-on-pi5-linux.sh"
}

refresh_arm64_images_from_existing_base() {
  local pool_container="bdag-pi5-pool-image-$STAMP"
  local node_container="bdag-pi5-node-image-$STAMP"

  docker image inspect bdag-release/asic-pool:bundle-arm64 >/dev/null
  docker image inspect bdag-release/node:bundle-arm64 >/dev/null

  docker tag bdag-release/asic-pool:bundle-arm64 "bdag-release/asic-pool:previous-bundle-arm64-$STAMP" || true
  docker tag bdag-release/node:bundle-arm64 "bdag-release/node:previous-bundle-arm64-$STAMP" || true

  docker rm -f "$pool_container" "$node_container" >/dev/null 2>&1 || true

  docker create --platform linux/arm64 --name "$pool_container" --entrypoint /bin/sh bdag-release/asic-pool:bundle-arm64 -c true >/dev/null
  docker cp "$PACKAGE_DIR/artifacts/binaries/linux-arm64/pool" "$pool_container:/usr/local/bin/pool"
  docker cp "$PACKAGE_DIR/artifacts/binaries/linux-arm64/pool" "$pool_container:/usr/local/bin/mining-pool" 2>/dev/null || true
  docker cp "$PACKAGE_DIR/artifacts/binaries/linux-arm64/dashboard-api" "$pool_container:/usr/local/bin/dashboard-api" 2>/dev/null || true
  docker commit \
    --change 'ENTRYPOINT ["/usr/local/bin/pool"]' \
    --change "LABEL org.opencontainers.image.source=BlockdagEngineering/pool" \
    --change "LABEL org.opencontainers.image.revision=$POOL_COMMIT" \
    --change "LABEL org.opencontainers.image.version=$RELEASE_NAME" \
    "$pool_container" "bdag-release/asic-pool:bundle-arm64" >/dev/null
  docker rm -f "$pool_container" >/dev/null

  docker create --platform linux/arm64 --name "$node_container" bdag-release/node:bundle-arm64 >/dev/null
  docker cp "$PACKAGE_DIR/artifacts/binaries/linux-arm64/bdag" "$node_container:/usr/local/bin/bdag"
  docker cp "$PACKAGE_DIR/artifacts/binaries/linux-arm64/nodeworker" "$node_container:/usr/local/bin/nodeworker"
  docker cp "$PACKAGE_DIR/artifacts/binaries/linux-arm64/fastsnap" "$node_container:/usr/local/bin/fastsnap"
  docker cp "$PACKAGE_DIR/src/docker/entrypoint.sh" "$node_container:/usr/local/bin/entrypoint.sh" 2>/dev/null || true
  docker commit \
    --change 'ENTRYPOINT ["/bin/sh", "/usr/local/bin/entrypoint.sh"]' \
    --change "LABEL org.opencontainers.image.source=BlockdagEngineering/blockdag-corechain" \
    --change "LABEL org.opencontainers.image.revision=$NODE_COMMIT" \
    --change "LABEL org.opencontainers.image.version=$RELEASE_NAME" \
    "$node_container" "bdag-release/node:bundle-arm64" >/dev/null
  docker rm -f "$node_container" >/dev/null

  docker tag bdag-release/asic-pool:bundle-arm64 bdag-release/asic-pool:local
  docker tag bdag-release/node:bundle-arm64 bdag-release/node:local
}

write_readmes() {
  local stack_zip="$1" data_zip="$2" image_sha="$3"
  cat > "$PACKAGE_DIR/README.html" <<EOF
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BlockDAG Pi5 Pool Installer</title>
  <style>
    :root { color-scheme: dark; --bg:#101318; --panel:#171d24; --panel2:#202833; --line:#344252; --text:#eef4f6; --muted:#aeb9c1; --green:#46d990; --blue:#62c8ff; --amber:#f0bd5d; }
    body { margin:0; background:var(--bg); color:var(--text); font:15px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    main { max-width:1080px; margin:0 auto; padding:28px 20px 44px; }
    h1 { margin:0 0 8px; font-size:32px; }
    h2 { margin-top:26px; border-bottom:1px solid var(--line); padding-bottom:7px; }
    code, pre { background:#0b0e12; border:1px solid var(--line); border-radius:6px; color:#f7fbff; }
    code { padding:2px 5px; }
    pre { padding:13px; overflow:auto; }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:12px; }
    .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; }
    .chip { display:inline-block; padding:3px 9px; border:1px solid var(--line); border-radius:999px; color:var(--muted); margin:0 5px 5px 0; }
    .good { color:var(--green); }
    .warn { color:var(--amber); }
    .blue { color:var(--blue); }
  </style>
</head>
<body>
<main>
  <h1>BlockDAG Pi5 Pool Installer</h1>
  <p>This is a Raspberry Pi 5 / linux-arm64 release package for a BlockDAG ASIC pool with selectable single-node or double-node backends, a beginner-friendly installer, and optional fast-sync chain seed.</p>
  <p><span class="chip good">ARM64</span><span class="chip">Pi5</span><span class="chip">Single-node default</span><span class="chip">Double-node option</span><span class="chip">Dashboard + sentinel</span><span class="chip">Optional miner scan</span></p>

  <section class="grid">
    <div class="card"><strong>Stack archive</strong><p><code>$stack_zip</code><br>Installer, compose files, dashboard tooling, ARM64 binaries, and prebuilt ARM64 BlockDAG images.</p></div>
    <div class="card"><strong>Data archive</strong><p><code>$data_zip.part-*</code><br>Split chain seed. Reassemble to reduce sync time on the Pi.</p></div>
    <div class="card"><strong>Stable pool commit</strong><p><code>$POOL_COMMIT</code></p></div>
    <div class="card"><strong>Stable node commit</strong><p><code>$NODE_COMMIT</code></p></div>
  </section>

  <h2>Fast Install On The Pi</h2>
  <pre><code>chmod +x install-on-pi5-linux.sh
./install-on-pi5-linux.sh</code></pre>
  <p>The helper unzips the stack, reassembles chain data if the part files are present, and then runs the guided installer.</p>

  <h2>Manual Install</h2>
  <pre><code>bash reassemble-blockdag-chain-data-linux-mac.sh
unzip $stack_zip
mv $data_zip $RELEASE_NAME/
cd $RELEASE_NAME
./install.sh</code></pre>

  <h2>What The Installer Asks</h2>
  <div class="grid">
    <div class="card"><strong>Pool LAN IP</strong><p>The IP your ASICs should connect to, for example <code>192.168.1.10</code>.</p></div>
    <div class="card"><strong>ASIC scan range</strong><p>Usually your subnet, for example <code>192.168.1.0/24</code>.</p></div>
    <div class="card"><strong>Backend mode</strong><p><code>single</code> runs node2 only for lower USB power draw. <code>double</code> enables node1 with the <code>dual-node</code> Compose profile.</p></div>
    <div class="card"><strong>Mining templates</strong><p>Disabled by default. Enable only when real miners will be attached; no-miner installs stay sync-only.</p></div>
    <div class="card"><strong>Reward wallet</strong><p>The BDAG/EVM address to mine to. The installer refuses the all-zero placeholder.</p></div>
    <div class="card"><strong>Dashboard exposure</strong><p>Default is local-only. Choose LAN exposure only on a trusted private network.</p></div>
  </div>

  <h2>After Install</h2>
  <pre><code>./tools/status.sh
docker compose ps</code></pre>
  <p>Miners should point to <code>stratum+tcp://&lt;pool-lan-ip&gt;:3334</code>. The local dashboard defaults to <code>http://127.0.0.1:8088</code> on the Pi.</p>

  <h2>Build Identity</h2>
  <p>ARM64 image archive SHA256: <code>$image_sha</code></p>
</main>
<script type="application/json" id="agent-metadata">
{
  "document_type": "pi5_arm64_release_readme",
  "release_name": "$RELEASE_NAME",
  "release_dir": "$RELEASE_DIR",
  "pool_commit": "$POOL_COMMIT",
  "node_commit": "$NODE_COMMIT",
  "architecture": "linux-arm64",
  "chain_source": "$CHAIN_SOURCE",
  "stack_archive": "$ARCHIVES_DIR/$stack_zip",
  "chain_data_archive": "$ARCHIVES_DIR/$data_zip",
  "image_archive_sha256": "$image_sha",
  "safety": "This package is built from the live stable production configuration. It defaults to one sync-only backend node, can opt into a second backend node, and does not require restarting the source mining host."
}
</script>
</body>
</html>
EOF

  cp "$PACKAGE_DIR/README.html" "$SHARE_DIR/READ_ME_FIRST_PI5.html"
}

write_manifest() {
  local image_sha="$1" stack_sha="$2" data_sha="$3" chain_source_resolved="$4"
  cat > "$PACKAGE_DIR/MANIFEST.json" <<EOF
{
  "package_name": "$RELEASE_NAME",
  "created_at_local": "$(date -Is)",
  "purpose": "Raspberry Pi 5 / linux-arm64 BlockDAG ASIC pool installer with current stable stack, selectable single/double backend nodes, and optional fast-sync chain seed.",
  "architecture": "linux-arm64",
  "runtime_topology": {
    "pool": "single Stratum pool by default",
    "backend_nodes": "1 by default, 2 with COMPOSE_PROFILES=dual-node",
    "database": "postgres:15-alpine",
    "rpc_failover": "haproxy:2.9-alpine",
    "dashboard": "local Python dashboard/watchdog installed by ops/install-dashboard.sh"
  },
  "source_refs": {
    "pool": {
      "repo": "$POOL_REPO",
      "commit": "$POOL_COMMIT"
    },
    "node": {
      "repo": "$NODE_REPO",
      "commit": "$NODE_COMMIT"
    }
  },
  "docker_images": {
    "linux_arm64": {
      "archive": "artifacts/images/linux-arm64/blockdag-stack-linux-arm64-images.tar.zst",
      "sha256": "$image_sha",
      "images": [
        "bdag-release/asic-pool:bundle-arm64",
        "bdag-release/node:bundle-arm64"
      ],
      "support_images_pulled_on_install": [
        "postgres:15-alpine",
        "haproxy:2.9-alpine"
      ]
    }
  },
  "archives": {
    "stack_sha256": "$stack_sha",
    "chain_data_sha256": "$data_sha",
    "chain_source": "$CHAIN_SOURCE",
    "chain_source_resolved": "$chain_source_resolved",
    "chain_data_part_size": "$PART_SIZE"
  },
  "safety_notes": [
    "The source mining host services were not restarted to create this package.",
    "The chain seed excludes node network identity material from the hourly snapshot process.",
    "The installer writes fresh local RPC and PostgreSQL passwords on the Pi."
  ]
}
EOF
}

write_release_page() {
  local stack_zip="$1" data_zip="$2"
  cat > "$RELEASE_DIR/RELEASE.html" <<EOF
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>$RELEASE_NAME</title></head>
<body>
  <h1>$RELEASE_NAME</h1>
  <p>Raspberry Pi 5 ARM64 BlockDAG pool release.</p>
  <ul>
    <li>Release folder: <code>$RELEASE_DIR</code></li>
    <li>Share folder: <code>$SHARE_DIR</code></li>
    <li>Stack: <code>archives/$stack_zip</code></li>
    <li>Chain data: <code>archives/$data_zip.part-*</code></li>
  </ul>
  <script type="application/json" id="agent-metadata">
  {
    "document_type": "pi5_arm64_release_record",
    "release_name": "$RELEASE_NAME",
    "release_dir": "$RELEASE_DIR",
    "share_dir": "$SHARE_DIR",
    "pool_commit": "$POOL_COMMIT",
    "node_commit": "$NODE_COMMIT",
    "architecture": "linux-arm64"
  }
  </script>
</body>
</html>
EOF
}

update_index() {
  ln -sfn "$RELEASE_DIR" "$RELEASE_ROOT/latest-blockdag-pool"
  cat > "$RELEASE_ROOT/INDEX.html" <<EOF
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>BlockDAG Releases</title></head>
<body>
  <h1>BlockDAG Releases</h1>
  <p>Latest: <a href="$RELEASE_NAME/RELEASE.html">$RELEASE_NAME</a></p>
  <p>Release root: <code>$RELEASE_ROOT</code></p>
  <script type="application/json" id="agent-metadata">
  {"document_type":"release_index","latest_release":"$RELEASE_NAME","release_root":"$RELEASE_ROOT"}
  </script>
</body>
</html>
EOF
}

verify_stack_status() {
  local status
  status="$(curl -fsS http://127.0.0.1:8088/api/status | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("overall","unknown"))' 2>/dev/null || true)"
  if [[ "$status" != "ok" ]]; then
    warn "Dashboard status is '$status'. Continuing because release packaging does not alter the live stack."
  fi
}

main() {
  need_tool git
  need_tool go
  need_tool docker
  need_tool python3
  need_tool rsync
  need_tool zip
  need_tool unzip
  need_tool split
  need_tool sha256sum
  need_tool zstd
  need_tool file
  need_tool aarch64-linux-gnu-gcc

  verify_stack_status

  local base node1_peers node2_peers bin_dir image_dir image_archive image_sha stack_zip data_zip stack_sha data_sha chain_source_resolved
  base="$(base_package)"
  node1_peers="$(extract_peer_value NODE1_PEER_ADDRESSES)"
  node2_peers="$(extract_peer_value NODE2_PEER_ADDRESSES)"
  bin_dir="$PACKAGE_DIR/artifacts/binaries/linux-arm64"
  image_dir="$PACKAGE_DIR/artifacts/images/linux-arm64"
  image_archive="$image_dir/blockdag-stack-linux-arm64-images.tar.zst"
  stack_zip="$RELEASE_NAME-stack.zip"
  data_zip="$RELEASE_NAME-chain-data.zip"
  chain_source_resolved="$(readlink -f "$CHAIN_SOURCE" 2>/dev/null || printf '%s' "$CHAIN_SOURCE")"

  say "Preparing release tree: $RELEASE_DIR"
  rm -rf "$RELEASE_DIR" "$BUILD_ROOT"
  git -C "$POOL_REPO" worktree prune
  git -C "$NODE_REPO" worktree prune
  mkdir -p "$PACKAGE_DIR" "$ARCHIVES_DIR" "$HELPERS_DIR" "$SHARE_DIR" "$BUILD_ROOT"

  say "Copying base installer from $base"
  rsync -a --delete \
    --exclude='.git/' \
    --exclude='data/' \
    --exclude='data-restore/' \
    --exclude='ops/runtime/' \
    --exclude='ops/runtime-*/' \
    --exclude='ops/__pycache__/' \
    --exclude='asic-pool/.env' \
    --exclude='.env' \
    --exclude='chain-data/' \
    "$base"/ "$PACKAGE_DIR"/

  say "Overlaying current production ops files"
  rsync -a --delete --exclude='runtime/' --exclude='runtime-*/' --exclude='__pycache__/' "$PROJECT_ROOT/ops"/ "$PACKAGE_DIR/ops"/
  rsync -a "$PROJECT_ROOT/scripts"/ "$PACKAGE_DIR/scripts"/
  rsync -a "$PROJECT_ROOT/haproxy.cfg" "$PACKAGE_DIR"/
  mkdir -p "$PACKAGE_DIR/asic-pool"
  rsync -a "$PROJECT_ROOT/asic-pool/schema.sql" "$PACKAGE_DIR/asic-pool/schema.sql"
  cp "$PROJECT_ROOT/ops/release-install.sh" "$PACKAGE_DIR/install.sh"
  chmod +x "$PACKAGE_DIR/install.sh"
  write_release_compose
  guard_release_compose

  rm -rf "$PACKAGE_DIR/artifacts/binaries" "$PACKAGE_DIR/artifacts/images"
  mkdir -p "$bin_dir" "$image_dir"
  write_env_examples "$node1_peers" "$node2_peers"
  write_image_build_files
  sanitize_release_tree
  guard_release_compose

  say "Creating source worktrees"
  git -C "$POOL_REPO" worktree add --detach "$BUILD_ROOT/pool-src" "$POOL_COMMIT"
  git -C "$NODE_REPO" worktree add --detach "$BUILD_ROOT/node-src" "$NODE_COMMIT"

  say "Building ARM64 pool binaries"
  (
    cd "$BUILD_ROOT/pool-src"
    env GOFLAGS=-buildvcs=false CC=aarch64-linux-gnu-gcc GOOS=linux GOARCH=arm64 CGO_ENABLED=1 go build -trimpath -ldflags="-s -w" -o "$bin_dir/pool" ./cmd/pool
    env GOFLAGS=-buildvcs=false CC=aarch64-linux-gnu-gcc GOOS=linux GOARCH=arm64 CGO_ENABLED=1 go build -trimpath -ldflags="-s -w" -o "$bin_dir/dashboard-api" ./cmd/dashboard-api
  )

  say "Building ARM64 node binaries"
  (
    cd "$BUILD_ROOT/node-src"
    env GOFLAGS=-buildvcs=false CC=aarch64-linux-gnu-gcc CGO_ENABLED=1 GOOS=linux GOARCH=arm64 \
      go build -trimpath -ldflags="-X github.com/BlockdagNetworkLabs/bdag/version.Build=release-${NODE_COMMIT:0:7}" \
      -o "$bin_dir/bdag" "github.com/BlockdagNetworkLabs/bdag/cmd/bdag"
    env GOFLAGS=-buildvcs=false CC=aarch64-linux-gnu-gcc CGO_ENABLED=1 GOOS=linux GOARCH=arm64 \
      go build -trimpath -ldflags="-X github.com/BlockdagNetworkLabs/bdag/version.Build=release-${NODE_COMMIT:0:7}" \
      -o "$bin_dir/nodeworker" "github.com/BlockdagNetworkLabs/bdag/cmd/nodeworker"
    env GOFLAGS=-buildvcs=false CC=aarch64-linux-gnu-gcc CGO_ENABLED=1 GOOS=linux GOARCH=arm64 \
      go build -trimpath -ldflags="-X github.com/BlockdagNetworkLabs/bdag/version.Build=release-${NODE_COMMIT:0:7}" \
      -o "$bin_dir/fastsnap" "github.com/BlockdagNetworkLabs/bdag/cmd/fastsnap"
  )

  chmod +x "$bin_dir"/pool "$bin_dir"/dashboard-api "$bin_dir"/bdag "$bin_dir"/nodeworker "$bin_dir"/fastsnap
  python3 "$PROJECT_ROOT/scripts/verify-release-architecture.py" --target linux-arm64 \
    "$bin_dir"/pool "$bin_dir"/dashboard-api "$bin_dir"/bdag "$bin_dir"/nodeworker "$bin_dir"/fastsnap
  file "$bin_dir"/pool "$bin_dir"/dashboard-api "$bin_dir"/bdag "$bin_dir"/nodeworker "$bin_dir"/fastsnap | tee "$bin_dir/FILE_TYPES.txt"
  (cd "$bin_dir" && sha256sum pool dashboard-api bdag nodeworker fastsnap > SHA256SUMS)

  say "Refreshing ARM64 Docker images from existing ARM64 base images"
  refresh_arm64_images_from_existing_base

  say "Exporting ARM64 Docker image bundle"
  docker save bdag-release/asic-pool:bundle-arm64 bdag-release/node:bundle-arm64 |
    zstd -T0 -3 -o "$image_archive"
  image_sha="$(sha256sum "$image_archive" | awk '{print $1}')"
  (cd "$PACKAGE_DIR" && sha256sum "artifacts/images/linux-arm64/blockdag-stack-linux-arm64-images.tar.zst" > artifacts/images/SHA256SUMS)
  docker image inspect bdag-release/asic-pool:bundle-arm64 bdag-release/node:bundle-arm64 \
    --format '{{.RepoTags}} {{.Architecture}} {{.Id}}' > "$image_dir/IMAGE_INSPECT.txt"

  stack_sha="see $stack_zip.sha256 sidecar file"

  if [[ -e "$CHAIN_SOURCE" ]]; then
    say "Creating chain-data archive from $chain_source_resolved"
    local data_stage="$RELEASE_DIR/data-stage"
    rm -rf "$data_stage"
    mkdir -p "$data_stage/chain-data"
    if [[ -d "$CHAIN_SOURCE" ]]; then
      (
        cd "$CHAIN_SOURCE"
        zip -qr -1 "$data_stage/chain-data/chain-data-seed.zip" . \
          -x 'mainnet/LOCK' \
          -x 'mainnet/network.key' \
          -x 'mainnet/peerstore/*' \
          -x 'mainnet/keystore/*' \
          -x 'mainnet/BdagChain/LOCK' \
          -x 'mainnet/bdageth/nodekey' \
          -x 'mainnet/bdageth/LOCK' \
          -x 'mainnet/bdageth/nodes/*' \
          -x 'mainnet/bdageth/blobpool/*' \
          -x 'mainnet/bdageth/transactions.rlp' \
          -x 'mainnet/bdageth/chaindata/LOCK'
      )
    else
      cp "$CHAIN_SOURCE" "$data_stage/chain-data/chain-data-seed.zip"
    fi
    (cd "$data_stage" && zip -qr -0 "$ARCHIVES_DIR/$data_zip" chain-data)
    data_sha="$(sha256sum "$ARCHIVES_DIR/$data_zip" | awk '{print $1}')"
    (cd "$ARCHIVES_DIR" && sha256sum "$data_zip" > "$data_zip.sha256")

    say "Splitting chain-data archive into $PART_SIZE parts"
    split -b "$PART_SIZE" -d -a 3 --numeric-suffixes=1 "$ARCHIVES_DIR/$data_zip" "$ARCHIVES_DIR/$data_zip.part-"
    (cd "$ARCHIVES_DIR" && sha256sum "$data_zip".part-* > "$data_zip.parts.sha256")
    rm -rf "$data_stage"
  else
    warn "Chain source not found: $CHAIN_SOURCE"
    data_sha="missing"
  fi

  write_reassemble_helpers "$data_zip"
  write_readmes "$stack_zip" "$data_zip" "$image_sha"
  write_manifest "$image_sha" "$stack_sha" "$data_sha" "$chain_source_resolved"
  write_release_page "$stack_zip" "$data_zip"

  say "Creating stack archive"
  (cd "$UNPACKED_DIR" && zip -qr "$ARCHIVES_DIR/$stack_zip" "$RELEASE_NAME")
  stack_sha="$(sha256sum "$ARCHIVES_DIR/$stack_zip" | awk '{print $1}')"
  (cd "$ARCHIVES_DIR" && sha256sum "$stack_zip" > "$stack_zip.sha256")

  cp "$ARCHIVES_DIR/$stack_zip" "$ARCHIVES_DIR/$stack_zip.sha256" "$SHARE_DIR"/
  if compgen -G "$ARCHIVES_DIR/$data_zip.part-*" >/dev/null; then
    cp "$ARCHIVES_DIR/$data_zip".part-* "$ARCHIVES_DIR/$data_zip.parts.sha256" "$ARCHIVES_DIR/$data_zip.sha256" "$SHARE_DIR"/
  fi
  cp "$HELPERS_DIR"/reassemble-blockdag-chain-data-linux-mac.sh "$HELPERS_DIR"/install-on-pi5-linux.sh "$SHARE_DIR"/

  update_index
  say "Release complete"
  echo "$RELEASE_DIR"
}

main "$@"
