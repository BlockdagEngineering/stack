#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_job_timing_optimizer as optimizer  # noqa: E402


class PoolJobTimingOptimizerTests(unittest.TestCase):
    def test_parse_prometheus_groups_pool_timing_metrics(self) -> None:
        metrics = """
pool_shares_accepted_total{pool_id="0"} 100
pool_shares_rejected_total{pool_id="0",reason="invalidated_job"} 5
pool_stale_shares_acked_total{pool_id="0",reason="invalidated_job"} 7
pool_block_submit_outcomes_total{pool_id="0",outcome="accepted",reason="ok"} 3
pool_block_submit_outcomes_total{pool_id="0",outcome="rejected",reason="tip-overdue"} 1
pool_blocks_rejected_by_node_total{pool_id="0",reason="tip-overdue"} 1
pool_template_broadcasts_total{pool_id="0"} 12
pool_template_fetch_duration_seconds_sum{pool_id="0"} 0.4
pool_template_fetch_duration_seconds_count{pool_id="0"} 2
pool_rpc_backend_submit_duration_seconds_sum{backend="node1",result="ok"} 0.2
pool_rpc_backend_submit_duration_seconds_count{backend="node1",result="ok"} 2
pool_job_health_ok{pool_id="0"} 1
"""

        parsed = optimizer.parse_prometheus(metrics)

        self.assertEqual(parsed["shares_accepted"], 100)
        self.assertEqual(parsed["share_rejected_by_reason"], {"invalidated_job": 5})
        self.assertEqual(parsed["stale_shares_acked_by_reason"], {"invalidated_job": 7})
        self.assertEqual(parsed["block_submit_by_outcome_reason"], {"accepted:ok": 3, "rejected:tip-overdue": 1})
        self.assertEqual(parsed["blocks_rejected_by_node"], {"tip-overdue": 1})
        self.assertEqual(parsed["pool_template_broadcasts_total"], 12)
        self.assertEqual(parsed["pool_job_health_ok"], 1)

    def test_summarize_window_scores_source_truth_block_outcomes(self) -> None:
        before = optimizer.parse_prometheus(
            """
pool_shares_accepted_total 100
pool_shares_rejected_total{reason="invalidated_job"} 10
pool_block_submit_outcomes_total{outcome="accepted",reason="ok"} 3
pool_block_submit_outcomes_total{outcome="rejected",reason="tip-overdue"} 1
pool_template_fetch_duration_seconds_sum 0.4
pool_template_fetch_duration_seconds_count 2
pool_rpc_backend_submit_duration_seconds_sum{backend="node1",result="ok"} 0.2
pool_rpc_backend_submit_duration_seconds_count{backend="node1",result="ok"} 2
"""
        )
        after = optimizer.parse_prometheus(
            """
pool_shares_accepted_total 160
pool_shares_rejected_total{reason="invalidated_job"} 15
pool_stale_shares_acked_total{reason="invalidated_job"} 4
pool_block_submit_outcomes_total{outcome="accepted",reason="ok"} 5
pool_block_submit_outcomes_total{outcome="rejected",reason="tip-overdue"} 1
pool_template_broadcasts_total 20
pool_template_fetch_duration_seconds_sum 1.0
pool_template_fetch_duration_seconds_count 4
pool_rpc_backend_submit_duration_seconds_sum{backend="node1",result="ok"} 0.6
pool_rpc_backend_submit_duration_seconds_count{backend="node1",result="ok"} 4
"""
        )

        summary = optimizer.summarize_window(before, after, 3600, {"overall": "ok", "can_submit_blocks": True})

        self.assertEqual(summary["shares"]["accepted"], 60)
        self.assertEqual(summary["shares"]["rejected"], 5)
        self.assertEqual(summary["shares"]["stale_acked_by_reason"], {"invalidated_job": 4})
        self.assertEqual(summary["blocks"]["accepted"], 2)
        self.assertEqual(summary["blocks"]["rejected"], 0)
        self.assertEqual(summary["blocks"]["accepted_per_hour"], 2)
        self.assertEqual(summary["templates"]["fetch_avg_seconds"], 0.3)
        self.assertEqual(summary["submit"]["avg_seconds"], 0.2)
        self.assertGreater(summary["score"], 0)


if __name__ == "__main__":
    unittest.main()
