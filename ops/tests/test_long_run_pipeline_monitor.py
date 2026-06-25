#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import long_run_pipeline_monitor as monitor  # noqa: E402


class LongRunPipelineMonitorTests(unittest.TestCase):
    def test_node_log_tail_tracks_import_age_and_open_graph_sync(self) -> None:
        payload = """
2026-06-25|01:46:38.367 [INFO ] Syncing graph state module=SYNC cur=(1) target=(2) peer=16PeerA protocol=45 services=Full|CF processID=7
2026-06-25|01:54:47.066 [INFO ] Imported new chain segment \x1b[0mnumber\x1b[0m=12,157,992 \x1b[0mhash\x1b[0m=f4666b..9f2684 \x1b[0mage\x1b[0m=2h22m33s
2026-06-25|01:54:48.066 [WARN ] Rewinding blockchain to block target=12,157,990
2026-06-25|01:54:49.066 [WARN ] Can't find tip module=DAG block id=12672018
"""

        summary = monitor.summarize_node_log_tail(payload)

        self.assertEqual(12157992, summary["latest_import"]["number"])
        self.assertEqual("2h22m33s", summary["latest_import"]["age"])
        self.assertEqual(8553, summary["latest_import"]["age_seconds"])
        self.assertTrue(summary["graph_sync_open"])
        self.assertEqual([7], summary["graph_sync_open_process_ids"])
        self.assertEqual("16PeerA", summary["graph_sync_last_open"]["peer"])
        self.assertEqual(1, summary["rewind_count_tail"])
        self.assertEqual(1, summary["missing_tip_count_tail"])

    def test_node_log_tail_closes_completed_graph_sync(self) -> None:
        payload = """
2026-06-25|01:46:38.367 [INFO ] Syncing graph state module=SYNC peer=16PeerA processID=7
2026-06-25|01:46:58.367 [INFO ] The sync of graph state has ended module=SYNC spend=20s processID=7
"""

        summary = monitor.summarize_node_log_tail(payload)

        self.assertFalse(summary["graph_sync_open"])
        self.assertEqual({"process_id": 7, "spend": "20s", "spend_seconds": 20}, summary["graph_sync_last_end"])

    def test_node_rpc_summary_exposes_mining_safety_contradiction(self) -> None:
        calls: list[str] = []

        def fake_rpc(url: str, method: str, **_kwargs):
            calls.append(f"{url}:{method}")
            if method == "getTemplateHealth":
                return (
                    {
                        "chain_current": True,
                        "main_order": 12559107,
                        "p2p_best_peer_main_order": 12560870,
                        "p2p_best_peer_lead_blocks": 1763,
                        "mineable_now": False,
                        "submit_ready": False,
                        "reason_code": "node_syncing",
                        "template_available": False,
                        "template_coinbase_valid": False,
                        "p2p_mining_fresh": False,
                        "p2p_mining_fresh_reason_code": "peer_lead_exceeds_tolerance",
                    },
                    None,
                    1.25,
                )
            return 12559108, None, 0.75

        original = monitor.json_rpc_call
        monitor.json_rpc_call = fake_rpc
        try:
            summary = monitor.summarize_node_rpc(
                "http://node:38131",
                timeout=1.0,
                user="user",
                password="pass",
            )
        finally:
            monitor.json_rpc_call = original

        self.assertEqual(["http://node:38131:getTemplateHealth", "http://node:38131:getBlockCount"], calls)
        self.assertTrue(summary["chain_current"])
        self.assertFalse(summary["mineable_now"])
        self.assertFalse(summary["submit_ready"])
        self.assertEqual("node_syncing", summary["reason_code"])
        self.assertEqual(12559107, summary["main_order"])
        self.assertEqual(12560870, summary["p2p_best_peer_main_order"])
        self.assertEqual(1763, summary["p2p_best_peer_lead_blocks"])
        self.assertEqual(12559108, summary["block_count"])


if __name__ == "__main__":
    unittest.main()
