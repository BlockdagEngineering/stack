#!/usr/bin/env python3

import pathlib
import sys
import unittest
from datetime import datetime, timezone

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class ChainRpcResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_retries = pool_ops.NODE_CHAIN_RPC_RETRIES
        self.old_pool_rpc_refused_warn_seconds = pool_ops.POOL_RPC_REFUSED_WARN_SECONDS
        self.old_mining_rpc_call = pool_ops.mining_rpc_call
        self.old_sleep = pool_ops.time.sleep
        self.old_time = pool_ops.time.time
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.NODE_CHAIN_RPC_RETRIES = self.old_retries
        pool_ops.POOL_RPC_REFUSED_WARN_SECONDS = self.old_pool_rpc_refused_warn_seconds
        pool_ops.mining_rpc_call = self.old_mining_rpc_call
        pool_ops.time.sleep = self.old_sleep
        pool_ops.time.time = self.old_time

    def test_get_block_count_retries_once_before_marking_unavailable(self) -> None:
        pool_ops.NODE_CHAIN_RPC_RETRIES = 2
        pool_ops.time.sleep = lambda *_args, **_kwargs: None
        calls = []

        def fake_rpc(_url, method, _params, timeout):
            calls.append((method, timeout))
            if method == "getBlockCount" and len(calls) == 1:
                raise TimeoutError("timed out")
            if method == "getBlockCount":
                return "8656586"
            if method == "getMainChainHeight":
                return "7001831"
            raise AssertionError(method)

        pool_ops.mining_rpc_call = fake_rpc

        snapshot = pool_ops.node_chain_rpc_snapshot("node2", "http://node2:38131", timeout=8.0)

        self.assertEqual(snapshot["chain_rpc_error"], "")
        self.assertEqual(snapshot["chain_block_count"], 8656586)
        self.assertEqual(snapshot["chain_main_height"], 7001831)
        self.assertEqual(snapshot["chain_rpc_attempts"], 2)
        self.assertEqual(snapshot["chain_rpc_timeout_seconds"], 8.0)
        self.assertIsNotNone(snapshot["chain_rpc_latency_ms"])
        self.assertEqual([method for method, _timeout in calls], ["getBlockCount", "getBlockCount", "getMainChainHeight"])

    def test_unknown_sync_progress_preserves_chain_rpc_attempt_detail(self) -> None:
        pool_ops.NODE_CHAIN_RPC_RETRIES = 2
        pool_ops.time.sleep = lambda *_args, **_kwargs: None

        def fake_rpc(_url, method, _params, _timeout):
            if method == "getBlockCount":
                raise TimeoutError("timed out")
            raise AssertionError(method)

        pool_ops.mining_rpc_call = fake_rpc

        progress = pool_ops.node_sync_progress("node2", "http://node2:38131", timeout=8.0)

        self.assertEqual(progress["status"], "unknown")
        self.assertEqual(progress["chain_rpc_attempts"], 2)
        self.assertEqual(progress["chain_rpc_retry_limit"], 2)
        self.assertIn("after 2 attempt", progress["chain_rpc_error"])

    def test_rpc_refused_is_recent_only_inside_warning_window(self) -> None:
        now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        pool_ops.time.time = lambda: now
        pool_ops.POOL_RPC_REFUSED_WARN_SECONDS = 120

        fresh = pool_ops.parse_pool_log(
            "2026/05/25 11:59:15 GBT ERROR: connect: connection refused\n"
        )
        stale = pool_ops.parse_pool_log(
            "2026/05/25 11:55:00 GBT ERROR: connect: connection refused\n"
        )

        self.assertTrue(fresh["rpc_refused"])
        self.assertTrue(fresh["rpc_refused_recent"])
        self.assertEqual(fresh["last_rpc_refused_age_seconds"], 45)
        self.assertTrue(stale["rpc_refused"])
        self.assertFalse(stale["rpc_refused_recent"])
        self.assertEqual(stale["last_rpc_refused_age_seconds"], 300)

    def test_no_miner_sync_noise_includes_template_and_stale_rpc_noise(self) -> None:
        self.assertTrue(pool_ops.is_no_miner_sync_noise("bdag-miner-node-2 is refusing live mining template probes"))
        self.assertTrue(pool_ops.is_no_miner_sync_noise("pool recently saw RPC connection refused"))
        self.assertTrue(pool_ops.is_no_miner_sync_noise("pool is waiting for node sync to finish"))
        self.assertFalse(pool_ops.is_no_miner_sync_noise("pool has not accepted a valid share"))

    def test_parse_proc_pressure_extracts_io_wait_signal(self) -> None:
        parsed = pool_ops.parse_proc_pressure(
            "some avg10=12.34 avg60=2.00 avg300=0.50 total=123456\n"
            "full avg10=0.25 avg60=0.10 avg300=0.05 total=789\n"
        )

        self.assertEqual(parsed["some_avg10"], 12.34)
        self.assertEqual(parsed["full_avg10"], 0.25)

    def test_parse_proc_stat_cpu_extracts_iowait_counter(self) -> None:
        parsed = pool_ops.parse_proc_stat_cpu(
            "cpu  100 20 30 400 50 0 0 0 0 0\n"
            "cpu0 10 2 3 40 5 0 0 0 0 0\n"
        )

        self.assertEqual(parsed["total"], 600)
        self.assertEqual(parsed["idle"], 400)
        self.assertEqual(parsed["iowait"], 50)


if __name__ == "__main__":
    unittest.main()
