# Rollback Plan

Rollback must affect only the parallel observability stack. The mining containers, root compose file, existing dashboard, watchdog, ASIC configuration, wallet material, and production data directories must be left untouched.

## Rollback Scope

In scope:

- Stop observability containers.
- Remove observability containers and network.
- Optionally remove observability-only named volumes after confirming no investigation data is needed.
- Leave generated repository config files in place unless a separate cleanup task asks to remove them.

Out of scope:

- Restarting Docker.
- Restarting `asic-pool`, node, RPC failover, PostgreSQL, dashboard, watchdog, or ASIC services.
- Editing root `docker-compose.yml`.
- Deleting `data/`, `data-restore/`, `asic-pool/.env`, wallet files, or runtime production state.

## Normal Stop

Use this in a future staged-run or production-adoption phase when the observability stack should be stopped but data should be preserved:

```bash
docker compose -p bdag-observability -f ops/observability/docker-compose.observability.yml down
```

Expected result:

- Grafana, Prometheus, Loki, Alertmanager, and exporters stop.
- `bdag-observability-net` is removed if no containers still use it.
- Observability named volumes remain.
- Mining services continue running.

## Full Observability Data Removal

Use only when explicitly approved and when no logs/metrics are needed for incident review:

```bash
docker compose -p bdag-observability -f ops/observability/docker-compose.observability.yml down -v
```

Expected result:

- Same as normal stop.
- Observability named volumes are removed.
- Production volumes and bind mounts are not removed.

Do not run broad Docker prune commands for this project.

## Post-Rollback Checks

After rollback, verify production services without changing them:

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

Check that these production containers are still running if they existed before rollback:

- `asic-pool`
- `rpc-failover`
- `bdag-miner-node-1`
- `bdag-miner-node-2`
- `pool-db`

Check that the existing dashboard is still reachable on its configured address, normally:

```bash
curl -fsS http://127.0.0.1:8088/api/status >/dev/null
```

These checks are read-only. If a production service is down, rollback did not authorize repairing it; follow the existing production repair process.

## Failure Cases

| Failure | Response |
| --- | --- |
| Grafana unavailable | Stop observability stack; old dashboard remains control surface |
| Prometheus high CPU or memory | Stop observability stack; preserve volumes for inspection unless disk pressure requires removal |
| Loki grows too fast | Stop observability stack; reduce labels/retention before next staged run |
| Exporter overloads dashboard API | Stop observability stack; increase scrape interval or remove expensive scrape target |
| Docker socket read causes concern | Stop stack; remove cAdvisor/Docker socket mounts before next run |
| Port conflict | Stop stack; change only observability port mappings |
| Accidental LAN exposure detected | Stop stack immediately; restore localhost-only bindings before restart |

## Rollback Acceptance Criteria

- Rollback command targets only `-p bdag-observability` and `docker-compose.observability.yml`.
- No root compose services are stopped, restarted, or recreated.
- No production volumes are removed.
- Old dashboard/watchdog remain the fallback repair path.
- Any follow-up fix is made in observability files before another staged run.
