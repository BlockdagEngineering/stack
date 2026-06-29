#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

required=(
  "bin/vqueen-backup.sh"
  "bin/vqueen-container-backup.sh"
  "bin/vqueen-nearhot-cycle.sh"
  "bin/vqueen-restore-test.sh"
  "lib/vqueen-backup-lib.sh"
  "lib/vqueen-logging.sh"
  "ops/container/Dockerfile"
  "ops/vqueen-nearhot-manifest-wrapper.sh"
  "ops/vqueen-nearhot-restore-proof-wrapper.sh"
  "ops/vqueen-nearhot-rsync-wrapper.sh"
  "ops/vqueen-nearhot-cycle-wrapper.sh"
  "ops/systemd/vqueen-nearhot-cycle.service"
  "ops/systemd/vqueen-nearhot-cycle.timer"
  "etc/vqueen-backup.conf.example"
  "docs/M2A-SCOPE.md"
  "docs/RUNBOOK.md"
  "tests/unit-guards.sh"
  "tests/unit-cycle.sh"
  "README.md"
  ".gitignore"
)

for rel in "${required[@]}"; do
  [ -e "$ROOT/$rel" ] || {
    printf 'missing required file: %s\n' "$rel" >&2
    exit 1
  }
done

bash -n "$ROOT/bin/vqueen-backup.sh"
bash -n "$ROOT/bin/vqueen-container-backup.sh"
bash -n "$ROOT/bin/vqueen-nearhot-cycle.sh"
bash -n "$ROOT/bin/vqueen-restore-test.sh"
bash -n "$ROOT/lib/vqueen-backup-lib.sh"
bash -n "$ROOT/lib/vqueen-logging.sh"
bash -n "$ROOT/tests/unit-guards.sh"
bash -n "$ROOT/tests/unit-cycle.sh"
bash -n "$ROOT/ops/vqueen-nearhot-manifest-wrapper.sh"
bash -n "$ROOT/ops/vqueen-nearhot-restore-proof-wrapper.sh"
bash -n "$ROOT/ops/vqueen-nearhot-rsync-wrapper.sh"
bash -n "$ROOT/ops/vqueen-nearhot-cycle-wrapper.sh"

if grep -R -nE '(^|[="[:space:]])/(usr/local/sbin|usr/local/lib|etc/vqueen-backup)' "$ROOT/bin" "$ROOT/lib" "$ROOT/etc" | \
  grep -v '/usr/local/sbin/vqueen-nearhot-rsync' | \
  grep -v '/usr/local/sbin/vqueen-nearhot-manifest' | \
  grep -v '/usr/local/sbin/vqueen-nearhot-restore-proof' | \
  grep -v '/usr/local/lib/vqueen-nearhot-backup/vqueen-logging.sh'; then
  printf 'forbidden install path found\n' >&2
  exit 1
fi

grep -q 'date '\''+%Y-%m-%d %H:%M:%S'\''' "$ROOT/lib/vqueen-logging.sh" || {
  printf 'logger missing local timestamp format\n' >&2
  exit 1
}

grep -q 'logger -t "$LOG_SYSLOG_TAG" -p "$priority" -- "$line"' "$ROOT/lib/vqueen-logging.sh" || {
  printf 'logger missing syslog handoff\n' >&2
  exit 1
}

if grep -R -nE 'log_line[[:space:]]+WARN([[:space:]]|$)|log_emit[[:space:]]+WARN([[:space:]]|$)|log_warn[[:space:]]*\(|printf '\''WARN:'\''' "$ROOT/bin" "$ROOT/lib" "$ROOT/ops"; then
  printf 'legacy WARN logging found\n' >&2
  exit 1
fi

if grep -R -nE 'sudo[[:space:]].*(bash|sh)[[:space:]]+-c|run_with_live_read_access[[:space:]]+(bash|sh)' "$ROOT/bin" "$ROOT/lib"; then
  printf 'generic sudo shell dispatch found\n' >&2
  exit 1
fi

if grep -R -n '/var/run/docker.sock' "$ROOT/bin" "$ROOT/lib" "$ROOT/ops"; then
  printf 'docker socket reference found\n' >&2
  exit 1
fi

if grep -R -n 'VQUEEN_SCHEDULED_BACKUP_APPROVED' "$ROOT/bin" "$ROOT/lib" "$ROOT/ops"; then
  printf 'scheduled backup-only approval token found\n' >&2
  exit 1
fi

grep -q 'ExecStart=.*/vqueen-nearhot-cycle-wrapper.sh --scheduled-cycle' "$ROOT/ops/systemd/vqueen-nearhot-cycle.service" || {
  printf 'cycle service does not call scheduled cycle wrapper\n' >&2
  exit 1
}

if grep -R -n 'vqueen-backup.sh --backup' "$ROOT/ops/systemd"; then
  printf 'systemd contains backup-only entrypoint\n' >&2
  exit 1
fi

grep -q 'M3 read-only preflight completed' "$ROOT/README.md" || {
  printf 'README missing M3 gate wording\n' >&2
  exit 1
}

grep -q 'M4 dry-run is closed' "$ROOT/docs/M2A-SCOPE.md" || {
  printf 'M2A scope missing M4 closed wording\n' >&2
  exit 1
}

grep -q 'VQUEEN_M5_BACKUP_APPROVED' "$ROOT/bin/vqueen-backup.sh" || {
  printf 'backup script missing explicit M5 approval gate\n' >&2
  exit 1
}

grep -q 'RSYNC_WRAPPER_BIN="/usr/local/sbin/vqueen-nearhot-rsync"' "$ROOT/etc/vqueen-backup.conf.example" || {
  printf 'example config missing rsync wrapper path\n' >&2
  exit 1
}

grep -q 'MANIFEST_WRAPPER_BIN="/usr/local/sbin/vqueen-nearhot-manifest"' "$ROOT/etc/vqueen-backup.conf.example" || {
  printf 'example config missing manifest wrapper path\n' >&2
  exit 1
}

grep -q 'POSTGRES_COMPOSE_SERVICE="pool-db"' "$ROOT/etc/vqueen-backup.conf.example" || {
  printf 'example config missing live Postgres compose service label\n' >&2
  exit 1
}

if grep -q 'POSTGRES_COMPOSE_SERVICE="postgres"' "$ROOT/etc/vqueen-backup.conf.example" "$ROOT/tests/unit-guards.sh"; then
  printf 'stale Postgres compose service label found\n' >&2
  exit 1
fi

grep -q 'only intended active-config divergence' "$ROOT/README.md" || {
  printf 'README missing active-config divergence wording\n' >&2
  exit 1
}

"$ROOT/tests/unit-guards.sh"
"$ROOT/tests/unit-cycle.sh"

printf 'static smoke passed\n'
