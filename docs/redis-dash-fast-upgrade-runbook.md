# Redis Dash Fast Upgrade Runbook

This is the agent and human fast path for the redis dashboard stack upgrade.
It records what should have been known before the 2026-06-22 redis-dash
cutover so the next upgrade is limited to build, redeploy, restart, and verify.

## Target Shape

Runtime services for this release line are:

- `node`: mainnet corechain node, chain RPC, EVM RPC/WS, and P2P sync.
- `pool`: Stratum mining, template/job handling, block submit, and accounting.
- `postgres`: pool accounting and durable block submission history.
- `dashboard`: redis-dash runtime, dashboard HTTP/UI, private Redis state, and
  dashboard ingest workers.
- Optional guard services may exist in the repo, but they must not race a
  controlled upgrade.

Retired `collector` and old `dashboard2` images are not part of this target.
`BlockdagEngineering/stack` is the authoritative stack repo, and `redis-dash`
is the only dashboard source repo for this release line.

## Non-Negotiable Invariants

- Mainnet only for production pool work. Set `BDAG_NETWORK=mainnet`.
- Do not use `test:test` as node RPC credentials. `.env` and `node.conf`
  `rpcuser` / `rpcpass` must match and must be site-specific.
- Accepted shares are not paid mining. Paid mining health needs accepted block
  submissions or on-chain production evidence.
- `getTemplateHealth` is the primary native safety gate. Require
  `submit_ready=true`, `mineable_now=true`, fresh P2P mining evidence, a fresh
  consensus peer floor of at least two, and peer lead inside tolerance.
- EVM/public-RPC lag is advisory only when native proof is safe.
- Set `POOL_RPC_ROUTER_EVM_HEAD_GUARD_ENABLED=false` for mining releases so
  local EVM indexing lag cannot override native-safe template health.
- ASIC identity is MAC-only. IP addresses are observations.
- Goldshell cloud-box/MCB-compatible installs must keep
  `POOL_STRATUM_SERVER_FIRST_DIFFICULTY_PROBE=false`. This firmware family
  expects client-first Stratum; on 2026-06-22 the server-first difficulty probe
  made both managed miners send `mining.subscribe`, then disconnect before
  `mining.authorize`. Repeated pool logs with `reason=no-request-eof` or metric
  `pool_stratum_no_request_disconnects_total` mean the ASIC is opening TCP and
  closing before any Stratum request; repair miner firmware/API/configuration
  and do not diagnose it as a wallet or payout issue. Enable the probe only as a
  lab diagnostic with an explicit soak test.
- The release watchdog must carry Goldshell cloud-box/MCB API-stall recovery.
  If managed ASICs answer `/mcb/status` while `/mcb/pools` or
  `/mcb/cgminer?cgminercmd=devs` stalls, and the pool has no active miners with
  no-request EOF churn, watchdog should use open `/mcb/restart` after the
  shorter no-active confirmation window rather than waiting for manual recovery.
- Build from current source before the mining freeze whenever possible.
- Preserve data volumes. Destructive reinstall means remove old code/images
  after verification, not deleting chain, peers, accounting, or dashboard Redis
  history.
- `NODE_DATA_DIR=./data/node` is the only canonical node datadir. The obsolete
  `BDAG_NODE_DATA_DIR` variable must not appear in final runtime config. Run
  `scripts/preflight-chain-data.sh` before compose start, and migrate legacy
  `stack_node-data` into `./data/node` with
  `scripts/migrate-node-data-volume-to-host.sh` when it is the best preserved
  source.

## Preserve List

Do not remove these during destructive reinstall:

- Docker volume `stack_node-data`
- Docker volume `stack_nodeworker-data`
- Docker volume `stack_postgres-data`
- Docker volume `stack_dashboard-redis`
- Project `.env`
- Project `node.conf`
- ASIC configuration stored on the miners
- `/home/jeremy/.codex/memories/codex-memory`

If a peerstore is contaminated, quarantine it with a timestamped suffix rather
than deleting it. Record the quarantine path in the checkpoint.

## Preflight

Run this before stopping mining:

1. Pull memory and read current stack memory:
   `git -C /home/jeremy/.codex/memories/codex-memory pull --ff-only`.
2. Fetch all source repos and verify the target branch/commit:
   `stack`, `redis-dash`, `pool`, and `blockdag-corechain`.
3. Record `git rev-parse HEAD` for each repo.
4. Confirm worktrees are clean or record exactly what local changes are part of
   the upgrade.
5. Confirm `.env` has `BDAG_NETWORK=mainnet`, production RPC credentials, and
   production DB defaults.
