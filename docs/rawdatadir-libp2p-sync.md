# Raw Datadir Libp2p Sync

## Final RC Approach

Use the existing Fast Artifact Sync V2 libp2p protocol instead of a new rsync
stream. Corechain serves a signed `raw_datadir_checkpoint` directory artifact;
the payload is the same stopped-node datadir archive we copy to USB, with node
identity, private material, and backup variants of those paths excluded.

This keeps the release candidate on the existing security model:

- explicit libp2p multiaddrs for discovery
- signed artifact manifests
- chain ID and network verification
- content-addressed artifact roots
- per-file hash verification
- resumable directory downloads

Source serving is automatic only after the local eligibility gate passes. The
gate fails closed on USB/removable/external storage, low disk/RAM/CPU, and
unsafe checkpoint conditions. Do not serve a live mining datadir;
publish only from a finalized sidecar copy.

`SYNC_SOURCE_NODE=0` is the raw-datadir publisher control. It does not by itself
turn a receiver into a USB/constrained host, and it does not disable the node's
normal `--fastartifactsync` startup flag. Bulk serving is suppressed by
`BDAG_NO_FASTSYNC_SERVE=1` or by `auto` detection of a real USB/low-IO chain
profile.

## Producer Flow

Producer host:

1. Confirm the active node is healthy enough to act as source truth.
2. Maintain a low-priority sidecar copy.
3. Finalize the sidecar only during an operator-approved window.
4. Wait for DB lock files to be free.
5. Publish only a signed manifest and immutable chunks that pass validation.
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

Default host:

1. Keep a local sidecar copy close to tip:

```bash
BDAG_RAWDATADIR_SIDECAR_SOURCE=./data/node/mainnet \
BDAG_RAWDATADIR_SIDECAR_DIR=./data-restore/rawdatadir-sidecar/mainnet \
./ops/maintain-rawdatadir-sidecar.sh
```

`maintain-rawdatadir-sidecar.sh` uses `sudo -n rsync` automatically when the
live datadir contains root-owned chain files and passwordless sudo is available.
Set `BDAG_RAWDATADIR_SIDECAR_USE_SUDO=0` only on hosts where all chain files are
readable by the installing user.

After each successful rsync pass, the sidecar also seals the hot copy into
`data-restore/rawdatadir-sidecar-content/current`: immutable SHA-256 chunks,
file descriptors, a canonical manifest root, and an ed25519 signature when
`BDAG_FASTSYNC_ARTIFACT_SIGNING_KEY_HEX` is configured. Hot generations carry a
`DO_NOT_PUBLISH` marker unless `BDAG_RAWDATADIR_SIDECAR_CONTENT_FINALIZED=1`
or an explicit local hot-publish override is set. This gives the future IPFS
path stable content-addressed data without pretending a live sidecar is a
finalized checkpoint.

2. When an operator approves a finalization window, let the publisher stop the
   production node, run one final sidecar sync, restart the node, and build the
   signed artifact from the finalized sidecar. The final sidecar sync also
   seals a publishable file/chunk content generation for IPFS transport:

```bash
BDAG_RAWDATADIR_FINALIZE=1 \
./ops/publish-rawdatadir-artifact.sh
```

The publisher refuses to create a manifest unless live RPC returns a real
`block_total`, `tip_order`, `tip_hash`, and, by default, `state_root`. It also
uses `sudo -n tar` automatically when the finalized sidecar contains root-owned
chain files.

The `ops/systemd/user-bdag-rawdatadir-sidecar.timer` refreshes the sidecar
every two hours, with jitter, in low-priority retry mode. `auto` mode keeps the
timer installed so a temporary mining-pressure or eligibility failure is retried
on the next tick; the service self-defers when host pressure or unsafe storage
would affect mining. USB-backed chain data is never a publishable default
source.

## Receiver Flow

Default release behavior:

The sync coordinator treats the fastest verified receiver path as the first
choice whenever a managed node is materially behind. Every retry window it
probes one deduplicated set of complete P2P multiaddrs. LAN, VPN, and public
route labels are not sync modes and are not priority classes. If a peer offers a
signed `raw_datadir_checkpoint` that is ahead enough, the coordinator stops only
the receiver node, imports the verified artifact with
`ops/fetch-rawdatadir-artifact.sh`, preserves local identity files, restarts the
node, and lets normal FastSync catch the remaining tail. If no source is
available, it records the reason and retries instead of falling back
permanently.

