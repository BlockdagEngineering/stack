#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET_ROOT="${BDAG_LIVE_RUNTIME_ROOT:-}"
BACKUP_ROOT="${BDAG_DEPLOY_BACKUP_ROOT:-/home/jeremy/blockdag-deploy-backups}"
RESTART_SERVICES="${BDAG_DEPLOY_RESTART_SERVICES:-bdag-dashboard.service bdag-watchdog.service}"
DRY_RUN=0
MARK_RUNTIME_COMPOSE=0
ROLLBACK_DIR=""
COMPOSE_BACKUP_BEFORE_MARK=""
FILES=(
  "ops/pool_ops.py"
  "ops/dashboard.py"
  "ops/build-pi5-arm64-release.sh"
  "ops/deploy-live-runtime-update.sh"
  "ops/release-install.sh"
  "ops/tests/test_chain_rpc_resilience.py"
  "scripts/validate-pi5-restart-hardening.sh"
  ".env.example"
  "README.md"
  "release-downloads/index.html"
)

usage() {
  cat <<'USAGE'
Deploy a safe dashboard/watchdog runtime update into an installed Pi5 stack.

Usage:
  ops/deploy-live-runtime-update.sh --target DIR [options]
  ops/deploy-live-runtime-update.sh --rollback BACKUP_DIR --target DIR

Options:
  --source DIR              Source checkout. Default: parent of this ops dir.
  --target DIR              Installed runtime root to update.
  --file PATH               Add one source-relative path to the copy whitelist.
  --restart-services LIST   Space-separated user services to restart.
                            Default: bdag-dashboard.service bdag-watchdog.service
  --mark-runtime-compose    Add the generated-runtime compose marker if missing.
  --backup-root DIR         Backup parent. Default: /home/jeremy/blockdag-deploy-backups
  --dry-run                 Print planned changes without copying or restarting.
  --rollback BACKUP_DIR     Restore files from a previous backup manifest.
  -h, --help                Show this help.

The script never copies .env, asic-pool/.env, data/, ops/runtime, chain data,
or Docker images. It refuses runtime compose files with build/dockerfile entries.
USAGE
}

say() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'deploy-live-runtime-update failed: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      SOURCE_ROOT="$(cd "${2:?--source requires a directory}" && pwd)"
      shift 2
      ;;
    --target)
      TARGET_ROOT="$(cd "${2:?--target requires a directory}" && pwd)"
      shift 2
      ;;
    --file)
      FILES+=("${2:?--file requires a source-relative path}")
      shift 2
      ;;
    --restart-services)
      RESTART_SERVICES="${2:-}"
      shift 2
      ;;
    --mark-runtime-compose)
      MARK_RUNTIME_COMPOSE=1
      shift
      ;;
    --backup-root)
      BACKUP_ROOT="${2:?--backup-root requires a directory}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --rollback)
      ROLLBACK_DIR="$(cd "${2:?--rollback requires a backup directory}" && pwd)"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

[[ -n "$TARGET_ROOT" ]] || die "--target DIR is required"
[[ -d "$SOURCE_ROOT" ]] || die "source root not found: $SOURCE_ROOT"
[[ -d "$TARGET_ROOT" ]] || die "target root not found: $TARGET_ROOT"

