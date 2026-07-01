#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT
export LOG_TO_SYSLOG=0
export LOG_LEVEL=INFO
export VQUEEN_LOGGING_LIB="$ROOT/lib/vqueen-logging.sh"

fail() {
  printf 'unit guard test failed: %s\n' "$*" >&2
  exit 1
}

cat >"$TMPDIR/logger" <<'EOF'
#!/usr/bin/env sh
printf '%s\n' "$*" >>"$LOGGER_CAPTURE"
EOF
chmod +x "$TMPDIR/logger"

PATH="$TMPDIR:$PATH" LOG_TO_SYSLOG=1 LOG_SYSLOG_TAG=unit-test LOGGER_CAPTURE="$TMPDIR/logger.capture" \
  bash -c "set -Eeuo pipefail; . '$ROOT/lib/vqueen-logging.sh'; log_info MainThread 'format probe'" \
  >"$TMPDIR/vqueen-unit-logger.out" 2>"$TMPDIR/vqueen-unit-logger.err" || fail "logger format probe failed"
[ ! -s "$TMPDIR/vqueen-unit-logger.out" ] || fail "logger wrote structured line to stdout"
grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}: INFO[[:space:]]{4} : [0-9]+ MainThread[[:space:]]{12} : format probe$' \
  "$TMPDIR/vqueen-unit-logger.err" || fail "logger format did not match required shape"
grep -q -- '-t unit-test -p user.info -- ' "$TMPDIR/logger.capture" || fail "logger did not call syslog handoff"

LOG_TO_SYSLOG=0 LOG_LEVEL=ERROR \
  bash -c "set -Eeuo pipefail; . '$ROOT/lib/vqueen-logging.sh'; log_info MainThread hidden; log_error MainThread shown" \
  >"$TMPDIR/vqueen-unit-level.out" 2>"$TMPDIR/vqueen-unit-level.err" || fail "logger level probe failed"
grep -q 'shown' "$TMPDIR/vqueen-unit-level.err" || fail "ERROR log was filtered unexpectedly"
if grep -q 'hidden' "$TMPDIR/vqueen-unit-level.err"; then
  fail "INFO log was not filtered by LOG_LEVEL=ERROR"
fi

LOG_TO_SYSLOG=0 bash -c "set -Eeuo pipefail; . '$ROOT/lib/vqueen-logging.sh'; log_emit WARN MainThread bad" \
  >"$TMPDIR/vqueen-unit-warn.out" 2>"$TMPDIR/vqueen-unit-warn.err" && fail "logger accepted legacy WARN level"

run_lib_pass() {
  local body="$1"
  bash -c "set -Eeuo pipefail; . '$ROOT/lib/vqueen-backup-lib.sh'; $body" \
    >"$TMPDIR/vqueen-unit-pass.out" 2>"$TMPDIR/vqueen-unit-pass.err" || {
      cat "$TMPDIR/vqueen-unit-pass.err" >&2
      fail "expected pass: $body"
    }
}

run_lib_fail() {
  local body="$1"
  if bash -c "set -Eeuo pipefail; . '$ROOT/lib/vqueen-backup-lib.sh'; $body" \
    >"$TMPDIR/vqueen-unit-fail.out" 2>"$TMPDIR/vqueen-unit-fail.err"; then
    fail "expected failure: $body"
  fi
}

write_config() {
  local conf="$1"
  local backup_root="$2"
  local restore_root="$3"
  local live_runtime="$4"
  local node_src="$5"
  local nodeworker_src="$6"
  local collector_src="$7"

  cat >"$conf" <<EOF
PROJECT_NAME="v657-nearhot-backup"
STACK_NAME="vqueen"
STACK_VERSION="v6.5.7"
CONSISTENCY_MODE="near-hot-live-two-pass-crash-consistent"
LIVE_COMPOSE_PROJECT="pool-stack-docker-pool-v657-linux-amd64"
LIVE_RUNTIME_DIR="$live_runtime"
LIVE_COMPOSE_FILE="\${LIVE_RUNTIME_DIR}/docker-compose.yml"
NODE_DATA_SRC="$node_src"
NODEWORKER_VOLUME="nodeworker-data"
NODEWORKER_DATA_SRC="$nodeworker_src"
COLLECTOR_RUNTIME_VOLUME="collector-runtime"
COLLECTOR_RUNTIME_SRC="$collector_src"
POSTGRES_CONTAINER="postgres"
POSTGRES_COMPOSE_SERVICE="pool-db"
POSTGRES_IMAGE_FAMILY="postgres:15-bookworm"
POSTGRES_DB="bdagpool"
POSTGRES_USER="bdag_pool"
LIVE_READ_ACCESS="controlled-sudo"
SUDO_BIN="sudo"
SUDO_FLAGS="-n"
BACKUP_ROOT="$backup_root"
DEV_RESTORE_ROOT="$restore_root"
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
POSTGRES_PASSWORD_FILE="$TMPDIR/postgres-password"
EOF
}

