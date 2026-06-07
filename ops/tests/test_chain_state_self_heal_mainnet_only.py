import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ChainStateSelfHealMainnetOnlyTest(unittest.TestCase):
    def test_self_heal_refuses_non_mainnet_network_and_pins_restore_path(self) -> None:
        script = (ROOT / "ops" / "chain-state-self-heal.sh").read_text(encoding="utf-8")

        self.assertIn("chain-state self-heal refuses non-mainnet network", script)
        self.assertIn('NETWORK="mainnet"', script)
        self.assertIn('NODE_NETWORK_DIR="$NODE_DATA_DIR/$NETWORK"', script)
        self.assertNotIn('${NETWORK:-mainnet}', script)

    def test_self_heal_prefers_verified_sidecar_and_preserves_identity(self) -> None:
        script = (ROOT / "ops" / "chain-state-self-heal.sh").read_text(encoding="utf-8")

        self.assertIn("safe_status_restore_source", script)
        self.assertIn("open_restore_point_source", script)
        self.assertIn("raw_sidecar_restore_source", script)
        self.assertIn("verify-rawdatadir-sidecar.py", script)
        self.assertIn('RESTORE_MODE_USED="verified_sidecar"', script)
        self.assertIn('RESTORE_MODE_USED="open_sidecar_restore_point"', script)
        self.assertIn('RESTORE_MODE_USED="raw_sidecar"', script)
        self.assertIn("preserve_node_identity", script)
        self.assertIn("restore_node_identity", script)
        self.assertIn("bdageth/nodekey", script)
        self.assertIn("network.key", script)


if __name__ == "__main__":
    unittest.main()
