#!/usr/bin/env python3
"""Discover local BlockDAG node peers and update node-specific addpeer lists."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / "asic-pool" / ".env"
ROOT_ENV_FILE = PROJECT_ROOT / ".env"
NODE_SPECS = {
    "bdag-miner-node-1": {"port": 8151, "env": "NODE1_PEER_ADDRESSES"},
    "bdag-miner-node-2": {"port": 8152, "env": "NODE2_PEER_ADDRESSES"},
}
PEER_RE = re.compile(r"Node started p2p server.*?/p2p/([A-Za-z0-9]+)")
ADDR_RE = re.compile(r"/(?:ip4|dns4|dns)/([^/\s,]+)/tcp/(\d+)/p2p/([A-Za-z0-9]+)")
IP4_ADDR_RE = re.compile(r"/ip4/([^/\s,]+)/tcp/(\d+)/p2p/([A-Za-z0-9]+)")
LOG_MULTIADDR_RE = re.compile(r"multiAddr:(/[^,\s]+/p2p/[A-Za-z0-9]+)")
EXTRA_PEER_KEYS = (
    "LAN_PEER_ADDRESSES",
    "VPN_PEER_ADDRESSES",
    "ZEROTIER_PEER_ADDRESSES",
    "EXTRA_PEER_ADDRESSES",
    "DISCOVERED_LAN_PEER_ADDRESSES",
    "DISCOVERED_VPN_PEER_ADDRESSES",
    "DISCOVERED_ZEROTIER_PEER_ADDRESSES",
)
HOST_P2P_SCOPES = ("lan", "zerotier", "vpn", "other")
NETWORK_SCOPES = ("zerotier", "vpn", "lan", "other", "docker")
VPN_INTERFACE_PREFIXES = (
    "zt",
    "wg",
    "tailscale",
    "tun",
    "tap",
    "vpn",
    "ppp",
    "ipsec",
    "nordlynx",
    "nebula",
    "ham",
)
PUBLIC_PEER_LATENCY_TIMEOUT_SECONDS = float(os.environ.get("BDAG_PUBLIC_PEER_LATENCY_TIMEOUT_SECONDS", "1.0"))


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
        output = run(["docker", "top", container], timeout=10)
    except Exception:
        return False
    return "/usr/local/bin/bdag" in output


def wait_for_node(container: str, timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if node_process_running(container):
            return
        time.sleep(3)
    raise RuntimeError(f"{container} did not show a running bdag process within {timeout}s")


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


def csv_items(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def peer_parts(peer: str) -> tuple[str, int, str] | None:
    match = ADDR_RE.search(peer)
    if not match:
        return None
    host, port_text, peer_id = match.groups()
    try:
        return host, int(port_text), peer_id
    except ValueError:
        return None


def configured_extra_peer_ids(values: dict[str, str]) -> set[str]:
    peer_ids: set[str] = set()
    for peer in configured_extra_peers(values):
        parts = peer_parts(peer)
        if parts:
            _, _, peer_id = parts
            peer_ids.add(peer_id)
    return peer_ids


def fallback_peer_ids(
    values: dict[str, str],
    local_hosts: set[str],
    blocked_peer_ids: set[str],
) -> dict[str, str]:
    by_port = {
        str(spec["port"]): node
        for node, spec in NODE_SPECS.items()
    }
    result: dict[str, str] = {}
    for key in ("LOCAL_PEER_ADDRESSES", "NODE1_PEER_ADDRESSES", "NODE2_PEER_ADDRESSES"):
        for host, port, peer_id in ADDR_RE.findall(values.get(key, "")):
            if host not in local_hosts or peer_id in blocked_peer_ids:
                continue
            node = by_port.get(port)
            if node:
                result[node] = peer_id
    return result


def docker_logs(container: str, tail: int | None = 5000, timeout: int = 20) -> str:
    command = ["docker", "logs", container]
    if tail is not None:
        command = ["docker", "logs", "--tail", str(tail), container]
    proc = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"docker logs failed for {container}").strip())
    return proc.stdout + proc.stderr


def latest_peer_id(container: str, fallback: str | None = None) -> str:
    for tail, timeout in ((5000, 20), (50000, 30), (200000, 45), (None, 60)):
        logs = docker_logs(container, tail=tail, timeout=timeout)
        matches = PEER_RE.findall(logs)
        if matches:
            return matches[-1]
    if fallback:
        return fallback
    raise RuntimeError(f"could not find local peer ID in logs for {container}")


def hostname_ipv4_addresses() -> list[str]:
    output = run(["hostname", "-I"], timeout=5)
    addresses: list[str] = []
    for token in output.split():
        try:
            ip = ipaddress.ip_address(token)
        except ValueError:
            continue
        if ip.version != 4 or ip.is_loopback or ip.is_link_local:
            continue
        addresses.append(str(ip))
    return addresses


def p2p_host_ips(address_groups: dict[str, list[str]]) -> list[str]:
    ips: list[str] = []
    for scope in HOST_P2P_SCOPES:
        ips.extend(address_groups.get(scope, []))
    return unique_list(ips)


def host_ipv4_groups(explicit: str | None = None) -> tuple[dict[str, list[str]], dict[str, list[ipaddress.IPv4Network]]]:
    groups = {"lan": [], "zerotier": [], "vpn": [], "docker": [], "other": []}
    networks: dict[str, list[ipaddress.IPv4Network]] = {"lan": [], "zerotier": [], "vpn": [], "docker": [], "other": []}
    if explicit:
        try:
            address = ipaddress.ip_address(explicit)
        except ValueError as exc:
            raise RuntimeError(f"invalid --host-ip value: {explicit}") from exc
        if address.version != 4 or address.is_loopback or address.is_link_local:
            raise RuntimeError(f"--host-ip must be a routable IPv4 address: {explicit}")
        groups["lan"].append(str(address))
        networks["lan"].append(ipaddress.ip_network(f"{address}/32", strict=False))
        return groups, networks

    try:
        raw = run(["ip", "-j", "-4", "addr", "show"], timeout=5)
        interfaces = json.loads(raw)
    except Exception:
        for address in hostname_ipv4_addresses():
            groups["other"].append(address)
            networks["other"].append(ipaddress.ip_network(f"{address}/32", strict=False))
        return groups, networks

    for iface in interfaces:
        name = str(iface.get("ifname", ""))
        for item in iface.get("addr_info", []):
            if item.get("family") != "inet":
                continue
            address_text = item.get("local")
            prefix = item.get("prefixlen")
            if not address_text or prefix is None:
                continue
            try:
                address = ipaddress.ip_address(address_text)
                network = ipaddress.ip_network(f"{address_text}/{prefix}", strict=False)
            except ValueError:
                continue
            if address.version != 4 or address.is_loopback or address.is_link_local:
                continue
            if name.startswith(("docker", "br-", "veth")):
                scope = "docker"
            elif name.startswith("zt"):
                scope = "zerotier"
            elif name.startswith(VPN_INTERFACE_PREFIXES):
                scope = "vpn"
            elif item.get("scope") == "global":
                scope = "lan"
            else:
                scope = "other"
            groups[scope].append(str(address))
            networks[scope].append(network)
    return groups, networks


def peer_network_scope(peer: str, networks: dict[str, list[ipaddress.IPv4Network]]) -> str | None:
    match = IP4_ADDR_RE.search(peer)
    if not match:
        return None
    host, _, _ = match.groups()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if address.is_loopback or address.is_link_local:
        return None
    for scope in NETWORK_SCOPES:
        if any(address in network for network in networks.get(scope, [])):
            return scope
    if address.is_private:
        return "private"
    return None


def configured_extra_peers(values: dict[str, str]) -> list[str]:
    peers: list[str] = []
    for key in EXTRA_PEER_KEYS:
        peers.extend(csv_items(values.get(key, "")))
    return peers


def discovered_log_peers(
    local_peer_ids: set[str],
    networks: dict[str, list[ipaddress.IPv4Network]],
) -> tuple[list[str], list[str], list[str]]:
    lan_peers: list[str] = []
    vpn_peers: list[str] = []
    zerotier_peers: list[str] = []
    for node in NODE_SPECS:
        try:
            logs = docker_logs(node, tail=10000)
        except Exception:
            continue
        for peer in LOG_MULTIADDR_RE.findall(logs):
            parts = peer_parts(peer)
            if not parts:
                continue
            _, _, peer_id = parts
            if peer_id in local_peer_ids:
                continue
            scope = peer_network_scope(peer, networks)
            if scope in ("lan", "other", "private"):
                lan_peers.append(peer)
            elif scope == "vpn":
                vpn_peers.append(peer)
            elif scope == "zerotier":
                zerotier_peers.append(peer)
    return unique_list(lan_peers), unique_list(vpn_peers), unique_list(zerotier_peers)


def unique_csv(items: list[str]) -> str:
    return ",".join(unique_list(items))


def unique_list(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        item = item.strip()
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def public_peer_latency_score(peer: str) -> tuple[int, float, str]:
    parts = peer_parts(peer)
    if not parts:
        return (2, 9999.0, peer)
    host, port, _ = parts
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=PUBLIC_PEER_LATENCY_TIMEOUT_SECONDS):
            elapsed = max(0.0, time.monotonic() - started)
            return (0, elapsed, peer)
    except OSError:
        return (1, 9999.0, peer)


def sort_public_peers_by_latency(public_peers: list[str]) -> list[str]:
    """Prefer responsive public peers while keeping deterministic fallback order."""

    peers = unique_list(public_peers)
    return sorted(peers, key=public_peer_latency_score)


def public_peer_assignment(public_peers: list[str], paused_follower: str = "") -> tuple[list[str], list[str]]:
    sorted_peers = sort_public_peers_by_latency(public_peers)
    node1_peers: list[str] = []
    node2_peers: list[str] = []
    paused_follower = str(paused_follower or "")
    if paused_follower == "bdag-miner-node-1":
        return [], sorted_peers
    if paused_follower == "bdag-miner-node-2":
        return sorted_peers, []
    for index, peer in enumerate(sorted_peers):
        if index % 2 == 0:
            node1_peers.append(peer)
        else:
            node2_peers.append(peer)
    return node1_peers, node2_peers


def split_public_peers(public_peers: list[str]) -> tuple[list[str], list[str]]:
    return public_peer_assignment(public_peers, paused_follower="")


def build_local_addrs(
    peers: dict[str, str],
    address_groups: dict[str, list[str]],
) -> dict[str, list[str]]:
    addrs: dict[str, list[str]] = {}
    host_ips = p2p_host_ips(address_groups)
    for node, spec in NODE_SPECS.items():
        node_addrs = [f"/dns4/{node}/tcp/{spec['port']}/p2p/{peers[node]}"]
        for host_ip in host_ips:
            node_addrs.append(f"/ip4/{host_ip}/tcp/{spec['port']}/p2p/{peers[node]}")
        addrs[node] = unique_list(node_addrs)
    return addrs


def sync_env_files(updates: dict[str, str]) -> tuple[list[Path], bool]:
    written: list[Path] = []
    primary_changed = False
    for path in (ENV_FILE, ROOT_ENV_FILE):
        if not path.exists():
            continue
        lines, values = read_env(path)
        changed = any(values.get(key) != value for key, value in updates.items())
        if not changed:
            continue
        write_env(path, lines, updates)
        written.append(path)
        if path == ENV_FILE:
            primary_changed = True
    return written, primary_changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-ip", help="Host/LAN IPv4 address reachable from both node containers")
    parser.add_argument("--apply", action="store_true", help="Restart node containers sequentially if peer lists changed")
    parser.add_argument("--force-apply", action="store_true", help="Restart node containers sequentially even if peer lists did not change")
    args = parser.parse_args()

    lines, values = read_env(ENV_FILE)
    public_peers = csv_items(values.get("PEER_ADDRESSES", ""))
    paused_follower = os.environ.get("BDAG_PAUSED_FOLLOWER", "")
    node1_public_peers, node2_public_peers = public_peer_assignment(public_peers, paused_follower=paused_follower)
    address_groups, networks = host_ipv4_groups(args.host_ip)
    local_hosts = {
        *NODE_SPECS.keys(),
        *p2p_host_ips(address_groups),
    }
    blocked_peer_ids = configured_extra_peer_ids(values)

    fallback_peers = fallback_peer_ids(values, local_hosts, blocked_peer_ids)
    peers: dict[str, str] = {}
    for node in NODE_SPECS:
        peers[node] = latest_peer_id(node, fallback=fallback_peers.get(node))

    local_addrs = build_local_addrs(peers, address_groups)
    local_peer_ids = set(peers.values())
    discovered_lan, discovered_vpn, discovered_zerotier = discovered_log_peers(local_peer_ids, networks)
    extra_peers = unique_list([*configured_extra_peers(values), *discovered_lan, *discovered_vpn, *discovered_zerotier])
    updates = {
        "NODE1_PEER_ADDRESSES": unique_csv([*node1_public_peers, *extra_peers, *local_addrs["bdag-miner-node-2"]]),
        "NODE2_PEER_ADDRESSES": unique_csv([*node2_public_peers, *extra_peers, *local_addrs["bdag-miner-node-1"]]),
        "LOCAL_PEER_ADDRESSES": unique_csv([*local_addrs["bdag-miner-node-1"], *local_addrs["bdag-miner-node-2"]]),
    }
    if discovered_lan:
        updates["DISCOVERED_LAN_PEER_ADDRESSES"] = unique_csv(discovered_lan)
    if discovered_vpn:
        updates["DISCOVERED_VPN_PEER_ADDRESSES"] = unique_csv(discovered_vpn)
    if discovered_zerotier:
        updates["DISCOVERED_ZEROTIER_PEER_ADDRESSES"] = unique_csv(discovered_zerotier)

    written, primary_changed = sync_env_files(updates)
    if written:
        for path in written:
            print(f"updated {path}")
    else:
        print("local peer configuration already current")
    print(f"lan_ips={','.join(address_groups.get('lan', [])) or '-'}")
    print(f"zerotier_ips={','.join(address_groups.get('zerotier', [])) or '-'}")
    print(f"vpn_ips={','.join(address_groups.get('vpn', [])) or '-'}")
    print(f"other_p2p_ips={','.join(address_groups.get('other', [])) or '-'}")
    print(f"p2p_interface_ips={','.join(p2p_host_ips(address_groups)) or '-'}")
    print(f"docker_gateway_ips={','.join(address_groups.get('docker', [])) or '-'}")
    print(
        f"public_peers={len(public_peers)} node1_public_peers={len(node1_public_peers)} "
        f"node2_public_peers={len(node2_public_peers)} paused_follower={paused_follower or '-'}"
    )
    print(f"extra_network_peers={len(extra_peers)} discovered_lan={len(discovered_lan)} discovered_vpn={len(discovered_vpn)} discovered_zerotier={len(discovered_zerotier)}")
    for node, addrs in local_addrs.items():
        print(f"{node}={','.join(addrs)}")

    if (args.apply and primary_changed) or args.force_apply:
        for node in NODE_SPECS:
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
