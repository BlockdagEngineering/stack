# pool-stack-docker-stack

This stack can be run in any environment where docker is installed. It includes
an upgradable BDAG node, a mining pool with its database, the Redis-backed
dashboard runtime from `BlockdagEngineering/redis-dash`, and containerized
watchdog/sentinel repair services.

On Ubuntu/Debian hosts, install the required Docker packages before running the
payload installer:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 docker-buildx
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Open a new shell, then verify `docker compose version` works without sudo.


| Service | Image / build | Purpose |
| --- | --- | --- |
| `node` | BlockDAG node, supervised by nodeworker | Consensus, P2P, and RPC |
| `pool` | Mining pool (Stratum :3334) | ASIC Stratum and block submission |
| `pool-db` | Postgres | Pool persistence, schema auto-loaded |
| `dashboard` | Redis-backed Go dashboard | Browser UI, private Redis state, live ingest, status API |
| `status-sampler` | Python ops runtime | Shared low-impact status sampling and mining imperative checks |
| `watchdog` / `sentinel` | Python ops runtime | Runtime repair and last-resort liveness recovery |


## Release package

GitHub Releases attach a pinned Linux bootstrap script (`install.sh`) plus one
runtime payload zip per Linux container architecture:

- `pool-stack-docker-<tag>-linux-amd64.zip`
- `pool-stack-docker-<tag>-linux-arm64.zip`

The bootstrap script is generated for one release tag. It requires Linux,
detects the host CPU architecture, selects `linux-amd64` or `linux-arm64`, and
downloads only the matching payload zip from that same tag.

Each payload zip contains `bin/` (pre-built `blockdag-node`, `nodeworker`,
`mining-pool`, `dashboard-api`, and `dashboard`), `docker-compose.yml`,
`dockerfile`, `.env.example`, `docker/`, one Linux payload installer
(`install.sh`), and the ops scripts required by the repair services. **Release images** stage
their binaries from `./bin`; release workflow source checkouts are limited to
`BlockdagEngineering/blockdag-corechain`, `BlockdagEngineering/pool`, and
`BlockdagEngineering/redis-dash` from `main`. Legacy collector, dashboard2,
CPU-miner, and GPU-miner source trees are not packaged or run.

Run the bootstrap script from the GitHub release, or manually unpack the
matching payload zip and run the payload installer from the extracted directory:

```bash
# Linux
bash install.sh
```

The payload installer makes two independent choices in two steps:

**Step 1 — what to install:**

1. **Mining pool stack with dashboard** (default) — the full stack: node, pool,
   Postgres, redis-dash dashboard, status sampler, watchdog, and sentinel.
2. **Standalone node only** — just the node, no pool/dashboard/ASIC services.

**Step 2 — chain data type (applies to either deployment):**

1. **Non-archive** (default) — pruned chain data, bootstrapped from the standard
   snapshot.
2. **Archive** — node started with `--archival` (consensus keeps full block
   history instead of pruning), bootstrapped from the archive snapshot.

Use installer flags or set `BDAG_DEPLOY_KIND=pool|node` and/or
`BDAG_CHAIN_MODE=archive|non-archive` to preselect either step for
non-interactive installs. The legacy
`BDAG_INSTALL_MODE=pool|archive-node|node` is still accepted and seeds both.

```bash
# Linux examples
bash install.sh --node              # standalone node; installer prompts chain mode
bash install.sh --node --archive    # archive standalone node, no prompt
bash install.sh --pool --no-archive # full pool stack with pruned node, no prompt
bash install.sh --pool --no-wait-for-node-sync # start stack while node catches up
```

The chain-data choice writes `BDAG_NODE_ARCHIVAL=0|1` in `.env`. Choosing archive
sets `BDAG_NODE_ARCHIVAL=1`, which makes the node entrypoint append the
`--archival` flag. Snapshot download is optional and node-owned: pass
`--snapshot-url` to the installer if you want first start to download/import a
`.bdsnap`; otherwise the node syncs from peers. The installer writes
`BDAG_SNAPSHOT_URL`, and the node only downloads from it when the configured
datadir has no local snapshot or chain data.

