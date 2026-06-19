#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import live_efficiency_monitor as monitor  # noqa: E402


class LiveEfficiencyMonitorTests(unittest.TestCase):
    def test_readiness_state_blocks_until_backend_ready(self) -> None:
        metrics = """
pool_rpc_backend_node_health_mineable{node="node",pool_id="0"} 0
pool_rpc_backend_node_health_submit_ready{node="node",pool_id="0"} 0
pool_rpc_backend_node_health_p2p_mining_fresh{node="node",pool_id="0"} 0
"""

        state = monitor.readiness_state(
            metrics,
            {
                "active_connections": 3,
                "authorized_connections": 3,
                "ready_connections": 3,
                "reason_code": "ok",
            },
        )

        self.assertFalse(state["ready"])
        self.assertEqual("backend-not-mineable", state["reason"])
        self.assertEqual(0.0, state["mineable"])
        self.assertEqual(3, state["ready_connections"])

    def test_readiness_state_allows_ready_mining_window(self) -> None:
        metrics = """
pool_rpc_backend_node_health_mineable{node="node",pool_id="0"} 1
pool_rpc_backend_node_health_submit_ready{node="node",pool_id="0"} 1
pool_rpc_backend_node_health_p2p_mining_fresh{node="node",pool_id="0"} 1
"""

        state = monitor.readiness_state(
            metrics,
            {
                "active_connections": 4,
                "authorized_connections": 4,
                "ready_connections": 4,
                "reason_code": "ok",
            },
        )

        self.assertTrue(state["ready"])
        self.assertEqual("ready", state["reason"])
        self.assertEqual(1.0, state["mineable"])
        self.assertEqual(1.0, state["submit_ready"])
        self.assertEqual(1.0, state["p2p_fresh"])


if __name__ == "__main__":
    unittest.main()
