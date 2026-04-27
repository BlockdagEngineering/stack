# Blockdag Pool-Stack Snapshot Documentation

## Overview

This repository now includes automated snapshot management using **Git LFS** and **GitHub Actions workflows**.

## What Changed

### 1. Git LFS for Large Files
- Snapshots (`.bdsnap` files) are now tracked via Git LFS
- Located in: `snapshots/` directory
- Current snapshot: `snapshots/latest.bdsnap`

### 2. New Scripts

#### `scripts/export-snapshot.sh`
Exports a fresh snapshot from a running node container.

```bash
# Basic usage (uses default container)
./scripts/export-snapshot.sh

# Custom container and output
./scripts/export-snapshot.sh my-container-name ./snapshots/my-snapshot.bdsnap
```

**What it does:**
1. Stops the node container cleanly (SIGTERM, then SIGKILL if needed)
2. Copies the datadir from the container to a temp location
3. Verifies the data integrity
4. Creates a snapshot using the local `blockdag-node` binary
5. Restarts the container

#### `scripts/setup-lfs.sh`
Initializes Git LFS for tracking snapshot files.

```bash
./scripts/setup-lfs.sh
```

### 3. Updated Workflows

Both workflows now include:

1. **Download snapshot from LFS** at start of build
2. **Import snapshot into node** during container runtime
3. **Export updated snapshot** with new data
4. **Include snapshot in release tarball**

## Workflow for Adding a Snapshot to Release

### Step 1: Export a Fresh Snapshot

On your local machine (with Docker running):

```bash
cd /home/ubuntu/repos/pool-stack-docker-stack

# Run the export script
./scripts/export-snapshot.sh

# This will:
# - Stop the node container
# - Copy and verify data
# - Create snapshots/latest.bdsnap
```

### Step 2: Verify the Export

```bash
# Check snapshot size (should be > 1KB for valid import)
ls -lh snapshots/latest.bdsnap

# Check snapshot contents
./scripts/export-snapshot.sh --info
```

### Step 3: Commit and Push to LFS

```bash
# Add the snapshot to git (LFS will handle it)
git add snapshots/latest.bdsnap
git commit -m "Update snapshot to latest mainnet state"
git push origin main
```

### Step 4: Trigger Release Workflow

The workflow will automatically:
1. Download `snapshots/latest.bdsnap` from LFS
2. Import it into the node during build
3. Export an updated snapshot with any changes
4. Include it in the release tarball

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Release Workflow                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. ───► Download snapshot from GitHub LFS                  │
│         (snapshots/latest.bdsnap)                           │
│                                                             │
│  2. ───► Import into node container                         │
│         /tmp/snapshot-candidate.bdsnap →                    │
│         /var/lib/bdagStack/node/mainnet                     │
│                                                             │
│  3. ───► Export updated snapshot                            │
│         (if changes detected or rebuild needed)             │
│                                                             │
│  4. ───► Include in release tarball                         │
│         blockdag-docker-stack-v1.x.y.tar.gz                 │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Manual Snapshot Creation (If Needed)

```bash
# If the export script fails, you can manually create a snapshot

# 1. Stop the node container
docker stop pool-stack-docker-stack-node-1

# 2. Create tarball of datadir
tar -czf snapshots/manual-snapshot.bdsnap \
  -C /home/ubuntu/repos/mainnet-data .

# 3. Restart the container
docker start pool-stack-docker-stack-node-1

# 4. Commit to LFS
git add snapshots/manual-snapshot.bdsnap
git commit -m "Manual snapshot export"
git push origin main
```

## Troubleshooting

### Snapshot Import Fails with "Corrupted" Error

The EVM state history freezer can sometimes be corrupted. Workaround:

```bash
# Use only the BdagChain database (more reliable)
tar -czf snapshots/clean-snapshot.bdsnap \
  -C /home/ubuntu/repos/mainnet-data BdagChain

# Or exclude the problematic EVM state
tar -czf snapshots/safe-snapshot.bdsnap \
  --exclude='mainnet-data/bdageth/chaindata/ancient/state' \
  -C /home/ubuntu/repos/mainnet-data .
```

### No Snapshot Found in LFS

The first release will need a snapshot uploaded manually:

```bash
# Upload existing snapshot directly to LFS
git lfs push --all origin main
```

## File Structure

```
pool-stack-docker-stack/
├── scripts/
│   ├── export-snapshot.sh    # Main snapshot export script
│   └── setup-lfs.sh          # Git LFS initialization
├── snapshots/
│   └── latest.bdsnap         # Current production snapshot
├── .gitattributes            # LFS file tracking rules
└── .github/workflows/
    ├── build-cpu.yml         # Updated with snapshot logic
    └── build-pool.yml        # Updated with snapshot logic
```

## Verification Checklist

Before cutting a release, verify:

- [ ] `snapshots/latest.bdsnap` exists and is > 1KB
- [ ] Snapshot was created with compatible blockdag-node version
- [ ] `git lfs pull` works (LFS files accessible)
- [ ] Docker build completes without snapshot import errors
- [ ] Release tarball includes the snapshot file
