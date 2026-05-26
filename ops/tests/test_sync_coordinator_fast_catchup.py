from __future__ import annotations

import pathlib
import sys
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))

import sync_coordinator  # noqa: E402


class SyncCoordinatorFastCatchupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_nodes = sync_coordinator.NODES
        sync_coordinator.NODES = ["node"]

    def tearDown(self) -> None:
        sync_coordinator.NODES = self.old_nodes

    def status(self, *, remaining: int, importing: bool = True) -> dict:
        return {
            "containers": {"node": {"running": True}},
            "nodes": {
                "node": {
                    "latest_block": 9000,
                    "last_import_age_seconds": 10 if importing else 9999,
                }
            },
            "sync_progress": {
                "highest_block": 9000 + remaining,
                "nodes": {
                    "node": {
                        "current_block": 9000,
                        "remaining_blocks": remaining,
                    }
                },
            },
        }

    def test_single_node_far_behind_accelerates_fastsync(self) -> None:
        decision = sync_coordinator.build_decision(self.status(remaining=1500), {})
        self.assertEqual(decision["action"], "accelerate_leader_catchup")
        self.assertEqual(decision["leader"], "node")
        self.assertTrue(decision["far_behind"])
        self.assertEqual(decision["thresholds"]["far_behind_blocks"], 1000)

    def test_single_node_within_policy_monitors(self) -> None:
        decision = sync_coordinator.build_decision(self.status(remaining=999), {})
        self.assertEqual(decision["action"], "monitor")
        self.assertFalse(decision["far_behind"])

    def test_command_line_fastartifact_flag_detection(self) -> None:
        self.assertTrue(sync_coordinator.node_command_has_fast_artifact_sync("/usr/local/bin/blockdag-node --fastartifactsync"))
        self.assertTrue(sync_coordinator.node_command_has_fast_artifact_sync("/usr/local/bin/blockdag-node --fastartifactsync=true"))
        self.assertFalse(sync_coordinator.node_command_has_fast_artifact_sync("/usr/local/bin/blockdag-node --fastartifactsync=false"))
        self.assertFalse(sync_coordinator.node_command_has_fast_artifact_sync("/usr/local/bin/blockdag-node"))

    def test_missing_fastartifact_flag_requests_restart(self) -> None:
        decision = sync_coordinator.build_decision(self.status(remaining=1500), {})
        reason = sync_coordinator.fast_sync_restart_reason(
            decision,
            {},
            "/usr/local/bin/blockdag-node --configfile /etc/bdagStack/node.conf",
            True,
        )
        self.assertIn("--fastartifactsync", reason)

    def test_restart_cooldown_suppresses_restart_reason(self) -> None:
        decision = sync_coordinator.build_decision(self.status(remaining=1500), {})
        reason = sync_coordinator.fast_sync_restart_reason(
            decision,
            {"last_fast_sync_restart_epoch": int(time.time())},
            "/usr/local/bin/blockdag-node",
            True,
        )
        self.assertEqual(reason, "")

    def test_stale_import_requests_restart_even_with_fastartifact_flag(self) -> None:
        decision = sync_coordinator.build_decision(self.status(remaining=1500, importing=False), {})
        reason = sync_coordinator.fast_sync_restart_reason(
            decision,
            {},
            "/usr/local/bin/blockdag-node --fastartifactsync",
            True,
        )
        self.assertIn("stale", reason)


if __name__ == "__main__":
    unittest.main()
