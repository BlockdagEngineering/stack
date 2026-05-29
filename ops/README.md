# BlockDAG Pool Ops

This folder contains local monitoring and repair tools for the BlockDAG ASIC pool stack.

## Dashboard

Run locally:

```bash
python3 ops/dashboard.py
```

Open:

```text
http://127.0.0.1:8088
```

The dashboard shows container state, latest imported block numbers, node sync state, pool errors, and recent logs.

Sync health checks are intentionally conservative. The dashboard and watchdog warn when:

- the two local nodes drift by more than `BDAG_NODE_LAG_WARN_BLOCKS` blocks, default `5`
- a node has not imported a block for `BDAG_NODE_IMPORT_STALE_SECONDS`, default `180`
- recent node logs contain repeated malformed peer or P2P stream-reset errors, controlled by `BDAG_NODE_P2P_ERROR_WARN_COUNT`, default `10`

Only real catch-up problems put the dashboard into `syncing`: pool initial download, node import staleness, node-to-node lag, peer-ahead lag, or RPC refusal. Maintenance warnings such as malformed peer spam stay visible in the alert list, but they do not mark the pool as syncing when both nodes are importing current blocks.

The dashboard also watches for pool share stalls. If miners are connected but the pool stops accepting valid shares for several minutes, that is treated as a recovery condition and the watchdog will restart the stack after the configured threshold.

The watchdog also has a fast-sync recovery path. If real syncing warnings persist for `BDAG_WATCHDOG_SYNCING_THRESHOLD` checks, default `5`, it runs a normal stack restart to force fresh peer/RPC connections and apply the current config. This restart is cooldown-limited by `BDAG_SYNCING_RESTART_COOLDOWN`, default `900` seconds, so it cannot loop continuously.

The persisted peer list in `asic-pool/.env` should contain only valid multiaddrs. Removing a bad peer from `.env` takes effect on the next controlled node restart; it does not interrupt currently running miners by itself.

The pool is configured to use the local `rpc-failover` service as its primary DAG RPC endpoint on the next stack start. Direct node RPC URLs remain listed as fallbacks, and the dashboard still compares both node heights so TCP-level failover does not hide node drift.

The Miners tab can scan the private LAN for ASIC web interfaces and configure selected miners to the current local pool endpoint. The scanner is limited to private IPv4 LAN targets, and every miner's existing pool list is backed up under:

```text
ops/runtime/miner-backups/
```

Default miner settings are derived from the running pool:

- Pool URL: `stratum+tcp://<pool-lan-ip>:3334`
- Worker/wallet: the `MINING_ADDRESS` in `asic-pool/.env`
- Pool password: `1234`

Managed miners are stored in:

```text
ops/runtime/miners.json
```

The watchdog checks these miners on every loop. If a managed miner is no longer configured for the local pool or stops submitting shares, the watchdog can re-apply the pool configuration when an admin password has been saved from the dashboard. The saved password file is local-only and mode `0600`:

```text
ops/runtime/miner-admin-password.txt
```

Two checks are used for miner health:

- the ASIC web API still reports the expected pool configuration
- the pool log shows recent accepted shares or active jobs for that miner IP

## Earnings

The Earnings tab reads the pool database for authoritative address credits, parses recent pool logs to estimate per-ASIC contribution, and records snapshots to:

```text
ops/runtime/earnings-snapshots.jsonl
```

The pool database credits the wallet/worker address, not the ASIC IP. Per-miner earnings are therefore estimated from accepted share work in the recent pool log window. The dashboard shows estimated per-miner totals, average BDAG per hour, and a USD/ZAR bar plot when a live price is available.

The dashboard checks wallet balance from the local BlockDAG nodes and attempts best-effort cross-checks against public explorer/API endpoints. Some explorer endpoints may block server-side requests or may not expose an Etherscan-compatible API; those failures are shown in the Wallet Cross-Check table without stopping local monitoring.

