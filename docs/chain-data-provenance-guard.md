# Chain Data Provenance Guard

The stack must never silently start a node from genesis, or from a tiny fresh
datadir, while usable chain data exists somewhere else on the host.

## Canonical Runtime Contract

`NODE_DATA_DIR` is the only valid node chain datadir variable.

The default value is:

```bash
NODE_DATA_DIR=./data/node
```

For a normal checkout this resolves to:

```text
<stack-root>/data/node
```

`BDAG_NODE_DATA_DIR` is obsolete. It must not appear in `.env`, generated env
files, compose assumptions, installer docs, or runtime scripts. If an old
installation still has it, treat it as evidence of legacy config drift and stop
until the config has been migrated to `NODE_DATA_DIR`.

## Failure Signature This Guard Prevents

The 2026-06-24 incident had this shape:

- compose mounted `${NODE_DATA_DIR:-./data/node}`;
- `.env` contained the obsolete node datadir variable but not `NODE_DATA_DIR`;
- compose therefore started the node on a small `./data/node`;
- a much larger preserved Docker volume, `stack_node-data`, still existed;
- local node height was near `200k` while peers were near `12.5M`;
- pool correctly entered `node_syncing` and stopped mining.

That is a data-source selection failure, not a normal peer-count failure.

## Required Preflight

Run this before rebuild, redeploy, `docker compose up`, or destructive
reinstall:

```bash
scripts/preflight-chain-data.sh
```

The script fails closed when:

- `NODE_DATA_DIR` is unset;
- `BDAG_NODE_DATA_DIR` exists;
- `NODE_DATA_DIR` does not resolve to canonical `./data/node` unless an explicit
  override is allowed;
- the selected datadir is invalid while a valid preserved candidate exists;
- the selected datadir is materially smaller than a preserved candidate such as
  `stack_node-data`;
- the install would sync from genesis without explicit `--fresh-chain-ok`.

The script accepts either a complete node datadir or a staged
`mainnet/snapshot.bdsnap` as usable non-genesis chain seed data. A complete,
newer datadir still wins over a snapshot.

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
   `scripts/preflight-chain-data.sh` can verify the restore without root.
8. Run `scripts/preflight-chain-data.sh` before starting node.
9. Start node and dashboard first. Start or allow pool only after node RPC,
   P2P freshness, and mining-template gates are sane.

The manifest for the restore must record the archive path, measured size,
checksum when practical, target path, quarantine path, and preserved peer files.

## Legacy Volume Migration

If `stack_node-data` is the best valid source, migrate it into canonical
`./data/node`:

```bash
scripts/migrate-node-data-volume-to-host.sh
```

The migration tool:

- stops the node before copying, unless `--no-stop` is supplied;
- quarantines the previous `./data/node`;
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
2. Run chain-data preflight.
3. Search known data locations before accepting fresh sync.
4. Migrate the best valid source into `./data/node`.
5. Stop pool before touching node.
6. Stop dashboard, status-sampler, watchdog, and sentinel.
7. Stop node.
8. Build/recreate images.
9. Start node only.
10. Verify node height and P2P/native readiness.
11. Start pool only after node gates are sane.
12. Start dashboard and guard services.
13. Save the final manifest.
14. Remove stale rejected data/images only after human verification.

Destructive reinstall removes code, images, and build artifacts. It does not
blindly delete chain data, peers, pool accounting, ASIC configuration, Stratum
client setup, or the only rollback evidence.
