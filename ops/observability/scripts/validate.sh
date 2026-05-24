#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== Python syntax =="
python3 -m py_compile \
  exporters/bdag_exporter/bdag_exporter.py \
  scripts/generate_dashboards.py

echo "== Generate dashboards =="
python3 scripts/generate_dashboards.py

echo "== JSON dashboards =="
python3 - <<'PY'
import json
from pathlib import Path

for path in sorted(Path("grafana/dashboards").glob("*.json")):
    with path.open(encoding="utf-8") as handle:
        json.load(handle)
    print(f"ok {path}")
PY

echo "== YAML files =="
python3 - <<'PY'
from pathlib import Path
try:
    import yaml
except ModuleNotFoundError:
    print("skip: PyYAML is not installed; YAML parsing not available")
    raise SystemExit(0)

for path in sorted(Path(".").rglob("*.yml")) + sorted(Path(".").rglob("*.yaml")):
    with path.open(encoding="utf-8") as handle:
        yaml.safe_load(handle)
    print(f"ok {path}")
PY

echo "== Required SRE alerts =="
python3 - <<'PY'
from pathlib import Path

alerts = Path("prometheus/alerts.yml").read_text(encoding="utf-8")
required = [
    "BDAGDashboardAPIUnavailable",
    "BDAGStatusContractStale",
    "BDAGMiningDisabledWithMiners",
    "BDAGP2PGuardUnavailable",
    "BDAGDiskSpaceLow",
    "BDAGPoolValidShareStall",
    "BDAGNodeSyncDrift",
    "BDAGMinerDown",
    "BDAGContainerRestarted",
]
missing = [name for name in required if name not in alerts]
if missing:
    raise SystemExit(f"missing required alerts: {', '.join(missing)}")
print("ok required SRE alerts")
PY

echo "== Exporter fixture tests =="
python3 -m unittest discover -s tests

echo "== Docker compose render =="
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  docker compose -f docker-compose.observability.yml config >/tmp/bdag-observability-compose-render.yml
  echo "ok docker compose config"
else
  echo "skip: docker compose plugin is not available"
fi

echo "== Prometheus validation =="
if command -v promtool >/dev/null 2>&1; then
  promtool check config prometheus/prometheus.yml
  promtool check rules prometheus/alerts.yml
elif command -v docker >/dev/null 2>&1; then
  docker run --rm --entrypoint promtool -v "$ROOT/prometheus:/etc/prometheus:ro" prom/prometheus:latest check config /etc/prometheus/prometheus.yml
  docker run --rm --entrypoint promtool -v "$ROOT/prometheus:/etc/prometheus:ro" prom/prometheus:latest check rules /etc/prometheus/alerts.yml
else
  echo "skip: promtool is not installed"
fi

echo "== Loki and Alloy validation =="
if command -v loki >/dev/null 2>&1; then
  loki -config.file=loki/loki.yml -verify-config
elif command -v docker >/dev/null 2>&1; then
  docker run --rm -v "$ROOT/loki:/etc/loki:ro" grafana/loki:latest -config.file=/etc/loki/loki.yml -verify-config
else
  echo "skip: loki binary is not installed"
fi
if command -v alloy >/dev/null 2>&1; then
  alloy validate alloy/config.alloy
elif command -v docker >/dev/null 2>&1; then
  docker run --rm -v "$ROOT/alloy:/etc/alloy:ro" grafana/alloy:latest validate /etc/alloy/config.alloy
else
  echo "skip: alloy binary is not installed"
fi

echo "validation complete"
