# Final Release Checklist From Issue 26

Issue #26 identified the RC4 deployment gaps found on `/home/matt`. The final
release candidate must preserve the operational hardening from that deployment
while removing local assumptions that caused install or sync drift.

## Source Manifest

- `pool-stack-docker`: `release/pool-stack-20260524-rc4-sre`
- `blockdag-corechain`: raw-datadir V2 sync source commit `c74f88b9c1b4fbf4213e15272d3bf1f63943e839`
  or newer, including directory V2 artifact support, latency-first artifact
  peer preference, and the zero-state-root `HasState` guard.
- `pool`: `develop` at `61b231c0501b32338f4ad47561a09e03e5933adc` or newer,
  pinned to a single backend submit path.
- `pool-dashboard`: `develop` at `c82978751f035afd0e9da2b3d5d1fac69eae6858`
  or newer.

## Release Requirements

- GitHub release workflows pin known source commits, use Go 1.26, and set
  `GOFLAGS=-buildvcs=false`; pool binaries also pass `-buildvcs=false`
  explicitly.
- Release archives are audited by `scripts/check-release-archive.py` so `.git`,
  package metadata, mutable data directories, local `.env`, `node.conf`, and
  transient snapshot files do not ship.
- Universal installers preserve existing node data, peer identity, signer
  material, and runtime state unless `BDAG_RESET_NODE_DATA=1` is set.
- Installers preflight architecture, Docker Compose, disk, port occupancy, time
  sync, optional `jq`, and seed reachability. Old/orphan Compose cleanup is a
  dry-run unless `BDAG_CLEAN_ORPHAN_CONTAINERS=1` is set.
- Installs configure one direct submit endpoint and do not enable endpoint
  fanout by default.
- Fast Artifact Sync V2 is default. When more than 1000 blocks behind, the sync
  coordinator accelerates the leader and restarts stale or non-V2 catch-up after
  the cooldown.
- V2 peer selection is latency/usefulness-first over libp2p. Address class is
  not a sync option or priority signal; complete P2P multiaddrs are the only
  sync candidates.
- Directory artifact serving must use a valid `artifact.manifest.json`; otherwise
  startup reports archive fallback instead of silently pretending directory mode
  is active.
- Scripts that still need `jq` preflight it explicitly. Release installers avoid
  `jq` for required JSON parsing.
- Live data scans must avoid mutable Postgres/node paths; release packaging uses
  tracked source plus explicit runtime/data exclusions.

Run `scripts/validate-pi5-restart-hardening.sh --mode source .` before tagging
the release candidate.
