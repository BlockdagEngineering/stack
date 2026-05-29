#!/usr/bin/env python3

import pathlib
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
