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
- Prefer the newest available chain data during recovery only after its
  manifest proves it is restore-safe.
- During one-node catch-up, give the active leader all known public peers sorted
  by reachability/latency and do not make it dial the paused follower.
- When any managed node is more than 1000 blocks behind the observed network
  tip, pause the laggiest running node and let exactly one selected leader sync
  alone. The selected leader must receive the highest Docker CPU shares and
  block IO weight while this policy is active.
- When a single running node, or the selected dual-node leader, is more than
  1000 blocks behind, the release default is fastest catch-up rather than
  passive monitoring. The node must start with `--fastartifactsync` enabled, and
  the sync coordinator may restart an unaccelerated or stale importer after the
  cooldown window so V2 artifact sync and preferred peers are active.
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
- The leader height is within `BDAG_SYNC_COORDINATOR_SEED_NEAR_TIP_BLOCKS`
  of that highest observed block.
- The follower is the planned paused follower.
- The final copy can stop the leader briefly for a consistent rsync.

This prevents an isolated or poorly peered node from being treated as fully
synced merely because `eth_syncing` returns false.

Plain follower resume is less aggressive than seeding: it is allowed when the
selected leader is within `BDAG_SYNC_COORDINATOR_LEADER_NEAR_TIP_BLOCKS` and
the follower is no more than `BDAG_SYNC_COORDINATOR_FAR_BEHIND_BLOCKS` behind.
This prevents a pause/resume loop where a follower still over 1000 blocks
behind is restarted only to be immediately paused again.

## Required Latest-Data Behavior

Recovery must scan the newest available chain manifests before assuming the
current importer is the best path. A newer candidate can replace live catch-up
only when its manifest is restore-safe and materially ahead of the current
importer.

Unsafe warm copies must be recorded and rejected, not repeatedly retried against
live nodes. The release bundle should include `ops/latest_chain_candidate.py`
for this read-only decision record. The checker writes
`ops/runtime/latest-chain-candidate-state.json` and should make the decision
explicit: newest safe candidate available, current importer is best, or newest
candidate rejected with reasons.

## Required Peer Selection Behavior

Large catch-up should avoid wasting startup dials on unreachable or paused
peers. The local peer updater should:

- Prefer complete FastSync multiaddr candidates in this order: LAN, private or
  VPN, then public internet. This default applies to pre-start FastSnap and
  normal node startup `--addpeer` arguments.
- Sort known public peer multiaddrs by TCP reachability and latency.
- When one managed node is paused for leader catch-up, assign all known public
  peers to the active leader.
- Omit the paused follower from the leader's startup peer list.
- Keep the local peer deferred-apply marker instead of recreating the active
  leader solely to apply peer-list changes.

This is an optimization, not a reason to restart a healthy importing leader.
If the leader already has a healthy peer count and is importing, let it run.

## Validation

Run the validation script against an unpacked Pi5 release bundle:

```bash
scripts/validate-pi5-restart-hardening.sh /path/to/unpacked/release
```

The script fails if the bundle still has the brittle dirty-shutdown clean
restore behavior or if the sync coordinator can seed from a stale leader.

## Release Candidate Self-Healing Defaults

The next Pi5 release candidate must also preserve these defaults:

- `BDAG_NODE_MODE=single` unless the installer/operator chooses double-node mode.
- `COMPOSE_PROFILES=dual-node` only for double-node mode.
- `BDAG_ENABLE_NODE_MINING=0`, `BDAG_NODE_MODULES=Blockdag`, and empty
  `BDAG_NODE_MINING_ARGS` until actual miners are present.
- `BDAG_FASTSYNC_PREPROCESS_WORKERS=1` on Pi catch-up hosts until the node-side
  parallel FastSync preprocessor fault is fixed and soaked.
- `BDAG_FASTARTIFACTSYNC_ENABLED=1` and
  `BDAG_SYNC_COORDINATOR_ACCELERATE_FASTSYNC=1`; a node more than 1000 blocks
  behind should use V2 artifact sync and the ordered peer list by default.
- `bdag-stack-sentinel.timer` and the guard timers are installed by default.
- Displayed dashboard block height comes only from chain RPC `getBlockCount`.

These are release gates, not local preferences. Future changes should update
the validation script in the same commit if they intentionally alter one of
these invariants.