The payload installer writes `.env` and `node.conf`, generates a strong Postgres
password unless `POSTGRES_PASSWORD` is already set, sets `DOCKER_PLATFORM` from
the downloaded payload's `release-payload.env`, builds Docker images, starts the
node first, asks whether to wait for node sync before starting the rest of the
stack, and waits for sync by default. Answer no, pass
`--no-wait-for-node-sync`, or set `BDAG_WAIT_FOR_NODE_SYNC_BEFORE_STACK=0` to
start every remaining service declared by `docker-compose.yml` while the node
continues syncing. Node-only installs build and start only the `node` service.
For pool-stack installs, the installer asks whether a local ASIC miner is
present before asking for ASIC LAN details; answer no to leave local ASIC
discovery scope empty.

Fresh installs assume zero miner sources. Initial install and chain sync must
work with no ASICs or Stratum miners configured; operators can opt in to the
miner wizard after sync and may configure 0..N miner sources. The RC must not
treat this host's five X100 devices as a release default.

The installer uses host-path chain storage at `NODE_DATA_DIR` and preserves
existing chain data. To replace existing chain data, stop the stack and move the
configured datadir aside deliberately before running the installer.

`NODE_DATA_DIR` is the only supported node datadir variable. The obsolete
`BDAG_NODE_DATA_DIR` name must not be written by installers. Before a clean
upgrade or destructive reinstall, if an old `stack_node-data` Docker volume or
another preserved datadir is newer or larger than `./node-data`, migrate it into
`./node-data` with
`scripts/migrate-node-data-volume-to-host.sh` before starting the node.

To import a `.bdsnap` on first node start, point the installer at the snapshot
URL you want to use:

```bash
bash install.sh --snapshot-url https://your-host.example/latest.bdsnap
```

If Docker reports an `xattr` error for files such as `._.env.example`, those are metadata files from the extracted folder or external drive. Current release packages include `.dockerignore` and the installer removes those files before building. For an older extracted folder, clean it manually and run the installer again:

```bash
find . -name '._*' -type f -delete
find . -name '.DS_Store' -type f -delete
rm -rf __MACOSX
bash install.sh
```

The same cleanup also ignores common desktop metadata such as `Thumbs.db`, `desktop.ini`, `$RECYCLE.BIN`, and `System Volume Information`.

## Configuration (what loads where)

Docker Compose reads `**.env`** in this directory for variable substitution and passes pool / miner settings into containers.


| Piece           | Purpose                                                                                                                                                                                                                                                                         |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `**node.conf`** | **Project root.** Mounted into the `**node`** container as `/etc/bdagStack/node.conf` (peers, `miningaddr`, RPC modules). **Copy from `node.conf.example**` — `node.conf` is gitignored. `**rpcuser` / `rpcpass` here must match `NODE_RPC_USER` / `NODE_RPC_PASS` in `.env`.** |
| `**.env`**      | Start from `**.env.example`**. `******NODE_RPC_URL` / `**PG_URL**` are set in `docker-compose.yml`. **Miner:** `MINER_POOL_URL`, `MINING_POOL_ADDRESS`, `MINER_POOL_PASS`, `MINER_WORKERS`.                                                                                     |

`MINING_POOL_ADDRESS` is required for pool and miner deployments. The stack
must fail configuration/rendering rather than mine to
`0x0000000000000000000000000000000000000000`.


The `**pool`** image bakes `**.env.example`** into the image at `/var/lib/bdagStack/pool/.env` for `godotenv` (the release `**dockerfile`** uses `**COPY .env.example**` from repo root). Compose still sets most variables via `environment:`.

## Mining resource priority

The compose file sets work-conserving Docker CPU and IO weights so mining-path
services win contention without reserving or wasting idle CPU:

