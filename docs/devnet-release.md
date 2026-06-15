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

## Start

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
