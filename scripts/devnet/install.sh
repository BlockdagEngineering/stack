#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

COMPOSE_FILES="-f docker-compose.yml -f docker-compose.devnet.yml"
ENV_FILE=".env.devnet"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command missing: $1" >&2
    exit 1
  fi
}

payload_value() {
  key="$1"
  file="release-payload.env"
  if [ ! -f "$file" ]; then
    return 0
  fi
  awk -F= -v k="$key" '$1 == k {print substr($0, index($0, "=") + 1); exit}' "$file"
}

set_env_value() {
  file="$1"
  key="$2"
  value="$3"
  tmp="${file}.tmp"
  if [ -f "$file" ] && grep -q "^${key}=" "$file"; then
    awk -v k="$key" -v v="$value" 'BEGIN{done=0} $0 ~ "^" k "=" {print k "=" v; done=1; next} {print} END{if(!done) print k "=" v}' "$file" > "$tmp"
  else
    cp "$file" "$tmp"
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  mv "$tmp" "$file"
}

if [ ! -f ".env.devnet.example" ]; then
  echo "Missing .env.devnet.example in release payload" >&2
  exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
  cp ".env.devnet.example" "$ENV_FILE"
  echo "Created $ENV_FILE from .env.devnet.example"
else
  echo "Using existing $ENV_FILE"
fi

docker_platform="$(payload_value DOCKER_PLATFORM || true)"
if [ -z "$docker_platform" ]; then
  arch="$(payload_value BDAG_RELEASE_PAYLOAD_ARCH || true)"
  if [ -n "$arch" ]; then
    docker_platform="linux/$arch"
  else
    docker_platform="linux/amd64"
  fi
fi

set_env_value "$ENV_FILE" DOCKERFILE dockerfile
set_env_value "$ENV_FILE" DOCKER_PLATFORM "$docker_platform"
set_env_value "$ENV_FILE" STACK_SRC_CONTEXT .
set_env_value "$ENV_FILE" BLOCKDAG_CORECHAIN_CONTEXT .
set_env_value "$ENV_FILE" POOL_SRC_CONTEXT .
set_env_value "$ENV_FILE" COLLECTOR_SRC_CONTEXT ./collector
set_env_value "$ENV_FILE" DASHBOARD_SRC_CONTEXT .

if [ "${BDAG_DEVNET_INSTALL_TEST_WRITE_ENV_ONLY:-0}" = "1" ]; then
  echo "Devnet installer smoke test wrote $ENV_FILE"
  exit 0
fi

require_command docker
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required. Install or update Docker, then re-run this installer." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is installed, but this user cannot reach the Docker daemon." >&2
  exit 1
fi

docker compose --env-file "$ENV_FILE" $COMPOSE_FILES up -d --build pool-db node pool collector dashboard cpu-miner
docker compose --env-file "$ENV_FILE" $COMPOSE_FILES ps

cat <<'EOF'

Devnet is starting.

Endpoints:
  EVM HTTP RPC:   http://127.0.0.1:18545
  EVM WebSocket:  ws://127.0.0.1:18546
  Native DAG RPC: http://127.0.0.1:38131
  Pool Stratum:   stratum+tcp://127.0.0.1:3334
  Pool metrics:   http://127.0.0.1:9090/metrics
  Collector API:  http://127.0.0.1:9280/api/status
  Dashboard:      http://127.0.0.1:8088

Status:
  docker compose --env-file .env.devnet -f docker-compose.yml -f docker-compose.devnet.yml ps
  docker compose --env-file .env.devnet -f docker-compose.yml -f docker-compose.devnet.yml logs -f node pool cpu-miner
EOF
