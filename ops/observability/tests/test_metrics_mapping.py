#!/usr/bin/env python3
"""Offline fixture tests for the read-only BlockDAG exporter mapping."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "exporters" / "bdag_exporter"))

import bdag_exporter  # noqa: E402


def load_fixture(name: str) -> dict:
    with (ROOT / "testdata" / f"{name}.json").open(encoding="utf-8") as handle:
        return json.load(handle)


class MetricsMappingTest(unittest.TestCase):
    def test_status_fixture_emits_core_metrics(self) -> None:
        metrics = bdag_exporter.Metrics()
        bdag_exporter.add_status_metrics(metrics, load_fixture("status"), True, 0.01)
        output = metrics.render().decode("utf-8")

        self.assertIn("bdag_dashboard_api_up{api=\"status\"} 1.0", output)
        self.assertIn("# TYPE bdag_pool_valid_shares_recent gauge", output)
        self.assertIn("# TYPE bdag_miner_up gauge", output)
        self.assertIn("# TYPE bdag_node_latest_block gauge", output)
        self.assertIn("# TYPE bdag_node_affects_production_health gauge", output)
        self.assertIn(
            'bdag_node_affects_production_health{health_scope="production",node="bdag-miner-node-1",role="managed"} 1.0',
            output,
        )
        self.assertIn("# TYPE bdag_pool_head_changes_recent gauge", output)
        self.assertNotIn("bdag_pool_valid_shares_recent_total", output)

    def test_status_observer_node_is_advisory(self) -> None:
        data = load_fixture("status")
        data["observer_node_services"] = ["bdag-observer-node-3"]
        data["node_services"] = [*data.get("node_services", []), "bdag-observer-node-3"]
        data["nodes"]["bdag-observer-node-3"] = {
            "role": "observer",
            "health_scope": "advisory",
            "affects_production_health": False,
            "child_running": True,
            "latest_block": 123,
            "last_import_age_seconds": 2,
            "import_count": 1,
            "p2p_stream_errors": 0,
            "mining_template_error_count": 0,
        }
        metrics = bdag_exporter.Metrics()
        bdag_exporter.add_status_metrics(metrics, data, True, 0.01)
        output = metrics.render().decode("utf-8")

        self.assertIn(
            'bdag_node_affects_production_health{health_scope="advisory",node="bdag-observer-node-3",role="observer"} 0.0',
            output,
        )
        self.assertIn(
            'bdag_node_latest_block{health_scope="advisory",node="bdag-observer-node-3",role="observer"} 123.0',
            output,
        )

    def test_earnings_fixture_emits_wallet_and_miner_metrics(self) -> None:
        metrics = bdag_exporter.Metrics()
        bdag_exporter.add_earnings_metrics(metrics, load_fixture("earnings"), True, 0.01)
        output = metrics.render().decode("utf-8")

        self.assertIn("# TYPE bdag_wallet_balance_bdag gauge", output)
        self.assertIn("# TYPE bdag_price_usd gauge", output)
        self.assertIn("# TYPE bdag_miner_estimated_usd_per_hour gauge", output)
        self.assertIn("# TYPE bdag_wallet_recent_usd_per_hour gauge", output)
        self.assertIn("# TYPE bdag_miner_estimated_bdag_total gauge", output)
        self.assertIn("# TYPE bdag_earnings_history_stale gauge", output)
        self.assertIn("# TYPE bdag_earnings_history_latest_age_seconds gauge", output)

    def test_global_fixture_emits_pool_metrics(self) -> None:
        metrics = bdag_exporter.Metrics()
        bdag_exporter.add_global_metrics(metrics, load_fixture("global"), True, 0.01)
        output = metrics.render().decode("utf-8")

        self.assertIn("# TYPE bdag_global_latest_block gauge", output)
        self.assertIn("# TYPE bdag_global_pool_work_percent gauge", output)
        self.assertIn("# TYPE bdag_peer_ip_count gauge", output)

    def test_sampler_fixture_emits_stale_metrics(self) -> None:
        metrics = bdag_exporter.Metrics()
        bdag_exporter.add_sampler_metrics(
            metrics,
            {
                "status": "ok",
                "stale": False,
                "latest_age_seconds": 42,
                "expected_interval_seconds": 120,
                "stale_threshold_seconds": 360,
            },
            True,
            0.01,
        )
        output = metrics.render().decode("utf-8")

        self.assertIn("bdag_dashboard_api_up{api=\"sampler\"} 1.0", output)
        self.assertIn("# TYPE bdag_earnings_sampler_stale gauge", output)
        self.assertIn("bdag_earnings_sampler_latest_age_seconds 42.0", output)

    def test_api_failure_is_observable(self) -> None:
        metrics = bdag_exporter.Metrics()
        bdag_exporter.add_status_metrics(metrics, None, False, 0.5)
        output = metrics.render().decode("utf-8")

        self.assertIn("bdag_dashboard_api_up{api=\"status\"} 0.0", output)
        self.assertIn("bdag_dashboard_api_scrape_seconds{api=\"status\"} 0.5", output)

    def test_router_fixture_emits_optimum_state_metrics(self) -> None:
        metrics = bdag_exporter.Metrics()
        bdag_exporter.add_router_metrics(
            metrics,
            {
                "current_primary": "bdag-miner-node-1",
                "recommended_primary": "bdag-miner-node-2",
                "should_switch": True,
                "score_delta": 20,
                "current_primary_suboptimal": True,
                "pool_pressure": {
                    "hard_pool_pressure": False,
                    "pool_quality_pressure": True,
                    "block_error_ratio": 0.16,
                    "stale_job_candidate_ratio": 0.06,
                    "tip_overdue_ratio": 0.08,
                    "valid_share_ratio": 0.49,
                },
                "scores": {
                    "bdag-miner-node-1": {
                        "score": 80,
                        "mining_template_failing": False,
                        "p2p_stream_errors": 21,
                    },
                    "bdag-miner-node-2": {
                        "score": 100,
                        "mining_template_failing": False,
                        "p2p_stream_errors": 0,
                    },
                },
            },
            True,
            0.01,
        )
        output = metrics.render().decode("utf-8")

        self.assertIn("# TYPE bdag_rpc_router_current_suboptimal gauge", output)
        self.assertIn("bdag_rpc_router_pool_quality_pressure 1.0", output)
        self.assertIn('bdag_rpc_node_p2p_stream_errors{node="bdag-miner-node-1"} 21.0', output)

    def test_p2p_fixture_emits_guard_metrics(self) -> None:
        metrics = bdag_exporter.Metrics()
        bdag_exporter.add_p2p_metrics(
            metrics,
            {
                "guard_state": "warning",
                "overall_score": 78,
                "active_primary": "bdag-miner-node-1",
                "best_alternate": "bdag-miner-node-2",
                "active_primary_score": 78,
                "best_alternate_score": 98,
                "nodes": {
                    "bdag-miner-node-1": {
                        "score": 78,
                        "public_peer_count": 12,
                        "native_peers": 20,
                        "native_dial_errors_delta": 3,
                    },
                    "bdag-miner-node-2": {
                        "score": 98,
                        "public_peer_count": 14,
                        "native_peers": 23,
                        "native_dial_errors_delta": 0,
                    },
                },
                "pool_quality": {
                    "valid_share_ratio": 0.61,
                    "block_error_ratio": 0.04,
                    "stale_job_ratio": 0.01,
                    "tip_overdue_ratio": 0.02,
                },
                "network": {
                    "default_route": {"interface": "enx0", "mining_interface_ok": True},
                    "gateway_ping": {"ip": "192.168.1.1", "up": True, "rtt_ms": 1.5},
                    "public_peer_ping_summary": {"up_count": 3, "avg_rtt_ms": 110.0},
                    "miner_ping_summary": {"up_count": 7, "avg_rtt_ms": 2.0},
                    "miner_pings": [{"miner": "Ajax", "ip": "192.168.1.11", "up": True, "rtt_ms": 1.1}],
                },
            },
            True,
            0.01,
        )
        output = metrics.render().decode("utf-8")

        self.assertIn("# TYPE bdag_p2p_guard_up gauge", output)
        self.assertIn(
            'bdag_p2p_node_active_primary{health_scope="production",node="bdag-miner-node-1",role="managed"} 1.0',
            output,
        )
        self.assertIn('bdag_network_gateway_rtt_ms{gateway="192.168.1.1"} 1.5', output)
        self.assertIn('bdag_lan_miner_ping_up{ip="192.168.1.11",miner="Ajax"} 1.0', output)


if __name__ == "__main__":
    unittest.main()
