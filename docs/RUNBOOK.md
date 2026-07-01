# VQueen v6.5.7 Near-Hot Backup Runbook

This runbook is a draft for later gates. M3 read-only preflight and M4 dry-run
completed on 2026/06/26. On 2026/06/28 Duncan approved repo-only drafting for
the containerized near-hot runner and restore-tested schedule. Backup, restore,
verify, cleanup, timer install/enablement, service actions, commits, pushes,
and payload movement remain blocked until separate approval.

## Runtime Paths

```text
tooling:      /opt/blockdag/eddie-dev/v657-nearhot-backup/
backup data:  /opt/backups/blockchain/vqueen-v6.5.7-nearhot/
dev restore:  /opt/backups/blockchain/dev-restore/
backup logs:  /var/log/blockdag/vqueen-backup/
restore logs: /var/log/blockdag/vqueen-restore-test/
logger lib:   /usr/local/lib/vqueen-nearhot-backup/vqueen-logging.sh
```

## Local Backup Payload Boundary

Backup data, restore sandboxes, PostgreSQL dumps, Docker volume payloads,
identity material, runtime metadata bundles, and sensitive manifests are
host-local only. They must remain on VQueen under the approved local paths and
must not be committed or pushed to git, GitHub, BlockdagEngineering, or any
other remote.

## Phase 1 Flow

1. Read-only preflight.
2. Dry-run. Completed for M4 on 2026/06/26.
3. Manual watched backup with controlled sudo for unreadable live sources.
4. Low-priority two-pass rsync for node, nodeworker, and collector runtime data.
5. PostgreSQL 15 custom dump and schema dump.
6. Second rsync pass.
7. Manifest/checksum generation.
8. Disposable PostgreSQL 15 restore validation.
9. Verify restore evidence.
10. Mark the backup known-good after restore proof passes.

The future scheduled path must use one full-cycle launcher:

```text
preflight -> container backup -> manifest verify -> restore proof ->
verify-only -> mark known-good
```

The draft systemd service must call:

```bash
/opt/blockdag/eddie-dev/v657-nearhot-backup/ops/vqueen-nearhot-cycle-wrapper.sh --scheduled-cycle
```

It must not call `bin/vqueen-backup.sh --backup` directly.

## Config Activation

`etc/vqueen-backup.conf` is ignored and host-local. The tracked
`etc/vqueen-backup.conf.example` is a template only. At M3 closeout, the active
local config differed from the example only by:

```text
RSYNC_BWLIMIT_KB="65536"
```

That bandwidth cap is the only intended active-config divergence from the
shared template; the active host-local config must also carry the production
cycle fields from the template before runtime use.
PostgreSQL identity must keep the Docker container name and Compose service
label distinct:

```text
POSTGRES_CONTAINER=postgres
POSTGRES_COMPOSE_SERVICE=pool-db
```

Before any later approved runtime gate:

```bash
cd /opt/blockdag/eddie-dev/v657-nearhot-backup
cp etc/vqueen-backup.conf.example etc/vqueen-backup.conf
vim etc/vqueen-backup.conf
```

Confirm these values before running any later preflight:

```text
LIVE_READ_ACCESS=controlled-sudo
NODE_DATA_SRC points at the live v6.5.7 node data
NODEWORKER_DATA_SRC points at the live v6.5.7 nodeworker Docker volume data
COLLECTOR_RUNTIME_SRC points at the live v6.5.7 collector runtime volume data
POSTGRES_CONTAINER is the intended live Postgres container name
POSTGRES_COMPOSE_SERVICE matches the compose service label
POSTGRES_IMAGE_FAMILY matches the expected Postgres image family
BACKUP_ROOT is outside the live runtime and tooling tree
DEV_RESTORE_ROOT is outside the live runtime and backup tree
BACKUP_LOG_DIR and RESTORE_LOG_DIR are under /var/log/blockdag/
MIN_FREE_BYTES is acceptable for the backup target
```

The active local config must be reconciled with new example fields before M4 is
requested.

For the containerized cycle draft, reconcile these host-local fields before any
future UAT request:

```text
CONTAINER_RUNNER_IMAGE is pinned to the reviewed local image/digest
CONTAINER_RUNNER_NETWORK is the approved live Postgres network only
POSTGRES_PASSWORD_FILE is root-owned, host-local, and not in git
OPERATION_LOCK is under PROJECT_STATE_DIR and is shared by cycle, backup, and restore modes
CYCLE_LOCK remains under PROJECT_STATE_DIR as a mode-specific diagnostic lock path
KEEP_KNOWN_GOOD=3 unless Duncan approves another retention count
CANDIDATE_RETENTION_DAYS=14
FAILED_RETENTION_DAYS=14
```

