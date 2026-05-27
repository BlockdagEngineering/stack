#!/bin/sh
set -eu

log() {
  printf '[%s] dashboard-entrypoint: %s\n' "$(date -Is)" "$*" >&2
}

export BDAG_PROJECT_ROOT="${BDAG_PROJECT_ROOT:-/workspace}"
export BDAG_RUNTIME_DIR="${BDAG_RUNTIME_DIR:-/var/lib/bdag-dashboard/runtime}"
export BDAG_POOL_ENV_FILE="${BDAG_POOL_ENV_FILE:-$BDAG_PROJECT_ROOT/.env}"
export BDAG_DASHBOARD_BIND="${BDAG_DASHBOARD_BIND:-0.0.0.0}"
export BDAG_DASHBOARD_PORT="${BDAG_DASHBOARD_PORT:-9280}"
export BDAG_DASHBOARD_REQUIRE_TOKEN="${BDAG_DASHBOARD_REQUIRE_TOKEN:-auto}"

mkdir -p "$BDAG_RUNTIME_DIR"

if [ -f /opt/pool-dashboard/ops/dashboard.py ]; then
  app_dir=/opt/pool-dashboard/ops
  app=/opt/pool-dashboard/ops/dashboard.py
elif [ -f /opt/pool-dashboard/dashboard.py ]; then
  app_dir=/opt/pool-dashboard
  app=/opt/pool-dashboard/dashboard.py
else
  log "dashboard.py not found in pool-dashboard checkout"
  find /opt/pool-dashboard -maxdepth 2 -type f | sort >&2 || true
  exit 1
fi

if [ ! -S /var/run/docker.sock ]; then
  log "warning: /var/run/docker.sock is not mounted; status and repair actions will be limited"
fi

log "starting canonical pool-dashboard from $app"
cd "$app_dir"
exec python3 "$app"
