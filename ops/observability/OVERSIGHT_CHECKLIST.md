# Oversight Checklist

This checklist is the project control surface for the parallel observability dashboard. The lead Codex agent should update it as phases complete.

## Phase Status

- [x] Phase 0: Charter and workflow created
- [x] Phase 1: Discovery complete
- [x] Phase 2: Architecture complete
- [x] Phase 3: Scaffold complete
- [x] Phase 4: Metrics cartridge complete
- [x] Phase 5: Logs pipeline complete
- [x] Phase 6: Grafana dashboards complete
- [x] Phase 7: Alerts complete
- [x] Phase 8: Offline validation complete
- [x] Phase 9: Operations review complete
- [x] Phase 10: Staged run complete
- [ ] Phase 11: Parity/adoption complete

## Safety Gates

- [x] Existing dashboard remains the production repair console
- [x] New project write scope is isolated under `ops/observability/`
- [x] Root `docker-compose.yml` unchanged for observability work
- [x] Existing `ops/dashboard.py` unchanged for observability work
- [x] Existing `ops/watchdog.py` unchanged for observability work
- [x] Existing `ops/pool_ops.py` unchanged unless a deliberate read-only exporter refactor is approved
- [x] ASIC endpoints/configuration unchanged
- [x] Wallet/private key material untouched
- [x] Runtime password/token files excluded from log ingestion
- [x] New services default to localhost-only bindings
- [x] No production ports conflict with new stack
- [x] Rollback stops only observability containers

## Data Source Coverage

- [x] `/api/status` mapped
- [x] `/api/earnings` mapped
- [x] `/api/global` mapped
- [x] Docker container metrics mapped
- [x] Host CPU/RAM/disk/network mapped
- [x] CPU thermal guard mapped
- [x] PostgreSQL health mapped
- [x] Stratum TCP probe mapped
- [x] Dashboard HTTP probe mapped
- [x] Pool logs mapped
- [x] Node logs mapped
- [x] Watchdog logs mapped
- [x] Repair/efficiency events mapped

## Dashboard Coverage

- [x] Overview dashboard
- [x] Miners dashboard
- [x] Pool dashboard
- [x] Nodes dashboard
- [x] Earnings dashboard
- [x] Host/system dashboard
- [x] Thermal dashboard
- [x] Logs/incidents dashboard

## Alert Coverage

- [x] Miner down > 2 minutes
- [x] Miner configured wrong
- [x] Miner low or missing work share
- [x] Pool valid share stall
- [x] Pool job notify stall
- [x] Stale submit increase
- [x] Block submit errors
- [x] Node sync drift
- [x] Node import stale
- [x] Node template errors
- [x] Container restart
- [x] Dashboard API unavailable
- [x] PostgreSQL unavailable
- [x] High CPU temperature
- [ ] Thermal guard disabled
- [x] Low disk space
- [x] Loki ingestion failure

## Validation Checks

- [x] YAML files parse
- [x] Compose config renders
- [x] Prometheus config validates
- [x] Prometheus alert rules validate
- [x] Grafana datasource provisioning validates structurally
- [x] Grafana dashboard JSON parses
- [x] Loki/Alloy/Promtail config validates or missing tool is documented
- [x] Exporter tests pass or skipped with reason
- [x] Fixture tests compare mapped metrics to old dashboard API samples

## Staged Run Checks

- [x] Explicit staged-run command recorded
- [x] Observability containers started without restarting mining containers
- [x] Old dashboard reachable on existing address
- [x] `asic-pool` still running
- [x] `bdag-miner-node-1` still running
- [x] `bdag-miner-node-2` still running
- [x] `pool-db` still running
- [x] All seven ASICs connected after staged run
- [x] Prometheus targets healthy
- [x] Grafana reachable
- [x] Loki reachable
- [x] Resource overhead measured
- [x] Rollback command documented

## Upgradeability Checks

- [x] Grafana dashboards are provisioned from files
- [x] Datasource UIDs are stable
- [x] Prometheus scrape config is file-based
- [x] Alerts are file-based
- [x] Custom code is isolated under `exporters/`
- [x] Custom exporter is read-only
- [x] No forked Grafana/Prometheus/Loki code
- [x] Version tags are explicit or upgrade policy is documented
- [x] Retention is configured for at least 30 days or bounded by documented disk cap

## Agent Completion Record

Each agent should append a short entry here:

```text
Agent: lead-orchestrator with discovery, architecture, and operations-review workers
Phase: 1-10
Files changed: ops/observability implementation, docs, generated dashboards, validation script, backlog/checklist
Commands run: read-only source/API probes, dashboard generation, unittest fixture tests, YAML/JSON parse, docker compose config render
Validation: ops/observability/scripts/validate.sh passed including containerized Prometheus, Loki, and Alloy validation
Risks: thermal-guard-disabled alert deferred; current mining data shows recent block submit errors as an operational watch item
Next: phase-11 parity/adoption after a longer observation window
```
