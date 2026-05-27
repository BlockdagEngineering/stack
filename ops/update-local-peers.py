#!/usr/bin/env python3
"""Discover local BlockDAG node peers and update node-specific addpeer lists."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / "asic-pool" / ".env"
RUNTIME_DIR = Path(os.environ.get("BDAG_RUNTIME_DIR", PROJECT_ROOT / "ops" / "runtime"))
RUNTIME_ENV_FILE = RUNTIME_DIR / "ops.env"
SYNC_COORDINATOR_STATE_FILE = RUNTIME_DIR / "sync-coordinator-state.json"
DEFERRED_APPLY_FILE = RUNTIME_DIR / "local-peers-deferred-apply"
NODE_SPECS = {
    "bdag-miner-node-1": {"port": 8151, "env": "NODE1_PEER_ADDRESSES"},
    "bdag-miner-node-2": {"port": 8152, "env": "NODE2_PEER_ADDRESSES"},
}
PEER_RE = re.compile(r"Node started p2p server.*?/p2p/([A-Za-z0-9]+)")
ADDR_RE = re.compile(r"/ip4/[^,\s]+/tcp/(\d+)/p2p/([A-Za-z0-9]+)")
PEER_RE_FULL = re.compile(r"/ip4/([^/]+)/tcp/(\d+)/p2p/([^,\s]+)")
PEER_LATENCY_TIMEOUT = float(os.environ.get("BDAG_LOCAL_PEER_LATENCY_TIMEOUT", "0.75"))
PEER_LATENCY_WORKERS = int(os.environ.get("BDAG_LOCAL_PEER_LATENCY_WORKERS", "16"))
DASHBOARD_STATUS_URL = os.environ.get("BDAG_DASHBOARD_STATUS_URL", "http://127.0.0.1:8088/api/status")
ACTIVE_MINING_RECENT_SECONDS = int(os.environ.get("BDAG_LOCAL_PEERS_ACTIVE_MINING_RECENT_SECONDS", "300"))


def docker_top_has_bdag_child(output: str) -> bool:
    for line in output.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        command = parts[1]
        if command == "bdag" or command.endswith("/bdag"):
            return True
    return False


def run(command: list[str], timeout: int = 20) -> str:
    proc = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"{command[0]} failed").strip())
    return proc.stdout


def node_process_running(container: str) -> bool:
    try:
        output = run(["docker", "top", container, "-eo", "pid,comm,args"], timeout=10)
    except Exception:
        return False
    return docker_top_has_bdag_child(output)


def wait_for_node(container: str, timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if node_process_running(container):
            return
        time.sleep(3)
    raise RuntimeError(f"{container} did not show a running bdag process within {timeout}s")


def container_running(container: str) -> bool:
    proc = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def stop_inactive_nodes(active_nodes: list[str]) -> None:
    for node in NODE_SPECS:
        if node in active_nodes or not container_running(node):
            continue
        print(f"stopping inactive {node}; not listed in BDAG_NODE_SERVICES")
        run(["docker", "compose", "stop", node], timeout=120)


def read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(errors="replace").splitlines() if path.exists() else []
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return lines, values


def read_env_values(path: Path) -> dict[str, str]:
    _, values = read_env(path)
    return values


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
        output.append("# Local node peer discovery")
        for key in missing:
            output.append(f"{key}={updates[key]}")
    path.write_text("\n".join(output) + "\n")


def write_deferred_apply(reason: str) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    DEFERRED_APPLY_FILE.write_text(reason + "\n")


def clear_deferred_apply() -> None:
    try:
        DEFERRED_APPLY_FILE.unlink()
    except FileNotFoundError:
        pass


def fallback_peer_ids(values: dict[str, str]) -> dict[str, str]:
    by_port = {
        str(spec["port"]): node
        for node, spec in NODE_SPECS.items()
    }
    result: dict[str, str] = {}
    for _, value in values.items():
        for port, peer_id in ADDR_RE.findall(value):
            node = by_port.get(port)
            if node:
                result[node] = peer_id
    return result


def public_peer_addresses(values: dict[str, str]) -> list[str]:
    peers: list[str] = []
    for key in ("BOOTSTRAP_PEER_ADDRESSES", "PEER_ADDRESSES", "NODE1_PEER_ADDRESSES", "NODE2_PEER_ADDRESSES"):
        for peer in values.get(key, "").split(","):
            peer = peer.strip()
            match = PEER_RE_FULL.search(peer)
            if not match:
                continue
            ip = match.group(1)
            if ip.startswith(("10.", "172.", "192.168.")):
                continue
            peers.append(peer)
    return unique_csv(peers).split(",") if peers else []


def peer_tcp_latency(peer: str) -> tuple[bool, float]:
    match = PEER_RE_FULL.search(peer)
    if not match:
        return False, float("inf")
    ip, port_text, _ = match.groups()
    try:
        port = int(port_text)
    except ValueError:
        return False, float("inf")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(PEER_LATENCY_TIMEOUT)
    started = time.monotonic()
    try:
        sock.connect((ip, port))
        return True, (time.monotonic() - started) * 1000
    except OSError:
        return False, float("inf")
    finally:
        sock.close()


def sort_public_peers_by_latency(peers: list[str]) -> list[str]:
    indexed = list(enumerate(peers))
    scores: dict[int, tuple[bool, float]] = {}
    with ThreadPoolExecutor(max_workers=max(1, PEER_LATENCY_WORKERS)) as executor:
        futures = {executor.submit(peer_tcp_latency, peer): index for index, peer in indexed}
        for future in as_completed(futures):
            scores[futures[future]] = future.result()
    indexed.sort(key=lambda item: (0 if scores.get(item[0], (False, float("inf")))[0] else 1, scores.get(item[0], (False, float("inf")))[1], item[0]))
    return [peer for _, peer in indexed]


def latest_peer_id(container: str, fallback: str | None = None) -> str:
    proc = subprocess.run(
        ["docker", "logs", "--tail", "5000", container],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if proc.returncode != 0:
        if fallback:
            return fallback
        raise RuntimeError((proc.stderr or proc.stdout or f"docker logs failed for {container}").strip())
    logs = proc.stdout + proc.stderr
    matches = PEER_RE.findall(logs)
    if not matches:
        if fallback:
            return fallback
        raise RuntimeError(f"could not find local peer ID in recent logs for {container}")
    return matches[-1]


def local_ipv4_addresses() -> list[str]:
    try:
        output = run(["hostname", "-I"], timeout=5)
    except Exception:
        output = ""
    result: list[str] = []
    for token in output.split():
        try:
            ip = ipaddress.ip_address(token)
        except ValueError:
            continue
        if ip.version != 4 or ip.is_loopback or ip.is_link_local:
            continue
        result.append(str(ip))
    return result


def choose_local_ip(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    addresses = local_ipv4_addresses()
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_private:
            return address
    if addresses:
        return addresses[0]
    raise RuntimeError("could not determine a host IPv4 address for local P2P")


def unique_csv(items: list[str]) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        item = item.strip()
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return ",".join(result)


def csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def update_value_changed(key: str, current: str | None, new: str) -> bool:
    if key.endswith("_PEER_ADDRESSES") or key == "LOCAL_PEER_ADDRESSES":
        return csv_set(current or "") != csv_set(new)
    return (current or "") != new


def env_enabled(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def safe_int(value: object, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def fetch_dashboard_status() -> dict[str, object]:
    request = urllib.request.Request(DASHBOARD_STATUS_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=3) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    return payload if isinstance(payload, dict) else {}


def active_mining_recreate_guard_reason() -> str:
    if not env_enabled("BDAG_LOCAL_PEERS_DEFER_NODE_RECREATE_WHILE_MINING", True):
        return ""
    try:
        status = fetch_dashboard_status()
    except Exception:
        return ""
    pool = status.get("pool") if isinstance(status.get("pool"), dict) else {}
    active_connections = safe_int(pool.get("metrics_active_connections"), 0)
    recent_share_age = safe_int(pool.get("last_valid_share_age_seconds"), 999999)
    recent_submit_age = safe_int(pool.get("last_submit_age_seconds"), 999999)
    recent_work = min(recent_share_age, recent_submit_age) <= ACTIVE_MINING_RECENT_SECONDS
    if active_connections <= 0 or not (status.get("can_accept_shares") or status.get("can_mine") or recent_work):
        return ""
    return (
        f"active mining detected: {active_connections} stratum connection(s), "
        f"last_valid_share_age_seconds={recent_share_age}, "
        f"last_submit_age_seconds={recent_submit_age}"
    )


def planned_paused_follower() -> str:
    try:
        state = json.loads(SYNC_COORDINATOR_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(state, dict):
        return ""
    paused = str(state.get("paused_follower") or "")
    if state.get("mode") == "leader_catchup" and paused in NODE_SPECS:
        return paused
    return ""


def configured_active_nodes(pool_values: dict[str, str]) -> list[str]:
    runtime_values = read_env_values(RUNTIME_ENV_FILE)
    raw = (
        os.environ.get("BDAG_NODE_SERVICES")
        or runtime_values.get("BDAG_NODE_SERVICES")
        or pool_values.get("BDAG_NODE_SERVICES")
        or ""
    )
    nodes = [item.strip() for item in raw.split(",") if item.strip()]
    active = [node for node in nodes if node in NODE_SPECS]
    return active or list(NODE_SPECS)


def split_public_peers(public_peers: list[str]) -> tuple[list[str], list[str]]:
    node1_peers: list[str] = []
    node2_peers: list[str] = []
    for index, peer in enumerate(public_peers):
        if index % 2 == 0:
            node1_peers.append(peer)
        else:
            node2_peers.append(peer)
    return node1_peers, node2_peers


def public_peer_assignment(public_peers: list[str], paused: str, active_nodes: list[str]) -> tuple[list[str], list[str]]:
    if len(active_nodes) == 1:
        if active_nodes[0] == "bdag-miner-node-1":
            return list(public_peers), []
        if active_nodes[0] == "bdag-miner-node-2":
            return [], list(public_peers)
    if paused == "bdag-miner-node-1":
        return [], list(public_peers)
    if paused == "bdag-miner-node-2":
        return list(public_peers), []
    return split_public_peers(public_peers)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-ip", help="Host/LAN IPv4 address reachable from both node containers")
    parser.add_argument("--apply", action="store_true", help="Restart node containers sequentially if peer lists changed")
    parser.add_argument("--force-apply", action="store_true", help="Restart node containers sequentially even if peer lists did not change")
    args = parser.parse_args()

    lines, values = read_env(ENV_FILE)
    active_nodes = configured_active_nodes(values)
    public_peers = sort_public_peers_by_latency(public_peer_addresses(values))
    paused = planned_paused_follower()
    node1_public_peers, node2_public_peers = public_peer_assignment(public_peers, paused, active_nodes)
    host_ip = choose_local_ip(args.host_ip)

    fallback_peers = fallback_peer_ids(values)
    peers: dict[str, str] = {}
    for node in active_nodes:
        peers[node] = latest_peer_id(node, fallback=fallback_peers.get(node))

    local_addrs = {
        node: f"/ip4/{host_ip}/tcp/{spec['port']}/p2p/{peers[node]}"
        for node, spec in NODE_SPECS.items()
        if node in peers
    }
    updates: dict[str, str] = {}
    if "bdag-miner-node-1" in active_nodes:
        node1_peers = list(node1_public_peers)
        if "bdag-miner-node-2" in active_nodes and paused != "bdag-miner-node-2" and "bdag-miner-node-2" in local_addrs:
            node1_peers.append(local_addrs["bdag-miner-node-2"])
        updates["NODE1_PEER_ADDRESSES"] = unique_csv(node1_peers)
    if "bdag-miner-node-2" in active_nodes:
        node2_peers = list(node2_public_peers)
        if "bdag-miner-node-1" in active_nodes and paused != "bdag-miner-node-1" and "bdag-miner-node-1" in local_addrs:
            node2_peers.append(local_addrs["bdag-miner-node-1"])
        updates["NODE2_PEER_ADDRESSES"] = unique_csv(node2_peers)
    if local_addrs:
        updates["LOCAL_PEER_ADDRESSES"] = unique_csv([local_addrs[node] for node in active_nodes if node in local_addrs])

    changed = any(update_value_changed(key, values.get(key), value) for key, value in updates.items())
    if changed:
        write_env(ENV_FILE, lines, updates)
        print(f"updated {ENV_FILE}")
    else:
        print("local peer configuration already current")
    print(f"host_ip={host_ip}")
    print(f"active_nodes={','.join(active_nodes)}")
    print(f"public_peers={len(public_peers)} node1_public_peers={len(node1_public_peers)} node2_public_peers={len(node2_public_peers)} paused_follower={paused or 'none'}")
    for node, addr in local_addrs.items():
        print(f"{node}={addr}")

    if paused and args.apply and not args.force_apply:
        write_deferred_apply(f"sync coordinator paused {paused}; apply local peers after leader catch-up")
        print(f"deferring container recreation while {paused} is paused for leader catch-up")
        return 0

    apply_needed = args.force_apply or (args.apply and (changed or DEFERRED_APPLY_FILE.exists()))
    if args.apply and not args.force_apply and apply_needed:
        guard_reason = active_mining_recreate_guard_reason()
        if guard_reason:
            write_deferred_apply(guard_reason)
            print(f"deferring container recreation: {guard_reason}")
            return 0
    if args.apply or args.force_apply:
        stop_inactive_nodes(active_nodes)
    if len(active_nodes) == 1 and args.apply and not args.force_apply and apply_needed:
        write_deferred_apply("single active node mode; peer config updated without recreating the only production node")
        print("not recreating active node automatically in single-node mode; use --force-apply for an explicit restart")
        return 0
    if apply_needed:
        paused = planned_paused_follower()
        for node in active_nodes:
            if node == paused:
                print(f"skipping {node}; sync coordinator has it paused for leader catch-up")
                continue
            print(f"recreating {node} to apply local peers")
            run([
                "docker",
                "compose",
                "--env-file",
                str(ENV_FILE),
                "-f",
                str(PROJECT_ROOT / "docker-compose.yml"),
                "up",
                "-d",
                "--force-recreate",
                "--no-deps",
                node,
            ], timeout=120)
            wait_for_node(node)
        clear_deferred_apply()
    return 0


if __name__ == "__main__":
    sys.exit(main())
