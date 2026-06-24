# Authoritative Stack Clean Upgrade Playbook

This repo, `BlockdagEngineering/stack`, is the authoritative install and upgrade
repo for the Redis dashboard mining stack. `BlockdagEngineering/stack-redis` is
retired as a runtime target; use it only as historical reference when migrating
useful scripts or defaults into this repo.

## Source Contract

Release and source installs build from these repos only:

- `BlockdagEngineering/stack`
- `BlockdagEngineering/blockdag-corechain`
- `BlockdagEngineering/pool`
- `BlockdagEngineering/redis-dash`

Use the `main` branch for release builds unless the release manifest explicitly
pins a different branch or SHA. Do not add release build contexts, workflow
checkouts, compose services, or install steps for collector, dashboard2, CPU
miner, GPU miner, or any old dashboard repo.

## Meaning Of Destructive Reinstall

Destructive reinstall means the incoming source and environment become the new
normal:

1. Build fresh images from the current source contract.
2. Recreate affected services from those images, with `--no-build --pull never`
   during deploy so the verified image IDs are the ones that run.
3. Remove stale source folders, old release images, dangling layers, and build
   caches only after explicit human verification.
4. Preserve live chain, peers, ASIC configuration, pool accounting, and rollback
   evidence.

It does not mean deleting live blockchain data, pool accounting, ASIC configs, or
the only working rollback set.

## Preserve And Replace Rules

Always preserve before mutation:

- `.env`, `node.conf`, compose files, and installer-generated runtime env files.
- Chain data and nodeworker state.
- P2P peer lists, node keys, and peerstore evidence.
- Postgres pool accounting and durable block-submission history.
- Dashboard Redis state when present; it is rebuildable but useful for history.
- Current image IDs for the running stack.
- ASIC configuration stored on the miners.

If newer chain data is supplied on USB or in the install folder, stage it as the
candidate replacement, compare its height/freshness against the live chain data,
and use the newer data only after recording a rollback copy of the old live data.
After the human verifies the upgraded stack, remove stale extra copies and keep
only the live data plus the intentional rollback set.

## Fast Upgrade Flow

1. Pull the memory repo and source repos.
2. Record source SHAs, `docker ps`, `docker compose ps`, image IDs, compose
   config, disk/RAM/load, and ASIC MAC/IP mapping.
3. Run release validation before stopping mining.
4. Build node, pool, dashboard, watchdog, status-sampler, and sentinel images
   from source before the mining freeze when possible.
5. Stop repair automation that could race the cutover.
6. Stop `pool` before touching node/dashboard so ASICs do not receive stale work.
7. Recreate services in order: `pool-db`, `node`, `dashboard`, `status-sampler`,
   `pool`, then `watchdog`, `sentinel`, and `miner-route` when configured.
8. Validate node RPC, native template health, P2P freshness, pool active miners,
   accepted block submissions, dashboard live global data, dashboard DAG data,
   Redis memory, and logs.
9. Ask for explicit human verification before pruning old images/caches or
   deleting rollback data.

## Required Release Guards

Keep these checks active so drift is caught in CI and local validation:

- `scripts/validate-release-build.sh` must reject collector/dashboard2/CPU/GPU
  release paths and require `redis-dash`.
- Deployment tests must assert compose and Dockerfiles build only node, pool,
  dashboard, watchdog, status-sampler, and sentinel.
- Mainnet defaults must never fall back to `test:test` RPC credentials.
- `POOL_STRATUM_SERVER_FIRST_DIFFICULTY_PROBE=false` must remain the production
  default for Goldshell cloud-box/MCB miners.
- Repair services must use `--no-build --pull never`; watchdog/sentinel must not
  build or pull during automatic repair.

## Rollback

Rollback uses the preserved evidence:

1. Stop affected services.
2. Restore previous compose/env/config if those changed.
3. Recreate from previous image IDs or rebuild previous SHAs.
4. Restore previous chain/accounting data only if the upgrade replaced it.
5. Start in order: `pool-db`, `node`, `dashboard`, `status-sampler`, `pool`,
   then guard services.
6. Revalidate mining and dashboard live data before exposing the pool.

Do not prune the previous image set or rollback data until the human explicitly
accepts the upgraded stack.