mkdir -p "$TMPDIR/root/child" "$TMPDIR/real" "$TMPDIR/backup/runs/good" "$TMPDIR/restore"
printf 'unit-secret\n' >"$TMPDIR/postgres-password"
ln -s "$TMPDIR/real" "$TMPDIR/link"

run_lib_pass "require_under_root '$TMPDIR/root/child' '$TMPDIR/root'"
run_lib_fail "require_under_root '$TMPDIR/rootish/child' '$TMPDIR/root'"
run_lib_fail "refuse_root_or_empty ''"
run_lib_fail "refuse_root_or_empty /"
run_lib_fail "refuse_symlink_path '$TMPDIR/link/child'"
run_lib_fail "refuse_symlink_path '$TMPDIR/link/../real'"
run_lib_fail "refuse_path_overlap '$TMPDIR/root' '$TMPDIR/root/child' 'unit overlap'"
run_lib_fail "refuse_path_overlap '$TMPDIR/root/child' '$TMPDIR/root' 'unit overlap'"

operation_lock="$TMPDIR/operation.lock"
run_lib_pass "PROJECT_STATE_DIR='$TMPDIR'; OPERATION_LOCK='$operation_lock'; export PROJECT_STATE_DIR OPERATION_LOCK; with_lock \"\$OPERATION_LOCK\" bash -c '. '''$ROOT/lib/vqueen-backup-lib.sh'''; operation_lock_already_held'"
run_lib_fail "PROJECT_STATE_DIR='$TMPDIR'; OPERATION_LOCK='$operation_lock'; VQUEEN_OPERATION_LOCK_FD=999; VQUEEN_OPERATION_LOCK_PATH='$operation_lock'; operation_lock_already_held"
run_lib_fail "PROJECT_STATE_DIR='$TMPDIR'; OPERATION_LOCK='$operation_lock'; VQUEEN_OPERATION_LOCK_FD=0; VQUEEN_OPERATION_LOCK_PATH='$operation_lock'; operation_lock_already_held"

mkdir -p "$TMPDIR/backup/runs/2026/06/25/vqueen-v6.5.7-20260625T000000Z-host-abcdef/data" \
  "$TMPDIR/backup/runs/2026/06/25/vqueen-v6.5.7-20260625T000000Z-host-abcdef/metadata" \
  "$TMPDIR/backup/runs/2026/06/25/vqueen-v6.5.7-20260625T000000Z-host-abcdef/manifests"
printf 'complete\n' >"$TMPDIR/backup/runs/2026/06/25/vqueen-v6.5.7-20260625T000000Z-host-abcdef/metadata/status.txt"
touch "$TMPDIR/backup/runs/2026/06/25/vqueen-v6.5.7-20260625T000000Z-host-abcdef/manifests/file-manifest.sha256"
run_lib_fail "BACKUP_ROOT='$TMPDIR/backup'; resolve_latest_known_good"
touch "$TMPDIR/backup/runs/2026/06/25/vqueen-v6.5.7-20260625T000000Z-host-abcdef/metadata/known-good.txt"
run_lib_pass "BACKUP_ROOT='$TMPDIR/backup'; resolve_latest_known_good"

good_conf="$TMPDIR/good.conf"
bad_conf="$TMPDIR/bad.conf"
write_config "$good_conf" "$TMPDIR/backup-ok" "$TMPDIR/restore-ok" "$TMPDIR/live" "$TMPDIR/node" "$TMPDIR/nodeworker" "$TMPDIR/collector"
write_config "$bad_conf" "$TMPDIR/live/backups" "$TMPDIR/restore-ok" "$TMPDIR/live" "$TMPDIR/node" "$TMPDIR/nodeworker" "$TMPDIR/collector"
run_lib_pass "PROJECT_ROOT='$ROOT' VQUEEN_BACKUP_CONF='$good_conf' validate_static_config"
run_lib_fail "PROJECT_ROOT='$ROOT' VQUEEN_BACKUP_CONF='$bad_conf' validate_static_config"
run_lib_fail ". '$good_conf'; RSYNC_NICE=20; validate_resource_config"
run_lib_fail ". '$good_conf'; RSYNC_IONICE_CLASS=1; validate_resource_config"
run_lib_fail ". '$good_conf'; CANDIDATE_RETENTION_DAYS=bad; validate_resource_config"
run_lib_fail ". '$good_conf'; LIVE_READ_ACCESS=controlled-sudo; SUDO_BIN=doas; SUDO_FLAGS=-n; validate_controlled_sudo_config"
run_lib_fail ". '$good_conf'; LIVE_READ_ACCESS=controlled-sudo; SUDO_BIN=sudo; SUDO_FLAGS=''; validate_controlled_sudo_config"
run_lib_fail ". '$good_conf'; POSTGRES_COMPOSE_SERVICE=''; validate_postgres_static_config"
run_lib_fail ". '$good_conf'; CONTAINER_RUNNER_IMAGE=''; validate_cycle_static_config"

