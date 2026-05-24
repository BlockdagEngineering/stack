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
