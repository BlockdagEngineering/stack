# Glossary

## Bootstrap Script

A pinned GitHub release asset that detects the operator host OS and CPU
architecture, selects the matching runtime payload zip, downloads it from the
same release tag, extracts it, and starts the payload installer.

## Runtime Payload Zip

A versioned release zip named
`pool-stack-docker-<tag>-linux-<arch>.zip`. It contains the normal installer
launchers, stack files, and native Linux service binaries for one Docker runtime
architecture.

## Linux ARM64 Runtime

The `linux-arm64` runtime payload used for native ARM64 Linux containers. Linux
ARM64, macOS ARM64 Docker Desktop, Windows ARM64 Docker Desktop, and Pi5 ARM64
hosts select this payload.

## Pi5 ARM64 Appliance Package

The separate Raspberry Pi 5 appliance/hardening package produced by
`ops/build-pi5-arm64-release.sh`. It may include appliance-specific image
archives, runtime compose generation, service hardening, and chain-data archive
flows that are not part of the normal runtime payload zips.
