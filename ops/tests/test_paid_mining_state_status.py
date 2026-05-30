#!/usr/bin/env python3

import pathlib
import sys
import tempfile
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class PaidMiningStateStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_global_cache_file = pool_ops.GLOBAL_CACHE_FILE
        self.old_global_history_file = pool_ops.GLOBAL_HISTORY_FILE
        self.old_seconds_since_epoch = pool_ops.seconds_since_epoch
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        pool_ops.GLOBAL_CACHE_FILE = self.old_global_cache_file
        pool_ops.GLOBAL_HISTORY_FILE = self.old_global_history_file
        pool_ops.seconds_since_epoch = self.old_seconds_since_epoch

    def test_fresh_shares_without_accepted_submit_is_unpaid(self) -> None:
        state = pool_ops.derive_status_paid_mining_state(
            overall="ok",
            connected_miners=1,
            sync_progress={"status": "synced"},
            pool_health={"block_submit_success_count": 0, "block_submit_failure_count": 3},
            pool_has_recent_share_activity=True,
            pool_has_recent_paid_work=False,
            source_job_hard_degraded=False,
            source_selected_backend_hard_degraded=False,
        )

        self.assertEqual("mining_unpaid", state["state"])
        self.assertTrue(state["has_recent_share_activity"])
        self.assertFalse(state["has_recent_accepted_submit"])

    def test_accepted_submit_without_chain_confirmation_is_degraded(self) -> None:
        state = pool_ops.derive_status_paid_mining_state(
            overall="ok",
            connected_miners=1,
            sync_progress={"status": "synced"},
            pool_health={"block_submit_success_count": 1},
            pool_has_recent_share_activity=True,
            pool_has_recent_paid_work=True,
            source_job_hard_degraded=False,
            source_selected_backend_hard_degraded=False,
        )

        self.assertEqual("mining_paid_degraded", state["state"])
        self.assertTrue(state["has_recent_accepted_submit"])
        self.assertFalse(state["has_recent_confirmed_onchain_paid_block"])

    def test_confirmed_paid_chain_block_is_ok(self) -> None:
        state = pool_ops.derive_status_paid_mining_state(
            overall="ok",
            connected_miners=1,
            sync_progress={"status": "synced"},
            pool_health={"block_submit_success_count": 1, "has_recent_confirmed_onchain_paid_block": True},
            pool_has_recent_share_activity=True,
            pool_has_recent_paid_work=True,
            source_job_hard_degraded=False,
            source_selected_backend_hard_degraded=False,
        )

        self.assertEqual("mining_paid_ok", state["state"])

    def test_onchain_paid_evidence_uses_chain_sourced_global_rows(self) -> None:
        address = "0x" + ("a" * 40)
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pool_ops.GLOBAL_CACHE_FILE = root / "global-cache.json"
            pool_ops.GLOBAL_HISTORY_FILE = root / "global-history.jsonl"
            pool_ops.GLOBAL_CACHE_FILE.write_text(
                '{"chain_clusters":[{"address":"%s","blocks":2,"last_seen_epoch":950}]}' % address,
                encoding="utf-8",
            )
            pool_ops.GLOBAL_HISTORY_FILE.write_text("", encoding="utf-8")
            pool_ops.seconds_since_epoch = lambda: 1000

            evidence = pool_ops.recent_confirmed_onchain_paid_block_evidence([address], max_age_seconds=100)

        self.assertTrue(evidence["recent"])
        self.assertEqual(50, evidence["age_seconds"])

    def test_onchain_paid_evidence_ignores_local_pool_rows(self) -> None:
        address = "0x" + ("b" * 40)
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pool_ops.GLOBAL_CACHE_FILE = root / "global-cache.json"
            pool_ops.GLOBAL_HISTORY_FILE = root / "global-history.jsonl"
            pool_ops.GLOBAL_CACHE_FILE.write_text(
                '{"clusters":[{"address":"%s","blocks":99,"last_seen_epoch":999,"local_pool":true}]}'
                % address,
                encoding="utf-8",
            )
            pool_ops.GLOBAL_HISTORY_FILE.write_text("", encoding="utf-8")
            pool_ops.seconds_since_epoch = lambda: 1000

            evidence = pool_ops.recent_confirmed_onchain_paid_block_evidence([address], max_age_seconds=100)

        self.assertFalse(evidence["recent"])
        self.assertIn("no matching", evidence["reason"])

    def test_no_miners_uses_sync_only_state(self) -> None:
        state = pool_ops.derive_status_paid_mining_state(
            overall="ok",
            connected_miners=0,
            sync_progress={"status": "syncing"},
            pool_health={},
            pool_has_recent_share_activity=False,
            pool_has_recent_paid_work=False,
            source_job_hard_degraded=False,
            source_selected_backend_hard_degraded=False,
        )

        self.assertEqual("sync_only_no_miners", state["state"])


if __name__ == "__main__":
    unittest.main()