6. Confirm `.env` has `NODE_DATA_DIR=./data/node` and no
   `BDAG_NODE_DATA_DIR`.
7. Run `scripts/preflight-chain-data.sh`; resolve any mismatch before the mining
   freeze.
8. Confirm `node.conf` has matching `rpcuser` / `rpcpass`, `modules=Blockdag`
   and `modules=miner` when ASIC mining is expected, and no bypass flags such
   as `--allowminingwhennearlysynced`.
9. Record `docker ps`, `docker compose ps`, `docker images` for stack images,
   and `docker compose config --quiet`.
10. Check disk/RAM/load with `df -h`, `free -h`, `docker system df`, and
   `docker stats --no-stream`.
11. Record MAC/IP mapping for managed ASICs in the private site checkpoint.
12. Check live gates before mutation:
    - node RPC `getTemplateHealth`
    - dashboard `/api/status`
    - dashboard `/api/live/global`
    - pool metrics and recent logs
    - accepted block submission delta over a short window
13. Check peer hygiene:
    - peer count has at least two fresh consensus peers
    - no stale peer dominates readiness
    - no private/loopback/ephemeral peerstore entries are being promoted as
      useful mainnet peers

## Peer Hygiene Lessons From 2026-06-22

The slowest part of the upgrade was not image build time. It was peer/readiness
hygiene after the stack came back.

Known bad peer observations belong in the private site checkpoint. If a peer
repeatedly stays stale and dominates readiness while other peers are current,
quarantine or block it before exposing mining.

Good peer acquisition required:

- complete public mainnet multiaddrs with peer IDs
- no stale private/ephemeral peerstore promotion
- enough inbound capacity for a stable fresh peer floor
- no single-peer mining

The 2026-06-22 runtime used `--maxinbound=8` after `--maxinbound=1` prevented
robust peer freshness. Do not blindly copy `--maxinbound=1` onto a live mining
pool unless a measured host profile proves it can still hold the required fresh
consensus peer set.

## Build Before Freeze

Where possible, build new images while mining is still live:

1. Pull source and verify commits.
2. Build images with no cache only when the user explicitly requested a
   destructive source rebuild.
3. Save build logs.
4. Verify the newly built image IDs and source labels where available.
5. Do not prune old images before the human has verified the new stack.

This keeps mining downtime close to the final container replacement window.

## Controlled Cutover

1. Stop watchdog/sentinel/automation services that might race the cutover.
2. Stop `pool` first so ASICs do not receive stale work.
3. Recreate `node` only if the node image, args, env, or config changed.
4. Start `postgres`; wait healthy.
5. Start `node`; wait for RPC and native template health.
6. Start `pool` only after node safety gates are good.
7. Start `dashboard` last; confirm Redis and ingest workers are live.
8. Re-enable guards only after node, pool, dashboard, and ASIC evidence is
   stable.

Do not call the upgrade complete after build alone. Completion requires
affected containers restarted and verified.

## Validation

Minimum validation after redeploy:

- `docker ps` shows `node`, `pool`, `postgres`, and `dashboard` running.
- Dashboard health is healthy at `http://127.0.0.1:8088`.
- Native node `getTemplateHealth` returns `reason_code=ok`,
  `submit_ready=true`, `mineable_now=true`, and
  `p2p_fresh_consensus_peer_count` at or above the configured floor.
- Fresh consensus peers are at or above the configured floor.
- Pool metrics show active/authorized/ready miners when ASICs are expected.
- Accepted block submissions increase after the pool is exposed.
- Dashboard `/api/live/global` is live, has low data age, and stream lag is
  close to zero or moving continuously.
- Producer plot is continuous and not collapsed into left-side dots.
- Redis memory, container memory, and reconnect logs are stable.

## Destructive Cleanup

After explicit human verification:

1. Remove superseded images from the old release line.
2. Prune dangling Docker images and BuildKit cache.
3. Remove old source folders that are not part of the target stack.
4. Keep preserved volumes/configs/memory.
5. Keep a checkpoint with git SHAs, image IDs, live validation, and any runtime
   mitigation such as peer quarantine or firewall blocks.

## Rollback

Rollback is possible because preserved data is not destroyed:

1. Stop affected services.
2. Restore prior compose/env/config if they changed.
3. Recreate prior images or checkout prior source SHAs and rebuild.
4. Start in order: `postgres`, `node`, `pool`, `dashboard`.
5. Revalidate native node gates, pool paid-submit evidence, ASIC MAC lanes, and
   dashboard live data.
