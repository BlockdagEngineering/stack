# pool-stack-docker-stack

Docker Compose port of [`pool-stack`](https://github.com/BlockdagEngineering/pool-stack).
Runs the same components — `blockdag-node` + `nodeworker`, `mining-pool`,
`bdag-netdata-exporter`, Postgres, and Netdata — as containers instead of
systemd services.

## Components

| Service    | Image / build                              | Purpose                              |
| ---------- | ------------------------------------------ | ------------------------------------ |
| `node`     | local build (`dockerfile-dev`/`-release`)  | BlockDAG node, supervised by nodeworker |
| `pool`     | local build                                | Mining pool (Stratum :3334, API :8080)  |
| `exporter` | local build                                | Prometheus text on :9198 for Netdata  |
| `postgres` | `postgres:15`                              | Pool persistence, schema auto-loaded |
| `netdata`  | `netdata/netdata:stable`                   | Dashboard at http://localhost:19999  |
| `miner`    | local build (profile: `miner`)             | Optional cpu-miner against the pool  |

## Release tarballs (`pool-v*` vs `cpu-v*`)

GitHub Releases attach `pool-stack-docker-<tag>.tar.gz` containing compose files, Dockerfiles, `config/*.example`, `sql/`, `docker/`, and docs. **Chain snapshots are not included:** GitHub caps each release asset at **2 GiB**, so multi‑gigabyte `.bdsnap` files cannot ship in the tarball. Use **Git LFS** in a git checkout, copy a snapshot locally, or sync from the network — see `SNAPSHOT-README.md`.

* **`pool-v*`** builds use `dockerfile-pool-release` (node + pool + exporter; no CPU miner image). Do **not** enable the Compose `miner` profile — there is no `miner` build target in that Dockerfile.
* **`cpu-v*`** builds use `dockerfile-cpu-release` (adds the CPU miner binary and image). Use **`cp .env.cpu.example .env`** (or set `COMPOSE_PROFILES=miner`) so `docker compose up` includes the miner service; CI validates `docker compose --profile miner config` for that release.

## Two build modes (controlled by a single `.env` flag)

Set `DEV` in `.env`:

* `DEV=true` → `dockerfile-dev`
  Builds from local clones in the parent directory:
  ```
  parent/
  ├── pool-stack-docker-stack/   (this repo)
  ├── blockdag-corechain/        (private repo, cloned locally)
  ├── asic-pool/                 (private repo, cloned locally)
  ├── cpu-miner/                 (private repo, cloned locally)
  └── pool-stack/                (this repo provides scripts/exporter)
  ```
* `DEV=false` → `dockerfile-release`
  Mirrors `pool-stack/.github/workflows/build.yml` exactly: clones
  `BlockdagEngineering/blockdag-corechain` and
  `BlockdagEngineering/asic-pool`, runs `make all` then `go build -o
  build/bin/pool ./cmd/pool`, generates a `checksums.txt`. Set
  `GITHUB_TOKEN` in `.env` to a PAT with `Contents: Read` on those repos.

The `Makefile` in this directory translates `DEV=true|false` into the right
`docker compose` `--build-arg` / context combination, so the same commands
work in either mode.

## Configuration (what loads where)

Docker Compose reads **`.env`** in this directory for variable substitution (ports, `POSTGRES_PASSWORD`, `NODE_RPC_USER`, etc.). Services pick up additional files as follows:

| Piece | Purpose |
| ----- | ------- |
| **`config/node.conf`** | Mounted into the **`node`** container as `/etc/bdagStack/node.conf` (RPC peers, `miningaddr`, modules). **Copy from `node.conf.example`** — `config/node.conf` is gitignored so local overrides are not committed. |
| **`config/pool.env`** | Loaded into the **`pool`** container via Compose `env_file` (Stratum bind, PPLNS, fees, maturity, node RPC password). **`NODE_RPC_URL`** and **`PG_URL`** are overridden in `docker-compose.yml` to use service names (`http://node:38131`, `postgres:5432`). Rebuild the **`pool`** image after changing `asic-pool` so the binary matches. |
| **`config/miner.env`** | Optional; used only with **`--profile miner`** (`required: false`). Variables like **`MINER_*` in root `.env`** are **not** passed to the miner unless you duplicate them here or add an `environment:` block. |

The **`pool`** image also embeds a copy of **`.env`** at build time for `godotenv` defaults; runtime env from Compose wins for overlapping keys.

## Snapshot import (optional)

To bake a local `.bdsnap` into the `node` image at build time, set
`SNAPSHOT_PATH` in `.env` to a path **relative to the compose build context**
(the parent directory when `BUILD_CONTEXT` is `..`). Example:
`blockdag-corechain/snapshot/snapshot.bdsnap`.

To **build without importing** any snapshot, leave `SNAPSHOT_PATH` unset or
empty in `.env`. Compose defaults it to `pool-stack-docker/docker/no-snapshot.marker`
(a tiny file). The Dockerfile only runs `snap import` when that file is **≥ 1KB**,
so the node syncs from the network instead.

## Quick start

```bash
git clone <this repo> pool-stack-docker
cd pool-stack-docker

# 1. Configure
cp .env.example .env
cp config/pool.env.example config/pool.env
cp config/node.conf.example config/node.conf
$EDITOR .env                    # DEV, POSTGRES_PASSWORD, NODE_RPC_*, optionally SNAPSHOT_PATH
$EDITOR config/node.conf        # rpcuser/rpcpass (must match .env), peers, miningaddr
$EDITOR config/pool.env         # pool fees, PPLNS, maturity (NODE_RPC_URL/PG_URL set by compose)

# 2. (DEV=true only) Make sure local clones are next to this repo
ls ../blockdag-corechain ../asic-pool ../cpu-miner ../pool-stack >/dev/null

# Optional: CPU miner settings (when using `--profile miner`)
cp config/miner.env.example config/miner.env
$EDITOR config/miner.env

# 3. Build & start
make build
make up

# 4. Tail logs
make logs
```

Once everything is running:

* Mining pool Stratum endpoint: `stratum+tcp://localhost:3334`
* Pool stats API: <http://localhost:8080/stats>
* Exporter metrics: <http://localhost:9198/metrics>
* Node-native metrics: <http://localhost:6060/metrics>
* Netdata dashboard: <http://localhost:19999/bdagstack.html>
  (run `bash scripts/setup-netdata.sh` after the first `make up` to install
   the bdagstack scrape jobs and dashboard page).

## Common operations

```bash
# Apply / re-apply pool schema (idempotent)
bash scripts/init-pool-postgres.sh

# Wire Netdata to the bdag scrape targets + install bdagstack.html
bash scripts/setup-netdata.sh

# Run a CPU miner against the pool (addresses in config/miner.env)
make miner-up

# Show the resolved compose config for the current DEV value
make config

# Stop everything (keeps volumes)
make down

# Stop + delete named volumes (DESTRUCTIVE - drops the pool DB and node data)
make clean
```

## Files

```
pool-stack-docker-stack/
├── .env.example
├── .env.cpu.example          # cpu-v* tarballs: release env + COMPOSE_PROFILES=miner
├── docker-compose.yml
├── dockerfile-dev            # DEV=true: builds from ../blockdag-corechain etc.
├── dockerfile-release        # DEV=false: mirrors pool-stack build.yml
├── Makefile                  # Toggles DEV -> DOCKERFILE/CONTEXT for compose
├── config/
│   ├── bdag-exporter.env.example   # reference only (exporter env is inline in compose)
│   ├── miner.env.example           # copy to miner.env for profile `miner`
│   ├── netdata-bdagstack.html
│   ├── node.conf.example           # copy to node.conf (gitignored; mounted into node)
│   └── pool.env.example            # copy to pool.env (gitignored; pool env_file)
├── scripts/
│   ├── init-pool-postgres.sh
│   └── setup-netdata.sh
└── sql/
    └── pool-schema.sql
```

## Differences vs. pool-stack (systemd)

| pool-stack (systemd)                          | pool-stack-docker-stack (compose)               |
| --------------------------------------------- | ----------------------------------------------- |
| `install.sh` provisions packages + units      | `make build && make up`                         |
| Postgres installed via apt                    | `postgres:15` container, schema auto-loaded     |
| Netdata installed via kickstart               | `netdata/netdata:stable` container              |
| Configs in `/etc/bdagStack/`                  | `config/node.conf` mounted as node config; `config/pool.env` for pool |
| Logs in journald                              | `docker compose logs <service>`                 |
| Snapshot import via manual `bdag snapshot ...`| Build-time, optional `SNAPSHOT_PATH` (.bdsnap)  |
