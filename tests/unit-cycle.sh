#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

fail() {
  printf 'cycle unit test failed: %s\n' "$*" >&2
  exit 1
}

conf="$TMPDIR/vqueen-backup.conf"
mkdir -p "$TMPDIR/live" "$TMPDIR/node" "$TMPDIR/nodeworker" "$TMPDIR/collector" \
  "$TMPDIR/backup" "$TMPDIR/restore" "$TMPDIR/logs/backup" "$TMPDIR/logs/restore" "$TMPDIR/secrets"
touch "$TMPDIR/live/docker-compose.yml"
printf 'secret-placeholder\n' >"$TMPDIR/secrets/postgres-password"

cat >"$conf" <<EOF
PROJECT_NAME="v657-nearhot-backup"
STACK_NAME="vqueen"
STACK_VERSION="v6.5.7"
LIVE_COMPOSE_PROJECT="pool-stack-docker-pool-v657-linux-amd64"
LIVE_RUNTIME_DIR="$TMPDIR/live"
LIVE_COMPOSE_FILE="\${LIVE_RUNTIME_DIR}/docker-compose.yml"
NODE_DATA_SRC="$TMPDIR/node"
NODEWORKER_DATA_SRC="$TMPDIR/nodeworker"
COLLECTOR_RUNTIME_SRC="$TMPDIR/collector"
POSTGRES_CONTAINER="postgres"
POSTGRES_COMPOSE_SERVICE="pool-db"
POSTGRES_IMAGE_FAMILY="postgres:15-bookworm"
POSTGRES_DB="bdagpool"
POSTGRES_USER="bdag_pool"
LIVE_READ_ACCESS="direct"
SUDO_BIN="sudo"
SUDO_FLAGS="-n"
BACKUP_ROOT="$TMPDIR/backup"
DEV_RESTORE_ROOT="$TMPDIR/restore"
BACKUP_LOG_DIR="$TMPDIR/logs/backup"
RESTORE_LOG_DIR="$TMPDIR/logs/restore"
PROJECT_STATE_DIR="\${PROJECT_ROOT}/state"
OPERATION_LOCK="\${PROJECT_STATE_DIR}/vqueen-v657-nearhot-operation.lock"
BACKUP_LOCK="\${PROJECT_STATE_DIR}/vqueen-v657-nearhot-backup.lock"
RESTORE_LOCK="\${PROJECT_STATE_DIR}/vqueen-v657-restore-test.lock"
CYCLE_LOCK="\${PROJECT_STATE_DIR}/vqueen-v657-nearhot-cycle.lock"
MIN_FREE_BYTES=1
RSYNC_NICE="15"
RSYNC_IONICE_CLASS="2"
RSYNC_IONICE_LEVEL="7"
RSYNC_BWLIMIT_KB=""
LOG_LEVEL="INFO"
LOG_TO_SYSLOG="0"
LOG_SYSLOG_TAG="vqueen-nearhot-backup"
KEEP_KNOWN_GOOD="3"
CANDIDATE_RETENTION_DAYS="14"
FAILED_RETENTION_DAYS="14"
RESTORE_COMPOSE_PROJECT_PREFIX="vqueen-v657-restore"
RESTORE_RPC_HOST="127.0.0.1"
RESTORE_RPC_PORT="18657"
RESTORE_POSTGRES_PORT="15432"
RSYNC_WRAPPER_BIN="/usr/local/sbin/vqueen-nearhot-rsync"
MANIFEST_WRAPPER_BIN="/usr/local/sbin/vqueen-nearhot-manifest"
RESTORE_WRAPPER_BIN="/usr/local/sbin/vqueen-nearhot-restore-proof"
CONTAINER_RUNNER_IMAGE="vqueen-nearhot-runner:test"
CONTAINER_RUNNER_NETWORK="\${LIVE_COMPOSE_PROJECT}_default"
POSTGRES_PASSWORD_FILE="$TMPDIR/secrets/postgres-password"
EOF

VQUEEN_BACKUP_CONF="$conf" "$ROOT/bin/vqueen-nearhot-cycle.sh" --show-plan \
  >"$TMPDIR/show-plan.out" 2>"$TMPDIR/show-plan.err" || {
    cat "$TMPDIR/show-plan.err" >&2
    fail "show-plan failed"
  }
grep -q 'PIPELINE=preflight,container-backup,manifest-verify,restore-proof,verify-only,mark-known-good' "$TMPDIR/show-plan.out" || \
  fail "show-plan missing known-good pipeline"
grep -q "OPERATION_LOCK=$ROOT/state/vqueen-v657-nearhot-operation.lock" "$TMPDIR/show-plan.out" || \
  fail "show-plan missing shared operation lock"

grep -q 'host_backup_path_from_container_report()' "$ROOT/bin/vqueen-nearhot-cycle.sh" || \
  fail "missing container BACKUP_RUN path translator"
grep -Fq '/backup/*) printf' "$ROOT/bin/vqueen-nearhot-cycle.sh" || \
  fail "translator does not handle container /backup paths"
grep -q 'host_run_path="$(host_backup_path_from_container_report "$run_path")"' "$ROOT/bin/vqueen-nearhot-cycle.sh" || \
  fail "extract_backup_run does not translate container path before validation"
grep -q 'restore proof command failed' "$ROOT/bin/vqueen-nearhot-cycle.sh" || \
  fail "restore proof command failure is not explicit"
grep -q 'operation_lock_already_held' "$ROOT/bin/vqueen-restore-test.sh" || \
  fail "restore runtime does not accept verified inherited operation lock"
if grep -R -nE -- '--restore-inner|--skip-lock' "$ROOT/bin" "$ROOT/docs" "$ROOT/ops"; then
  fail "public restore lock bypass found"
fi

VQUEEN_BACKUP_CONF="$conf" "$ROOT/bin/vqueen-nearhot-cycle.sh" --scheduled-cycle \
  >"$TMPDIR/no-token.out" 2>"$TMPDIR/no-token.err" && fail "scheduled cycle ran without token"

if grep -R -n '/var/run/docker.sock' "$ROOT/bin/vqueen-nearhot-cycle.sh" "$ROOT/ops/container" "$ROOT/ops/systemd"; then
  fail "docker socket reference found in cycle implementation"
fi

grep -q 'ExecStart=.*/vqueen-nearhot-cycle-wrapper.sh --scheduled-cycle' "$ROOT/ops/systemd/vqueen-nearhot-cycle.service" || \
  fail "cycle service does not call scheduled cycle wrapper"
if grep -R -n 'vqueen-backup.sh --backup' "$ROOT/ops/systemd"; then
  fail "systemd still contains backup-only entrypoint"
fi
if grep -R -n 'VQUEEN_SCHEDULED_BACKUP_APPROVED' "$ROOT/bin" "$ROOT/lib" "$ROOT/ops"; then
  fail "scheduled backup-only approval token still exists"
fi

printf 'cycle unit tests passed\n'
