# Metrics Catalog

This catalog documents the first-pass metrics used by the parallel Grafana/Prometheus stack. The custom BlockDAG exporter is read-only and scrapes the existing dashboard APIs at Prometheus cadence. It does not call repair, restart, scan, or ASIC configuration endpoints.

## Primary Exporter

File: `exporters/bdag_exporter/bdag_exporter.py`

Default source:

- `GET /api/status`
- `GET /api/earnings`
- `GET /api/global`

Default Prometheus job:

- `bdag-exporter`, target `bdag-exporter:9108`

The exporter caches old dashboard reads to reduce production dashboard load:

| Endpoint | Default cache |
| --- | ---: |
| `/api/status` | 30 seconds |
| `/api/earnings` | 300 seconds |
| `/api/global` | 300 seconds |

API scrape health:

| Metric | Type | Labels | Source | Meaning |
| --- | --- | --- | --- | --- |
| `bdag_dashboard_api_up` | gauge | `api` | all dashboard APIs | `1` when the source API returned parseable JSON. |
| `bdag_dashboard_api_scrape_seconds` | gauge | `api` | all dashboard APIs | API request duration. |
| `bdag_exporter_build_info` | gauge | `version` | exporter | Exporter build marker. |

Stack and sync:

| Metric | Type | Labels | Source | Meaning |
| --- | --- | --- | --- | --- |
| `bdag_stack_status` | gauge | `state` | `/api/status` | Overall old dashboard state as `1` ok, `0.5` degraded/syncing, `0` down/unknown. |
| `bdag_sync_progress_percent` | gauge | none | `/api/status` | Overall chain sync percentage. |
| `bdag_sync_remaining_blocks` | gauge | none | `/api/status` | Overall remaining sync gap. |
| `bdag_node_block_lag` | gauge | none | `/api/status` | Height lag between configured nodes. |
| `bdag_node_main_order_lag` | gauge | none | `/api/status` | Main-order lag between configured nodes. |
| `bdag_node_recent_importers` | gauge | none | `/api/status` | Nodes with recent import activity. |
| `bdag_node_latest_block` | gauge | `node` | `/api/status` | Latest block observed per managed node. |
| `bdag_node_last_import_age_seconds` | gauge | `node` | `/api/status` | Seconds since last import log per node. |
| `bdag_node_template_errors_recent` | gauge | `node` | `/api/status` | Recent mining template error count from logs. |
| `bdag_node_p2p_stream_errors_recent` | gauge | `node` | `/api/status` | Recent P2P stream reset/error count from logs. |

Pool:

| Metric | Type | Labels | Source | Meaning |
| --- | --- | --- | --- | --- |
| `bdag_pool_connected_miners` | gauge | none | `/api/status` | Connected miners. |
| `bdag_pool_managed_miners` | gauge | none | `/api/status` | Managed miners. |
| `bdag_pool_valid_shares_recent` | gauge | none | `/api/status` | Valid shares in the recent log window. This is not monotonic. |
| `bdag_pool_submits_recent` | gauge | none | `/api/status` | Submit events in the recent log window. This is not monotonic. |
| `bdag_pool_stale_submits_recent` | gauge | none | `/api/status` | Stale submit events in the recent log window. |
| `bdag_pool_block_submit_success_recent` | gauge | none | `/api/status` | Successful block submits in the recent log window. |
| `bdag_pool_block_submit_error_recent` | gauge | none | `/api/status` | Failed block submits in the recent log window. |
| `bdag_pool_last_valid_share_age_seconds` | gauge | none | `/api/status` | Age of newest valid share. |
| `bdag_pool_last_job_notify_age_seconds` | gauge | none | `/api/status` | Age of newest stratum job notify. |
| `bdag_pool_last_block_submit_age_seconds` | gauge | none | `/api/status` | Age of newest block submit. |
| `bdag_pool_share_stall` | gauge | none | `/api/status` | Old dashboard share-stall detector state. |
| `bdag_pool_job_stall` | gauge | none | `/api/status` | Old dashboard job-stall detector state. |
| `bdag_pool_template_frozen` | gauge | none | `/api/status` | Old dashboard template-frozen detector state. |

Miners:

