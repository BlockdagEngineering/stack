#!/usr/bin/env python3
"""Prepare disjoint public peer sets for 2/3/4 BlockDAG node experiments."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / "asic-pool" / ".env"
DEFAULT_PORTS = {1: 8151, 2: 8152, 3: 8153, 4: 8154}
PEER_ID_RE = re.compile(r"/ip4/([^/]+)/tcp/(\d+)/p2p/([^,\s]+)")


def read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return lines, values


def write_env(path: Path, lines: list[str], updates: dict[str, str]) -> None:
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                output.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        output.append(line)
    missing = [key for key in updates if key not in seen]
    if missing:
        if output and output[-1].strip():
            output.append("")
        output.append("# Multi-node peer-set experiment")
        for key in missing:
            output.append(f"{key}={updates[key]}")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        item = item.strip()
        if not item or item in seen:
            continue
        result.append(item)
        seen.add(item)
    return result


def public_peers(values: dict[str, str]) -> list[str]:
    peers = [item for item in values.get("PEER_ADDRESSES", "").split(",") if item.strip()]
    if peers:
        return unique(peers)
    # Fallback to existing node-specific public peers if PEER_ADDRESSES is not present.
    result: list[str] = []
    for key, value in values.items():
        if not key.startswith("NODE") or not key.endswith("_PEER_ADDRESSES"):
            continue
        for peer in value.split(","):
            match = PEER_ID_RE.search(peer)
            if not match:
                continue
            ip, _, _ = match.groups()
            if ip.startswith("192.168.") or ip.startswith("172."):
                continue
            result.append(peer)
    return unique(result)


def existing_local_peer_addrs(values: dict[str, str]) -> dict[int, str]:
    by_node: dict[int, str] = {}
    candidates: list[str] = []
    for key in ("LOCAL_PEER_ADDRESSES", "NODE1_PEER_ADDRESSES", "NODE2_PEER_ADDRESSES"):
        candidates.extend(item for item in values.get(key, "").split(",") if item.strip())
    for peer in candidates:
        match = PEER_ID_RE.search(peer)
        if not match:
            continue
        ip, port_text, _ = match.groups()
        if not (ip.startswith("192.168.") or ip.startswith("172.")):
            continue
        try:
            port = int(port_text)
        except ValueError:
            continue
        for node_index, node_port in DEFAULT_PORTS.items():
            if node_port == port:
                by_node[node_index] = peer
    return by_node


def split_peer_sets(peers: list[str], node_count: int) -> dict[int, list[str]]:
    sets = {index: [] for index in range(1, node_count + 1)}
    for index, peer in enumerate(peers):
        node_index = index % node_count + 1
        sets[node_index].append(peer)
    return sets


def build_updates(values: dict[str, str], node_count: int, include_known_local: bool) -> dict[str, str]:
    peers = public_peers(values)
    if len(peers) < node_count:
        raise RuntimeError(f"only {len(peers)} public peers available for {node_count} nodes")
    split = split_peer_sets(peers, node_count)
    local_addrs = existing_local_peer_addrs(values) if include_known_local else {}
    updates: dict[str, str] = {}
    for node_index in range(1, node_count + 1):
        node_peers = list(split[node_index])
        for other_index in range(1, node_count + 1):
            if other_index == node_index:
                continue
            if other_index in local_addrs:
                node_peers.append(local_addrs[other_index])
        updates[f"NODE{node_index}_PEER_ADDRESSES"] = ",".join(unique(node_peers))
    known_locals = [addr for index, addr in sorted(local_addrs.items()) if index <= node_count]
    if known_locals:
        updates["LOCAL_PEER_ADDRESSES"] = ",".join(unique(known_locals))
    return updates


def summarize(updates: dict[str, str]) -> None:
    peer_id_sets: dict[str, set[str]] = {}
    for key in sorted(updates):
        if not key.startswith("NODE") or not key.endswith("_PEER_ADDRESSES"):
            continue
        peers = [item for item in updates[key].split(",") if item.strip()]
        public = []
        local = []
        ids: set[str] = set()
        for peer in peers:
            match = PEER_ID_RE.search(peer)
            if match:
                ip, _, peer_id = match.groups()
                ids.add(peer_id)
                if ip.startswith("192.168.") or ip.startswith("172."):
                    local.append(peer)
                else:
                    public.append(peer)
        peer_id_sets[key] = ids
        print(f"{key}: total={len(peers)} public={len(public)} local={len(local)}")
    keys = sorted(peer_id_sets)
    for index, left in enumerate(keys):
        for right in keys[index + 1 :]:
            print(f"overlap {left} vs {right}: {len(peer_id_sets[left] & peer_id_sets[right])}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nodes", type=int, choices=(2, 3, 4), required=True)
    parser.add_argument("--env-file", type=Path, default=ENV_FILE)
    parser.add_argument("--write-env", action="store_true")
    parser.add_argument(
        "--only-node",
        type=int,
        choices=(1, 2, 3, 4),
        help="Only print or write the peer variable for one node. Useful for adding observer nodes without changing live node1/node2 settings.",
    )
    parser.add_argument("--print-values", action="store_true", help="Print KEY=value peer assignments after the summary")
    parser.add_argument("--include-known-local", action="store_true", help="Include known local peer addresses from the existing env file")
    args = parser.parse_args()

    if args.only_node and args.only_node > args.nodes:
        parser.error("--only-node cannot be greater than --nodes")

    lines, values = read_env(args.env_file)
    updates = build_updates(values, args.nodes, args.include_known_local)
    if args.only_node:
        updates = {f"NODE{args.only_node}_PEER_ADDRESSES": updates[f"NODE{args.only_node}_PEER_ADDRESSES"]}
    summarize(updates)
    if args.print_values:
        for key in sorted(updates):
            print(f"{key}={updates[key]}")
    if not args.write_env:
        print("dry_run=true")
        return 0

    backup = args.env_file.with_name(f"{args.env_file.name}.before-multinode-peers-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(args.env_file, backup)
    write_env(args.env_file, lines, updates)
    print(f"backup={backup}")
    print(f"updated={args.env_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
