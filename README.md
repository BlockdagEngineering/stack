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

GitHub Releases ship `pool-stack-docker-<tag>.tar.gz` from this repo:

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

## Snapshot import (optional)

To bake a local `.bdsnap` into the `node` image at build time, set
`SNAPSHOT_PATH` in `.env` to a path **relative to the compose build context**
(`..` when `DEV=true`, this repo when `DEV=false`). Example for dev:
`blockdag-corechain/snapshot/snapshot.bdsnap`. If `SNAPSHOT_PATH` is empty,
`make build` supplies a tiny placeholder file so the image still builds but no
import runs (the node syncs from the network).

## Quick start

```bash
git clone <this repo> pool-stack-docker-stack
cd pool-stack-docker-stack

# 1. Configure
cp .env.example .env
cp config/pool.env.example  config/pool.env
$EDITOR .env                    # set DEV, POSTGRES_PASSWORD, optionally SNAPSHOT_PATH
# Optional: edit config/node.conf.example (generic defaults; mounted read-only as the node config)

# 2. (DEV=true only) Make sure local clones are next to this repo
ls ../blockdag-corechain ../asic-pool ../cpu-miner ../pool-stack >/dev/null

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

# Run a CPU miner against the pool
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
│   ├── bdag-exporter.env.example
│   ├── netdata-bdagstack.html
│   ├── node.conf.example
│   └── pool.env.example
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
| Configs in `/etc/bdagStack/`                  | `config/*.env` and `config/node.conf.example` mounted as node config |
| Logs in journald                              | `docker compose logs <service>`                 |
| Snapshot import via manual `bdag snapshot ...`| Build-time, optional `SNAPSHOT_PATH` (.bdsnap)  |
