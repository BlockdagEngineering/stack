# Data Source Inventory

Generated: 2026-05-05

## Standard Exporter Metrics

| Source | Access | Signals | Notes |
|---|---|---|---|
| Host OS | node_exporter | CPU, memory, disk, network, filesystem, thermal zones where exposed | Needed for system and thermal dashboards. |
| Docker/container runtime | cAdvisor or Docker metrics | container up, restarts, CPU, memory, I/O, network | Covers `asic-pool`, `pool-db`, `rpc-failover`, `bdag-miner-node-*`. |
| PostgreSQL | postgres_exporter | DB up, connections, locks, table/index stats, disk pressure | Business totals still need custom SQL/exporter. |
| Blackbox probes | blackbox_exporter | HTTP/TCP availability | Probe old dashboard, stratum port `3334`, RPC failover `38131`, node metrics ports, PostgreSQL TCP. |
| Native BlockDAG node Prometheus | `:6061/debug/metrics/prometheus`, `:6062/debug/metrics/prometheus` | node runtime metrics exported by the node binary | Path is not `/metrics`; configure scrape path explicitly. |

## Old Dashboard JSON APIs

| Endpoint | Safe use | Key payloads | Target class |
|---|---|---|---|
| `GET /api/status` | Conservative scrape interval; read-only | overall, containers, nodes, sync progress, pool health, miner health, warnings/failures | json_exporter from old API |
| `GET /api/global` | Conservative scrape interval; read-only but can perform RPC/external geo/cache work | latest block, block scan window, clusters, pool estimates, peer location guess, history | json_exporter from old API |
| `GET /api/earnings` | Conservative scrape interval; read-only but performs DB/wallet/price/history work | credits, price, wallet, miner estimates, history | json_exporter from old API |
| `GET /api/miners/defaults` | Rare/manual | default scan target, pool URL, worker, pool password default | old-control-only metadata |
| `GET /api/miners/registry` | Rare/manual or bdag_exporter input | saved miner registry | custom read-only bdag_exporter metric |
| `GET /api/token-required` | Rare/manual | action token requirement | old-control-only |

Do not scrape mutating endpoints:

- `POST /api/action`
- `POST /api/miners/scan`
- `POST /api/miners/configure`
- `POST /api/miners/save-auth`

## Logs For Loki

| Log source | Location/access | Signals | Target class |
|---|---|---|---|
| Pool container logs | Docker logs for `asic-pool` | valid shares, submits, stale submits, job notify, template freeze, duplicate blocks, block submit success/error, RPC refused | Loki/log-derived signal |
| Node container logs | Docker logs for `bdag-miner-node-1/2` | imported blocks, best main order, peer-ahead/sync delta, template errors, nonce-too-low, critical/fatal, P2P errors | Loki/log-derived signal |
| Watchdog log | `ops/runtime/logs/watchdog.log` | failure mode transitions, repair decisions, cooldown suppression | Loki/log-derived signal |
| Efficiency events | `ops/runtime/logs/efficiency-events.jsonl` | structured incidents: `syncing`, `pool_stall`, `asic_degraded`, `miner_down`, `repair_failed`, `docker_unavailable` | Loki/log-derived signal |
| Action logs | `ops/runtime/logs/action-*.log` | repair command outcomes | Loki/log-derived signal |
| Thermal guard log | `ops/runtime/logs/cpu-thermal-guard.log` | CPU temp, policy, min/max perf percentages | Loki/log-derived signal |
| Dashboard access log | `ops/runtime/dashboard-access.log` | dashboard request audit if populated | Loki/log-derived signal |
| Hourly snapshot log | `ops/runtime/logs/hourly-chain-snapshot.log` | snapshot activity/maintenance windows | Loki/log-derived signal |

Exclude from Loki:

- `asic-pool/.env`
- `ops/runtime/dashboard-token.txt`
- `ops/runtime/miner-admin-password.txt`
- `ops/runtime/miner-backups/**`
- `data/**`
- wallet/private-key material

