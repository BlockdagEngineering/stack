# Phase 1 Discovery Report

Generated: 2026-05-05

## Scope

This discovery covers the current production dashboard, watchdog, pool operations helpers, root `docker-compose.yml`, and read-only runtime/API/log sources. No production services were changed, restarted, or reconfigured.

## Current Production Surface

The old dashboard is a Python `ThreadingHTTPServer` in `ops/dashboard.py`, defaulting to `127.0.0.1:8088` unless overridden by environment. The live process was observed listening on `0.0.0.0:8088`, so the old dashboard is reachable beyond loopback on this host. It remains the control console.

Current dashboard tabs:

- Pool Status
- Miners
- Global
- Earnings

Current dashboard read APIs:

- `GET /api/status`
- `GET /api/token-required`
- `GET /api/miners/defaults`
- `GET /api/miners/registry`
- `GET /api/global`
- `GET /api/earnings`

Current dashboard write/control APIs, old-control-only:

- `POST /api/action`
- `POST /api/miners/scan`
- `POST /api/miners/configure`
- `POST /api/miners/save-auth`

## Runtime Snapshot

| Container | Image | Published ports | Migration source class |
|---|---|---:|---|
| `asic-pool` | `nathanbdagnetwork/asic-pool:optimised-mainnet` | `3334/tcp` | cAdvisor/Docker exporter, Loki, json_exporter, bdag_exporter |
| `rpc-failover` | `haproxy:2.9-alpine` | `38131/tcp` | cAdvisor/Docker exporter, blackbox_exporter, Loki |
| `pool-db` | `postgres:15-alpine` | `5432/tcp` | postgres_exporter, cAdvisor/Docker exporter, Loki |
| `bdag-miner-node-1` | `nathanbdagengineering/bdag-gameday-binaries:optimised-mainnet` | `8151/tcp`, `6061->6060/tcp` | node native Prometheus endpoint, cAdvisor/Docker exporter, Loki, bdag_exporter |
| `bdag-miner-node-2` | `nathanbdagengineering/bdag-gameday-binaries:optimised-mainnet` | `8152/tcp`, `6062->6060/tcp` | node native Prometheus endpoint, cAdvisor/Docker exporter, Loki, bdag_exporter |

Node native metrics are available at:

- `http://127.0.0.1:6061/debug/metrics/prometheus`
- `http://127.0.0.1:6062/debug/metrics/prometheus`

`/metrics` returned `404`; scrape config must use `/debug/metrics/prometheus`.

## Migration Taxonomy

- `standard exporter metric`: node_exporter, cAdvisor/Docker metrics, postgres_exporter, blackbox_exporter, or native node Prometheus metrics.
- `json_exporter from old API`: direct extraction from old dashboard JSON read endpoints.
- `Loki/log-derived signal`: parsed from Docker logs, runtime log files, or JSONL event logs.
- `custom read-only bdag_exporter metric`: read-only logic needed to query JSON-RPC, PostgreSQL, ASIC HTTP APIs, or combine multiple sources.
- `old-control-only`: mutating actions that must stay in the old dashboard/watchdog until explicitly migrated as links or runbooks.

## Watchdog Failure Modes

