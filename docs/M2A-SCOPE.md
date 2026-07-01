# Gate Scope

Date format: `YYYY/MM/DD`

M2a creates a reviewable project package only.

M2c tightens that package after specialist review. It is still a scaffold-only
stage: no runtime path probing, preflight, dry-run, backup, restore, cleanup,
service lifecycle action, timer, commit, or push is approved.

M2e was a review-cleanup patch.

M3 read-only preflight completed on 2026/06/26 with exit code 0 and this
evidence line:

```text
2026/06/26T12:30:27Z level=INFO run=no-run stage=preflight msg=static\ path\ and\ tool\ checks\ passed
```

This document now records the post-M4 hardening boundary. M4 dry-run is closed.
M5 backup execution remains blocked until a separate approval names the exact
command, write scope, sudo call surface, monitoring, evidence capture, and abort
conditions. On 2026/06/28 Duncan approved repo-only drafting for a
containerized near-hot runner and restore-tested schedule; this does not approve
installing, enabling, running, promoting, committing, pushing, or touching
production services.

## Approved

- Create `/opt/blockdag/eddie-dev/v657-nearhot-backup/`.
- Create project-local `bin/`, `lib/`, `etc/`, `docs/`, and `tests/`.
- Draft scripts, example config, documentation, and static tests.
- Run file-presence, shell syntax, and static checks.
- Review git status and diff.
- For M2c, add unit guard tests for path containment, symlink escapes, latest known-good
  target containment, unknown arguments, runtime-mode refusal, and config overlap.
- For M2c, document controlled sudo and explicit nodeworker inclusion.
- For M2e, if approved, tighten the M2d review findings: collector existence,
  exact backup-id matching, symlink/.. traversal, controlled sudo validation,
  resource ranges, and local-backup-only documentation.
- For post-M3 hardening, edit only reviewed tooling/docs/tests/config examples
  and run shell syntax/static/unit tests.
- For post-M4/M5 preparation, edit only reviewed tooling/docs/tests and run
  shell syntax/static/unit tests plus read-only sudo/dry-run checks.
- For the 2026/06/28 cycle draft, edit only repo files for the containerized
  runner, restore-tested scheduled entrypoint, fail-closed proof checks,
  known-good marking, retention policy, and related docs/tests.

## Not Approved

- No additional preflight execution.
- No backup execution.
- No restore execution.
- No verify execution.
- No cleanup execution.
- No timers or automation.
- No timer install or enablement.
- No service lifecycle action.
- No production lifecycle action.
- No writes to `/usr/local/sbin`, `/usr/local/lib`, or `/etc`.
- No production runtime changes.
- No BlockdagEngineering push.
- No backup payloads, restore payloads, dumps, manifests, identity material, or
  runtime data committed or pushed to any git remote.

## M2c Decisions

- Use controlled sudo for unreadable live node/Docker volume paths.
- Include nodeworker data in Phase 1 backups until restore testing proves it is
  disposable/rebuildable.
- Keep unit/static tests as a required gate for this and future project work.
- Keep backup payloads local to VQueen only; git is for reviewed tooling, docs,
  tests, and examples unless a separate explicit exception is approved.

## Post-M3 Hardening Decisions

- `--backup` refuses in the current gate.
- Public `--backup-inner` dispatch is removed so `BACKUP_INNER_APPROVED=1` cannot
  bypass the outer lock intent.
- `--verify`, restore `--restore`, restore `--verify-only`, and restore
  `--cleanup` validate their target paths and then refuse in the current gate.
- Restore and backup-run selection use one completed-run validator under
  `BACKUP_ROOT/runs`.
- Write roots, log roots, state dirs, locks, restore targets, and future run
  targets must refuse symlink components and live/tooling overlap.
- Controlled sudo future runtime gates refuse broad `NOPASSWD: ALL`.
- The backup tool enforces its own sudo call surface and must not use generic
  sudo shell or generic sudo dispatch paths.
- M5 controlled-sudo rsync reads must go through
  `/usr/local/sbin/vqueen-nearhot-rsync`, with wrapper-side validation of source
  labels, fixed source paths, run-dir shape, derived destinations, and fixed
  rsync arguments.
- PostgreSQL backup execution requires compose project/service/image identity
  validation before any `docker exec`.
- The live Postgres Docker container name is `postgres`; its Compose service
  label is `pool-db`. The tracked example and active config must keep those
  fields distinct.
- The only intended active-config divergence before M4 is
  `RSYNC_BWLIMIT_KB="65536"`.
- M5 first backup requires the explicit one-run approval token
  `VQUEEN_M5_BACKUP_APPROVED="vqueen-v6.5.7-first-backup-2026-06-26"`.
- Logging uses the shared `lib/vqueen-logging.sh` framework. Structured action
  logs use local host time, accepted levels `TRACE DEBUG INFO NOTICE WARNING
  ERROR CRITICAL FATAL`, width-8 levels, `BASHPID`, width-22 action names,
  project log output, and syslog/journald handoff through `logger`.
- The root rsync wrapper sources the root-owned copy at
  `/usr/local/lib/vqueen-nearhot-backup/vqueen-logging.sh` so wrapper refusals
  and validation steps use the same format.

## 2026/06/28 Cycle Draft Decisions

- The only future scheduled entrypoint is `bin/vqueen-nearhot-cycle.sh
  --scheduled-cycle`, reached through
  `ops/vqueen-nearhot-cycle-wrapper.sh`.
- Draft systemd files are renamed to `vqueen-nearhot-cycle.service` and
  `vqueen-nearhot-cycle.timer`; the backup-only service/timer draft is removed.
- The cycle order is fixed:
  `preflight -> container-backup -> manifest-verify -> restore-proof ->
  verify-only -> mark-known-good`.
- The backup runner container must not mount `/var/run/docker.sock`, must not
  run privileged, and must use read-only source binds plus a single backup-root
  write bind.
- Restore proof must fail closed on non-zero `pg_restore`, stderr from custom
  restore, schema-only restore failure, zero table count, missing rc/stderr
  evidence, or manifest verification failure.
- A backup is marked known-good after `verify-only` accepts restore evidence.
- Retention defaults: keep the last 3 known-good backups; keep
  failed/candidate evidence for 14 days.

## Later Git Target

```text
repo:   BlockdagEngineering/stack
branch: refs/heads/eddie-dev/v657-nearhot-backup
```

Any actual push requires a separate read-only preflight and explicit approval.
