#!/usr/bin/env bash

run_low_priority() {
  local command=("$@")
  if command -v ionice >/dev/null 2>&1; then
    command=(ionice -c3 "${command[@]}")
  fi
  if command -v nice >/dev/null 2>&1; then
    command=(nice -n 19 "${command[@]}")
  fi
  "${command[@]}"
}

snapshot_sync_summary() {
  local project_root="$1"
  PYTHONPATH="$project_root/ops" python3 -c '
import os
import urllib.request

from pool_ops import collect_sync_progress, json_rpc_call, node_rpc_urls

progress = collect_sync_progress()
nodes = progress.get("nodes") or {}
remaining = []
unknown = 0
for item in nodes.values():
    if item.get("status") == "unknown":
        unknown += 1
    value = item.get("remaining_blocks")
    if value is not None:
        remaining.append(int(value))
max_remaining = max(remaining) if remaining else -1

def metric_urls():
    configured = os.environ.get(
        "BDAG_NODE_METRICS_URLS",
        "node1=http://127.0.0.1:6061/debug/metrics/prometheus,node2=http://127.0.0.1:6062/debug/metrics/prometheus",
    )
    urls = []
    for item in configured.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            _, url = item.split("=", 1)
        else:
            url = item
        urls.append(url.strip())
    return urls

def scrape_metric(url, names):
    data = urllib.request.urlopen(url, timeout=3.0).read().decode("utf-8", errors="replace")
    values = {}
    for raw in data.splitlines():
        if not raw or raw.startswith("#") or " " not in raw:
            continue
        key, value = raw.split(None, 1)
        if key in names:
            try:
                values[key] = int(float(value.strip()))
            except ValueError:
                pass
    return values

dag_positions = []
for url in metric_urls():
    try:
        metrics = scrape_metric(url, {"Blockdag_mainorder", "chain_head_block"})
    except Exception:
        continue
    # DAG main-order is the best signal for node catch-up on this chain. Falling
    # back to chain_head_block is still better than only checking EVM blockNumber.
    if "Blockdag_mainorder" in metrics:
        dag_positions.append(metrics["Blockdag_mainorder"])
    elif "chain_head_block" in metrics:
        dag_positions.append(metrics["chain_head_block"])

blocks = []
for _, url in node_rpc_urls():
    try:
        value = json_rpc_call(url, "eth_blockNumber", [], timeout=4.0)
        blocks.append(int(str(value), 16))
    except Exception:
        unknown += 1

if len(dag_positions) >= 2:
    block_lag = max(dag_positions) - min(dag_positions)
elif len(blocks) >= 2:
    block_lag = max(blocks) - min(blocks)
else:
    block_lag = -1
print(progress.get("status") or "unknown", max_remaining, unknown, block_lag)
'
}

snapshot_rsync_node() {
  local source_dir="$1"
  local stage_dir="$2"
  local bwlimit="${BDAG_SNAPSHOT_RSYNC_BWLIMIT_KB:-12000}"
  local rsync_args=(
    -a
    --delete-during
    --no-owner
    --no-group
    --chmod=Du+rwx,Dgo+rx,Fu+rw,Fgo+r
    --exclude=/mainnet/LOCK
    --exclude=/mainnet/network.key
    --exclude=/mainnet/peerstore/
    --exclude=/mainnet/keystore/
    --exclude=/mainnet/BdagChain/LOCK
    --exclude=/mainnet/bdageth/nodekey
    --exclude=/mainnet/bdageth/LOCK
    --exclude=/mainnet/bdageth/nodes/
    --exclude=/mainnet/bdageth/blobpool/
    --exclude=/mainnet/bdageth/transactions.rlp
    --exclude=/mainnet/bdageth/chaindata/LOCK
  )
  if [[ "$bwlimit" != "0" ]]; then
    rsync_args+=(--bwlimit="$bwlimit")
  fi

  mkdir -p "$stage_dir"
  run_low_priority rsync "${rsync_args[@]}" "$source_dir"/ "$stage_dir"/
}