| Failure mode | Current detector | Current action | Target source class |
|---|---|---|---|
| Docker unavailable | `docker ps` failure in `collect_status()` | record critical event, no repair | standard exporter metric plus Loki/log-derived signal |
| Container missing/not running | Docker inspect in `collect_status()` | start/restart stack after thresholds | standard exporter metric |
| Node wrapper up but `bdag` child not running | `docker top` process check | restart stack or clean restore | custom read-only bdag_exporter metric |
| Node critical log entries | node Docker logs with critical/fatal patterns | clean restore after threshold | Loki/log-derived signal |
| Node mining template failing | node Docker logs: repeated `Failed to create new block template` | targeted node restart or sync repair | Loki/log-derived signal, optionally bdag_exporter count |
| Node sync drift/block lag | parsed node logs and `eth_syncing` JSON-RPC | targeted node restart after threshold | custom read-only bdag_exporter metric |
| Node import stale | parsed node logs, last import age | sync warning/repair path | Loki/log-derived signal, optionally bdag_exporter |
| P2P malformed peer/reset storm | parsed node logs | maintenance warning only | Loki/log-derived signal |
| Pool initial download / RPC refused | pool logs | sync warning/repair path | Loki/log-derived signal |
| Pool share stall | pool logs: valid share age while miners connected | targeted pool/node restart | Loki/log-derived signal plus bdag_exporter aggregate |
| Pool job notify stall | pool logs: job notify age while miners connected | fast repair eligibility | Loki/log-derived signal plus bdag_exporter aggregate |
| Pool stale submit dominance | pool logs: stale submit count vs valid shares | warning/repair eligibility | Loki/log-derived signal |
| Pool mining template frozen | pool logs: `FREEZE DETECTED` age | node/pool restart | Loki/log-derived signal |
| Duplicate block storm | pool logs duplicate block count/ratio | pool restart for pool-template behavior | Loki/log-derived signal |
| ASIC/miner down | miner registry, ASIC HTTP, pool activity logs | miner restart/configure after cooldown | custom read-only bdag_exporter metric plus Loki/log-derived signal |
| ASIC low-difficulty flood | pool activity windows and configured threshold | miner restart | custom read-only bdag_exporter metric |
| ASIC degraded: submitting but no accepted shares | pool activity plus miner registry | node/pool/miner repair depending context | custom read-only bdag_exporter metric |
| Saved miner admin password missing | runtime file absence | skip miner repair | old-control-only, expose only a boolean status metric if needed |
| Dirty shutdown marker | runtime marker file | boot clean restore | old-control-only with Loki/log-derived incident |
| Hourly snapshot active | runtime lock file | suppress stack repair | old-control-only or bdag_exporter read-only state |
| Watchdog check crash | watchdog exception log and efficiency event | keep loop running | Loki/log-derived signal |
| Repair failed/suppressed | efficiency-events JSONL | record incident | Loki/log-derived signal |

## Findings

The old dashboard is already a useful JSON source for Phase 3/4 bootstrap, but it is not a final observability dependency. It performs Docker inspection, log parsing, PostgreSQL queries, external price requests, chain JSON-RPC calls, and ASIC HTTP probes during normal reads. Heavy panels should be migrated to purpose-built exporters or Loki queries to avoid repeatedly exercising the old control process.

The cleanest first split is:

- standard exporters for host, Docker/container, PostgreSQL, blackbox probes, and native BlockDAG node metrics;
- Loki for pool/node/watchdog/thermal guard patterns and incident timelines;
- json_exporter for coarse old dashboard status/earnings/global bootstrap signals;
- a small read-only `bdag_exporter` for cross-source mining semantics: miner health, share/job stall ages, ASIC configuration status, wallet/credit cross-checks, sync progress, and runtime control state.

## Data-Source Gaps

- The pool container does not expose a discovered Prometheus endpoint; pool share/job/template health currently comes from log parsing.
- ASIC miner telemetry requires LAN HTTP calls (`/mcb/status`, `/mcb/pools`, `/mcb/cgminer?cgminercmd=devs`). This should not be done by json_exporter against the old dashboard at high frequency; use a rate-limited read-only exporter.
- Dashboard API reads can be expensive because `include_logs=True` and earnings/global calls perform multiple backend queries. json_exporter scrape intervals should be conservative.
- Thermal guard state is only in `ops/runtime/logs/cpu-thermal-guard.log`; node_exporter can cover host temperatures, but policy state needs Loki or a custom textfile/exporter metric.
- Repair actions, saved miner admin password handling, ASIC pool configuration, and clean restore must remain old-control-only.
- Existing node metrics endpoint path is nonstandard: `/debug/metrics/prometheus`.
- Public bind exposure exists for old dashboard and production ports. Observability design should default to loopback/LAN-only with authentication.

## Risks

- Scraping old dashboard APIs too frequently can add Docker, log, database, external HTTP, and JSON-RPC load to the mining host.
- Logs may contain operational details and wallet addresses. Loki ingestion must exclude secrets (`asic-pool/.env`, dashboard token, miner admin password) and keep labels low-cardinality.
- ASIC LAN probing can cause latency or configuration risk if mixed with control endpoints. Exporter must be read-only and never call `/mcb/restart` or pool mutation endpoints.
- Some old dashboard values are windowed from recent logs, not cumulative counters; alerts must account for resets and sparse log windows.
- Current live status was healthy during discovery, but runtime logs show recent repeated template failures, pool stalls, ASIC degradation, and targeted restarts. Alert thresholds should match existing cooldown behavior before paging.
