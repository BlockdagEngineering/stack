# Agentic SDLC For The Parallel Observability Dashboard

This document defines how Codex and its agents should build the new dashboard system without interrupting the current BlockDAG mining pool.

The existing dashboard/watchdog remains production control. The new system is built beside it, under `ops/observability/`, until it has proven parity and better long-term monitoring.

## Project Goal

Build a second dashboard stack using standard open-source tools:

- Grafana for dashboards and alert UI
- Prometheus for metrics and alert rules
- Loki plus Alloy or Promtail for logs
- Alertmanager or Grafana Alerting for notifications
- node_exporter for host metrics
- cAdvisor or Docker metrics for containers
- postgres_exporter for pool database health
- blackbox_exporter for HTTP/TCP probes
- json_exporter and/or a small `bdag_exporter` for BlockDAG-specific metrics

The custom code should be limited to a thin BlockDAG cartridge only where standard tooling cannot express the requirement through configuration.

## Operating Principle

Codex should proceed autonomously through design, scaffolding, offline validation, and read-only verification. It should batch any live-risk actions into explicit deployment steps instead of asking for approval at every small step.

Live mining uptime has priority over dashboard speed.

## Repository Boundary

Primary write scope:

```text
ops/observability/
```

Allowed supporting writes:

```text
AGENTS.md
docs/
```

Do not modify the existing production dashboard or watchdog unless a specific migration task requires it and there is a rollback path.

## Phase Model

### Phase 0: Charter

Purpose: establish goals, boundaries, and agent workflow.

Outputs:

- `AGENTIC_SDLC.md`
- `AGENT_PROMPTS.md`
- `TASK_BACKLOG.yml`
- `OVERSIGHT_CHECKLIST.md`

Exit criteria:

- Agent roles are clear.
- Work packets are discrete.
- Safety gates are defined.

### Phase 1: Discovery

Purpose: inventory read-only data sources and map the current dashboard capabilities.

Tasks:

- Inventory current dashboard API endpoints.
- Inventory runtime logs.
- Inventory Docker containers, ports, and networks.
- Inventory database access patterns.
- Inventory current metrics hidden in JSON/logs.
- Define which data can be collected by existing exporters.
- Define which data needs `json_exporter` or `bdag_exporter`.

Outputs:

- `docs/discovery-report.md`
- `docs/current-dashboard-capability-map.md`
- `docs/data-source-inventory.md`

Exit criteria:

- No live service changes.
- Every current dashboard tab has a target migration path.

### Phase 2: Architecture

Purpose: design the new stack with upgradeability and low custom code.

Tasks:

- Select exporters.
- Define ports and volumes.
- Define retention and resource limits.
- Define authentication/LAN exposure plan.
- Define dashboard folders.
- Define alert categories.
- Define rollback.

Outputs:

- `docs/architecture.md`
- `docs/security-model.md`
- `docs/rollback.md`

Exit criteria:

- New stack does not share mutable state with mining services except read-only mounts or read-only APIs.
- Port conflicts are checked.
- Resource budget is documented.

### Phase 3: Scaffold

Purpose: create the project skeleton without starting services.

Tasks:

- Create `docker-compose.observability.yml`.
- Create Prometheus config.
- Create exporter configs.
- Create Grafana provisioning config.
- Create Loki/Alloy config.
- Create initial dashboards as provisioned JSON.
- Create `.env.example`.

Outputs:

```text
ops/observability/docker-compose.observability.yml
ops/observability/prometheus/
ops/observability/grafana/
ops/observability/loki/
ops/observability/alloy/
ops/observability/exporters/
```

Exit criteria:

- Static config checks pass where possible.
- Compose config renders.
- No containers are started unless explicitly in a staged-run task.

### Phase 4: Metrics Cartridge

Purpose: expose BlockDAG-specific signals in standard Prometheus shape.

Preferred order:

1. Use `json_exporter` against existing `/api/status`, `/api/earnings`, and `/api/global`.
2. Use exporter configs for host/container/db/probe metrics.
3. Add a small `bdag_exporter` only for metrics that cannot be mapped cleanly from JSON.

Required metric groups:

