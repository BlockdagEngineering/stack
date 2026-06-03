#!/usr/bin/env python3

import pathlib
import sys
import unittest
from unittest import mock

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import watchdog  # noqa: E402


ADDRESS = "0x1111111111111111111111111111111111111111"
DEFAULT_POOL_URL = watchdog.default_miner_pool_settings()["pool_url"]


def miner_row(
    ip: str,
    *,
    lane_status: str,
    submits: int = 0,
    shares: int = 0,
    last_submit_epoch: int = 0,
    last_pool_seen_epoch: int = 0,
) -> dict[str, object]:
    return {
        "connected": True,
        "device_type": "stratum",
        "display_name": ip,
        "expected_pool_url": DEFAULT_POOL_URL,
        "expected_worker_user": ADDRESS,
        "ip": ip,
        "lane_status": lane_status,
        "last_pool_seen_epoch": last_pool_seen_epoch,
        "last_submit_epoch": last_submit_epoch,
        "shares": shares,
        "submits": submits,
        "workers": [ADDRESS],
    }


def status_for(miners: list[dict[str, object]], *, expected: int, imbalanced: int) -> dict[str, object]:
    return {
        "mining_address": ADDRESS,
        "pool_health": {"initial_download": False, "job_notify_count": 1},
        "sync_health": {},
        "sync_progress": {"remaining_blocks": 0, "status": "synced"},
        "miner_health": {
            "connected_count": len(miners),
            "managed_count": 0,
            "lane_balance": {
                "expected_lane_count": expected,
                "imbalanced_count": imbalanced,
            },
            "miners": miners,
        },
    }


class WatchdogMinerSourceCountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1_779_180_000
        self.old_time = watchdog.time.time
        watchdog.time.time = lambda: self.now

    def tearDown(self) -> None:
        watchdog.time.time = self.old_time

    def test_zero_miners_do_not_create_degradation(self) -> None:
        status = status_for([], expected=0, imbalanced=0)

        self.assertEqual([], watchdog.degraded_primary_miners(status, 120))

    def test_no_window_work_does_not_make_all_miners_degraded(self) -> None:
        miners = [
            miner_row(f"192.168.1.{14 + index}", lane_status="no-window-work", last_pool_seen_epoch=self.now - 15)
            for index in range(5)
        ]
        status = status_for(miners, expected=5, imbalanced=0)

        self.assertEqual([], watchdog.degraded_primary_miners(status, 120))

    def test_pool_seen_without_submit_is_not_connected_submitting(self) -> None:
        miners = [
            miner_row("192.168.1.14", lane_status="no-work", last_pool_seen_epoch=self.now - 15),
            miner_row("192.168.1.15", lane_status="balanced", shares=12, last_submit_epoch=self.now - 10),
        ]
        status = status_for(miners, expected=2, imbalanced=1)

        self.assertEqual([], watchdog.degraded_primary_miners(status, 120))

    def test_work_pool_active_false_overrides_stale_connection_identity(self) -> None:
        row = miner_row("192.168.1.14", lane_status="no-work", submits=1, last_submit_epoch=self.now - 15)
        row["work_pool_active"] = False
        status = status_for([row], expected=1, imbalanced=1)

        self.assertEqual([], watchdog.degraded_primary_miners(status, 120))

    def test_low_lane_with_recent_submit_is_degraded(self) -> None:
        miners = [
            miner_row("192.168.1.14", lane_status="low", submits=1, last_submit_epoch=self.now - 15),
            miner_row("192.168.1.15", lane_status="balanced", shares=12, last_submit_epoch=self.now - 10),
        ]
        status = status_for(miners, expected=2, imbalanced=1)

        degraded = watchdog.degraded_primary_miners(status, 120)

        self.assertEqual(1, len(degraded))
        self.assertEqual("192.168.1.14", degraded[0]["ip"])

    def test_useful_work_stall_timer_uses_mac_and_survives_transient_degraded_sample(self) -> None:
        row = miner_row("192.168.1.16", lane_status="no-work", submits=1, last_submit_epoch=self.now - 200)
        row["mac"] = "28:e2:97:4d:44:3a"
        row["device_id"] = "mac:28:e2:97:4d:44:3a"
        state = {"miner_useful_work_stall_since": {"mac:28:e2:97:4d:44:3a": self.now - 180}}

        since = watchdog.update_useful_work_stall_since(state, [], [row], self.now)

        self.assertEqual({"mac:28:e2:97:4d:44:3a": self.now - 180}, since)

    def test_useful_work_stall_timer_migrates_legacy_ip_key_to_mac(self) -> None:
        row = miner_row("192.168.1.16", lane_status="no-work", submits=1, last_submit_epoch=self.now - 200)
        row["mac"] = "28:e2:97:4d:44:3a"
        state = {"miner_useful_work_stall_since": {"192.168.1.16": self.now - 180}}

        since = watchdog.update_useful_work_stall_since(state, [], [row], self.now)

        self.assertEqual({"mac:28:e2:97:4d:44:3a": self.now - 180}, since)

    def test_useful_work_stall_timer_clears_after_recovery(self) -> None:
        state = {"miner_useful_work_stall_since": {"mac:28:e2:97:4d:44:3a": self.now - 180}}

        since = watchdog.update_useful_work_stall_since(state, [], [], self.now)

        self.assertEqual({}, since)

    def test_failed_expired_job_reconnect_without_clients_restarts_pool(self) -> None:
        state: dict[str, object] = {}
        events: list[tuple[str, str, str, dict[str, object]]] = []
        restarts: list[str] = []
        written: list[dict[str, object]] = []
        status = {
            "failures": [],
            "stack_failures": [],
            "miner_failures": [],
            "mining_address": ADDRESS,
            "nodes": {},
            "sync_health": {},
            "sync_progress": {"status": "synced", "remaining_blocks": 0, "nodes": {}},
            "pool_health": {
                "expired_job_reconnect_failed_no_share": True,
                "expired_job_reconnect_count": 14,
                "expired_job_reauthorize_after_reconnect_count": 14,
                "expired_job_client_timeout_after_reconnect_count": 1,
                "expired_job_client_timeout_last_at": "2026-06-03T01:08:08",
                "expired_job_client_timeout_last_line": "2026/06/03 01:08:08 [192.168.1.16:33726] read error: i/o timeout",
                "stale_submit_count": 180,
                "valid_share_count": 0,
            },
            "miner_health": {
                "connected_count": 0,
                "connected_count_effective": 0,
                "miners": [],
            },
        }

        def record(event_type: str, severity: str, message: str, details=None) -> None:
            events.append((event_type, severity, message, details or {}))

        with mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_status_cached", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "constrained_fastartifact_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", side_effect=record
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_pool_restart", side_effect=lambda reason: restarts.append(reason) or True
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("pool_expired_job_reconnect_exhausted", result["watchdog_state"]["last_status"])
        self.assertEqual(1, len(restarts))
        self.assertIn("pool expired-job reconnect exhausted", restarts[0])
        self.assertEqual("pool_expired_job_reconnect_exhausted", events[0][0])
        self.assertEqual("critical", events[0][1])
        self.assertTrue(events[0][3]["expired_job_reconnect_failed"])
        self.assertTrue(written)


if __name__ == "__main__":
    unittest.main()
