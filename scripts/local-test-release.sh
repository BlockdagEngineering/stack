#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="${1:-pool-vlocal-test}"
TARGET="${TARGET:-linux-amd64}"
GOARCH="${GOARCH:-amd64}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
PACKAGE_NAME="${PACKAGE_NAME:-pool-stack-docker}"
PAYLOAD_SOURCE="${PAYLOAD_SOURCE:-/home/ben/repos/test-stack}"
OUT_DIR="${OUT_DIR:-$ROOT/release-downloads}"

case "$TARGET" in
  linux-amd64|linux-arm64) ;;
  *) echo "unsupported TARGET=$TARGET" >&2; exit 1 ;;
esac

need_file() {
  local file="$1"
  [[ -f "$file" ]] || { echo "missing required file: $file" >&2; exit 1; }
}

need_dir() {
  local dir="$1"
  [[ -d "$dir" ]] || { echo "missing required directory: $dir" >&2; exit 1; }
}

git_head_or_unknown() {
  local dir="$1"
  git -C "$dir" rev-parse HEAD 2>/dev/null || printf '%s\n' unknown
}

need_file "$PAYLOAD_SOURCE/bin/blockdag-node"
need_file "$PAYLOAD_SOURCE/bin/nodeworker"
need_file "$PAYLOAD_SOURCE/bin/mining-pool"
need_file "$PAYLOAD_SOURCE/bin/dashboard-api"
need_file "$PAYLOAD_SOURCE/bin/dashboard"

mkdir -p "$OUT_DIR"
WORK_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

RELEASE_ROOT="${PACKAGE_NAME}-${VERSION}-${TARGET}"
STAGE="$WORK_DIR/$RELEASE_ROOT"
OUT="$OUT_DIR/${RELEASE_ROOT}.zip"
OUT_SHA256="$OUT.sha256"

mkdir -p "$STAGE"
cp docker-compose.yml dockerfile .dockerignore .env.example node.conf.example README.md "$STAGE/"
printf '%s\n' "$VERSION" > "$STAGE/version.txt"
cat > "$STAGE/release-payload.env" <<EOF
BDAG_RELEASE_VERSION=${VERSION}
BDAG_RELEASE_PAYLOAD_TARGET=${TARGET}
BDAG_RELEASE_PAYLOAD_ARCH=${GOARCH}
DOCKER_PLATFORM=${DOCKER_PLATFORM}
STACK_SOURCE_COMMIT=${STACK_SOURCE_COMMIT:-$(git_head_or_unknown "$ROOT")}
CORECHAIN_SOURCE_COMMIT=${CORECHAIN_SOURCE_COMMIT:-$(git_head_or_unknown "$ROOT/../blockdag-corechain")}
POOL_SOURCE_COMMIT=${POOL_SOURCE_COMMIT:-$(git_head_or_unknown "$ROOT/../pool")}
REDIS_DASH_SOURCE_COMMIT=${REDIS_DASH_SOURCE_COMMIT:-$(git_head_or_unknown "$ROOT/../redis-dash")}
STACK_RUNTIME_MANIFEST=${STACK_RUNTIME_MANIFEST:-${RELEASE_ROOT}}
EOF

cp scripts/release/install.sh scripts/release/install-node.sh scripts/release/install.ps1 scripts/release/install.cmd "$STAGE/"
cp -a scripts/release/installers "$STAGE/installers"
cp -a sql docker tools "$STAGE/"

mkdir -p "$STAGE/bin"
cp "$PAYLOAD_SOURCE/bin/blockdag-node" \
   "$PAYLOAD_SOURCE/bin/nodeworker" \
   "$PAYLOAD_SOURCE/bin/mining-pool" \
   "$PAYLOAD_SOURCE/bin/dashboard-api" \
   "$PAYLOAD_SOURCE/bin/dashboard" \
   "$STAGE/bin/"
chmod +x "$STAGE/bin/blockdag-node" "$STAGE/bin/nodeworker" "$STAGE/bin/mining-pool" "$STAGE/bin/dashboard-api" "$STAGE/bin/dashboard"
(
  cd "$STAGE"
  sha256sum bin/blockdag-node bin/nodeworker bin/mining-pool bin/dashboard-api bin/dashboard > checksums.txt
)

rsync -a --exclude='__pycache__/' --exclude='*.pyc' scripts "$STAGE/"
rsync -a --exclude='runtime/' --exclude='runtime-*/' --exclude='__pycache__/' --exclude='*.pyc' ops "$STAGE/"
chmod +x "$STAGE/install.sh" "$STAGE/install-node.sh" "$STAGE/installers/"*.sh

python3 scripts/check-release-archive.py "$STAGE"

rm -f "$OUT" "$OUT_SHA256"
(cd "$WORK_DIR" && zip -qr "$OUT" "$RELEASE_ROOT")
python3 scripts/check-release-archive.py "$OUT"
sha256sum "$OUT" > "$OUT_SHA256"

echo "$OUT"
echo "$OUT_SHA256"
