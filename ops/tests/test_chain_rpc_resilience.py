#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class ChainRpcResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_retries = pool_ops.NODE_CHAIN_RPC_RETRIES
        self.old_mining_rpc_call = pool_ops.mining_rpc_call
        self.old_sleep = pool_ops.time.sleep
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.NODE_CHAIN_RPC_RETRIES = self.old_retries
        pool_ops.mining_rpc_call = self.old_mining_rpc_call
        pool_ops.time.sleep = self.old_sleep

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
        self.assertEqual([method for method, _timeout in calls], ["getBlockCount", "getBlockCount", "getMainChainHeight"])


if __name__ == "__main__":
    unittest.main()