| Metric | Type | Labels | Source | Meaning |
| --- | --- | --- | --- | --- |
| `bdag_miner_managed_count` | gauge | none | `/api/status` | Managed miner count. |
| `bdag_miner_ok_count` | gauge | none | `/api/status` | Miners in OK state. |
| `bdag_miner_connected_count` | gauge | none | `/api/status` | Connected miners. |
| `bdag_miner_up` | gauge | `miner`, `ip`, `mac`, `worker` | `/api/status` | Miner status is OK. |
| `bdag_miner_connected` | gauge | `miner`, `ip`, `mac`, `worker` | `/api/status` | Miner is pool-active or connected. |
| `bdag_miner_configured` | gauge | `miner`, `ip`, `mac`, `worker` | `/api/status` | Miner is configured for expected pool. |
| `bdag_miner_work_percent` | gauge | `miner`, `ip`, `mac`, `worker` | `/api/status` | Accepted work share percentage. |
| `bdag_miner_last_share_age_seconds` | gauge | `miner`, `ip`, `mac`, `worker` | `/api/status` | Age of newest share from miner. |
| `bdag_miner_last_submit_age_seconds` | gauge | `miner`, `ip`, `mac`, `worker` | `/api/status` | Age of newest submit from miner. |
| `bdag_miner_shares_recent` | gauge | `miner`, `ip`, `mac`, `worker` | `/api/status` | Recent shares in old dashboard log window. |
| `bdag_miner_submits_recent` | gauge | `miner`, `ip`, `mac`, `worker` | `/api/status` | Recent submits in old dashboard log window. |
| `bdag_miner_blocks_found_recent` | gauge | `miner`, `ip`, `mac`, `worker` | `/api/status` | Recent blocks found in old dashboard log window. |
| `bdag_miner_hashrate_ghs` | gauge | `miner`, `ip`, `mac`, `worker` | `/api/status` | ASIC reported current hashrate when available. |
| `bdag_miner_avg_hashrate_ghs` | gauge | `miner`, `ip`, `mac`, `worker` | `/api/status` | ASIC reported average hashrate when available. |
| `bdag_miner_hw_error_ratio` | gauge | `miner`, `ip`, `mac`, `worker` | `/api/status` | ASIC hardware error ratio when available. |

Earnings and global view:

| Metric | Type | Labels | Source | Meaning |
| --- | --- | --- | --- | --- |
| `bdag_wallet_balance_bdag` | gauge | none | `/api/earnings` | Wallet BDAG balance from old dashboard. |
| `bdag_wallet_recent_bdag_per_hour` | gauge | none | `/api/earnings` | Recent estimated wallet BDAG per hour. |
| `bdag_wallet_avg_bdag_per_hour` | gauge | none | `/api/earnings` | Tracked average wallet BDAG per hour. |
| `bdag_wallet_24h_bdag` | gauge | none | `/api/earnings` | Estimated wallet BDAG for last 24 hours. |
| `bdag_price_usd` | gauge | none | `/api/earnings` | BDAG/USD from old dashboard price feed. |
| `bdag_price_zar` | gauge | none | `/api/earnings` | BDAG/ZAR from old dashboard price feed. |
| `bdag_miner_estimated_usd_per_hour` | gauge | `miner`, `ip`, `mac` | `/api/earnings` | Estimated USD per hour by miner. |
| `bdag_miner_estimated_bdag_per_hour` | gauge | `miner`, `ip`, `mac` | `/api/earnings` | Estimated BDAG per hour by miner. |
| `bdag_global_latest_block` | gauge | none | `/api/global` | Latest global block scanned. |
| `bdag_global_unique_miners` | gauge | none | `/api/global` | Unique global miners in scan window. |
| `bdag_global_pool_work_percent` | gauge | `pool`, `address` | `/api/global` | Global block share percentage by pool cluster. |
| `bdag_global_pool_blocks` | gauge | `pool`, `address` | `/api/global` | Blocks per pool cluster in scan window. |
| `bdag_global_pool_estimated_usd_per_hour` | gauge | `pool`, `address` | `/api/global` | Estimated USD per hour by pool cluster. |

RPC router:

| Metric | Type | Labels | Source | Meaning |
| --- | --- | --- | --- | --- |
| `bdag_rpc_router_should_switch` | gauge | none | `/api/router` | `1` when the app-aware router recommends changing the active RPC primary. |
| `bdag_rpc_router_current_suboptimal` | gauge | none | `/api/router` | `1` when the current primary has node-specific degraded score or a meaningful healthier alternate. |
| `bdag_rpc_router_score_delta` | gauge | none | `/api/router` | Recommended node score minus current primary score. |
| `bdag_rpc_router_pool_quality_pressure` | gauge | none | `/api/router` | `1` when recent submit/share quality crosses pre-stall warning thresholds. |
| `bdag_rpc_router_hard_pool_pressure` | gauge | none | `/api/router` | `1` when the pool has a hard stall/storm/refused condition. |
| `bdag_rpc_router_block_error_ratio` | gauge | none | `/api/router` | Recent block submit errors per successful block submit. |
| `bdag_rpc_router_stale_job_ratio` | gauge | none | `/api/router` | Recent stale-job candidates per successful block submit. |
| `bdag_rpc_router_tip_overdue_ratio` | gauge | none | `/api/router` | Recent tip-overdue submits per successful block submit. |
| `bdag_rpc_router_valid_share_ratio` | gauge | none | `/api/router` | Recent accepted shares per submit. |
| `bdag_rpc_node_score` | gauge | `node` | `/api/router` | Application-aware score for each RPC backend node. |
| `bdag_rpc_node_primary` | gauge | `node` | `/api/router` | `1` when the node is the current HAProxy primary. |
| `bdag_rpc_node_recommended` | gauge | `node` | `/api/router` | `1` when the node is the router's recommended primary. |
| `bdag_rpc_node_template_failing` | gauge | `node` | `/api/router` | `1` when the router sees recent template failure on the node. |
| `bdag_rpc_node_p2p_stream_errors` | gauge | `node` | `/api/router` | Router-visible recent P2P stream resets by node. |