For CoinMarketCap prices, set an API key in the dashboard/watchdog service environment file:

```bash
mkdir -p ops/runtime
printf 'CMC_PRO_API_KEY=your-key\n' > ops/runtime/ops.env
systemctl --user restart bdag-boot-repair.service bdag-dashboard.service bdag-watchdog.service
```

The BlockDAG CoinMarketCap ID used by default is `31162`; override it with `BDAG_CMC_ID` if CoinMarketCap changes the listing.

Action buttons are intentionally limited to known maintenance tasks:

- Start stack
- Restart stack
- Clean restore from latest snapshot
- Write a Codex handoff file
- Scan/configure LAN miners from the Miners tab

It does not provide arbitrary shell access.

## Shared Status Sampler

Routine monitoring processes should share one status collection instead of each
process independently collecting Docker logs, node RPC, pool metrics, and miner
state. Run one sample:

```bash
python3 ops/status_sampler.py --json
```

Run continuously:

```bash
python3 ops/status_sampler.py --loop
```

The sampler writes `ops/runtime/status-sampler.json` atomically. Dashboard,
watchdog, sync coordinator, P2P guard, and startup checks consume it through
`collect_status_cached()` while it is fresh. Use `max_age_seconds=0` only for
explicit live diagnostics or hard repair paths that must bypass cached state.

The sampler is also the backstop for the mining imperative. If the user-systemd
guard units drift disabled, it re-enables them. If `asic-pool` is stopped while
miner demand is visible, an ASIC LAN neighbor is present, or the chain is synced
and ready to mine, it starts the pool container without recreating dependencies.
Set `BDAG_MINING_IMPERATIVE_REPAIR_ENABLED=0` only for an intentional maintenance
window where mining must remain stopped.

## Watchdog

Run one check:

```bash
python3 ops/watchdog.py --once
```

Run continuously:

```bash
python3 ops/watchdog.py --loop
```

Repair modes:

```bash
python3 ops/watchdog.py --repair start
python3 ops/watchdog.py --repair restart
python3 ops/watchdog.py --repair clean
```

The watchdog performs a staged repair:

1. Start missing containers.
2. Restart if the node wrapper is up but the `bdag` child process is gone.
3. Clean restore only after repeated hard failures, such as critical database startup errors.

Clean restore stops the stack, moves existing `data/node1` and `data/node2` to timestamped backups, restores the newest snapshot from `data-restore/`, and starts the stack.

Boot-time recovery is handled by `bdag-boot-repair.service`, which waits for Docker, checks the dirty-shutdown marker, and preserves existing chain data by default. A dirty marker now triggers a conservative start/restart path; automatic clean restore is disabled unless `BDAG_ENABLE_AUTOMATIC_CLEAN_RESTORE=1` is set explicitly.

## P2P Guard

The P2P guard is a passive network-health sampler. It does not restart nodes, pools, miners, or HAProxy. It records whether the active RPC primary and standby node are healthy enough for mining failover, and whether the local network path is still low-latency.

Run one sample:

```bash
python3 ops/p2p_guard.py --once
```

Create a comparison marker:

```bash
python3 ops/p2p_guard.py --once --mark "before network change"
```

Compare after a window:

```bash
latest=$(ls -1t ops/runtime/p2p-guard-marker-*.json | head -1)
python3 ops/p2p_guard.py --compare-marker "$latest" --window-seconds 3600
```

## Runtime Files

Runtime logs and status files are written to:

```text
ops/runtime/
```

Important files:

- `ops/runtime/watchdog-state.json`
- `ops/runtime/latest-action.json`
- `ops/runtime/codex-handoff.md`
- `ops/runtime/p2p-health-state.json`
- `ops/runtime/p2p-health-history.jsonl`
- `ops/runtime/logs/watchdog.log`
- `ops/runtime/logs/p2p-guard.log`

## User Systemd

The installed setup uses user-level systemd services, so no root-owned service files are required.

Installed unit files:

```text
~/.config/systemd/user/bdag-boot-repair.service
~/.config/systemd/user/bdag-dashboard.service
~/.config/systemd/user/bdag-stack-sentinel.service
~/.config/systemd/user/bdag-stack-sentinel.timer
~/.config/systemd/user/bdag-p2p-guard.service
~/.config/systemd/user/bdag-watchdog.service
~/.config/systemd/user/bdag-sync-coordinator.timer
~/.config/systemd/user/bdag-chain-restore-guard.timer
~/.config/systemd/user/bdag-chain-presync.timer
~/.config/systemd/user/bdag-hourly-snapshot.timer
~/.config/systemd/user/bdag-local-peers.timer
```

Service templates are in:

```text
ops/systemd/user-bdag-boot-repair.service
ops/systemd/user-bdag-dashboard.service
ops/systemd/user-bdag-watchdog.service
```

Install or update them with the generated, path-correct units:

```bash
./ops/install-dashboard.sh
```

Enable lingering so user services can start at boot without an active login:

```bash
loginctl enable-linger jeremy
```

Check status:

```bash
systemctl --user status bdag-boot-repair.service bdag-dashboard.service bdag-watchdog.service bdag-stack-sentinel.timer
```

View logs:

```bash
journalctl --user -u bdag-boot-repair.service -u bdag-dashboard.service -u bdag-watchdog.service -u bdag-stack-sentinel.service -f
```

The watchdog writes `ops/runtime/dirty-shutdown.marker` while it is running and clears it on a clean stop. If the host loses power, the marker remains; the boot-repair unit preserves current node data, starts the stack, and keeps any sync-coordinator paused follower stopped so one node can continue catching up. Do not enable automatic clean restore unless the current snapshots are known safe and replacing live chain data is explicitly intended.

## Remote Access

The dashboard binds to `127.0.0.1` by default. For remote viewing, use SSH forwarding:

```bash
ssh -L 8088:127.0.0.1:8088 jeremy@POOL_HOST
```

Then open `http://127.0.0.1:8088` on your local computer.

Avoid exposing the dashboard directly to the public internet.

## Portable Installs

The dashboard is now configurable through `ops/runtime/ops.env`, so it can be copied to another pool host or run as multiple named instances on one management machine.

Create a clean bundle that excludes runtime logs, passwords, chain data, database data, snapshots, and `asic-pool/.env`:

```bash
./ops/package-dashboard.sh
```

Install the dashboard/watchdog from any copied repository:

```bash
./ops/install-dashboard.sh
```

For multiple pools on one host, use separate names, ports, and runtime directories:

```bash
./ops/install-dashboard.sh --name pool-a --port 8088 --runtime-dir /var/lib/bdag-pool-a
./ops/install-dashboard.sh --name pool-b --port 8089 --runtime-dir /var/lib/bdag-pool-b
```

## Codex Memory

Codex context is stored in a local SQLite database with compressed payloads and provenance:

```text
~/.codex/memories/context-store/context.sqlite
```

The memory service tails `~/.codex/history.jsonl` and also ingests markdown notes from:

- `~/.codex/memories`
- `ops/runtime`

It also writes session handoff snapshots to:

```text
~/.codex/memories/snapshots/
```

The handoff generator also writes a short restart checklist to:

```text
ops/runtime/codex-restart-checklist.md
```

Read that checklist first on a fresh restart. It is regenerated together with the main handoff file and ingested by the memory service because `ops/runtime` is part of the watched paths.

Install and start it:

```bash
./ops/install-codex-memory.sh
```

Search it:

```bash
python3 ops/codex_memory.py search "pool restart"
python3 ops/codex_memory.py session <session-id>
```

The service keeps the raw payload compressed, stores provenance for every entry, and indexes summaries for fast lookup.

Edit the generated env file for that pool's wallet, LAN pool address, miner scan target, and container names. See:

```text
ops/PORTABLE.md
ops/portable.env.example
```
