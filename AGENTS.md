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

Fresh installs assume zero miner sources. Do not hard-code one, four, five, or
any other miner count into release defaults, installers, watchdog repairs,
dashboard success criteria, or tests. Miner sources are configured after initial
install and sync, and the runtime must handle 0..N ASIC or Stratum miners.

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

USB-backed ASIC router/mining hosts must not serve bulk sync. Keep
`BDAG_NO_FASTSYNC_SERVE=auto` or set it explicitly to `1` on Pi5 profiles where
chain data lives on USB and the same host is the ASIC DHCP/NAT gateway. These
nodes may consume sync and relay blocks, but they must not advertise or handle
FastSync range, snapshot, or artifact serving while mining; disk latency on USB
directly reduces paid-block conversion.

## Five ASIC Template Conversion Invariant

For five-X100 local mining hosts and other multi-miner deployments, connected
miner count and raw hash activity are not enough. The release success metric is
accepted block conversion per miner-hour. The pool must keep one canonical
mining-template epoch at a time, and backend switches, catch-up maintenance,
and clean-job broadcasts must be atomic from the miner point of view.

Do not re-enable active/active template fan-in as a quick fix for low output.
The 2026-05-25 regression showed that five ASICs can amplify stale-parent,
tip-overdue, duplicate-block, invalidated-job, and non-current-job losses when
template epochs or routing are unstable. During one-node catch-up, a paused
follower is maintenance standby; if the leader is near tip and accepting blocks,
the dashboard/router path must not treat the paused follower as global mining
unavailability.

Keep the RC guard in `docs/five-asic-template-conversion-guard.html` current.
The guard is conditional on observed/configured miner sources; it must not make
five miners the default install assumption. Future fixes must use
MAC-address-based ASIC attribution for diagnostics; IP addresses, worker
labels, ports, and display names remain ephemeral.

For physical ASIC identity, MAC address is the primary key. The dashboard miner
column must default to the full MAC address. If an operator assigns a human name,
render it with the last three hex characters of the MAC as the suffix
(`Name-abc`), never an IP suffix. Release defaults must not auto-generate or ship
site-specific miner names; fresh installs start with no custom miner names and
only display configured names after an operator explicitly adds them.

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

Pool block-candidate submit fanout must remain a candidate-only hot-path
optimization. Current pool releases use the RPC router with
`POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT=true`; `POOL_SUBMIT_RPC_URLS` remains
configured for compatibility with older binaries. Normal shares must not fan
out. Valid block candidates should return to the miner after the first accepted
endpoint while slower endpoint outcomes are recorded asynchronously. Keep the
default release value to one endpoint on single-node hosts.

Keep Issue #26 final release mitigations in
`docs/final-release-issue-26-checklist.md` current when changing source repo
pins, installer reset behavior, V2 sync defaults, or release packaging.

## Low-I/O Monitoring And Repair Invariants

Recurring guards and dashboards must prefer the shared status sampler and
`collect_status_cached` path unless they explicitly need an uncached one-shot
diagnostic. This prevents dashboard refreshes, watchdog ticks, sync
coordination, P2P guard, and startup checks from stampeding Docker logs and node
RPC at the same time. Hard diagnostic paths can force a direct sample with
`max_age_seconds=0`; routine loops should not.

The node entrypoint must not recursively `chown` the full chain datadir on every
start. Keep ownership repair conditional through `BDAG_ENTRYPOINT_CHOWN_MODE`
and only run the second repair pass after FastSnap import has actually mutated
the datadir.

The stack sentinel must be single-flight and must never build or pull images as
part of automatic repair. Recreate repairs must use Compose with
`--no-build --pull never` so a constrained Pi cannot start compiling, fetching,
or changing provenance during a liveness repair.

Live runtime update tooling must validate post-restart health before declaring
success. Keep `ops/deploy-live-runtime-update.sh` waiting for dashboard API
recovery, fresh watchdog state when the watchdog is restarted, and running
critical containers. If that post-deploy health gate fails, copied files must be
rolled back from the backup manifest.

JSONL histories used by the dashboard should append each sample and compact only
at a bounded threshold. Do not reintroduce full-history rewrite loops for every
sample on the Pi USB data path.

Recurring timers must include modest `RandomizedDelaySec` jitter so node-child
guard, sync coordinator, incident reporter, runtime priority, snapshot, and
peer-discovery work do not wake together and stampede Docker/RPC on constrained
hosts.

