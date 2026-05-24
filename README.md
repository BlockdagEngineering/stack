# pool-stack-docker-stack

This stack can be run in any environment where docker is installed. It includes an upgradable BDAG node, a mining pool with its db, and a minimal dashboard that  provides essential realtime monitoring.


| Service     | Image / build                           | Purpose |
| ----------- | --------------------------------------- | ------- |
| `node`      | BlockDAG node, supervised by nodeworker |         |
| `pool`      | Mining pool (Stratum :3334)             |         |
| `postgres`  | Pool persistence, schema auto-loaded    |         |
| `dashboard` | Essential monitoring                    |         |


## Release package

GitHub Releases attach `pool-stack-docker-<tag>.zip` with `bin/` (pre-built `**blockdag-node**`, `**nodeworker**`, `**mining-pool**`), `dashboard/` (Compose builds `dashboard`), `docker-compose.yml`, `dockerfile`, `.env.example`, `docker/`, and cross-platform installers. **Release images** stage binaries from `./bin`; no git clone inside Docker.

After unpacking, run the installer from the extracted directory:

```bash
# Linux / macOS
bash install.sh
```

```powershell
# Windows
.\install.ps1
```

The installer detects the host OS and CPU architecture, writes `.env` and `node.conf`, generates a strong Postgres password unless `POSTGRES_PASSWORD` is already set, downloads `latest.bdsnap` when needed, and runs `docker compose build && docker compose up -d`. The release currently runs the service images as `linux/amd64`; ARM hosts need Docker Desktop or Docker Engine with amd64 emulation enabled.

On macOS, the installer uses `aria2c` for faster, resumable snapshot downloads and installs it with Homebrew when missing. If that path fails, it opens a browser download link and Finder at the installer folder, then waits for `latest.bdsnap` to appear there. Browsers may still save to Downloads unless you choose the installer folder. To skip the dependency install, force curl with `BDAG_SNAPSHOT_DOWNLOADER=curl bash install.sh`; to go straight to the browser helper, use `BDAG_SNAPSHOT_DOWNLOADER=browser bash install.sh`. On Windows, the installer uses `aria2c` when available, tries to install it with `winget`, then falls back to BITS and PowerShell download.

Snapshot import happens while the node image is built. If you re-run the installer against an existing Docker `node-data` volume, Docker will keep using the old volume and the newly imported snapshot will be hidden. The installer resets the local node data volume by default so the snapshot is used. To keep existing node data instead, use:

```bash
BDAG_RESET_NODE_DATA=0 bash install.sh
```

If the default snapshot host is unavailable, point the installer at the snapshot URL you want to use:

```bash
BDAG_SNAPSHOT_URL=https://your-host.example/latest.bdsnap bash install.sh
```

The installer requires a valid snapshot by default. To allow the node to sync from P2P when no valid snapshot can be downloaded, use:

```bash
BDAG_REQUIRE_SNAPSHOT=0 bash install.sh
```

On macOS, if Docker reports an `xattr` error for files such as `._.env.example`, those are AppleDouble metadata files from the extracted folder or external drive. Current release packages include `.dockerignore` and the installer removes those files before building. For an older extracted folder, clean it manually and run the installer again:

```bash
find . -name '._*' -type f -delete
find . -name '.DS_Store' -type f -delete
rm -rf __MACOSX
bash install.sh
```

The same cleanup also ignores common Windows metadata such as `Thumbs.db`, `desktop.ini`, `$RECYCLE.BIN`, and `System Volume Information`.

## Configuration (what loads where)

Docker Compose reads `**.env`** in this directory for variable substitution and passes pool / miner settings into containers.


| Piece           | Purpose                                                                                                                                                                                                                                                                         |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `**node.conf`** | **Project root.** Mounted into the `**node`** container as `/etc/bdagStack/node.conf` (peers, `miningaddr`, RPC modules). **Copy from `node.conf.example**` — `node.conf` is gitignored. `**rpcuser` / `rpcpass` here must match `NODE_RPC_USER` / `NODE_RPC_PASS` in `.env`.** |
| `**.env`**      | Start from `**.env.example`**. `******NODE_RPC_URL` / `**PG_URL**` are set in `docker-compose.yml`. **Miner:** `MINER_POOL_URL`, `MINING_POOL_ADDRESS`, `MINER_POOL_PASS`, `MINER_WORKERS`.                                                                                     |


