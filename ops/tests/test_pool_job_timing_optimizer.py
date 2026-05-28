#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_job_timing_optimizer as optimizer  # noqa: E402
import pool_adaptive_optimizer as adaptive  # noqa: E402


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


class PoolAdaptiveOptimizerTests(unittest.TestCase):
    def test_lane_health_uses_mac_identity_and_flags_weak_lanes(self) -> None:
        status = {
            "miner_health": {
                "lane_balance": {"identity_basis": "mac"},
                "miners": [
                    {
                        "identity_key": "mac:28:e2:97:1e:c0:b5",
                        "mac": "28:e2:97:1e:c0:b5",
                        "display_label": "Achilles-0b5",
                        "connected": True,
                        "work_percent": "80.00",
                        "expected_work_percent": "100.00",
                        "shares": 12,
                        "blocks_found": 3,
                        "debug": {"av_hashrate": 250.0, "hwerr_ratio": 0.03, "valid": 6},
                    }
                ],
            }
        }

        health = adaptive.lane_health(status)

        self.assertEqual(health["identity_basis"], "mac")
        self.assertEqual(health["connected_count"], 1)
        self.assertEqual(health["max_work_imbalance_percent"], 20.0)
        self.assertEqual(health["weak_lanes"][0]["identity_key"], "mac:28:e2:97:1e:c0:b5")

    def test_safety_rejects_unhealthy_runtime_and_high_iowait(self) -> None:
        args = adaptive.build_parser().parse_args([])
        status = {
            "overall": "degraded",
            "can_mine": True,
            "can_submit_blocks": True,
            "host_pressure": {"iowait_percent": 40.0},
            "miner_health": {"lane_balance": {"identity_basis": "mac"}, "miners": []},
        }
        summary = {
            "shares": {"accept_ratio": 0.5, "stale_rejects_per_minute": 0.0},
            "blocks": {"accepted_per_hour": 10.0, "rejected_per_hour": 0.0},
            "templates": {"fetch_avg_seconds": 0.1},
        }

        safety = adaptive.summarize_safety(
            summary,
            status,
            args,
            {"pool_job_health_ok": 0.0},
            "pool_job_health_ok=0",
        )

        self.assertFalse(safety["ok"])
        self.assertIn("runtime_abort_pool_job_health_ok_0", safety["violations"])
        self.assertIn("pool_job_health_not_ok", safety["violations"])
        self.assertIn("dashboard_overall_degraded", safety["violations"])
        self.assertIn("host_iowait_high", safety["violations"])

    def test_choose_next_config_reduces_candidate_age_on_old_template_rejects(self) -> None:
        args = adaptive.build_parser().parse_args([])
        current = adaptive.AdaptiveConfig(block_candidate_job_age_ms=1200)
        summary = {
            "shares": {"rejected_by_reason": {}, "stale_rejects_per_minute": 0.0},
            "blocks": {"by_outcome_reason": {"rejected:old-template-age": 2}},
            "templates": {"fetch_avg_seconds": 0.1},
        }

        next_config, reason = adaptive.choose_next_config(current, summary, {"host_pressure": {}}, args)

        self.assertEqual(next_config.block_candidate_job_age_ms, 1050)
        self.assertIn("old-template-age", reason)

    def test_choose_next_config_reduces_share_load_on_stale_share_rejects(self) -> None:
        args = adaptive.build_parser().parse_args([])
        current = adaptive.AdaptiveConfig(vardiff_target_share_seconds=3.0)
        summary = {
            "shares": {
                "rejected_by_reason": {"invalidated_job": 4},
                "stale_rejects_per_minute": 2.0,
            },
            "blocks": {"by_outcome_reason": {}},
            "templates": {"fetch_avg_seconds": 0.1},
        }

        next_config, reason = adaptive.choose_next_config(current, summary, {"host_pressure": {}}, args)

        self.assertEqual(next_config.vardiff_target_share_seconds, 4.0)
        self.assertIn("telemetry share load", reason)


if __name__ == "__main__":
    unittest.main()
