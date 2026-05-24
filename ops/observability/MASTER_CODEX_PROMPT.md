# Master Codex Prompt

Use this prompt to continue the project with minimal human intervention.

```text
You are Codex working in /home/jeremy/blockdag-asic-pool.

Build the new parallel BlockDAG observability dashboard using agents and strict SDLC.

Read first:
- AGENTS.md
- ops/observability/README.md
- ops/observability/AGENTIC_SDLC.md
- ops/observability/TASK_BACKLOG.yml
- ops/observability/AGENT_PROMPTS.md
- ops/observability/OVERSIGHT_CHECKLIST.md

Goal:
Create a new Grafana/Prometheus/Loki based dashboard stack under ops/observability that runs beside the existing dashboard. Keep the old dashboard and watchdog as the production repair/control console until the new system proves parity.

Autonomy:
Proceed without asking me for every implementation decision. Spawn agents for bounded tasks when useful. Complete pending phases in order. Keep tasks discrete, reviewable, and easy to understand.

Allowed without asking:
- read repo files
- read safe logs
- query local read-only dashboard APIs
- inspect Docker status/logs read-only
- create/edit files under ops/observability
- create docs and validation scripts
- run offline validation
- generate Grafana dashboards, Prometheus config, Loki config, exporter config, and test fixtures

Do not do during normal build phases:
- do not modify live ASIC configuration
- do not restart mining containers
- do not restart Docker
- do not stop the old dashboard/watchdog
- do not edit wallet/private key material
- do not delete runtime data, logs, DB data, backups, or snapshots

Batch live-risk work:
When implementation and offline validation are complete, prepare a staged-run report and a single deployment command set for the new observability stack only. The staged run must not restart mining services.

Process:
1. Identify the first pending phase in TASK_BACKLOG.yml.
2. Spawn agents using AGENT_PROMPTS.md for bounded tasks.
3. Enforce disjoint write scopes.
4. Integrate agent results.
5. Run validations.
6. Update OVERSIGHT_CHECKLIST.md and TASK_BACKLOG.yml.
7. Continue until offline validation and operations review are complete.
8. Stop before staged run if platform permissions require explicit approval for image pulls or starting containers.

Final response format:
- phase completed
- files changed
- validation results
- mining safety status
- next phase
```
