# ADR 0001: Pinned Bootstrap Scripts And Runtime Payload Zips

Date: 2026-06-02

## Status

Accepted

## Context

The old normal pool release attached one primary `pool-stack-docker-<tag>.zip`
that behaved like a universal package while bundling Linux AMD64 service
binaries. ARM hosts had to rely on Docker `linux/amd64` emulation even when the
runtime should have been able to run native Linux ARM64 containers.

The release also needs to keep the Pi5 appliance package separate from the
normal pool payload. The Pi5 builder owns appliance hardening, image archive,
and chain-data flows; normal releases should remain a binaries-plus-local-Docker
build package.

## Decision

Normal pool releases publish pinned bootstrap assets plus runtime-architecture
payload zips:

- `install.sh` for Linux/macOS bootstrap.
- `install.ps1` for Windows bootstrap.
- `pool-stack-docker-<tag>-linux-amd64.zip`.
- `pool-stack-docker-<tag>-linux-arm64.zip`.

Each bootstrap is generated for exactly one release tag and downloads payloads
only from that same tag. Host OS and CPU architecture decide the payload:
Linux/macOS/Windows AMD64 use `linux-amd64`; Linux/macOS/Windows ARM64 use
`linux-arm64`.

Each payload includes `release-payload.env`, and payload installers write
`DOCKER_PLATFORM` from that payload metadata. The installer no longer tells ARM
hosts to use AMD64 emulation.

The Pi5 ARM64 appliance package remains owned by
`ops/build-pi5-arm64-release.sh` and is not merged into the normal payload zip
model.

## Consequences

CI must build and package both Linux runtime architectures and run
`scripts/verify-release-architecture.py --target linux-<arch>` before zipping
each payload.

Operators can start from a small host-specific bootstrap asset while still
receiving a pinned, reproducible payload for the release tag.

ARM64 Docker hosts get native `linux/arm64` service binaries in the normal pool
release path, while the Pi5 appliance path keeps its additional hardening and
archive behavior.