normalize_file() {
  local rel="$1"
  [[ -n "$rel" ]] || die "empty relative path in whitelist"
  [[ "$rel" != /* ]] || die "whitelist path must be relative: $rel"
  [[ "$rel" != *".."* ]] || die "whitelist path must not contain '..': $rel"
  printf '%s\n' "$rel"
}

runtime_compose_guard() {
  local compose="$TARGET_ROOT/docker-compose.yml"
  [[ -f "$compose" ]] || die "missing target docker-compose.yml"
  if grep -Eq '^[[:space:]]*(build|dockerfile):' "$compose"; then
    die "target docker-compose.yml contains build/dockerfile entries; refusing dev compose"
  fi
  if ! grep -q '^# BDAG_GENERATED_PI5_RUNTIME_COMPOSE=1$' "$compose"; then
    if [[ "$MARK_RUNTIME_COMPOSE" -ne 1 ]]; then
      die "target compose lacks BDAG_GENERATED_PI5_RUNTIME_COMPOSE=1; rerun with --mark-runtime-compose after confirming this is the generated runtime compose"
    fi
    if [[ "$DRY_RUN" -eq 1 ]]; then
      say "Would add generated-runtime compose marker to $compose"
    else
      local tmp
      tmp="$(mktemp)"
      COMPOSE_BACKUP_BEFORE_MARK="$(mktemp)"
      cp -a "$compose" "$COMPOSE_BACKUP_BEFORE_MARK"
      {
        printf '# BDAG_GENERATED_PI5_RUNTIME_COMPOSE=1\n'
        printf '# Generated Pi5 runtime compose. Do not replace with the source/dev compose file.\n'
        cat "$compose"
      } > "$tmp"
      mv "$tmp" "$compose"
    fi
  fi
}

run_source_validation() {
  say "Validating source checkout"
  (cd "$SOURCE_ROOT" && PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q ops scripts)
  (cd "$SOURCE_ROOT" && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s ops/tests -p 'test_*.py')
  (cd "$SOURCE_ROOT" && python3 scripts/check-doc-consistency.py)
  find "$SOURCE_ROOT/ops" "$SOURCE_ROOT/scripts" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  find "$SOURCE_ROOT/ops" "$SOURCE_ROOT/scripts" -name '*.pyc' -delete 2>/dev/null || true
  (cd "$SOURCE_ROOT" && bash scripts/validate-pi5-restart-hardening.sh --mode source .)
}

run_target_validation() {
  say "Validating live runtime target"
  bash "$TARGET_ROOT/scripts/validate-pi5-restart-hardening.sh" --mode live-runtime "$TARGET_ROOT"
}

rollback_from_backup() {
  local manifest="$ROLLBACK_DIR/manifest.tsv"
  [[ -f "$manifest" ]] || die "rollback manifest not found: $manifest"
  say "Rolling back from $ROLLBACK_DIR"
  while IFS=$'\t' read -r rel state; do
    [[ -n "$rel" ]] || continue
    case "$state" in
      existing)
        mkdir -p "$TARGET_ROOT/$(dirname "$rel")"
        cp -a "$ROLLBACK_DIR/files/$rel" "$TARGET_ROOT/$rel"
        ;;
      absent)
        rm -f "$TARGET_ROOT/$rel"
        ;;
      *)
        die "invalid rollback state for $rel: $state"
        ;;
    esac
  done < "$manifest"
  run_target_validation
  say "Rollback complete"
}

if [[ -n "$ROLLBACK_DIR" ]]; then
  rollback_from_backup
  exit 0
fi

runtime_compose_guard
run_source_validation

stamp="$(date +%Y%m%d-%H%M%S)"
commit="$(git -C "$SOURCE_ROOT" rev-parse --short=12 HEAD 2>/dev/null || printf 'nogit')"
backup_dir="$BACKUP_ROOT/live-runtime-update-${commit}-${stamp}"

say "Preparing backup: $backup_dir"
if [[ "$DRY_RUN" -eq 0 ]]; then
  mkdir -p "$backup_dir/files"
fi

if [[ "$DRY_RUN" -eq 0 && -n "$COMPOSE_BACKUP_BEFORE_MARK" ]]; then
  mkdir -p "$backup_dir/files"
  cp -a "$COMPOSE_BACKUP_BEFORE_MARK" "$backup_dir/files/docker-compose.yml"
  printf '%s\texisting\n' "docker-compose.yml" >> "$backup_dir/manifest.tsv"
fi

for raw_rel in "${FILES[@]}"; do
  rel="$(normalize_file "$raw_rel")"
  src="$SOURCE_ROOT/$rel"
  dst="$TARGET_ROOT/$rel"
  [[ -f "$src" ]] || die "source file missing: $rel"
  if [[ "$rel" == ".env" || "$rel" == "asic-pool/.env" || "$rel" == data/* || "$rel" == ops/runtime* || "$rel" == chain-data/* ]]; then
    die "refusing unsafe live-runtime file path: $rel"
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf 'would copy %s -> %s\n' "$src" "$dst"
    continue
  fi
  if [[ -e "$dst" ]]; then
    mkdir -p "$backup_dir/files/$(dirname "$rel")"
    cp -a "$dst" "$backup_dir/files/$rel"
    printf '%s\texisting\n' "$rel" >> "$backup_dir/manifest.tsv"
  else
    printf '%s\tabsent\n' "$rel" >> "$backup_dir/manifest.tsv"
  fi
  mkdir -p "$(dirname "$dst")"
  cp -a "$src" "$dst"
done

if [[ "$DRY_RUN" -eq 1 ]]; then
  say "Dry run complete"
  exit 0
fi

if ! run_target_validation; then
  warn "Target validation failed; rolling back copied files"
  ROLLBACK_DIR="$backup_dir"
  rollback_from_backup
  exit 1
fi

if [[ -n "$RESTART_SERVICES" ]]; then
  say "Restarting user services: $RESTART_SERVICES"
  if ! systemctl --user restart $RESTART_SERVICES; then
    warn "Service restart failed; rolling back copied files"
    ROLLBACK_DIR="$backup_dir"
    rollback_from_backup
    exit 1
  fi
  systemctl --user is-active $RESTART_SERVICES
fi

say "Live runtime update complete"
printf 'Backup: %s\n' "$backup_dir"