mkdir -p "$TMPDIR/tools" "$TMPDIR/live" "$TMPDIR/node" "$TMPDIR/nodeworker"
touch "$TMPDIR/live/docker-compose.yml"
run_lib_pass "PROJECT_ROOT='$ROOT'; . '$good_conf'; LIVE_READ_ACCESS=direct; path_exists_dir '$TMPDIR/live'"
run_lib_fail "PROJECT_ROOT='$ROOT'; . '$good_conf'; LIVE_READ_ACCESS=direct; path_exists_dir '$TMPDIR/missing'"
ln -s "$TMPDIR/real" "$TMPDIR/symlink-backup-root"
write_config "$bad_conf" "$TMPDIR/symlink-backup-root" "$TMPDIR/restore-ok" "$TMPDIR/live" "$TMPDIR/node" "$TMPDIR/nodeworker" "$TMPDIR/collector"
run_lib_fail "PROJECT_ROOT='$ROOT' VQUEEN_BACKUP_CONF='$bad_conf' validate_static_config"

for tool in docker rsync; do
  cat >"$TMPDIR/tools/$tool" <<'EOF'
#!/usr/bin/env sh
exit 0
EOF
  chmod +x "$TMPDIR/tools/$tool"
done
cat >"$TMPDIR/tools/sudo" <<'EOF'
#!/usr/bin/env sh
if [ "${1:-}" = "-n" ]; then
  shift
fi
if [ "${1:-}" = "-l" ]; then
  printf 'User eddie may run the following commands on vqueen:\n'
  printf '    (root) NOPASSWD: /usr/bin/test -d *, /usr/bin/rsync *\n'
  exit 0
fi
exec "$@"
EOF
chmod +x "$TMPDIR/tools/sudo"

mkdir -p "$TMPDIR/broad-tools"
cat >"$TMPDIR/broad-tools/sudo" <<'EOF'
#!/usr/bin/env sh
if [ "${1:-}" = "-n" ]; then
  shift
fi
if [ "${1:-}" = "-l" ]; then
  printf 'User eddie may run the following commands on vqueen:\n'
  printf '    (ALL) NOPASSWD: /usr/bin/bash, /usr/bin/su\n'
  exit 0
fi
exec "$@"
EOF
chmod +x "$TMPDIR/broad-tools/sudo"
run_lib_pass "PROJECT_ROOT='$ROOT'; . '$good_conf'; PATH='$TMPDIR/broad-tools:$PATH'; validate_live_read_gate"
run_lib_fail "PROJECT_ROOT='$ROOT'; . '$good_conf'; run_with_live_read_access bash -c true"
run_lib_fail "PROJECT_ROOT='$ROOT'; . '$good_conf'; BACKUP_LOG_DIR='$TMPDIR/missing-log-root'; setup_log_dir \"\$BACKUP_LOG_DIR\""
"$ROOT/ops/vqueen-nearhot-rsync-wrapper.sh" bad-label "$TMPDIR/not-a-run" \
  >"$TMPDIR/vqueen-unit-wrapper-bad-label.out" 2>"$TMPDIR/vqueen-unit-wrapper-bad-label.err" && fail "wrapper accepted bad label"
"$ROOT/ops/vqueen-nearhot-rsync-wrapper.sh" chain-node /tmp/not-approved \
  >"$TMPDIR/vqueen-unit-wrapper-bad-run.out" 2>"$TMPDIR/vqueen-unit-wrapper-bad-run.err" && fail "wrapper accepted bad run dir"

VQUEEN_BACKUP_CONF="$good_conf" "$ROOT/bin/vqueen-backup.sh" --show-config --extra \
  >"$TMPDIR/vqueen-unit-argv.out" 2>"$TMPDIR/vqueen-unit-argv.err" && fail "backup accepted unknown extra argument"
VQUEEN_BACKUP_CONF="$good_conf" "$ROOT/bin/vqueen-restore-test.sh" --dry-run --backup latest --extra \
  >"$TMPDIR/vqueen-unit-restore-argv.out" 2>"$TMPDIR/vqueen-unit-restore-argv.err" && fail "restore accepted unknown extra argument"
PATH="$TMPDIR/tools:$PATH" VQUEEN_BACKUP_CONF="$good_conf" "$ROOT/bin/vqueen-restore-test.sh" --dry-run --backup latest --backup latest \
  >"$TMPDIR/vqueen-unit-restore-repeated.out" 2>"$TMPDIR/vqueen-unit-restore-repeated.err" && fail "restore accepted repeated --backup"
