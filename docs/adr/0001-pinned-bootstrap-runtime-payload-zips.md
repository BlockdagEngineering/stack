# ADR 0001: Pinned Bootstrap Scripts And Runtime Payload Zips

Date: 2026-06-02

## Status

Accepted

## Context

The old normal pool release attached one primary `pool-stack-docker-<tag>.zip`
that behaved like a universal package while bundling Linux AMD64 service
binaries. ARM hosts had to rely on Docker `linux/amd64` emulation even when the
runtime should have been able to run native Linux ARM64 containers.

The release should have one supported distribution path: bootstrap scripts that
download runtime-architecture payload zips. Appliance-specific image/archive
builders are outside the repo's active release path.

## Decision

Normal pool releases publish one pinned Linux bootstrap asset plus
runtime-architecture payload zips:

- `install.sh` for Linux bootstrap and payload install.
- `pool-stack-docker-<tag>-linux-amd64.zip`.
- `pool-stack-docker-<tag>-linux-arm64.zip`.

Each bootstrap is generated for exactly one release tag and downloads payloads
only from that same tag. Linux host CPU architecture decides the payload:
AMD64 uses `linux-amd64`; ARM64 uses `linux-arm64`. macOS and Windows hosts
are rejected by the bootstrap and payload installer.

Each payload includes `release-payload.env`, and the single payload installer
writes `DOCKER_PLATFORM` from that payload metadata. The installer no longer
tells ARM hosts to use AMD64 emulation.

## Consequences

CI must build and package both Linux runtime architectures and run
`scripts/verify-release-architecture.py --target linux-<arch>` before zipping
each payload.

Operators can start from a small host-specific bootstrap asset while still
receiving a pinned, reproducible payload for the release tag.

ARM64 Docker hosts get native `linux/arm64` service binaries in the normal pool
release path without a separate appliance builder.
