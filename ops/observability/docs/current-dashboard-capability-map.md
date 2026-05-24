# Current Dashboard Capability Map

Generated: 2026-05-05

## Summary

The current dashboard has four tabs. Visualization and alerting can move to Grafana/Prometheus/Loki, but repair and ASIC configuration controls stay in the old dashboard.

## Pool Status Tab

| Current capability | Current source | Target migration path |
|---|---|---|
| Overall stack state | `/api/status.overall`, failures/warnings | json_exporter from old API initially; custom read-only bdag_exporter metric later |
| Node sync progress bar | `/api/status.sync_progress`, node JSON-RPC `eth_syncing` | custom read-only bdag_exporter metric |
| Node block and main-order status | node Docker logs parsed by `parse_node_log()` | Loki/log-derived signal; bdag_exporter for numeric gauges |
| Pool endpoint and local IPs | local host commands/env in `pool_ops.py` | json_exporter from old API, then static config/bdag_exporter |
| Container status/image/restarts | Docker inspect | standard exporter metric via cAdvisor/Docker metrics |
| Active alerts list | `/api/status.failures` and `warnings` | json_exporter from old API initially; Prometheus alert rules later |
| Node log tails | Docker logs | Loki/log-derived signal |
| Pool log tail | Docker logs | Loki/log-derived signal |
| Latest action log/state | `ops/runtime/latest-action.json` and runtime action logs | Loki/log-derived signal plus bdag_exporter state |
| Start/restart/clean restore buttons | `POST /api/action` | old-control-only |
| Codex handoff button | `POST /api/action` handoff | old-control-only |

## Miners Tab

| Current capability | Current source | Target migration path |
|---|---|---|
| Tracked miner counts | `/api/status.miner_health` | custom read-only bdag_exporter metric |
| Miner status/configured/connected | miner registry, ASIC HTTP, pool logs | custom read-only bdag_exporter metric |
| Worker/wallet identities | pool logs and registry | bdag_exporter labels with cardinality controls |
| Shares, share work, work percent | pool logs parsed by `parse_pool_activity()` | custom read-only bdag_exporter metric; Loki for raw evidence |
| Found blocks | pool logs | Loki/log-derived signal plus bdag_exporter counter |
| Last share/submit/pool seen age | pool logs and registry | custom read-only bdag_exporter metric |
| Miner issue text | derived dashboard strings | Prometheus alerts from bdag_exporter metrics |
| Miner work share trend | `/api/earnings.history[].miner_estimates` | json_exporter from old API initially; bdag_exporter recording rules later |
| LAN scan target/defaults | `/api/miners/defaults` | old-control-only |
| Scan LAN | `POST /api/miners/scan` | old-control-only |
| Configure selected miners | `POST /api/miners/configure` | old-control-only |
| Save password for watchdog | `POST /api/miners/save-auth` | old-control-only |
| Discovered miners table | scan result and registry | old-control-only for scanning; bdag_exporter can expose registry status read-only |

## Global Tab

| Current capability | Current source | Target migration path |
|---|---|---|
| Latest block | `/api/global.latest_block` from node JSON-RPC | custom read-only bdag_exporter metric |
| Scanned/fetched blocks | `/api/global.fetched_blocks`, scan window | json_exporter from old API initially |
| Unique miners | recent block headers | json_exporter from old API initially; bdag_exporter later |
| Scan window and avg block seconds | recent block headers | json_exporter from old API initially |
| Top share | `/api/global.clusters[0].share_percent` | json_exporter from old API |
| Estimated earnings by pool | block header clustering plus price feed | json_exporter from old API initially; bdag_exporter only if old global API is retired |
| Pool earnings trend | `/api/global.history` | json_exporter from old API initially; long-term Prometheus recording after exporter |
| Observed peer IPs/geolocation | container `/proc/net/tcp*` plus `ipwho.is` cache | old-control-only enrichment; Loki/bdag_exporter for counts only |

## Earnings Tab

| Current capability | Current source | Target migration path |
|---|---|---|
| Current BDAG price USD/ZAR | exchange APIs and USD/ZAR cache | json_exporter from old API initially; optional bdag_exporter if needed |
| Wallet averages and 24h values | wallet balance sources plus snapshot history | json_exporter from old API initially |
| Wallet totals | JSON-RPC/explorer balance checks | json_exporter from old API initially; bdag_exporter later |
| Address credits | PostgreSQL `credits` and `blocks` tables | postgres_exporter for DB health; bdag_exporter/custom SQL for business metrics |
| Estimated earnings by miner | PostgreSQL credits plus pool-log share work plus price | json_exporter from old API initially; bdag_exporter later |
| Miner earnings trend | `ops/runtime/earnings-snapshots.jsonl` via `/api/earnings.history` | json_exporter from old API initially; Loki or bdag_exporter/recording rules later |
| Wallet cross-check | wallet RPC/explorer sources | json_exporter from old API initially |
| Price feed details | exchange source statuses | json_exporter from old API |
| Earnings snapshot log | `ops/runtime/earnings-snapshots.jsonl` | Loki/log-derived signal or bdag_exporter |

## Cross-Cutting Controls

| Control | Current endpoint | Target migration path |
|---|---|---|
| Dashboard action token | `/api/token-required`, runtime token file | old-control-only |
| Stack start | `POST /api/action` action `start` | old-control-only |
| Stack restart | `POST /api/action` action `restart` | old-control-only |
| Clean restore | `POST /api/action` action `clean_restore` | old-control-only |
| Miner scan/config/save-auth | `POST /api/miners/*` | old-control-only |

## Coverage Summary

Every current tab has a migration path:

- Pool Status: mostly standard exporters and Loki, with bdag_exporter for BlockDAG-specific state.
- Miners: primarily custom read-only bdag_exporter because ASIC and pool-log joins are domain-specific.
- Global: json_exporter can bootstrap from old API; long-term replacement needs bdag_exporter if the old API is retired.
- Earnings: json_exporter can bootstrap; PostgreSQL health is standard, but earnings semantics are custom business metrics.

Controls are intentionally not migrated into Grafana. Grafana panels should link back to the old dashboard for repair actions.
