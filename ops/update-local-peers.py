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
NODE_PEER_ID_ENV = {
    "bdag-miner-node-1": ("BDAG_LOCAL_NODE1_PEER_ID", "BDAG_NODE1_PEER_ID"),
    "bdag-miner-node-2": ("BDAG_LOCAL_NODE2_PEER_ID", "BDAG_NODE2_PEER_ID"),
}
PEER_RE = re.compile(r"Node started p2p server.*?/p2p/([A-Za-z0-9]+)")
ADDR_RE = re.compile(r"/ip4/[^,\s]+/tcp/(\d+)/p2p/([A-Za-z0-9]+)")
PEER_RE_FULL = re.compile(r"/ip4/([^/]+)/tcp/(\d+)/p2p/([^,\s]+)")
PEER_LATENCY_TIMEOUT = float(os.environ.get("BDAG_LOCAL_PEER_LATENCY_TIMEOUT", "0.75"))
PEER_LATENCY_WORKERS = int(os.environ.get("BDAG_LOCAL_PEER_LATENCY_WORKERS", "16"))
DASHBOARD_STATUS_URL = os.environ.get("BDAG_DASHBOARD_STATUS_URL", "http://127.0.0.1:8088/api/status")
ACTIVE_MINING_RECENT_SECONDS = int(os.environ.get("BDAG_LOCAL_PEERS_ACTIVE_MINING_RECENT_SECONDS", "300"))
DEFAULT_ASIC_LAN_CIDRS = "192.168.50.0/24"
TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
AUTO_VALUES = {"", "auto", "detect"}
CGNAT_NETWORKS = [ipaddress.ip_network("100.64.0.0/10")]
LAN_PEER_KEYS = (
    "BDAG_P2P_LAN_PEERS",
    "LAN_PEER_ADDRESSES",
    "LOCAL_PEER_ADDRESSES",
    "BDAG_FASTSYNC_LAN_PEERS",
    "BDAG_FASTSYNC_LOCAL_PEERS",
)
VPN_PEER_KEYS = (
    "BDAG_P2P_VPN_PEERS",
    "VPN_PEER_ADDRESSES",
    "ZEROTIER_PEER_ADDRESSES",
    "BDAG_FASTSYNC_VPN_PEERS",
    "BDAG_FASTSYNC_PRIVATE_PEERS",
)
PUBLIC_PEER_KEYS = ("BDAG_P2P_PUBLIC_PEERS", "BDAG_FASTSYNC_PUBLIC_PEERS")
GENERIC_PEER_KEYS = (
    "BOOTSTRAP_PEER_ADDRESSES",
    "PEER_ADDRESSES",
    "BDAG_FASTSYNC_PEERS",
    "BDAG_FASTSNAP_PEERS",
    "NODE1_PEER_ADDRESSES",
    "NODE2_PEER_ADDRESSES",
)


class PeerTiers:
    def __init__(self, lan: list[str], vpn: list[str], public: list[str], excluded_asic_lan: list[str]) -> None:
        self.lan = lan
        self.vpn = vpn
        self.public = public
        self.excluded_asic_lan = excluded_asic_lan


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


def env_value(values: dict[str, str], key: str, default: str = "") -> str:
    return os.environ.get(key) or values.get(key) or default


def truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def split_peer_csv(value: str) -> list[str]:
    peers: list[str] = []
    for peer in re.split(r"[\s,]+", value or ""):
        peer = peer.strip()
        if peer:
            peers.append(peer)
    return peers


def peer_values(values: dict[str, str], keys: tuple[str, ...]) -> list[str]:
    peers: list[str] = []
    for key in keys:
        peers.extend(split_peer_csv(env_value(values, key)))
    return peers


def peer_parts(peer: str) -> tuple[str, int, str] | None:
    match = PEER_RE_FULL.search(peer)
    if not match:
        return None
    host, port_text, peer_id = match.groups()
    try:
        port = int(port_text)
    except ValueError:
        return None
    return host, port, peer_id


def peer_ip(peer: str) -> ipaddress.IPv4Address | None:
    parts = peer_parts(peer)
    if not parts:
        return None
    try:
        ip = ipaddress.ip_address(parts[0])
    except ValueError:
        return None
    if ip.version != 4:
        return None
    return ip


def parse_networks(raw: str, default: str = "") -> list[ipaddress.IPv4Network]:
    networks: list[ipaddress.IPv4Network] = []
    for item in re.split(r"[\s,]+", raw or default):
        item = item.strip()
        if not item:
            continue
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            continue
        if network.version == 4:
            networks.append(network)
    return networks


