# v657-nearhot-backup

Host-local VQueen v6.5.7 near-hot backup and dev-restore proof tooling.

This is Astute/OpenClaw side-project tooling. It must not change BlockDAG software,
BlockDAG logging, production compose files, production service lifecycle, or
production runtime behavior.

## Current Gate Boundary

M3 read-only preflight completed on 2026/06/26 with exit code 0:

```text
2026/06/26T12:30:27Z level=INFO run=no-run stage=preflight msg=static\ path\ and\ tool\ checks\ passed
```

M4 dry-run is closed. On 2026/06/28 Duncan approved repo-only drafting for the
containerized near-hot runner and restore-tested schedule. Runtime execution
remains blocked until Duncan separately approves staging/UAT.

Allowed now:

- update this project under `/opt/blockdag/eddie-dev/v657-nearhot-backup/`
- draft and refine scripts, config examples, docs, and tests
- run file-presence, shell syntax, static, and unit guard checks
- inspect git status/diff

Not allowed in the current gate:

- no backup
- no restore
- no verify
- no cleanup
- no timers or automation
- no timer install or enablement
- no service start, stop, or restart
- no production lifecycle action
- no writes to `/usr/local/sbin`, `/usr/local/lib`, or `/etc`
- no push to BlockdagEngineering

## Local Backup Payload Boundary

Backup payloads are local host-only material. They stay on VQueen under the
approved local backup tree and must not be committed, pushed, uploaded, mirrored,
or otherwise sent to GitHub, BlockdagEngineering, or any other remote.

Git may later track only the reviewed tooling package:

- scripts
- docs
- tests
- config examples

Git must not contain backup payloads, restore payloads, chain/node data,
nodeworker data, collector runtime data, Docker volume contents, PostgreSQL
dumps, identity material, runtime metadata bundles, or sensitive manifests unless
that is separately reviewed and approved.

## Project Layout

```text
/opt/blockdag/eddie-dev/v657-nearhot-backup/
  bin/
    vqueen-backup.sh
    vqueen-container-backup.sh
    vqueen-nearhot-cycle.sh
    vqueen-restore-test.sh
  lib/
    vqueen-backup-lib.sh
    vqueen-logging.sh
  ops/
    container/Dockerfile
    systemd/vqueen-nearhot-cycle.service
    systemd/vqueen-nearhot-cycle.timer
    vqueen-nearhot-cycle-wrapper.sh
    vqueen-nearhot-rsync-wrapper.sh
  etc/
    vqueen-backup.conf.example
  docs/
  tests/
```

Runtime logs remain outside git:

```text
/var/log/blockdag/vqueen-backup/
/var/log/blockdag/vqueen-restore-test/
```

The privileged rsync wrapper also sources a root-owned copy of the shared
logging helper:

```text
/usr/local/lib/vqueen-nearhot-backup/vqueen-logging.sh
```

## Logging Framework

Project action logs use the shared Bash logger in `lib/vqueen-logging.sh`.
Every meaningful action should emit a structured line in this format:

```text
YYYY-MM-DD HH:MM:SS: LEVEL    : PID ThreadOrAction         : message
```

Rules:

- timestamp is local host time
- accepted levels are `TRACE`, `DEBUG`, `INFO`, `NOTICE`, `WARNING`, `ERROR`,
  `CRITICAL`, and `FATAL`
- level field width is 8 characters, matching `CRITICAL`
- PID is `BASHPID` for shell scripts
- thread/action field width starts at 22 characters, matching 10 plus the
  longest initial action name from `MainThread`, `RsyncWrapper`, and
  `PostgresDump`
- `LOG_LEVEL` defaults to `INFO`; `DEBUG` and `TRACE` are for development only
- accepted structured lines are emitted to the project log stream and to
  syslog/journald through `logger`
- command output, rsync stats, and tool stderr remain in the project log stream;
  they are not piped wholesale to syslog
- rsync must not enable per-file verbose, progress, itemized, or out-format logs

Backup and restore payloads remain outside git:

```text
/opt/backups/blockchain/vqueen-v6.5.7-nearhot/
/opt/backups/blockchain/dev-restore/
```

## Phase 1 Backup Set

Phase 1 includes these project-owned sources:

