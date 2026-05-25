#!/bin/sh
# Fix ownership of persisted paths on every container start (named/bind volumes
# are often populated as root → bdagStack cannot open bdageth/chaindata/ancient/*).
set -eu

import_snapshot_if_present() {
  snapshot_enabled="${BDAG_SNAPSHOT_IMPORT_ENABLED:-1}"
  snapshot_path="${BDAG_SNAPSHOT_IMPORT_PATH:-/snapshot/latest.bdsnap}"
  snapshot_min_bytes="${BDAG_SNAPSHOT_MIN_BYTES:-1048576}"
  snapshot_marker="${BDAG_SNAPSHOT_IMPORT_MARKER:-/var/lib/bdagStack/node/.snapshot-imported}"
  node_datadir="${BDAG_NODE_DATADIR:-/var/lib/bdagStack/node/mainnet}"

  if [ "$snapshot_enabled" = "0" ]; then
    echo "Snapshot import disabled; node will use existing data or sync from P2P"
    return 0
  fi

  if [ ! -f "$snapshot_path" ]; then
    echo "No snapshot bind mount at $snapshot_path; node will sync from P2P"
    return 0
  fi

  snapshot_size="$(stat -c%s "$snapshot_path" 2>/dev/null || echo 0)"
  if [ "$snapshot_size" -lt "$snapshot_min_bytes" ]; then
    echo "No valid snapshot at $snapshot_path (${snapshot_size} bytes); node will sync from P2P"
    return 0
  fi

  if [ -f "$snapshot_marker" ]; then
    echo "Snapshot already imported for this node-data volume; skipping import"
    return 0
  fi

  mkdir -p "$node_datadir" "$(dirname "$snapshot_marker")"
  echo "Importing snapshot from $snapshot_path (${snapshot_size} bytes) into $node_datadir"
  /usr/local/bin/blockdag-node snap import \
    --datadir "$node_datadir" \
    --path "$snapshot_path"
  printf 'imported_at=%s\nsnapshot_size=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$snapshot_size" > "$snapshot_marker"
  chown -R bdagStack:bdagStack /var/lib/bdagStack/node /var/log/bdagStack
  echo "Snapshot import finished"
}

if [ "$(id -u)" = 0 ]; then
  mkdir -p /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack
  chown -R bdagStack:bdagStack /var/lib/bdagStack/node /var/lib/bdagStack/nodeworker /var/log/bdagStack || true
  import_snapshot_if_present
  exec runuser -u bdagStack -g bdagStack -- "$@"
fi
exec "$@"
