from __future__ import annotations

import importlib.util
import sys
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
    def test_detects_single_node_asic_router_and_uses_default_route_ip(self) -> None:
        old_run = update_local_peers.run

        def fake_run(command: list[str], timeout: int = 20) -> str:
            if command == ["ip", "route"]:
                return "default via 192.168.68.1 dev wlan0 proto dhcp src 192.168.68.60 metric 600\n"
            if command == ["ip", "-br", "addr"]:
                return "\n".join(
                    [
                        "eth0 UP 192.168.77.1/24",
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
                "BDAG_ASIC_LAN_CIDRS": "192.168.77.0/24",
            }
            self.assertEqual("single-node-asic-router", update_local_peers.detect_network_topology(values))
            self.assertEqual("192.168.68.60", update_local_peers.choose_local_ip(values=values))
        finally:
            update_local_peers.run = old_run

    def test_tiered_peers_excludes_asic_lan_and_keeps_private_before_public(self) -> None:
        old_latency = update_local_peers.peer_tcp_latency
        update_local_peers.peer_tcp_latency = lambda peer: (True, 5.0)
        try:
            values = {
                "BDAG_P2P_LAN_PEERS": "/ip4/192.168.68.55/tcp/8152/p2p/peerLAN",
                "BDAG_P2P_VPN_PEERS": "/ip4/10.207.244.12/tcp/8152/p2p/peerVPN",
                "BOOTSTRAP_PEER_ADDRESSES": ",".join(
                    [
                        "/ip4/192.168.77.22/tcp/8152/p2p/peerASIC",
                        "/ip4/13.245.135.249/tcp/18150/p2p/peerPUB",
                    ]
                ),
                "BDAG_ASIC_LAN_CIDRS": "192.168.77.0/24",
            }

            tiers = update_local_peers.tiered_peer_addresses(values, "single-node-asic-router")
        finally:
            update_local_peers.peer_tcp_latency = old_latency

        self.assertEqual(["/ip4/192.168.68.55/tcp/8152/p2p/peerLAN"], tiers.lan)
        self.assertEqual(["/ip4/10.207.244.12/tcp/8152/p2p/peerVPN"], tiers.vpn)
        self.assertEqual(["/ip4/13.245.135.249/tcp/18150/p2p/peerPUB"], tiers.public)
        self.assertEqual(["/ip4/192.168.77.22/tcp/8152/p2p/peerASIC"], tiers.excluded_asic_lan)

    def test_stale_private_lan_address_does_not_outrank_reachable_vpn(self) -> None:
        old_run = update_local_peers.run
        old_latency = update_local_peers.peer_tcp_latency

        def fake_run(command: list[str], timeout: int = 20) -> str:
            if command == ["ip", "-br", "addr"]:
                return "\n".join(
                    [
                        "eth0 UP 192.168.77.1/24",
                        "wlan0 UP 192.168.68.60/22",
                        "ztcdcjczoy UNKNOWN 10.207.244.83/24",
                    ]
                )
            if command == ["ip", "route"]:
                return "default via 192.168.68.1 dev wlan0 proto dhcp src 192.168.68.60 metric 600\n"
            raise AssertionError(command)

        def fake_latency(peer: str) -> tuple[bool, float]:
            if "192.168.68.55" in peer:
                return True, 0.002
            if "10.207.244.12" in peer:
                return True, 0.003
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
                "BDAG_ASIC_LAN_CIDRS": "192.168.77.0/24",
            }

            tiers = update_local_peers.tiered_peer_addresses(values, "single-node-asic-router")
        finally:
            update_local_peers.run = old_run
            update_local_peers.peer_tcp_latency = old_latency

        self.assertEqual(["/ip4/192.168.68.55/tcp/8152/p2p/peerLAN"], tiers.lan)
        self.assertEqual(
            [
                "/ip4/10.207.244.12/tcp/8152/p2p/peerVPN",
                "/ip4/192.168.1.120/tcp/8152/p2p/peerOLDLAN",
            ],
            tiers.vpn,
        )
        self.assertEqual("", update_local_peers.normalize_lan_prefixes({"BDAG_FASTSYNC_LAN_PREFIXES": "192.168."}, "single-node-asic-router"))
        self.assertEqual("tiered-latency", update_local_peers.normalize_peer_ordering("1"))

    def test_single_active_node_does_not_add_itself_as_a_peer(self) -> None:
        peers = [
            "/ip4/192.168.68.60/tcp/8152/p2p/localNode2",
            "/ip4/10.207.244.12/tcp/8152/p2p/remoteVpn",
            "/ip4/13.245.135.249/tcp/18150/p2p/publicSeed",
        ]

        self.assertEqual(
            [
                "/ip4/10.207.244.12/tcp/8152/p2p/remoteVpn",
                "/ip4/13.245.135.249/tcp/18150/p2p/publicSeed",
            ],
            update_local_peers.without_peer_ids(peers, {"localNode2"}),
        )


if __name__ == "__main__":
    unittest.main()
