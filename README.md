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

Fresh installs assume zero miner sources. Initial install and chain sync must
work with no ASICs or Stratum miners configured; operators can opt in to the
miner wizard after sync and may configure 0..N miner sources. The RC must not
treat this host's five X100 devices as a release default.

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

`MINING_POOL_ADDRESS` is required for pool and miner deployments. The stack
must fail configuration/rendering rather than mine to
`0x0000000000000000000000000000000000000000`.


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

## FastSync Peer Selection

New nodes use protocol 46 Fast Artifact Sync V2 by default and prefer nearby
peers before public internet seeds. Configure complete multiaddrs with peer IDs
in `.env`:

```text
BDAG_P2P_LAN_PEERS=/ip4/192.168.68.55/tcp/8151/p2p/...
BDAG_P2P_VPN_PEERS=/ip4/10.207.244.12/tcp/8151/p2p/...
BDAG_P2P_PUBLIC_PEERS=/ip4/203.0.113.10/tcp/8151/p2p/...
```

The node entrypoint and `ops/update-local-peers.py` fold those values together
with `BDAG_FASTSYNC_PEERS`, `BDAG_FASTSNAP_PEERS`,
`BOOTSTRAP_PEER_ADDRESSES`, and `node.conf` `addpeer` lines. The release
default is `BDAG_FASTSYNC_PEER_ORDERING=tiered-latency`: reachable LAN peers
first, private/VPN peers second, and public internet peers last. Within each
tier, the peer refresh helper sorts candidates by TCP latency so sub-10ms local
or VPN seeds win before slower public routes.
Generic private peers are treated as LAN only when they are on a currently
connected non-VPN host subnet, or when `BDAG_FASTSYNC_LAN_PREFIXES` is set as an
operator override; stale private subnets fall back to the private/VPN tier.

Single-node ASIC-router hosts are detected when the default route is on one
interface, usually WiFi, while the ASIC Ethernet interface owns
`BDAG_ASIC_LAN_CIDRS` (empty by default). Any ASIC-facing subnet is
not a blockchain P2P LAN by default, because directly attached ASICs are
Stratum clients, not FastSync peers. Set `BDAG_ALLOW_ASIC_LAN_P2P=1` only if a
real BlockDAG node is deliberately placed on that Ethernet segment.

Nodes also start with `--fastartifactsync` by default
(`BDAG_FASTARTIFACTSYNC_ENABLED=1`) so they advertise and consume Fast Artifact
Sync V2 whenever the core binary supports it. The sync coordinator treats more
than `BDAG_SYNC_COORDINATOR_FAR_BEHIND_BLOCKS=1000` remaining blocks as an
automatic fastest-catch-up condition: it raises the selected leader's Docker CPU
and IO weights, keeps duplicate sync work paused in dual-node mode, and restarts
an unaccelerated or stale leader after the cooldown window so startup peer order
and V2 artifact serving are active.

USB-backed ASIC router/mining profiles are the exception. Leave
`BDAG_NO_FASTSYNC_SERVE=auto` enabled, or set it to `1`, when a Pi5 runs chain
data from USB and also serves DHCP/NAT to directly attached ASICs. The node will
still consume sync and relay found blocks, but it will not advertise bulk
FastSync range, snapshot, or artifact serving from the same USB-backed miner.

`BDAG_FASTSYNC_LAN_PEERS`, `BDAG_FASTSYNC_VPN_PEERS`, and
`BDAG_FASTSYNC_PUBLIC_PEERS` remain accepted as compatibility aliases. Set
`BDAG_FASTSYNC_PEER_ORDERING=flat-latency` only to reproduce the older flat
latency path during a rollback.

## Fast Artifact Sync V2 Directory Mode

Fast Artifact Sync V2 directory artifacts are now the preferred empty-datadir
bootstrap path when a peer offers them. The node entrypoint first checks whether
the packaged `fastsnap` binary supports directory install flags. When supported,
it passes both `--dir-out` and `--out`: directory-capable peers install verified
manifest files directly into the node datadir, while archive-only peers still
fall back to the `.bdsnap` path. If the binary is older, the entrypoint stays on
the V2 archive path instead of failing before normal sync can start.

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
payload exposes the active dashboard URL, RPC latency, Linux IO pressure
metrics, and the resolved capability profile. When PSI is unavailable, the
dashboard falls back to `/proc/stat` `iowait` deltas and raises a maintenance
warning after sustained high IO wait. The ops layer detects both
`BDAG_HOST_PROFILE=auto` and `BDAG_CAPABILITY_PROFILE=auto`, then uses adaptive
worker budgets for expensive dashboard/global/miner scans. Capability profiles
include chain storage class and ASIC-router topology, so a USB-backed Pi5
router is treated differently from an NVMe dual-node server even when both are
ARM64. See `docs/platform-adaptive-runtime.md`.

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

Constrained mining appliances also run a read-only install preflight before
chain seeding or stack start. `scripts/mining-appliance-preflight.py` checks the
host/capability profile, root and chain-data free space, filesystem and mount
options, single-node duplicate data, swap sizing, Docker root placement,
network route, schema presence, and resource-sensitive `.env` defaults. The
installer reports warnings and continues by default. Set
`BDAG_APPLIANCE_PREFLIGHT_STRICT=1` to make hard failures stop the install, or
`BDAG_APPLIANCE_PREFLIGHT=0` to skip it explicitly. The field report behind
these checks is in
`docs/t430-single-node-appliance-hardening.md`.

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
`docs/release-readiness-gates.html`. Active multi-miner deployments, including
five-X100 hosts, must also preserve the template-conversion release guard in
`docs/five-asic-template-conversion-guard.html`: accepted block conversion per
miner-hour is the success metric for active multi-miner deployments, and
tip-overdue, duplicate-local, invalidated-job, and non-current-job losses must
not be hidden by connected miner count alone. The guard is conditional on the
configured or observed miner source count; five miners are not an install-time
default. FastSnap maintenance must keep the CPU cap guard in
`docs/fastsnap-maintenance-resource-guard.html` and must not run archive
finalization or verification without an explicit bounded CPU policy.

Issue #26 final-release mitigations are captured in
`docs/final-release-issue-26-checklist.md`; keep that checklist current when
changing pinned source repos, installer reset behavior, V2 sync defaults, or
release packaging.
  

# Common operations

## Show the resolved compose config

docker compose config

## Stop everything (keeps volumes)

docker compose down

## Stop + delete named volumes (DESTRUCTIVE)

docker compose down -v

```

```
