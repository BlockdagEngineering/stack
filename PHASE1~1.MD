# pool-stack-docker `cpu-v1.3.3` — Bug & Fix Report

**To:** BlockDAG core engineering team
**From:** Dawie Nel — CIO/CTO, DAG Tech Solution (Ras al-Khaimah, UAE)
**Found during:** dry-run rehearsal for the BlockDAG community AMA, 30 April 2026
**Audience this affected:** ~60 000 community members about to follow these instructions live

## TL;DR

Three blocker-class issues found in the `pool-stack-docker-cpu-v1.3.3` release. Two prevent a successful `docker compose build` against the snapshot the same release tells operators to download. One of them is a **release-engineering coupling problem** (binary version vs snapshot format version), the other is a **trivial CLI argument-order bug** in the supplied `dockerfile-cpu-release`. Plus one documentation bug that misleads operators about the pool's HTTP API surface, one cosmetic startup warning, and one node.conf parser gotcha worth calling out for safety.

Patches are included where the fix is unambiguous, plus a working click-through demo kit so anyone reproducing this can confirm in <10 minutes.

---

## Environment & artifact identity

| Item                          | Value                                                                  |
| ----------------------------- | ---------------------------------------------------------------------- |
| Release tarball               | `pool-stack-docker-cpu-v1.3.3.tar.gz`                                  |
| Tarball SHA-256               | `32C87BD59328A4B1681607131D976854551BC4E6FEC244DEB1DE81B09D1F676E`     |
| `bin/blockdag-node`           | SHA-256 `b21615ae63379559208ebcd0922d1dcb500e58299e7a367c5650c79104580168` |
| `bin/blockdag-node` `--version` | `BDAG version 1.0.0` (Go 1.22.12)                                    |
| `bin/nodeworker`              | SHA-256 `84b50d4c079f221d7e52e3ee17555b73bc0c45bac1548ec208d828d9419d7af8` |
| `bin/mining-pool`             | SHA-256 `e00f1a828f3f675ab2a40aa932ad5c6cc9b61a2c72081f6c8dd29e6a0ff3f39a` |
| `bin/cpu-miner`               | SHA-256 `8bca94425fd65f577fc1c599390ab53c255e4413d86fe51c8fae3c016e40652a` |
| Snapshot tested               | `latest.bdsnap`, 4 391 816 709 bytes (4.39 GB), reported format version 4 |
| Build host                    | Windows 11, Docker Desktop 4.65, engine 29.2.1, Compose v5.1.0, WSL2 (Ubuntu 24.04 base) |
| Compose project               | `pool-stack-docker-cpu-v133`                                           |
| Date confirmed                | 2026-04-30                                                             |

All four binary checksums in `checksums.txt` matched on extraction (no tarball corruption).

---

## Bug 1 — `dockerfile-cpu-release` flag-position bug **(BLOCKER)**

**Severity:** Build always fails when `SNAPSHOT_PATH` points at a real `.bdsnap`.
**File:** `dockerfile-cpu-release`, lines **30–41** (the `RUN` block that runs `blockdag-node snap import`).

### What goes wrong

The Dockerfile invokes the CLI as:

```
/usr/local/bin/blockdag-node snap import \
    --datadir /var/lib/bdagStack/node/mainnet \
    --path /tmp/snapshot-candidate.bdsnap;
```

`blockdag-node` uses a urfave/cli-style command tree where `--datadir` is a **global** flag and `snap import` is a subcommand. Global flags must appear **before** the subcommand. Here `--datadir` is being consumed by the `snap import` subcommand parser, which doesn't define it, so the build aborts with:

```
[ERROR] flag provided but not defined: -datadir
ERROR: process "/bin/sh -c set -eu;     if [ \"$(stat -c%s /tmp/snapshot-candidate.bdsnap)\" -ge 1024 ]; then …
       /usr/local/bin/blockdag-node snap import         --datadir /var/lib/bdagStack/node/mainnet …" did not complete successfully: exit code: 1
```

### Reproduction

```bash
# In Ubuntu 24.04 (matches release runtime):
./blockdag-node snap import --help          # confirms only --path / --no-network-check are recognised here
./blockdag-node --help | grep -E -- '--data|--app|--config|--home'
# Output:
#   --appdata value, -A value     (default: ".")
#   --configfile value, -C value  (default: ".conf")
#   --datadir value, -b value     (default: "data")
```

`--datadir` is a global flag — short form `-b`. It's available; it's just being passed in the wrong position.

