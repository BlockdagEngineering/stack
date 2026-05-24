# Phase 2 Architecture

This phase designs a parallel observability stack for the BlockDAG mining pool. It does not change production services, restart containers, pull images, or start the new stack.

## Goals

- Run Grafana, Prometheus, Loki, Alertmanager, and exporters beside the existing dashboard/watchdog.
- Preserve the existing dashboard as the repair and control console.
- Default all browser/API exposure to localhost only.
- Keep observability storage, networks, and container names separate from mining storage and networks.
- Retain metrics and logs for 30 days where disk budget allows.
- Prefer standard exporters and configuration over custom BlockDAG code.

## Current Production Surface To Avoid

The root `docker-compose.yml` currently publishes these production ports:

| Purpose | Published port |
| --- | ---: |
| Pool stratum, primary and multi-pool profiles | 3334-3340 |
| RPC failover | 38131 |
| Node P2P | 8151, 8152 |
| Node metrics | 6061, 6062 |
| PostgreSQL | 5432 |
| Existing dashboard default from `ops/dashboard.py` | 8088 |

The observability stack must not bind those ports and must not edit the root compose file.

## Target Components

| Component | Role | Exposure |
| --- | --- | --- |
| Grafana | Dashboard UI, datasource browser, alert UI | `127.0.0.1:3001 -> 3000` |
| Prometheus | Metrics storage, scrape scheduling, alert rules | `127.0.0.1:9091 -> 9090` |
| Alertmanager | Optional notification routing for Prometheus alerts | `127.0.0.1:9094 -> 9093` |
| Loki | Log storage and query backend | `127.0.0.1:3101 -> 3100` |
| Alloy or Promtail | Log collector for Docker/container logs and selected runtime logs | internal only |
| node_exporter | Host CPU, RAM, disk, filesystem, network, thermal files where available | internal only |
| cAdvisor or Docker metrics | Container CPU, memory, restart, filesystem, network | internal only |
| postgres_exporter | PostgreSQL health and pool DB read-only metrics | internal only |
| blackbox_exporter | HTTP/TCP probes for dashboard, stratum, node metrics, RPC failover | internal only |
| json_exporter | Metrics mapped from existing read-only dashboard APIs | internal only |
| optional `bdag_exporter` | Thin read-only BlockDAG metrics cartridge for derived or stateful metrics | internal only |

Grafana, Prometheus, and Loki are the base because they give the pool durable metrics, log search, alerts, provisioned dashboards, and standard operational workflows without replacing the existing repair code. The old dashboard remains responsible for POST actions such as miner scans, configuration writes, restarts, and repair workflows.

## Image Version Policy

The scaffold uses upstream images without forking Grafana, Prometheus, Loki, Alloy, or exporters. Before long-term adoption, pin each image to a tested version or digest after a successful staged run. Keep upgrades as a deliberate maintenance task: update one image group, run offline validation, run a supervised staged check, then record the tested versions in this document or a release note.

## Compose Shape

Phase 3 should create a separate file:

```text
ops/observability/docker-compose.observability.yml
```

Recommended project name:

```text
bdag-observability
```

Recommended startup command for the later gated staged-run phase:

```bash
docker compose -p bdag-observability -f ops/observability/docker-compose.observability.yml up -d
```

This command is documented for later use only. It must not be run in Phase 2.

## Networks

Use a dedicated bridge network:

```text
bdag-observability-net
```

Do not attach production mining containers to this network. Do not attach observability services to `pool-net` by default.

Prometheus and exporters should reach host-published production read-only surfaces through `host.docker.internal` with a Linux `host-gateway` mapping in the observability compose file. This keeps the new network isolated while allowing reads from:

- `http://host.docker.internal:8088/api/status`
- `http://host.docker.internal:8088/api/earnings`
- `http://host.docker.internal:8088/api/global`
- `host.docker.internal:3334` for stratum TCP probe
- `http://host.docker.internal:6061/debug/metrics/prometheus`
- `http://host.docker.internal:6062/debug/metrics/prometheus`
- `host.docker.internal:5432` for postgres_exporter, using read-only credentials

If a future staged run proves host-gateway access is not viable on this host, the fallback is to attach only the specific observability service that needs it to the existing `pool-net`, still without changing production container definitions.

## Volumes

Use named volumes owned by the observability compose project:

| Volume | Mounted by | Purpose |
| --- | --- | --- |
| `bdag_obs_prometheus_data` | Prometheus | TSDB blocks and WAL |
| `bdag_obs_loki_data` | Loki | Chunks, index, compactor state |
| `bdag_obs_grafana_data` | Grafana | SQLite DB, plugins, local UI state |
| `bdag_obs_alertmanager_data` | Alertmanager | silences and notification state |

All configuration should remain file-based under `ops/observability/` and mounted read-only into containers.

Allowed read-only host mounts:

- Docker socket for cAdvisor or Docker metrics: `/var/run/docker.sock:/var/run/docker.sock:ro`
- Docker/container logs for Alloy or Promtail, scoped as tightly as practical and read-only
- Host filesystem mounts for node_exporter, read-only with standard path exclusions
- Selected `ops/runtime` files only if a later logs phase needs them, excluding token, environment, wallet, key, and credential files

Do not mount `data/postgres`, node datadirs, wallet files, or `.env` files into Grafana, Prometheus, or Loki.

## Retention

Retention target is 30 days.

| Store | Target | Initial cap |
| --- | --- | --- |
| Prometheus | `--storage.tsdb.retention.time=30d` | `--storage.tsdb.retention.size=15GB` |
| Loki | `retention_period: 744h` through compactor/limits | 20GB operational disk budget |
| Grafana | No time-series retention; provision dashboards from files | 1GB budget |
| Alertmanager | Keep silences only | less than 100MB |