- Pool health
- Node sync and template health
- ASIC online/configured/connected state
- Miner work percentage
- Last share age
- Submit/share/stale counters
- Earnings estimates
- Thermal guard state
- Watchdog repair events

Outputs:

- `prometheus/prometheus.yml`
- `prometheus/alerts.yml`
- `exporters/json_exporter.yml`
- optional `exporters/bdag_exporter.py`
- `docs/metrics-catalog.md`

Exit criteria:

- Each required metric has a name, labels, source, and validation method.

### Phase 5: Dashboards

Purpose: create polished Grafana dashboards that replace historical charting from the custom dashboard.

Dashboard folders:

- Overview
- Miners
- Pool
- Nodes
- Earnings
- System
- Thermals
- Incidents

Outputs:

- `grafana/dashboards/*.json`
- `grafana/provisioning/dashboards/*.yml`
- `grafana/provisioning/datasources/*.yml`

Exit criteria:

- Each dashboard has datasource variables or fixed provisioned datasources.
- Dashboards are versioned in Git as JSON.
- Panels link back to the old control dashboard for repair actions.

### Phase 6: Alerts

Purpose: turn known mining degradation modes into standard alert rules.

Required alert groups:

- Miner down > 2 minutes
- Miner configured wrong
- Miner low work share
- Pool share stall
- Job notify stall
- Stale submit increase
- Block submit errors
- Node sync drift
- Node template errors
- Container restart
- Dashboard API unavailable
- PostgreSQL unavailable
- CPU temperature high
- Disk free space low
- Thermal guard disabled

Outputs:

- `prometheus/alerts.yml`
- `alertmanager/alertmanager.yml` or Grafana alert provisioning
- `docs/alerts.md`

Exit criteria:

- Every current watchdog failure mode has either an alert or an explicit reason why it remains custom-only.

### Phase 7: Offline Validation

Purpose: test configs without production disruption.

Tasks:

- Render Compose config.
- Validate YAML.
- Validate Prometheus config.
- Validate Grafana provisioning files structurally.
- Validate dashboards are JSON.
- Validate exporter mappings against saved API payload fixtures.

Outputs:

- `docs/validation-report.md`
- `testdata/*.json`
- optional helper scripts under `scripts/`

Exit criteria:

- Offline checks pass.
- Any skipped checks are documented with exact reason.

### Phase 8: Staged Run

Purpose: run the new stack in parallel.

Tasks:

- Start only observability services.
- Verify old dashboard still responds.
- Verify pool and miners are still mining.
- Verify new Grafana loads.
- Verify Prometheus targets.
- Verify Loki receives logs.
- Measure CPU/RAM/disk overhead.

Outputs:

- `docs/staged-run-report.md`

Exit criteria:

- Mining remains healthy.
- New stack resource cost is acceptable.
- No port conflicts.

### Phase 9: Parity And Adoption

Purpose: compare old and new dashboards.

Tasks:

- Compare status values.
- Compare miner list/name/work share.
- Compare node sync status.
- Compare pool submit/share counters.
- Compare earnings trends.
- Compare incidents.

Outputs:

- `docs/parity-matrix.md`
- `docs/migration-checklist.md`

Exit criteria:

- Each old dashboard view is marked `keep`, `migrated`, or `custom-control-only`.

## Oversight Model

Each agent task must return:

- files changed
- commands run
- test/validation result
- risks found
- next recommended task

The lead Codex agent integrates results and performs final review. The lead must not mark the project complete until the oversight checklist passes.

## Autonomy Rules

Proceed without user interruption for:

- repo-local planning files
- repo-local config generation
- read-only source/log/API inspection
- offline tests
- JSON/YAML validation
- dashboard JSON generation
- Prometheus rule generation
- docs and reports

Batch for one explicit deployment step:

- pulling images
- starting new containers
- binding services to LAN
- installing packages
- systemd installation
- firewall changes

Even when trusted, live-risk work should be grouped into one clear staged-run step so the mining pool is not changed accidentally.

## Completion Definition

The project is complete when:

- new stack runs alongside old dashboard
- all core dashboards render
- alerts exist for mining degradation modes
- 30-day retention is configured
- resource overhead is measured
- rollback is documented
- old dashboard remains available as fallback
- migration checklist identifies what still needs old custom controls
