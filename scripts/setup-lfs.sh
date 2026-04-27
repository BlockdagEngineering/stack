#!/bin/bash
set -euo pipefail

# =============================================================================
# Git LFS Setup Script for Blockdag Pool-Stack
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

log() {
    echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Check if Git LFS is installed
if ! command -v git-lfs &>/dev/null; then
    log "Installing Git LFS..."
    sudo apt-get update && sudo apt-get install -y git-lfs
fi

# Initialize LFS in the project directory
cd "$PROJECT_ROOT"

log "Initializing Git LFS..."
git lfs install

# Track snapshot files
log "Configuring LFS to track snapshot files..."
git lfs track "*.bdsnap"
git lfs track "snapshots/*.bdsnap"

# Add .gitattributes to git
git add .gitattributes 2>/dev/null || true
git commit -m "Add Git LFS configuration for snapshots" --allow-empty 2>/dev/null || true

log ""
log "=============================================="
log "Git LFS Setup Complete!"
log "=============================================="
log "Tracked file patterns:"
git lfs ls-files | head -10 || echo "(No files tracked yet)"
log ""
log "To push LFS objects to remote:"
log "  git push origin main --include-ssh-keys"
log "=============================================="
