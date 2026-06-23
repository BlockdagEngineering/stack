#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import wait_for_node_sync  # noqa: E402


class WaitForNodeSyncTests(unittest.TestCase):
    def test_log_snapshot_parses_sync_gap_from_syncing_graph_state(self) -> None:
        log = "\n".join(
            [
                "2026-06-23|15:07:07.606 [INFO ] Syncing graph state module=SYNC cur=(12397234,9487393,9500135,12493900,1) target=(12399668,9489347,9502090,12496787,1) peer=16Uiu2HAmEFxRaBbbf3sRi43CCvMk5Y6zPkuGY9s4uRK2FKJVJkqo protocol=45 services=Full|CF processID=1",
                "2026-06-23|15:07:14.640 [INFO ] Processed 13 blocks in the last 10.25s  module=CHAIN     transactions=17   order=12396663    time=2026-06-23T15:07:13+0000",
            ]
        )

        original = wait_for_node_sync.pool_ops.docker_logs
        wait_for_node_sync.pool_ops.docker_logs = lambda _name, lines=240: log
        try:
            snapshot = wait_for_node_sync.log_snapshot()
        finally:
            wait_for_node_sync.pool_ops.docker_logs = original

        self.assertEqual(snapshot["status"], "syncing")
        self.assertEqual(snapshot["current_block"], 12397234)
        self.assertEqual(snapshot["highest_block"], 12399668)
        self.assertEqual(snapshot["remaining_blocks"], 2434)
        self.assertGreater(snapshot["processed_rate_blocks_per_second"], 0)

    def test_log_snapshot_parses_sync_gap_from_startup_state_log(self) -> None:
        log = "2026-06-23|15:29:11.731 [INFO ] Start to find cur block state       module=BDAG      state.order=12397234 evm.Number=12040112 cur.number=0"

        original = wait_for_node_sync.pool_ops.docker_logs
        wait_for_node_sync.pool_ops.docker_logs = lambda _name, lines=240: log
        try:
            snapshot = wait_for_node_sync.log_snapshot()
        finally:
            wait_for_node_sync.pool_ops.docker_logs = original

        self.assertEqual(snapshot["status"], "syncing")
        self.assertEqual(snapshot["current_block"], 0)
        self.assertEqual(snapshot["highest_block"], 12397234)
        self.assertEqual(snapshot["remaining_blocks"], 12397234)

    def test_describe_progress_formats_gap_message(self) -> None:
        progress = {
            "status": "syncing",
            "current_block": 12397234,
            "highest_block": 12399668,
            "remaining_blocks": 2434,
            "processed_rate_blocks_per_second": 1.267,
        }

        message, state = wait_for_node_sync.describe_progress(progress, {}, 0.0)

        self.assertIn("gap 2,434 blocks", message)
        self.assertIn("ETA", message)
        self.assertEqual(state["remaining_blocks"], 2434)

    def test_describe_progress_marks_unchanged_log_snapshots(self) -> None:
        progress = {
            "status": "syncing",
            "current_block": 12397234,
            "highest_block": 12402058,
            "remaining_blocks": 4824,
            "processed_rate_blocks_per_second": None,
            "last_log_update_seconds": 30,
        }
        previous = {
            "status": "syncing",
            "current_block": 12397234,
            "highest_block": 12402058,
            "remaining_blocks": 4824,
            "epoch": 100.0,
            "poll_interval": 10.0,
        }

        message, _state = wait_for_node_sync.describe_progress(progress, previous, 110.0)

        self.assertIn("unchanged for 30s", message)


if __name__ == "__main__":
    unittest.main()