### Fix (verified working)

Move `--datadir` ahead of the subcommand:

```diff
       /usr/local/bin/blockdag-node \
-        snap import \
         --datadir /var/lib/bdagStack/node/mainnet \
+        snap import \
         --path /tmp/snapshot-candidate.bdsnap;
```

Final RUN block after fix (whitespace-faithful):

```dockerfile
RUN set -eu; \
    if [ "$(stat -c%s /tmp/snapshot-candidate.bdsnap)" -ge 1024 ]; then \
      echo "Importing local snapshot ($(stat -c%s /tmp/snapshot-candidate.bdsnap) bytes)"; \
      /usr/local/bin/blockdag-node \
        --datadir /var/lib/bdagStack/node/mainnet \
        snap import \
        --path /tmp/snapshot-candidate.bdsnap; \
      chown -R bdagStack:bdagStack /var/lib/bdagStack/node /var/log/bdagStack; \
      echo "Snapshot import finished"; \
    else \
      echo "No snapshot file; node will sync from genesis or P2P"; \
    fi; \
    rm -f /tmp/snapshot-candidate.bdsnap
```

### Verification

After applying the patch and re-running `docker compose build`, the binary's argument parser accepts the invocation. (It then immediately fails on Bug 2 — the snapshot itself — but the dockerfile is now correct.)

### Note for `dockerfile-pool-release`

`dockerfile-pool-release` uses the same `RUN` template. Audit it; if it ships the same flag order, it's affected by the same bug.

---

## Bug 2 — Snapshot format / binary version skew **(BLOCKER)**

**Severity:** The published snapshot can never be imported by the binary in the same release tarball.
**Files affected:** every `cpu-v*` and `pool-v*` tarball where `bin/blockdag-node` is at the v1.0.0 line and the linked `latest.bdsnap` is at format ≥ 4.

### What goes wrong

```
$ blockdag-node snap info --path snapshots/latest.bdsnap
2026-04-30|13:52:51.114 [INFO ] Active network: mainnet (from flags)
2026-04-30|13:52:51.118 [ERROR] snapshot: unsupported format version 4
```

The shipped binary identifies itself as `BDAG version 1.0.0`. Its `snap import` and `snap info` commands reject the manifest as an unsupported format version. We can't even *peek* at the manifest — the version check fails before any data is read. Skipping import (`SNAPSHOT_PATH=docker/no-snapshot.marker`) lets the build succeed but defeats the entire purpose of the published snapshot.

### Why this matters operationally

The advertised path in `SNAPSHOT-README.md` is "download the snapshot, set `SNAPSHOT_PATH`, build". For a community of 60 000 expecting fast bootstrap, every operator following the README to the letter currently:

1. Downloads 4.39 GB of `latest.bdsnap`,
2. Burns 6–8 minutes of build time pushing it through the daemon,
3. Hits `unsupported format version 4` on the very last build step,
4. Has to either find a v3 snapshot or skip snapshot entirely,
5. If they skip, they sync from genesis at maybe 7k blocks/min — so a chain at 600k tip is ~85 minutes of patience.

### Fixes — pick one, both work

**Option A — Re-export `snapshots/latest.bdsnap` with the v1.0.0 binary**
Run `scripts/export-snapshot.sh` (which already exists) using the `bin/blockdag-node` shipped in this release, then push the resulting v3 snapshot to LFS. This is the cheapest fix; no code change.

**Option B — Ship a newer `bin/blockdag-node`**
If format v4 is now the long-term standard, the v1.0.0 binary is the wrong artifact for `cpu-v1.3.3`. Bump the binary in `bin/` to the version that supports v4, retag the release.

Either way, the **invariant the release-engineering pipeline must enforce** is: at release time, run `bin/blockdag-node snap info --path snapshots/latest.bdsnap` against the artifacts in the same tarball, and fail the build if it errors. Right now `SNAPSHOT-README.md` (Section "What CI does (and does not do)" → step 3) explicitly says CI only validates the snapshot exists and is ≥ 1 KiB — that's how this slipped through.

### Recommended CI gate

```bash
# In .github/workflows/build-cpu.yml after assembling latest.bdsnap:
./bin/blockdag-node snap info --path snapshots/latest.bdsnap
# fail the workflow if non-zero exit
```

A 5-line CI step would have caught this before tagging.

---

## Bug 3 — `README.md` advertises a pool stats API that this build does not serve **(DOC)**

