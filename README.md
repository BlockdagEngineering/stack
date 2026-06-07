# pool-stack-docker-stack

This stack can be run in any environment where docker is installed. It includes an upgradable BDAG node, a mining pool with its db, and the canonical operations dashboard/control plane.


| Service     | Image / build                           | Purpose |
| ----------- | --------------------------------------- | ------- |
| `node`      | BlockDAG node, supervised by nodeworker |         |
| `pool`      | Mining pool (Stratum :3334)             |         |
| `postgres`  | Pool persistence, schema auto-loaded    |         |
| `dashboard` | Essential monitoring                    |         |


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
`mining-pool`, and `dashboard-api`), `docker-compose.yml`, `dockerfile`,
`.env.example`,
`docker/`, and the cross-platform payload installers. **Node and pool release
images** stage binaries from `./bin`; the `dashboard` image uses the
compose-provided `dashboard_src` build context from the checked-out
`BlockdagEngineering/dashboard` `develop` tree.

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
password unless `POSTGRES_PASSWORD` is already set, sets `DOCKER_PLATFORM` from
the downloaded payload's `release-payload.env`, and runs
`docker compose build && docker compose up -d --no-build --pull never postgres node dashboard`.

Fresh installs assume zero miner sources. Initial install and chain sync must
work with no ASICs or Stratum miners configured; operators can opt in to the
miner wizard after sync and may configure 0..N miner sources. The RC must not
treat this host's five X100 devices as a release default.

The installer uses host-path chain storage at `BDAG_NODE_DATA_DIR` and preserves
existing chain data. Chain recovery/bootstrap is owned by the raw-datadir/IPFS
sidecar path, not by legacy monolithic archive downloads or build-time imports.
To replace existing chain data, stop the stack and move the configured datadir
aside deliberately before running the IPFS/raw-datadir restore flow.

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
into `BOOTSTRAP_PEER_ADDRESSES`, then clears those bucket values. Do not add new LAN,
VPN, or public sync options.

Upgrades that keep existing chain data should also mine that data for peer
evidence. After the node starts, the release installer runs
`ops/update-local-peers.py --force-apply`, parses preserved chain peerstore
startup logs, probes candidate multiaddrs for TCP reachability, writes
`ops/runtime/peer-discovery-current.json`, and applies the resulting
`BOOTSTRAP_PEER_ADDRESSES` to the active single node. TCP-open status is only a
bootstrap hint; install completion and mining readiness still require normal
peer handshakes, sync freshness, RPC health, and template checks.

## IPFS Recovery Path

The stack does not start the node through a legacy bulk bootstrap mode.
The node starts from its configured datadir, uses normal P2P bootstrap peers,
and the pool readiness gates decide whether mining can run.

Recovery content is owned by the low-priority raw-datadir sidecar and the IPFS
content/segment writers. The sidecar keeps a conservative local copy, seals
immutable signed generations, and the IPFS workers publish only safe content
after mining and host pressure gates pass. Receivers must verify signed
manifests, segment hashes, network identity, order continuity, tip/state roots,
and normal consensus before restoring data.

## IPFS Content Discovery

Future systems should read `ops/ipfs-content-discovery.json` for the durable
IPFS/IPNS discovery contract. The stable latest pointer is
`/ipns/k51qzi5uqu5djjlh4vxtmzyswx0qk4s3wdlf3yrpkszp38gq5sl71zcgmmc3jk`; the current
immutable latest-index CID is
recorded in that discovery file. At the first live segment publish on
2026-05-31 it was `bafkreifvqj7qhkoifykybbvlxxmq3jhydgzq2kjxuq5fznjizjjogzgthi`.
The current implementation writes append-only live-tail chain-order segments
from the local node. The durable protocol design is recorded in
`docs/ipfs-append-only-segment-protocol.html`. IPFS and IPNS are
not chain trust. Receivers must verify segment CIDs, payload hashes, order
continuity, network/genesis identity, tip/state roots, and normal consensus
before using the data.

## Runtime Stability Defaults

No-miner deployments are sync-only by default: `BDAG_ENABLE_NODE_MINING=0`,
`BDAG_NODE_MODULES=Blockdag`, and an empty `BDAG_NODE_MINING_ARGS`. Enable node
mining/template flags only when real miners are attached. Do not add unsynced
mining bypass flags; readiness gates must fail closed until node sync and P2P
freshness are healthy. The compose `dashboard` service is the only operator UI
and control-plane surface. Host-side watchdog, stack sentinel, P2P guard, peer
refresh, chain restore guard, and IPFS/raw-datadir sidecar timers are support
services installed by `ops/install-p2p-services.sh` unless explicitly disabled. Runtime tooling
uses the current stack service names: `node`, `pool`, and `postgres`. Concrete
Compose container names may include project and ordinal suffixes.

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
payload exposes the active dashboard URL, RPC latency, and Linux IO pressure
metrics. When PSI is unavailable, the dashboard falls back to `/proc/stat`
`iowait` deltas and raises a maintenance warning after sustained high IO wait.
The ops layer also detects a host profile with `BDAG_HOST_PROFILE=auto` and
uses adaptive worker budgets for expensive dashboard/global/miner scans. The
same release source is expected to behave conservatively on constrained ARM64
hosts, while AMD64 and larger ARM64 hosts can use more parallelism when pressure
is low. See `docs/platform-adaptive-runtime.md`.

