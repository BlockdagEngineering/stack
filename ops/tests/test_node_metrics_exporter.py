from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXPORTER = ROOT / "docker" / "node_metrics_exporter.py"


def load_exporter():
    spec = importlib.util.spec_from_file_location("node_metrics_exporter", EXPORTER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {EXPORTER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NodeMetricsExporterTests(unittest.TestCase):
    def test_prometheus_rendering_exposes_p2p_and_chain_metrics(self) -> None:
        exporter = load_exporter()

        metrics = exporter.build_metrics(
            health={
                "main_order": 1234,
                "p2p_consensus_peer_count": 5,
                "p2p_fresh_consensus_peer_count": 4,
                "p2p_stale_consensus_peer_count": 1,
                "p2p_best_peer_main_order": 1239,
                "p2p_best_peer_lead_blocks": 5,
                "p2p_best_peer_graph_state_age_ms": 1200,
                "p2p_avg_graph_state_age_ms": 900,
                "p2p_max_graph_state_age_ms": 1800,
                "p2p_sync_peer_present": True,
                "p2p_sync_peer_fresh": False,
                "p2p_sync_peer_graph_state_age_ms": 2500,
                "p2p_mining_fresh": True,
            },
            active_peer_count=7,
            block_count=1235,
            scrape_error="",
        )
        text = exporter.render_prometheus(metrics)

        self.assertIn("p2p_peers_ 7", text)
        self.assertIn("p2p_miningFreshness_consensusPeers 5", text)
        self.assertIn("p2p_miningFreshness_freshConsensusPeers 4", text)
        self.assertIn("p2p_miningFreshness_staleConsensusPeers 1", text)
        self.assertIn("p2p_miningFreshness_bestPeerMainOrder 1239", text)
        self.assertIn("p2p_miningFreshness_bestPeerLeadBlocks 5", text)
        self.assertIn("p2p_miningFreshness_syncPeerPresent 1", text)
        self.assertIn("p2p_miningFreshness_syncPeerFresh 0", text)
        self.assertIn("p2p_miningFreshness_miningFresh 1", text)
        self.assertIn("Blockdag_mainorder 1234", text)
        self.assertIn("chain_head_block 1235", text)
        self.assertIn("node_metrics_exporter_scrape_success 1", text)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
