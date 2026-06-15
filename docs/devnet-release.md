# Devnet Release

This branch provides a local BlockDAG devnet for dapp developers. It starts one
devnet node, Postgres, the mining pool, collector API, dashboard, and a CPU
miner that connects to the pool over Stratum.

The devnet overlay disables the pool's production node-health router gate
because a single local devnet node does not have consensus peers. Production
compose files keep the health gate enabled.

## GitOps Branches

Use the `devnet` branch in each source repository:

- `blockdag-corechain`
- `pool`
- `stack`
- `collector`
- `dashboard2`

Keep release changes reviewable per repository. Do not edit generated runtime
state, checked-out chain data, or local `.env` files. Put developer defaults in
tracked examples and keep secrets in untracked env files.

## Shareable Release

The `Build devnet release` workflow packages a self-contained devnet runtime
from the `devnet` branches of:

- `blockdag-corechain`
- `pool`
- `stack`
- `collector`
- `dashboard2`

Create a release by either pushing a tag like `devnet-v0.1.0` or running the
workflow manually with that version. The release publishes:

- `blockdag-devnet-<version>-linux-amd64.zip`
- `blockdag-devnet-<version>-linux-arm64.zip`
- `install.sh`
- `install.ps1`
- `version.txt`

Linux users can run the pinned bootstrap:

```bash
curl -fsSL https://github.com/BlockdagEngineering/stack/releases/download/devnet-v0.1.0/install.sh | sh
```

Windows PowerShell users can run:

```powershell
iwr https://github.com/BlockdagEngineering/stack/releases/download/devnet-v0.1.0/install.ps1 -OutFile install.ps1
.\install.ps1
```

If the GitHub repository remains private, the release asset URLs still require
GitHub authentication. To avoid giving dapp developers repository access,
publish the generated zip and installer assets through the approved external
artifact channel, or send them the matching zip directly.

## Start From Source

From the `stack` repository:

```bash
cp .env.devnet.example .env.devnet
docker compose --env-file .env.devnet -f docker-compose.yml -f docker-compose.devnet.yml build node pool collector dashboard cpu-miner
docker compose --env-file .env.devnet -f docker-compose.yml -f docker-compose.devnet.yml up -d pool-db node pool collector dashboard cpu-miner
```

On Windows PowerShell:

```powershell
Copy-Item .env.devnet.example .env.devnet
docker compose --env-file .env.devnet -f docker-compose.yml -f docker-compose.devnet.yml build node pool collector dashboard cpu-miner
docker compose --env-file .env.devnet -f docker-compose.yml -f docker-compose.devnet.yml up -d pool-db node pool collector dashboard cpu-miner
```

## Endpoints

- Native DAG RPC: `http://127.0.0.1:38131`
- EVM HTTP RPC: `http://127.0.0.1:18545`
- EVM WebSocket RPC: `ws://127.0.0.1:18546`
- Pool Stratum: `stratum+tcp://127.0.0.1:3334`
- Pool metrics: `http://127.0.0.1:9090/metrics`
- Collector API: `http://127.0.0.1:9280/api/status`
- Dashboard: `http://127.0.0.1:8088`

## Operate

```bash
docker compose --env-file .env.devnet -f docker-compose.yml -f docker-compose.devnet.yml ps
docker compose --env-file .env.devnet -f docker-compose.yml -f docker-compose.devnet.yml logs -f node pool cpu-miner
```

Stop the devnet while preserving chain and database volumes:

```bash
docker compose --env-file .env.devnet -f docker-compose.yml -f docker-compose.devnet.yml down
```

Delete the local devnet state only when you intentionally want a fresh chain:

```bash
docker compose --env-file .env.devnet -f docker-compose.yml -f docker-compose.devnet.yml down -v
```
