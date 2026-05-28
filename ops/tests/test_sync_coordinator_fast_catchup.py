from __future__ import annotations

import pathlib
import sys
import time
import unittest
import unittest.mock


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
        self.assertEqual(decision["reason"], "single-node sync is within policy")
        self.assertFalse(decision["far_behind"])

    def test_running_node_with_unknown_height_accelerates_recovery(self) -> None:
        status = {
            "containers": {"node": {"running": True}},
            "nodes": {"node": {"latest_block": 0, "last_import_age_seconds": 9999}},
            "sync_progress": {"highest_block": 0, "nodes": {"node": {"current_block": 0}}},
        }
        decision = sync_coordinator.build_decision(status, {})
        self.assertEqual(decision["action"], "accelerate_leader_catchup")
        self.assertEqual(decision["leader"], "node")
        self.assertTrue(decision["leader_height_unknown"])
        self.assertTrue(decision["far_behind"])

    def test_single_node_ignores_retired_paused_follower_state(self) -> None:
        previous_state = {
            "mode": "leader_catchup",
            "paused_follower": "node2",
            "paused_follower_remaining_blocks": 200_000,
            "last_decision": {
                "network_highest": 10_500,
                "nodes": {
                    "node": {"height": 9000, "remaining_blocks": 1500},
                    "node2": {"height": 0, "remaining_blocks": 200_000},
                },
            },
        }

        decision = sync_coordinator.build_decision(self.status(remaining=1500), previous_state)

        self.assertEqual(decision["action"], "accelerate_leader_catchup")
        self.assertEqual(decision["leader"], "node")
        self.assertEqual(decision["target"], "")
        self.assertNotIn("node2", decision["nodes"])

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

    def test_near_tip_leader_does_not_restart_for_missing_fastartifact_flag(self) -> None:
        decision = sync_coordinator.build_decision(self.status(remaining=10), {})
        reason = sync_coordinator.fast_sync_restart_reason(
            decision,
            {},
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

    def test_rawdatadir_peer_candidates_are_fastest_first_and_deduped(self) -> None:
        env_values = {
            "BDAG_RAWDATADIR_PEERS": "/ip4/10.0.0.2/tcp/8151/p2p/raw",
            "BDAG_FASTSYNC_LAN_PEERS": "/ip4/192.168.1.2/tcp/8151/p2p/lan,/ip4/10.0.0.2/tcp/8151/p2p/raw",
            "BDAG_FASTSYNC_VPN_PEERS": "/ip4/10.207.244.12/tcp/8151/p2p/vpn",
            "BDAG_FASTSYNC_PUBLIC_PEERS": "/ip4/203.0.113.1/tcp/8151/p2p/public",
            "NODE_ARGS_APPEND": "--addpeer=/ip4/198.51.100.1/tcp/8151/p2p/addpeer",
        }
        with unittest.mock.patch.dict(sync_coordinator.os.environ, {}, clear=True):
            self.assertEqual(
                sync_coordinator.fastest_artifact_peer_candidates(env_values),
                [
                    "/ip4/10.0.0.2/tcp/8151/p2p/raw",
                    "/ip4/192.168.1.2/tcp/8151/p2p/lan",
                    "/ip4/10.207.244.12/tcp/8151/p2p/vpn",
                    "/ip4/203.0.113.1/tcp/8151/p2p/public",
                    "/ip4/198.51.100.1/tcp/8151/p2p/addpeer",
                ],
            )

    def test_signed_manifest_provides_trust_on_first_signed_spec(self) -> None:
        manifest = {
            "artifact_type": "raw_datadir_checkpoint",
            "block_total": 9_100_000,
            "signatures": [
                {
                    "key_id": "pool-abc",
                    "public_key": "0123456789abcdef",
                    "signature": "feedface",
                }
            ],
        }
        self.assertEqual(
            sync_coordinator.collect_signature_specs(manifest),
            ["pool-abc:0123456789abcdef"],
        )
        self.assertEqual(sync_coordinator.rawdatadir_manifest_progress(manifest)["best_height"], 9_100_000)

    def test_unsigned_manifest_does_not_create_trust_spec(self) -> None:
        manifest = {
            "artifact_type": "raw_datadir_checkpoint",
            "block_total": 9_100_000,
            "key_id": "pool-abc",
            "public_key": "0123456789abcdef",
        }
        self.assertEqual(sync_coordinator.collect_signature_specs(manifest), [])

    def test_rawdatadir_retry_cooldown_suppresses_immediate_retry(self) -> None:
        remaining = sync_coordinator.fast_artifact_retry_cooldown_remaining(
            {"last_fast_artifact_attempt_epoch": int(time.time())}
        )
        self.assertGreater(remaining, 0)

    def test_legacy_restart_lagging_follower_flag_remains_compatible(self) -> None:
        args = sync_coordinator.parse_args([
            "--once",
            "--repair",
            "--pause-follower",
            "--resume-follower",
            "--restart-lagging-follower",
        ])
        self.assertTrue(args.accelerate_fastsync)


class SyncCoordinatorLeaderSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_nodes = sync_coordinator.NODES
        sync_coordinator.NODES = ["node1", "node2"]

    def tearDown(self) -> None:
        sync_coordinator.NODES = self.old_nodes

    def two_node_status(
        self,
        *,
        node1_height: int,
        node1_remaining: int,
        node1_importing: bool,
        node1_running: bool = True,
        node2_height: int,
        node2_remaining: int,
        node2_importing: bool,
        node2_running: bool = True,
    ) -> dict:
        highest = max(node1_height + node1_remaining, node2_height + node2_remaining)
        return {
            "containers": {
                "node1": {"running": node1_running},
                "node2": {"running": node2_running},
            },
            "nodes": {
                "node1": {
                    "latest_block": node1_height,
                    "last_import_age_seconds": 10 if node1_importing else 9999,
                    "importing": node1_importing,
                },
                "node2": {
                    "latest_block": node2_height,
                    "last_import_age_seconds": 10 if node2_importing else 9999,
                    "importing": node2_importing,
                },
            },
            "sync_progress": {
                "highest_block": highest,
                "nodes": {
                    "node1": {
                        "current_block": node1_height,
                        "remaining_blocks": node1_remaining,
                    },
                    "node2": {
                        "current_block": node2_height,
                        "remaining_blocks": node2_remaining,
                    },
                },
            },
        }

    def test_highest_node_wins_over_lower_importing_node(self) -> None:
        status = self.two_node_status(
            node1_height=8_645_942,
            node1_remaining=202_540,
            node1_importing=True,
            node2_height=8_848_676,
            node2_remaining=0,
            node2_importing=False,
        )
        decision = sync_coordinator.build_decision(status, {})
        self.assertEqual(decision["leader"], "node2")
        self.assertEqual(decision["target"], "node1")
        self.assertEqual(decision["action"], "pause_follower")
        safe, reason = sync_coordinator.pause_follower_safety(decision)
        self.assertTrue(safe, reason)

    def test_paused_ahead_node_is_resumed_not_kept_paused(self) -> None:
        status = self.two_node_status(
            node1_height=8_668_005,
            node1_remaining=182_930,
            node1_importing=True,
            node2_height=0,
            node2_remaining=0,
            node2_importing=False,
            node2_running=False,
        )
        previous_state = {
            "mode": "leader_catchup",
            "paused_follower": "node2",
            "last_decision": {
                "network_highest": 8_850_935,
                "nodes": {
                    "node1": {"height": 8_645_942, "remaining_blocks": 202_540},
                    "node2": {"height": 8_848_676, "remaining_blocks": 0},
                },
            },
        }
        decision = sync_coordinator.build_decision(status, previous_state)
        self.assertEqual(decision["action"], "seed_or_resume_follower")
        self.assertEqual(decision["target"], "node2")
        self.assertIn("resuming", decision["reason"])

    def test_pause_safety_refuses_target_ahead_of_leader(self) -> None:
        decision = {
            "leader": "node1",
            "target": "node2",
            "nodes": {
                "node1": {"height": 8_668_005, "remaining_blocks": 182_930},
                "node2": {"height": 8_848_676, "remaining_blocks": 0},
            },
        }
        safe, reason = sync_coordinator.pause_follower_safety(decision)
        self.assertFalse(safe)
        self.assertIn("refusing to pause node2", reason)


if __name__ == "__main__":
    unittest.main()