The dashboard, watchdog, sync coordinator, P2P guard, and startup checks also
share one cross-process status sample. `ops/status_sampler.py` writes
`ops/runtime/status-sampler.json` atomically, and routine callers read it
through `collect_status_cached()` when it is fresh. The default sampler reuse
window is bounded at 120 seconds so constrained hosts do not repeatedly probe
Docker, node RPC, pool metrics, and miner state while the node is catching up.
Direct repair diagnostics can still force a live collection with
`max_age_seconds=0`.

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
`TEMP` to avoid accidental temp spillover into overlay layers. Large IPFS
restore-point staging stays on capacity storage unless deliberately overridden.
The installer reports
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
checker reads ELF/Mach-O/PE headers directly so it can be used from Linux,
macOS, and Windows build hosts.

The compose `dashboard` service is the canonical operator interface and source
of truth, exposed on `DASHBOARD_HOST_BIND:DASHBOARD_HOST_PORT`, default
`127.0.0.1:8088`. Its tabs separate pool status, miners, earnings, and global
chain production, and each global production view must be sourced from native
BlockDAG chain RPC `getBlockCount`/ordered block/coinbase calls. EVM RPC belongs
to wallet balance views only. The compose dashboard owns status collection
through its bounded shared cache.

Source checkout tests require Python's standard library test runner plus
`pytest`. On Ubuntu/Debian hosts, install the test dependency with:

```bash
sudo apt-get update
sudo apt-get install -y python3-pytest
```

Agents should verify it with `python3 -m pytest --version` before running
`ops/tests` through pytest-backed deployment checks.

The dashboard runtime collectors use Python's standard HTTP client for local
pool metrics and public enrichment calls. Do not make live status depend on
host utilities such as `curl`; release packages should behave the same on Linux
AMD64, Linux ARM64, macOS Docker Desktop, and Windows Docker Desktop once Docker
and Python are available.

For live compose-dashboard/watchdog-only updates, use:

```bash
ops/deploy-live-runtime-update.sh --target /path/to/installed/runtime --mark-runtime-compose
```

The deploy helper copies only a small whitelist, backs up changed files, refuses
dev compose files, validates source and target, restarts the configured compose
dashboard service plus any requested user support services, and rolls back copied
files if validation or restart fails.
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

To include optional services controlled by `.env`, set `COMPOSE_PROFILES` before
`docker compose up`. Example: `COMPOSE_PROFILES=miner` enables the CPU miner
service; leave `COMPOSE_PROFILES` empty to disable it.

Once everything is running:

- Operations dashboard, authoritative compose service: `http://localhost:8088`
- Mining pool Stratum endpoint: `stratum+tcp://localhost:3334`
- RPC endpoint: `http://localhost:38131`

For ASIC deployments, the installer records the host-facing pool address and
ASIC LAN scope in `.env` as `BDAG_POOL_HOST`, `BDAG_POOL_URL`,
`BDAG_MINER_SCAN_TARGET`, and `BDAG_ASIC_LAN_CIDRS`. The dashboard and repair
tools use those values instead of guessing from inside Docker. Docker bridge
networks default to `172.16.0.0/12` and are filtered from ASIC discovery and
displayed Stratum endpoints; seeing `172.*` as a miner IP or pool endpoint is a
configuration failure, not a valid physical miner.

## Default IPFS Recovery Source

New installs do not use node startup artifact sync. They start from the
configured datadir, normal P2P peers, and the stack-owned IPFS/raw-datadir
recovery sidecar. IPFS content and segment workers are the only stack recovery
publication path.

Eligible source hosts maintain a low-priority raw datadir sidecar and publish
signed IPFS restore content from a finalized sidecar generation after mining,
chain, and host-pressure gates pass. The publisher does not stop the live node
automatically. Set `BDAG_RAWDATADIR_FINALIZE=1` only for an operator-approved
finalization window.

All restore consumers must validate signed manifests, content CIDs, network and
genesis identity, order continuity, tip/state roots, and normal consensus before
using restored data.

Check source eligibility and status with:

```bash
./ops/rawdatadir_source_eligibility.py --full --json
```

Refresh/publish the raw datadir source path with:

```bash
./ops/publish-rawdatadir-artifact.sh
```

See `docs/rawdatadir-libp2p-sync.md` and
`docs/ipfs-append-only-segment-protocol.html`.

IPFS segments and finalized raw-datadir sidecars are the supported
content-publication paths. Published files must be manifest-indexed and
consensus-validated before use.

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
