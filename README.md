# pool-stack-docker-stack

This stack can be run in any environment where docker is installed. It includes an upgradable BDAG node, a mining pool with its db, and a minimal dashboard that  provides essential realtime monitoring.


| Service     | Image / build                           | Purpose |
| ----------- | --------------------------------------- | ------- |
| `node`      | BlockDAG node, supervised by nodeworker |         |
| `pool`      | Mining pool (Stratum :3334, API :8080)  |         |
| `postgres`  | Pool persistence, schema auto-loaded    |         |
| `dashboard` | Essential monitoring                    |         |


## Release tarballs (`pool-v*` vs `cpu-v*`)

GitHub Releases attach `pool-stack-docker-<tag>.tar.gz` with `bin/` (pre-built `**blockdag-node**`, `**nodeworker**`, `**mining-pool**`, `**dashboard-api**`), `dashboard/` (Compose builds `dashboard`), `docker-compose.yml`, `dockerfile`, `.env.example`, `docker/`, etc. **Release images** stage binaries from `./bin`; no git clone inside Docker. 

After unpacking, run from the extracted directory with `BUILD_CONTEXT=.` (already set in those examples).

## Configuration (what loads where)

Docker Compose reads `**.env`** in this directory for variable substitution and passes pool / miner settings into containers.


| Piece           | Purpose                                                                                                                                                                                                                                                                                                                     |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `**node.conf`** | **Project root.** Mounted into the `**node`** container as `/etc/bdagStack/node.conf` (peers, `miningaddr`, RPC modules). **Copy from `node.conf.example`** — `node.conf` is gitignored. `**rpcuser` / `rpcpass` here must match `NODE_RPC_USER` / `NODE_RPC_PASS` in `.env`.**                                             |
| `**.env`**      | Start from `**.env.cpu.example`** (miner + cpu release) or `**.env.pool.example`** (pool-only, no miner). **Pool:** vars as in `**asic-pool/cmd/pool/main.go`**. `**NODE_RPC_URL`** / `**PG_URL**` are set in `docker-compose.yml`. **Miner:** `MINER_POOL_URL`, `MINING_POOL_ADDRESS`, `MINER_POOL_PASS`, `MINER_WORKERS`. |


The `**pool`** image bakes `**.env.example**` into the image at `/var/lib/bdagStack/pool/.env` for `godotenv` (release `**dockerfile**` uses `**COPY .env.example**` relative to tarball root; git dev `**dockerfile-dev**` uses `**COPY pool-stack-docker/.env.example**`). Compose still sets most variables via `environment:`.



## Quick start

```bash
# 1. Put the tarball and the snapshot in a folder together

# 2. Uncompress the tarball:
tar -xzf pool-stack-docker-v1.3.20.tar.gz

# 3. Move the snapshot into the root of the tarball folder

# 4. Set up the configs: 
cp .env.example .env        
cp node.conf.example node.conf # node specific 

# 5. Set the miningaddr in node.conf: this will be the earning address

# 6. Build & start
docker compose build
docker compose up -d

# 5 logs:
docker compose logs -f node
docker compose logs -f pool
```

Once everything is running:

- Dashboard: `http://localhost:9280` ( Run in browser, or use the VSC/Cursor Simple Browser! )
- Mining pool Stratum endpoint: `stratum+tcp://localhost:3334`


## Common operations


# Show the resolved compose config
docker compose config

# Stop everything (keeps volumes)
docker compose down

# Stop + delete named volumes (DESTRUCTIVE)
docker compose down -v
```

