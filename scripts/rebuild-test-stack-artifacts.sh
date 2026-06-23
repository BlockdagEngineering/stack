#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPOS_DIR="${REPOS_DIR:-$(cd "$ROOT/.." && pwd)}"
TEST_STACK="${TEST_STACK:-$REPOS_DIR/test-stack}"
TARGET="${TARGET:-linux-amd64}"
GOOS="${GOOS:-linux}"
CGO_ENABLED="${CGO_ENABLED:-1}"

usage() {
  cat <<'EOF'
usage: scripts/rebuild-test-stack-artifacts.sh [all|node|pool|dashboard|collector|BIN...]

Builds local upstream clones into test-stack so local release packaging can pick
up fresh artifacts after source changes.

Components:
  node        builds blockdag-node and nodeworker from ../blockdag-corechain
  pool        builds mining-pool and dashboard-api from ../pool
  dashboard   builds dashboard from ../dashboard2
  collector   syncs ../collector into test-stack/collector
  all         runs node, pool, dashboard, and collector

Useful overrides:
  REPOS_DIR=/home/ben/repos
  TEST_STACK=/home/ben/repos/test-stack
  GOARCH=amd64|arm64
  CC=gcc|aarch64-linux-gnu-gcc
EOF
}

case "$TARGET" in
  linux-amd64)
    GOARCH="${GOARCH:-amd64}"
    CC="${CC:-gcc}"
    ;;
  linux-arm64)
    GOARCH="${GOARCH:-arm64}"
    CC="${CC:-aarch64-linux-gnu-gcc}"
    ;;
  *)
    echo "unsupported TARGET=$TARGET" >&2
    exit 2
    ;;
esac

need_dir() {
  local dir="$1"
  [[ -d "$dir" ]] || { echo "missing directory: $dir" >&2; exit 1; }
}

need_file() {
  local file="$1"
  [[ -f "$file" ]] || { echo "missing file: $file" >&2; exit 1; }
}

go_common_env() {
  env \
    GOOS="$GOOS" \
    GOARCH="$GOARCH" \
    CGO_ENABLED="$CGO_ENABLED" \
    CC="$CC" \
    GOFLAGS=-buildvcs=false \
    "$@"
}

build_node() {
  local src="$REPOS_DIR/blockdag-corechain"
  need_file "$src/cmd/bdag/bdag.go"
  need_file "$src/cmd/nodeworker/main.go"
  mkdir -p "$TEST_STACK/bin"
  (
    cd "$src"
    local build_ref
    build_ref="$(git rev-parse --short=7 HEAD 2>/dev/null || printf local)"
    go_common_env go build -trimpath \
      -ldflags="-X github.com/BlockdagNetworkLabs/bdag/version.Build=local-${build_ref}" \
      -o "$TEST_STACK/bin/blockdag-node" ./cmd/bdag
    go_common_env go build -trimpath \
      -ldflags="-X github.com/BlockdagNetworkLabs/bdag/version.Build=local-${build_ref}" \
      -o "$TEST_STACK/bin/nodeworker" ./cmd/nodeworker
  )
}

build_pool() {
  local src="$REPOS_DIR/pool"
  need_file "$src/cmd/pool/main.go"
  need_file "$src/cmd/dashboard-api/main.go"
  mkdir -p "$TEST_STACK/bin" "$src/build/bin"
  (
    cd "$src"
    go_common_env go build -buildvcs=false -trimpath -o "$src/build/bin/pool" ./cmd/pool
    go_common_env go build -buildvcs=false -trimpath -o "$src/build/bin/dashboard-api" ./cmd/dashboard-api
    cp -f "$src/build/bin/pool" "$TEST_STACK/bin/mining-pool"
    cp -f "$src/build/bin/dashboard-api" "$TEST_STACK/bin/dashboard-api"
  )
}

build_dashboard() {
  local src="$REPOS_DIR/dashboard2"
  need_file "$src/main.go"
  mkdir -p "$TEST_STACK/bin"
  (
    cd "$src"
    env GOOS="$GOOS" GOARCH="$GOARCH" CGO_ENABLED=0 GOFLAGS=-buildvcs=false \
      go build -trimpath -o "$TEST_STACK/bin/dashboard" .
  )
}

sync_collector() {
  local src="$REPOS_DIR/collector"
  local collector_entry
  mkdir -p "$TEST_STACK/collector"
  rsync -a --delete \
    --exclude='.git/' \
    --exclude='.github/' \
    --exclude='.pytest_cache/' \
    --exclude='__pycache__/' \
    --exclude='runtime/' \
    --exclude='runtime-*/' \
    --exclude='data/' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    "$src/" "$TEST_STACK/collector/"
  collector_entry="$(find "$TEST_STACK/collector" -type f -name collector.py -not -path '*/.git/*' -print -quit)"
  [[ -n "$collector_entry" ]] || { echo "missing file: $src/**/collector.py" >&2; exit 1; }
  if [[ "$collector_entry" != "$TEST_STACK/collector/collector.py" ]]; then
    cp "$collector_entry" "$TEST_STACK/collector/collector.py"
  fi
}

verify_outputs() {
  need_file "$TEST_STACK/bin/blockdag-node"
  need_file "$TEST_STACK/bin/nodeworker"
  need_file "$TEST_STACK/bin/mining-pool"
  need_file "$TEST_STACK/bin/dashboard-api"
  need_file "$TEST_STACK/bin/dashboard"
  chmod +x "$TEST_STACK/bin/blockdag-node" \
           "$TEST_STACK/bin/nodeworker" \
           "$TEST_STACK/bin/mining-pool" \
           "$TEST_STACK/bin/dashboard-api" \
           "$TEST_STACK/bin/dashboard"
  if [[ -x "$ROOT/scripts/verify-release-architecture.py" ]]; then
    "$ROOT/scripts/verify-release-architecture.py" --target "$TARGET" \
      "$TEST_STACK/bin/blockdag-node" \
      "$TEST_STACK/bin/nodeworker" \
      "$TEST_STACK/bin/mining-pool" \
      "$TEST_STACK/bin/dashboard-api" \
      "$TEST_STACK/bin/dashboard"
  fi
  sha256sum "$TEST_STACK/bin/blockdag-node" \
            "$TEST_STACK/bin/nodeworker" \
            "$TEST_STACK/bin/mining-pool" \
            "$TEST_STACK/bin/dashboard-api" \
            "$TEST_STACK/bin/dashboard" > "$TEST_STACK/checksums.txt"
}

run_component() {
  case "$1" in
    node|blockdag-node|nodeworker)
      build_node
      ;;
    pool|mining-pool|dashboard-api)
      build_pool
      ;;
    dashboard)
      build_dashboard
      ;;
    collector)
      sync_collector
      ;;
    all)
      build_node
      build_pool
      build_dashboard
      sync_collector
      ;;
    -h|--help|help)
      usage
      exit 0
      ;;
    *)
      echo "unknown component: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
}

need_dir "$TEST_STACK"
mkdir -p "$TEST_STACK/bin"

if [[ $# -eq 0 ]]; then
  usage
  exit 2
fi

for component in "$@"; do
  run_component "$component"
done

verify_outputs
echo "updated test-stack artifacts in $TEST_STACK"
