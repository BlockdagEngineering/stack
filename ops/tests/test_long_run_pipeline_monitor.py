#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import long_run_pipeline_monitor as monitor  # noqa: E402


class LongRunPipelineMonitorTests(unittest.TestCase):
    def sample(self, at: str, **metrics: float) -> dict[str, object]:
        metric_payload: dict[str, float] = {}
        for name, value in metrics.items():
            metric_payload[monitor.SUMMARY_METRICS[name]] = value
        return {
            "event": "sample",
            "sampled_at": at,
            "errors": {"job_state": None, "metrics": None, "dashboard": None},
            "metrics": metric_payload,
            "pool_job_state": {
                "ready_connections": int(metrics.get("ready_miners", 4)),
                "reason_code": "ok",
            },
        }

    def test_node_log_tail_tracks_import_age_and_open_graph_sync(self) -> None:
        payload = """
2026-06-25|01:46:38.367 [INFO ] Syncing graph state module=SYNC cur=(1) target=(2) peer=16PeerA protocol=45 services=Full|CF processID=7
2026-06-25|01:54:47.066 [INFO ] Imported new chain segment \x1b[0mnumber\x1b[0m=12,157,992 \x1b[0mhash\x1b[0m=f4666b..9f2684 \x1b[0mage\x1b[0m=2h22m33s
2026-06-25|01:54:48.066 [WARN ] Rewinding blockchain to block target=12,157,990
2026-06-25|01:54:49.066 [WARN ] Can't find tip module=DAG block id=12672018
"""

        summary = monitor.summarize_node_log_tail(payload)

        self.assertEqual(12157992, summary["latest_import"]["number"])
        self.assertEqual("2h22m33s", summary["latest_import"]["age"])
        self.assertEqual(8553, summary["latest_import"]["age_seconds"])
        self.assertTrue(summary["graph_sync_open"])
        self.assertEqual([7], summary["graph_sync_open_process_ids"])
        self.assertEqual("16PeerA", summary["graph_sync_last_open"]["peer"])
        self.assertEqual(1, summary["rewind_count_tail"])
        self.assertEqual(1, summary["missing_tip_count_tail"])

    def test_node_log_tail_closes_completed_graph_sync(self) -> None:
        payload = """
2026-06-25|01:46:38.367 [INFO ] Syncing graph state module=SYNC peer=16PeerA processID=7
2026-06-25|01:46:58.367 [INFO ] The sync of graph state has ended module=SYNC spend=20s processID=7
"""

        summary = monitor.summarize_node_log_tail(payload)

        self.assertFalse(summary["graph_sync_open"])
        self.assertEqual({"process_id": 7, "spend": "20s", "spend_seconds": 20}, summary["graph_sync_last_end"])

    def test_node_rpc_summary_exposes_mining_safety_contradiction(self) -> None:
        calls: list[str] = []

        def fake_rpc(url: str, method: str, **_kwargs):
            calls.append(f"{url}:{method}")
            if method == "getTemplateHealth":
                return (
                    {
                        "chain_current": True,
                        "main_order": 12559107,
                        "p2p_best_peer_main_order": 12560870,
                        "p2p_best_peer_lead_blocks": 1763,
                        "mineable_now": False,
                        "submit_ready": False,
                        "reason_code": "node_syncing",
                        "template_available": False,
                        "template_coinbase_valid": False,
                        "p2p_mining_fresh": False,
                        "p2p_mining_fresh_reason_code": "peer_lead_exceeds_tolerance",
                    },
                    None,
                    1.25,
                )
            return 12559108, None, 0.75

        original = monitor.json_rpc_call
        monitor.json_rpc_call = fake_rpc
        try:
            summary = monitor.summarize_node_rpc(
                "http://node:38131",
                timeout=1.0,
                user="user",
                password="pass",
            )
        finally:
            monitor.json_rpc_call = original

        self.assertEqual(["http://node:38131:getTemplateHealth", "http://node:38131:getBlockCount"], calls)
        self.assertTrue(summary["chain_current"])
        self.assertFalse(summary["mineable_now"])
        self.assertFalse(summary["submit_ready"])
        self.assertEqual("node_syncing", summary["reason_code"])
        self.assertEqual(12559107, summary["main_order"])
        self.assertEqual(12560870, summary["p2p_best_peer_main_order"])
        self.assertEqual(1763, summary["p2p_best_peer_lead_blocks"])
        self.assertEqual(12559108, summary["block_count"])

    def test_reset_aware_counter_delta_handles_pool_restart(self) -> None:
        samples = [
            self.sample("2026-06-25T05:00:00+02:00", accepted_blocks=100),
            self.sample("2026-06-25T05:01:00+02:00", accepted_blocks=130),
            self.sample("2026-06-25T05:02:00+02:00", accepted_blocks=5),
            self.sample("2026-06-25T05:03:00+02:00", accepted_blocks=20),
        ]

        delta = monitor.reset_aware_counter_delta(samples, "accepted_blocks")

        self.assertEqual(50, delta["delta"])
        self.assertEqual(1, delta["resets"])
        self.assertEqual(100, delta["first"])
        self.assertEqual(20, delta["last"])

    def test_sample_window_summary_reports_paid_blocks_and_reject_ratio(self) -> None:
        samples = [
            self.sample(
                "2026-06-25T05:00:00+02:00",
                accepted_blocks=100,
                blocks_found=100,
                blocks_submitted=100,
                stale_job_rejects=2,
                stale_parent_rejects=1,
                duplicate_rejects=0,
                ready_miners=4,
                p2p_mining_fresh=1,
                peer_lead_blocks=0,
                mineable=1,
                submit_ready=1,
                template_age_seconds=0.5,
            ),
            self.sample(
                "2026-06-25T05:10:00+02:00",
                accepted_blocks=160,
                blocks_found=160,
                blocks_submitted=160,
                stale_job_rejects=5,
                stale_parent_rejects=3,
                duplicate_rejects=1,
                ready_miners=4,
                p2p_mining_fresh=1,
                peer_lead_blocks=0,
                mineable=1,
                submit_ready=1,
                template_age_seconds=0.7,
            ),
        ]

        summary = monitor.summarize_sample_window(samples)

        self.assertEqual(60, summary["counters"]["accepted_blocks"]["delta"])
        self.assertEqual(6, summary["local_reject_delta"])
        self.assertEqual(0.1, summary["local_rejects_per_accepted"])
        self.assertEqual(360.0, summary["accepted_blocks_per_hour"])
        self.assertEqual(4, summary["gauges"]["ready_miners"]["min"])
        self.assertEqual(0, summary["anomaly_count"])

    def test_sample_window_summary_flags_peer_lead_stall(self) -> None:
        samples = [
            self.sample(
                "2026-06-25T05:34:00+02:00",
                accepted_blocks=604,
                ready_miners=0,
                p2p_mining_fresh=0,
                peer_lead_blocks=30,
                mineable=0,
                submit_ready=0,
                template_age_seconds=52,
            )
        ]

        summary = monitor.summarize_sample_window(samples)

        self.assertEqual(1, summary["anomaly_count"])
        reasons = summary["anomaly_samples"][0]["reasons"]
        self.assertIn("ready_miners_below_4", reasons)
        self.assertIn("p2p_mining_not_fresh", reasons)
        self.assertIn("peer_lead_exceeds_tolerance", reasons)
        self.assertIn("mineable_false", reasons)
        self.assertIn("submit_ready_false", reasons)

    def test_sample_window_summary_ignores_safe_backend_readiness_flicker(self) -> None:
        samples = [
            self.sample(
                "2026-06-25T06:00:00+02:00",
                accepted_blocks=100,
                blocks_found=100,
                blocks_submitted=100,
                ready_miners=4,
                p2p_mining_fresh=1,
                peer_lead_blocks=0,
                mineable=0,
                submit_ready=0,
                template_age_seconds=0.2,
            ),
            self.sample(
                "2026-06-25T06:01:00+02:00",
                accepted_blocks=120,
                blocks_found=120,
                blocks_submitted=120,
                ready_miners=4,
                p2p_mining_fresh=1,
                peer_lead_blocks=0,
                mineable=1,
                submit_ready=1,
                template_age_seconds=0.4,
            ),
        ]

        summary = monitor.summarize_sample_window(samples)

        self.assertEqual(0, summary["anomaly_count"])
        self.assertEqual([], summary["window_anomaly_reasons"])

    def test_sample_window_summary_flags_no_paid_block_progress_with_miner_demand(self) -> None:
        samples = [
            self.sample(
                "2026-06-25T04:00:00+02:00",
                accepted_blocks=0,
                blocks_found=0,
                blocks_submitted=0,
                ready_miners=0,
                authorized_miners=4,
                p2p_mining_fresh=0,
                peer_lead_blocks=733,
                mineable=0,
                submit_ready=0,
                template_age_seconds=52,
            ),
            self.sample(
                "2026-06-25T04:10:00+02:00",
                accepted_blocks=0,
                blocks_found=0,
                blocks_submitted=0,
                ready_miners=0,
                authorized_miners=4,
                p2p_mining_fresh=0,
                peer_lead_blocks=1364,
                mineable=0,
                submit_ready=0,
                template_age_seconds=74,
            ),
        ]

        summary = monitor.summarize_sample_window(samples)

        self.assertIn("accepted_blocks_not_advancing", summary["window_anomaly_reasons"])
        self.assertIn("ready_miners_zero_for_window", summary["window_anomaly_reasons"])
        self.assertIn("p2p_mining_not_fresh_for_window", summary["window_anomaly_reasons"])
        self.assertIn("peer_lead_exceeds_tolerance_for_window", summary["window_anomaly_reasons"])


if __name__ == "__main__":
    unittest.main()
