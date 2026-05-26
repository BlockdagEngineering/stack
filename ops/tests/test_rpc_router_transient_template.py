#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import rpc_router  # noqa: E402


class RpcRouterTransientTemplateTests(unittest.TestCase):
    def test_transient_initial_download_probe_loses_to_clean_node_under_pool_pressure(self) -> None:
        status = {
            "nodes": {
                "bdag-miner-node-1": {
                    "child_running": True,
                    "latest_block": 100,
                    "last_import_age_seconds": 0,
                    "template_probe_sample_count": 1,
                    "template_probe_error_count": 0,
                },
                "bdag-miner-node-2": {
                    "child_running": True,
                    "latest_block": 100,
                    "last_import_age_seconds": 0,
                    "template_probe_sample_count": 1,
                    "template_probe_error_count": 1,
                    "template_probe_benign_tx_template_error": True,
                },
            },
            "pool_health": {
                "block_submit_error_count": 10,
                "block_submit_success_count": 2,
                "stale_job_candidate_count": 2,
                "submit_count": 20,
                "valid_share_count": 5,
            },
        }

        decision = rpc_router.recommend_rpc_primary(status, current_primary="bdag-miner-node-2")

        self.assertEqual(decision["recommended_primary"], "bdag-miner-node-1")
        self.assertTrue(decision["should_switch"])
        self.assertIn(
            "template-probe-transient-initial-download-1-1",
            decision["scores"]["bdag-miner-node-2"]["reasons"],
        )


if __name__ == "__main__":
    unittest.main()
