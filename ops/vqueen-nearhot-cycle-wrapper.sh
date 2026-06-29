#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="/opt/blockdag/eddie-dev/v657-nearhot-backup"
cd "$PROJECT_ROOT"
exec "$PROJECT_ROOT/bin/vqueen-nearhot-cycle.sh" "$@"
