#!/usr/bin/env python3
import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import compose_migrations  # noqa: E402


class RuntimeComposeMigrationTests(unittest.TestCase):
    def test_inserts_duplicate_safe_flag_into_generated_pool_services(self) -> None:
        compose = """# BDAG_GENERATED_PI5_RUNTIME_COMPOSE=1
services:
  asic-pool:
    image: pool
    environment:
      NODE_RPC_URL: http://rpc-failover:38131
      NODE_RPC_URLS: http://rpc-failover:38131
      NODE_RPC_USER: ${NODE_RPC_USER:-test}
  asic-pool-hector:
    image: pool
    environment:
      NODE_RPC_URL: http://rpc-failover:38131
      NODE_RPC_URLS: http://rpc-failover:38131
      NODE_RPC_PASS: ${NODE_RPC_PASS:-test}
  bdag-miner-node-1:
    image: node
    environment:
      NODE_RPC_URLS: unused
"""

        result = compose_migrations.ensure_duplicate_safe_submit_flag(compose)

        self.assertTrue(result.changed)
        self.assertEqual(2, result.inserted_count)
        self.assertIn(
            "      NODE_RPC_URLS: http://rpc-failover:38131\n"
            "      POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT: ${POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT:-true}\n"
            "      NODE_RPC_USER:",
            result.text,
        )
        self.assertNotIn("bdag-miner-node-1:\n    image: node\n    environment:\n      POOL_DUPLICATE", result.text)

    def test_existing_duplicate_safe_flag_is_noop(self) -> None:
        compose = """services:
  asic-pool:
    environment:
      NODE_RPC_URLS: http://rpc-failover:38131
      POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT: ${POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT:-true}
"""

        result = compose_migrations.ensure_duplicate_safe_submit_flag(compose)

        self.assertFalse(result.changed)
        self.assertEqual(0, result.inserted_count)
        self.assertEqual(compose, result.text)

    def test_missing_pool_service_is_reported_as_unmodified(self) -> None:
        compose = """services:
  bdag-miner-node-1:
    environment:
      NODE_RPC_URLS: unused
"""

        result = compose_migrations.ensure_duplicate_safe_submit_flag(compose)

        self.assertFalse(result.changed)
        self.assertEqual(0, result.inserted_count)
        self.assertNotIn("POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT", result.text)


if __name__ == "__main__":
    unittest.main()
