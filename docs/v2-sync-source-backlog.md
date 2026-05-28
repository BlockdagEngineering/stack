# V2 Sync Source Backlog

This backlog captures the work required for a single-node BlockDAG mining pool
to serve new installs with Fast Artifact Sync V2 without risking live mining.
The final design is:

```text
live node1 datadir
-> low-priority raw datadir sidecar
-> sealed immutable generation
-> signed raw_datadir_checkpoint manifest
-> retained artifact generations
-> libp2p FastArtifact V2 serving
```

## P0 - Single-Node Source Must Never Export The Live Datadir

Problem: The old `build-fastsnap-seed.sh` workflow was built for dual-node
active/standby pools and may stop an export backend. On a one-node pool that
backend is the production node, so the workflow can interrupt mining and chain
service.

Required behavior:

- In `BDAG_NODE_MODE=single`, never call the old FastSnap seed workflow.
- Build raw datadir artifacts only from a sidecar/finalized copy.
- Require `BDAG_RAWDATADIR_SINGLE_NODE_FINALIZE=1` before stopping the single
  node for a final sync window.
- Keep `BDAG_FASTSNAP_SEED_TIMER_ENABLED=0` as the single-node default.

Status: stack guard added; legacy archive seeding remains available only as an
explicit dual-node compatibility path.

## P0 - USB/Removable Storage Must Disable Source Mode

Problem: Serving a sync source from USB-backed chain data can contend with the
node, increase write amplification, and make weak removable media look like an
official bootstrap source.

Required behavior:

- Check active datadir, sidecar dir, artifact dir, temp dir, Docker root, and
  restore roots.
- Use `findmnt`, `lsblk`, sysfs transport/removable flags, fstype, and mount
  path checks.
- Fail closed for USB, removable, hotplug, unsafe network/FUSE filesystems,
  `/media`, and `/run/media`.
- Write a machine-readable disabled reason for the dashboard and future agents.

Status: `ops/fastartifact_source_eligibility.py` implements the first pass.

## P0 - Published Artifact Must Be Immutable And Verified

Problem: An actively mutating sidecar copy is not a valid artifact. A receiver
can request chunks while `current` changes, or fetch a file set that does not
match a consistent chain state.

Required behavior:

- Publish into `data-restore/rawdatadir/artifacts/<generation>`.
- Verify hashes and archive readability before promotion.
- Atomically promote `data-restore/rawdatadir/current`.
- Retain at least three generations so receivers can complete downloads that
  started before `current` advanced.
- Treat artifact lag over `10000` main-order blocks as stale.

Status: stack publisher retains generations. Core serving still needs retained
root addressing if peers request an older root after `current` advances.

## P0 - Corechain Must Support Raw Datadir Artifact Type

Problem: Older node images support directory download but not
`raw_datadir_checkpoint`, so they cannot act as the new source.

Required behavior:

- Include corechain commit with `fastsnap --artifact-type`.
- Support `raw_datadir_checkpoint` manifest validation.
- Serve file chunks for directory artifacts over FastArtifact V2 libp2p.
- Prefer signed manifests and trusted signer public keys.

Status: stack default node commit now points at the raw-datadir-capable
corechain commit. Receiver testing is still required on a second host.

## P1 - Installer Must Make New Pools Self-Describing

Problem: A new location/install needs enough local context to understand whether
it should serve sync data, why it is disabled, and how to fetch from peers.

Required behavior:

- Install the eligibility tool, sidecar refresher, publisher, fetch helper, and
  documentation in the release package.
- Default fresh installs to node1-only, no miners configured, sync-only.
- Install raw datadir source timers only through eligibility policy.
- Leave serving disabled with a clear reason on USB-backed systems.
- Preserve signer secrets outside memory/docs/logs.

Status: release env and installer defaults have been updated for node1 and raw
datadir source mode.

## P1 - Dashboard Needs A V2 Sync Source Panel

Problem: `synced` does not mean `serving artifacts`. Operators and agents need
to see whether this host is a usable libp2p source.

Required behavior:

- Show eligible/disabled/backoff reason.
- Show live tip, artifact tip, lag, manifest age/expiry, signer key ID, last
  sidecar sync, last publish, retained generation count, and libp2p probe.
- Alert if artifact lag exceeds `10000` blocks or the manifest is missing.

Status: status JSON is written by the new tools; dashboard rendering remains a
follow-up.

## P1 - Automatic Discovery Should Prefer LAN/VPN Sources

Problem: New nodes should not fail with “no peers support V2 artifact protocol”
when an eligible local source exists.

Required behavior:

- Keep local peer discovery enabled.
- Prefer complete LAN/VPN multiaddrs with peer IDs.
- Pass all candidate peers into one `fastsnap` attempt where possible so the
  downloader can select the best provider.
- Keep public peers as fallback.

Status: existing tiered peer discovery remains. End-to-end testing between this
host and a fresh install is still required.

## P2 - Simplify And Retire Duplicate Sync Systems

Problem: The stack currently contains archive FastSnap, hot snapshot refresh,
raw datadir artifacts, local peer discovery, and chain seed import paths. Some
overlap and can cause maintenance jobs to compete with mining.

Required behavior:

- Keep V2 directory/raw-datadir artifact sync as the preferred online bootstrap.
- Keep legacy archive FastSnap only for explicit compatibility.
- Keep offline USB/package chain data separate from online source serving.
- Do not re-enable hotsnap/FastSnap seed timers on single-node systems.
- Ensure every optional background sync job respects maintenance backoff and
  low-priority systemd scheduling.

Status: single-node default disables archive FastSnap seed. Additional cleanup
can remove obsolete docs and hotsnap defaults after receiver testing confirms
raw-datadir V2 covers the release path.

## P2 - Cross-Platform Resource Policy

Problem: Pi5, AMD64, ARM64 servers, Docker Desktop, and USB systems expose
different pressure/storage signals.

Required behavior:

- Use Linux pressure signals where available.
- Degrade to conservative defaults when signals are missing.
- Keep resource caps configurable with environment variables.
- Never assume USB SSDs report removable correctly; use multiple signals.

Status: first-pass Linux eligibility exists. macOS/Windows Docker Desktop
preflight behavior remains a release packaging follow-up.