- node chain data: `NODE_DATA_SRC`
- nodeworker data: `NODEWORKER_DATA_SRC`
- collector runtime data: `COLLECTOR_RUNTIME_SRC`
- PostgreSQL logical dumps
- runtime metadata needed to prove restore behavior
- identity material while this remains host-local and permission-protected

Nodeworker data remains included until a later restore test proves it is safely
rebuildable/disposable.

## Controlled Sudo Decision

Unreadable live node and Docker volume paths use controlled sudo. The config makes
this explicit with:

```text
LIVE_READ_ACCESS="controlled-sudo"
SUDO_BIN="sudo"
SUDO_FLAGS="-n"
```

Runtime code must keep sudo narrow, logged, and reviewable before any backup gate.
The backup tool enforces its own sudo call surface and must not use generic
sudo shells or generic sudo dispatch. The only approved M4 live sudo probe was
`sudo -n test -d <source>`. M5 rsync sudo usage is routed through the
root-owned `/usr/local/sbin/vqueen-nearhot-rsync` wrapper, which validates source
labels, fixed source paths, approved run-dir shape, derived destination paths,
and fixed rsync arguments.

## Local Config Status

`etc/vqueen-backup.conf` is intentionally ignored and host-local. The tracked
example is a template only. At M3 closeout, the active local config differed from
the example only by:

```text
RSYNC_BWLIMIT_KB="65536"
```

That bandwidth cap is the only intended active-config divergence before M4.
PostgreSQL identity uses both the Docker container name and the Compose service
label:

```text
POSTGRES_CONTAINER="postgres"
POSTGRES_COMPOSE_SERVICE="pool-db"
```

## Later Gate Commands

These commands are documented for later approval gates. Do not run them in the
current gate.

```bash
/opt/blockdag/eddie-dev/v657-nearhot-backup/bin/vqueen-backup.sh --preflight
/opt/blockdag/eddie-dev/v657-nearhot-backup/bin/vqueen-backup.sh --dry-run
/opt/blockdag/eddie-dev/v657-nearhot-backup/bin/vqueen-backup.sh --backup
/opt/blockdag/eddie-dev/v657-nearhot-backup/bin/vqueen-restore-test.sh --preflight --backup latest
/opt/blockdag/eddie-dev/v657-nearhot-backup/bin/vqueen-restore-test.sh --dry-run --backup latest
/opt/blockdag/eddie-dev/v657-nearhot-backup/bin/vqueen-restore-test.sh --restore --backup latest
/opt/blockdag/eddie-dev/v657-nearhot-backup/bin/vqueen-nearhot-cycle.sh --show-plan
/opt/blockdag/eddie-dev/v657-nearhot-backup/bin/vqueen-nearhot-cycle.sh --scheduled-cycle
```

M5 first backup additionally requires this explicit one-run approval token:

```bash
VQUEEN_M5_BACKUP_APPROVED="vqueen-v6.5.7-first-backup-2026-06-26" \
  /opt/blockdag/eddie-dev/v657-nearhot-backup/bin/vqueen-backup.sh --backup
```

The scheduled cycle additionally requires the explicit approval token in the
draft systemd service:

```bash
VQUEEN_NEARHOT_CYCLE_APPROVED="vqueen-v6.5.7-restore-tested-cycle-2026-06-28" \
  /opt/blockdag/eddie-dev/v657-nearhot-backup/bin/vqueen-nearhot-cycle.sh --scheduled-cycle
```

The scheduled cycle is the only future automation entrypoint:

```text
preflight -> container backup -> manifest verify -> restore proof ->
verify-only -> mark known-good
```

The runner container must not mount `/var/run/docker.sock`, must not run
privileged, and must not receive broad host control. Host Docker restore actions
remain in fixed reviewed host wrappers or host orchestration.

## Known-Good Rule

A backup is known-good when the restore proof passes:

- final rsync pass complete
- file manifests and SHA256 checks pass
- PostgreSQL custom dump and schema dump exist
- disposable PostgreSQL 15 restore succeeds
- restore `--verify-only` accepts the restore path

The scheduled cycle records `metadata/known-good.txt`, writes
`metadata/cycle-state.txt` as `known-good`, and then keeps the newest three
known-good backups.
