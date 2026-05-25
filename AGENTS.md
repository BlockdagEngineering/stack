# Pool Stack Agent Notes

## Release Candidate Dashboard Source

The only dashboard repository for this release candidate is
`BlockdagEngineering/pool-dashboard`. Its `main` branch was replaced with the
live Python operations dashboard captured from
`/home/jeremy/blockdag-asic-pool/ops` at commit
`6585347bfa78a1e6ed2a6178eaa38c7ccac9d022`.

Do not reintroduce the retired standalone read-only dashboard, command-center
prototype, or Grafana/Prometheus/Loki observability dashboard as RC dashboard
sources. The stack repository still owns full-node, pool, Docker, installer,
and chain-sync packaging. The dashboard repository owns the dashboard/control
plane source, and any dashboard code imported into this RC must preserve the
hardening gates in `scripts/validate-pi5-restart-hardening.sh`.

## No-Miner Sync-Only Invariant

When a deployment has no managed or connected miners, node services must run as
sync-only receivers. Do not enable `--miner`, `--allowminingwhennearlysynced`,
`modules=miner`, or mining-template probes by default on no-miner hosts.

Mining/template flags are opt-in only for deployments with actual managed
miners. If a node is behind tip and `miner_health.connected_count == 0` or
`miner_health.managed_count == 0`, preserve sync-only behavior and prioritize
chain catch-up over template generation.

`ops/pool_ops.py` must skip live `getBlockTemplate` probe RPCs entirely when
both managed and connected miner counts are zero. Suppressing warnings after
probing is not enough; no-miner mode should not spend node CPU, pool RPC, or USB
I/O on mining-template readiness work.

## Catch-Up Priority Invariant

When dashboard status or `sync_progress.status` is `syncing`, chain import is
the priority. Nodes should receive the strongest CPU and IO priority until they
are caught up. Hosts with active miners may keep the pool/router path alive, but
node catch-up still wins scheduling priority. Hosts with no miners must idle or
stop pool/router/database work and stay in sync-only mode.

When any managed node is more than 1000 blocks behind the observed network tip,
do not let multiple nodes compete for catch-up IO. The sync coordinator must
pause the laggiest running node and let exactly one selected leader sync alone
until the leader is within 1000 blocks. During that one-node catch-up window,
the selected leader must receive the highest Docker CPU shares and block IO
weight available on the host. Do not weaken this behavior or reintroduce a
productive-mining exception without a measured release-candidate test.

Until the FastSync nil-preprocessed-block fix is deployed in the node image,
prefer `BDAG_FASTSYNC_PREPROCESS_WORKERS=1` on Pi catch-up hosts. The parallel
preprocessor has previously panicked in `processFastBlockRange`; uptime and
steady catch-up beat the small parallel precheck speedup.

## Self-Healing Release Invariants

The Pi5 release candidate must install `bdag-stack-sentinel.timer` and the
dashboard/watchdog/peer/chain guards by default. A stopped `pool-db`,
`rpc-failover`, or `asic-pool` container is a stack failure even when there are
no miners. No-miner mode means no mining work is sent; it does not mean services
are allowed to stay down.

Dashboard block height must come from the node chain RPC `getBlockCount` only.
`getMainChainHeight`, template height, log imports, fan-in metrics, and peer
lead values are diagnostics and must not be displayed as the node block count.
Keep `scripts/validate-pi5-restart-hardening.sh` enforcing this so future drift
cannot reintroduce mixed height sources.

## Low-I/O Monitoring And Repair Invariants

Recurring guards and dashboards must prefer the shared `collect_status_cached`
path unless they explicitly need an uncached one-shot diagnostic. This prevents
dashboard refreshes, watchdog ticks, sync coordination, P2P guard, and startup
checks from stampeding Docker logs and node RPC at the same time.

The node entrypoint must not recursively `chown` the full chain datadir on every
start. Keep ownership repair conditional through `BDAG_ENTRYPOINT_CHOWN_MODE`
and only run the second repair pass after FastSnap import has actually mutated
the datadir.

The stack sentinel must be single-flight and must never build or pull images as
part of automatic repair. Recreate repairs must use Compose with
`--no-build --pull never` so a constrained Pi cannot start compiling, fetching,
or changing provenance during a liveness repair.

JSONL histories used by the dashboard should append each sample and compact only
at a bounded threshold. Do not reintroduce full-history rewrite loops for every
sample on the Pi USB data path.

Optional background work must respect `background_maintenance_decision()`.
Hourly snapshot staging, FastSnap seed builds, and global dashboard blockchain
scans must defer while the node is catching up or host IO/CPU pressure is above
the configured release thresholds. Chain import and live mining are the primary
jobs; background freshness work is allowed to lag until the host is healthy.

## FastSync Candidate Ordering

New nodes must prefer nearby FastSync candidates before public internet seeds.
The default ordering is:

1. LAN candidates from `BDAG_FASTSYNC_LAN_PEERS` or addresses matching
   `BDAG_FASTSYNC_LAN_PREFIXES`.
2. Private/VPN candidates from `BDAG_FASTSYNC_VPN_PEERS` or private-address
   multiaddrs.
3. Public internet candidates from `BDAG_FASTSYNC_PUBLIC_PEERS` plus any
   public entries discovered in generic `BDAG_FASTSYNC_PEERS`,
   `BDAG_FASTSNAP_PEERS`, `BOOTSTRAP_PEER_ADDRESSES`, and `node.conf`
   `addpeer` lines.

Peer candidates must be complete multiaddrs with peer IDs. Do not replace this
ordering with public-first bootstrapping in future RCs.
