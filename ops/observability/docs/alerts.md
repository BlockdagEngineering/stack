# Alerts

Alert rules live in `prometheus/alerts.yml`. They notify only. They do not perform restarts, repairs, miner configuration, or node control.

## Critical Alerts

| Alert | Condition | Duration | Purpose |
| --- | --- | --- | --- |
| `BDAGPoolValidShareStall` | `bdag_pool_share_stall == 1` or last valid share age over 300 seconds | 2m | Detect pool-wide loss of accepted shares. |
| `BDAGMinerDown` | `bdag_miner_up == 0` | 2m | Detect miner degradation beyond the existing two-minute repair threshold. |
| `BDAGStratumEndpointDown` | blackbox TCP probe to stratum fails | 1m | Detect unreachable pool endpoint. |
| `BDAGDashboardAPIUnavailable` | old dashboard API or HTTP probe fails | 2m | Detect loss of the API backing BlockDAG-specific metrics. |
| `BDAGNodeTemplateErrors` | recent node template errors above zero | 2m | Detect template failures that can degrade work allocation. |
| `BDAGActiveRPCPrimaryTemplateFailure` | active RPC primary is template-failing | 1m | Detect when HAProxy is still pointed at a bad template source. |
| `BDAGRPCSwitchRecommended` | router recommends an RPC primary switch | 1m | Detect a router decision that should be repaired by the watchdog. |
| `BDAGRPCPrimarySuboptimal` | router sees node-specific primary degradation | 2m | Detect the pre-drop state seen before the 2026-05-09 Blocks/hour jump. |
| `BDAGP2PGuardUnavailable` | `bdag_p2p_guard_up == 0` | 5m | Detect loss of the P2P/network health layer. |
| `BDAGP2PActivePrimaryDegraded` | active primary P2P score under 80 | 2m | Detect when the mining RPC path is using a poor P2P backend. |
| `BDAGP2PNodeDegraded` | any node P2P score under 80 | 5m | Detect degraded standby health before failover quality is needed. |

## Warning Alerts

| Alert | Condition | Duration | Purpose |
| --- | --- | --- | --- |
| `BDAGMinerNotConfigured` | `bdag_miner_configured == 0` | 2m | Detect miner pointed at the wrong endpoint or worker. |
| `BDAGMinerLowWorkShare` | connected miner under 5% work share | 10m | Detect underperforming miners without alerting on short variance. |
| `BDAGNodeSyncDrift` | node block lag or remaining sync blocks over 5 | 3m | Detect nodes falling behind. |
| `BDAGNodeImportStale` | node import age over 180 seconds | 3m | Detect stale import flow. |
| `BDAGPoolJobNotifyStall` | job stall or job age over 180 seconds | 2m | Detect pool not issuing fresh work. |
| `BDAGBlockSubmitErrors` | recent block submit errors above zero | 1m | Detect failed block submissions. |
| `BDAGBlockSubmitErrorStorm` | block submit error storm flag is set | 1m | Detect a high error-to-success submit ratio. |
| `BDAGPoolQualityPressure` | router pool-quality pressure flag is set | 2m | Detect elevated submit/stale/overdue or low accepted-share ratios before a hard stall. |
| `BDAGPrimaryP2PResets` | active RPC primary has at least 10 recent P2P resets | 1m | Detect the exact class of active-primary degradation that preceded the 01:08 performance gain. |
| `BDAGP2PPeerCountLow` | native peer count below 4 | 5m | Detect weak peer diversity on either backend node. |
| `BDAGMiningDefaultRouteChanged` | default route is not on the wired mining interface | 1m | Detect accidental mining traffic migration to Wi-Fi or ZeroTier. |
| `BDAGMiningGatewayLatencyHigh` | gateway RTT over 20ms or ping loss | 2m | Detect LAN/router latency that can hurt P2P and ASIC traffic. |
| `BDAGStaleJobCandidateStorm` | stale-job candidate storm flag is set | 1m | Detect ASIC candidates being found against stale jobs. |
| `BDAGDuplicateBlockSpike` | duplicate block submits exceed 5 | 2m | Detect wasted submit work or stale template racing. |
| `BDAGStaleSubmitIncrease` | recent stale submits above zero | 3m | Detect stale-work symptoms. |
| `BDAGHighCPUTemperature` | thermal zone over 85C | 5m | Detect thermal risk to sustained performance. |
| `BDAGDiskSpaceLow` | root filesystem free below 15% | 10m | Detect disk-pressure risk to DB/node performance. |
| `BDAGContainerRestarted` | mining container start time changes | immediate | Detect unexpected production container restarts. |
| `BDAGPostgresExporterDown` | PostgreSQL exporter target down | 2m | Detect DB monitoring blind spot. |
| `BDAGLokiIngestionUnavailable` | Loki metrics target down | 2m | Detect log ingestion or Loki monitoring blind spot. |

## Alertmanager

`alertmanager/alertmanager.yml` groups alerts locally with a placeholder receiver named `local-log`. No external notifier is configured yet. During staged run, add a notification receiver only after confirming the stack overhead is acceptable.

## Known Gaps

- `BDAGThermalGuardDisabled` is not implemented yet because the current thermal guard state is log-derived. It should be added once Alloy/Loki recording rules or a tiny read-only runtime-status metric are available.
- A deeper Loki ingestion quality alert is deferred until staged run because it needs live Loki metrics volume. The current first-pass alert covers the Loki metrics endpoint being unavailable.
- Alert thresholds are intentionally conservative because discovery found live template failures, pool stalls, and repairs in recent logs. Tighten only after a 24-hour baseline.
