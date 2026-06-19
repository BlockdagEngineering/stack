#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_timing_calibrator as calibrator  # noqa: E402


class PoolTimingCalibratorTests(unittest.TestCase):
    def test_discovers_current_runtime_knobs_from_metrics(self) -> None:
        metrics = calibrator.parse_metrics(
            """
pool_block_timing_controller_job_age_ms{pool_id="0"} 250
pool_block_timing_controller_template_ttl_ms{pool_id="0"} 100
"""
        )

        knobs = calibrator.discover_current_knobs(metrics)

        self.assertEqual(knobs.age_ms, 250)
        self.assertEqual(knobs.ttl_ms, 100)
        self.assertTrue(knobs.allow_multiple)

    def test_calibration_gate_blocks_when_backend_not_ready(self) -> None:
        metrics = calibrator.parse_metrics(
            """
pool_rpc_backend_node_health_mineable{node="node",pool_id="0"} 0
pool_rpc_backend_node_health_submit_ready{node="node",pool_id="0"} 0
pool_rpc_backend_node_health_p2p_mining_fresh{node="node",pool_id="0"} 0
"""
        )

        ready, reason = calibrator.calibration_gate(
            metrics,
            {"active_connections": 4, "ready_connections": 4},
        )

        self.assertFalse(ready)
        self.assertEqual(reason, "backend-not-mineable")

    def test_calibration_gate_requires_ready_miners(self) -> None:
        metrics = calibrator.parse_metrics(
            """
pool_rpc_backend_node_health_mineable{node="node",pool_id="0"} 1
pool_rpc_backend_node_health_submit_ready{node="node",pool_id="0"} 1
pool_rpc_backend_node_health_p2p_mining_fresh{node="node",pool_id="0"} 1
"""
        )

        ready, reason = calibrator.calibration_gate(
            metrics,
            {"active_connections": 4, "ready_connections": 0},
        )

        self.assertFalse(ready)
        self.assertEqual(reason, "no-ready-miners")

    def test_calibration_gate_allows_real_mining_window(self) -> None:
        metrics = calibrator.parse_metrics(
            """
pool_rpc_backend_node_health_mineable{node="node",pool_id="0"} 1
pool_rpc_backend_node_health_submit_ready{node="node",pool_id="0"} 1
pool_rpc_backend_node_health_p2p_mining_fresh{node="node",pool_id="0"} 1
"""
        )

        ready, reason = calibrator.calibration_gate(
            metrics,
            {"active_connections": 4, "ready_connections": 4},
        )

        self.assertTrue(ready)
        self.assertEqual(reason, "ready")


if __name__ == "__main__":
    unittest.main()
