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

The installer detects the host OS and CPU architecture, writes `.env` and `node.conf`, generates a strong Postgres password unless `POSTGRES_PASSWORD` is already set, downloads `latest.bdsnap` when needed, and runs `docker compose build && docker compose up -d --no-build --pull never`. The release currently runs the service images as `linux/amd64`; ARM hosts need Docker Desktop or Docker Engine with amd64 emulation enabled.

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

## Mining resource priority

The compose file sets work-conserving Docker CPU and IO weights so mining-path
services win contention without reserving or wasting idle CPU:

| Service | CPU shares | Block IO weight | OOM score | Reason |
| --- | ---: | ---: | ---: | --- |
| `node` | `4096` | `1000` | `-900` | Block templates, validation, and P2P propagation are consensus-critical. |
| `pool` | `3072` | `900` | `-800` | ASIC submits must reach the selected node with the lowest possible tail latency. |
| `postgres` | `3072` | `900` | `-800` | Accounting writes matter, but source code keeps them off the solved-block submit path. |
| `dashboard` | `256` | `100` | `300` | Operator visibility must not compete with paid block production. |

Do not replace these weights with hard CPU quotas or realtime priority unless a
profile proves normal cgroup weighting is insufficient. The goal is maximum paid
blocks per miner-hour, not maximum dashboard refresh rate or synthetic CPU use.

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
FastSnap V2 on empty datadirs and is also appended as startup `--addpeer`
arguments so protocol 46 FastSync peers are available before public fallback
dials dominate startup. V2 is the default on upgraded full nodes; a separate
`BDAG_FASTSNAP_PEERS` value is only needed when the operator wants to pin a
specific artifact source.

`BDAG_FASTSYNC_LAN_PREFIXES` defaults to `192.168.`. If your premises LAN uses
another private range, either put those complete multiaddrs in
`BDAG_FASTSYNC_LAN_PEERS` or extend the prefix list in `.env`.

## Fast Artifact Sync V2 Directory Mode

Fast Artifact Sync V2 directory artifacts are now the preferred empty-datadir
bootstrap path when a peer offers them. The node entrypoint passes both
`--dir-out` and `--out` to `fastsnap`: directory-capable peers install verified
manifest files directly into the node datadir, while archive-only peers still
fall back to the `.bdsnap` path. This keeps new nodes compatible with old seeds
without paying archive assembly overhead when a V2 directory source exists.

`BDAG_FASTSNAP_DIRECTORY_MODE=1` is the default. Set
`BDAG_FASTSNAP_DIRECTORY_STAGING` only when the staging directory must live on a
specific filesystem; otherwise the entrypoint creates a temporary staging path
beside the node datadir. Serving a maintained directory hot stage is opt-in:
set `BDAG_FASTSYNC_ARTIFACT_DIRECTORY` to the verified file root and
`BDAG_FASTSYNC_ARTIFACT_MANIFEST` to the manifest sidecar. When a node was
bootstrapped from a directory artifact, the entrypoint automatically exposes
that verified checkpoint from the node datadir by using
`artifact.manifest.json`.

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
diagnostics. Chain RPC checks retry slow storage-bound samples via
`BDAG_NODE_CHAIN_RPC_TIMEOUT` and `BDAG_NODE_CHAIN_RPC_RETRIES`, and the status
payload exposes the active dashboard URL, RPC latency, and Linux IO pressure
metrics. When PSI is unavailable, the dashboard falls back to `/proc/stat`
`iowait` deltas and raises a maintenance warning after sustained high IO wait.
The ops layer also detects a host profile with `BDAG_HOST_PROFILE=auto` and
uses adaptive worker budgets for expensive dashboard/global/miner scans. The
same release source is expected to behave conservatively on Pi5 or other
constrained ARM64 hosts, while AMD64 and larger ARM64 hosts can use more
parallelism when pressure is low. See
`docs/platform-adaptive-runtime.md`.

The dashboard, watchdog, sync coordinator, P2P guard, and startup checks also
share one cross-process status sample. `ops/status_sampler.py` writes
`ops/runtime/status-sampler.json` atomically, and routine callers read it
through `collect_status_cached()` when it is fresh. Direct repair diagnostics
can still force a live collection with `max_age_seconds=0`.

