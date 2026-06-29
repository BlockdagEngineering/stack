# Chain Data Provenance Guard

The stack must never silently start a node from genesis, or from a tiny fresh
datadir, while usable chain data exists somewhere else on the host.

## Canonical Runtime Contract

`NODE_DATA_DIR` is the only valid node chain datadir variable.

The default value is:

```bash
NODE_DATA_DIR=./node-data
```

For a normal checkout this resolves to:

```text
<stack-root>/node-data
```

`BDAG_NODE_DATA_DIR` is obsolete. It must not appear in `.env`, generated env
files, compose assumptions, installer docs, or runtime scripts. If an old
installation still has it, treat it as evidence of legacy config drift and stop
until the config has been migrated to `NODE_DATA_DIR`.

## Failure Signature This Guard Prevents

The 2026-06-24 incident had this shape:

- compose mounted `${NODE_DATA_DIR:-./node-data}`;
- `.env` contained the obsolete node datadir variable but not `NODE_DATA_DIR`;
- compose therefore started the node on a small `./node-data`;
- a much larger preserved Docker volume, `stack_node-data`, still existed;
- local node height was near `200k` while peers were near `12.5M`;
- pool correctly entered `node_syncing` and stopped mining.

That is a data-source selection failure, not a normal peer-count failure.

## Runtime Chain DB Validation

The standalone shell check has been removed. The node and its chain DB import
path now own validation of the selected database before normal operation.
Installers must still write only `NODE_DATA_DIR`, preserve or migrate known-good
chain data before start, and start the node before pool services. Runtime
provenance checks and pool start gates continue to detect suspicious resets,
low-height starts, unreadable datadirs, and better preserved legacy candidates.

## Local Chain Archive Restore

When a human supplies chain data in `~/Downloads`, on a USB drive, or beside the
installer, treat it as a candidate data source before allowing genesis sync.
Do not trust the file extension alone. The 2026-06-25 restore used a file named
`bdag-latest-snapshot (1).tar.gz` that was actually a zstd-compressed tar
archive. Always detect the type with `file` or the installer archive detector.

A local chain archive may contain only the chain payload:

```text
BdagChain/
bdageth/
metaData
```

That is valid chain/EVM state, but it is not the complete runtime identity.
When replacing a live datadir from such an archive:

1. Stop pool first, then dashboard, status-sampler, watchdog, sentinel, and
   node.
2. Preserve `.env`, `node.conf`, pool accounting, ASIC settings, and
   `MINING_POOL_ADDRESS`.
3. Preserve peer identity from the existing datadir when present:
   `mainnet/peerstore`, `mainnet/network.key`, and `mainnet/recent-peers.json`.
4. Quarantine the previous `BdagChain`, `bdageth`, and `metaData` directories
   instead of deleting them.
5. Extract the archive into `NODE_DATA_DIR/mainnet`.
6. Restore container ownership, normally UID/GID `999:999` for `bdagStack`.
7. Keep directories searchable by the installer user, normally `0755`, so
   runtime provenance checks can inspect the selected datadir.
8. Start node and dashboard first. Start or allow pool only after node RPC,
   P2P freshness, and mining-template gates are sane.

The manifest for the restore must record the archive path, measured size,
checksum when practical, target path, quarantine path, and preserved peer files.

## Legacy Volume Migration

If `stack_node-data` is the best valid source, migrate it into canonical
`./node-data`:

```bash
scripts/migrate-node-data-volume-to-host.sh
```

The migration tool:

- stops the node before copying, unless `--no-stop` is supplied;
- quarantines the previous `./node-data`;
- copies active runtime paths only: `mainnet`, `rpc.cert`, `rpc.key`;
- leaves stale legacy folders such as `failed-node-data`, `data`, `logs`, and
  `build` out of the live datadir;
- writes a measured manifest under `ops/runtime/`;
- preserves the source volume until the upgraded node is verified and cleanup
  is explicitly approved.

Do not hardcode chain data sizes. Record measured sizes and compare candidates
relatively by validity, verified height when available, freshness, then size.

## Upgrade Ordering

1. Record current stack state and image IDs.
2. Search known data locations before accepting fresh sync.
3. Migrate the best valid source into `./node-data`.
4. Stop pool before touching node.
5. Stop dashboard, status-sampler, watchdog, and sentinel.
6. Stop node.
7. Build/recreate images.
8. Start node only.
9. Verify node height and P2P/native readiness.
10. Start pool only after node gates are sane.
11. Start dashboard and guard services.
12. Save the final manifest.
13. Remove stale rejected data/images only after human verification.

Destructive reinstall removes code, images, and build artifacts. It does not
blindly delete chain data, peers, pool accounting, ASIC configuration, Stratum
client setup, or the only rollback evidence.
