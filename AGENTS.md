# Pool Stack Agent Notes

## Agent skills

### Issue tracker

Issues and PRDs are tracked in GitHub Issues for `BlockdagEngineering/stack` using the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the five canonical triage labels exactly as named. See `docs/agents/triage-labels.md`.

### Domain docs

Multi-context repo with shared context in sibling `../codex-memory`; read that memory repo before repo-local context docs when present. See `docs/agents/domain.md`.

## Release Candidate Dashboard Source


The only dashboard repository for this release candidate is
`BlockdagEngineering/dashboard2`. Release builds must always use its `main`
branch.

Do not reintroduce the retired standalone read-only dashboard, command-center
prototype, or Grafana/Prometheus/Loki observability dashboard as RC dashboard
sources. The stack repository still owns full-node, pool, Docker, installer,
and chain-sync packaging. The dashboard repository owns the dashboard/control
plane source, and any dashboard code imported into this RC must preserve the
release build gates in `scripts/validate-release-build.sh`.

## Stack Configuration Source Of Truth

Stack-owned deployment defaults live in `ops/config/stack-defaults.env`.
Examples, compose fallback expressions, release installers, dashboard runtime
helpers, watchdogs, and validators are projections of those defaults and must
stay coherent through `scripts/validate-stack-defaults.py`. Do not reintroduce
hard-coded copies of values such as dashboard scan windows, status cache
durations, sidecar rsync bandwidth caps, catch-up thresholds, or pool template
pressure defaults in installer or validation code.

## No-Miner Sync-Only Invariant

When a deployment has no managed or connected miners, node services must run as
sync-only receivers. Do not enable `--miner`, `--allowminingwhennearlysynced`,
`modules=miner`, or mining-template probes by default on no-miner hosts.

Mining/template flags are opt-in only for deployments with actual managed
miners. If a node is behind tip and `miner_health.connected_count == 0` or
`miner_health.managed_count == 0`, preserve sync-only behavior and prioritize
chain catch-up over template generation.

When actual ASIC demand is present, the opposite invariant applies: the selected
node must be able to build templates and accept block candidates during brief
false `Client in initial download` windows. Runtime repairs must enable
`BDAG_ENABLE_NODE_MINING=1`, `BDAG_NODE_MODULES=Blockdag,miner`, and
`BDAG_NODE_MINING_ARGS` containing `--miner` and a non-zero
`--miningaddr=<wallet>`.
Keep both `Blockdag` and `miner` RPC modules exposed when miner demand is
present; this release image still enables mining work through the `--miner`
node argument.
Do not add `--allowminingwhennearlysynced` or `--allowsubmitwhennotsynced`.
Those bypass flags can make template health report ready while the node has
stale or absent P2P mining freshness; future runtime repair must remove them
and keep the pool stopped until direct readiness passes.
For constrained USB/router appliances also keep `--maxinbound=1`, because
inbound catch-up peers and artifact requests have caused rewind/sync churn that
converted valid ASIC work into `node-syncing`, `tip-overdue`, and
`invalidated_job` losses.

Keep pool `getBlockTemplate` pressure below the node RPC client ceiling. Do not
override `POOL_GBT_MIN_INTERVAL_MS` below `1000`, do not override
`POOL_GBT_PRESSURE_INTERVAL_MS` below `250`, and keep node-health probes at
least `10` seconds apart unless a measured soak test proves the node can absorb
more frequent RPC traffic while importing and mining.

Any system with USB-backed blockchain data is a FastSync/FastArtifact consumer,
not a source, by default. Keep `SYNC_SOURCE_NODE=0`; do not reintroduce
`NODE_ARGS_APPEND=--fastartifactsync` or artifact serving on low-IO USB hosts.
These nodes must still do normal outbound sync and block relay, but must not
serve bulk range, snapshot, or artifact traffic from the USB chain path unless a
human deliberately overrides the policy for a proven
high-IO source host.

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
are caught up. Hosts with active miners must stop `pool` while the node is in
catch-up, because accepting shares or submitting block candidates against an
unsafe or non-submit-ready backend spends ASIC hash on work that cannot become
paid on-chain blocks. Restart `pool` only through `ops/pool_start_gate.py` after
mainnet, sync, P2P freshness, canonical safety, template health, and submit
readiness all pass. Hosts with no miners must idle or stop pool/database work
and stay in sync-only mode.

The release metric is paid accepted blocks per unit of hash power, not raw
shares, raw submissions, or keeping the Stratum port open. Do not reintroduce a
pool-side-only template pause for catch-up. If `sync_progress.status` is
`syncing` or `catchup_pause`, or status reports `can_submit_blocks=false`, a
running `pool` container is a containment condition to repair, not a healthy
steady state.

When any managed node is more than 1000 blocks behind the observed network tip,
do not let multiple nodes compete for catch-up IO. The sync coordinator must
pause the laggiest running node and let exactly one selected leader sync alone
until the leader is within 1000 blocks. During that one-node catch-up window,
the selected leader must receive the highest Docker CPU shares and block IO
weight available on the host. Do not weaken this behavior or reintroduce a
productive-mining exception without a measured release-candidate test.