VQUEEN_BACKUP_CONF="$good_conf" "$ROOT/bin/vqueen-backup.sh" --backup-inner \
  >"$TMPDIR/vqueen-unit-runtime.out" 2>"$TMPDIR/vqueen-unit-runtime.err" && fail "backup inner mode was not refused"
BACKUP_INNER_APPROVED=1 VQUEEN_BACKUP_CONF="$good_conf" "$ROOT/bin/vqueen-backup.sh" --backup-inner \
  >"$TMPDIR/vqueen-unit-runtime-env.out" 2>"$TMPDIR/vqueen-unit-runtime-env.err" && fail "backup inner env bypass was not refused"
VQUEEN_BACKUP_CONF="$good_conf" "$ROOT/bin/vqueen-restore-test.sh" --restore --backup latest \
  >"$TMPDIR/vqueen-unit-restore-runtime.out" 2>"$TMPDIR/vqueen-unit-restore-runtime.err" && fail "restore runtime mode was not refused"
VQUEEN_BACKUP_CONF="$good_conf" "$ROOT/bin/vqueen-restore-test.sh" --cleanup "$TMPDIR/outside" \
  >"$TMPDIR/vqueen-unit-cleanup-outside.out" 2>"$TMPDIR/vqueen-unit-cleanup-outside.err" && fail "cleanup accepted outside restore root"

grep -q 'pg_isready -h 127.0.0.1 -p 5432' "$ROOT/bin/vqueen-restore-test.sh" || \
  fail "restore postgres readiness does not use TCP"
grep -q 'pg_restore .* -h 127.0.0.1 -p 5432' "$ROOT/bin/vqueen-restore-test.sh" || \
  fail "restore pg_restore does not use TCP"
grep -q 'createdb -h 127.0.0.1 -p 5432' "$ROOT/bin/vqueen-restore-test.sh" || \
  fail "restore createdb does not use TCP"
grep -q 'psql -h 127.0.0.1 -p 5432' "$ROOT/bin/vqueen-restore-test.sh" || \
  fail "restore psql does not use TCP"

PATH="$TMPDIR/tools:$PATH" VQUEEN_BACKUP_CONF="$good_conf" "$ROOT/bin/vqueen-backup.sh" --preflight \
  >"$TMPDIR/vqueen-unit-preflight.out" 2>"$TMPDIR/vqueen-unit-preflight.err" && fail "backup preflight accepted missing collector runtime"

mkdir -p "$TMPDIR/backup-ok/runs/2026/06/25/vqueen-v6.5.7-20260625T000000Z-host-abcdef/data" \
  "$TMPDIR/backup-ok/runs/2026/06/25/vqueen-v6.5.7-20260625T000000Z-host-abcdef/metadata" \
  "$TMPDIR/backup-ok/runs/2026/06/25/vqueen-v6.5.7-20260625T000000Z-host-abcdef/manifests" \
  "$TMPDIR/collector"
printf 'complete\n' >"$TMPDIR/backup-ok/runs/2026/06/25/vqueen-v6.5.7-20260625T000000Z-host-abcdef/metadata/status.txt"
touch "$TMPDIR/backup-ok/runs/2026/06/25/vqueen-v6.5.7-20260625T000000Z-host-abcdef/manifests/file-manifest.sha256"
PATH="$TMPDIR/tools:$PATH" RUN_ID="vqueen-v6.5.7-20260625T000001Z-host-abcdef" VQUEEN_BACKUP_CONF="$good_conf" "$ROOT/bin/vqueen-backup.sh" --backup \
  >"$TMPDIR/vqueen-unit-backup.out" 2>"$TMPDIR/vqueen-unit-backup.err" && fail "backup runtime mode was not refused without M5 token"
PATH="$TMPDIR/tools:$PATH" VQUEEN_BACKUP_CONF="$good_conf" "$ROOT/bin/vqueen-restore-test.sh" --dry-run --backup 'vqueen-v6.5.7-*' \
  >"$TMPDIR/vqueen-unit-restore-wildcard.out" 2>"$TMPDIR/vqueen-unit-restore-wildcard.err" && fail "restore accepted wildcard backup id"
PATH="$TMPDIR/tools:$PATH" VQUEEN_BACKUP_CONF="$good_conf" "$ROOT/bin/vqueen-restore-test.sh" --dry-run --backup vqueen-v6.5.7-20260625T000000Z-host-abcdef \
  >"$TMPDIR/vqueen-unit-restore-exact.out" 2>"$TMPDIR/vqueen-unit-restore-exact.err" || {
    cat "$TMPDIR/vqueen-unit-restore-exact.err" >&2
    fail "restore rejected exact backup id"
  }

printf 'unit guards passed\n'
