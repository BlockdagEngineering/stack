# Blockdag Pool-Stack Snapshot Documentation

## Overview

Snapshots (`.bdsnap`) for pool-stack builds are stored in **Git LFS** and assembled in **GitHub Actions**. CI **reuses** whatever snapshot is already in the repository; it does **not** export a new snapshot from a live node. To ship a **newer** chain baseline in a release, someone must run `export-snapshot.sh` (or an equivalent process), update LFS, and push **before** the release workflow runs.

## Git LFS layout

- **Assembled file:** `snapshots/latest.bdsnap` — CI uses this for the **Docker image build** on the runner. **GitHub Releases do not attach** this file (multi‑GiB snapshots exceed GitHub’s **2GiB-per-asset** limit); download via Git LFS locally or sync from the network; see README release notes.
- **Chunked storage (optional):** `snapshots/lfs-parts/<stem>.000`, `.001`, … — `scripts/ci-assemble-snapshot.sh` joins these into `latest.bdsnap` at the start of CI. After assembly, CI may delete the chunk directory on the runner to save disk space.
- **Single-file workflow:** You can track `snapshots/latest.bdsnap` directly in LFS if you are not using split parts.

## Scripts

### `scripts/export-snapshot.sh`

Exports a `.bdsnap` from a **stopped** node container using `blockdag-node snap export` (needs `blockdag-node` on the host PATH, `bin/blockdag-node` in the repo, or the binary copied from the container image).

```bash
# Defaults: container pool-stack-docker-stack-node-1, output snapshots/exported-snapshot.bdsnap
./scripts/export-snapshot.sh

# Custom container and output (use this to write directly to latest.bdsnap)
./scripts/export-snapshot.sh my-container-name ./snapshots/latest.bdsnap
```

**What it does:**

1. Optionally stops the node container so the datadir is consistent.
2. `docker cp` copies `/var/lib/bdagStack/node/mainnet` from the container.
3. Verifies layout (expects `BdagChain` under the datadir).
4. Runs `blockdag-node snap export` into the output path.
5. On exit, restarts the container if it was running before export.



### `scripts/ci-assemble-snapshot.sh`

Run in CI (and locally if you want the same logic): if `snapshots/latest.bdsnap` already exists and is large enough, it is kept; otherwise chunks under `snapshots/lfs-parts/` are joined into `latest.bdsnap`.

### `scripts/split-snapshot-for-lfs.sh` / `scripts/join-snapshot-parts.sh`

Split a large `.bdsnap` into LFS-friendly chunks or reassemble them (see script headers for usage).

### `scripts/setup-lfs.sh`

Initializes Git LFS tracking for snapshot patterns in this repo.

```bash
./scripts/setup-lfs.sh
```

## What CI does (and does not do)

Workflows such as `build-pool.yml` and `build-cpu.yml`:

1. **Pull LFS** and run `**ci-assemble-snapshot.sh`** to produce `snapshots/latest.bdsnap` when starting from chunks.
2. **Import at build time:** hardlink or copy `latest.bdsnap` into `snapshot-import/snapshot.bdsnap` for the Docker build (when the file is valid).
3. **“Export updated snapshot” step:** This step **only validates** that `snapshots/latest.bdsnap` exists and is at least 1KB. It does **not** run `export-snapshot.sh` and does **not** refresh the snapshot from a synced node.
4. **Release tarball:** Includes `snapshots/latest.bdsnap` when it passes the size check.

So: **you do not need to run `export-snapshot.sh` for every release** if you are fine shipping the snapshot already committed in Git. You **do** need to run it (or otherwise produce a new `.bdsnap`) when you want releases to carry updated chain state.

## Workflow: publish a new snapshot for upcoming releases

### 1. Export locally

```bash
cd pool-stack-docker-stack

./scripts/export-snapshot.sh pool-stack-docker-stack-node-1 ./snapshots/latest.bdsnap
# or export to exported-snapshot.bdsnap then mv/replace after verification
```

### 2. Verify

```bash
ls -lh snapshots/latest.bdsnap   # should be well above 1KB for a real snapshot
# Optional: inspect archive listing if your tooling supports it
tar -tzf snapshots/latest.bdsnap 2>/dev/null | head
```

Ensure the `blockdag-node` version used for export is compatible with the node version in the Docker image you ship.

### 3. LFS: single file or chunks

- **Single file:** `git add snapshots/latest.bdsnap` (LFS will track per `.gitattributes`).
- **Chunked:** run `split-snapshot-for-lfs.sh` on the new snapshot, commit updated `snapshots/lfs-parts/`*, and ensure CI can assemble (see `ci-assemble-snapshot.sh` / `SNAPSHOT_LFS_STEM` if needed).

```bash
git add snapshots/latest.bdsnap   # and/or lfs-parts
git commit -m "Update mainnet snapshot for release"
git push origin main
```

### 4. Cut the release

Tag or run the release workflow as usual. The tarball will contain the `latest.bdsnap` you pushed, not a newly exported one from the runner.

## Architecture (release pipeline)

```
┌─────────────────────────────────────────────────────────────┐
│                    Release workflow                         │
├─────────────────────────────────────────────────────────────┤
│  1. Git LFS pull + ci-assemble-snapshot.sh → latest.bdsnap   │
│  2. Docker build imports snapshot-import/snapshot.bdsnap     │
│  3. Validate latest.bdsnap size (no live export on CI)       │
│  4. Tarball includes snapshots/latest.bdsnap if valid        │
└─────────────────────────────────────────────────────────────┘
```

Live export from a node remains a **maintainer/local** step via `export-snapshot.sh`.

## Manual fallback (if `export-snapshot.sh` fails)

Prefer fixing the script or binary path. Raw `tar` of a datadir is **not** the same as `blockdag-node snap export`; only use ad hoc archives if your ops process explicitly supports them.

```bash
docker stop pool-stack-docker-stack-node-1
# … copy datadir and run blockdag-node snap export by hand, or fix export-snapshot.sh …
docker start pool-stack-docker-stack-node-1
```

## Troubleshooting

### Snapshot import fails with “corrupted” or freezer errors

EVM state / freezer paths can occasionally be inconsistent. Mitigations depend on node version; see internal runbooks. The export script copies the full mainnet datadir and runs the official `snap export` path.

### No snapshot in LFS yet

Bootstrap `snapshots/latest.bdsnap` (or chunks) from a trusted export, commit with Git LFS, then push:

```bash
git lfs push --all origin main
```

## File structure

```
pool-stack-docker-stack/
├── scripts/
│   ├── export-snapshot.sh       # Local export from Docker node → .bdsnap
│   ├── ci-assemble-snapshot.sh # CI/local: chunks → latest.bdsnap
│   ├── join-snapshot-parts.sh
│   ├── split-snapshot-for-lfs.sh
│   └── setup-lfs.sh
├── snapshots/
│   ├── latest.bdsnap            # Assembled / canonical snapshot for builds (LFS)
│   ├── exported-snapshot.bdsnap # Default output of export-snapshot.sh (don’t commit unless intended)
│   └── lfs-parts/               # Optional chunked LFS storage
├── .gitattributes
└── .github/workflows/
    ├── build-cpu.yml
    └── build-pool.yml
```

## Verification checklist (before a release)

- `snapshots/latest.bdsnap` (or LFS chunks) is updated if you need a new chain height.
- Snapshot size is realistic (not a tiny placeholder).
- Export used a `blockdag-node` version compatible with the release image.
- `git lfs pull` works for collaborators and CI.
- Docker build imports the snapshot without errors.
- Release tarball contains `snapshots/latest.bdsnap` when expected.