**Severity:** Cosmetic on day one, embarrassing in front of an audience.
**File:** `README.md`, "Once everything is running" section (~ line 80).

### What goes wrong

README states:

> Pool stats API: <http://localhost:8080/stats>

In this release, the `mining-pool` binary opens **only**:

- Stratum on `:3334` (logged: `Stratum server is listening on 0.0.0.0:3334`),
- An internal Prometheus on `:9090` (logged: `Prometheus metrics server listening on :9090/metrics`).

The compose file maps host `:8080 → container :8080`, but nothing inside the container listens on it. Curling `http://localhost:8080/stats` returns connection-refused / aborted. Independently confirmed via `docker compose exec pool ss -ltnp` shows no listener on 8080.

### Fix

Pick one:

- **Code fix:** Implement an HTTP API server in `mining-pool` that serves `/stats` on `:8080`. The `exporter` already exposes `bdag_pool_*` metrics by scraping pool internals — but those flow through the exporter, not directly from pool. A pool HTTP API is a sensible addition.
- **Doc fix:** Drop the `8080/stats` line from `README.md` and replace with the canonical surface that does work:

  ```
  Exporter metrics:    http://localhost:9198/metrics      (bdag_pool_* and bdag_node_* gauges)
  Node-native metrics: http://localhost:6060/metrics
  Netdata dashboard:   http://localhost:19999/bdagstack.html
  ```

  Either remove the `:8080` host port-mapping from `docker-compose.yml` (services: pool: ports:) **OR** keep the mapping but mark it `# reserved for future pool HTTP API`.

The current state — port mapped on the host, README pointing operators at it, no listener inside — is the worst of all worlds for diagnostics.

---

## Bug 4 — Cosmetic startup warning in pool container **(LOW)**

**Severity:** Confusing log noise on startup; nothing is actually broken.
**File:** `bin/mining-pool` startup behavior; `dockerfile-cpu-release` does not stage a `.env` into the image.

### What goes wrong

First lines of the pool container's logs every time:

```
2026/04/30 14:05:41 load env: open .env: no such file or directory
2026/04/30 14:05:41 Note: WALLET_RPC_URL not set, checking RPC_URL or defaulting
```

Pool actually receives all its config via Compose `environment:` injection (NODE_RPC_URL, PG_URL, POOL_BIND_ADDR, …) — those work fine; the rest of the boot proceeds normally. But the binary's first action is to try to read `./.env` from the container's WORKDIR, which doesn't exist in the release Dockerfile (release builds explicitly do not embed `.env`).

### Fix

In `mining-pool`'s env loader, if `.env` is missing, log at DEBUG level (or silently fall back to process env). Currently it logs at INFO/error level which scares operators who think they've misconfigured something on first boot. Two-line change in `cmd/pool/main.go` (or wherever `godotenv.Load()` is called).

---

## Bug 5 — `node.conf` `modules=` parsing footgun **(GOTCHA — already correct in example, worth hardening)**

**Severity:** Silent loss of functionality if an operator condenses two lines into one.
**File:** `node.conf.example` already shows the correct form (separate lines), but the parser's behavior is dangerous for novices.

### What goes wrong

If an operator writes:

```
modules=Blockdag,miner
```

The parser treats the entire string `Blockdag,miner` as a **single module name** (which is unrecognised). The result is RPC's `getBlockTemplate` and `getNodeInfo` returning JSON-RPC error code `-32601` ("Method not found"), and the pool can never get work. Not obvious from the symptoms.

The example is correct:

```
modules=Blockdag
modules=miner
```

But operators copying from any condensed wiki/example will silently break their pool.

### Fix

Make the parser tolerant — split `modules=...` on `,` and treat each token as a module name. Keep the repeat-line form too. Two lines in the config loader. Backwards compatible.

If you don't want to change the parser, at least add a one-line `WARN` log when a `modules=` value contains a comma:

```
WARN: modules='Blockdag,miner' looks comma-separated; this parser treats it as a single module name. Use separate 'modules=' lines.
```

---

## Bug 6 — Windows install gotcha (your installer should know about this) **(ENVIRONMENTAL)**

**Severity:** Affects new Windows operators on first launch.
**Not your code, but worth documenting in the install guide.**

### What goes wrong

If Docker Desktop is launched from a process whose environment has `%ProgramData%` stripped (some embedded shells, some PowerShell-MCP wrappers, some CI runners), `com.docker.backend.exe` crashes on init with:

