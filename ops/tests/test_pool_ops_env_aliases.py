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

    def test_goldshell_probe_drift_warning_is_empty_when_disabled(self) -> None:
        with mock.patch.object(pool_ops, "POOL_ENV_FILE", pathlib.Path("/nonexistent/pool-ops-test.env")), mock.patch.dict(
            pool_ops.os.environ,
            {"POOL_STRATUM_SERVER_FIRST_DIFFICULTY_PROBE": "false"},
            clear=True,
        ):
            self.assertEqual("", pool_ops.goldshell_server_first_probe_drift_warning({}))

    def test_goldshell_probe_drift_warning_detects_env_true(self) -> None:
        with mock.patch.object(pool_ops, "POOL_ENV_FILE", pathlib.Path("/nonexistent/pool-ops-test.env")), mock.patch.dict(
            pool_ops.os.environ,
            {"POOL_STRATUM_SERVER_FIRST_DIFFICULTY_PROBE": "true"},
            clear=True,
        ):
            warning = pool_ops.goldshell_server_first_probe_drift_warning({})

        self.assertIn("POOL_STRATUM_SERVER_FIRST_DIFFICULTY_PROBE is enabled", warning)
        self.assertIn(".env=true", warning)
        self.assertIn("disconnect before authorize", warning)

    def test_goldshell_probe_drift_warning_detects_running_container_true(self) -> None:
        containers = {
            pool_ops.POOL_CONTAINER: {
                "pool_stratum_server_first_difficulty_probe": "true",
                "pool_stratum_server_first_difficulty_probe_enabled": True,
            }
        }
        with mock.patch.object(pool_ops, "POOL_ENV_FILE", pathlib.Path("/nonexistent/pool-ops-test.env")), mock.patch.dict(
            pool_ops.os.environ,
            {"POOL_STRATUM_SERVER_FIRST_DIFFICULTY_PROBE": "false"},
            clear=True,
        ):
            warning = pool_ops.goldshell_server_first_probe_drift_warning(containers)

        self.assertIn("running container=true", warning)
        self.assertIn("Goldshell cloud-box/MCB production must keep it false", warning)


if __name__ == "__main__":
    unittest.main()