P2P and network guard:

| Metric | Type | Labels | Source | Meaning |
| --- | --- | --- | --- | --- |
| `bdag_p2p_guard_up` | gauge | none | `/api/p2p` | `1` when the P2P guard has current state. |
| `bdag_p2p_guard_state` | gauge | `state` | `/api/p2p` | Guard state as `1` ok, `0.5` warning, `0` critical/unknown. |
| `bdag_p2p_overall_score` | gauge | none | `/api/p2p` | Lowest node P2P score. |
| `bdag_p2p_active_primary_score` | gauge | none | `/api/p2p` | P2P score of the active RPC primary. |
| `bdag_p2p_best_alternate_score` | gauge | none | `/api/p2p` | P2P score of the best standby backend. |
| `bdag_p2p_node_score` | gauge | `node` | `/api/p2p` | Per-node P2P score based on resets, template health, import freshness, and peer count. |
| `bdag_p2p_node_active_primary` | gauge | `node` | `/api/p2p` | `1` for the active RPC primary. |
| `bdag_p2p_node_public_peer_count` | gauge | `node` | `/api/p2p` | Public peer IPs observed from node sockets. |
| `bdag_p2p_node_native_peer_count` | gauge | `node` | native node metrics | Native P2P peer count. |
| `bdag_p2p_node_dial_errors_delta` | gauge | `node` | native node metrics | Native P2P dial error delta since the previous guard sample. |
| `bdag_p2p_pool_valid_share_ratio` | gauge | none | `/api/p2p` | Accepted shares per submit in the current guard sample. |
| `bdag_p2p_pool_block_error_ratio` | gauge | none | `/api/p2p` | Block submit errors per successful block submit. |
| `bdag_p2p_pool_stale_job_ratio` | gauge | none | `/api/p2p` | Stale-job candidates per successful block submit. |
| `bdag_p2p_pool_tip_overdue_ratio` | gauge | none | `/api/p2p` | Tip-overdue submits per successful block submit. |
| `bdag_network_default_route_ok` | gauge | `interface` | `/api/p2p` | `1` when the default route is on the wired mining interface. |
| `bdag_network_gateway_ping_up` | gauge | `gateway` | `/api/p2p` | Default gateway ping success. |
| `bdag_network_gateway_rtt_ms` | gauge | `gateway` | `/api/p2p` | Default gateway RTT in milliseconds. |
| `bdag_lan_miner_ping_up` | gauge | `miner`, `ip` | `/api/p2p` | LAN miner ping success in the guard sample. |
| `bdag_lan_miner_ping_rtt_ms` | gauge | `miner`, `ip` | `/api/p2p` | LAN miner RTT in milliseconds. |

## Standard Exporters

| Job | Source | Purpose |
| --- | --- | --- |
| `bdag-native-node` | `host.docker.internal:6061` and `:6062` at `/debug/metrics/prometheus` | Native node metrics such as `Blockdag_mainheight`, `Blockdag_tips_total`, `Blockdag_unsequenced`, DB compaction and disk metrics. |
| `node-exporter` | host mount read-only | Host CPU, RAM, disk, network, thermal zones, and CPU frequency. |
| `cadvisor` | Docker read-only mounts | Container CPU, memory, IO, and restart visibility. |
| `postgres-exporter` | configured read-only DSN | Database availability and PostgreSQL health. |
| `blackbox-exporter` | TCP/HTTP probes | Old dashboard HTTP and stratum TCP reachability. |

## Cardinality Rules

- Miner labels use dashboard display names plus IP/MAC for correlation, not dynamic URLs.
- Loki labels must stay coarse: job, container, service, level.
- Wallet and pool addresses are labels only on global/earnings metrics where series count is bounded.
- Rolling log-window values are gauges, not counters.