def peer_in_networks(peer: str, networks: list[ipaddress.IPv4Network]) -> bool:
    ip = peer_ip(peer)
    return bool(ip and any(ip in network for network in networks))


def host_matches_lan_prefixes(host: str, prefixes: str) -> bool:
    return any(host.startswith(prefix.strip()) for prefix in prefixes.split(",") if prefix.strip())


def peer_is_lan(peer: str, values: dict[str, str]) -> bool:
    parts = peer_parts(peer)
    if not parts:
        return False
    prefixes = env_value(values, "BDAG_FASTSYNC_LAN_PREFIXES")
    if prefixes and host_matches_lan_prefixes(parts[0], prefixes):
        return True
    return peer_in_networks(peer, local_lan_networks(values))


def peer_is_private_or_vpn(peer: str) -> bool:
    ip = peer_ip(peer)
    return bool(ip and (ip.is_private or any(ip in network for network in CGNAT_NETWORKS)))


def normalize_peer_ordering(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"", "1", "true", "yes", "on", "enabled", "latency", "legacy-buckets", "buckets"}:
        return "tiered-latency"
    return normalized


def normalize_lan_prefixes(values: dict[str, str], topology: str) -> str:
    prefixes = env_value(values, "BDAG_FASTSYNC_LAN_PREFIXES")
    if topology == "single-node-asic-router" and prefixes.strip() == "192.168.":
        return ""
    return prefixes


def asic_lan_networks(values: dict[str, str]) -> list[ipaddress.IPv4Network]:
    return parse_networks(env_value(values, "BDAG_ASIC_LAN_CIDRS", DEFAULT_ASIC_LAN_CIDRS))


