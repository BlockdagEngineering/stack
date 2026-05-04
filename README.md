# pool-stack-docker-stack

This stack can be run in any environment where docker is installed. It includes an upgradable BDAG node, a mining pool with its db, and a minimal dashboard that  provides essential realtime monitoring.


| Service     | Image / build                           | Purpose |
| ----------- | --------------------------------------- | ------- |
| `node`      | BlockDAG node, supervised by nodeworker |         |
| `pool`      | Mining pool (Stratum :3334, API :8080)  |         |
| `postgres`  | Pool persistence, schema auto-loaded    |         |
| `dashboard` | Essential monitoring                    |         |


## Release tarballs (`pool-v*` vs `cpu-v*`)

GitHub Releases attach `pool-stack-docker-<tag>.tar.gz` with `bin/` (pre-built Linux binaries), `docker-compose.yml`, `dockerfile`,  `.env.example`,  `docker/`, /bin etc. **Images** `COPY` **from** `./bin` **only** — no git clone inside Docker. 



After unpacking, run from the extracted directory with `BUILD_CONTEXT=.` (already set in those examples).

## Configuration (what loads where)

Docker Compose reads `**.env`** in this directory for variable substitution and passes pool / miner settings into containers.


| Piece           | Purpose                                                                                                                                                                                                                                                                                                                     |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `**node.conf**` | **Project root.** Mounted into the `**node`** container as `/etc/bdagStack/node.conf` (peers, `miningaddr`, RPC modules). **Copy from `node.conf.example`** — `node.conf` is gitignored. `**rpcuser` / `rpcpass` here must match `NODE_RPC_USER` / `NODE_RPC_PASS` in `.env`.**                                             |
| `**.env`**      | Start from `**.env.cpu.example`** (miner + cpu release) or `**.env.pool.example**` (pool-only, no miner). **Pool:** vars as in `**asic-pool/cmd/pool/main.go`**. `**NODE_RPC_URL`** / `**PG_URL**` are set in `docker-compose.yml`. **Miner:** `MINER_POOL_URL`, `MINING_POOL_ADDRESS`, `MINER_POOL_PASS`, `MINER_WORKERS`. |


The `**pool`** image built with `**dockerfile-dev**` still `**COPY`s** `**.env`** at build time into the image for `godotenv` defaults; release Dockerfiles do not embed `.env`.

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

- Mining pool Stratum endpoint: `stratum+tcp://localhost:3334`
- Pool stats API: [http://localhost:8080/stats](http://localhost:8080/stats)
- Node-native metrics: [http://localhost:6060/metrics](http://localhost:6060/metrics)
- Netdata dashboard: [http://localhost:19999/bdagstack.html](http://localhost:19999/bdagstack.html)
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