## Runtime JSON And JSONL

| File | Signals | Target class |
|---|---|---|
| `ops/runtime/watchdog-state.json` | current watchdog state, counters, last failures, repair timestamps | custom read-only bdag_exporter metric |
| `ops/runtime/latest-action.json` | latest repair/action status | custom read-only bdag_exporter metric plus Loki |
| `ops/runtime/earnings-snapshots.jsonl` | earnings history for trends | Loki/log-derived signal or bdag_exporter |
| `ops/runtime/global-history.jsonl` | global pool/chain history | Loki/log-derived signal or bdag_exporter |
| `ops/runtime/global-cache.json` | latest global chain scan cache | json_exporter from old API or bdag_exporter |
| `ops/runtime/price-cache.json` | latest price source cache | json_exporter from old API or bdag_exporter |
| `ops/runtime/global-pool-labels.json` | address labels | custom read-only bdag_exporter config input |

## Database Sources

| Source | Current access | Signals | Target class |
|---|---|---|---|
| `pool-db` PostgreSQL | `docker exec pool-db psql` in `pool_ops.py` | credits totals, paid/pending credits, per-address credits, block counts/status/rewards | custom read-only bdag_exporter metric |
| PostgreSQL health | postgres_exporter | DB up, connection and table health | standard exporter metric |

The dashboard currently uses SQL over `credits` and `blocks`. Use postgres_exporter for database health, but use a scoped read-only exporter or custom query config for business counters.

## Network And API Sources

| Source | Current access | Signals | Target class |
|---|---|---|---|
| BlockDAG node JSON-RPC | `eth_syncing`, `eth_getBlockByNumber`, `eth_getBalance` | sync progress, latest block, block header clustering, wallet balance | custom read-only bdag_exporter metric |
| ASIC miner HTTP API | `/mcb/status`, `/mcb/pools`, `/mcb/cgminer?cgminercmd=devs` | miner config, health, temp/fan/hashrate, accepted/rejected/hw errors | custom read-only bdag_exporter metric |
| External price feeds | Coinstore, Pionex, Bitmart, USD/ZAR API | BDAG/USD/ZAR prices and source health | json_exporter from old API initially |
| Explorer APIs | bdagscan/blockscout/RPC fallback | wallet balance cross-check | json_exporter from old API initially |
| Peer geolocation | `ipwho.is` plus cache | best-effort location guess | old-control-only enrichment; avoid alert dependency |

## Port Inventory

| Port | Current owner | Purpose | Observability handling |
|---:|---|---|---|
| `3334` | `asic-pool` | Stratum pool | blackbox TCP probe; pool logs via Loki |
| `38131` | `rpc-failover` | BlockDAG RPC failover | blackbox HTTP/TCP probe |
| `5432` | `pool-db` | PostgreSQL | postgres_exporter and blackbox TCP |
| `6061` | `bdag-miner-node-1` | native metrics mapped from container `6060` | Prometheus scrape `/debug/metrics/prometheus` |
| `6062` | `bdag-miner-node-2` | native metrics mapped from container `6060` | Prometheus scrape `/debug/metrics/prometheus` |
| `8151` | `bdag-miner-node-1` | P2P | blackbox TCP only if useful |
| `8152` | `bdag-miner-node-2` | P2P | blackbox TCP only if useful |
| `8088` | old dashboard | control dashboard/API | blackbox HTTP probe; json_exporter read endpoints |

## Recommended Phase 2 Inputs

- Keep new observability ports separate from all ports above.
- Default Grafana/Prometheus/Loki bindings to loopback or authenticated LAN-only access.
- Use read-only Docker socket access only if required and documented; prefer cAdvisor where possible.
- Rate-limit old dashboard/API scrapes; avoid using old API as the only long-term metrics source.
- Treat all ASIC interactions as read-only in observability code.
