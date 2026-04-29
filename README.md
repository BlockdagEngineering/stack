# pool-stack-docker-stack

Docker Compose port of [`pool-stack`](https://github.com/BlockdagEngineering/pool-stack).
Runs the same components — `blockdag-node` + `nodeworker`, `mining-pool`,
`bdag-netdata-exporter`, Postgres, and Netdata — as containers instead of
systemd services.

## Components

| Service    | Image / build                              | Purpose                              |
| ---------- | ------------------------------------------ | ------------------------------------ |
| `node`     | local build (`dockerfile-dev` or `dockerfile-*-release`)  | BlockDAG node, supervised by nodeworker |
| `pool`     | local build                                | Mining pool (Stratum :3334, API :8080)  |
| `exporter` | local build                                | Prometheus text on :9198 for Netdata  |
| `postgres` | `postgres:15`                              | Pool persistence, schema auto-loaded |
| `netdata`  | `netdata/netdata:stable`                   | Dashboard at http://localhost:19999  |
| `miner`    | local build (profile `miner`; **default ON** in `.env.cpu.example`) | CPU stratum miner against the pool  |

## Release tarballs (`pool-v*` vs `cpu-v*`)

GitHub Releases attach `pool-stack-docker-<tag>.tar.gz` with `bin/` (pre-built Linux binaries), `docker-compose.yml`, `dockerfile-*-release`, `.env.cpu.example` / `.env.pool.example`, `netdata/`, `scripts/bdag_netdata_exporter.py`, `docker/`, etc. **Images `COPY` from `./bin` only** — no git clone inside Docker. **Chain snapshots are not included** (2 GiB cap) — see `SNAPSHOT-README.md`.

* **`pool-v*`** — `dockerfile-pool-release` (node + pool + exporter; no miner image). Use **`cp .env.pool.example .env`** and do **not** enable the miner profile.
* **`cpu-v*`** — `dockerfile-cpu-release` (adds `bin/cpu-miner`). Use **`cp .env.cpu.example .env`** (includes `COMPOSE_PROFILES=miner`).

After unpacking, run from the extracted directory with `BUILD_CONTEXT=.` (already set in those examples).

## Build modes (`dockerfile-dev` vs release Dockerfiles)

* **`dockerfile-dev`** (local git workspace): build context **`..`**, compile from sibling repos `../blockdag-corechain`, `../asic-pool`, `../cpu-miner`; exporter script from this repo at `scripts/bdag_netdata_exporter.py`. Set `DOCKERFILE=pool-stack-docker/dockerfile-dev`, `BUILD_CONTEXT=..`.

* **`dockerfile-pool-release` / `dockerfile-cpu-release`** (release tarball): build context **`.`**, copy **`./bin`** and `scripts/bdag_netdata_exporter.py`; no upstream fetch in the image build. **`GITHUB_TOKEN` is not used** for `docker compose build` offline (CI uses it only to compile `bin/` before attaching the tarball).

## Configuration (what loads where)

Docker Compose reads **`.env`** in this directory for variable substitution and passes pool / miner settings into containers.

| Piece | Purpose |
| ----- | ------- |
| **`node.conf`** | **Project root.** Mounted into the **`node`** container as `/etc/bdagStack/node.conf` (peers, `miningaddr`, RPC modules). **Copy from `node.conf.example`** — `node.conf` is gitignored. **`rpcuser` / `rpcpass` here must match `NODE_RPC_USER` / `NODE_RPC_PASS` in `.env`.** |
| **`.env`** | Start from **`.env.cpu.example`** (miner + cpu release) or **`.env.pool.example`** (pool-only, no miner). **Pool:** vars as in **`asic-pool/cmd/pool/main.go`**. **`NODE_RPC_URL`** / **`PG_URL`** are set in `docker-compose.yml`. **Miner:** `MINER_POOL_URL`, `MINER_POOL_USER`, `MINER_POOL_PASS`, `MINER_WORKERS`. |
| **`netdata/`** | Netdata bind-mounts: `netdata.conf`, `go.d` plugin configs, `bdagstack.html`. |

The **`pool`** image built with **`dockerfile-dev`** still **`COPY`s** **`.env`** at build time into the image for `godotenv` defaults; release Dockerfiles do not embed `.env`.

## Snapshot import (optional)

To bake a local `.bdsnap` into the `node` image at build time, set
`SNAPSHOT_PATH` in `.env` to a path **relative to the compose build context**
(the parent directory when `BUILD_CONTEXT` is `..`). Example:
`blockdag-corechain/snapshot/snapshot.bdsnap`.

For an **unpacked tarball** (`BUILD_CONTEXT=.`), keep `SNAPSHOT_PATH=docker/no-snapshot.marker` in `.env` (see `.env.cpu.example`) or point at a local `.bdsnap` beside `bin/`.

To **build without importing** any snapshot while using **Git dev** (`BUILD_CONTEXT=..`), leave `SNAPSHOT_PATH` unset: Compose defaults to `pool-stack-docker/docker/no-snapshot.marker`.

## Quick start

```bash
git clone <this repo> pool-stack-docker
cd pool-stack-docker

# 1. Configure — pick one template as .env
cp .env.cpu.example .env        # full stack + miner (default COMPOSE_PROFILES=miner)
# or:  cp .env.pool.example .env   # pool-only; no miner image / profile
cp node.conf.example node.conf
$EDITOR .env                    # For git dev: set BUILD_CONTEXT=.. and DOCKERFILE per file header
$EDITOR node.conf              # rpcuser/rpcpass (must match .env), peers, miningaddr

# 2. (Git dev) Ensure local clones exist next to this repo
ls ../blockdag-corechain ../asic-pool ../cpu-miner >/dev/null

# `.env.cpu.example` sets COMPOSE_PROFILES=miner. Use `.env.pool.example` for pool-only (empty profile).

# 3. Build & start
docker compose build
docker compose up -d

# 4. Logs
docker compose logs -f node
```

Once everything is running:

* Mining pool Stratum endpoint: `stratum+tcp://localhost:3334`
* Pool stats API: <http://localhost:8080/stats>
* Exporter metrics: <http://localhost:9198/metrics>
* Node-native metrics: <http://localhost:6060/metrics>
* Netdata dashboard: <http://localhost:19999/bdagstack.html>
  (run `bash scripts/setup-netdata.sh` after the first `docker compose up` to install
   the bdagstack scrape jobs and dashboard page).

## Common operations

```bash
# Apply / re-apply pool schema (idempotent)
bash scripts/init-pool-postgres.sh

# Wire Netdata to the bdag scrape targets + install bdagstack.html
bash scripts/setup-netdata.sh

# Start / restart only the CPU miner (if COMPOSE_PROFILES=miner is not set)
docker compose --profile miner up -d miner

# Show the resolved compose config
docker compose config

# Stop everything (keeps volumes)
docker compose down

# Stop + delete named volumes (DESTRUCTIVE)
docker compose down -v
```

## Files

```
pool-stack-docker/
├── .env.cpu.example          # template: cpu stack + miner (tarball or git dev)
├── .env.pool.example         # template: pool-only (no miner)
├── node.conf.example         # copy → node.conf (gitignored) for the BlockDAG node
├── bdag-exporter.env.example # reference when running exporter outside compose
├── docker-compose.yml
├── dockerfile-dev             # Git dev — BUILD_CONTEXT=..
├── dockerfile-cpu-release     # Release — BUILD_CONTEXT=. , copies ./bin (+ cpu-miner)
├── dockerfile-pool-release    # Release — BUILD_CONTEXT=. , copies ./bin (no miner image)
├── netdata/
│   ├── bdagstack.html
│   ├── go.d.conf
│   ├── netdata.conf
│   └── go.d/
│       └── prometheus.conf
├── scripts/
│   ├── bdag_netdata_exporter.py   # Netdata metrics (vendored from pool-stack; used in images)
│   ├── init-pool-postgres.sh
│   └── setup-netdata.sh
└── sql/
    └── pool-schema.sql
```

## Differences vs. pool-stack (systemd)

| pool-stack (systemd)                          | pool-stack-docker-stack (compose)               |
| --------------------------------------------- | ----------------------------------------------- |
| `install.sh` provisions packages + units      | `docker compose build && docker compose up -d`                         |
| Postgres installed via apt                    | `postgres:15` container, schema auto-loaded     |
| Netdata installed via kickstart               | `netdata/netdata:stable` container              |
| Configs in `/etc/bdagStack/`                  | Root **`node.conf`** + **`.env`** (pool, miner vars); **`.env.cpu.example`** sets **`COMPOSE_PROFILES=miner`** when you want the miner service |
| Logs in journald                              | `docker compose logs <service>`                 |
| Snapshot import via manual `bdag snapshot ...`| Build-time, optional `SNAPSHOT_PATH` (.bdsnap)  |