Receiver startup uses an acceptance window, not exact-tip chasing. A seed or
sidecar within `BDAG_SYNC_ACCEPTABLE_STARTUP_LAG_BLOCKS=4000` blocks is
considered close enough to start, and the node should catch up the tail over
normal P2P/FastSync. The shared policy can widen the window from the previous
copy duration with `BDAG_SYNC_COPY_MINUTE_BLOCK_ALLOWANCE=4` block(s) per copy
minute. For raw-datadir receiver fetches, set `BDAG_RAWDATADIR_TARGET_TIP` to
the observed network or source tip; when `BDAG_RAWDATADIR_MIN_TIP` is not set,
the fetcher requests `target_tip - acceptable_lag` so an otherwise good artifact
is not rejected only because the copy took time. Once the receiver and source
are within the acceptance window, fix peer connectivity if catch-up stalls; do
not continuously redo the full copy just to close the last blocks.

Relevant defaults:

```bash
BDAG_FAST_CATCHUP_ARTIFACT_MODE=auto
BDAG_FAST_CATCHUP_ARTIFACT_RETRY_SECONDS=300
BDAG_FAST_CATCHUP_ARTIFACT_MIN_BEHIND_BLOCKS=1000
BDAG_FAST_CATCHUP_ARTIFACT_MIN_GAIN_BLOCKS=1000
BDAG_SYNC_ACCEPTABLE_STARTUP_LAG_BLOCKS=4000
BDAG_SYNC_COPY_MINUTE_BLOCK_ALLOWANCE=4
BDAG_FAST_CATCHUP_ARTIFACT_TRUST_ON_FIRST_SIGNED=1
BDAG_FAST_CATCHUP_ALLOW_UNSIGNED_ARTIFACTS=0
BDAG_FAST_CATCHUP_ARTIFACT_TIMEOUT=21600s
```

`BDAG_FAST_CATCHUP_ARTIFACT_TRUST_ON_FIRST_SIGNED=1` allows the receiver to
learn a signer public key from a signed manifest during probing, then fetch and
verify the artifact using that signer. It does not make unsigned data trusted;
fully unsigned raw datadir artifacts still require
`BDAG_FAST_CATCHUP_ALLOW_UNSIGNED_ARTIFACTS=1` or
`BDAG_RAWDATADIR_ALLOW_UNSIGNED=1`, and those are local-test overrides.

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
BDAG_RAWDATADIR_IMPORT_TARGET=./data/node/mainnet \
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
- Do not accept unsigned raw datadir artifacts outside local tests. Automatic
  receiver catch-up may trust a newly discovered signed manifest, but the
  content and manifest signature still have to verify before import.
- Trust signer public keys, not peer IDs.
- Do not import the sender's `network.key`, `bdageth/nodekey`, `keystore`,
  `peerstore`, `nodes`, `bdageth/nodes`, backup variants of those paths, or
  IPC/socket files.
- Keep at least one parked receiver datadir until the imported node has started,
  verified chain ID `1404`, and caught the normal FastSync tail.
- Direct public internet serving still needs reachable libp2p TCP ports or a
  reachable relay/seed. Current RC operations should use explicit P2P multiaddrs
  and let measured P2P latency/usefulness decide which source is fastest.

## RC Limitations

- No deltas.
- Full raw datadir checkpoints only.
- Automatic discovery merges complete P2P multiaddrs into one candidate pool.
  Address class is not a sync option or priority signal.
- No automatic live-service restart to enable serving env.
- Receiver import is automatic only for the local managed node during far-behind
  catch-up, and only after a signed/verified manifest is found. Manual fetch
  remains available for operator-directed recovery.
- The sidecar timer is low-priority and retrying by default. IO-sensitive ASIC
  test windows should rely on the background maintenance backoff rather than
  permanently disabling the timer, unless an operator explicitly requests a
  full pause.
