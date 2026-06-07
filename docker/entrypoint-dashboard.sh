#!/bin/sh
set -eu

log() {
  printf '[%s] dashboard-entrypoint: %s\n' "$(date -Is)" "$*" >&2
}

export BDAG_PROJECT_ROOT="${BDAG_PROJECT_ROOT:-/workspace}"
export BDAG_RUNTIME_DIR="${BDAG_RUNTIME_DIR:-/var/lib/bdag-dashboard/runtime}"
export BDAG_POOL_ENV_FILE="${BDAG_POOL_ENV_FILE:-$BDAG_PROJECT_ROOT/.env}"
export BDAG_DASHBOARD_BIND="${BDAG_DASHBOARD_BIND:-0.0.0.0}"
export BDAG_DASHBOARD_PORT="${BDAG_DASHBOARD_PORT:-8088}"
export BDAG_DASHBOARD_REQUIRE_TOKEN="${BDAG_DASHBOARD_REQUIRE_TOKEN:-auto}"

mkdir -p "$BDAG_RUNTIME_DIR"

if [ -f /opt/dashboard/ops/dashboard.py ]; then
  app_dir=/opt/dashboard/ops
  app=/opt/dashboard/ops/dashboard.py
elif [ -f /opt/dashboard/dashboard.py ]; then
  app_dir=/opt/dashboard
  app=/opt/dashboard/dashboard.py
else
  log "dashboard.py not found in dashboard checkout"
  find /opt/dashboard -maxdepth 2 -type f | sort >&2 || true
  exit 1
fi

if [ ! -S /var/run/docker.sock ]; then
  log "warning: /var/run/docker.sock is not mounted; status and repair actions will be limited"
fi

log "starting canonical dashboard from $app"
cd "$app_dir"
exec python3 "$app"