```
backend crashed, dumping error to file and reporting to user:
initializing backend: initializing settings loader and loading startup providers:
initializing settings: getting default data folder: unable to get 'ProgramData'
initializing engine paths: retrieving VM directory path: unable to get 'ProgramData'
initializing app: getting VM directory: unable to get 'ProgramData'
```

Cause: Docker Desktop reads the data folder via `os.Getenv("ProgramData")` rather than falling back to the Machine-scope env or the registry value at `HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment\ProgramData`.

### Workaround until fixed (already in our install scripts)

```cmd
if "%ProgramData%"=="" set ProgramData=C:\ProgramData
if "%ALLUSERSPROFILE%"=="" set ALLUSERSPROFILE=C:\ProgramData
```

Set those before launching Docker Desktop. Or always launch via Explorer's normal click path (which inherits explorer.exe's full env).

### Suggested fix in Docker Desktop

Defensive read order: env → Machine env via Win32 API → registry → hard-coded `C:\ProgramData`. This is more a Docker bug than a BlockDAG bug, but please note it in the operator install docs so the BlockDAG community doesn't generate 60 000 GitHub issues against `pool-stack-docker` for what is actually Docker Desktop being fragile.

---

## Working repro & demo kit (proof we ran it end-to-end)

The dry-run brought all six services up, watched the node sync from genesis past block 70 000 in ~10 minutes (peer count 18, no snapshot), saw `getBlockTemplate` return `Blockdag is downloading blocks... : Client in initial download` until sync would complete, and observed an external miner authorise successfully against the pool's Stratum endpoint.

Everything below is in `BlockDAG-Pool-Node-AMA-Demo/` and ready to be repackaged for community distribution once Bugs 1 and 2 are fixed.

### Click-through scripts (Windows; Linux/macOS equivalents are Phase 2)

| File                       | Purpose                                                      |
| -------------------------- | ------------------------------------------------------------ |
| `00_RUN_THIS.cmd`          | Idempotent boot: wakes Docker if asleep, `compose up -d`     |
| `01_TAIL_NODE_LOGS.cmd`    | `docker compose logs -f node` — the audience-friendly window |
| `02_OPEN_DASHBOARDS.cmd`   | Opens Netdata + exporter metrics + prints live `getNodeInfo` |
| `03_TEAR_DOWN.cmd`         | `compose down`, keeps volumes (chain data preserved)         |
| `03b_TEAR_DOWN_TOTAL.cmd`  | `compose down -v` — full wipe, including chain data          |

### Files preserved alongside

```
files/
  dockerfile-cpu-release.orig       # original buggy Dockerfile
  dockerfile-cpu-release.patched    # patched Dockerfile (Bug 1 fix applied)
  .env.demo                         # the env we used (snapshot skipped, strong POSTGRES_PASSWORD)
  node.conf.demo                    # the node config we used (defaults from your example)

evidence/
  build.log     / build.err         # 1st build attempt — caught Bug 1
  build2.log    / build2.err        # 2nd build attempt — succeeded after patch + skip-snapshot
  stack-up.txt                      # docker compose ps showing all six services Up
  live-node-info.json               # real getNodeInfo response with version 1.0.0, network mainnet
  exporter-metrics.txt              # bdag_node_block_height, bdag_node_peer_count, bdag_node_is_synced
```

---

## What we'd like from you

1. **Confirm Bug 1 patch** — happy to PR `dockerfile-cpu-release` (and `-pool-release` if affected).
2. **Pick a fix path for Bug 2** — re-export the snapshot, or bump the binary. Either way, please add the `snap info` CI gate so this can never reach a release again.
3. **Acknowledge Bug 3** — if the pool HTTP API is on the roadmap, a one-line README note ("coming soon, currently not served") would prevent ~50 % of the AMA-day "why doesn't this URL work" questions. Otherwise just remove the line.
4. **Bug 4 + 5** at your leisure — both are quality-of-life.
5. **Bug 6** — please add a "if Docker Desktop crashes with `unable to get 'ProgramData'`, do X" note to the Windows install section. Saves your support team future tickets.

I'll have a Phase 2 cross-platform installer (Windows / Linux / macOS, with prereq detection, interactive setup, install-complete notification) ready to push to the community git **as soon as the binary or snapshot is fixed**. Aiming to give the community a *single* clickable install path, not the current "follow this README and hit a wall on step 5" experience.

— Dawie

*"Cautiously optimistic about AI. Less cautious about release pipelines that don't run their own examples end-to-end."*
