# Raw Datadir Libp2p Sync

## Final RC Approach

Use the existing Fast Artifact Sync V2 libp2p protocol instead of a new rsync
stream. Corechain serves a signed `raw_datadir_checkpoint` directory artifact;
the payload is the same stopped-node datadir archive we copy to USB, with node
identity and private material excluded.

This keeps the release candidate on the existing security model:

- explicit libp2p multiaddrs for discovery
- signed artifact manifests
- chain ID and network verification
- content-addressed artifact roots
- per-file hash verification
- resumable directory downloads

Source serving is automatic only after the local eligibility gate passes. The
gate fails closed on USB/removable/external storage, low disk/RAM/CPU, and
unsafe single-node checkpoint conditions. Do not serve a live mining datadir;
publish only from a drained standby node or a finalized sidecar copy.

## Producer Flow

Dual-node mining host:

1. Confirm the pool has an active selected backend and fresh jobs.
2. Pick the non-selected backend as the export source.
3. Put that backend into router maintenance mode.
4. Stop only that backend.
5. Wait for DB lock files to be free.
6. Archive `$datadir/mainnet` while excluding identity/private material.
7. Restart the backend and clear maintenance before heavy verification.
8. Verify the archive, write `SHA256SUMS`, build a signed
   `raw_datadir_checkpoint` manifest, and promote `data-restore/rawdatadir/current`.

Command:

```bash
BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_ID=ops-rawdatadir \
BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_HEX=... \
./ops/build-rawdatadir-artifact.sh
```

The script prints the serving paths:

```bash
BDAG_FASTSYNC_ARTIFACT_DIRECTORY=./data-restore/rawdatadir/current
BDAG_FASTSYNC_ARTIFACT_MANIFEST=./data-restore/rawdatadir/current/manifest.json
```

Those env values must be present in the node process that should serve the
artifact. A node restart may be required on the serving host to pick them up.
Do not promote a new `current` artifact while a receiver is actively
downloading; the RC server advertises one current artifact root at a time.

Single-node host:

1. Keep a local sidecar copy close to tip:

```bash
BDAG_RAWDATADIR_SIDECAR_SOURCE=./data/node1/mainnet \
BDAG_RAWDATADIR_SIDECAR_DIR=./data-restore/rawdatadir-sidecar/mainnet \
./ops/maintain-rawdatadir-sidecar.sh
```

`maintain-rawdatadir-sidecar.sh` uses `sudo -n rsync` automatically when the
live datadir contains root-owned chain files and passwordless sudo is available.
Set `BDAG_RAWDATADIR_SIDECAR_USE_SUDO=0` only on hosts where all chain files are
readable by the installing user.

2. When an operator approves a finalization window, let the publisher stop the
   single node, run one final sidecar sync, restart the node, and build the
   signed artifact from the finalized sidecar:

```bash
BDAG_RAWDATADIR_SINGLE_NODE_FINALIZE=1 \
./ops/publish-rawdatadir-artifact.sh
```

The publisher refuses to create a manifest unless live RPC returns a real
`block_total`, `tip_order`, `tip_hash`, and, by default, `state_root`. It also
uses `sudo -n tar` automatically when the finalized sidecar contains root-owned
chain files.

The optional `ops/systemd/user-bdag-rawdatadir-sidecar.timer` refreshes the
sidecar every two hours, with jitter. `ops/install-p2p-services.sh` installs it
only through the raw datadir source eligibility policy. USB-backed chain data is
never a default source.

## Receiver Flow

Fetch only:

```bash
BDAG_RAWDATADIR_PEERS='/ip4/10.0.0.5/tcp/8151/p2p/16U...' \
BDAG_RAWDATADIR_TRUSTED_SIGNERS='ops-rawdatadir:<ed25519-public-key-hex>' \
./ops/fetch-rawdatadir-artifact.sh
```

Fetch and install into a stopped receiver datadir:

```bash
BDAG_RAWDATADIR_PEERS='/ip4/10.0.0.5/tcp/8151/p2p/16U...' \
BDAG_RAWDATADIR_TRUSTED_SIGNERS='ops-rawdatadir:<ed25519-public-key-hex>' \
BDAG_RAWDATADIR_IMPORT_TARGET=./data/node1/mainnet \
BDAG_RAWDATADIR_IMPORT_REPLACE=1 \
./ops/fetch-rawdatadir-artifact.sh
```

The fetch script downloads with:

```bash
fastsnap --artifact-type raw_datadir_checkpoint --legacy-fallback=false --dir-out ...
```

It verifies the manifest and file hashes, validates the tar archive, extracts to
a temporary directory, preserves the receiver's local identity paths when they
exist, parks the old datadir as `before-rawdatadir-*`, and then moves the new
datadir into place.

## Guardrails

- Never export from the active single mining backend unless the operator has
  explicitly approved downtime.
- Do not serve a live datadir. Serve only a finalized artifact directory.
- Do not accept unsigned raw datadir artifacts outside local tests.
- Trust signer public keys, not peer IDs.
- Do not import the sender's `network.key`, `bdageth/nodekey`, `keystore`,
  `peerstore`, or IPC/socket files.
- Keep at least one parked receiver datadir until the imported node has started,
  verified chain ID `1404`, and caught the normal FastSync tail.
- Direct public internet serving still needs reachable libp2p TCP ports or a
  reachable relay/seed. Current RC operations should prefer explicit LAN/VPN
  multiaddrs.

## RC Limitations

- No deltas.
- Full raw datadir checkpoints only.
- Automatic local/VPN discovery is preferred when peer multiaddrs are available;
  public internet serving still requires reachable P2P ports or relays.
- No automatic live-service restart to enable serving env.
- Receiver import is an operator action against a stopped datadir.
- The sidecar timer is optional and must be disabled during IO-sensitive ASIC
  test windows unless the operator asks for it.
