# Agent Notes: Redis Dash Upgrade

Use this with `docs/redis-dash-fast-upgrade-runbook.md` and
`docs/authoritative-stack-clean-upgrade-playbook.md`.

Before changing a live mining stack:

1. Pull `/home/jeremy/.codex/memories/codex-memory`.
2. Read memory stack invariants, especially native readiness gates, MAC-only
   ASIC identity, no `test:test` RPC defaults, and paid-mining evidence.
3. Fetch the authoritative source repos and record exact target SHAs:
   `stack`, `blockdag-corechain`, `pool`, and `redis-dash`.
4. Build replacement images from current source before stopping `pool` whenever
   possible.
5. Preserve `stack_node-data`, `stack_nodeworker-data`, `stack_postgres-data`,
   `stack_dashboard-redis`, `.env`, `node.conf`, ASIC config, and codex-memory.
6. Treat destructive cleanup as a post-verification action. Do not prune the
   last known-good images before the human validates the new stack.

Fast validation commands:

```bash
docker compose config --quiet
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
curl -fsS http://127.0.0.1:8088/api/status
curl -fsS http://127.0.0.1:8088/api/live/global
```

Native readiness must be checked with `getTemplateHealth`. The pool may be
started only when the node is native-safe for mining: submit-ready, mineable,
fresh P2P mining evidence, fresh consensus peer floor, and peer lead inside
tolerance.
Keep `POOL_RPC_ROUTER_EVM_HEAD_GUARD_ENABLED=false` in mining releases; EVM lag
is an advisory dashboard/sync signal when native health is safe.

The 2026-06-22 upgrade proved that peer hygiene is a release gate. If a stale
peer dominates readiness, record it in private memory, quarantine or block it,
and rebuild the fresh peer set before exposing Stratum.