The size caps are protective defaults. If the 30-day goal exceeds disk budget after staged-run measurement, prefer reducing high-cardinality labels and scrape frequency before lowering retention.

Suggested scrape intervals:

- 15s: dashboard API health, pool/node critical health, blackbox probes
- 30s: node_exporter, cAdvisor, postgres_exporter
- 60s: earnings/global estimates and slower derived metrics

## Resource Budget

Initial resource budget for the whole stack:

| Resource | Steady target | Hard planning cap |
| --- | ---: | ---: |
| CPU | less than 0.75 cores average | 2 cores burst |
| Memory | less than 1.5GB RSS | 2.5GB |
| Disk | 25-40GB for 30 days | 45GB before retention tuning |
| Disk I/O | background-only; no sustained contention with mining DB/node data | must be measured in staged run |

Per-service planning limits for Phase 3:

| Service | CPU guidance | Memory guidance |
| --- | ---: | ---: |
| Grafana | 0.25-0.50 CPU | 256-512MB |
| Prometheus | 0.50-1.00 CPU | 768MB-1.5GB |
| Loki | 0.25-0.75 CPU | 512MB-1GB |
| Alloy/Promtail | 0.10-0.25 CPU | 128-256MB |
| Exporters combined | 0.10-0.50 CPU | 256-512MB |
| Alertmanager | 0.05 CPU | 64-128MB |

Mining containers already have high CPU and I/O priority in root compose. The observability stack should use lower CPU shares and normal or reduced I/O weight where Compose support allows.

## Data Source Mapping

### Standard Exporters

Use standard exporters for:

- Host CPU, RAM, disk, filesystem, network, load, and thermal files: `node_exporter`
- Container CPU, memory, restarts, network, and filesystem: cAdvisor or Docker metrics
- PostgreSQL availability, locks, table size, connection count, and slow health indicators: `postgres_exporter`
- HTTP and TCP liveness: `blackbox_exporter`
- Node built-in metrics already published on host ports `6061` and `6062`: direct Prometheus scrape
- Logs from pool, nodes, dashboard/watchdog, and observability services: Alloy or Promtail into Loki

### `json_exporter` Is Sufficient For

Use `json_exporter` against read-only dashboard GET endpoints when a value is already a scalar, boolean, or array item that can be labeled directly:

- `/api/status`
  - overall status encoded as up/down/syncing gauges
  - container running/restart status from `containers`
  - pool counters: submits, valid shares, stale submits, job notifications, head changes, block submit successes/errors
  - pool ages: last submit, last valid share, last job notify, last block submit
  - pool booleans: share stall, job stall, template frozen, duplicate block storm, needs fast repair
  - node values: latest block, block lag, import age, template error count, P2P error count, child running
  - miner summary: managed, connected, tracked, ok counts
  - per-miner scalar fields: connected, configured, pool active, work percent, shares, share work, blocks found, hashrate, accepted, rejected, hardware errors, temperature if parseable as a scalar string
- `/api/earnings`
  - total credited, pending, paid, wallet balance, wallet coverage boolean
  - recent/hourly BDAG and fiat estimates
  - history sample count and existing earnings history retention days
  - per-miner estimate scalars already present in `miner_estimates`
- `/api/global`
  - latest block, average block seconds, fetched block count, unique miners, reward estimates, fetch error count

This keeps the first metrics cartridge mostly declarative and avoids duplicating dashboard parsing logic.

### Custom Read-Only `bdag_exporter` May Be Needed For

Add a custom exporter only where configuration becomes brittle or cannot preserve correct semantics:

- Deriving stable counters from log-tail windows where `/api/status` only reports recent-window counts that may reset each scrape.
- Converting non-numeric strings such as temperatures, fan speeds, BDAG decimal strings, or fiat values when `json_exporter` mapping would be fragile.
- Computing per-miner last-share age and low-work-share state with consistent labels across `/api/status` and `/api/earnings`.
- Joining miner registry identity, pool log activity, and earnings estimates into one stable label set.
- Representing exporter/API failures as explicit metrics instead of scrape crashes.
- Reading selected runtime/watchdog state files read-only to expose repair event counts and thermal guard state if those values are not exposed cleanly by dashboard JSON.

The custom exporter must be read-only. It must never call dashboard POST endpoints, never execute repair commands, never write ASIC configuration, and never require wallet/private key material.

## Dashboard Folders

Grafana provisioning should create these folders:

- Overview
- Miners
- Pool
- Nodes
- Earnings
- System
- Thermals
- Incidents

Repair actions in Grafana should be links back to the existing dashboard, not new controls.

## Alert Categories

Prometheus rules should cover:

- Miner down or not connected for more than 2 minutes
- Miner configured wrong
- Miner low or missing work share
- Pool valid share stall
- Pool job notify stall
- Stale submit increase
- Block submit errors
- Node sync drift
- Node import stale
- Node template errors
- Container restart
- Dashboard API unavailable
- PostgreSQL unavailable
- High CPU temperature
- Low disk space
- Thermal guard disabled or unknown
- Loki ingestion failure

Alerts are notify-only. Auto-repair remains in the old watchdog/control system.

## Acceptance Criteria

- `docker-compose.observability.yml` is separate from root compose.
- Default published ports bind only `127.0.0.1`.
- No default observability port conflicts with production ports.
- All mutable storage uses observability-only named volumes.
- Retention and resource budgets are configured and documented.
- Exporters prefer read-only APIs, read-only mounts, and read-only DB credentials.
- Custom code is limited to an optional read-only `bdag_exporter`.
- Rollback stops/removes only observability containers and, only when requested, observability volumes.
