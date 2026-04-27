# GitHub LFS Configuration for Large Snapshots

## Setup LFS for Your Repository

```bash
# Install Git LFS
git lfs install

# Track snapshot files
git lfs track "*.bdsnap"
git add .gitattributes

# Commit and push
git commit -m "Add LFS tracking for snapshots"
git push origin main
```

## Workflow Integration

```yaml
- name: Download LFS objects
  run: |
    git lfs pull
```

---

**Note**: For a single 4GB file, GitHub Releases with chunked uploads is typically more practical than LFS.
