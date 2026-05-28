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
startup. On constrained appliances the release installer now resolves
`BDAG_STORAGE_PROFILE=auto` into explicit paths. The default policy keeps the
large, growing node datadirs on the capacity disk, then moves frequent small
writes to internal storage when the boot disk has at least 4 GiB free:

```bash
BDAG_CHAIN_DATA_DIR=/mnt/bdag-usb/blockdag-chain
BDAG_NODE1_DATA_DIR=/mnt/bdag-usb/blockdag-chain/node1
BDAG_NODE2_DATA_DIR=/mnt/bdag-usb/blockdag-chain/node2
BDAG_POSTGRES_DATA_DIR=/opt/blockdag-pool/runtime-data/postgres
BDAG_RUNTIME_DIR=/opt/blockdag-pool/runtime-data/ops-runtime
```

Leave old parked chain snapshots on the SD card unless the USB has enough spare
space. This keeps the USB focused on the hot node chain and FastSnap artifacts
while the OS disk absorbs Postgres WAL, dashboard history, guard state, and
small log churn. If the internal disk is too small, the installer falls back to
a single-device USB profile and the preflight reports that all hot writes share
one device.

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
