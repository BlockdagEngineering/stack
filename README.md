# pool-stack-docker-stack

This stack can be run in any environment where docker is installed. It includes an upgradable BDAG node, a mining pool with its db, and a minimal dashboard that  provides essential realtime monitoring.


| Service     | Image / build                           | Purpose |
| ----------- | --------------------------------------- | ------- |
| `node`      | BlockDAG node, supervised by nodeworker |         |
| `pool`      | Mining pool (Stratum :3334)             |         |
| `postgres`  | Pool persistence, schema auto-loaded    |         |
| `dashboard` | Essential monitoring                    |         |


## Release tarball

GitHub Releases attach `pool-stack-docker-<tag>.tar.gz` with `bin/` (pre-built `**blockdag-node**`, `**nodeworker**`, `**mining-pool**`), `dashboard/` (Compose builds `dashboard`), `docker-compose.yml`, `dockerfile`, `.env.example`, `docker/`, etc. **Release images** stage binaries from `./bin`; no git clone inside Docker. 

After unpacking, run from the extracted directory with `BUILD_CONTEXT=.` (already set in those examples).

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

## Quick start

```bash
# 1. Put the tarball and the snapshot in a folder together

# 2. Uncompress the tarball:
tar -xzf pool-stack-docker-v1.3.22.tar.gz

# 3. Move the latest.bdag file into the root of the tarball folder

# 4. Set up the configs: 
cp .env.example .env        # set postgres passowrd
cp node.conf.example node.conf # node specific 

# 5. Set the miningaddr in node.conf: this will be the earning address

# 6. Build & start
docker compose build
docker compose up -d

# 7. Install P2P reachability, local peer discovery, host tuning, and
#    default FastSnap seed refresh for serving verified snapshots to peers:
./ops/install-p2p-services.sh

# 8. Verify release/install readiness before marking the stack healthy:
./scripts/release-readiness-check.py

# 9 logs:
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
default release behaviour: `./ops/install-p2p-services.sh` installs
`bdag-fastsnap-seed.timer`, which refreshes the public seed every two hours at
low CPU and I/O priority.

Run an immediate refresh manually with:

```bash
./ops/build-fastsnap-seed.sh
```

The script requires a pool binary with
`/admin/rpc-backend-maintenance`, `POOL_RUNTIME_ADMIN_ENABLED=true`, and
`POOL_RPC_ROUTER_ENABLED=true`. It drains the export backend, proves the pool is
still selected on the other backend, stops only the drained node, exports
`snapshot.bdsnap`, restores the node before heavy verification, then verifies and
installs the archive and manifest into both node datadirs. The installed files
are hardlinks to a single archive under `data-restore/fastsnap`, so the host does
**not** duplicate the node databases or keep separate per-node snapshot copies.

The export path now refuses to publish a stale public seed by default unless the
standby/export backend is within `BDAG_FASTSNAP_MAX_EXPORT_BACKEND_LAG` main-order
units of the selected backend. The default is `1000`. Keep this gate enabled for
public seeds.

To disable the automatic public seed refresh on a constrained host, set
`BDAG_FASTSNAP_SEED_TIMER_ENABLED=0` before running
`./ops/install-p2p-services.sh`.

The installer writes host-specific paths to
`~/.config/bdag-fastsnap-seed.env`. Advanced operators can edit that file:

```bash
systemctl --user status bdag-fastsnap-seed.timer
systemctl --user start bdag-fastsnap-seed.service
```

See `docs/fastsnap-maintenance-handoff.html`.

## Release readiness

Container health alone does not prove that a deployment can mine. Before
marking an install healthy, run:

```bash
./scripts/release-readiness-check.py
```

The checker is read-only. It verifies the pool Postgres schema, node
mineable/synced state, sane external peers after self/invalid-address/loopback
filtering, and a functional `getBlockTemplate` response. It also repeats the mining RPC,
peer, and template gates across a short default stability window so startup or
backend flapping is not marked healthy from a single lucky sample. See
`docs/release-readiness-gates.html` for gate details and CI/installer options.

## Release provenance

Before publishing a release or handing it to another operator, write a
provenance manifest:

```bash
./scripts/release-provenance-manifest.py \
  --image bdag-release/node:local \
  --image bdag-release/asic-pool:local \
  --snapshot snapshots/latest.bdsnap
```

The script writes `release-provenance.json` and `release-provenance.html` with
the source commit, dirty status, schema hash, redacted feature flags, optional
Docker image IDs, and snapshot checksums. Do not publish a package whose
manifest shows unexpected dirty source, missing schema hash, or a missing
snapshot when the release advertises fast-sync data.

## P2P reachability and local peer discovery

Release packages should install the persistent P2P firewall helper in `ops/`
so BlockDAG P2P ports are accepted on all configured host interfaces, including
LAN, ZeroTier, WireGuard, Tailscale, and other VPN interfaces. The helper is
intentionally interface-agnostic; Docker published ports still decide which node
ports are reachable.

For dual-node pool packages, `ops/update-local-peers.py` keeps node1 and node2
in each other's startup peer lists using Docker DNS plus every routable
non-Docker host IPv4 address. Optional `LAN_PEER_ADDRESSES`,
`VPN_PEER_ADDRESSES`, `ZEROTIER_PEER_ADDRESSES`, and `EXTRA_PEER_ADDRESSES`
allow operators to pin known LAN/VPN peers. See
`docs/p2p-interface-discovery-standard.html`.
  

# Common operations

## Show the resolved compose config

docker compose config

## Stop everything (keeps volumes)

docker compose down

## Stop + delete named volumes (DESTRUCTIVE)

docker compose down -v

```

```
