#!/usr/bin/env python3

import pathlib
import sys
import unittest
from types import SimpleNamespace
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


def api_stalled_asic_row(
    ip: str = "192.168.1.16",
    *,
    managed: bool = True,
    mac: str = "28:e2:97:4d:44:3a",
    stale_age: int = 600,
    status: str = "down",
    device_telemetry_errors: str | None = None,
) -> dict[str, object]:
    return {
        "configured": False,
        "connected": False,
        "debug": {"available": False},
        "debug_error": "HTTP 500 Server Error",
        "device_accepted": "0",
        "device_id": f"mac:{mac}",
        "device_telemetry_errors": device_telemetry_errors,
        "device_telemetry_status": "degraded" if device_telemetry_errors else None,
        "device_type": "asic",
        "display_name": ip,
        "expected_pool_url": DEFAULT_POOL_URL,
        "expected_worker_user": ADDRESS,
        "hashrate": "unknown",
        "ip": ip,
        "issue": f"miner request failed for {ip}/mcb/cgminer?cgminercmd=devs: timed out",
        "last_pool_seen_age_seconds": stale_age,
        "mac": mac,
        "managed": managed,
        "pool_active": False,
        "status": status,
        "work_pool_active": False,
        "workers": [ADDRESS],
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

    def test_api_stall_detector_requires_managed_primary_asic_and_clear_pool_faults(self) -> None:
        status = status_for([api_stalled_asic_row()], expected=1, imbalanced=0)

        affected = watchdog.asic_api_stall_primary_miners(status, stale_seconds=180)

        self.assertEqual(1, len(affected))
        self.assertEqual("192.168.1.16", affected[0]["ip"])
        self.assertTrue(affected[0]["restart_open_first"])

        unmanaged_status = status_for([api_stalled_asic_row(managed=False)], expected=1, imbalanced=0)
        self.assertEqual([], watchdog.asic_api_stall_primary_miners(unmanaged_status, stale_seconds=180))

        pool_fault_status = status_for([api_stalled_asic_row()], expected=1, imbalanced=0)
        pool_fault_status["pool_health"]["expired_job_reconnect_failed_no_share"] = True
        self.assertEqual([], watchdog.asic_api_stall_primary_miners(pool_fault_status, stale_seconds=180))

    def test_api_stall_detector_handles_all_mcb_miners_down_without_job_notify_count(self) -> None:
        rows = [
            api_stalled_asic_row(ip="192.168.1.16", mac="28:e2:97:4d:44:3a"),
            api_stalled_asic_row(ip="192.168.1.17", mac="28:e2:97:4d:44:3b"),
        ]
        status = {
            "mining_address": ADDRESS,
            "pool_health": {
                "initial_download": False,
                "job_state_reason": "no_active_miners",
            },
            "pool_job_state": {
                "active_connections": 0,
                "authorized_connections": 0,
                "ready_connections": 0,
                "reason_code": "no_active_miners",
            },
            "pool_metrics": {
                "active_connections": 0,
                "stratum_no_request_disconnects_total": 100,
            },
            "sync_health": {},
            "sync_progress": {"remaining_blocks": 0, "status": "synced"},
            "miner_health": {
                "connected_count": 0,
                "connected_count_effective": 0,
                "managed_count": 2,
                "miners": rows,
            },
        }

        affected = watchdog.asic_api_stall_primary_miners(status, stale_seconds=180)

        self.assertEqual(["192.168.1.16", "192.168.1.17"], [item["ip"] for item in affected])
        self.assertTrue(all(item["api_stall_no_active_pool"] for item in affected))
        self.assertTrue(all(item["restart_open_first"] for item in affected))

    def test_api_stall_detector_handles_no_stratum_api_dead_signature(self) -> None:
        telemetry_errors = (
            'cgminer_devs: Get "http://192.168.1.16/mcb/cgminer?cgminercmd=devs": '
            "context deadline exceeded | "
            'pools: Get "http://192.168.1.16/mcb/pools": context deadline exceeded'
        )
        row = api_stalled_asic_row(
            ip="192.168.1.16",
            mac="28:e2:97:4d:44:3a",
            status="no-stratum",
            stale_age=0,
            device_telemetry_errors=telemetry_errors,
        )
        row["debug"] = {}
        row["debug_error"] = None
        row["expected_pool_url"] = None
        row["expected_worker_user"] = None
        row["configured_pool_url"] = DEFAULT_POOL_URL
        row["intended_wallet"] = ADDRESS
        row["workers"] = []
        row["issue"] = "no active stratum connection from managed ASIC"
        status = {
            "mining_address": ADDRESS,
            "pool_health": {
                "initial_download": False,
                "job_state_reason": "no_active_miners",
            },
            "pool_job_state": {
                "active_connections": 0,
                "authorized_connections": 0,
                "ready_connections": 0,
                "reason_code": "no_active_miners",
            },
            "pool_metrics": {
                "active_connections": 0,
                "stratum_no_request_disconnects_total": 50,
            },
            "sync_health": {},
            "sync_progress": {"remaining_blocks": 0, "status": "synced"},
            "miner_health": {
                "connected_count": 0,
                "connected_count_effective": 0,
                "managed_count": 1,
                "miners": [row],
            },
        }

        affected = watchdog.asic_api_stall_primary_miners(status, stale_seconds=180)

        self.assertEqual(1, len(affected))
        self.assertEqual("192.168.1.16", affected[0]["ip"])
        self.assertEqual("no-stratum", affected[0]["status"])
        self.assertTrue(affected[0]["api_stall_no_active_pool"])
        self.assertIn("/mcb/pools", affected[0]["api_stall_issue"])
        self.assertTrue(affected[0]["restart_open_first"])

    def test_api_stall_detector_does_not_restart_share_producing_asic_for_telemetry_only(self) -> None:
        row = api_stalled_asic_row(
            status="degraded",
            stale_age=2,
            device_telemetry_errors="cgminer_devs: HTTP 500",
        )
        row.update(
            {
                "connected": True,
                "ready": True,
                "pool_active": True,
                "work_pool_active": True,
                "shares": 25,
                "last_share_age_seconds": 2,
            }
        )
        status = {
            "mining_address": ADDRESS,
            "pool_health": {
                "initial_download": False,
                "job_state_reason": "no_active_miners",
            },
            "pool_job_state": {
                "active_connections": 0,
                "authorized_connections": 0,
                "ready_connections": 0,
                "reason_code": "no_active_miners",
            },
            "pool_metrics": {
                "active_connections": 0,
                "stratum_no_request_disconnects_total": 50,
            },
            "sync_health": {},
            "sync_progress": {"remaining_blocks": 0, "status": "synced"},
            "miner_health": {
                "connected_count": 1,
                "connected_count_effective": 1,
                "managed_count": 1,
                "miners": [row],
            },
        }

        self.assertEqual([], watchdog.asic_api_stall_primary_miners(status, stale_seconds=180))

    def test_api_stall_watchdog_restarts_one_asic_open_first_after_confirmation(self) -> None:
        row = api_stalled_asic_row()
        status = {
            "failures": [],
            "stack_failures": [],
            "miner_failures": ["miner request failed for 192.168.1.16/mcb/cgminer?cgminercmd=devs: timed out"],
            "mining_address": ADDRESS,
            "nodes": {},
            "sync_health": {},
            "sync_progress": {"status": "synced", "remaining_blocks": 0, "nodes": {}},
            "pool_health": {
                "initial_download": False,
                "job_notify_count": 1,
                "valid_share_count": 20,
            },
            "miner_health": {
                "connected_count": 1,
                "connected_count_effective": 1,
                "managed_count": 1,
                "miners": [row],
            },
        }
        state = {"asic_api_stall_since": {"mac:28:e2:97:4d:44:3a": self.now - 180}}
        restarts: list[tuple[list[dict[str, object]], str]] = []
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog,
            "run_miner_restarts",
            side_effect=lambda targets, reason: restarts.append((targets, reason))
            or {"status": "ok", "target_count": len(targets), "results": []},
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("asic_api_stall", result["watchdog_state"]["last_status"])
        self.assertEqual(1, len(restarts))
        self.assertIn("ASIC API-stall watchdog", restarts[0][1])
        self.assertEqual("192.168.1.16", restarts[0][0][0]["ip"])
        self.assertTrue(restarts[0][0][0]["restart_open_first"])
        self.assertEqual({"192.168.1.16": self.now}, result["watchdog_state"]["last_miner_restart_at_by_ip"])
        self.assertEqual(
            {"mac:28:e2:97:4d:44:3a": self.now - 180},
            result["watchdog_state"]["asic_api_stall_since"],
        )
        self.assertEqual(
            self.now,
            result["watchdog_state"]["asic_staged_recovery_by_identity"]["mac:28:e2:97:4d:44:3a"][
                "open_restart_at"
            ],
        )
        self.assertTrue(written)

    def test_api_stall_watchdog_restarts_all_mcb_miners_when_pool_has_no_active_miners(self) -> None:
        rows = [
            api_stalled_asic_row(ip="192.168.1.16", mac="28:e2:97:4d:44:3a"),
            api_stalled_asic_row(ip="192.168.1.17", mac="28:e2:97:4d:44:3b"),
        ]
        status = {
            "failures": [],
            "stack_failures": [],
            "miner_failures": [],
            "mining_address": ADDRESS,
            "nodes": {},
            "sync_health": {},
            "sync_progress": {"status": "synced", "remaining_blocks": 0, "nodes": {}},
            "pool_health": {
                "initial_download": False,
                "job_state_reason": "no_active_miners",
                "valid_share_count": 20,
            },
            "pool_job_state": {
                "active_connections": 0,
                "authorized_connections": 0,
                "ready_connections": 0,
                "reason_code": "no_active_miners",
            },
            "pool_metrics": {
                "active_connections": 0,
                "stratum_no_request_disconnects_total": 100,
            },
            "miner_health": {
                "connected_count": 0,
                "connected_count_effective": 0,
                "managed_count": 2,
                "miners": rows,
            },
        }
        state = {
            "asic_api_stall_since": {
                "mac:28:e2:97:4d:44:3a": self.now - watchdog.DEFAULT_ASIC_API_STALL_NO_ACTIVE_CONFIRM_SECONDS,
                "mac:28:e2:97:4d:44:3b": self.now - watchdog.DEFAULT_ASIC_API_STALL_NO_ACTIVE_CONFIRM_SECONDS,
            }
        }
        restarts: list[tuple[list[dict[str, object]], str]] = []
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog,
            "run_miner_restarts",
            side_effect=lambda targets, reason: restarts.append((targets, reason))
            or {"status": "ok", "target_count": len(targets), "results": []},
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("asic_api_stall", result["watchdog_state"]["last_status"])
        self.assertEqual(1, len(restarts))
        self.assertIn("ASIC API-stall watchdog", restarts[0][1])
        self.assertEqual(["192.168.1.16", "192.168.1.17"], [item["ip"] for item in restarts[0][0]])
        self.assertTrue(all(item["restart_open_first"] for item in restarts[0][0]))
        self.assertEqual(
            {"192.168.1.16": self.now, "192.168.1.17": self.now},
            result["watchdog_state"]["last_miner_restart_at_by_ip"],
        )
        self.assertEqual(
            {
                "mac:28:e2:97:4d:44:3a": self.now - watchdog.DEFAULT_ASIC_API_STALL_NO_ACTIVE_CONFIRM_SECONDS,
                "mac:28:e2:97:4d:44:3b": self.now - watchdog.DEFAULT_ASIC_API_STALL_NO_ACTIVE_CONFIRM_SECONDS,
            },
            result["watchdog_state"]["asic_api_stall_since"],
        )
        staged = result["watchdog_state"]["asic_staged_recovery_by_identity"]
        self.assertEqual(self.now, staged["mac:28:e2:97:4d:44:3a"]["open_restart_at"])
        self.assertEqual(self.now, staged["mac:28:e2:97:4d:44:3b"]["open_restart_at"])
        self.assertTrue(written)

    def test_api_stall_watchdog_escalates_to_authenticated_config_rewrite(self) -> None:
        row = api_stalled_asic_row()
        status = {
            "failures": [],
            "stack_failures": [],
            "miner_failures": ["miner request failed for 192.168.1.16/mcb/cgminer?cgminercmd=devs: timed out"],
            "mining_address": ADDRESS,
            "nodes": {},
            "sync_health": {},
            "sync_progress": {"status": "synced", "remaining_blocks": 0, "nodes": {}},
            "pool_health": {
                "initial_download": False,
                "job_notify_count": 1,
                "valid_share_count": 20,
            },
            "miner_health": {
                "connected_count": 1,
                "connected_count_effective": 1,
                "managed_count": 1,
                "miners": [row],
            },
        }
        identity = "mac:28:e2:97:4d:44:3a"
        state = {
            "asic_api_stall_since": {identity: self.now - 900},
            "asic_staged_recovery_by_identity": {
                identity: {
                    "first_seen_at": self.now - 900,
                    "open_restart_at": self.now - watchdog.DEFAULT_ASIC_STAGED_AUTH_RETRY_SECONDS,
                    "last_stage": "open-restart",
                }
            },
        }
        restarts: list[tuple[list[dict[str, object]], str]] = []

        with mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", lambda _payload: None
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog,
            "run_miner_restarts",
            side_effect=lambda targets, reason: restarts.append((targets, reason))
            or {"status": "ok", "target_count": len(targets), "results": []},
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("asic_api_stall", result["watchdog_state"]["last_status"])
        self.assertEqual(1, len(restarts))
        self.assertEqual("auth-restart-configure", restarts[0][0][0]["staged_recovery_stage"])
        self.assertFalse(restarts[0][0][0]["restart_open_first"])
        staged = result["watchdog_state"]["asic_staged_recovery_by_identity"][identity]
        self.assertEqual(self.now, staged["auth_retry_at"])
        self.assertEqual("auth-restart-configure", staged["last_stage"])

    def test_api_stall_watchdog_marks_hardware_power_cycle_required_after_retry_window(self) -> None:
        row = api_stalled_asic_row()
        status = {
            "failures": [],
            "stack_failures": [],
            "miner_failures": ["miner request failed for 192.168.1.16/mcb/cgminer?cgminercmd=devs: timed out"],
            "mining_address": ADDRESS,
            "nodes": {},
            "sync_health": {},
            "sync_progress": {"status": "synced", "remaining_blocks": 0, "nodes": {}},
            "pool_health": {
                "initial_download": False,
                "job_notify_count": 1,
                "valid_share_count": 20,
            },
            "miner_health": {
                "connected_count": 1,
                "connected_count_effective": 1,
                "managed_count": 1,
                "miners": [row],
            },
        }
        identity = "mac:28:e2:97:4d:44:3a"
        state = {
            "asic_api_stall_since": {identity: self.now - 1200},
            "asic_staged_recovery_by_identity": {
                identity: {
                    "first_seen_at": self.now - 1200,
                    "open_restart_at": self.now - 900,
                    "auth_retry_at": self.now - watchdog.DEFAULT_ASIC_STAGED_POWER_CYCLE_SECONDS,
                    "last_stage": "auth-restart-configure",
                }
            },
        }
        events: list[tuple[str, str, str, dict[str, object]]] = []

        def record(event_type: str, severity: str, message: str, details=None) -> None:
            events.append((event_type, severity, message, details or {}))

        with mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", lambda _payload: None
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", side_effect=record
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_miner_restarts", side_effect=AssertionError("hardware stage must not restart again")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("asic_hardware_power_cycle_required", result["watchdog_state"]["last_status"])
        staged = result["watchdog_state"]["asic_staged_recovery_by_identity"][identity]
        self.assertEqual(self.now, staged["power_cycle_required_at"])
        self.assertEqual("hardware-power-cycle-required", staged["last_stage"])
        self.assertTrue(any(event[0] == "asic_hardware_power_cycle_required" for event in events))
        self.assertTrue(any("power-cycle required" in event[2] for event in events))

    def test_remote_power_cycle_runs_configured_command_by_mac(self) -> None:
        row = api_stalled_asic_row()
        state: dict[str, object] = {}
        logged: list[list[str]] = []

        class DummyLock:
            def close(self) -> None:
                pass

        with mock.patch.dict(
            watchdog.os.environ,
            {"BDAG_ASIC_POWER_CYCLE_COMMAND_BY_MAC": "28:e2:97:4d:44:3a=/bin/true {mac} {ip}"},
            clear=False,
        ), mock.patch.object(
            watchdog, "automation_mutation_allowed", return_value=True
        ), mock.patch.object(
            watchdog, "acquire_lock", return_value=DummyLock()
        ), mock.patch.object(
            watchdog, "write_action_state", lambda _payload: None
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog,
            "run_logged",
            side_effect=lambda command, _log_path, timeout=None: logged.append(command)
            or SimpleNamespace(ok=True, returncode=0, elapsed=0.1),
        ):
            result = watchdog.run_asic_remote_power_cycles([row], "hardware required", state, self.now)

        self.assertEqual("ok", result["status"])
        self.assertEqual([["/bin/sh", "-c", "/bin/true 28:e2:97:4d:44:3a 192.168.1.16"]], logged)
        self.assertEqual(
            self.now,
            state["last_asic_power_cycle_at_by_identity"]["mac:28:e2:97:4d:44:3a"],
        )

    def test_failed_expired_job_reconnect_without_clients_restarts_pool(self) -> None:
        state: dict[str, object] = {}
        events: list[tuple[str, str, str, dict[str, object]]] = []
        restarts: list[str] = []
        written: list[dict[str, object]] = []
        status = {
            "failures": [],
            "stack_failures": [],
            "miner_failures": [],
            "overall": "ok",
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
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
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

    def test_missing_top_level_failures_does_not_hide_stratum_no_request_event(self) -> None:
        row = miner_row("192.168.1.16", lane_status="no-work")
        row["mac"] = "28:e2:97:4d:44:3a"
        row["device_id"] = "mac:28:e2:97:4d:44:3a"
        row["managed"] = True
        status = {
            "stack_failures": [],
            "miner_failures": [],
            "overall": "ok",
            "mining_address": ADDRESS,
            "nodes": {},
            "sync_health": {},
            "sync_progress": {"status": "synced", "remaining_blocks": 0, "nodes": {}},
            "pool_health": {
                "initial_download": False,
                "job_notify_count": 1,
                "valid_share_count": 0,
            },
            "pool_metrics": {
                "active_connections": 0,
                "stratum_no_request_disconnects": {"mac:no-request-eof": 14},
                "stratum_no_request_disconnects_total": 14,
            },
            "miner_health": {
                "connected_count": 0,
                "connected_count_effective": 0,
                "managed_count": 1,
                "miners": [row],
            },
        }
        state = {"last_stratum_no_request_disconnects_total": 4}
        events: list[tuple[str, str, str, dict[str, object]]] = []

        def record(event_type: str, severity: str, message: str, details=None) -> None:
            events.append((event_type, severity, message, details or {}))

        with mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", lambda _payload: None
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", side_effect=record
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=False)

        self.assertEqual(14, result["watchdog_state"]["last_stratum_no_request_disconnects_total"])
        self.assertEqual(10, result["watchdog_state"]["last_stratum_no_request_disconnects_delta"])
        self.assertEqual("stratum_no_request_disconnects", events[0][0])
        self.assertEqual("warning", events[0][1])

    def test_mac_classified_no_request_counts_without_miner_rows(self) -> None:
        status = {
            "stack_failures": [],
            "miner_failures": [],
            "overall": "degraded",
            "mining_address": ADDRESS,
            "nodes": {},
            "sync_health": {},
            "sync_progress": {"status": "synced", "remaining_blocks": 0, "nodes": {}},
            "pool_health": {
                "initial_download": False,
                "job_notify_count": 1,
                "valid_share_count": 0,
            },
            "pool_metrics": {
                "active_connections": 0,
                "stratum_no_request_disconnects": {"mac:no-request-eof": 40},
                "stratum_no_request_disconnects_total": 40,
            },
            "miner_health": {
                "connected_count": 0,
                "connected_count_effective": 0,
                "managed_count": 0,
                "miners": [],
            },
        }
        state = {"last_stratum_no_request_disconnects_total": 30}
        events: list[tuple[str, str, str, dict[str, object]]] = []

        def record(event_type: str, severity: str, message: str, details=None) -> None:
            events.append((event_type, severity, message, details or {}))

        with mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", lambda _payload: None
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", side_effect=record
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=False)

        self.assertEqual(10, result["watchdog_state"]["last_stratum_no_request_disconnects_delta"])
        self.assertEqual("stratum_no_request_disconnects", events[0][0])
        self.assertTrue(events[0][3]["mac_source"])
        self.assertEqual(0, events[0][3]["primary_miner_count"])


if __name__ == "__main__":
    unittest.main()
