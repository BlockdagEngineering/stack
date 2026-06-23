#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPOS_DIR="${REPOS_DIR:-$(cd "$ROOT/.." && pwd)}"
TARGET_DIR="${TARGET_DIR:-${TEST_STACK:-$REPOS_DIR/test-stack}}"
VERSION="${VERSION:-pool-vlocal-test}"
COMPOSE_CONFIG_OUT="${COMPOSE_CONFIG_OUT:-/tmp/stack-test-compose.yml}"

TARGET_DIR="$(realpath -m "$TARGET_DIR")"

usage() {
  cat <<'EOF'
usage: scripts/sim-release.sh

Clears the local test stack directory except for node-data/, rebuilds release
payload artifacts, packages a local release zip, deploys that zip back into the
test stack directory, and validates the rendered compose config.

Environment overrides:
  REPOS_DIR=/home/ben/repos
  TARGET_DIR=/home/ben/repos/test-stack
  TEST_STACK=/home/ben/repos/test-stack
  VERSION=pool-vlocal-test
  TARGET=linux-amd64
  GOARCH=amd64
  DOCKER_PLATFORM=linux/amd64
  COMPOSE_CONFIG_OUT=/tmp/stack-test-compose.yml
  SIM_RELEASE_SKIP_COMPOSE_DOWN=1
EOF
}

case "${1:-}" in
  -h|--help|help)
    usage
    exit 0
    ;;
  "")
    ;;
  *)
    echo "unexpected argument: $1" >&2
    usage >&2
    exit 2
    ;;
esac

refuse_unsafe_target() {
  case "$TARGET_DIR" in
    ""|"/"|"$HOME"|"$REPOS_DIR"|"$ROOT"|"$ROOT/"*)
      echo "Refusing to clear unsafe release simulation target: $TARGET_DIR" >&2
      exit 1
      ;;
  esac
}

clear_target_dir() {
  refuse_unsafe_target
  mkdir -p "$TARGET_DIR"

  if [[ "${SIM_RELEASE_SKIP_COMPOSE_DOWN:-0}" != "1" && -f "$TARGET_DIR/docker-compose.yml" ]]; then
    echo "Stopping existing compose stack in $TARGET_DIR"
    (cd "$TARGET_DIR" && docker compose down --remove-orphans) || true
  fi

  echo "Clearing $TARGET_DIR"
  if [[ -e "$TARGET_DIR/node-data" ]]; then
    echo "Preserving existing node-data/"
  fi
  local attempt
  for attempt in 1 2 3; do
    chmod -R u+rwX "$TARGET_DIR" 2>/dev/null || true
    if find "$TARGET_DIR" -mindepth 1 -maxdepth 1 ! -name node-data -exec rm -rf -- {} +; then
      return 0
    fi
    sleep "$attempt"
  done
  echo "Failed to clear $TARGET_DIR after retries." >&2
  exit 1
}

require_file() {
  local path="$1"
  [[ -f "$path" ]] || { echo "missing expected file: $path" >&2; exit 1; }
}

clear_target_dir

echo "Rebuilding payload artifacts into $TARGET_DIR"
TEST_STACK="$TARGET_DIR" REPOS_DIR="$REPOS_DIR" "$ROOT/scripts/rebuild-test-stack-artifacts.sh" all

echo "Building local release zip from $TARGET_DIR"
release_output="$(
  PAYLOAD_SOURCE="$TARGET_DIR" "$ROOT/scripts/local-test-release.sh" "$VERSION"
)"
printf '%s\n' "$release_output"
ZIP_PATH="$(printf '%s\n' "$release_output" | awk '/[.]zip$/ {print; exit}')"
[[ -n "$ZIP_PATH" ]] || { echo "local-test-release.sh did not print a release zip path" >&2; exit 1; }
require_file "$ZIP_PATH"

echo "Deploying $ZIP_PATH into $TARGET_DIR"
"$ROOT/scripts/local-deploy-test-release.sh" "$ZIP_PATH" "$TARGET_DIR"

echo "Validating compose config"
(
  cd "$TARGET_DIR"
  BDAG_STACK_HOST_ROOT="$TARGET_DIR" docker compose --env-file .env config > "$COMPOSE_CONFIG_OUT"
)

grep -q "source: $TARGET_DIR/node-data" "$COMPOSE_CONFIG_OUT" || {
  echo "compose config does not render node-data as a bind mount from $TARGET_DIR/node-data" >&2
  exit 1
}
grep -q "target: /var/lib/bdagStack/node" "$COMPOSE_CONFIG_OUT" || {
  echo "compose config does not mount node data at /var/lib/bdagStack/node" >&2
  exit 1
}

echo "Release simulation complete"
echo "  target:  $TARGET_DIR"
echo "  zip:     $ZIP_PATH"
echo "  compose: $COMPOSE_CONFIG_OUT"
