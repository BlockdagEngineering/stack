from __future__ import annotations

import importlib.util
import pathlib
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("update_local_peers", ROOT / "ops" / "update-local-peers.py")
assert SPEC is not None
assert SPEC.loader is not None
update_local_peers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update_local_peers)


class UpdateLocalPeersActiveMiningGuardTest(unittest.TestCase):
    def patch_status(self, status: dict[str, object]):
        return mock.patch.object(update_local_peers, "fetch_dashboard_status", return_value=status)

    def test_defers_node_recreate_while_miners_are_active(self) -> None:
        status = {
            "can_accept_shares": True,
            "pool": {
                "metrics_active_connections": 4,
                "last_valid_share_age_seconds": 2,
                "last_submit_age_seconds": 1,
            },
        }
        with self.patch_status(status):
            reason = update_local_peers.active_mining_recreate_guard_reason()
        self.assertIn("active mining detected", reason)
        self.assertIn("4 stratum connection", reason)

    def test_zero_miner_install_does_not_defer_peer_apply(self) -> None:
        status = {
            "can_accept_shares": True,
            "pool": {
                "metrics_active_connections": 0,
                "last_valid_share_age_seconds": 999999,
                "last_submit_age_seconds": 999999,
            },
        }
        with self.patch_status(status):
            self.assertEqual(update_local_peers.active_mining_recreate_guard_reason(), "")

    def test_guard_can_be_disabled_for_explicit_maintenance(self) -> None:
        status = {
            "can_accept_shares": True,
            "pool": {
                "metrics_active_connections": 4,
                "last_valid_share_age_seconds": 2,
                "last_submit_age_seconds": 1,
            },
        }
        with mock.patch.dict("os.environ", {"BDAG_LOCAL_PEERS_DEFER_NODE_RECREATE_WHILE_MINING": "false"}):
            with self.patch_status(status):
                self.assertEqual(update_local_peers.active_mining_recreate_guard_reason(), "")


if __name__ == "__main__":
    unittest.main()
