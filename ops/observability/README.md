# BlockDAG Parallel Observability Dashboard

This directory contains the plan and future implementation for a new dashboard stack built beside the existing BlockDAG dashboard.

The existing dashboard/watchdog remains production control. This project adds a standard open-source observability stack for polished long-term monitoring, alerting, and logs.

## Start Here For Codex

Read these files in order:

1. `../../AGENTS.md`
2. `AGENTIC_SDLC.md`
3. `TASK_BACKLOG.yml`
4. `AGENT_PROMPTS.md`
5. `OVERSIGHT_CHECKLIST.md`

Then continue from the first pending phase in `TASK_BACKLOG.yml`.

## Target Stack

- Grafana
- Prometheus
- Loki
- Alloy or Promtail
- Alertmanager or Grafana Alerting
- node_exporter
- cAdvisor or Docker metrics
- postgres_exporter
- blackbox_exporter
- json_exporter
- optional read-only `bdag_exporter`

## Project Rules

- Build under `ops/observability/`.
- Do not replace the old dashboard yet.
- Do not restart mining services during design/scaffold/validation.
- Prefer configuration over custom code.
- Keep custom code isolated to read-only exporter modules.
- Use file provisioning for dashboards, datasources, alerts, and scrape config.

## Current Status

Phases 1-10 are complete through staged run. Phase 11 parity/adoption is pending a longer observation window.

Implemented artifacts include:

- isolated observability compose stack
- Prometheus scrape and alert configuration
- Loki and Alloy log pipeline configuration
- read-only BlockDAG metrics exporter
- Grafana provisioning and generated dashboards
- metrics, logs, alerts, dashboard, validation, architecture, security, rollback, and operations review docs

Validation passes, including Prometheus, Loki, and Alloy checks through containerized validator commands when local binaries are unavailable.

## Human-Free Workflow Shape

Codex should work autonomously through:

- discovery
- architecture
- scaffold
- metrics mapping
- dashboards
- alerts
- offline validation
- operations review

Live deployment must be batched into a staged-run phase. That phase starts only the new observability stack and verifies that mining is unaffected.

## Old Dashboard Role

The old dashboard stays responsible for:

- repair actions
- ASIC configuration
- miner restarts
- node/pool restarts
- clean restore
- BlockDAG-specific operational controls

The new dashboard should first replace:

- long-term plots
- metrics history
- logs
- alert visibility
- incident timeline
- system and thermal monitoring