def interface_ipv4_addresses() -> dict[str, list[str]]:
    try:
        output = run(["ip", "-br", "addr"], timeout=5)
    except Exception:
        return {}
    result: dict[str, list[str]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        iface = parts[0]
        for token in parts[2:]:
            if "/" not in token:
                continue
            ip_text = token.split("/", 1)[0]
            try:
                ip = ipaddress.ip_address(ip_text)
            except ValueError:
                continue
            if ip.version == 4 and not ip.is_loopback and not ip.is_link_local:
                result.setdefault(iface, []).append(str(ip))
    return result


def interface_ipv4_networks() -> dict[str, list[ipaddress.IPv4Network]]:
    try:
        output = run(["ip", "-br", "addr"], timeout=5)
    except Exception:
        return {}
    result: dict[str, list[ipaddress.IPv4Network]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        iface = parts[0]
        for token in parts[2:]:
            if "/" not in token:
                continue
            try:
                interface = ipaddress.ip_interface(token)
            except ValueError:
                continue
            if interface.version == 4 and not interface.ip.is_loopback and not interface.ip.is_link_local:
                result.setdefault(iface, []).append(interface.network)
    return result


def vpn_or_container_interface(iface: str) -> bool:
    return iface.startswith(("zt", "wg", "tun", "tap", "tailscale", "docker", "br-", "veth"))


def local_lan_networks(values: dict[str, str]) -> list[ipaddress.IPv4Network]:
    asic_networks = asic_lan_networks(values)
    networks: list[ipaddress.IPv4Network] = []
    for iface, iface_networks in interface_ipv4_networks().items():
        if vpn_or_container_interface(iface):
            continue
        for network in iface_networks:
            if any(network.subnet_of(asic_network) or asic_network.subnet_of(network) for asic_network in asic_networks):
                continue
            if network.is_private:
                networks.append(network)
    return networks


def default_route_interface() -> str:
    try:
        output = run(["ip", "route"], timeout=5)
    except Exception:
        return ""
    for line in output.splitlines():
        parts = line.split()
        if not parts or parts[0] != "default":
            continue
        try:
            return parts[parts.index("dev") + 1]
        except (ValueError, IndexError):
            return ""
    return ""


def ip_in_networks(address: str, networks: list[ipaddress.IPv4Network]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.version == 4 and any(ip in network for network in networks)


def detect_network_topology(values: dict[str, str]) -> str:
    explicit = env_value(values, "BDAG_NETWORK_TOPOLOGY", "auto").strip().lower()
    if explicit not in AUTO_VALUES:
        return explicit
    if truthy(env_value(values, "BDAG_ASIC_LAN_ENABLED")):
        return "single-node-asic-router"

    default_iface = default_route_interface()
    asic_iface = env_value(values, "BDAG_ASIC_LAN_INTERFACE", "eth0")
    networks = asic_lan_networks(values)
    for iface, addresses in interface_ipv4_addresses().items():
        if default_iface and iface == default_iface:
            continue
        if asic_iface and iface != asic_iface:
            continue
        if any(ip_in_networks(address, networks) for address in addresses):
            return "single-node-asic-router"
    return "standard"


def exclude_asic_lan_peer(peer: str, values: dict[str, str], topology: str) -> bool:
    if truthy(env_value(values, "BDAG_ALLOW_ASIC_LAN_P2P")):
        return False
    if topology != "single-node-asic-router":
        return False
    return peer_in_networks(peer, asic_lan_networks(values))


def sort_peers_by_latency(peers: list[str]) -> list[str]:
    indexed = list(enumerate(unique_csv(peers).split(","))) if peers else []
    scores: dict[int, tuple[bool, float]] = {}
    with ThreadPoolExecutor(max_workers=max(1, PEER_LATENCY_WORKERS)) as executor:
        futures = {executor.submit(peer_tcp_latency, peer): index for index, peer in indexed}
        for future in as_completed(futures):
            scores[futures[future]] = future.result()
    indexed.sort(key=lambda item: (0 if scores.get(item[0], (False, float("inf")))[0] else 1, scores.get(item[0], (False, float("inf")))[1], item[0]))
    return [peer for _, peer in indexed]


def tiered_peer_addresses(values: dict[str, str], topology: str) -> PeerTiers:
    tiers = PeerTiers(lan=[], vpn=[], public=[], excluded_asic_lan=[])
    seen: set[str] = set()

    def add(peer: str, tier: str) -> None:
        peer = peer.strip()
        if not peer or peer in seen or not peer_parts(peer):
            return
        seen.add(peer)
        if exclude_asic_lan_peer(peer, values, topology):
            tiers.excluded_asic_lan.append(peer)
            return
        getattr(tiers, tier).append(peer)

    for peer in peer_values(values, LAN_PEER_KEYS):
        add(peer, "lan")
    for peer in peer_values(values, VPN_PEER_KEYS):
        add(peer, "vpn")
    for peer in peer_values(values, PUBLIC_PEER_KEYS):
        add(peer, "public")
    for peer in peer_values(values, GENERIC_PEER_KEYS):
        if peer_is_lan(peer, values):
            add(peer, "lan")
        elif peer_is_private_or_vpn(peer):
            add(peer, "vpn")
        else:
            add(peer, "public")

    tiers.lan = sort_peers_by_latency(tiers.lan)
    tiers.vpn = sort_peers_by_latency(tiers.vpn)
    tiers.public = sort_peers_by_latency(tiers.public)
    return tiers


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
    for node, keys in NODE_PEER_ID_ENV.items():
        for key in keys:
            peer_id = values.get(key, "").strip()
            if peer_id:
                result[node] = peer_id
                break
    for value in (values.get("LOCAL_PEER_ADDRESSES", ""),):
        for port, peer_id in ADDR_RE.findall(value):
            node = by_port.get(port)
            if node and node not in result:
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
    return sort_peers_by_latency(peers)


def latest_peer_id(container: str, fallback: str | None = None) -> str:
    if fallback:
        return fallback
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


def choose_local_ip(explicit: str | None = None, values: dict[str, str] | None = None) -> str:
    if explicit:
        return explicit
    values = values or {}
    configured = env_value(values, "BDAG_P2P_ADVERTISE_IP")
    if configured:
        return configured

    networks = asic_lan_networks(values)
    interfaces = interface_ipv4_addresses()
    preferred_iface = env_value(values, "BDAG_P2P_INTERFACE")
    if preferred_iface:
        for address in interfaces.get(preferred_iface, []):
            if not ip_in_networks(address, networks):
                return address

    default_iface = default_route_interface()
    if default_iface:
        for address in interfaces.get(default_iface, []):
            if not ip_in_networks(address, networks):
                return address

    addresses = local_ipv4_addresses()
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_private and not ip_in_networks(address, networks):
            return address
    if addresses:
        for address in addresses:
            if not ip_in_networks(address, networks):
                return address
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


def without_peer_ids(peers: list[str], peer_ids: set[str]) -> list[str]:
    if not peer_ids:
        return peers
    result: list[str] = []
    for peer in peers:
        parts = peer_parts(peer)
        if parts and parts[2] in peer_ids:
            continue
        result.append(peer)
    return result


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
    parser.add_argument("--env-file", type=Path, default=ENV_FILE, help="Pool stack .env file to update")
    parser.add_argument("--compose-file", type=Path, default=PROJECT_ROOT / "docker-compose.yml", help="Compose file used when --apply restarts node containers")
    parser.add_argument("--apply", action="store_true", help="Restart node containers sequentially if peer lists changed")
    parser.add_argument("--force-apply", action="store_true", help="Restart node containers sequentially even if peer lists did not change")
    args = parser.parse_args()

    env_file = args.env_file
    compose_file = args.compose_file
    lines, values = read_env(env_file)
    active_nodes = configured_active_nodes(values)
    topology = detect_network_topology(values)
    peer_values_for_ranking = dict(values)
    peer_values_for_ranking["BDAG_FASTSYNC_LAN_PREFIXES"] = normalize_lan_prefixes(values, topology)
    peer_tiers = tiered_peer_addresses(peer_values_for_ranking, topology)
    public_peers = peer_tiers.public
    paused = planned_paused_follower()
    node1_public_peers, node2_public_peers = public_peer_assignment(public_peers, paused, active_nodes)
    priority_peers = unique_csv(peer_tiers.lan + peer_tiers.vpn).split(",") if (peer_tiers.lan or peer_tiers.vpn) else []
    host_ip = choose_local_ip(args.host_ip, values)

    fallback_peers = fallback_peer_ids(values)
    peers: dict[str, str] = {}
    for node in active_nodes:
        peers[node] = latest_peer_id(node, fallback=fallback_peers.get(node))
    inactive_local_peer_ids = {
        peer_id
        for node, peer_id in fallback_peers.items()
        if node not in active_nodes and peer_id
    }

    local_addrs = {
        node: f"/ip4/{host_ip}/tcp/{spec['port']}/p2p/{peers[node]}"
        for node, spec in NODE_SPECS.items()
        if node in peers
    }
    updates: dict[str, str] = {}
    if "bdag-miner-node-1" in active_nodes:
        node1_peers = without_peer_ids(
            list(priority_peers) + list(node1_public_peers),
            inactive_local_peer_ids | {peers.get("bdag-miner-node-1", "")},
        )
        if "bdag-miner-node-2" in active_nodes and paused != "bdag-miner-node-2" and "bdag-miner-node-2" in local_addrs:
            node1_peers.insert(0, local_addrs["bdag-miner-node-2"])
        updates["NODE1_PEER_ADDRESSES"] = unique_csv(node1_peers)
    if "bdag-miner-node-2" in active_nodes:
        node2_peers = without_peer_ids(
            list(priority_peers) + list(node2_public_peers),
            inactive_local_peer_ids | {peers.get("bdag-miner-node-2", "")},
        )
        if "bdag-miner-node-1" in active_nodes and paused != "bdag-miner-node-1" and "bdag-miner-node-1" in local_addrs:
            node2_peers.insert(0, local_addrs["bdag-miner-node-1"])
        updates["NODE2_PEER_ADDRESSES"] = unique_csv(node2_peers)
    if local_addrs:
        updates["LOCAL_PEER_ADDRESSES"] = unique_csv([local_addrs[node] for node in active_nodes if node in local_addrs])
    for node, peer_id in peers.items():
        peer_id_keys = NODE_PEER_ID_ENV.get(node, ())
        if peer_id_keys:
            updates[peer_id_keys[0]] = peer_id
    updates["BDAG_NETWORK_TOPOLOGY"] = env_value(values, "BDAG_NETWORK_TOPOLOGY", "auto") or "auto"
    updates["BDAG_DETECTED_NETWORK_TOPOLOGY"] = topology
    updates["BDAG_FASTSYNC_PEER_ORDERING"] = normalize_peer_ordering(env_value(values, "BDAG_FASTSYNC_PEER_ORDERING", "tiered-latency"))
    updates["BDAG_FASTSYNC_LAN_PREFIXES"] = peer_values_for_ranking.get("BDAG_FASTSYNC_LAN_PREFIXES", "")
    updates["BDAG_FASTSYNC_LAN_PEERS"] = unique_csv(peer_tiers.lan)
    updates["BDAG_FASTSYNC_VPN_PEERS"] = unique_csv(peer_tiers.vpn)
    updates["BDAG_FASTSYNC_PUBLIC_PEERS"] = unique_csv(peer_tiers.public)

    changed = any(update_value_changed(key, values.get(key), value) for key, value in updates.items())
    if changed:
        write_env(env_file, lines, updates)
        print(f"updated {env_file}")
    else:
        print("local peer configuration already current")
    print(f"host_ip={host_ip}")
    print(f"network_topology={topology}")
    print(f"active_nodes={','.join(active_nodes)}")
    print(f"lan_peers={len(peer_tiers.lan)} vpn_peers={len(peer_tiers.vpn)} public_peers={len(public_peers)} excluded_asic_lan_peers={len(peer_tiers.excluded_asic_lan)} node1_public_peers={len(node1_public_peers)} node2_public_peers={len(node2_public_peers)} paused_follower={paused or 'none'}")
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
                str(env_file),
                "-f",
                str(compose_file),
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
