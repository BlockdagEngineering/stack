# Final Release Checklist From Issue 26

Issue #26 identified the RC4 deployment gaps found on `/home/matt`. The final
release candidate must preserve the operational hardening from that deployment
while removing local assumptions that caused install or sync drift.

## Source Manifest

- `pool-stack-docker`: `release/pool-stack-20260524-rc4-sre`
- `blockdag-corechain`: mainnet sync source commit `c74f88b9c1b4fbf4213e15272d3bf1f63943e839`
  or newer, including latency-first peer preference and the zero-state-root
  `HasState` guard.
- `pool`: `develop` at `61b231c0501b32338f4ad47561a09e03e5933adc` or newer,
  pinned to a single backend submit path.
- `dashboard2`: `main`; release builds always use this branch.

## Release Requirements

- GitHub release workflows pin known source commits, use Go 1.26, and set
  `GOFLAGS=-buildvcs=false`; pool binaries also pass `-buildvcs=false`
  explicitly. Normal pool releases build both `linux-amd64` and `linux-arm64`
  runtime payload zips and generate pinned bootstrap scripts for the same tag.
- Release archives are audited by `scripts/check-release-archive.py` so `.git`,
  package metadata, mutable data directories, local `.env`, `node.conf`, and
  transient chain snapshot downloads do not ship.
- Payload installers preserve existing node data, peer identity, signer
  material, and runtime state. When `BDAG_CHAIN_DB_ARCHIVE_URL` is set and the
  configured node datadir has no chain markers, installers download the trusted
  `.bdsnap` chain DB snapshot archive, import it into a clean staged
  `mainnet/` datadir, validate `mainnet/BdagChain`, and move it into that host
  datadir before first start. They set `DOCKER_PLATFORM` from
  `release-payload.env`, not from a universal AMD64 assumption.
- Installers preflight architecture, Docker Compose, disk, port occupancy, time
  sync, optional `jq`, and seed reachability. Old/orphan Compose cleanup is a
  dry-run unless `BDAG_CLEAN_ORPHAN_CONTAINERS=1` is set.
- Installs configure one direct submit endpoint and do not enable endpoint
  fanout by default.
- When more than 1000 blocks behind, the sync coordinator accelerates the leader
  and restarts stale catch-up after the cooldown.
- V2 peer selection is latency/usefulness-first over libp2p. Address class is
  not a sync option or priority signal; complete P2P multiaddrs are the only
  sync candidates.
- Scripts that still need `jq` preflight it explicitly. Release installers avoid
  `jq` for required JSON parsing.
- Live data scans must avoid mutable Postgres/node paths; release packaging uses
  tracked source plus explicit runtime/data exclusions.

Run `scripts/validate-release-build.sh .` before tagging the release candidate.
