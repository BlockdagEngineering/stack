# pool-stack-docker-stack

This stack can be run in any environment where docker is installed. It includes an upgradable BDAG node, a mining pool with its database, a read-only collector API, and the Go dashboard UI.


| Service | Image / build | Purpose |
| --- | --- | --- |
| `node` | BlockDAG node, supervised by nodeworker | Consensus, P2P, and RPC |
| `pool` | Mining pool (Stratum :3334) | ASIC Stratum and block submission |
| `pool-db` | Postgres | Pool persistence, schema auto-loaded |
| `collector` | Python collector | Read-only JSON API and normalized logs |
| `dashboard` | Go dashboard | Browser UI over the collector API |


## Release package

GitHub Releases attach pinned bootstrap scripts (`install.sh` for Linux/macOS
and `install.ps1` for Windows) plus one runtime payload zip per Linux container
architecture:

- `pool-stack-docker-<tag>-linux-amd64.zip`
- `pool-stack-docker-<tag>-linux-arm64.zip`

The bootstrap script is generated for one release tag. It detects the host OS
and CPU architecture, selects `linux-amd64` or `linux-arm64`, and downloads only
the matching payload zip from that same tag. Linux ARM64, macOS ARM64, and
Windows ARM64 hosts use the `linux-arm64` runtime payload.

Each payload zip contains `bin/` (pre-built `blockdag-node`, `nodeworker`,
`mining-pool`, `dashboard-api`, and `dashboard`), `docker-compose.yml`, `dockerfile`,
`.env.example`,
`docker/`, and the cross-platform payload installers. **Node and pool release
images** stage binaries from `./bin`; the `collector` image checks out
the `develop` branch from `BlockdagEngineering/collector`. Export
`GITHUB_TOKEN` before `docker compose build` if that repository is private in
your environment.

Run the bootstrap script from the GitHub release, or manually unpack the
matching payload zip and run the payload installer from the extracted directory:

```bash
# Linux / macOS
bash install.sh
```

```powershell
# Windows
.\install.ps1
```

The payload installer writes `.env` and `node.conf`, generates a strong Postgres
password unless `POSTGRES_PASSWORD` is already set, downloads `latest.bdsnap`
when needed, sets `DOCKER_PLATFORM` from the downloaded payload's
`release-payload.env`, and runs
`docker compose build && docker compose up -d --no-build --pull never`.

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


The `**pool`** image bakes `**.env.example`** into the image at `/var/lib/bdagStack/pool/.env` for `godotenv` (release `**dockerfile`** uses `**COPY .env.example**` from repo root; git dev `**dockerfile-dev**` copies it from the named `**stack_src**` context). Compose still sets most variables via `environment:`.

## Mining resource priority

The compose file sets work-conserving Docker CPU and IO weights so mining-path
services win contention without reserving or wasting idle CPU:

| Service | CPU shares | Block IO weight | OOM score | Reason |
| --- | ---: | ---: | ---: | --- |
| `node` | `4096` | `1000` | `-900` | Block templates, validation, and P2P propagation are consensus-critical. |
| `pool` | `3072` | `900` | `-800` | ASIC submits must reach the selected node with the lowest possible tail latency. |
| `postgres` | `3072` | `900` | `-800` | Accounting writes matter, but source code keeps them off the solved-block submit path. |
| `collector` | `256` | `100` | `300` | Read-only status collection must not compete with paid block production. |
| `dashboard` | `256` | `100` | `300` | Browser UI reads the collector API; same low-priority class as `collector`. |

Do not replace these weights with hard CPU quotas or realtime priority unless a
profile proves normal cgroup weighting is insufficient. The goal is maximum paid
blocks per miner-hour, not maximum dashboard refresh rate or synthetic CPU use.

## P2P Peer Configuration

Configure complete P2P multiaddrs with peer IDs in `.env` or `node.conf`.
`BOOTSTRAP_PEER_ADDRESSES` and `node.conf` `addpeer` lines are ordinary startup
peers; address class is not a sync mode, priority class, or eligibility signal.

