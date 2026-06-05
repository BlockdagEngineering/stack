#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


ADDRESS = "0x1111111111111111111111111111111111111111"


class PoolOpsEnvAliasTests(unittest.TestCase):
    def test_compose_mining_pool_address_is_seeded_from_mining_address(self) -> None:
        with mock.patch.dict(pool_ops.os.environ, {"MINING_ADDRESS": ADDRESS}, clear=True):
            pool_ops.apply_stack_env_aliases()

            self.assertEqual(ADDRESS, pool_ops.os.environ["MINING_POOL_ADDRESS"])

    def test_mining_address_is_seeded_from_compose_mining_pool_address(self) -> None:
        with mock.patch.dict(pool_ops.os.environ, {"MINING_POOL_ADDRESS": ADDRESS}, clear=True):
            pool_ops.apply_stack_env_aliases()

            self.assertEqual(ADDRESS, pool_ops.os.environ["MINING_ADDRESS"])

    def test_read_env_value_accepts_mining_pool_address_alias(self) -> None:
        with mock.patch.dict(pool_ops.os.environ, {"MINING_POOL_ADDRESS": ADDRESS}, clear=True):
            self.assertEqual(ADDRESS, pool_ops.read_env_value("MINING_ADDRESS"))

    def test_host_bind_rewrites_loopback_rpc_urls(self) -> None:
        with mock.patch.dict(
            pool_ops.os.environ,
            {
                "BDAG_HOST_BIND_IP": "192.168.100.120",
                "BDAG_NODE_RPC_URLS": "node=http://127.0.0.1:38131",
                "BDAG_GLOBAL_CHAIN_RPC_URLS": "node=http://localhost:38131",
                "NODE_RPC_URLS": "http://node:38131",
            },
            clear=True,
        ):
            pool_ops.apply_host_bind_rpc_aliases()

            self.assertEqual("node=http://192.168.100.120:38131", pool_ops.os.environ["BDAG_NODE_RPC_URLS"])
            self.assertEqual(
                "node=http://192.168.100.120:38131",
                pool_ops.os.environ["BDAG_GLOBAL_CHAIN_RPC_URLS"],
            )
            self.assertEqual("http://node:38131", pool_ops.os.environ["NODE_RPC_URLS"])


if __name__ == "__main__":
    unittest.main()
