#!/usr/bin/env python3

import os
import pathlib
import sys
import unittest
from unittest import mock

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import stack_status_source  # noqa: E402


class StackStatusSourceTests(unittest.TestCase):
    def test_dashboard_asic_telemetry_enrichment_marks_mcb_api_stall(self) -> None:
        mining_address = "0xD5F8DF0A60bC1Ff4636250b2a2dff319Bf79B1f5"
        payload = {
            "mining_address": mining_address,
            "pool_health": {"initial_download": False},
            "pool_job_state": {
                "active_connections": "1",
                "authorized_connections": "1",
                "ready_connections": "1",
                "clients": [
                    {
                        "asic_mac": "28:e2:97:3d:95:13",
                        "authorized": True,
                        "current_job_id": "job-1",
                    }
                ],
            },
            "asic_telemetry": {
                "devices": [
                    {
                        "mac": "28:E2:97:2E:00:1B",
                        "status": "degraded",
                        "errors": {
                            "pools": 'Get "http://192.168.100.80/mcb/pools": context deadline exceeded',
                            "cgminer_devs": (
                                'Get "http://192.168.100.80/mcb/cgminer?cgminercmd=devs": '
                                "context deadline exceeded"
                            ),
                        },
                    },
                    {
                        "mac": "28:e2:97:3d:95:13",
                        "status": "ok",
                        "errors": {},
                    },
                ],
            },
            "miner_health": {
                "connected_count": 0,
                "miners": [
                    {
                        "mac": "28:e2:97:2e:00:1b",
                        "ip": "192.168.100.80",
                        "managed": True,
                        "device_type": "asic",
                        "status": "configured",
                        "health": "configured",
                        "configured_pool_url": "stratum+tcp://192.168.100.114:3334",
                        "intended_wallet": mining_address,
                    },
                    {
                        "mac": "28:e2:97:3d:95:13",
                        "ip": "192.168.100.83",
                        "managed": True,
                        "device_type": "asic",
                        "status": "configured",
                        "health": "configured",
                        "configured_pool_url": "stratum+tcp://192.168.100.114:3334",
                        "intended_wallet": mining_address,
                    },
                ],
            },
        }

        enriched = stack_status_source._with_dashboard_asic_telemetry_enrichment(payload)

        miners = {row["mac"]: row for row in enriched["miner_health"]["miners"]}
        stalled = miners["28:e2:97:2e:00:1b"]
        active = miners["28:e2:97:3d:95:13"]

        self.assertTrue(enriched["asic_telemetry_enriched"])
        self.assertEqual("down", stalled["status"])
        self.assertEqual("down", stalled["health"])
        self.assertFalse(stalled["connected"])
        self.assertIn("/mcb/pools", stalled["api_error"])
        self.assertIn("cgminercmd=devs", stalled["debug_error"])
        self.assertFalse(stalled["debug"]["available"])
        self.assertEqual("stratum+tcp://192.168.100.114:3334", stalled["expected_pool_url"])
        self.assertEqual(mining_address, stalled["expected_worker_user"])

        self.assertTrue(active["connected"])
        self.assertTrue(active["pool_active"])
        self.assertTrue(active["work_pool_active"])
        self.assertEqual(1, enriched["pool_health"]["job_notify_count"])
        self.assertEqual(1, enriched["miner_health"]["connected_count"])

    def test_dashboard_asic_telemetry_all_down_overrides_stale_connected_rows(self) -> None:
        mining_address = "0xD5F8DF0A60bC1Ff4636250b2a2dff319Bf79B1f5"
        timeout_error = 'Get "http://192.168.100.80/mcb/pools": context deadline exceeded'
        payload = {
            "mining_address": mining_address,
            "pool_health": {
                "initial_download": False,
                "job_state_reason": "no_active_miners",
            },
            "pool_job_state": {
                "active_connections": 0,
                "authorized_connections": 0,
                "ready_connections": 0,
                "reason_code": "no_active_miners",
                "clients": [],
            },
            "pool_metrics": {
                "active_connections": 0,
                "authorized_miners": 0,
                "ready_miners": 0,
            },
            "asic_telemetry": {
                "devices": [
                    {
                        "mac": "28:e2:97:2e:00:1b",
                        "status": "degraded",
                        "errors": {"pools": timeout_error},
                    },
                    {
                        "mac": "28:e2:97:3d:95:13",
                        "status": "degraded",
                        "errors": {"cgminer_devs": "HTTP 500 Server Error"},
                    },
                ],
            },
            "miner_health": {
                "connected_count": 2,
                "connected_count_effective": 2,
                "managed_count": 2,
                "miners": [
                    {
                        "mac": "28:e2:97:2e:00:1b",
                        "ip": "192.168.100.80",
                        "managed": True,
                        "device_type": "asic",
                        "status": "connected",
                        "health": "connected",
                        "connected": True,
                        "configured_pool_url": "stratum+tcp://192.168.100.114:3334",
                        "intended_wallet": mining_address,
                    },
                    {
                        "mac": "28:e2:97:3d:95:13",
                        "ip": "192.168.100.83",
                        "managed": True,
                        "device_type": "asic",
                        "status": "connected",
                        "health": "connected",
                        "connected": True,
                        "configured_pool_url": "stratum+tcp://192.168.100.114:3334",
                        "intended_wallet": mining_address,
                    },
                ],
            },
        }

        enriched = stack_status_source._with_dashboard_asic_telemetry_enrichment(payload)

        self.assertEqual(0, enriched["miner_health"]["connected_count"])
        self.assertEqual(0, enriched["miner_health"]["connected_count_effective"])
        for row in enriched["miner_health"]["miners"]:
            self.assertEqual("down", row["status"])
            self.assertEqual("down", row["health"])
            self.assertFalse(row["connected"])
            self.assertFalse(row["pool_active"])
            self.assertFalse(row["work_pool_active"])
            self.assertFalse(row["debug"]["available"])

    def test_dashboard_payload_is_enriched_with_new_pool_metrics(self) -> None:
        captured: list[dict[str, object]] = []

        def collect_pool_metrics(containers: dict[str, object]) -> dict[str, object]:
            captured.append(containers)
            return {
                "status": "ok",
                "active_connections": 0,
                "stratum_no_request_disconnects": {"mac:no-request-eof": 12},
                "stratum_no_request_disconnects_total": 12.0,
                "stratum_server_first_difficulty_probes": {"sent": 12},
                "stratum_server_first_difficulty_probes_total": 12.0,
            }

        with mock.patch.dict(
            os.environ,
            {"BDAG_STATUS_SOURCE_URL": "http://dashboard:8088/api/status"},
            clear=False,
        ), mock.patch.object(
            stack_status_source,
            "fetch_status_endpoint",
            return_value={
                "status": "ok",
                "containers": {"pool": {"running": True}},
                "pool_metrics": {"active_connections": 0},
            },
        ), mock.patch.object(
            stack_status_source,
            "collect_pool_prometheus_metrics",
            side_effect=collect_pool_metrics,
        ), mock.patch.object(
            stack_status_source,
            "POOL_CONTAINERS",
            ["pool"],
        ), mock.patch.object(
            stack_status_source,
            "docker_inspect",
            return_value={},
        ):
            payload = stack_status_source.collect_stack_status(prefer_http=True)

        self.assertTrue(payload["pool_metrics_enriched"])
        self.assertEqual(12.0, payload["pool_metrics"]["stratum_no_request_disconnects_total"])
        self.assertEqual("host", captured[0]["pool"]["network_mode"])

    def test_collect_stack_status_defaults_to_in_process_cache_before_http(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"BDAG_STATUS_SOURCE_URL": "http://dashboard:8088/api/status"},
            clear=False,
        ), mock.patch.object(
            stack_status_source,
            "fetch_status_endpoint",
            side_effect=AssertionError("default status source must not call dashboard HTTP"),
        ), mock.patch.object(
            stack_status_source,
            "collect_status_cached",
            return_value={"overall": "ok"},
        ):
            payload = stack_status_source.collect_stack_status()

        self.assertEqual("ok", payload["overall"])
        self.assertEqual("in-process", payload["stack_status_source"]["source"])

    def test_dashboard_payload_is_enriched_with_container_lifecycle_fields(self) -> None:
        payload = {
            "status": "ok",
            "containers": {
                "node": {"running": True},
                "pool": {
                    "running": True,
                    "started_at": "2026-06-25T10:00:00.000000000Z",
                },
            },
        }

        with mock.patch.object(
            stack_status_source,
            "SERVICES",
            ["node", "pool"],
        ), mock.patch.object(
            stack_status_source,
            "docker_inspect",
            return_value={
                "node": {
                    "running": True,
                    "status": "running",
                    "started_at": "2026-06-25T11:00:00.000000000Z",
                    "restart_count": 4,
                },
                "pool": {
                    "running": True,
                    "status": "running",
                    "started_at": "2026-06-25T11:30:00.000000000Z",
                },
            },
        ):
            enriched = stack_status_source._with_direct_container_lifecycle_enrichment(payload)

        self.assertTrue(enriched["container_lifecycle_enriched"])
        self.assertEqual(
            "2026-06-25T11:00:00.000000000Z",
            enriched["containers"]["node"]["started_at"],
        )
        self.assertEqual(4, enriched["containers"]["node"]["restart_count"])
        self.assertEqual(
            "2026-06-25T10:00:00.000000000Z",
            enriched["containers"]["pool"]["started_at"],
        )

    def test_metric_enrichment_failure_keeps_original_payload(self) -> None:
        with mock.patch.object(
            stack_status_source,
            "collect_pool_prometheus_metrics",
            side_effect=RuntimeError("metrics down"),
        ):
            payload = stack_status_source._with_direct_pool_metric_enrichment(
                {"pool_metrics": {"active_connections": 0}}
            )

        self.assertEqual({"active_connections": 0}, payload["pool_metrics"])
        self.assertNotIn("pool_metrics_enriched", payload)

    def test_metric_enrichment_runs_when_backend_health_missing(self) -> None:
        with mock.patch.object(
            stack_status_source,
            "collect_pool_prometheus_metrics",
            return_value={
                "status": "ok",
                "source_backend_health": {
                    "node": {"selected": True, "node_p2p_best_peer_lead_blocks": 733}
                },
                "selected_backend_source_health": {
                    "selected": True,
                    "node_p2p_best_peer_lead_blocks": 733,
                },
            },
        ):
            payload = stack_status_source._with_direct_pool_metric_enrichment(
                {
                    "containers": {"pool": {"running": True}},
                    "pool_metrics": {
                        "active_connections": 4,
                        "stratum_no_request_disconnects": {},
                        "stratum_no_request_disconnects_total": 0,
                        "stratum_server_first_difficulty_probes": {},
                        "stratum_server_first_difficulty_probes_total": 0,
                    },
                }
            )

        self.assertTrue(payload["pool_metrics_enriched"])
        self.assertEqual(
            733,
            payload["pool_metrics"]["selected_backend_source_health"]["node_p2p_best_peer_lead_blocks"],
        )

    def test_metric_enrichment_fills_sparse_backend_health_peer_counts(self) -> None:
        with mock.patch.object(
            stack_status_source,
            "collect_pool_prometheus_metrics",
            return_value={
                "status": "ok",
                "source_backend_health": {
                    "node": {
                        "node_p2p_best_peer_lead_blocks": -1,
                        "node_p2p_fresh_consensus_peer_count": 3,
                        "node_p2p_consensus_peer_count": 4,
                    }
                },
                "selected_backend_source_health": {
                    "node_p2p_best_peer_lead_blocks": -1,
                    "node_p2p_fresh_consensus_peer_count": 3,
                    "node_p2p_consensus_peer_count": 4,
                },
                "stratum_no_request_disconnects": {},
                "stratum_no_request_disconnects_total": 0,
                "stratum_server_first_difficulty_probes": {},
                "stratum_server_first_difficulty_probes_total": 0,
            },
        ):
            payload = stack_status_source._with_direct_pool_metric_enrichment(
                {
                    "containers": {"pool": {"running": True}},
                    "pool_metrics": {
                        "selected_backend_source_health": {
                            "node_mineable": True,
                            "node_p2p_best_peer_lead_blocks": -1,
                        },
                    },
                }
            )

        selected = payload["pool_metrics"]["selected_backend_source_health"]
        self.assertTrue(payload["pool_metrics_enriched"])
        self.assertTrue(selected["node_mineable"])
        self.assertEqual(-1, selected["node_p2p_best_peer_lead_blocks"])
        self.assertEqual(3, selected["node_p2p_fresh_consensus_peer_count"])
        self.assertEqual(4, selected["node_p2p_consensus_peer_count"])


if __name__ == "__main__":
    unittest.main()
