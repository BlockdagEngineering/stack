#!/usr/bin/env python3

import os
import pathlib
import sys
import tempfile
import unittest
from decimal import Decimal

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
        self.old_background_maintenance_decision = pool_ops.background_maintenance_decision
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.read_json_file = self.old_read_json_file
        pool_ops.read_global_history = self.old_read_global_history
        pool_ops.seconds_since_epoch = self.old_seconds_since_epoch
        pool_ops.global_evm_rpc_urls = self.old_global_evm_rpc_urls
        pool_ops.json_rpc_call = self.old_json_rpc_call
        pool_ops.background_maintenance_decision = self.old_background_maintenance_decision

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
        pool_ops.background_maintenance_decision = lambda task: {"allowed": True, "task": task, "reasons": []}

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


class LocalPoolSourceTruthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            name: getattr(pool_ops, name)
            for name in (
                "pool_db_json",
                "node_rpc_urls",
                "mining_rpc_call",
                "local_worker_identity_map",
                "LOCAL_POOL_SOURCE_TRUTH_LIMIT",
                "LOCAL_POOL_SOURCE_TRUTH_WORKERS",
                "LOCAL_POOL_SOURCE_TRUTH_SETTLE_SECONDS",
            )
        }
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        for name, value in self.originals.items():
            setattr(pool_ops, name, value)

    def test_local_pool_overlay_counts_only_confirmed_blue_coinbase_blocks(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        rows = [
            {"node_block_hash": "0xblue", "submitted_at": "2026-05-27T13:00:00Z"},
            {"node_block_hash": "0xpending", "submitted_at": "2026-05-27T13:01:00Z"},
            {"node_block_hash": "0xzero", "submitted_at": "2026-05-27T13:02:00Z"},
        ]
        blue = {"0xblue": 1, "0xpending": 2, "0xzero": 1}
        coinbase = {"0xblue": wallet, "0xpending": wallet, "0xzero": pool_ops.ZERO_ETH_ADDRESS}

        pool_ops.pool_db_json = lambda _sql: rows
        pool_ops.node_rpc_urls = lambda: [("node2", "http://node2:38131")]
        pool_ops.local_worker_identity_map = lambda: {wallet.lower(): {"display_label": "Achilles-0b5"}}
        pool_ops.LOCAL_POOL_SOURCE_TRUTH_LIMIT = 100
        pool_ops.LOCAL_POOL_SOURCE_TRUTH_WORKERS = 1
        pool_ops.LOCAL_POOL_SOURCE_TRUTH_SETTLE_SECONDS = 90

        def fake_rpc(_url: str, method: str, params: list[object], timeout: float = 0) -> object:
            block_hash = str(params[0])
            if method == "isBlue":
                return blue[block_hash]
            if method == "getCoinbaseAddress":
                return coinbase[block_hash]
            if method == "getBlockV2":
                return {"confirmations": 3, "order": 123} if blue[block_hash] == 1 else {"confirmations": 0}
            raise AssertionError(method)

        pool_ops.mining_rpc_call = fake_rpc

        clusters = pool_ops.collect_local_pool_global_clusters(
            scan_window_seconds=3600,
            total_global_blocks=100,
            scan_window_hours=Decimal("1"),
            price={"usd": "0.01", "zar": "0.18"},
            avg_reward_bdag=Decimal("10"),
        )

        self.assertEqual(len(clusters), 1)
        cluster = clusters[0]
        self.assertEqual(cluster["address"], wallet.lower())
        self.assertEqual(cluster["pool_name"], "Achilles-0b5")
        self.assertEqual(cluster["source_truth"], "bdag-rpc-isBlue")
        self.assertEqual(cluster["blocks"], 1)
        self.assertEqual(cluster["found_blocks"], 1)
        self.assertEqual(cluster["shares"], 1)
        self.assertEqual(cluster["accepted_submissions"], 2)
        self.assertEqual(cluster["pending_submissions"], 1)
        self.assertEqual(cluster["credited_bdag"], "10.00")
        self.assertEqual(cluster["share_percent"], "1.00")


class GlobalHistoryWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            name: getattr(pool_ops, name)
            for name in (
                "GLOBAL_HISTORY_FILE",
                "GLOBAL_HISTORY_STATE_FILE",
                "GLOBAL_HISTORY_LIMIT",
                "GLOBAL_HISTORY_COMPACT_MULTIPLIER",
                "ensure_runtime",
            )
        }
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        for name, value in self.originals.items():
            setattr(pool_ops, name, value)

    def test_global_history_appends_and_compacts_only_after_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pool_ops.GLOBAL_HISTORY_FILE = root / "global-history.jsonl"
            pool_ops.GLOBAL_HISTORY_STATE_FILE = root / "global-history-state.json"
            pool_ops.GLOBAL_HISTORY_LIMIT = 3
            pool_ops.GLOBAL_HISTORY_COMPACT_MULTIPLIER = 2
            pool_ops.ensure_runtime = lambda: None

            for block in range(6):
                pool_ops.record_global_snapshot({"latest_block": block})

            self.assertEqual(len(pool_ops.GLOBAL_HISTORY_FILE.read_text(encoding="utf-8").splitlines()), 6)
            self.assertEqual([row["latest_block"] for row in pool_ops.read_global_history()], [0, 1, 2, 3, 4, 5])

            pool_ops.record_global_snapshot({"latest_block": 6})

            self.assertEqual([row["latest_block"] for row in pool_ops.read_global_history()], [4, 5, 6])
            state = pool_ops.read_json_file(pool_ops.GLOBAL_HISTORY_STATE_FILE, {})
            self.assertEqual(state["row_count"], 3)
            self.assertTrue(state["compacted"])


class EarningsEvmRpcSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_global_evm_rpc_urls = pool_ops.global_evm_rpc_urls
        self.old_node_rpc_urls = pool_ops.node_rpc_urls
        self.old_named_urls_from_env = pool_ops.named_urls_from_env
        self.old_json_rpc_balance = pool_ops.json_rpc_balance
        self.old_adaptive_worker_count = pool_ops.adaptive_worker_count
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.global_evm_rpc_urls = self.old_global_evm_rpc_urls
        pool_ops.node_rpc_urls = self.old_node_rpc_urls
        pool_ops.named_urls_from_env = self.old_named_urls_from_env
        pool_ops.json_rpc_balance = self.old_json_rpc_balance
        pool_ops.adaptive_worker_count = self.old_adaptive_worker_count

    def test_wallet_balances_use_evm_rpc_not_mining_rpc(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        called_urls: list[str] = []
        pool_ops.global_evm_rpc_urls = lambda: [("node2-evm", "http://172.22.0.5:18545")]
        pool_ops.node_rpc_urls = lambda: [("node2-mining", "http://127.0.0.1:38131")]
        pool_ops.named_urls_from_env = lambda _name, _defaults: []
        pool_ops.adaptive_worker_count = lambda *_args, **_kwargs: 1

        def fake_balance(url: str, _address: str, timeout: float = 6.0) -> dict[str, str]:
            called_urls.append(url)
            return {"wei": "1000000000000000000", "bdag": "1.00"}

        pool_ops.json_rpc_balance = fake_balance

        payload = pool_ops.collect_wallet_balances_for_addresses([wallet])

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(called_urls, ["http://172.22.0.5:18545"])
        self.assertEqual(payload["addresses"][0]["type"], "evm-rpc")


class GlobalLocalPoolOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_pool_db_json = pool_ops.pool_db_json
        self.old_read_miner_registry = pool_ops.read_miner_registry
        self.old_read_global_pool_labels = pool_ops.read_global_pool_labels
        self.old_node_rpc_urls = pool_ops.node_rpc_urls
        self.old_mining_rpc_call = pool_ops.mining_rpc_call
        self.old_source_truth_limit = pool_ops.LOCAL_POOL_SOURCE_TRUTH_LIMIT
        self.old_source_truth_workers = pool_ops.LOCAL_POOL_SOURCE_TRUTH_WORKERS
        self.old_source_truth_settle = pool_ops.LOCAL_POOL_SOURCE_TRUTH_SETTLE_SECONDS
        self.old_nodes = pool_ops.NODES
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.pool_db_json = self.old_pool_db_json
        pool_ops.read_miner_registry = self.old_read_miner_registry
        pool_ops.read_global_pool_labels = self.old_read_global_pool_labels
        pool_ops.node_rpc_urls = self.old_node_rpc_urls
        pool_ops.mining_rpc_call = self.old_mining_rpc_call
        pool_ops.LOCAL_POOL_SOURCE_TRUTH_LIMIT = self.old_source_truth_limit
        pool_ops.LOCAL_POOL_SOURCE_TRUTH_WORKERS = self.old_source_truth_workers
        pool_ops.LOCAL_POOL_SOURCE_TRUTH_SETTLE_SECONDS = self.old_source_truth_settle
        pool_ops.NODES = self.old_nodes

    def test_local_pool_source_truth_row_uses_asic_worker_identity(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        pool_ops.NODES = ["bdag-miner-node-2"]
        pool_ops.pool_db_json = lambda _sql: [
            {
                "node_block_hash": "0xblue",
                "submitted_at": "2026-05-27T00:01:00Z",
            }
        ]
        pool_ops.node_rpc_urls = lambda: [("node2", "http://node2:38131")]
        pool_ops.LOCAL_POOL_SOURCE_TRUTH_LIMIT = 100
        pool_ops.LOCAL_POOL_SOURCE_TRUTH_WORKERS = 1
        pool_ops.LOCAL_POOL_SOURCE_TRUTH_SETTLE_SECONDS = 90

        def fake_rpc(_url: str, method: str, params: list[object], timeout: float = 0) -> object:
            if method == "isBlue":
                return 1
            if method == "getCoinbaseAddress":
                return wallet
            if method == "getBlockV2":
                return {"confirmations": 3, "order": 123}
            raise AssertionError(method)

        pool_ops.mining_rpc_call = fake_rpc
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {
                    "ip": "192.168.50.177",
                    "mac": "28:e2:97:1e:c0:b5",
                    "display_name": "Achilles",
                    "device_type": "asic",
                    "last_workers": [wallet],
                }
            ]
        }

        rows = pool_ops.collect_local_pool_global_clusters(
            scan_window_seconds=120,
            total_global_blocks=100,
            scan_window_hours=Decimal("0.0333333333"),
            price={"status": "ok", "usd": "0.01", "zar": "0.18"},
            avg_reward_bdag=Decimal("10"),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["address"], wallet.lower())
        self.assertEqual(rows[0]["pool_name"], "Achilles-0b5")
        self.assertEqual(rows[0]["nodes"], ["bdag-miner-node-2"])
        self.assertTrue(rows[0]["local_pool"])
        self.assertEqual(rows[0]["source_truth"], "bdag-rpc-isBlue")
        self.assertEqual(rows[0]["shares"], 1)
        self.assertEqual(rows[0]["credit_blocks"], 1)
        self.assertEqual(rows[0]["found_blocks"], 1)
        self.assertEqual(rows[0]["credited_bdag"], "10.00")

    def test_local_pool_overlay_is_preserved_when_address_matches_chain_cluster(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        merged = pool_ops.merge_global_local_pool_clusters(
            [{"address": wallet, "blocks": 2, "last_seen_at": "2026-05-27T00:01:00Z"}],
            [
                {
                    "address": wallet,
                    "blocks": 3,
                    "pool_name": "Achilles-0b5",
                    "local_pool": True,
                    "source": "local-pool-bdag-rpc",
                    "source_truth": "bdag-rpc-isBlue",
                    "credit_blocks": 3,
                    "accepted_submissions": 4,
                    "pending_submissions": 1,
                }
            ],
        )

        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0]["local_pool"])
        self.assertEqual(merged[0]["pool_name"], "Achilles-0b5")
        self.assertEqual(merged[0]["source"], "on-chain+local-pool-bdag-rpc")
        self.assertEqual(merged[0]["source_truth"], "bdag-rpc-isBlue")
        self.assertNotIn("credit_blocks", merged[0])
        self.assertEqual(merged[0]["local_credit_blocks"], 3)
        self.assertEqual(merged[0]["accepted_submissions"], 4)
        self.assertEqual(merged[0]["pending_submissions"], 1)

    def test_local_pool_overlay_preserves_local_display_amounts(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        merged = pool_ops.merge_global_local_pool_clusters(
            [{"address": wallet, "blocks": 2, "estimated_usd": "$old", "last_seen_at": "2026-05-27T00:01:00Z"}],
            [
                {
                    "address": wallet,
                    "blocks": 5,
                    "shares": 6,
                    "local_pool": True,
                    "source": "local-pool-bdag-rpc",
                    "estimated_usd": "$new",
                    "estimated_usd_avg_hour": "$1.23",
                }
            ],
        )

        self.assertEqual(merged[0]["local_blocks"], 5)
        self.assertEqual(merged[0]["local_shares"], 6)
        self.assertEqual(merged[0]["local_estimated_usd"], "$new")
        self.assertEqual(merged[0]["local_estimated_usd_avg_hour"], "$1.23")

    def test_require_spendable_eth_address_rejects_zero_worker(self) -> None:
        with self.assertRaises(ValueError):
            pool_ops.require_spendable_eth_address(pool_ops.ZERO_ETH_ADDRESS, "worker_user")

    def test_local_pool_identity_is_not_replaced_by_static_global_label(self) -> None:
        wallet = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        pool_ops.read_global_pool_labels = lambda: {wallet.lower(): "Pipin"}

        payload = pool_ops.annotate_global_pool_labels(
            {
                "clusters": [
                    {
                        "address": wallet,
                        "pool_name": "Achilles-0b5",
                        "local_pool": True,
                    }
                ],
                "history": [],
            }
        )

        self.assertEqual(payload["clusters"][0]["pool_name"], "Achilles-0b5")
        self.assertEqual(payload["clusters"][0]["pool_label"], "Achilles-0b5 (0xA1Ee...7DFc)")


class GlobalMaintenanceBackoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_read_json_file = pool_ops.read_json_file
        self.old_read_global_history = pool_ops.read_global_history
        self.old_background_maintenance_decision = pool_ops.background_maintenance_decision
        self.old_global_evm_rpc_urls = pool_ops.global_evm_rpc_urls
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.read_json_file = self.old_read_json_file
        pool_ops.read_global_history = self.old_read_global_history
        pool_ops.background_maintenance_decision = self.old_background_maintenance_decision
        pool_ops.global_evm_rpc_urls = self.old_global_evm_rpc_urls

    def test_global_scan_defers_to_stale_cache_when_maintenance_backoff_blocks_work(self) -> None:
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

        def should_not_fetch_rpc() -> list[tuple[str, str]]:
            raise AssertionError("global EVM RPC discovery must not run while maintenance is deferred")

        pool_ops.read_json_file = fake_read_json_file
        pool_ops.read_global_history = lambda limit=None: [{"latest_block": 122, "clusters": []}]
        pool_ops.background_maintenance_decision = lambda task: {
            "allowed": False,
            "task": task,
            "reasons": ["chain catch-up has priority status=syncing remaining=42 threshold=0"],
        }
        pool_ops.global_evm_rpc_urls = should_not_fetch_rpc

        payload = pool_ops.collect_global_blockchain()

        self.assertEqual(payload["status"], "stale")
        self.assertTrue(payload["maintenance_deferred"])
        self.assertIn("global blockchain scan deferred", payload["error"])
        self.assertEqual(payload["latest_block"], 123)


if __name__ == "__main__":
    unittest.main()