| Service | CPU shares | Block IO weight | OOM score | Reason |
| --- | ---: | ---: | ---: | --- |
| `node` | `6144` | `1000` | `-900` | Block templates, validation, and P2P propagation are consensus-critical. |
| `pool` | `5120` | `950` | `-800` | ASIC submits must reach the selected node with the lowest possible tail latency. |
| `postgres` | `4096` | `950` | `-800` | Accounting writes matter, but source code keeps them off the solved-block submit path. |
| `dashboard` | `128` | `100` | `300` | Operator visibility must not compete with paid block production. |

Do not replace these weights with hard CPU quotas or realtime priority unless a
profile proves normal cgroup weighting is insufficient. The goal is maximum paid
blocks per miner-hour, not maximum dashboard refresh rate or synthetic CPU use.

## P2P Peer Configuration

Configure complete P2P multiaddrs with peer IDs in `.env` or `node.conf`.
`BOOTSTRAP_PEER_ADDRESSES` and `node.conf` `addpeer` lines are ordinary startup
peers; address class is not a sync mode, priority class, or eligibility signal.

During upgrades, `ops/update-local-peers.py` imports any existing
address-bucket values only long enough to normalize complete P2P multiaddrs
into `BDAG_FASTSYNC_PEERS`, then clears those bucket values. Do not add new LAN,
VPN, or public sync options.

Upgrades that keep existing chain data should also mine that data for peer
evidence. After the node starts, the release installer runs
`ops/update-local-peers.py --force-apply`, parses preserved chain peerstore
startup logs, probes candidate multiaddrs for TCP reachability, writes
`ops/runtime/peer-discovery-current.json`, and applies the resulting
`BDAG_FASTSYNC_PEERS` to the active single node. Peerstore-derived candidates
are intentionally filtered to public bootstrap-style service ports
(`BDAG_CHAIN_PEERSTORE_SERVICE_PORTS`, default `8150,8151,8152,8153,8154`) so
private LAN addresses and transient high NAT ports cannot poison the next
restart. TCP-open status is only a bootstrap hint; install completion and mining
readiness still require normal peer handshakes, at least two fresh consensus
peers, sync freshness, RPC health, and template checks.

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

## IPFS Content Discovery

Future systems should read `ops/ipfs-content-discovery.json` for the durable
IPFS/IPNS discovery contract. The stable latest pointer is
`/ipns/k51qzi5uqu5djjlh4vxtmzyswx0qk4s3wdlf3yrpkszp38gq5sl71zcgmmc3jk`; the current
immutable latest-index CID is
recorded in that discovery file. At the first live segment publish on
2026-05-31 it was `bafkreifvqj7qhkoifykybbvlxxmq3jhydgzq2kjxuq5fznjizjjogzgthi`.
The stale monolithic FastSnap seed has been deprecated. The current implementation
writes append-only live-tail chain-order segments from the local node. The
durable protocol design is recorded in
`docs/ipfs-append-only-segment-protocol.html`. IPFS and IPNS are
not chain trust. Receivers must verify segment CIDs, payload hashes, order
continuity, network/genesis identity, tip/state roots, and normal consensus
before using the data.

## Runtime Stability Defaults

No-miner deployments are sync-only by default: `BDAG_ENABLE_NODE_MINING=0`,
`BDAG_NODE_MODULES=Blockdag,miner`, and an empty `BDAG_NODE_MINING_ARGS`. Enable node
mining/template flags only when real miners are attached. Do not add unsynced
mining bypass flags; readiness gates must fail closed until node sync and P2P
freshness are healthy. The dashboard,
watchdog, stack sentinel, P2P guard, peer refresh, chain restore guard, and
snapshot timers are installed by `ops/install-dashboard.sh` unless explicitly
disabled. Runtime tooling uses the current stack service names: `node`, `pool`,
and `postgres`. Concrete Compose container names may include project and ordinal
suffixes.