Old installations may still contain legacy address-bucket variable names.
`ops/update-local-peers.py` treats them only as migration input and clears the
bucket values. Do not add new LAN, VPN, or public sync options.

## Runtime Stability Defaults

No-miner deployments are sync-only by default: `BDAG_ENABLE_NODE_MINING=0`,
`BDAG_NODE_MODULES=Blockdag`, and an empty `BDAG_NODE_MINING_ARGS`. Enable node
mining/template flags only when real miners are attached. The collector is a
read-only JSON API: it does not restart services, clean-restore chain data,
configure miners, save ASIC credentials, or expose action tokens. Runtime
tooling defaults to the current single-node stack names `node`, `pool`, and
`pool-db`.

Collector block height is sourced from chain RPC `getBlockCount`; template
height, logs, and main-order values are shown only as
diagnostics. Build and release flows should run through
`scripts/bdag-low-io-build.sh`, which uses idle I/O priority, low CPU priority,
and `BDAG_BUILD_TMPDIR` so image builds do not compete with chain sync or block
submission. Chain RPC checks retry slow storage-bound samples via
`BDAG_NODE_CHAIN_RPC_TIMEOUT` and `BDAG_NODE_CHAIN_RPC_RETRIES`, and the status
payload exposes RPC latency and Linux IO pressure metrics. When PSI is unavailable, the collector falls back to `/proc/stat`
`iowait` deltas and raises a maintenance warning after sustained high IO wait.
The ops layer also detects a host profile with `BDAG_HOST_PROFILE=auto` and
uses adaptive worker budgets for expensive collector/global/miner scans. The
same release source is expected to behave conservatively on constrained ARM64
hosts, while AMD64 and larger ARM64 hosts can use more parallelism when pressure
is low. See `docs/platform-adaptive-runtime.md`.

The collector, sync coordinator, P2P guard, and startup checks also share one
cross-process status sample. `ops/status_sampler.py` writes
`ops/runtime/status-sampler.json` atomically, and routine callers read it
through `collect_status_cached()` when it is fresh. The default sampler reuse
window is bounded at 120 seconds so constrained hosts do not repeatedly probe
Docker, node RPC, pool metrics, and miner state while the node is catching up.
Diagnostics can still force a live collection with `max_age_seconds=0`.

The Pi5 release builder marks generated runtime compose files with
`BDAG_GENERATED_PI5_RUNTIME_COMPOSE=1` and rejects `build:`/`dockerfile:`
entries in runtime packages. Runtime starts use `--no-build --pull never` by
default; set an explicit pull/build flag only when intentionally refreshing
images. Keep `scripts/validate-pi5-restart-hardening.sh` in the release gate
before cutting an RC, and use `--mode live-runtime` for an installed stack where
`ops/runtime` and Python bytecode are expected service artifacts.

Constrained mining appliances also run a read-only install preflight before
chain seeding or stack start. `scripts/mining-appliance-preflight.py` checks the
host profile, root and chain-data free space, filesystem and mount options,
storage profile split, duplicate node data, swap sizing, Docker root
placement, network route, schema presence, and resource-sensitive `.env`
defaults. The installer resolves `BDAG_STORAGE_PROFILE=auto` into concrete
chain, Postgres, and runtime paths so capacity USB storage can carry the growing
chain while internal or other non-USB storage absorbs small frequent writes when
it has enough headroom. USB-backed chain data always prefers this split. Small
ephemeral scratch is kept on bounded tmpfs through `BDAG_EPHEMERAL_DIR`,
`BDAG_CONTAINER_TMPFS_SIZE`, and node-specific `BDAG_NODE_TMPFS_SIZE`; service
containers also mount `/var/tmp` as tmpfs and export `TMPDIR`, `TMP`, and
`TEMP` to avoid accidental temp spillover into overlay layers. Large
snapshot and chain-artifact staging stays on capacity storage unless
deliberately overridden. The installer reports
warnings and continues by default. Set `BDAG_APPLIANCE_PREFLIGHT_STRICT=1` to
make hard failures stop the install, or `BDAG_APPLIANCE_PREFLIGHT=0` to skip it
explicitly. The field report behind these checks is in
`docs/t430-appliance-hardening.md`.

