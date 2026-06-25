#!/usr/bin/env python3

import pathlib
import os
import sys
import tempfile
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

    def test_env_or_file_value_loads_rpc_credentials_from_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "NODE_RPC_USER=test-user",
                        "NODE_RPC_PASS='test-pass'",
                        "BDAG_NODE_RPC_URL=http://127.0.0.1:38131",
                    ]
                ),
                encoding="utf-8",
            )
            monitor._ENV_FILE_CACHE.clear()
            old_user = os.environ.pop("NODE_RPC_USER", None)
            old_pass = os.environ.pop("NODE_RPC_PASS", None)
            try:
                self.assertEqual("test-user", monitor.env_or_file_value("NODE_RPC_USER", path=env_file))
                self.assertEqual("test-pass", monitor.env_or_file_value("NODE_RPC_PASS", path=env_file))
                os.environ["NODE_RPC_USER"] = "env-user"
                self.assertEqual("env-user", monitor.env_or_file_value("NODE_RPC_USER", path=env_file))
            finally:
                monitor._ENV_FILE_CACHE.clear()
                if old_user is not None:
                    os.environ["NODE_RPC_USER"] = old_user
                else:
                    os.environ.pop("NODE_RPC_USER", None)
                if old_pass is not None:
                    os.environ["NODE_RPC_PASS"] = old_pass
                else:
                    os.environ.pop("NODE_RPC_PASS", None)

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

    def test_node_log_tail_tracks_import_height_without_age_field(self) -> None:
        payload = """
2026-06-25|08:17:39.810 [INFO ] Imported new chain segment number=12,198,983 hash=aed655..0bbb55 blocks=1 txs=0 mgas=0.000 elapsed=28.757ms
"""

        summary = monitor.summarize_node_log_tail(payload)

        self.assertEqual(12198983, summary["latest_import"]["number"])
        self.assertIsNone(summary["latest_import"]["age"])
        self.assertIsNone(summary["latest_import"]["age_seconds"])

    def test_node_log_tail_closes_completed_graph_sync(self) -> None:
        payload = """
2026-06-25|01:46:38.367 [INFO ] Syncing graph state module=SYNC peer=16PeerA processID=7
2026-06-25|01:46:58.367 [INFO ] The sync of graph state has ended module=SYNC spend=20s processID=7
"""

        summary = monitor.summarize_node_log_tail(payload)

        self.assertFalse(summary["graph_sync_open"])
        self.assertEqual({"process_id": 7, "spend": "20s", "spend_seconds": 20}, summary["graph_sync_last_end"])

    def test_parse_metrics_keeps_paid_block_counter(self) -> None:
        metrics = monitor.parse_metrics(
            """
# HELP pool_blocks_paid_total Paid blocks confirmed for the pool.
# TYPE pool_blocks_paid_total counter
pool_blocks_paid_total{pool_id="0"} 3508
pool_block_submit_outcomes_total{outcome="accepted",pool_id="0",reason="ok"} 5705
"""
        )

        self.assertEqual(3508.0, metrics["pool_blocks_paid_total{pool_id=0}"])
        self.assertEqual(
            5705.0,
            metrics["pool_block_submit_outcomes_total{outcome=accepted,pool_id=0,reason=ok}"],
        )

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
            if method == "getPeerInfo":
                return (
                    [
                        {
                            "id": "16PeerFresh",
                            "address": "/ip4/129.121.92.232/tcp/8152/p2p/16PeerFresh",
                            "state": True,
                            "active": True,
                            "services": "Full|CF",
                            "direction": "Outbound",
                            "syncnode": True,
                            "graphstate": {
                                "tips": ["0xfresh main"],
                                "mainorder": 12560870,
                                "mainheight": 9640000,
                                "layer": 9650000,
                            },
                            "gsupdate": "0s",
                            "latency_ms": 12,
                            "reconnect": 1,
                            "bads": [],
                            "conntime": "20m53s",
                            "dagport": 38131,
                        },
                        {
                            "id": "16PeerLagging",
                            "address": "/ip4/207.244.230.191/tcp/8153/p2p/16PeerLagging",
                            "state": True,
                            "active": True,
                            "services": "Full|CF",
                            "direction": "Outbound",
                            "graphstate": {
                                "tips": ["0xlagging main"],
                                "mainorder": 12560600,
                                "mainheight": 9639900,
                                "layer": 9649900,
                            },
                            "gsupdate": "1s",
                            "latency_ms": 883702,
                            "reconnect": 4,
                            "bads": ["ErrStreamBase"],
                            "conntime": "18m29s",
                            "dagport": 38131,
                        },
                    ],
                    None,
                    2.5,
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

        self.assertEqual(
            [
                "http://node:38131:getTemplateHealth",
                "http://node:38131:getBlockCount",
                "http://node:38131:getPeerInfo",
            ],
            calls,
        )
        self.assertTrue(summary["chain_current"])
        self.assertFalse(summary["mineable_now"])
        self.assertFalse(summary["submit_ready"])
        self.assertEqual("node_syncing", summary["reason_code"])
        self.assertEqual(12559107, summary["main_order"])
        self.assertEqual(12560870, summary["p2p_best_peer_main_order"])
        self.assertEqual(1763, summary["p2p_best_peer_lead_blocks"])
        self.assertEqual(12559108, summary["block_count"])
        self.assertEqual(2, summary["connected_peer_count"])
        self.assertEqual(2, summary["active_peer_count"])
        self.assertEqual(2, summary["consensus_peer_count"])
        self.assertEqual(1, summary["syncnode_peer_count"])
        self.assertEqual(12560600, summary["peer_graph_main_order_min"])
        self.assertEqual(12560870, summary["peer_graph_main_order_max"])
        self.assertEqual(270, summary["peer_graph_main_order_spread"])
        self.assertEqual(2.5, summary["latency_ms"]["getPeerInfo"])
        self.assertEqual("16PeerFresh", summary["peers"][0]["id"])
        self.assertEqual("/ip4/129.121.92.232/tcp/8152/p2p/16PeerFresh", summary["peers"][0]["address"])
        self.assertEqual(12560870, summary["peers"][0]["graph_main_order"])
        self.assertEqual("0xfresh main", summary["peers"][0]["graph_tip"])
        self.assertEqual(1, summary["peers"][1]["bad_count"])

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
                paid_blocks=95,
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
                paid_blocks=151,
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
        self.assertEqual(56, summary["counters"]["paid_blocks"]["delta"])
        self.assertEqual(6, summary["local_reject_delta"])
        self.assertEqual(0.1, summary["local_rejects_per_accepted"])
        self.assertEqual(360.0, summary["accepted_blocks_per_hour"])
        self.assertEqual(336.0, summary["paid_blocks_per_hour"])
        self.assertEqual(0.933333, summary["paid_blocks_per_accepted"])
        self.assertEqual(4, summary["gauges"]["ready_miners"]["min"])
        self.assertEqual(0, summary["anomaly_count"])

    def test_sample_window_summary_flags_peer_lead_stall(self) -> None:
        samples = [
            self.sample(
                "2026-06-25T05:34:00+02:00",
                accepted_blocks=604,
                authorized_miners=4,
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
        self.assertIn("hard_peer_lead_template_stall", reasons)

    def test_summary_prefers_job_state_when_ready_metric_is_racy(self) -> None:
        sample = self.sample(
            "2026-06-25T16:54:00+02:00",
            accepted_blocks=100,
            blocks_found=100,
            blocks_submitted=100,
            stale_job_rejects=0,
            stale_parent_rejects=0,
            duplicate_rejects=0,
            authorized_miners=4,
            ready_miners=0,
            p2p_mining_fresh=1,
            peer_lead_blocks=0,
            mineable=1,
            submit_ready=1,
            template_age_seconds=0.5,
        )
        sample["pool_job_state"] = {
            "status": "ok",
            "reason_code": "ok",
            "active_connections": 4,
            "authorized_connections": 4,
            "ready_connections": 4,
            "last_broadcast_age_ms": 500,
            "max_current_job_age_ms": 500,
        }

        summary = monitor.summarize_sample_window([sample])

        self.assertEqual(4.0, summary["gauges"]["ready_miners"]["min"])
        self.assertEqual(0, summary["anomaly_count"])
        self.assertEqual(1, summary["advisory_count"])
        self.assertIn(
            "ready_miners_metric_job_state_mismatch",
            summary["advisory_samples"][0]["reasons"],
        )

    def test_summary_falls_back_to_ready_metric_without_job_state(self) -> None:
        sample = self.sample(
            "2026-06-25T16:55:00+02:00",
            accepted_blocks=100,
            blocks_found=100,
            blocks_submitted=100,
            stale_job_rejects=0,
            stale_parent_rejects=0,
            duplicate_rejects=0,
            authorized_miners=4,
            ready_miners=0,
            p2p_mining_fresh=1,
            peer_lead_blocks=0,
            mineable=1,
            submit_ready=1,
            template_age_seconds=0.5,
        )
        sample["pool_job_state"] = {}

        summary = monitor.summarize_sample_window([sample])

        self.assertEqual(0.0, summary["gauges"]["ready_miners"]["min"])
        self.assertEqual(1, summary["anomaly_count"])
        self.assertIn("ready_miners_below_4", summary["anomaly_samples"][0]["reasons"])

    def test_sample_window_summary_flags_peer_lead_stall_from_pool_job_age(self) -> None:
        sample = self.sample(
            "2026-06-25T15:37:00+02:00",
            accepted_blocks=1185,
            authorized_miners=4,
            ready_miners=0,
            p2p_mining_fresh=0,
            peer_lead_blocks=38,
            mineable=0,
            submit_ready=0,
            template_age_seconds=14,
        )
        sample["pool_job_state"] = {
            "status": "degraded",
            "reason_code": "invalidated_current_job",
            "active_connections": 4,
            "authorized_connections": 4,
            "ready_connections": 0,
            "invalid_current_job_connections": 4,
            "last_broadcast_age_ms": 18411,
            "max_current_job_age_ms": 18411,
            "clients": [
                {"ready": False, "reason_code": "invalidated_current_job", "current_job_age_ms": 18411},
                {"ready": False, "reason_code": "invalidated_current_job", "current_job_age_ms": 18410},
            ],
        }

        summary = monitor.summarize_sample_window([sample])

        self.assertEqual(1, summary["anomaly_count"])
        reasons = summary["anomaly_samples"][0]["reasons"]
        self.assertIn("pool_job_age_over_12s", reasons)
        self.assertNotIn("template_age_over_30s", reasons)
        self.assertIn("hard_peer_lead_template_stall", reasons)
        self.assertEqual(1, len(summary["critical_anomaly_samples"]))
        self.assertEqual(18.411, summary["critical_anomaly_samples"][0]["pool_job_age_seconds"])
        self.assertEqual(18.411, summary["gauges"]["pool_job_age_seconds"]["max"])
        self.assertIn("hard_peer_lead_template_stall_observed", summary["window_anomaly_reasons"])

    def test_sample_window_summary_does_not_mark_short_job_age_as_hard_stall(self) -> None:
        sample = self.sample(
            "2026-06-25T15:36:00+02:00",
            accepted_blocks=1185,
            authorized_miners=4,
            ready_miners=0,
            p2p_mining_fresh=0,
            peer_lead_blocks=38,
            mineable=0,
            submit_ready=0,
            template_age_seconds=14,
        )
        sample["pool_job_state"] = {
            "status": "degraded",
            "reason_code": "invalidated_current_job",
            "active_connections": 4,
            "authorized_connections": 4,
            "ready_connections": 0,
            "last_broadcast_age_ms": 11000,
            "clients": [{"ready": False, "current_job_age_ms": 11000}],
        }

        summary = monitor.summarize_sample_window([sample])

        self.assertEqual(1, summary["anomaly_count"])
        self.assertNotIn("hard_peer_lead_template_stall", summary["anomaly_samples"][0]["reasons"])
        self.assertEqual([], summary["critical_anomaly_samples"])
        self.assertNotIn("hard_peer_lead_template_stall_observed", summary["window_anomaly_reasons"])

    def test_sample_window_summary_flags_sustained_hard_peer_lead_stall(self) -> None:
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
            ),
            self.sample(
                "2026-06-25T05:39:00+02:00",
                accepted_blocks=604,
                authorized_miners=4,
                ready_miners=0,
                p2p_mining_fresh=0,
                peer_lead_blocks=56,
                mineable=0,
                submit_ready=0,
                template_age_seconds=104,
            ),
        ]

        summary = monitor.summarize_sample_window(samples)

        self.assertIn("accepted_blocks_not_advancing", summary["window_anomaly_reasons"])
        self.assertIn("hard_peer_lead_template_stall_observed", summary["window_anomaly_reasons"])
        self.assertIn("hard_peer_lead_template_stall_for_window", summary["window_anomaly_reasons"])
        self.assertEqual(2, len(summary["critical_anomaly_samples"]))

    def test_sample_window_summary_surfaces_single_hard_stall_inside_productive_hour(self) -> None:
        samples = [
            self.sample(
                "2026-06-25T05:00:00+02:00",
                accepted_blocks=100,
                authorized_miners=4,
                ready_miners=4,
                p2p_mining_fresh=1,
                peer_lead_blocks=0,
                mineable=1,
                submit_ready=1,
                template_age_seconds=1,
            ),
            self.sample(
                "2026-06-25T05:34:45+02:00",
                accepted_blocks=604,
                authorized_miners=4,
                ready_miners=0,
                p2p_mining_fresh=0,
                peer_lead_blocks=30,
                mineable=0,
                submit_ready=0,
                template_age_seconds=52,
            ),
            self.sample(
                "2026-06-25T05:59:00+02:00",
                accepted_blocks=700,
                authorized_miners=4,
                ready_miners=4,
                p2p_mining_fresh=1,
                peer_lead_blocks=0,
                mineable=1,
                submit_ready=1,
                template_age_seconds=1,
            ),
        ]

        summary = monitor.summarize_sample_window(samples)

        self.assertIn("hard_peer_lead_template_stall_observed", summary["window_anomaly_reasons"])
        self.assertNotIn("accepted_blocks_not_advancing", summary["window_anomaly_reasons"])
        self.assertNotIn("hard_peer_lead_template_stall_for_window", summary["window_anomaly_reasons"])
        self.assertEqual(1, len(summary["critical_anomaly_samples"]))
        self.assertEqual("2026-06-25T05:34:45+02:00", summary["critical_anomaly_samples"][0]["sampled_at"])

    def test_hard_stall_summary_keeps_graph_sync_and_reorg_context(self) -> None:
        sample = self.sample(
            "2026-06-25T09:28:45+02:00",
            accepted_blocks=6770,
            authorized_miners=4,
            ready_miners=0,
            p2p_mining_fresh=0,
            peer_lead_blocks=19,
            mineable=0,
            submit_ready=0,
            template_age_seconds=73,
        )
        sample["node_log_tail"] = {
            "graph_sync_open": True,
            "graph_sync_open_process_ids": [820],
            "graph_sync_last_open": {"peer": "16PeerA", "process_id": 820},
            "graph_sync_last_end": {"process_id": 819, "spend": "1m27s", "spend_seconds": 87},
            "rewind_count_tail": 4,
            "latest_import": {"number": 12194568, "age": "1m24s", "age_seconds": 84},
        }

        summary = monitor.summarize_sample_window([sample])

        self.assertIn("graph_sync_open_during_hard_stall", summary["window_anomaly_reasons"])
        self.assertIn("reorgs_observed_during_hard_stall", summary["window_anomaly_reasons"])
        context = summary["critical_anomaly_samples"][0]["node_log_context"]
        self.assertTrue(context["graph_sync_open"])
        self.assertEqual([820], context["graph_sync_open_process_ids"])
        self.assertEqual(4, context["rewind_count_tail"])
        self.assertEqual(87, context["graph_sync_last_end"]["spend_seconds"])

    def test_node_rpc_peer_spread_is_kept_when_pool_metrics_are_missing(self) -> None:
        sample = {
            "event": "sample",
            "sampled_at": "2026-06-25T13:08:42+02:00",
            "errors": {"job_state": None, "metrics": None, "dashboard": None},
            "metrics": {},
            "pool_job_state": {"ready_connections": 0, "reason_code": "node_syncing"},
            "node_rpc": {
                "reason_code": "node_syncing",
                "mineable_now": False,
                "submit_ready": False,
                "p2p_mining_fresh": False,
                "p2p_mining_fresh_reason_code": "peer_lead_exceeds_tolerance",
                "p2p_best_peer_lead_blocks": 18,
                "p2p_fresh_consensus_peer_count": 16,
                "connected_peer_count": 16,
                "active_peer_count": 16,
                "consensus_peer_count": 16,
                "peer_graph_main_order_min": 12606410,
                "peer_graph_main_order_max": 12630472,
                "peer_graph_main_order_spread": 24062,
                "template_age_ms": 52000,
            },
        }

        summary = monitor.summarize_sample_window([sample])

        reasons = summary["anomaly_samples"][0]["reasons"]
        self.assertIn("ready_miners_below_4", reasons)
        self.assertIn("p2p_mining_not_fresh", reasons)
        self.assertIn("peer_lead_exceeds_tolerance", reasons)
        self.assertIn("template_age_over_30s", reasons)
        self.assertIn("mineable_false", reasons)
        self.assertIn("submit_ready_false", reasons)
        self.assertIn("hard_peer_lead_template_stall", reasons)
        anomaly = summary["anomaly_samples"][0]
        self.assertEqual(18.0, anomaly["peer_lead_blocks"])
        self.assertEqual(52.0, anomaly["template_age_seconds"])
        self.assertEqual("node_syncing", anomaly["node_rpc_context"]["reason_code"])
        self.assertEqual(24062, anomaly["node_rpc_context"]["peer_graph_main_order_spread"])
        self.assertIn("peer_graph_spread_observed_during_anomaly", summary["window_anomaly_reasons"])

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

    def test_sample_window_summary_records_productive_parent_stale_as_advisory(self) -> None:
        sample = self.sample(
            "2026-06-25T15:23:21+02:00",
            accepted_blocks=746,
            ready_miners=4,
            p2p_mining_fresh=1,
            peer_lead_blocks=2,
            mineable=0,
            submit_ready=0,
            template_age_seconds=1.8,
        )
        sample["node_rpc"] = {
            "reason_code": "template_parent_stale",
            "mineable_now": False,
            "submit_ready": False,
            "p2p_mining_fresh": True,
            "p2p_mining_fresh_reason_code": "ok",
            "p2p_best_peer_lead_blocks": 2,
            "p2p_fresh_consensus_peer_count": 9,
        }
        sample["node_log_tail"] = {"graph_sync_open": True, "rewind_count_tail": 12}

        summary = monitor.summarize_sample_window([sample])

        self.assertEqual(0, summary["anomaly_count"])
        self.assertEqual(1, summary["advisory_count"])
        self.assertIn("productive_template_parent_stale", summary["advisory_samples"][0]["reasons"])
        self.assertIn("productive_template_parent_stale_observed", summary["window_advisory_reasons"])
        self.assertEqual([], summary["window_anomaly_reasons"])

    def test_sample_window_summary_records_peer_lead_risk_before_zero_ready(self) -> None:
        sample = self.sample(
            "2026-06-25T15:01:21+02:00",
            accepted_blocks=700,
            ready_miners=4,
            p2p_mining_fresh=0,
            peer_lead_blocks=18,
            mineable=0,
            submit_ready=0,
            template_age_seconds=4,
        )
        sample["node_rpc"] = {
            "reason_code": "node_syncing",
            "mineable_now": False,
            "submit_ready": False,
            "p2p_mining_fresh": False,
            "p2p_mining_fresh_reason_code": "peer_lead_exceeds_tolerance",
            "p2p_best_peer_lead_blocks": 18,
            "p2p_fresh_consensus_peer_count": 9,
        }

        summary = monitor.summarize_sample_window([sample])

        self.assertEqual(1, summary["anomaly_count"])
        self.assertIn("peer_lead_risk_before_zero_ready", summary["advisory_samples"][0]["reasons"])
        self.assertIn("peer_lead_risk_before_zero_ready_observed", summary["window_advisory_reasons"])
        self.assertNotIn("hard_peer_lead_template_stall", summary["anomaly_samples"][0]["reasons"])

    def test_sample_window_summary_records_graph_turbulence_with_mining_intact(self) -> None:
        sample = self.sample(
            "2026-06-25T15:20:21+02:00",
            accepted_blocks=730,
            ready_miners=4,
            p2p_mining_fresh=1,
            peer_lead_blocks=0,
            mineable=1,
            submit_ready=1,
            template_age_seconds=0.4,
        )
        sample["node_rpc"] = {
            "reason_code": "ok",
            "mineable_now": True,
            "submit_ready": True,
            "p2p_mining_fresh": True,
            "p2p_best_peer_lead_blocks": 0,
        }
        sample["node_log_tail"] = {"graph_sync_open": False, "rewind_count_tail": 8}

        summary = monitor.summarize_sample_window([sample])

        self.assertEqual(0, summary["anomaly_count"])
        self.assertEqual(1, summary["advisory_count"])
        self.assertIn("graph_sync_reorg_turbulence_mining_intact", summary["advisory_samples"][0]["reasons"])
        self.assertIn("graph_sync_reorg_turbulence_mining_intact_observed", summary["window_advisory_reasons"])

    def test_sample_window_summary_counts_dashboard_template_skew_when_node_rpc_is_safe(self) -> None:
        sample = self.sample(
            "2026-06-25T13:40:02+02:00",
            accepted_blocks=100,
            ready_miners=4,
            p2p_mining_fresh=1,
            peer_lead_blocks=0,
            mineable=0,
            submit_ready=0,
            template_age_seconds=0.8,
        )
        sample["dashboard_status"] = {
            "can_mine": True,
            "template_reason_code": "submit-not-ready",
            "mineable_now": False,
            "submit_ready": False,
        }
        sample["node_rpc"] = {
            "reason_code": "ok",
            "mineable_now": True,
            "submit_ready": True,
            "p2p_mining_fresh": True,
        }

        summary = monitor.summarize_sample_window([sample])
        consistency = summary["status_consistency"]

        self.assertEqual(1, consistency["can_mine_template_contradiction_count"])
        self.assertEqual(1, consistency["node_rpc_proven_safe_skew_count"])
        self.assertEqual(0, consistency["unresolved_contradiction_count"])
        self.assertEqual("submit-not-ready", consistency["first_contradiction"]["template_reason_code"])
        self.assertEqual("ok", consistency["first_contradiction"]["node_rpc_reason_code"])

    def test_sample_window_summary_counts_unresolved_dashboard_template_contradiction(self) -> None:
        sample = self.sample(
            "2026-06-25T13:40:45+02:00",
            accepted_blocks=100,
            ready_miners=4,
            p2p_mining_fresh=1,
            peer_lead_blocks=0,
            mineable=0,
            submit_ready=0,
            template_age_seconds=0.8,
        )
        sample["dashboard_status"] = {
            "can_mine": True,
            "template_reason_code": "submit-not-ready",
            "mineable_now": False,
            "submit_ready": False,
        }

        summary = monitor.summarize_sample_window([sample])
        consistency = summary["status_consistency"]

        self.assertEqual(1, consistency["can_mine_template_contradiction_count"])
        self.assertEqual(0, consistency["node_rpc_proven_safe_skew_count"])
        self.assertEqual(1, consistency["unresolved_contradiction_count"])
        self.assertEqual("submit-not-ready", consistency["last_contradiction"]["template_reason_code"])

    def test_sample_window_summary_flags_no_paid_block_progress_with_miner_demand(self) -> None:
        samples = [
            self.sample(
                "2026-06-25T04:00:00+02:00",
                accepted_blocks=0,
                blocks_found=0,
                paid_blocks=0,
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
                paid_blocks=0,
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

    def test_sample_window_summary_flags_paid_conversion_stall_after_long_window(self) -> None:
        samples = [
            self.sample(
                "2026-06-25T04:00:00+02:00",
                accepted_blocks=100,
                blocks_found=100,
                paid_blocks=50,
                blocks_submitted=100,
                ready_miners=4,
                authorized_miners=4,
                p2p_mining_fresh=1,
                peer_lead_blocks=0,
                mineable=1,
                submit_ready=1,
                template_age_seconds=0.5,
            ),
            self.sample(
                "2026-06-25T04:31:00+02:00",
                accepted_blocks=160,
                blocks_found=160,
                paid_blocks=50,
                blocks_submitted=160,
                ready_miners=4,
                authorized_miners=4,
                p2p_mining_fresh=1,
                peer_lead_blocks=0,
                mineable=1,
                submit_ready=1,
                template_age_seconds=0.7,
            ),
        ]

        summary = monitor.summarize_sample_window(samples)

        self.assertEqual(60, summary["counters"]["accepted_blocks"]["delta"])
        self.assertEqual(0, summary["counters"]["paid_blocks"]["delta"])
        self.assertIn("paid_blocks_not_advancing_with_accepted_work", summary["window_anomaly_reasons"])


if __name__ == "__main__":
    unittest.main()