Catch-up has priority over mining when a production node is I/O-bound while it
is behind peers or while the selected backend is not mineable/submit-ready.
`BDAG_CATCHUP_IO_PRESSURE_PAUSE_ENABLED=1` makes this the primary mitigation
using `iowait`, `io_some`, and `io_full` pressure signals; a production node
more than `BDAG_CATCHUP_PAUSE_THRESHOLD_BLOCKS=300` blocks behind peers is the
backup trigger when pressure signals are missing or delayed.
The status sampler stops the pool, disables node mining/template runtime churn,
raises the node cache toward `BDAG_CATCHUP_NODE_CACHE_MB` within the host memory
budget, and recreates only the node service when that runtime change is needed.
The dashboard reports this as a deliberate catch-up pause, not a pool failure,
and tells operators to leave miners configured until I/O pressure drops, peer lag
is back inside the safe window, and template health is ready.

Dashboard block height is sourced from chain RPC `getBlockCount`; template
height, logs, and main-order values are shown only as
diagnostics. Build and release flows should run through
`scripts/bdag-low-io-build.sh`, which uses idle I/O priority, low CPU priority,
and `BDAG_BUILD_TMPDIR` so image builds do not compete with chain sync or block
submission. Chain RPC checks retry slow storage-bound samples via
`BDAG_NODE_CHAIN_RPC_TIMEOUT` and `BDAG_NODE_CHAIN_RPC_RETRIES`, and the status
payload exposes RPC latency and Linux IO pressure metrics. When PSI is
unavailable, the status sampler falls back to `/proc/stat` `iowait` deltas and
raises a maintenance warning after sustained high IO wait.
The ops layer also detects a host profile with `BDAG_HOST_PROFILE=auto` and
uses adaptive worker budgets for expensive dashboard/global/miner scans. The
same release source is expected to behave conservatively on constrained ARM64
hosts, while AMD64 and larger ARM64 hosts can use more parallelism when pressure
is low. See `docs/platform-adaptive-runtime.md`.

The dashboard, sync coordinator, P2P guard, and startup checks also share one
cross-process status sample. `ops/status_sampler.py` writes
`ops/runtime/status-sampler.json` atomically, and routine callers read it
through `collect_status_cached()` when it is fresh. The default sampler reuse
window is bounded at 120 seconds so constrained hosts do not repeatedly probe
Docker, node RPC, pool metrics, and miner state while the node is catching up.
Diagnostics can still force a live collection with `max_age_seconds=0`.
Repair actors should acquire stack status through `ops/stack_status_source.py`.
That module prefers the dashboard status API, then falls back to the shared
status sampler/direct collection path, so watchdogs and sentinels do not each
recreate their own monitoring fallback order.

For offline triage testing, `ops/stack_status_source.py` also accepts a fixture
payload via `BDAG_STATUS_SOURCE_FIXTURE` or `BDAG_STATUS_SOURCE_FIXTURE_FILE`.
Capture a live payload with `ops/capture_status_payload.py`, then replay it
through the guards with `ops/replay_triage.py`. Watchdog, sentinel, and the
30-minute mining guard all support dry-run execution so they can classify
incidents without mutating the stack.

If a node stops importing while peers continue advancing, the dashboard must not
describe the state as ordinary catch-up. Node logs that contain `Irreparable
error`, `Not DAG block`, DAG tip/block damage, or repeated `missing trie node`
warnings are chain-data restore triggers. The status sampler fails mining closed,
starts the one-shot `${INSTANCE}-chain-state-self-heal.service`, and the script
`ops/chain-state-self-heal.sh` quarantines the damaged node datadir, restores
from `BDAG_CHAIN_STATE_RESTORE_SOURCE` or `BDAG_CHAIN_STATE_RESTORE_SNAPSHOT`,
restarts `node` and `dashboard` with `--no-build --pull never`, and leaves
`pool` stopped until readiness gates pass. A softer adjacent detector records
sustained stuck height while peer lag grows; by default it requires 900 seconds,
at least 1000 blocks of peer lead, and 60 blocks of gap growth before it triggers
the same fail-closed self-heal flow. Remote restore sources should use key-based
SSH via `BDAG_CHAIN_STATE_RESTORE_SSH_COMMAND`; do not put passwords in source or
checked-in env files.

The Pi5 release builder marks generated runtime compose files with
`BDAG_GENERATED_PI5_RUNTIME_COMPOSE=1` and rejects `build:`/`dockerfile:`
entries in runtime packages. Runtime starts use `--no-build --pull never` by
default; set an explicit pull/build flag only when intentionally refreshing
images. Runtime package validation should be performed through the active
release build checks before cutting an RC.

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

