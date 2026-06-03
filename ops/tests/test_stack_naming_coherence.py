#!/usr/bin/env python3

import pathlib
import unittest


ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]


def read(rel: str) -> str:
    return (ROOT_DIR / rel).read_text(encoding="utf-8")


class StackNamingCoherenceTests(unittest.TestCase):
    def test_compose_dashboard_exports_current_container_names(self) -> None:
        compose = read("docker-compose.yml")

        self.assertIn("container_name: postgres", compose)
        self.assertIn("container_name: node", compose)
        self.assertIn("container_name: pool", compose)
        self.assertIn("BDAG_NODE_SERVICES: node", compose)
        self.assertIn("BDAG_STACK_SERVICES: postgres,node,pool", compose)
        self.assertIn("BDAG_POOL_CONTAINER: pool", compose)
        self.assertIn("BDAG_POOL_DB_CONTAINER: postgres", compose)
        self.assertIn("BDAG_NODE_RPC_URLS: node=http://node:38131", compose)

    def test_env_examples_and_installer_use_current_names(self) -> None:
        env_example = read(".env.example")
        portable = read("ops/portable.env.example")
        installer = read("ops/install-dashboard.sh")

        for text in (env_example, portable):
            self.assertIn("BDAG_POOL_CONTAINER=pool", text)
            self.assertIn("BDAG_POOL_DB_CONTAINER=postgres", text)
            self.assertIn("BDAG_NODE_SERVICES=node", text)
            self.assertIn("BDAG_STACK_SERVICES=postgres,node,pool", text)
            self.assertIn("NODE_RPC_URLS=http://node:38131", text)

        self.assertIn("BDAG_POOL_CONTAINER=pool", installer)
        self.assertIn("BDAG_POOL_DB_CONTAINER=postgres", installer)
        self.assertIn("BDAG_NODE_SERVICES=node", installer)
        self.assertIn("BDAG_STACK_SERVICES=postgres,node,pool", installer)
        self.assertIn("ensure_env_value BDAG_STACK_SERVICES \"postgres,node,pool\"", installer)
        self.assertIn(
            'migrate_legacy_env_value BDAG_STACK_SERVICES "pool-db,bdag-miner-node-1,asic-pool" "postgres,node,pool"',
            installer,
        )

    def test_release_installer_generates_current_runtime_topology(self) -> None:
        compose = read("docker-compose.yml")
        installer = read("ops/release-install.sh")

        self.assertIn("  pool:", compose)
        self.assertIn("container_name: pool", compose)
        self.assertIn("  node:", compose)
        self.assertIn("container_name: node", compose)
        self.assertIn("container_name: postgres", compose)
        self.assertIn("NODE_RPC_URLS: ${NODE_RPC_URLS:-http://node:38131}", compose)
        self.assertIn("BDAG_STACK_SERVICES: postgres,node,pool", compose)
        self.assertIn('set_env_value .env BDAG_NODE_SERVICES "node"', installer)
        self.assertIn('set_env_value .env BDAG_STACK_SERVICES "postgres,node,pool"', installer)
        self.assertIn('set_env_value .env POOL_RPC_BACKENDS "node=http://node:38131"', installer)

    def test_watchdogs_default_to_current_names_with_legacy_compatibility(self) -> None:
        pool_ops = read("ops/pool_ops.py")
        sampler = read("ops/status_sampler.py")
        node_guard = read("ops/node_child_guard.py")
        host_guard = read("host/mining-appliance/bdag-node-child-guard")
        peer_refresh = read("ops/update-local-peers.py")

        self.assertIn('POOL_CONTAINER = os.environ.get("BDAG_POOL_CONTAINER", "pool")', pool_ops)
        self.assertIn('POOL_DB_CONTAINER = os.environ.get("BDAG_POOL_DB_CONTAINER", "postgres")', pool_ops)
        self.assertIn('NODES = split_env_list("BDAG_NODE_SERVICES", "node")', pool_ops)
        self.assertIn('"postgres,node,pool"', pool_ops)
        self.assertIn('config_value("BDAG_NODE_SERVICES", "node")', sampler)
        self.assertIn('DEFAULT_NODE_CHILD_GUARD_NODES = "node,bdag-miner-node-1"', node_guard)
        self.assertIn('DEFAULT_NODE_CHILD_GUARD_NODES = "node,bdag-miner-node-1"', host_guard)
        self.assertIn("blockdag-node", node_guard)
        self.assertIn("blockdag-node", host_guard)
        self.assertIn('DEFAULT_ACTIVE_NODE_SERVICES = ["node"]', peer_refresh)

    def test_validator_locks_current_topology_into_build_checks(self) -> None:
        validator = read("scripts/validate-pi5-restart-hardening.sh")

        self.assertIn('need_grep \'BDAG_STACK_SERVICES=postgres,node,pool\' ".env.example"', validator)
        self.assertIn('need_grep \'BDAG_NODE_SERVICES: node\' "docker-compose.yml"', validator)
        self.assertIn('need_grep \'container_name: postgres\' "docker-compose.yml"', validator)
        self.assertIn('need_file "ops/tests/test_stack_naming_coherence.py"', validator)


if __name__ == "__main__":
    unittest.main()