## Stop Triggers

Abort the active gate if any of these appear:

```text
live node RPC becomes unhealthy
node or nodeworker logs show fatal errors
host load or iowait materially harms the live node
free space drops below MIN_FREE_BYTES
any path resolves outside its approved root
latest known-good resolves outside BACKUP_ROOT/runs
backup, restore, log, tooling, or live source paths overlap
runner container needs docker.sock, privileged mode, or broad host control
sudo prompts interactively or the backup tool uses commands outside its reviewed
sudo call surface
Postgres container identity cannot be proven by compose project/service/image
```

Abort outcome:

```text
do not mark the run known-good
keep logs and manifests for review
return to the team for M4 evidence review
```

## Restore-Tested Cycle Gate

The scheduled cycle is not approved to run yet. When it is separately approved
for staging/UAT, it must include:

```bash
cd /opt/blockdag/eddie-dev/v657-nearhot-backup
VQUEEN_NEARHOT_CYCLE_APPROVED="vqueen-v6.5.7-restore-tested-cycle-2026-06-28" \
  bin/vqueen-nearhot-cycle.sh --scheduled-cycle
```

A backup is known-good after `bin/vqueen-restore-test.sh --verify-only
<restore-path>` accepts the restore evidence. The scheduled cycle records
`metadata/known-good.txt`, writes `metadata/cycle-state.txt` as `known-good`,
and keeps the newest three known-good backups.

## M5 First Backup Gate

M5 backup execution is not approved by this runbook. When separately approved,
the first backup command must include the one-run approval token:

```bash
cd /opt/blockdag/eddie-dev/v657-nearhot-backup
VQUEEN_M5_BACKUP_APPROVED="vqueen-v6.5.7-first-backup-2026-06-26" \
  bin/vqueen-backup.sh --backup
```

The backup tool may coexist with unrelated admin sudo entries, but its own code
path must use only reviewed sudo calls. It must not use `sudo bash`, `sudo su`,
or generic sudo dispatch.

For controlled-sudo rsync reads, the tool must call only:

```bash
sudo -n /usr/local/sbin/vqueen-nearhot-rsync <source-label> <run-dir>
```

The wrapper accepts only `chain-node`, `nodeworker`, and `collector-runtime`
labels, derives destinations itself under the approved run directory, and uses
fixed rsync arguments.

## Logging Standard

All project scripts use the shared Bash logging framework. Each meaningful
action should log before/after state, refusals, and status transitions using:

```text
YYYY-MM-DD HH:MM:SS: LEVEL    : PID ThreadOrAction         : message
```

Field rules:

```text
time:          local host time
levels:        TRACE DEBUG INFO NOTICE WARNING ERROR CRITICAL FATAL
level width:   8 characters
pid:           BASHPID for shell scripts
action width:  22 characters initially
syslog:        logger -t <tag> -p user.<priority>
default level: INFO
```

`DEBUG` and `TRACE` are development levels only. During watched backup work,
leave `LOG_LEVEL=INFO` unless a separate troubleshooting decision raises it.

Log these actions:

```text
gate accepted or refused
lock acquired and released
run id and run dir selected
free-space check value and result
rsync pass start, complete, rc, and tolerated rc24
Postgres identity check and dump start/complete
metadata, manifest, and status writes
staging promotion
final result or failure status
```

Do not log per-file rsync paths, dump contents, docker logs, approval token
values, full sudo listings, or payload data. Keep raw command output in the
project log stream only; do not pipe the whole stream to syslog.

## Watched Manual Run Signals

When later approved, watch:

```bash
uptime
iostat -xz 5
vmstat 5
pidstat
```

Stop if load or iowait harms the live node.

## Verification Evidence

Collect these before a backup can be marked known-good:

```text
backup run id and timestamp
git status/diff of the tooling used
config hash or captured redacted config
node, nodeworker, collector runtime, and Postgres dump inventory
manifest and SHA256 verification result
PostgreSQL custom restore result
schema-only restore result
isolated node/container start result
isolated nodeworker/container start result
RPC/health probe result
sync/catch-up evidence
fatal log scan result
resource/load observations from the watched run
```
