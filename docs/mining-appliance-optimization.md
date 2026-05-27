# Mining Appliance Optimization

This repo ships defaults for dedicated BlockDAG mining hosts. They are intended
for single-node and dual-node deployments; every node service should use the same
node resource profile and the same host profile.

## Docker Defaults

The Compose files use:

- Docker `local` logs capped at `10m` x `2` files.
- High CPU and block IO weights for node, pool, and Postgres.
- Lower CPU and block IO weights for dashboard/control-plane services.
- Large `nofile` limits for node and pool sockets.
- Graceful stop windows for node and database shutdown.
- Node cache, BD cache, DAG cache, reduced log verbosity, and no file logging
  via `NODE_ARGS_APPEND`.
- `BDAG_NODE_SUBMIT_OBSOLETE_HEIGHT` defaults to `20` on mining appliances so
  high-throughput ASIC solves that arrive a few DAG tips behind still reach
  corechain consensus instead of being rejected by the RPC submit precheck.
- `BDAG_NODE_MAX_BAD_RESPONSES` defaults to `12` so public peers that repeatedly
  reset block-broadcast streams are rotated out sooner, while explicitly ordered
  LAN/VPN fast-sync peers remain the preferred low-latency path.

For dual-node overlays, apply the same settings to every production node:

```yaml
logging: *mining-logging
cpu_shares: 4096
blkio_config:
  weight: 1000
oom_score_adj: -900
ulimits:
  nofile:
    soft: 1048576
    hard: 1048576
environment:
  NODE_ARGS_APPEND: >-
    --cache=${BDAG_NODE_CACHE_MB:-4096}
    --bdcachesize=${BDAG_NODE_BD_CACHE_SIZE:-8192}
    --dagcachesize=${BDAG_NODE_DAG_CACHE_SIZE:-8192}
    --obsoleteheight=${BDAG_NODE_SUBMIT_OBSOLETE_HEIGHT:-20}
    --maxbadresp=${BDAG_NODE_MAX_BAD_RESPONSES:-12}
    --debuglevel=${BDAG_NODE_DEBUG_LEVEL:-error}
    --evmtrietimeout=${BDAG_EVM_TRIE_TIMEOUT_SECONDS:-7200}
    --nofilelogging
```

Do not add `--allowminingwhennearlysynced`, `--miner`, `--miningaddr`, or
`modules=miner` on no-miner hosts. When a node is behind tip, catch-up is the
first priority. The runtime priority service therefore boosts node import above
all other stack work while the dashboard reports `syncing`; when no miners are
tracked, it also idles pool, database, and RPC-routing containers so the host is
effectively sync-only until caught up.

## Host Profile

Install once on a dedicated mining host before starting the stack:

```bash
sudo scripts/install-mining-appliance-profile.sh
```

This installs:

- `/etc/sysctl.d/90-mining-appliance.conf`
- `/etc/systemd/journald.conf.d/90-mining-appliance.conf`
- `/usr/local/sbin/mining-appliance-host-tuning`
- `/usr/local/sbin/bdag-runtime-priority`
- `/usr/local/sbin/bdag-node-child-guard`
- `/etc/systemd/system/mining-appliance-tuning.service`
- `/etc/systemd/system/bdag-runtime-priority.service`
- `/etc/systemd/system/bdag-runtime-priority.timer`
- `/etc/systemd/system/bdag-node-child-guard.service`
- `/etc/systemd/system/bdag-node-child-guard.timer`
- `/etc/docker/daemon.json` defaults for `live-restore` and local logs

It also tunes P2P/RPC socket buffers, raises the mining block-device queue,
keeps the CPU governor in performance mode, and re-applies runtime nice/ionice
priorities every minute. Node, pool, Postgres, RPC routing, Docker/containerd,
Wi-Fi, and ZeroTier are favored. Dashboard, browser, Codex, and other desktop
helpers are lowered so live blockchain and pool work wins CPU, memory pressure,
disk IO, and network scheduling.

The node child guard checks every minute that a nodeworker container still has a
real `bdag` child process and an open RPC or WebSocket listener. If the wrapper
is alive but the node child has crashed, the guard restarts only the node
container; it does not start mining services on no-miner hosts.

## USB Chain Data

For Pi-class hosts where SD-card random write latency is the sync bottleneck,
keep the OS on the SD card and place the active stack data on a faster USB
filesystem. The runtime profile detects USB block devices and applies the
mining storage queue profile at boot: `mq-deadline`, 2048 KiB read-ahead, 256
queue requests where the device allows it, `max_sectors_kb=1024`, non-rotational
media classification, and no entropy collection from block IO.

Use a stable mount such as `/mnt/bdag-usb`, mount ext4 or F2FS with
`noatime,lazytime`, and make Docker require that mount before container
startup. Then point only active data directories at the USB, for example:

```bash
data/node1 -> /mnt/bdag-usb/blockdag-stack/data/node1
data/node2 -> /mnt/bdag-usb/blockdag-stack/data/node2
data/postgres -> /mnt/bdag-usb/blockdag-stack/data/postgres
ops/runtime -> /mnt/bdag-usb/blockdag-stack/ops-runtime
```

Leave old parked chain snapshots on the SD card unless the USB has enough spare
space. This keeps the USB focused on the hot node, pool DB, dashboard history,
and guard-log write path while preserving rollback copies on the OS disk.

The installer disables common non-mining timers and services such as apt daily
jobs, cron, Avahi, CUPS, NFS/rpcbind, and desktop disk/power helpers. It leaves
Bluetooth available so local keyboards and mice can be paired without undoing
the mining appliance profile.

For a desktop that should only run the dashboard and Codex, run as the desktop
user:

```bash
scripts/install-mining-user-session-profile.sh
```

This masks audio, keyring, GVFS, and desktop portal services that are not needed
for a dashboard/Codex appliance session.
