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

The installer detects the host OS and CPU architecture, writes `.env` and `node.conf`, downloads `latest.bdsnap` when needed, and runs `docker compose build && docker compose up -d`. The release currently runs the service images as `linux/amd64`; ARM hosts need Docker Desktop or Docker Engine with amd64 emulation enabled.

## Configuration (what loads where)

Docker Compose reads `**.env`** in this directory for variable substitution and passes pool / miner settings into containers.


| Piece           | Purpose                                                                                                                                                                                                                                                                         |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `**node.conf`** | **Project root.** Mounted into the `**node`** container as `/etc/bdagStack/node.conf` (peers, `miningaddr`, RPC modules). **Copy from `node.conf.example**` — `node.conf` is gitignored. `**rpcuser` / `rpcpass` here must match `NODE_RPC_USER` / `NODE_RPC_PASS` in `.env`.** |
| `**.env`**      | Start from `**.env.example`**. `******NODE_RPC_URL` / `**PG_URL**` are set in `docker-compose.yml`. **Miner:** `MINER_POOL_URL`, `MINING_POOL_ADDRESS`, `MINER_POOL_PASS`, `MINER_WORKERS`.                                                                                     |


The `**pool`** image bakes `**.env.example`** into the image at `/var/lib/bdagStack/pool/.env` for `godotenv` (release `**dockerfile`** uses `**COPY .env.example**` relative to tarball root; git dev `**dockerfile-dev**` uses `**COPY pool-stack-docker/.env.example**`). Compose still sets most variables via `environment:`.

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
