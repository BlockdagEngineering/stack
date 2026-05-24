# Phase 10 Staged Run Report

Date: 2026-05-05

Scope: Start and verify the parallel observability stack without restarting mining services or altering ASIC configuration.

## Timeline

| Event | Time |
| --- | --- |
| Baseline captured | 2026-05-05T07:17:52+02:00 |
| Observability stack started | 2026-05-05T05:21:58Z |
| Final verification snapshot | 2026-05-05T07:33:24+02:00 |

## Commands Used

Validation:

```bash
ops/observability/scripts/validate.sh
```

Start:

```bash
docker compose --env-file ops/observability/.env -f ops/observability/docker-compose.observability.yml up -d
```

Targeted observability-only reloads after tuning:

```bash
docker compose --env-file ops/observability/.env -f ops/observability/docker-compose.observability.yml up -d blackbox-exporter
docker restart bdag-alloy
docker compose --env-file ops/observability/.env -f ops/observability/docker-compose.observability.yml up -d alloy cadvisor
docker restart bdag-prometheus
```

No mining containers were restarted.

## Baseline

Old dashboard status before start:

```json
{
  "overall": "ok",
  "connected": 7,
  "ok": 7,
  "pool": 7,
  "share_age": 0,
  "job_age": 0
}
```

Production containers before start:

- `asic-pool` running
- `rpc-failover` running
- `pool-db` running
- `bdag-miner-node-1` running
- `bdag-miner-node-2` running

## Validation

`scripts/validate.sh` now performs:

- Python syntax checks
- dashboard generation
- Grafana dashboard JSON parsing
- YAML parsing
- exporter fixture tests
- `docker compose config`
- Prometheus config and rule validation through `prom/prometheus:latest`
- Loki config validation through `grafana/loki:latest`
- Alloy config validation through `grafana/alloy:latest`

Result: pass.

## Grafana

Grafana health:

```json
{
  "database": "ok",
  "version": "13.0.1"
}
```

Provisioned dashboards:

- BDAG Overview
- BDAG Miners
- BDAG Pool
- BDAG Nodes
- BDAG Earnings
- BDAG Host And Containers
- BDAG Thermals
- BDAG Logs And Incidents

URL:

```text
http://127.0.0.1:3001
```

## Prometheus Targets

Final target state:

| Job | Instance | State |
| --- | --- | --- |
| `alertmanager` | `alertmanager:9093` | up |
| `bdag-exporter` | `bdag-exporter:9108` | up |
| `bdag-native-node` | `host.docker.internal:6061` | up |
| `bdag-native-node` | `host.docker.internal:6062` | up |
| `cadvisor` | `cadvisor:8080` | up |
| `loki` | `loki:3100` | up |
| `node-exporter` | `node-exporter:9100` | up |
| `old-dashboard-http` | `http://host.docker.internal:8088/api/status` | up |
| `pool-stratum-tcp` | `host.docker.internal:3334` | up |
| `postgres-exporter` | `postgres-exporter:9187` | up |
| `prometheus` | `localhost:9090` | up |

## Loki

Loki health:

```text
ready
```

Observed labels:

- `container`
- `filename`
- `job`
- `level`
- `service_name`
- `source`

Log ingestion was tuned during staged run:

- Docker INFO/DEBUG lines are dropped.
- Runtime ingestion is limited to watchdog, thermal guard, hourly chain snapshot, and efficiency events.

## Mining Impact

Old dashboard status after start and tuning:

```json
{
  "overall": "ok",
  "connected": 7,
  "ok": 7,
  "pool": 7,
  "share_age": 1,
  "job_age": 2,
  "sync": 100.0
}
```

All seven ASICs remained connected. The production containers remained running. No ASIC configuration was changed.

## Resource Snapshot

Final observability container snapshot:

| Container | CPU | Memory |
| --- | ---: | ---: |
| `bdag-grafana` | 0.60% | 120.2MiB / 384MiB |
| `bdag-prometheus` | 0.07% | 75.5MiB / 768MiB |
| `bdag-loki` | 1.02% | 126.4MiB / 384MiB |
| `bdag-alloy` | 4.88% | 84.37MiB / 192MiB |
| `bdag-exporter` | 1.41% | 32.7MiB / 96MiB |
| `bdag-alertmanager` | 0.07% | 11.53MiB / 96MiB |
| `bdag-node-exporter` | 0.00% | 8.23MiB / 64MiB |
| `bdag-blackbox-exporter` | 0.00% | 10.5MiB / 64MiB |
| `bdag-postgres-exporter` | 0.00% | 7.59MiB / 96MiB |
| `bdag-cadvisor` | 0.14% | 13.95MiB / 192MiB |

The expensive parts were tuned during the run:

- cAdvisor now collects only CPU and memory metrics with slower housekeeping.
- cAdvisor scrape interval is 60 seconds.
- Alloy is capped at 0.05 CPU and filters high-volume logs.

## Current Alerts

At final check, one alert was pending:

- `BDAGBlockSubmitErrors` warning: recent block submit errors.

This reflects current pool/node data from the old dashboard, not an observability startup failure. At the same time the old dashboard still reported `overall: ok`, 7/7 connected, and fresh share/job activity.

## Rollback

Rollback command:

```bash
docker compose --env-file ops/observability/.env -f ops/observability/docker-compose.observability.yml down
```

This stops only the observability project. To remove observability data too, add `-v` only after deliberate approval.

## Result

Phase 10 staged run passed with one operational watch item: recent block submit errors should be observed in the new dashboard and correlated with the existing repair dashboard over the next 24 hours.
