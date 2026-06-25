from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops" / "update-local-peers.py"
SPEC = importlib.util.spec_from_file_location("update_local_peers", SCRIPT)
update_local_peers = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = update_local_peers
SPEC.loader.exec_module(update_local_peers)


class UpdateLocalPeersTopologyTests(unittest.TestCase):
    ENV_KEYS = (
        "BDAG_P2P_LAN_PEERS",
        "BDAG_P2P_VPN_PEERS",
        "BDAG_P2P_PUBLIC_PEERS",
        "BDAG_NODE_PEER_ADDRESSES",
        "BOOTSTRAP_PEER_ADDRESSES",
        "DISCOVERED_LAN_PEER_ADDRESSES",
        "DISCOVERED_ZEROTIER_PEER_ADDRESSES",
        "EXTRA_PEER_ADDRESSES",
        "LAN_PEER_ADDRESSES",
        "LOCAL_PEER_ADDRESSES",
        "P2P_PORT",
        "PEER_ADDRESSES",
        "VPN_PEER_ADDRESSES",
        "ZEROTIER_PEER_ADDRESSES",
        "BDAG_NETWORK_TOPOLOGY",
        "BDAG_DETECTED_NETWORK_TOPOLOGY",
        "BDAG_ASIC_LAN_ENABLED",
        "BDAG_ASIC_LAN_INTERFACE",
        "BDAG_ASIC_LAN_CIDRS",
        "BDAG_CHAIN_PEERSTORE_PEER_EXTRACTION_ENABLED",
        "BDAG_CHAIN_PEERSTORE_SERVICE_PORTS",
    )

    def setUp(self) -> None:
        self._old_env = {key: os.environ.get(key) for key in self.ENV_KEYS}
        self._tmp_runtime = tempfile.TemporaryDirectory()
        self._old_runtime_paths = {
            "RUNTIME_DIR": update_local_peers.RUNTIME_DIR,
            "RUNTIME_ENV_FILE": update_local_peers.RUNTIME_ENV_FILE,
            "SYNC_COORDINATOR_STATE_FILE": update_local_peers.SYNC_COORDINATOR_STATE_FILE,
            "DEFERRED_APPLY_FILE": update_local_peers.DEFERRED_APPLY_FILE,
            "CHAIN_PEERSTORE_CANDIDATES_FILE": update_local_peers.CHAIN_PEERSTORE_CANDIDATES_FILE,
            "LIVE_PEERS_FILE": update_local_peers.LIVE_PEERS_FILE,
            "PEER_DISCOVERY_FILE": update_local_peers.PEER_DISCOVERY_FILE,
        }
        runtime = Path(self._tmp_runtime.name)
        update_local_peers.RUNTIME_DIR = runtime
        update_local_peers.RUNTIME_ENV_FILE = runtime / "ops.env"
        update_local_peers.SYNC_COORDINATOR_STATE_FILE = runtime / "sync-coordinator-state.json"
        update_local_peers.DEFERRED_APPLY_FILE = runtime / "local-peers-deferred-apply"
        update_local_peers.CHAIN_PEERSTORE_CANDIDATES_FILE = runtime / "chain-peerstore-candidates.txt"
        update_local_peers.LIVE_PEERS_FILE = runtime / "live-peers-current.txt"
        update_local_peers.PEER_DISCOVERY_FILE = runtime / "peer-discovery-current.json"
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["BDAG_CHAIN_PEERSTORE_PEER_EXTRACTION_ENABLED"] = "0"

    def tearDown(self) -> None:
        for name, value in self._old_runtime_paths.items():
            setattr(update_local_peers, name, value)
        self._tmp_runtime.cleanup()
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_detects_asic_router_and_uses_default_route_ip(self) -> None:
        old_run = update_local_peers.run

        def fake_run(command: list[str], timeout: int = 20) -> str:
            if command == ["ip", "route"]:
                return "default via 192.168.68.1 dev wlan0 proto dhcp src 192.168.68.60 metric 600\n"
            if command == ["ip", "-br", "addr"]:
                return "\n".join(
                    [
                        "eth0 UP 192.168.1.105/24",
                        "wlan0 UP 192.168.68.60/22",
                        "ztcdcjczoy UNKNOWN 10.207.244.83/24",
                    ]
                )
            raise AssertionError(command)

        try:
            update_local_peers.run = fake_run
            values = {
                "BDAG_NETWORK_TOPOLOGY": "auto",
                "BDAG_ASIC_LAN_INTERFACE": "eth0",
                "BDAG_ASIC_LAN_CIDRS": "192.168.1.0/24",
            }
            self.assertEqual("asic-router", update_local_peers.detect_network_topology(values))
            self.assertEqual("192.168.68.60", update_local_peers.choose_local_ip(values=values))
        finally:
            update_local_peers.run = old_run

    def test_blank_asic_lan_interface_auto_detects_matching_non_default_interface(self) -> None:
        old_run = update_local_peers.run

        def fake_run(command: list[str], timeout: int = 20) -> str:
            if command == ["ip", "route"]:
                return "default via 192.168.68.1 dev wlan0 proto dhcp src 192.168.68.60 metric 600\n"
            if command == ["ip", "-br", "addr"]:
                return "\n".join(
                    [
                        "enp2s0 UP 192.168.1.105/24",
                        "wlan0 UP 192.168.68.60/22",
                    ]
                )
            raise AssertionError(command)

        try:
            update_local_peers.run = fake_run
            values = {
                "BDAG_NETWORK_TOPOLOGY": "auto",
                "BDAG_ASIC_LAN_INTERFACE": "",
                "BDAG_ASIC_LAN_CIDRS": "192.168.1.0/24",
            }
            self.assertEqual("asic-router", update_local_peers.detect_network_topology(values))
        finally:
            update_local_peers.run = old_run

    def test_p2p_candidates_merge_all_complete_peer_sources_by_latency(self) -> None:
        old_latency = update_local_peers.peer_tcp_latency

        def fake_latency(peer: str) -> tuple[bool, float]:
            if "peerASIC" in peer:
                return True, 1.0
            if "peerVPN" in peer:
                return True, 2.0
            if "peerPUB" in peer:
                return True, 3.0
            if "peerLAN" in peer:
                return True, 4.0
            return False, float("inf")

        update_local_peers.peer_tcp_latency = fake_latency
        try:
            values = {
                "BDAG_P2P_LAN_PEERS": "/ip4/192.168.68.55/tcp/8152/p2p/peerLAN",
                "BDAG_P2P_VPN_PEERS": "/ip4/10.207.244.12/tcp/8152/p2p/peerVPN",
                "BOOTSTRAP_PEER_ADDRESSES": ",".join(
                    [
                        "/ip4/192.168.1.22/tcp/8152/p2p/peerASIC",
                        "/ip4/13.245.135.249/tcp/18150/p2p/peerPUB",
                    ]
                ),
                "BDAG_ASIC_LAN_CIDRS": "192.168.1.0/24",
            }

            candidates = update_local_peers.p2p_peer_candidates(values)
        finally:
            update_local_peers.peer_tcp_latency = old_latency

        self.assertEqual(
            [
                "/ip4/192.168.1.22/tcp/8152/p2p/peerASIC",
                "/ip4/10.207.244.12/tcp/8152/p2p/peerVPN",
                "/ip4/13.245.135.249/tcp/18150/p2p/peerPUB",
                "/ip4/192.168.68.55/tcp/8152/p2p/peerLAN",
            ],
            candidates.peers,
        )
        self.assertEqual([], candidates.rejected_non_p2p)

    def test_address_class_does_not_outrank_measured_latency(self) -> None:
        old_run = update_local_peers.run
        old_latency = update_local_peers.peer_tcp_latency

        def fake_run(command: list[str], timeout: int = 20) -> str:
            if command == ["ip", "-br", "addr"]:
                return "\n".join(
                    [
                        "eth0 UP 192.168.1.105/24",
                        "wlan0 UP 192.168.68.60/22",
                        "ztcdcjczoy UNKNOWN 10.207.244.83/24",
                    ]
                )
            if command == ["ip", "route"]:
                return "default via 192.168.68.1 dev wlan0 proto dhcp src 192.168.68.60 metric 600\n"
            raise AssertionError(command)

        def fake_latency(peer: str) -> tuple[bool, float]:
            if "192.168.68.55" in peer:
                return True, 4.0
            if "10.207.244.12" in peer:
                return True, 2.0
            if "192.168.1.120" in peer:
                return True, 1.0
            return False, float("inf")

        try:
            update_local_peers.run = fake_run
            update_local_peers.peer_tcp_latency = fake_latency
            values = {
                "BOOTSTRAP_PEER_ADDRESSES": ",".join(
                    [
                        "/ip4/192.168.68.55/tcp/8152/p2p/peerLAN",
                        "/ip4/192.168.1.120/tcp/8152/p2p/peerOLDLAN",
                        "/ip4/10.207.244.12/tcp/8152/p2p/peerVPN",
                    ]
                ),
                "BDAG_ASIC_LAN_CIDRS": "192.168.1.0/24",
            }

            candidates = update_local_peers.p2p_peer_candidates(values)
        finally:
            update_local_peers.run = old_run
            update_local_peers.peer_tcp_latency = old_latency

        self.assertEqual(
            [
                "/ip4/192.168.1.120/tcp/8152/p2p/peerOLDLAN",
                "/ip4/10.207.244.12/tcp/8152/p2p/peerVPN",
                "/ip4/192.168.68.55/tcp/8152/p2p/peerLAN",
            ],
            candidates.peers,
        )

    def test_single_active_node_does_not_add_itself_as_a_peer(self) -> None:
        peers = [
            "/ip4/192.168.68.60/tcp/8151/p2p/localSelf",
            "/ip4/10.207.244.12/tcp/8152/p2p/remoteVpn",
            "/ip4/13.245.135.249/tcp/18150/p2p/publicSeed",
        ]

        self.assertEqual(
            [
                "/ip4/10.207.244.12/tcp/8152/p2p/remoteVpn",
                "/ip4/13.245.135.249/tcp/18150/p2p/publicSeed",
            ],
            update_local_peers.without_peer_ids(peers, {"localSelf"}),
        )

    def test_single_active_node_keeps_configured_local_node_addrs(self) -> None:
        old_local_ipv4_addresses = update_local_peers.local_ipv4_addresses
        try:
            update_local_peers.local_ipv4_addresses = lambda: ["192.168.1.120", "10.207.244.12"]
            peers = [
                "/ip4/192.168.1.120/tcp/8151/p2p/oldLocalSelf",
                "/ip4/10.207.244.12/tcp/8151/p2p/oldLocalVpn",
                "/dns4/node/tcp/8151/p2p/oldLocalDns",
                "/ip4/10.207.244.83/tcp/8152/p2p/remoteNode",
            ]

            self.assertEqual(
                peers,
                update_local_peers.without_inactive_local_node_peers(
                    peers,
                    ["node"],
                    "192.168.1.120",
                ),
            )
        finally:
            update_local_peers.local_ipv4_addresses = old_local_ipv4_addresses

    def test_configured_p2p_port_uses_single_compose_port(self) -> None:
        self.assertEqual(8150, update_local_peers.configured_p2p_port({}))
        self.assertEqual(18150, update_local_peers.configured_p2p_port({"P2P_PORT": "18150"}))
        self.assertEqual(8150, update_local_peers.configured_p2p_port({"P2P_PORT": "bad"}))
        self.assertEqual(8150, update_local_peers.configured_p2p_port({"P2P_PORT": "70000"}))

    def test_peerstore_log_candidates_keep_only_public_service_ports(self) -> None:
        old_docker_logs = update_local_peers.docker_logs
        os.environ["BDAG_CHAIN_PEERSTORE_PEER_EXTRACTION_ENABLED"] = "1"

        def fake_docker_logs(_node: str, tail: str = "5000", timeout: int = 20) -> str:
            return "\n".join(
                [
                    "Try to connect from peer store:{peerPub: "
                    "[/ip4/102.182.77.21/tcp/8150 /ip4/102.182.77.21/tcp/52604 "
                    "/ip4/192.168.1.9/tcp/8150 /ip4/169.254.20.10/tcp/8150 /dns4/node/tcp/8150]}",
                    "Try to connect from peer store:{peerAlt: [/ip4/129.121.92.232/tcp/8153]}",
                ]
            )

        try:
            update_local_peers.docker_logs = fake_docker_logs
            peers = update_local_peers.node_peerstore_log_candidates({}, active_nodes=["node"])
        finally:
            update_local_peers.docker_logs = old_docker_logs

        self.assertEqual(
            [
                "/ip4/102.182.77.21/tcp/8150/p2p/peerPub",
                "/ip4/129.121.92.232/tcp/8153/p2p/peerAlt",
            ],
            peers,
        )

    def test_peerstore_candidate_file_does_not_replay_private_or_high_ports(self) -> None:
        old_latency = update_local_peers.peer_tcp_latency
        public_peer = "/ip4/102.182.77.16/tcp/8150/p2p/peerPublic"
        private_peer = "/ip4/192.168.1.9/tcp/8150/p2p/peerPrivate"
        high_port_peer = "/ip4/102.182.77.16/tcp/52604/p2p/peerHighPort"
        update_local_peers.CHAIN_PEERSTORE_CANDIDATES_FILE.write_text(
            "\n".join([public_peer, private_peer, high_port_peer]) + "\n",
            encoding="utf-8",
        )

        def fake_latency(_peer: str) -> tuple[bool, float]:
            return True, 1.0

        try:
            update_local_peers.peer_tcp_latency = fake_latency
            candidates = update_local_peers.p2p_peer_candidates({})
        finally:
            update_local_peers.peer_tcp_latency = old_latency

        self.assertEqual([public_peer], candidates.peers)
        self.assertEqual(
            {"chain-peerstore-candidate-file": [public_peer]},
            candidates.source_peers,
        )


if __name__ == "__main__":
    unittest.main()