The release builder also runs `scripts/verify-release-architecture.py` before
image assembly so ARM64 packages cannot silently receive AMD64 binaries; the
checker reads ELF/Mach-O/PE headers directly so it can be used from Linux,
macOS, and Windows build hosts.

The dashboard UI is normally exposed on host port `8080`. It talks to the
collector API, which is bound to localhost on host port `9280` by default. Global
production data must be sourced from native BlockDAG chain RPC
`getBlockCount`/ordered block/coinbase calls. EVM RPC belongs to wallet balance
views only.

When testing directly from a source checkout, start the collector with
environment that matches the actual container names for the stack it is
watching. On Linux, that process needs Docker API access for container status
and logs; use a system service account with Docker socket access or an explicit
`DOCKER_HOST`.

Source checkout tests require Python's standard library test runner plus
`pytest`. On Ubuntu/Debian hosts, install the test dependency with:

```bash
sudo apt-get update
sudo apt-get install -y python3-pytest
```

Agents should verify it with `python3 -m pytest --version` before running
`ops/tests` through pytest-backed deployment checks.

The collector runtime uses Python's standard HTTP client for local
pool metrics and public enrichment calls. Do not make live status depend on
host utilities such as `curl`; release packages should behave the same on Linux
AMD64, Linux ARM64, macOS Docker Desktop, and Windows Docker Desktop once Docker
and Python are available.

For live collector-only updates, use:

```bash
ops/deploy-live-runtime-update.sh --target /path/to/installed/runtime --mark-runtime-compose
```

The deploy helper copies only a small whitelist, backs up changed files, refuses
dev compose files, validates source and target, restarts only the configured
services, and rolls back copied files if validation or restart fails.
It also checks that every live-runtime file required by the RC hardening
validator is present in the copy contract before touching the installed stack.

For source and release-candidate performance slices, collect comparable baseline
evidence with:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B ops/optimization_measurement.py --duration-seconds 300 --interval-seconds 15 --label baseline
```

Add `--status-url http://127.0.0.1:9280/api/status` when measuring collector
HTTP latency as part of the same run. The harness writes JSONL samples and an
HTML summary under `ops/runtime/measurements`.

## Quick start

```bash
# 1. Run the pinned bootstrap from the GitHub release, or unzip the matching
#    pool-stack-docker-<tag>-linux-<arch>.zip payload.

# 2. Run the installer
bash install.sh

# 3. Logs
docker compose logs -f node
docker compose logs -f pool
```

To include optional services controlled by `.env`, set `COMPOSE_PROFILES` before
`docker compose up`. Example: `COMPOSE_PROFILES=miner` enables the CPU miner
service; leave `COMPOSE_PROFILES` empty to disable it.

Once everything is running:

- Dashboard: `http://localhost:8080`
- Collector API: `http://localhost:9280`
- Mining pool Stratum endpoint: `stratum+tcp://localhost:3334`
- RPC endpoint: `http://localhost:38131`

## Sync Defaults

New installs bootstrap from the installer-managed `latest.bdsnap` snapshot when
available, then continue normal P2P sync. `SYNC_SOURCE_NODE=0` remains the
release default so constrained hosts do not advertise themselves as special
sync sources during first boot.

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
service semantics, dashboard source-of-truth rules, and packaged self-healing
files. See
`docs/release-readiness-gates.html`. Active multi-miner deployments, including
five-X100 hosts, must also preserve the template-conversion release guard in
`docs/five-asic-template-conversion-guard.html`: accepted block conversion per
miner-hour is the success metric for active multi-miner deployments, and
tip-overdue, duplicate-local, invalidated-job, and non-current-job losses must
not be hidden by connected miner count alone. The guard is conditional on the
configured or observed miner source count; five miners are not an install-time
default. Background maintenance must preserve bounded CPU/I/O policy.

Issue #26 final-release mitigations are captured in
`docs/final-release-issue-26-checklist.md`; keep that checklist current when
changing pinned source repos, installer reset behavior, sync defaults, or
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
