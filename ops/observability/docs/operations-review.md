# Phase 9 Operations Review

Review date: 2026-05-05

Reviewer role: operations-reviewer-agent

Scope reviewed:

- `AGENTS.md`
- `ops/observability/MASTER_CODEX_PROMPT.md`
- `ops/observability/TASK_BACKLOG.yml`
- `ops/observability/OVERSIGHT_CHECKLIST.md`
- `ops/observability/docs/architecture.md`
- `ops/observability/docs/security-model.md`
- `ops/observability/docs/rollback.md`
- `ops/observability/docker-compose.observability.yml`
- `ops/observability/prometheus/prometheus.yml`
- `ops/observability/prometheus/alerts.yml`
- `ops/observability/loki/loki.yml`
- `ops/observability/alloy/config.alloy`
- `ops/observability/exporters/bdag_exporter/bdag_exporter.py`
- `ops/observability/exporters/json_exporter.yml`
- `ops/observability/grafana/provisioning/**`
- `ops/observability/grafana/dashboards/*.json`
- `ops/observability/scripts/validate.sh`
- `ops/observability/docs/validation-report.md`

## Readiness Verdict For Staged Run

Verdict: not ready for an unsupervised staged run yet.

The implementation is directionally correct: it uses a separate observability compose file, separate named volumes, a dedicated bridge network, localhost-only published ports, provisioned Grafana dashboards, Prometheus rules, Loki/Alloy config, and a read-only `bdag_exporter`. The old dashboard remains the repair/control surface through Grafana links rather than migrated actions.

However, the staged run should be gated until the must-fix items below are addressed or explicitly accepted by the operator. The largest blockers are incomplete binary validation, default credentials/DSN posture, exporter/API scrape load against the old dashboard, resource caps above the documented budget, and unnecessary host publication of internal exporter ports.

## Must-Fix Issues

1. Complete validation for Prometheus, Loki, and Alloy before starting the stack.

   `docs/validation-report.md` reports pass for Python syntax, generated dashboards, JSON/YAML parse, fixture tests, and compose render, but `promtool`, `loki`, and `alloy` validation were skipped. The skip is documented in `docs/validation-report.md`, and the corresponding checks are optional in `scripts/validate.sh:52-69`. Before staged run, validate `prometheus/prometheus.yml`, `prometheus/alerts.yml`, `loki/loki.yml`, and `alloy/config.alloy` with the actual binaries or containerized equivalents without starting mining services.

   Status after lead remediation: still open. The validation script documents the missing local tools and passes all checks available offline.

2. Replace staged-run credentials and the PostgreSQL DSN before enabling the stack.

   `docker-compose.observability.yml:27-30` defaults Grafana to `admin` / `change-me-before-lan-exposure`, and `.env.example:18-19` repeats that placeholder. `docker-compose.observability.yml:232-233` defaults `postgres-exporter` to `postgresql://test:test@host.docker.internal:5432/pool?sslmode=disable`, also shown in `.env.example:16`. This conflicts with the security model requirement for generated Grafana credentials and a dedicated read-only monitoring database account. For the first staged run, provide a non-committed Grafana password and either a confirmed read-only `BDAG_POSTGRES_DSN` or disable `postgres-exporter` until that account exists.

   Status after lead remediation: partially addressed. The fake PostgreSQL DSN was removed from defaults, and `.env.example` now requires a local, non-committed read-only DSN. A non-default Grafana password is still a staged-run prerequisite.

3. Reduce old-dashboard API scrape risk.

   `prometheus/prometheus.yml:1-4` sets a global 30s scrape/evaluation interval. `prometheus/prometheus.yml:19-24` scrapes `bdag-exporter` at that cadence, and `bdag_exporter.py` calls `/api/status`, `/api/earnings`, and `/api/global` on every `/metrics` request. Discovery notes that these old dashboard APIs can perform Docker inspection, log parsing, database queries, JSON-RPC calls, and external price/feed work. Before staged run, set a conservative per-job scrape interval for `bdag-exporter` or split expensive endpoints so the old control dashboard is not exercised every 30 seconds under failure conditions.

   Status after lead remediation: addressed for first staged run. `bdag_exporter.py` now caches `/api/status` for 30 seconds and `/api/earnings` plus `/api/global` for 300 seconds.

4. Bring resource limits back under the documented budget.

   `docs/architecture.md` budgets the entire stack for less than 0.75 CPU average, 2 CPU burst, and 2.5 GB memory hard planning cap. Current compose limits total about 4.25 CPUs and nearly 3 GB memory across Grafana, Prometheus, Alertmanager, Loki, Alloy, exporters, node-exporter, cAdvisor, and postgres-exporter (`docker-compose.observability.yml:44-45`, `73-74`, `91-92`, `108-109`, `129-130`, `153-154`, `169-170`, `187-188`, `203-204`, `222-223`, `238-239`). These are limits rather than reservations, but they allow the observability stack to compete harder than the stated plan. Tighten limits or document an explicit supervised measurement plan before staged run.

   Status after lead remediation: addressed for first staged run. Default limits are now under the documented 2 CPU burst and 2.5 GB memory planning cap.

