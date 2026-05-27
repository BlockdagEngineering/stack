from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "mining-appliance-preflight.py"
SPEC = importlib.util.spec_from_file_location("mining_appliance_preflight", SCRIPT)
preflight = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = preflight
SPEC.loader.exec_module(preflight)


class MiningAppliancePreflightTest(unittest.TestCase):
    def test_load_env_file_strips_quotes_and_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "# comment",
                        "BDAG_NODE_MODE='single'",
                        'BDAG_NODE_CACHE_MB="1024"',
                        "EMPTY=",
                    ]
                ),
                encoding="utf-8",
            )
            env = preflight.load_env_file(env_file)
        self.assertEqual(env["BDAG_NODE_MODE"], "single")
        self.assertEqual(env["BDAG_NODE_CACHE_MB"], "1024")
        self.assertEqual(env["EMPTY"], "")

    def test_constrained_env_warnings_for_double_node_and_large_cache(self) -> None:
        profile = preflight.HostProfile(
            os_name="linux",
            arch="x86_64",
            cpu_count=2,
            memory_bytes=3 * preflight.GIB,
            profile="constrained",
            kernel="test",
        )
        checks = []
        preflight.check_env_defaults(
            checks,
            {
                "BDAG_NODE_MODE": "double",
                "BDAG_NODE_CACHE_MB": "4096",
                "NODE_MAX_PEERS": "512",
                "BDAG_FASTSYNC_PREPROCESS_WORKERS": "4",
                "BDAG_FASTARTIFACTSYNC_ENABLED": "0",
                "BDAG_SYNC_COORDINATOR_ACCELERATE_FASTSYNC": "0",
                "BDAG_SYNC_COORDINATOR_FAST_RESTART_COOLDOWN_SECONDS": "3600",
                "BDAG_STATUS_SAMPLER_ENABLED": "0",
                "BDAG_ADAPTIVE_CONCURRENCY_ENABLED": "0",
                "BDAG_ENTRYPOINT_CHOWN_MODE": "always",
            },
            profile,
        )
        warnings = {check.name for check in checks if check.status == "warn"}
        self.assertIn("constrained_node_mode", warnings)
        self.assertIn("node_cache_budget", warnings)
        self.assertIn("peer_budget", warnings)
        self.assertIn("fastsync_preprocess_workers", warnings)
        self.assertIn("fastartifactsync", warnings)
        self.assertIn("fastsync_acceleration", warnings)
        self.assertIn("fastsync_restart_cooldown", warnings)
        self.assertIn("status_sampler", warnings)
        self.assertIn("adaptive_concurrency", warnings)
        self.assertIn("entrypoint_chown_mode", warnings)

    def test_no_fastsync_serve_suppresses_fastartifact_warning(self) -> None:
        profile = preflight.HostProfile(
            os_name="linux",
            arch="aarch64",
            cpu_count=4,
            memory_bytes=8 * preflight.GIB,
            profile="pi5",
            kernel="test",
        )
        checks = []
        preflight.check_env_defaults(
            checks,
            {
                "BDAG_NO_FASTSYNC_SERVE": "1",
                "BDAG_FASTARTIFACTSYNC_ENABLED": "0",
            },
            profile,
        )
        statuses = {check.name: check.status for check in checks}
        self.assertEqual(statuses["fastartifactsync"], "pass")

    def test_capability_profile_requires_no_fastsync_serving_on_usb_router(self) -> None:
        checks = []
        preflight.check_capability_profile(
            checks,
            {"BDAG_NO_FASTSYNC_SERVE": "auto", "BDAG_NODE_CACHE_MB": "6144"},
            {
                "capability_profile": "pi5-usb-asic-router",
                "host_facts": {
                    "topology": "single-node-asic-router",
                    "node_mode": "single",
                    "chain_paths": [{"storage_class": "usb-removable-flash"}],
                },
                "recommendations": {
                    "BDAG_NODE_CACHE_MB": "6144",
                    "BDAG_BLOCK_READ_AHEAD_KB": "256",
                    "BDAG_BLOCK_NR_REQUESTS": "128",
                },
            },
        )
        statuses = {check.name: check.status for check in checks}
        self.assertEqual(statuses["capability_profile"], "pass")
        self.assertEqual(statuses["capability_no_fastsync_serve"], "fail")

    def test_zero_wallet_is_release_blocking(self) -> None:
        checks = []
        preflight.check_wallet(checks, {"MINING_ADDRESS": preflight.ZERO_ETH_ADDRESS, "BDAG_ENABLE_NODE_MINING": "0"})
        self.assertEqual(checks[0].name, "mining_address")
        self.assertEqual(checks[0].status, "fail")

    def test_single_node_duplicate_data_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data" / "node1" / "mainnet" / "BdagChain").mkdir(parents=True)
            (root / "data" / "node2" / "mainnet" / "BdagChain").mkdir(parents=True)
            checks = []
            preflight.check_node_data_layout(checks, root, {"BDAG_NODE_MODE": "single"})
        found = {check.name: check.status for check in checks}
        self.assertEqual(found["single_node_duplicate_data"], "warn")

    def test_compose_bind_mount_overrides_default_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.override.yml").write_text(
                "\n".join(
                    [
                        "services:",
                        "  node:",
                        "    volumes:",
                        "      - /srv/bdag-chain-usb:/data:ro",
                        "      - /srv/bdag-chain-usb/node-data:/var/lib/bdagStack/node",
                    ]
                ),
                encoding="utf-8",
            )
            data_dir = preflight.env_data_dir(root, {})
        self.assertEqual(data_dir, Path("/srv/bdag-chain-usb/node-data"))

    def test_live_node_child_passes_when_compose_node_is_absent(self) -> None:
        old_run = preflight.run

        def fake_run(command: list[str], timeout: float = 5.0, cwd: Path | None = None):
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return Result()

        try:
            preflight.run = fake_run
            checks = []
            preflight.check_live_node_child(checks, Path("/tmp"))
        finally:
            preflight.run = old_run

        self.assertEqual(checks[0].status, "pass")

    def test_live_node_child_fails_when_wrapper_has_no_child(self) -> None:
        old_run = preflight.run

        def fake_run(command: list[str], timeout: float = 5.0, cwd: Path | None = None):
            class Result:
                returncode = 0
                stdout = "container-id\n"
                stderr = ""

            result = Result()
            if "exec" in command:
                result.returncode = 1
                result.stdout = ""
            return result

        try:
            preflight.run = fake_run
            checks = []
            preflight.check_live_node_child(checks, Path("/tmp"))
        finally:
            preflight.run = old_run

        self.assertEqual(checks[0].status, "fail")

    def test_asic_router_network_profile_checks_direct_miner_path(self) -> None:
        old_run = preflight.run

        def fake_run(command: list[str], timeout: float = 5.0, cwd: Path | None = None):
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            result = Result()
            if command == ["ip", "-o", "-4", "route", "get", "1.1.1.1"]:
                result.stdout = "1.1.1.1 via 192.168.68.1 dev wlan0 src 192.168.68.60 uid 1000\n"
            elif command == ["ip", "-br", "-4", "addr", "show", "dev", "eth0"]:
                result.stdout = "eth0 UP 192.168.50.1/24\n"
            elif command == ["sysctl", "-n", "net.ipv4.ip_forward"]:
                result.stdout = "1\n"
            elif command == ["nft", "list", "ruleset"]:
                result.stdout = "ip saddr 192.168.50.0/24 ip daddr != 192.168.50.0/24 masquerade\n"
            elif command == ["ip", "neigh", "show", "dev", "eth0"]:
                result.stdout = "192.168.50.177 lladdr 28:e2:97:1e:c0:b5 REACHABLE\n"
            elif command == ["ethtool", "-S", "eth0"]:
                result.stdout = "rx_frame_check_sequence_errors: 0\ntx_late_collisions: 0\n"
            else:
                raise AssertionError(command)
            return result

        try:
            preflight.run = fake_run
            checks = []
            preflight.check_network(
                checks,
                {
                    "BDAG_DETECTED_NETWORK_TOPOLOGY": "single-node-asic-router",
                    "BDAG_ASIC_LAN_INTERFACE": "eth0",
                    "BDAG_ASIC_LAN_CIDRS": "192.168.50.0/24",
                },
            )
        finally:
            preflight.run = old_run

        statuses = {check.name: check.status for check in checks}
        self.assertEqual(statuses["default_route"], "warn")
        self.assertEqual(statuses["asic_router_lan_address"], "pass")
        self.assertEqual(statuses["asic_router_default_route"], "pass")
        self.assertEqual(statuses["asic_router_ip_forward"], "pass")
        self.assertEqual(statuses["asic_router_nat"], "pass")
        self.assertEqual(statuses["asic_router_lan_neighbour"], "pass")
        self.assertEqual(statuses["asic_router_link_errors"], "pass")

    def test_asic_router_network_fails_when_default_route_uses_miner_lan(self) -> None:
        old_run = preflight.run

        def fake_run(command: list[str], timeout: float = 5.0, cwd: Path | None = None):
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            result = Result()
            if command == ["ip", "-o", "-4", "route", "get", "1.1.1.1"]:
                result.stdout = "1.1.1.1 via 192.168.50.1 dev eth0 src 192.168.50.1 uid 1000\n"
            elif command == ["ip", "-br", "-4", "addr", "show", "dev", "eth0"]:
                result.stdout = "eth0 UP 192.168.50.1/24\n"
            elif command == ["sysctl", "-n", "net.ipv4.ip_forward"]:
                result.stdout = "0\n"
            elif command == ["nft", "list", "ruleset"]:
                result.stdout = ""
                result.returncode = 1
            elif command == ["sudo", "-n", "nft", "list", "ruleset"]:
                result.stdout = ""
                result.returncode = 1
            elif command == ["iptables", "-t", "nat", "-S"]:
                result.stdout = ""
            elif command == ["sudo", "-n", "iptables", "-t", "nat", "-S"]:
                result.stdout = ""
            elif command == ["ip", "neigh", "show", "dev", "eth0"]:
                result.stdout = ""
            elif command == ["ethtool", "-S", "eth0"]:
                result.stdout = "rx_frame_check_sequence_errors: 3\n"
            else:
                raise AssertionError(command)
            return result

        try:
            preflight.run = fake_run
            checks = []
            preflight.check_network(
                checks,
                {
                    "BDAG_DETECTED_NETWORK_TOPOLOGY": "single-node-asic-router",
                    "BDAG_ASIC_LAN_INTERFACE": "eth0",
                    "BDAG_ASIC_LAN_CIDRS": "192.168.50.0/24",
                },
            )
        finally:
            preflight.run = old_run

        statuses = {check.name: check.status for check in checks}
        self.assertEqual(statuses["default_route"], "pass")
        self.assertEqual(statuses["asic_router_default_route"], "fail")
        self.assertEqual(statuses["asic_router_ip_forward"], "fail")
        self.assertEqual(statuses["asic_router_nat"], "warn")
        self.assertEqual(statuses["asic_router_lan_neighbour"], "warn")
        self.assertEqual(statuses["asic_router_link_errors"], "warn")


if __name__ == "__main__":
    unittest.main()