Mining hosts install `bdag-mining-host-tuning.service` and timer through
`ops/install-p2p-services.sh`; fresh release installs run that support-service
installer after the stack starts. The release installer also applies
`scripts/install-mining-appliance-profile.sh` in non-destructive mode by
default, which installs sysctl/tmpfiles/Docker log defaults and a recurring
runtime-priority timer without masking common background services unless
`BDAG_INSTALL_APPLIANCE_PROFILE_DISABLE_SERVICES=1` is set. The tuning script
discovers the active Compose containers, raises node/pool/Postgres CPU and
block I/O weights, applies process `nice`/`ionice`, writes cgroup v2
`memory.low` protection, and keeps selected host interfaces on `fq_codel` when
`tc` is available. Docker does not provide a portable per-container network
priority control in this release path; network protection is host qdisc tuning
plus keeping mining-critical process, CPU, and disk I/O scheduling ahead of
dashboard and maintenance work. The policy is safe to reapply and uses the
`BDAG_*_CPU_SHARES`, `BDAG_*_MEMORY_LOW`, and `BDAG_TUNE_NET_QDISC` knobs from
`.env`.

The release builder also runs `scripts/verify-release-architecture.py` before
image assembly so ARM64 packages cannot silently receive AMD64 binaries; the
checker reads binary headers directly so it can be used from Linux build hosts.

The dashboard UI and status API are normally exposed on host port `8088`.
Global production data must be sourced from native BlockDAG chain RPC
`getBlockCount`/ordered block/coinbase calls. EVM RPC belongs to wallet balance
views only. The packaged redis-dash UI on `DASHBOARD_HOST_PORT` is the
authoritative operational dashboard for this stack line.

When testing directly from a source checkout, start the dashboard/status API
with environment that matches the actual container names for the stack it is
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

The ops status runtime uses Python's standard HTTP client for local pool metrics
and public enrichment calls. Do not make live status depend on host utilities
such as `curl`; release packages should behave the same on Linux AMD64 and
Linux ARM64 once Docker and Python are available.

For live runtime updates, use:

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

Add `--status-url http://127.0.0.1:8088/api/status` when measuring dashboard
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

Once everything is running:

- Dashboard and status API: `http://localhost:8088`
- Status payload: `http://localhost:8088/api/status`
- Mining pool Stratum endpoint: `stratum+tcp://localhost:3334`
- RPC endpoint: `http://localhost:38131`

For ASIC deployments, the installer records the host-facing pool address and
ASIC LAN scope in `.env` as `BDAG_POOL_HOST`, `BDAG_POOL_URL`,
`BDAG_MINER_SCAN_TARGET`, and `BDAG_ASIC_LAN_CIDRS`. The dashboard and repair
tools use those values instead of guessing from inside Docker. Docker bridge
networks default to `172.16.0.0/12` and are filtered from ASIC discovery and
displayed Stratum endpoints; seeing `172.*` as a miner IP or pool endpoint is a
configuration failure, not a valid physical miner.

## Default V2 Sync Source

New installs use the canonical `NODE_DATA_DIR=./node-data` chain-data path. Node
startup and runtime provenance checks validate the selected chain DB. If a valid
preserved volume, USB copy, Downloads archive, or configured chain DB archive is
available, select or migrate it before the node starts.

For local archives, inspect content rather than trusting the filename. A file
ending in `.tar.gz` may still be a zstd tar archive. Archives that contain
`BdagChain/`, `bdageth/`, and `metaData` are chain/EVM payloads, not complete
runtime identity. Preserve `mainnet/peerstore`, `mainnet/network.key`, and
`mainnet/recent-peers.json` from the existing install when available, quarantine
old chain directories, and restore ownership to the container UID/GID before
starting node.

Fresh-from-genesis is allowed only when no better chain data exists or when the
operator explicitly approves a fresh chain start. Pool startup remains gated on
node data provenance and node readiness.

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