5. Remove host-published ports for internal-only exporters unless there is a specific staged-run reason.

   The security model says exporter ports should not be published to the host and should be reachable only on `bdag-observability-net`. Current compose publishes `bdag-exporter` (`docker-compose.observability.yml:137-138`), `blackbox-exporter` (`161-162`), `json-exporter` (`177-178`), `cadvisor` (`212-213`), and `postgres-exporter` (`230-231`) to `127.0.0.1`. This is not public exposure, but it violates the documented least-exposure model and increases local attack surface. Grafana, Prometheus, Alertmanager, and Loki localhost bindings are acceptable for staged run; exporter ports should stay internal unless actively debugged.

   Status after lead remediation: addressed. Internal exporter and cAdvisor host-published ports were removed.

6. Add or explicitly defer missing acceptance alerts before declaring operations readiness.

   `prometheus/alerts.yml` covers miner down, miner misconfiguration, low work share, share/job stalls, stale submits, block submit errors, node sync/import/template problems, container restart, dashboard API failure, PostgreSQL exporter down, high CPU temperature, and low disk. It does not currently cover thermal guard disabled/unknown or Loki ingestion failure, both listed in `docs/architecture.md` and `OVERSIGHT_CHECKLIST.md`. Add those rules or document them as explicit staged-run deferrals.

   Status after lead remediation: partially addressed. Loki endpoint availability alerting was added. Thermal guard disabled/unknown remains explicitly deferred until a read-only runtime-state metric or Loki-derived rule exists.

## Should-Fix Issues

1. Pin image versions instead of using floating `latest` tags.

   Grafana, Prometheus, Alertmanager, Loki, Alloy, blackbox-exporter, json-exporter, node-exporter, cAdvisor, and postgres-exporter use floating tags in `docker-compose.observability.yml:21`, `48`, `77`, `95`, `112`, `157`, `173`, `191`, `207`, and `226`. This reduces reproducibility and can change behavior at pull time during the gated staged-run phase.

2. Remove `--web.enable-lifecycle` unless live reload is required.

   Prometheus enables the lifecycle endpoint in `docker-compose.observability.yml:59`. With localhost-only binding this is limited, but it is still an unauthenticated mutation surface on the Prometheus process. File changes can be applied by restarting only the observability stack during staged testing.

3. Decide whether `json-exporter` is still needed.

   `docker-compose.observability.yml:172-188` defines `json-exporter`, and `exporters/json_exporter.yml` contains a small bootstrap mapping. `prometheus/prometheus.yml` does not scrape it. If `bdag-exporter` is the primary path, remove or disable `json-exporter`; if it is retained, add explicit scrape jobs and document why both exporters are needed.

4. Expand blackbox probes or align documentation to current coverage.

   `docs/data-source-inventory.md` recommends probes for RPC failover, PostgreSQL TCP, and node metrics ports in addition to old dashboard and stratum. `prometheus/prometheus.yml:26-58` currently probes only old dashboard HTTP and stratum TCP. This is acceptable for a narrow first run, but the coverage gap should be intentional.

5. Add healthchecks for staged-run observability containers.

   Compose currently relies on `depends_on` ordering, for example `grafana` depends on `prometheus` and `loki` in `docker-compose.observability.yml:41-43`, but no service healthchecks are defined. Healthchecks would make staged-run triage clearer without touching mining services.

6. Tighten log secret handling for Docker logs.

   Runtime file logs have a drop stage for sensitive terms in `alloy/config.alloy:76-79`. Docker log ingestion from mining containers flows through `loki.process "mining"` in `alloy/config.alloy:42-54`, which labels level but does not apply the same secret-pattern drop. The current selected containers are appropriate, but Docker logs should receive the same defensive filtering before staged run or the risk should be accepted.

7. Clarify phase bookkeeping.

   `TASK_BACKLOG.yml` and `OVERSIGHT_CHECKLIST.md` still show phases 1-9 as pending even though discovery, architecture, scaffold, metrics, logs, dashboards, alerts, and validation artifacts exist. The user explicitly limited this review to `docs/operations-review.md`, so this file does not update those control documents. Before handoff, update them in a separate authorized task.

## Resource And Security Risks

Resource risks:

- The aggregate compose CPU and memory limits exceed the architecture budget, as noted above.
- Prometheus retention is configured for 30 days and 15 GB in `docker-compose.observability.yml:57-58`. Loki retention is configured for 744h in `loki/loki.yml:28-39`. These are good defaults, but disk write rate and compactor behavior are unmeasured until staged run.
- `alloy` ingests Docker logs through the Docker socket and reads `/var/lib/docker/containers` plus `../runtime/logs` in `docker-compose.observability.yml:120-124`. This is read-only but can add log I/O on a mining host.
- `node-exporter` and `cadvisor` mount broad host paths read-only (`docker-compose.observability.yml:198-199`, `214-219`). This is normal for those exporters but should be monitored for disk and metadata I/O.
- The old dashboard API remains a dependency for most BlockDAG metrics. If dashboard reads become slow, `bdag-exporter` can hold up scrapes while serially querying all three endpoints.

Security risks:

- Grafana anonymous signup is disabled (`docker-compose.observability.yml:30`), and all published ports are bound to `127.0.0.1`. That satisfies the no-public-bind safety gate.
- Placeholder Grafana credentials and the default PostgreSQL DSN must not be used for an actual staged run.
- Internal exporters are unnecessarily host-published on localhost.
- Prometheus, Loki, and Alertmanager have no native auth, which is acceptable only while bound to localhost as designed.
- Loki has `auth_enabled: false` in `loki/loki.yml:1`; do not expose it beyond localhost without an authenticated proxy.
- The log pipeline excludes broad secret file paths by only mounting `../runtime/logs`, not the whole runtime tree, and does not mount `asic-pool/.env`, wallet files, or database data. The Docker-log path still needs the same sensitive-line drop used for runtime logs.

## Rollback Review

Rollback is well-scoped on paper.

`docs/rollback.md` uses:

```bash
docker compose -p bdag-observability -f ops/observability/docker-compose.observability.yml down
```

for normal rollback, and documents `down -v` only for explicitly approved observability data removal. The rollback plan does not stop root compose services, does not restart Docker, and does not remove production data. The compose file uses a distinct project name, network, and observability-only named volumes (`docker-compose.observability.yml:1`, `9-17`), so the rollback command should target only the parallel stack.

Rollback acceptance is conditional on starting the stack with the documented project/file pair. Do not use broad Docker prune commands. Post-rollback checks in `docs/rollback.md` are read-only and correctly avoid repair operations.

## Old-Dashboard Fallback Review

The old dashboard fallback is preserved.

- `docs/current-dashboard-capability-map.md` keeps repair, stack start/restart, clean restore, miner scan, miner configure, and saved miner auth as old-control-only.
- Generated dashboards include a link titled `Old Repair Dashboard` pointing to `http://127.0.0.1:8088`; this is visible in all eight dashboard JSON files.
- `docs/dashboard-map.md` states that dashboards do not call old dashboard APIs directly from browser JavaScript. Grafana reads Prometheus and Loki only; repair remains a link back to the old console.
- `bdag_exporter.py` performs only GET reads against `/api/status`, `/api/earnings`, and `/api/global`; it does not call action, scan, configure, save-auth, Docker exec, or ASIC mutation paths.

Fallback caveat: the new metrics stack depends heavily on old dashboard API availability. `prometheus/alerts.yml:34-42` correctly alerts when the old dashboard API is unavailable, but that means the new stack is not yet independent of the old control process for BlockDAG-specific metrics.

## Mining Safety Confirmation

This operations review did not restart mining services, did not run `docker compose up`, did not pull images, did not restart Docker, did not call ASIC endpoints, did not alter ASIC configuration, did not edit wallet/private-key material, and did not modify the root mining compose file.

The only file written by this review is:

- `ops/observability/docs/operations-review.md`

## Lead Remediation Update

After this review, the lead orchestrator addressed the offline-safe must-fix items that did not require starting containers or changing production services:

- Removed host-published ports for internal exporters and cAdvisor in `docker-compose.observability.yml`; Grafana, Prometheus, Alertmanager, and Loki remain localhost-only for staged testing.
- Reduced default compose resource ceilings to stay under the documented planning cap for the default stack.
- Removed Prometheus `--web.enable-lifecycle`.
- Added cache intervals to `bdag_exporter.py`: status 30s, earnings 300s, global 300s.
- Removed the fake `test:test` PostgreSQL DSN default; `.env.example` now requires a real non-committed read-only DSN before staged run.
- Applied secret-pattern drops to Docker log ingestion as well as runtime file logs.
- Added Prometheus scraping and alert coverage for Loki endpoint availability.
- Re-ran `ops/observability/scripts/validate.sh`; it passed again with the same documented skips for missing `promtool`, `loki`, and `alloy` binaries.

Remaining gates before staged run:

- Validate Prometheus, Loki, and Alloy with their actual binaries or explicitly accept this as a staged-run risk.
- Provide a non-committed Grafana admin password and read-only PostgreSQL DSN.
- Keep the thermal-guard-disabled alert as an explicit deferral until a read-only runtime-state metric or Loki-derived rule exists.
- Consider pinning image versions and adding healthchecks before long-term adoption.