The `**pool`** image bakes `**.env.example`** into the image at `/var/lib/bdagStack/pool/.env` for `godotenv` (release `**dockerfile`** uses `**COPY .env.example**` relative to tarball root; git dev `**dockerfile-dev**` uses `**COPY pool-stack-docker/.env.example**`). Compose still sets most variables via `environment:`.

## FastSync Peer Discovery Order

New nodes prefer nearby FastSync sources before falling back to public seeds.
Configure complete multiaddrs with peer IDs in `.env`:

```text
BDAG_FASTSYNC_LAN_PEERS=/ip4/192.168.1.10/tcp/8151/p2p/...
BDAG_FASTSYNC_VPN_PEERS=/ip4/10.0.0.10/tcp/8151/p2p/...
BDAG_FASTSYNC_PUBLIC_PEERS=
```

The node entrypoint folds those values together with `BDAG_FASTSNAP_PEERS`,
`BOOTSTRAP_PEER_ADDRESSES`, and `node.conf` `addpeer` lines in this order:
LAN, private/VPN, public internet. The ordered list is used for pre-start
FastSnap on empty datadirs and is also appended as startup `--addpeer`
arguments so protocol 46 FastSync peers are available before public fallback
dials dominate startup.

`BDAG_FASTSYNC_LAN_PREFIXES` defaults to `192.168.`. If your premises LAN uses
another private range, either put those complete multiaddrs in
`BDAG_FASTSYNC_LAN_PEERS` or extend the prefix list in `.env`.

## Pi5 Release Candidate Stability Defaults

The Pi5 ARM64 release builder (`ops/build-pi5-arm64-release.sh`) now generates a
self-monitoring stack package. It defaults to `BDAG_NODE_MODE=single`, which
runs `bdag-miner-node-2` only to reduce USB power pressure. Choose `double` in
the installer, or set `BDAG_NODE_MODE=double` with `COMPOSE_PROFILES=dual-node`,
to add `bdag-miner-node-1`.

No-miner deployments are sync-only by default: `BDAG_ENABLE_NODE_MINING=0`,
`BDAG_NODE_MODULES=Blockdag`, and an empty `BDAG_NODE_MINING_ARGS`. Enable node
mining/template flags only when real miners are attached. The dashboard,
watchdog, stack sentinel, P2P guard, peer refresh, chain restore guard, and
snapshot timers are installed by `ops/install-dashboard.sh` unless explicitly
disabled.

Dashboard block height is sourced from chain RPC `getBlockCount`; template
height, logs, fan-in metrics, and main-order values are shown only as
diagnostics. Keep `scripts/validate-pi5-restart-hardening.sh` in the release
gate before cutting an RC.

## Quick start

```bash
# 1. Unzip pool-stack-docker-<tag>.zip

# 2. Run the installer
bash install.sh

# 3. Logs
docker compose logs -f node
docker compose logs -f pool
```

Once everything is running:

- Dashboard: `http://localhost:9280` ( Run in browser, or use the VSC/Cursor Simple Browser! )
- Mining pool Stratum endpoint: `stratum+tcp://localhost:3334`
- RPC endpoint: `http://localhost:38131`

## Dedicated snapshot node (mining stack unchanged)

For hourly or on-demand **snap export**, run a **second** node with its own volumes and host ports so stopping it does not interrupt the pool’s RPC node.

From this directory:

```bash
cp .env.snapshot.example .env.snapshot
cp node.snapshot.conf.example node.snapshot.conf
docker compose -p snapshot-node -f docker-compose.snapshot-node.yml --env-file .env.snapshot build
docker compose -p snapshot-node -f docker-compose.snapshot-node.yml --env-file .env.snapshot up -d
```

- Named volumes **`bdag_snapshot_node_data`** / **`bdag_snapshot_nodeworker_data`** stay separate from the full stack’s `node-data`.
- Default host ports **`9150`** (P2P), **`48131`** (BDAG RPC), **`28545`** / **`28546`** (EVM), **`16060`** (metrics) avoid clashes with the mining compose defaults.
- Point export automation at container **`snapshot-node-node-1`** (see `docker compose -p snapshot-node ps`).
  

# Common operations

## Show the resolved compose config

docker compose config

## Stop everything (keeps volumes)

docker compose down

## Stop + delete named volumes (DESTRUCTIVE)

docker compose down -v

```

```