The Pi5 release builder marks generated runtime compose files with
`BDAG_GENERATED_PI5_RUNTIME_COMPOSE=1` and rejects `build:`/`dockerfile:`
entries in runtime packages. Runtime starts use `--no-build --pull never` by
default; set an explicit pull/build flag only when intentionally refreshing
images. Keep `scripts/validate-pi5-restart-hardening.sh` in the release gate
before cutting an RC, and use `--mode live-runtime` for an installed stack where
`ops/runtime` and Python bytecode are expected service artifacts.
The release builder also runs `scripts/verify-release-architecture.py` before
image assembly so ARM64 packages cannot silently receive AMD64 binaries; the
checker reads ELF/Mach-O/PE headers directly so it can be used from Linux,
macOS, and Windows build hosts.

When testing directly from a source checkout, keep the two dashboard surfaces
separate. The Compose dashboard is the lightweight container UI on
`DASHBOARD_HOST_PORT`/`9280`. The Python operations dashboard is the control
plane normally exposed on `BDAG_DASHBOARD_PORT`/`8088`, and it must be started
with environment that matches the actual container names for the stack it is
watching. On Linux, that process also needs Docker API access; use a system
service account with Docker socket access or an explicit `DOCKER_HOST`. On
macOS and Windows Docker Desktop hosts, prefer the packaged installer or run the
ops dashboard from a session where the Docker CLI already works instead of
installing Linux systemd units.

The dashboard runtime collectors use Python's standard HTTP client for local
pool metrics and public enrichment calls. Do not make live status depend on
host utilities such as `curl`; release packages should behave the same on Pi5
ARM64, Linux AMD64, macOS Docker Desktop, and Windows Docker Desktop once Docker
and Python are available.

For live dashboard/watchdog-only updates, use:

```bash
ops/deploy-live-runtime-update.sh --target /path/to/installed/runtime --mark-runtime-compose
```

The deploy helper copies only a small whitelist, backs up changed files, refuses
dev compose files, validates source and target, restarts only the configured
user services, and rolls back copied files if validation or restart fails.
It also checks that every live-runtime file required by the RC hardening
validator is present in the copy contract before touching the installed stack.

For source and release-candidate performance slices, collect comparable baseline
evidence with:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B ops/optimization_measurement.py --duration-seconds 300 --interval-seconds 15 --label baseline
```

Add `--status-url http://127.0.0.1:8088/api/status` when measuring dashboard
HTTP latency as part of the same run. The harness writes JSONL samples and an
HTML summary under `ops/runtime/measurements`.

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

## Default dual-node FastSnap seeding

Dual-node mining hosts can serve a public P2P FastSnap archive without mining
against a stopped node by using the pool router maintenance handoff. This is a
default release behaviour: `bdag-fastsnap-seed.timer` refreshes the public seed
every two hours at low CPU and I/O priority when
`BDAG_FASTSNAP_SEED_TIMER_ENABLED=1`.

Run an immediate refresh manually with:

```bash
./ops/build-fastsnap-seed.sh
```

The script requires a pool binary with `/admin/rpc-backend-maintenance`,
`POOL_RUNTIME_ADMIN_ENABLED=true`, and `POOL_RPC_ROUTER_ENABLED=true`. It drains
the export backend, proves the pool is still selected on the other backend,
stops only the drained node, exports `snapshot.bdsnap`, restores the node before
heavy verification, then verifies and installs the archive and manifest into
both node datadirs. The installed files are hardlinks to a single archive under
`data-restore/fastsnap`, so the host does not duplicate the node databases or
keep separate per-node snapshot copies.

The export path refuses to publish a stale public seed by default unless the
standby/export backend is within `BDAG_FASTSNAP_MAX_EXPORT_BACKEND_LAG`
main-order units of the selected backend. The default is `1000`.

See `docs/fastsnap-maintenance-handoff.html`.

## Release readiness

Container health alone does not prove that a deployment can mine. Before
marking an install healthy, run:

```bash
./scripts/release-readiness-check.py
./scripts/validate-rc-local.sh
```

These checks do not touch live services. The local RC validator copies the
tracked and unignored source tree to a temporary directory, runs tests with a
temporary runtime directory, and leaves any live `ops/runtime` state in the
checkout alone. It verifies the pool schema, source-health gates, no-miner
service semantics, FastSync/FastSnap safety defaults, dashboard source-of-truth
rules, and packaged self-healing files. See
`docs/release-readiness-gates.html`.
  

# Common operations

## Show the resolved compose config

docker compose config

## Stop everything (keeps volumes)

docker compose down

## Stop + delete named volumes (DESTRUCTIVE)

docker compose down -v

```

```
