# Platform Adaptive Runtime

The release stack is expected to run on Linux AMD64 and ARM64 hosts first, with
installer support for macOS and Windows Docker hosts. Runtime optimization must
therefore be adaptive instead of Pi-only.

## Capability Profiles

`ops/capability_profile.py` is the shared resolver for hardware, storage,
network-topology, and memory policy. It deliberately looks past CPU architecture:
accepted block production is usually limited by the active chain device, network
topology, memory pressure, template freshness, and whether background sync
serving is competing with mining.

The resolver emits a named `BDAG_CAPABILITY_PROFILE` and recommended environment
settings for:

- node and embedded EVM cache sizes
- Go `GOMEMLIMIT` budgets
- FastSync serving policy
- FastSnap/FastSync parallelism
- P2P peer budget
- block device read-ahead and queue depth
- Linux VM writeback/cache settings
- PostgreSQL cache/checkpoint defaults

Supported automatic profile names include:

- `pi5-usb-asic-router`: Pi5-style host serving DHCP/NAT to directly attached
  ASICs while the active chain DB is on USB/flash.
- `usb-asic-router`: same topology on non-Pi hardware.
- `fragile-flash`: SD card, removable USB flash, or F2FS flash-backed active
  chain data without the ASIC-router topology.
- `usb-ssd`: USB-attached SSD-class chain storage.
- `nvme-single-node` / `nvme-dual-node`: NVMe chain storage.
- `standard`, `constrained`, and `large-ssd`: fallback host classes.

Keep `BDAG_CAPABILITY_PROFILE=auto` for normal installs. Override it only for a
measured A/B test or a known lab setup.

For a `pi5-usb-asic-router` profile the resolver forces
`BDAG_NO_FASTSYNC_SERVE=1`. That host can consume FastSync, relay blocks, and
mine, but it must not spend the same USB chain device on bulk snapshot/range
serving while ASICs are trying to earn paid blocks.

The cache policy uses RAM aggressively but does not try to pin the whole chain
inside Go heap. It sets process cache and `GOMEMLIMIT` budgets while reserving
RAM for the Linux page cache and PostgreSQL. This matches how LevelDB/Pebble and
RocksDB-style databases benefit from both database block cache and OS page
cache, and avoids converting memory pressure into slow USB/SD swap or writeback
stalls.

Primary references behind this policy:

- Linux VM dirty writeback/cache sysctls:
  <https://www.kernel.org/doc/html/latest/admin-guide/sysctl/vm.html>
- Go `GOMEMLIMIT` behavior:
  <https://go.dev/doc/gc-guide>
- systemd resource controls for memory/IO weighting:
  <https://www.freedesktop.org/software/systemd/man/devel/systemd.resource-control.html>
- F2FS flash-oriented filesystem behavior:
  <https://docs.kernel.org/filesystems/f2fs.html>
- RocksDB/LevelDB cache and memory budgeting:
  <https://github.com/facebook/rocksdb/wiki/Memory-usage-in-RocksDB>
  and <https://chromium.googlesource.com/external/leveldb/+/HEAD/doc/index.md>

`ops/pool_ops.py` detects a lightweight host profile from OS, CPU architecture,
CPU count, memory, and hardware model:

- `pi5`: Linux ARM64 Raspberry Pi 5 class hosts.
- `constrained`: small ARM64 or AMD64 hosts with low CPU or memory.
- `standard`: mid-size desktops, laptops, mini PCs, and VMs.
- `large`: higher-core, higher-memory servers and workstations.

The profile is advisory. Operators can override it with:

```sh
BDAG_HOST_PROFILE=auto
```

Supported override values are `pi5`, `constrained`, `standard`, and `large`.
`auto` is the default.

Adaptive concurrency is enabled by default:

```sh
BDAG_ADAPTIVE_CONCURRENCY_ENABLED=1
```

Routine control-plane loops also share one sampled status file by default:

```sh
BDAG_STATUS_SAMPLER_ENABLED=1
BDAG_STATUS_SAMPLER_INTERVAL_SECONDS=10
BDAG_STATUS_SAMPLER_MAX_AGE_SECONDS=12
```

`ops/status_sampler.py` writes `ops/runtime/status-sampler.json` atomically.
Dashboard, watchdog, sync coordinator, P2P guard, and startup checks use that
file through `collect_status_cached()` while it is fresh, instead of each
process independently collecting Docker logs, node RPC, pool metrics, and miner
state. Explicit repair diagnostics can still bypass the sampler and short cache
with `max_age_seconds=0`.

The existing worker settings remain hard caps:

```sh
BDAG_GLOBAL_RPC_WORKERS=24
BDAG_MINER_SCAN_WORKERS=64
BDAG_MINER_HASHRATE_PROBE_WORKERS=8
```

The adaptive layer chooses lower worker counts when the detected host class is
small or when pressure signals show the host is waiting on I/O, CPU, or slow
chain RPC. On Linux it uses `/proc/pressure/*`, `/proc/stat` iowait, chain RPC
latency from node status, and the sustained iowait state already exposed in the
dashboard. On macOS and Windows Docker hosts those Linux pressure files are not
assumed to exist; the profile still detects OS/arch/CPU, and pressure-specific
shrinking simply degrades to the available signals.

This preserves the Pi5 behavior that protects USB-backed chain import, while
letting AMD64 or larger ARM64 hosts use more concurrency when the machine is
idle enough to benefit.

When the capability resolver is available, dashboard/watchdog status includes
both the legacy `profile` and the richer `capability_profile`. Adaptive worker
budgets prefer the richer profile, so an AMD64 box with USB flash chain data is
treated as a flash-constrained miner rather than as a generic desktop.

Pool block submit fanout is also adaptive by configuration. Single-node hosts
keep one endpoint by default. Dual-node or larger hosts can set
`POOL_SUBMIT_RPC_URLS` or `NODE_RPC_URLS` to an ordered list of direct node RPC
endpoints. The pool only races valid block-candidate submits, returns after the
first accepted endpoint, and records slower peer outcomes asynchronously, so
normal share validation and no-miner sync-only mode stay low-overhead.

Systemd timers are also staggered. Short-interval guards and priority loops use
small `RandomizedDelaySec` values so they remain responsive but do not all wake
on the same second after boot or after a shared interval boundary. Longer
snapshot, FastSnap seed, chain pre-sync, local-peer, and incident-report timers
use larger jitter because freshness can safely lag behind chain import and live
mining.
