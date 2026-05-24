# Pool Stack Agent Notes

## No-Miner Sync-Only Invariant

When a deployment has no managed or connected miners, node services must run as
sync-only receivers. Do not enable `--miner`, `--allowminingwhennearlysynced`,
`modules=miner`, or mining-template probes by default on no-miner hosts.

Mining/template flags are opt-in only for deployments with actual managed
miners. If a node is behind tip and `miner_health.connected_count == 0` or
`miner_health.managed_count == 0`, preserve sync-only behavior and prioritize
chain catch-up over template generation.

## Catch-Up Priority Invariant

When dashboard status or `sync_progress.status` is `syncing`, chain import is
the priority. Nodes should receive the strongest CPU and IO priority until they
are caught up. Hosts with active miners may keep the pool/router path alive, but
node catch-up still wins scheduling priority. Hosts with no miners must idle or
stop pool/router/database work and stay in sync-only mode.

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
