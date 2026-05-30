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

    def test_adds_submit_hardening_flags_to_each_existing_pool_service(self) -> None:
        compose = """services:
  asic-pool:
    environment:
      NODE_RPC_URLS: http://rpc-failover:38131
      POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT: ${POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT:-true}
      NODE_RPC_USER: ${NODE_RPC_USER:-test}
  asic-pool-hector:
    environment:
      NODE_RPC_URLS: http://rpc-failover:38131
      NODE_RPC_USER: ${NODE_RPC_USER:-test}
  bdag-miner-node-1:
    environment:
      NODE_RPC_URLS: unused
"""

        result = compose_migrations.ensure_pool_submit_hardening_flags(compose)

        self.assertTrue(result.changed)
        self.assertEqual(13, result.inserted_count)
        self.assertIn(
            "      POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT: ${POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT:-true}\n"
            "      POOL_SUBMIT_STALE_BLOCK_CANDIDATES: ${POOL_SUBMIT_STALE_BLOCK_CANDIDATES:-false}\n"
            "      POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED: ${POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED:-true}\n"
            "      POOL_SUBMIT_BLOCK_HEADER_V3_ENABLED: ${POOL_SUBMIT_BLOCK_HEADER_V3_ENABLED:-true}\n"
            "      POOL_TEMPLATE_VALIDITY_CONTRACT_ENABLED: ${POOL_TEMPLATE_VALIDITY_CONTRACT_ENABLED:-true}\n"
            "      POOL_BLOCK_CANDIDATE_POLICY: ${POOL_BLOCK_CANDIDATE_POLICY:-contract}\n"
            "      POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD: ${POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD:-1}\n"
            "      NODE_RPC_USER:",
            result.text,
        )
        self.assertIn(
            "  asic-pool-hector:\n"
            "    environment:\n"
            "      NODE_RPC_URLS: http://rpc-failover:38131\n"
            "      POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT: ${POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT:-true}\n"
            "      POOL_SUBMIT_STALE_BLOCK_CANDIDATES: ${POOL_SUBMIT_STALE_BLOCK_CANDIDATES:-false}\n"
            "      POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED: ${POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED:-true}\n"
            "      POOL_SUBMIT_BLOCK_HEADER_V3_ENABLED: ${POOL_SUBMIT_BLOCK_HEADER_V3_ENABLED:-true}\n"
            "      POOL_TEMPLATE_VALIDITY_CONTRACT_ENABLED: ${POOL_TEMPLATE_VALIDITY_CONTRACT_ENABLED:-true}\n"
            "      POOL_BLOCK_CANDIDATE_POLICY: ${POOL_BLOCK_CANDIDATE_POLICY:-contract}\n"
            "      POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD: ${POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD:-1}\n"
            "      NODE_RPC_USER:",
            result.text,
        )
        self.assertNotIn(
            "bdag-miner-node-1:\n"
            "    environment:\n"
            "      POOL_SUBMIT_STALE_BLOCK_CANDIDATES",
            result.text,
        )

    def test_existing_submit_hardening_flags_are_noop(self) -> None:
        compose = """services:
  asic-pool:
    environment:
      NODE_RPC_URLS: http://rpc-failover:38131
      POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT: ${POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT:-true}
      POOL_SUBMIT_STALE_BLOCK_CANDIDATES: ${POOL_SUBMIT_STALE_BLOCK_CANDIDATES:-false}
      POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED: ${POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED:-true}
      POOL_SUBMIT_BLOCK_HEADER_V3_ENABLED: ${POOL_SUBMIT_BLOCK_HEADER_V3_ENABLED:-true}
      POOL_TEMPLATE_VALIDITY_CONTRACT_ENABLED: ${POOL_TEMPLATE_VALIDITY_CONTRACT_ENABLED:-true}
      POOL_BLOCK_CANDIDATE_POLICY: ${POOL_BLOCK_CANDIDATE_POLICY:-contract}
      POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD: ${POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD:-1}
"""

        result = compose_migrations.ensure_pool_submit_hardening_flags(compose)

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
