#!/usr/bin/env python3

import pathlib
import re
import unittest


ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]
FORBIDDEN_NODE_NAME_RE = re.compile(r"\b(?:bdag-miner-node-[12]|node[12]|BDAG_NODE[12])\b")


def read(rel: str) -> str:
    return (ROOT_DIR / rel).read_text(encoding="utf-8")


class StackNamingCoherenceTests(unittest.TestCase):
    def test_active_stack_files_do_not_reference_retired_node_names(self) -> None:
        checked_roots = [
            ROOT_DIR / "ops",
            ROOT_DIR / "scripts",
            ROOT_DIR / "docs",
        ]
        checked_files = [
            ROOT_DIR / ".env.example",
            ROOT_DIR / ".env.cpu.example",
            ROOT_DIR / "docker-compose.yml",
            ROOT_DIR / "AGENTS.md",
        ]
        for checked_root in checked_roots:
            for path in checked_root.rglob("*"):
                if path.is_dir() or "__pycache__" in path.parts or "runtime" in path.parts:
                    continue
                checked_files.append(path)

        offenders: list[str] = []
        for path in checked_files:
            if not path.exists() or path.is_dir():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if FORBIDDEN_NODE_NAME_RE.search(text):
                offenders.append(str(path.relative_to(ROOT_DIR)))
        self.assertEqual(offenders, [])

    def test_compose_dashboard_exports_current_container_names(self) -> None:
        compose = read("docker-compose.yml")

        self.assertNotIn("container_name:", compose)
        self.assertIn("  postgres:", compose)
        self.assertIn("  node:", compose)
        self.assertIn("  pool:", compose)
        self.assertIn("BDAG_NODE_SERVICES: node", compose)
        self.assertIn("BDAG_STACK_SERVICES: postgres,node,pool,dashboard", compose)
        self.assertIn("BDAG_POOL_CONTAINER: pool", compose)
        self.assertIn("BDAG_POOL_DB_CONTAINER: postgres", compose)
        self.assertIn("BDAG_NODE_RPC_URLS: node=http://node:38131", compose)
        self.assertIn("BDAG_STATUS_SAMPLER_ENABLED: ${BDAG_STATUS_SAMPLER_ENABLED:-0}", compose)
        self.assertIn("BDAG_DASHBOARD_DIRECT_STATUS_FALLBACK: ${BDAG_DASHBOARD_DIRECT_STATUS_FALLBACK:-1}", compose)
        self.assertIn("BDAG_DASHBOARD_STATUS_CACHE_SECONDS: ${BDAG_DASHBOARD_STATUS_CACHE_SECONDS:-120}", compose)
        self.assertIn("BDAG_DASHBOARD_SAMPLER_CACHE_SECONDS: ${BDAG_DASHBOARD_SAMPLER_CACHE_SECONDS:-120}", compose)
        self.assertIn("BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS: ${BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS:-120}", compose)
        self.assertIn('${DASHBOARD_HOST_BIND:-127.0.0.1}:${DASHBOARD_HOST_PORT:-8088}:${BDAG_DASHBOARD_PORT:-8088}', compose)

    def test_env_examples_and_installer_use_current_names(self) -> None:
        env_example = read(".env.example")
        portable = read("ops/portable.env.example")

        for text in (env_example, portable):
            self.assertIn("BDAG_POOL_CONTAINER=pool", text)
            self.assertIn("BDAG_POOL_DB_CONTAINER=postgres", text)
            self.assertIn("BDAG_NODE_SERVICES=node", text)
            self.assertIn("BDAG_STACK_SERVICES=postgres,node,pool,dashboard", text)
            self.assertIn("BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS=120", text)
            self.assertIn("BDAG_STATUS_SAMPLER_ENABLED=0", text)
            self.assertIn("BDAG_DASHBOARD_DIRECT_STATUS_FALLBACK=1", text)
        self.assertIn("POOL_GBT_MIN_INTERVAL_MS=1100", env_example)
        self.assertIn("POOL_GBT_PRESSURE_INTERVAL_MS=500", env_example)
        self.assertIn("POOL_GBT_PRESSURE_WINDOW_SECONDS=10", env_example)
        self.assertIn("POOL_RPC_ROUTER_NODE_HEALTH_PROBE_SECONDS=15", env_example)
        self.assertIn("POOL_RPC_ROUTER_NODE_HEALTH_MAX_AGE_SECONDS=30", env_example)
        self.assertIn("BDAG_CHAIN_PEERSTORE_PEER_EXTRACTION_ENABLED=1", env_example)
        self.assertIn("NODE_RPC_URLS=http://node:38131", env_example)
        self.assertIn("NODE_RPC_URLS=http://127.0.0.1:38131", portable)

    def test_release_installer_generates_current_runtime_topology(self) -> None:
        compose = read("docker-compose.yml")
        installer = read("ops/release-install.sh")

        self.assertIn("  pool:", compose)
        self.assertIn("  node:", compose)
        self.assertIn("  postgres:", compose)
        self.assertNotIn("container_name:", compose)
        self.assertIn("NODE_RPC_URLS: ${NODE_RPC_URLS:-http://node:38131}", compose)
        self.assertIn("BDAG_STACK_SERVICES: postgres,node,pool,dashboard", compose)
        self.assertIn("POOL_GBT_MIN_INTERVAL_MS: ${POOL_GBT_MIN_INTERVAL_MS:-1100}", compose)
        self.assertIn("POOL_GBT_PRESSURE_INTERVAL_MS: ${POOL_GBT_PRESSURE_INTERVAL_MS:-500}", compose)
        self.assertIn("POOL_GBT_PRESSURE_WINDOW_SECONDS: ${POOL_GBT_PRESSURE_WINDOW_SECONDS:-10}", compose)
        self.assertIn("POOL_RPC_ROUTER_NODE_HEALTH_PROBE_SECONDS: ${POOL_RPC_ROUTER_NODE_HEALTH_PROBE_SECONDS:-15}", compose)
        self.assertIn("POOL_RPC_ROUTER_NODE_HEALTH_MAX_AGE_SECONDS: ${POOL_RPC_ROUTER_NODE_HEALTH_MAX_AGE_SECONDS:-30}", compose)
        self.assertIn("DASHBOARD_HOST_BIND", installer)
        self.assertNotIn("install_dashboard", installer)
        self.assertIn("set_stack_default_env_value .env BDAG_NODE_SERVICES", installer)
        self.assertIn("set_stack_default_env_value .env BDAG_STACK_SERVICES", installer)
        self.assertIn('set_env_value .env POOL_RPC_BACKENDS "node=http://node:38131"', installer)
        self.assertIn("set_stack_default_env_value .env POOL_GBT_MIN_INTERVAL_MS", installer)
        self.assertIn("set_stack_default_env_value .env POOL_GBT_PRESSURE_INTERVAL_MS", installer)
        self.assertIn("set_stack_default_env_value .env POOL_RPC_ROUTER_NODE_HEALTH_PROBE_SECONDS", installer)

    def test_watchdogs_default_to_current_names(self) -> None:
        pool_ops = read("ops/pool_ops.py")
        sampler = read("ops/status_sampler.py")
        node_guard = read("ops/node_child_guard.py")
        host_guard = read("host/mining-appliance/bdag-node-child-guard")
        peer_refresh = read("ops/update-local-peers.py")

        self.assertIn('POOL_CONTAINER = os.environ.get("BDAG_POOL_CONTAINER", "pool")', pool_ops)
        self.assertIn('POOL_DB_CONTAINER = os.environ.get("BDAG_POOL_DB_CONTAINER", "postgres")', pool_ops)
        self.assertIn('NODES = split_env_list("BDAG_NODE_SERVICES", "node")', pool_ops)
        self.assertIn('"postgres,node,pool,dashboard"', pool_ops)
        self.assertIn('config_value("BDAG_NODE_SERVICES", "node")', sampler)
        self.assertIn('DEFAULT_NODE_CHILD_GUARD_NODES = "node"', node_guard)
        self.assertIn('DEFAULT_NODE_CHILD_GUARD_NODES = "node"', host_guard)
        self.assertIn("blockdag-node", node_guard)
        self.assertIn("blockdag-node", host_guard)
        self.assertIn('DEFAULT_ACTIVE_NODE_SERVICES = ["node"]', peer_refresh)

    def test_systemd_watchdogs_share_current_names_and_sampler_defaults(self) -> None:
        root_watchdog = read("ops/systemd/bdag-watchdog.service")
        root_sampler = read("ops/systemd/bdag-status-sampler.service")
        user_watchdog = read("ops/systemd/user-bdag-watchdog.service")
        user_sampler = read("ops/systemd/user-bdag-status-sampler.service")

        for unit in (root_watchdog, root_sampler, user_watchdog, user_sampler):
            self.assertIn("BDAG_NODE_SERVICES=node", unit)
            self.assertIn("BDAG_STACK_SERVICES=postgres,node,pool,dashboard", unit)
            self.assertIn("BDAG_POOL_CONTAINER=pool", unit)
            self.assertIn("BDAG_POOL_DB_CONTAINER=postgres", unit)

        self.assertIn("bdag-status-sampler.service", root_watchdog)
        self.assertIn("BDAG_STATUS_SAMPLER_MAX_AGE_SECONDS=120", root_sampler)
        self.assertIn("BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS=120", root_sampler)
        self.assertIn("BDAG_MINING_IMPERATIVE_GUARD_UNITS=", root_sampler)

        self.assertIn("bdag-status-sampler.service", user_watchdog)
        self.assertIn("EnvironmentFile=-/home/jeremy/blockdag-asic-pool/ops/runtime/ops.env", user_watchdog)
        self.assertIn("BDAG_STATUS_SAMPLER_MAX_AGE_SECONDS=120", user_sampler)
        self.assertIn("BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS=120", user_sampler)

    def test_validator_locks_current_topology_into_build_checks(self) -> None:
        validator = read("scripts/validate-pi5-restart-hardening.sh")

        self.assertIn('need_grep \'BDAG_STACK_SERVICES=postgres,node,pool,dashboard\' ".env.example"', validator)
        self.assertIn('need_grep \'BDAG_NODE_SERVICES: node\' "docker-compose.yml"', validator)
        self.assertIn('reject_grep \'container_name:\' "docker-compose.yml"', validator)
        self.assertIn('need_file "ops/tests/test_stack_naming_coherence.py"', validator)
        self.assertIn('need_file "ops/systemd/bdag-status-sampler.service"', validator)
        self.assertIn('python3 "$root/scripts/validate-stack-defaults.py" "$root"', validator)
        self.assertIn('need_grep \'BDAG_STATUS_SAMPLER_MAX_AGE_SECONDS=\' ".env.example"', validator)
        self.assertIn('need_grep \'BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS=\' ".env.example"', validator)
        self.assertIn('need_grep \'BDAG_DASHBOARD_DIRECT_STATUS_FALLBACK=1\' ".env.example"', validator)
        self.assertIn('need_grep \'POOL_GBT_MIN_INTERVAL_MS=\' ".env.example"', validator)
        self.assertIn('need_grep \'pool_template_rpc_pressure\' "scripts/mining-appliance-preflight.py"', validator)


if __name__ == "__main__":
    unittest.main()