Startup seed freshness must have operational slack. If a remote seed is within
`BDAG_SYNC_ACCEPTABLE_STARTUP_LAG_BLOCKS` (default 4000 blocks), start the node
and let P2P catch the tail. Use the recorded copy duration allowance to avoid
repeated full copies; do not recopy just to close an already-acceptable lag.

## Multi-Agent Regression Guard

Before editing this stack, read available Codex memory and current repo docs,
then pull the target branch with `git pull --ff-only`. If the branch cannot
fast-forward cleanly, inspect and resolve deliberately; do not overwrite another
agent's work. Keep successful, validated changes committed and pushed so other
agents do not rediscover or regress the same fix.

When changing mining readiness, catch-up, watchdog, sentinel, or dashboard
status logic, add or update tests that would fail if a future agent leaves
`pool` running while the backend cannot submit paid work. Treat contradictory
comments or docs as bugs and update them in the same change.

## Five ASIC Template Conversion Invariant

For five-X100 local mining hosts and other multi-miner deployments, connected
miner count and raw hash activity are not enough. The release success metric is
accepted block conversion per miner-hour. The pool must keep one canonical
mining-template epoch at a time. During one-node catch-up, the dashboard must
keep mining unavailable until the active node is safe for templates and submits.

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
dashboard/watchdog/peer/chain guards by default. A stopped `postgres`,
`node`, or `pool` container is a stack failure even when there are
no miners. No-miner mode means no mining work is sent; it does not mean services
are allowed to stay down.

Dashboard block height must come from the node chain RPC `getBlockCount` only.
`getMainChainHeight`, template height, log imports, and peer
lead values are diagnostics and must not be displayed as the node block count.
Keep source and release validation enforcing this so future drift cannot
reintroduce mixed height sources.

Pool block-candidate submission must use the single configured backend endpoint.
Normal shares must not fan out, and valid block candidates should return to the
miner as soon as the active endpoint accepts them. Keep release defaults pinned
to one endpoint.

Installers and dashboards must publish the host-facing ASIC LAN endpoint, not a
Docker bridge address. `BDAG_POOL_HOST`, `BDAG_POOL_URL`,
`BDAG_MINER_SCAN_TARGET`, and `BDAG_ASIC_LAN_CIDRS` must be written during
install/upgrade and passed into the dashboard container. Docker bridge CIDRs
default to `172.16.0.0/12`; those addresses are infrastructure and must not be
used as ASIC identities, miner scan targets, unmanaged miner rows, or the
displayed Stratum endpoint unless an operator explicitly overrides the bridge
filter for a nonstandard real ASIC LAN.

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
and only run the second repair pass after snapshot import has actually mutated
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

After any full rebuild plus redeploy, always restart every affected stack
container before declaring the work complete. For this mining stack that means
`node`, `pool`, `postgres`, and `dashboard`; then verify container uptime, node
RPC height, pool miner readiness, and accepted block-submit activity. Do not
report a full rebuild/redeploy as complete before this restart and evidence
exist.

JSONL histories used by the dashboard should append each sample and compact only
at a bounded threshold. Do not reintroduce full-history rewrite loops for every
sample on the Pi USB data path.

Recurring timers must include modest `RandomizedDelaySec` jitter so node-child
guard, sync coordinator, incident reporter, runtime priority, snapshot, and
peer-discovery work do not wake together and stampede Docker/RPC on constrained
hosts.

Optional background work must respect `background_maintenance_decision()`.
Hourly snapshot staging and global dashboard blockchain scans must defer while
the node is catching up or host IO/CPU pressure is above the configured release
thresholds. Chain import and live mining are the primary jobs; background
freshness work is allowed to lag until the host is healthy.

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

Normal pool GitHub releases use pinned bootstrap scripts plus Runtime Payload
Zips, not one universal zip. Use the glossary terms in `docs/glossary.md`:
Bootstrap Script, Runtime Payload Zip, and Linux ARM64 Runtime. The normal
payload zips are split by Linux Docker runtime architecture (`linux-amd64` and
`linux-arm64`); do not reintroduce a separate appliance package path.

Source-checkout validation must never delete an active local runtime. If a live
machine runs the RC directly from the source checkout, `ops/runtime` can hold
the dashboard environment and sampler state currently used by systemd. Keep
`scripts/validate-rc-local.sh` validating a temporary source copy with a
temporary runtime directory instead of cleaning the checkout in place.

Collector code must avoid host-only command dependencies for normal status. Use
Python's standard HTTP client for local pool metrics and public
enrichment calls so Linux AMD64, Linux ARM64/Pi5, macOS Docker Desktop, and
Windows Docker Desktop behave consistently once Docker and Python are present.

When operating from source, keep surfaces explicit: the Go dashboard is exposed
on host port `8080`; the status API is bound to localhost on host port
`9280` and must be configured with the real container names and Docker access
for the stack being watched.

## P2P Peer Configuration

Peer candidates must be complete P2P multiaddrs with peer IDs. Address class is
not a sync mode, priority class, or eligibility signal.

Old installations may still contain legacy address-bucket variable names. Treat
them only as migration input and clear the bucket values. Do not add new LAN,
VPN, or public sync options, and do not reintroduce LAN-first, VPN-second,
public-last ordering.
