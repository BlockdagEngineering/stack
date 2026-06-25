#!/usr/bin/env python3

from collections import Counter
import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class PoolEfficiencyLossLedgerTests(unittest.TestCase):
    def test_loss_ledger_flags_block_share_and_template_waste(self) -> None:
        ledger = pool_ops.build_pool_efficiency_loss_ledger(
            block_submit_outcomes=Counter(
                {
                    "accepted:ok": 100,
                    "rejected:tip-overdue": 40,
                    "rejected-local:duplicate-block": 10,
                }
            ),
            shares_accepted_total=50,
            shares_rejected_by_reason=Counter({"invalidated_job": 70, "non_current_job": 20}),
            block_totals=Counter({"found": 150, "submitted": 150, "mature": 100}),
            blocks_rejected_by_node=Counter({"tip-overdue": 40}),
            share_processing={"count": 10, "sum_seconds": 3},
            template_conversion_stall={"active_miners": 5, "failure_ratio": 42.0},
        )

        self.assertEqual(ledger["severity"], "warning")
        self.assertEqual(ledger["block_outcomes"]["accepted_ratio_percent"], 66.67)
        self.assertEqual(ledger["share_outcomes"]["accepted_ratio_percent"], 35.71)
        self.assertTrue(any("template conversion loss" in item for item in ledger["warnings"]))
        self.assertEqual(ledger["top_loss_reasons"][0]["reason"], "invalidated_job")

    def test_loss_ledger_escalates_critical_template_conversion_loss(self) -> None:
        ledger = pool_ops.build_pool_efficiency_loss_ledger(
            block_submit_outcomes=Counter({"accepted:ok": 20, "rejected:tip-overdue": 10}),
            shares_accepted_total=100,
            shares_rejected_by_reason=Counter(),
            block_totals=Counter(),
            blocks_rejected_by_node=Counter(),
            template_conversion_stall={"active_miners": 5, "failure_ratio": 55.0},
        )

        self.assertEqual(ledger["severity"], "critical")

    def test_readiness_contract_distinguishes_contradiction_from_hard_unready(self) -> None:
        source_health = {"node_mineable": False, "node_submit_ready": False, "node_p2p_mining_fresh": True}
        job_health = {"ok": False}

        contradiction = pool_ops.selected_backend_readiness_contract("node", source_health, job_health, True)
        hard_unready = pool_ops.selected_backend_readiness_contract("node", source_health, job_health, False)

        self.assertTrue(contradiction["contradiction"])
        self.assertFalse(contradiction["hard_unready"])
        self.assertFalse(hard_unready["contradiction"])
        self.assertTrue(hard_unready["hard_unready"])

    def test_pool_job_state_zero_ready_forces_source_job_health_not_ok(self) -> None:
        job_health = pool_ops.merge_pool_job_state_into_source_job_health(
            {},
            {
                "active_connections": 4,
                "authorized_connections": 4,
                "subscribed_connections": 4,
                "ready_connections": 0,
                "invalid_current_job_connections": 4,
                "reason_code": "invalidated_current_job",
            },
        )

        self.assertFalse(job_health["ok"])
        self.assertFalse(job_health["pool_job_state_ok"])
        self.assertEqual("4", job_health["authorized_miners"])
        self.assertEqual("0", job_health["ready_miners"])
        self.assertEqual("4", job_health["invalid_current_job_miners"])
        self.assertEqual("invalidated_current_job", job_health["reason_code"])

        contract = pool_ops.selected_backend_readiness_contract("node", {}, job_health, False)
        self.assertTrue(contract["hard_unready"])

    def test_pool_job_state_ready_lanes_can_mark_source_job_health_ok(self) -> None:
        job_health = pool_ops.merge_pool_job_state_into_source_job_health(
            {},
            {
                "active_connections": 4,
                "authorized_connections": 4,
                "subscribed_connections": 4,
                "ready_connections": 4,
                "reason_code": "ok",
            },
        )

        self.assertTrue(job_health["ok"])
        self.assertTrue(job_health["pool_job_state_ok"])
        self.assertEqual("4", job_health["ready_miners"])

    def test_selected_backend_unready_reasons_include_peer_freshness(self) -> None:
        reasons = pool_ops.selected_backend_unready_reasons(
            {
                "node_mineable": False,
                "node_submit_ready": False,
                "node_p2p_mining_fresh": False,
                "node_last_template_build_error_blocking": True,
                "node_template_coinbase_valid": False,
                "node_p2p_best_peer_lead_blocks": 40,
                "node_p2p_peer_lead_tolerance_blocks": 10,
            }
        )

        self.assertEqual(
            reasons,
            [
                "mineable=false",
                "submit_ready=false",
                "p2p_mining_fresh=false",
                "template_coinbase_valid=false",
                "template_build_error_blocking=true",
                "p2p_best_peer_lead_blocks=40>10",
            ],
        )

    def test_selected_backend_template_health_is_unsafe_on_peer_lead(self) -> None:
        health = pool_ops.selected_backend_template_health(
            {
                "node_mineable": True,
                "node_submit_ready": True,
                "node_p2p_mining_fresh": True,
                "node_template_coinbase_valid": True,
                "node_p2p_best_peer_lead_blocks": 11,
                "node_p2p_peer_lead_tolerance_blocks": 10,
            }
        )

        self.assertFalse(health["safe_for_mining"])
        self.assertIn("p2p_best_peer_lead_blocks=11>10", health["blocking_reasons"])

    def test_selected_backend_source_degradation_is_advisory_with_recent_paid_work(self) -> None:
        advisory = pool_ops.selected_backend_source_degradation(True, True)
        hard = pool_ops.selected_backend_source_degradation(True, False)

        self.assertTrue(advisory["degraded"])
        self.assertTrue(advisory["advisory"])
        self.assertFalse(advisory["hard"])
        self.assertTrue(hard["hard"])
        self.assertFalse(hard["advisory"])

    def test_catchup_policy_pauses_pool_above_threshold(self) -> None:
        policy = pool_ops.build_catchup_policy(
            {"status": "syncing", "remaining_blocks": 450},
            {"node": {"peer_ahead_blocks": 20}},
            {"pool": {"running": False}},
            {},
        )

        self.assertTrue(policy["active"])
        self.assertTrue(policy["pool_pause_active"])
        self.assertEqual(policy["threshold_blocks"], 300)
        self.assertIn("mining work is intentionally paused", policy["summary"])
        self.assertIn("Leave miners configured", policy["user_message"])
        self.assertEqual(policy["trigger"], "lag_threshold")

    def test_catchup_policy_uses_io_pressure_as_primary_trigger(self) -> None:
        policy = pool_ops.build_catchup_policy(
            {"status": "syncing", "remaining_blocks": 80},
            {"node": {"peer_ahead_blocks": 80}},
            {"pool": {"running": True}},
            {"node_mineable": False, "node_submit_ready": False},
            {"iowait_percent": 18.0, "io_some_avg10": 22.0, "io_full_avg10": 23.0},
            mining_ready=False,
        )

        self.assertTrue(policy["active"])
        self.assertEqual(policy["trigger"], "io_pressure")
        self.assertTrue(policy["io_pressure_active"])
        self.assertFalse(policy["lag_threshold_active"])
        self.assertEqual(policy["lag_blocks"], 80)
        self.assertIn("I/O-bound", policy["summary"])
        self.assertIn("I/O pressure drops", policy["next_step"])
        self.assertTrue(any("io_full_avg10" in reason for reason in policy["io_pressure_reasons"]))

    def test_catchup_policy_uses_backend_peer_lead_when_sync_claims_synced(self) -> None:
        policy = pool_ops.build_catchup_policy(
            {"status": "synced", "remaining_blocks": 0},
            {"node": {}},
            {"pool": {"running": True}},
            {
                "node_mineable": False,
                "node_submit_ready": False,
                "node_p2p_mining_fresh": True,
                "node_p2p_best_peer_lead_blocks": 80,
            },
            {"io_full_avg10": 23.0},
            mining_ready=False,
        )

        self.assertTrue(policy["active"])
        self.assertEqual(policy["trigger"], "io_pressure")
        self.assertEqual(policy["lag_blocks"], 80)

    def test_catchup_policy_pauses_backend_unready_under_io_pressure_without_lag(self) -> None:
        policy = pool_ops.build_catchup_policy(
            {"status": "synced", "remaining_blocks": 0},
            {"node": {}},
            {"pool": {"running": True}},
            {"node_mineable": False, "node_submit_ready": False, "node_p2p_mining_fresh": True},
            {"iowait_percent": 21.0, "io_full_avg10": 22.0},
            mining_ready=False,
        )

        self.assertTrue(policy["active"])
        self.assertEqual(policy["trigger"], "io_pressure")
        self.assertEqual(policy["lag_blocks"], 0)
        self.assertTrue(policy["backend_unready_under_pressure"])
        self.assertIn("backend is not ready", policy["summary"])
        self.assertIn("stale or invalid work", policy["user_message"])


class NodeSyncProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_node_chain_rpc_snapshot = pool_ops.node_chain_rpc_snapshot
        self.old_evm_rpc_lag_snapshot = pool_ops.evm_rpc_lag_snapshot
        self.old_native_sync_progress = pool_ops.native_sync_progress

    def tearDown(self) -> None:
        pool_ops.node_chain_rpc_snapshot = self.old_node_chain_rpc_snapshot
        pool_ops.evm_rpc_lag_snapshot = self.old_evm_rpc_lag_snapshot
        pool_ops.native_sync_progress = self.old_native_sync_progress

    def test_evm_head_lag_is_advisory_when_native_p2p_is_current(self) -> None:
        pool_ops.node_chain_rpc_snapshot = lambda *_args, **_kwargs: {
            "chain_block_count": 12553174,
            "chain_main_height": 9607762,
            "chain_rpc_source": "getBlockCount",
            "chain_syncing": False,
        }
        pool_ops.evm_rpc_lag_snapshot = lambda *_args, **_kwargs: {
            "evm_block_count": 12160943,
            "evm_reference_block_count": 12193847,
            "evm_lag_to_reference": 32904,
        }
        pool_ops.native_sync_progress = lambda _source: None

        progress = pool_ops.node_sync_progress("node", "http://127.0.0.1:38131")

        self.assertEqual("synced", progress["status"])
        self.assertEqual(12553174, progress["current_block"])
        self.assertEqual(12553174, progress["highest_block"])
        self.assertEqual(0, progress["remaining_blocks"])
        self.assertTrue(progress["native_is_current"])
        self.assertTrue(progress["mining_advisory_sync"])
        self.assertTrue(progress["evm_chain_syncing"])
        self.assertEqual(12160943, progress["sync_current_block"])
        self.assertEqual(12193847, progress["sync_highest_block"])

    def test_native_p2p_lead_remains_hard_sync_even_when_evm_lags(self) -> None:
        pool_ops.node_chain_rpc_snapshot = lambda *_args, **_kwargs: {
            "chain_block_count": 12553174,
            "chain_main_height": 9607762,
            "chain_rpc_source": "getBlockCount",
            "chain_syncing": False,
        }
        pool_ops.evm_rpc_lag_snapshot = lambda *_args, **_kwargs: {
            "evm_block_count": 12160943,
            "evm_reference_block_count": 12193847,
            "evm_lag_to_reference": 32904,
        }
        pool_ops.native_sync_progress = lambda _source: {
            "status": "syncing",
            "percent": 99.9,
            "current_block": 12553174,
            "highest_block": 12553624,
            "remaining_blocks": 450,
            "source": "node:native-p2p-lead",
            "error": "",
        }

        progress = pool_ops.node_sync_progress("node", "http://127.0.0.1:38131")

        self.assertEqual("syncing", progress["status"])
        self.assertEqual(12553174, progress["current_block"])
        self.assertEqual(450, progress["remaining_blocks"])
        self.assertEqual("node:native-p2p-lead", progress["source"])
        self.assertNotIn("mining_advisory_sync", progress)


class PoolJobStateSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_pool_containers = pool_ops.POOL_CONTAINERS
        self.old_fetch_json_url = pool_ops.fetch_json_url
        self.old_pool_metrics_endpoint_for_container = pool_ops.pool_metrics_endpoint_for_container
        pool_ops.POOL_CONTAINERS = ["pool"]

    def tearDown(self) -> None:
        pool_ops.POOL_CONTAINERS = self.old_pool_containers
        pool_ops.fetch_json_url = self.old_fetch_json_url
        pool_ops.pool_metrics_endpoint_for_container = self.old_pool_metrics_endpoint_for_container

    def test_pool_job_state_summary_preserves_live_ready_lanes_for_no_logs_status(self) -> None:
        pool_ops.pool_metrics_endpoint_for_container = lambda *_args, **_kwargs: ("127.0.0.1:9090", "")
        pool_ops.fetch_json_url = lambda *_args, **_kwargs: {
            "status": "ok",
            "reason_code": "ok",
            "active_connections": 4,
            "authorized_connections": 4,
            "subscribed_connections": 4,
            "ready_connections": 4,
            "connections_without_current_job": 0,
            "clients": [
                {
                    "remote_host": "192.168.1.101",
                    "asic_mac": "2A:71:C7:F5:1F:1E",
                    "lane_id": "mac:2a:71:c7:f5:1f:1e",
                    "authorized": True,
                    "subscribed": True,
                    "ready": True,
                    "reason_code": "ok",
                    "current_job_id": "1",
                    "template_seq": 10,
                }
            ],
        }

        summary = pool_ops.collect_pool_job_state_summary({"pool": {"running": True}})

        self.assertEqual("ok", summary["status"])
        self.assertEqual(4, summary["active_connections"])
        self.assertEqual(4, summary["authorized_connections"])
        self.assertEqual(4, summary["ready_connections"])
        self.assertEqual(0, summary["connections_without_current_job"])
        self.assertEqual("2a:71:c7:f5:1f:1e", summary["clients"][0]["asic_mac"])

        connected = pool_ops.effective_connected_miner_count(
            {},
            {"active_connections": summary["active_connections"]},
            {
                "authorized_miners": str(summary["authorized_connections"]),
                "ready_miners": str(summary["ready_connections"]),
            },
        )
        self.assertEqual(4, connected)


class PoolPrometheusMetricsParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_fetch = pool_ops.fetch_text_url
        self.old_pool_containers = pool_ops.POOL_CONTAINERS
        pool_ops.POOL_CONTAINERS = ["asic-pool"]
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.fetch_text_url = self.old_fetch
        pool_ops.POOL_CONTAINERS = self.old_pool_containers

    def test_pool_metrics_parse_loss_ledger_and_source_health_contract_inputs(self) -> None:
        metrics = """
pool_active_connections 5
pool_rpc_backend_selected{backend="node",pool_id="0"} 1
pool_rpc_backend_healthy{backend="node",pool_id="0"} 1
pool_rpc_backend_node_health_mineable{backend="node",pool_id="0"} 0
pool_rpc_backend_node_health_submit_ready{backend="node",pool_id="0"} 0
pool_job_health_ok{pool_id="0"} 0
pool_job_health_ready_miners{pool_id="0"} 5
pool_template_conversion_stall_active_miners{pool_id="0"} 5
pool_template_conversion_stall_failure_ratio{pool_id="0"} 55
pool_template_conversion_stall_window_candidates{kind="accepted",pool_id="0"} 2
pool_template_conversion_stall_window_candidates{kind="failed",pool_id="0"} 3
pool_block_submit_outcomes_total{outcome="accepted",pool_id="0",reason="ok"} 10
pool_block_submit_outcomes_total{outcome="rejected",pool_id="0",reason="tip-overdue"} 8
pool_block_submit_backend_outcomes_total{backend="node",outcome="rejected",pool_id="0",reason="tip-overdue"} 8
pool_blocks_found_total{pool_id="0"} 18
pool_blocks_submitted_total{pool_id="0"} 18
pool_blocks_rejected_by_node_total{pool_id="0",reason="tip-overdue"} 8
pool_share_processing_duration_seconds_sum{pool_id="0"} 1.2
pool_share_processing_duration_seconds_count{pool_id="0"} 4
pool_shares_accepted_total{pool_id="0"} 5
pool_shares_rejected_total{pool_id="0",reason="invalidated_job"} 15
"""
        pool_ops.fetch_text_url = lambda *_args, **_kwargs: metrics

        payload = pool_ops.collect_pool_prometheus_metrics(
            {"asic-pool": {"running": True, "network_ips": ["10.0.0.2"]}}
        )

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["active_connections"], 5)
        self.assertEqual(payload["selected_backend"], "node")
        self.assertFalse(payload["selected_backend_source_health"]["node_mineable"])
        self.assertEqual(payload["loss_ledger"]["severity"], "critical")
        self.assertEqual(payload["loss_ledger"]["share_outcomes"]["accepted_ratio_percent"], 25.0)


if __name__ == "__main__":
    unittest.main()
