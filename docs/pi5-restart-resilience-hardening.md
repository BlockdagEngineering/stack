# Pi5 Restart Resilience Hardening

## Problem

The Pi5 mining bundle must survive operator restarts and power failures without
replacing current chain data just because the watchdog left a dirty-shutdown
marker behind. A dirty marker is expected after a power loss. It is not proof
that both node data directories are unusable.

The restart-safe policy is:

- Preserve existing `data/node*` chain data on boot.
- Start or restart services first.
- Respect `ops/runtime/sync-coordinator-state.json` and keep a planned paused
  follower stopped while the leader catches up.
- Disable automatic clean restore by default.
- Allow clean restore only when explicitly enabled by the operator and current
  snapshots are known safe.
- Do not seed a follower unless the leader is proven near the highest observed
  network block height.

## Required Runtime Defaults

The installed `ops/runtime/ops.env` and generated boot-repair unit should set:

```text
BDAG_ENABLE_AUTOMATIC_CLEAN_RESTORE=0
BDAG_BOOT_REPAIR_DIRTY_POLICY=start
BDAG_BOOT_REPAIR_CRITICAL_POLICY=restart
```

With these defaults, `bdag-boot-repair.service` may start or restart the stack
after a dirty shutdown, but it must not move `data/node1` or `data/node2` into
backup directories or restore snapshots unless automatic clean restore has been
explicitly enabled.

## Required Sync-Coordinator Behavior

The sync coordinator must remember the highest network height it has observed
in `observed_highest_block` and use that value when current peers report a
lower or missing `highestBlock`.

Follower seeding is allowed only when all of these are true:

- There is a known `network_highest` value.
- The leader height is within `BDAG_SYNC_COORDINATOR_LEADER_NEAR_TIP_BLOCKS`
  of that highest observed block.
- The follower is the planned paused follower.
- The final copy can stop the leader briefly for a consistent rsync.

This prevents an isolated or poorly peered node from being treated as fully
synced merely because `eth_syncing` returns false.

## Validation

Run the validation script against an unpacked Pi5 release bundle:

```bash
scripts/validate-pi5-restart-hardening.sh /path/to/unpacked/release
```

The script fails if the bundle still has the brittle dirty-shutdown clean
restore behavior or if the sync coordinator can seed from a stale leader.
