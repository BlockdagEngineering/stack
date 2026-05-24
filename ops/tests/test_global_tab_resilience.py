#!/usr/bin/env python3

import os
import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class GlobalTabRpcSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_env = os.environ.copy()
        self.old_nodes = pool_ops.NODES
        self.old_services = pool_ops.SERVICES
        self.old_pool_containers = pool_ops.POOL_CONTAINERS
        self.old_docker_container_ip = pool_ops.docker_container_ip
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)
        pool_ops.NODES = self.old_nodes
        pool_ops.SERVICES = self.old_services
        pool_ops.POOL_CONTAINERS = self.old_pool_containers
        pool_ops.docker_container_ip = self.old_docker_container_ip

    def test_global_uses_evm_rpc_even_when_node_rpc_is_mining_rpc(self) -> None:
        os.environ["BDAG_NODE_RPC_URLS"] = "node2=http://127.0.0.1:38131"
        for key in ("BDAG_GLOBAL_RPC_URLS", "BDAG_EVM_RPC_URLS", "WALLET_RPC_URLS"):
            os.environ.pop(key, None)
        pool_ops.NODES = ["bdag-miner-node-2"]
        pool_ops.SERVICES = ["bdag-miner-node-2"]
        pool_ops.POOL_CONTAINERS = []
        pool_ops.docker_container_ip = lambda name: "172.22.0.2" if name == "bdag-miner-node-2" else ""

        self.assertEqual(
            pool_ops.global_evm_rpc_urls(),
            [("bdag-miner-node-2", "http://172.22.0.2:18545")],
        )

    def test_global_rewrites_compose_service_hostname_for_host_dashboard(self) -> None:
        os.environ["BDAG_GLOBAL_RPC_URLS"] = "node2=http://bdag-miner-node-2:18545"
        pool_ops.NODES = ["bdag-miner-node-2"]
        pool_ops.SERVICES = ["bdag-miner-node-2"]
        pool_ops.POOL_CONTAINERS = []
        pool_ops.docker_container_ip = lambda name: "172.22.0.2" if name == "bdag-miner-node-2" else ""

        self.assertEqual(
            pool_ops.global_evm_rpc_urls(),
            [("node2", "http://172.22.0.2:18545")],
        )


class GlobalTabFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_read_json_file = pool_ops.read_json_file
        self.old_read_global_history = pool_ops.read_global_history
        self.old_seconds_since_epoch = pool_ops.seconds_since_epoch
        self.old_global_evm_rpc_urls = pool_ops.global_evm_rpc_urls
        self.old_json_rpc_call = pool_ops.json_rpc_call
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.read_json_file = self.old_read_json_file
        pool_ops.read_global_history = self.old_read_global_history
        pool_ops.seconds_since_epoch = self.old_seconds_since_epoch
        pool_ops.global_evm_rpc_urls = self.old_global_evm_rpc_urls
        pool_ops.json_rpc_call = self.old_json_rpc_call

    def test_global_returns_stale_cache_instead_of_raising_when_evm_rpc_fails(self) -> None:
        cached = {
            "status": "ok",
            "updated_at_epoch": 100,
            "latest_block": 123,
            "clusters": [{"address": "0xabc", "blocks": 1}],
            "fetch_errors": [],
        }

        def fake_read_json_file(path: pathlib.Path, fallback: object) -> object:
            if path == pool_ops.GLOBAL_CACHE_FILE:
                return cached
            return fallback

        pool_ops.read_json_file = fake_read_json_file
        pool_ops.read_global_history = lambda limit=None: [{"latest_block": 122, "clusters": []}]
        pool_ops.seconds_since_epoch = lambda: 999_999
        pool_ops.global_evm_rpc_urls = lambda: [("bad-node", "http://127.0.0.1:18545")]

        def fail_rpc(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("rpc unavailable")

        pool_ops.json_rpc_call = fail_rpc

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "stale")
        self.assertTrue(payload["cache_hit"])
        self.assertEqual(payload["latest_block"], 123)
        self.assertEqual(payload["clusters"][0]["address"], cached["clusters"][0]["address"])
        self.assertEqual(payload["clusters"][0]["blocks"], cached["clusters"][0]["blocks"])
        self.assertIn("unable to fetch latest global block height", payload["error"])


if __name__ == "__main__":
    unittest.main()