Optional background work must respect `background_maintenance_decision()`.
Hourly snapshot staging, FastSnap seed builds, and global dashboard blockchain
scans must defer while the node is catching up or host IO/CPU pressure is above
the configured release thresholds. Chain import and live mining are the primary
jobs; background freshness work is allowed to lag until the host is healthy.

Runtime limits must be platform-adaptive. Do not hard-code Pi-only worker
counts as universal behavior: the stack must support Linux AMD64 and ARM64
first, and installer-supported macOS/Windows Docker hosts where the same Linux
pressure signals may not exist. Use `host_runtime_profile()`,
`adaptive_worker_count()`, and explicit env caps so Pi5/USB hosts stay
conservative while larger AMD64 or ARM64 hosts can expand safely when pressure
is low.

Release packages must prove executable architecture before building or
deploying images. Keep `scripts/verify-release-architecture.py` in the RC path
and run it before image assembly so an AMD64 binary cannot be copied into an
ARM64 Pi package or container by accident. Prefer header-based verification
over host-specific `file` output so the same gate works from Linux, macOS, and
Windows build hosts.

Source-checkout validation must never delete an active local runtime. If a live
machine runs the RC directly from the source checkout, `ops/runtime` can hold
the dashboard environment and sampler state currently used by systemd. Keep
`scripts/validate-rc-local.sh` validating a temporary source copy with a
temporary runtime directory instead of cleaning the checkout in place.

Dashboard collectors must avoid host-only command dependencies for normal
status. Use Python's standard HTTP client for local pool metrics and public
enrichment calls so Linux AMD64, Linux ARM64/Pi5, macOS Docker Desktop, and
Windows Docker Desktop behave consistently once Docker and Python are present.

When operating from source, keep dashboard surfaces explicit: Compose owns the
container dashboard on `9280`; the Python operations dashboard owns the `8088`
control-plane view and must be configured with the real container names and
Docker access for the stack being watched. Do not report a source checkout
healthy until `8088/api/status` points at the intended project root and returns
`overall=ok` or the expected no-miner mode.

## FastSync Candidate Ordering

New nodes must prefer nearby FastSync candidates before public internet seeds.
The release default is `BDAG_FASTSYNC_PEER_ORDERING=tiered-latency`, with this
ordering:

1. LAN candidates from `BDAG_FASTSYNC_LAN_PEERS`, addresses on a currently
   connected non-VPN host subnet, or addresses matching an explicit
   `BDAG_FASTSYNC_LAN_PREFIXES` override.
2. Private/VPN candidates from `BDAG_FASTSYNC_VPN_PEERS` or private-address
   multiaddrs.
3. Public internet candidates from `BDAG_FASTSYNC_PUBLIC_PEERS` plus any
   public entries discovered in generic `BDAG_FASTSYNC_PEERS`,
   `BDAG_FASTSNAP_PEERS`, `BOOTSTRAP_PEER_ADDRESSES`, and `node.conf`
   `addpeer` lines.

Peer candidates must be complete multiaddrs with peer IDs. On single-node
ASIC-router hosts, the direct ASIC Ethernet subnet (`BDAG_ASIC_LAN_CIDRS`,
default `192.168.50.0/24`) is not a blockchain P2P LAN unless
`BDAG_ALLOW_ASIC_LAN_P2P=1` is explicitly set. ASICs on that subnet are Stratum
clients; adding them to FastSync or P2P peer lists wastes time and can hide the
actual low-latency VPN/LAN node candidates. Do not replace this ordering with
public-first bootstrapping in future RCs.

## Fast Artifact Sync V2 Directory Mode

Directory artifacts are the preferred Fast Artifact Sync V2 bootstrap primitive.
Keep `BDAG_FASTSNAP_DIRECTORY_MODE=1` as the release default so new nodes use
verified file chunks and atomic directory install when peers offer it, while
retaining `.bdsnap` archive fallback for older seeds. Serving a hot-stage
directory artifact is opt-in through `BDAG_FASTSYNC_ARTIFACT_DIRECTORY` and
`BDAG_FASTSYNC_ARTIFACT_MANIFEST`; nodes bootstrapped from a directory artifact
auto-serve that verified checkpoint from `artifact.manifest.json`. Do not make
future changes that force archive assembly back into the default fast path.
