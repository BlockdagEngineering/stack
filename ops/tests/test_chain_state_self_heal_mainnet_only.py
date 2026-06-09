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

    def test_destructive_self_heal_defaults_to_fail_closed(self) -> None:
        script = (ROOT / "ops" / "chain-state-self-heal.sh").read_text(encoding="utf-8")
        stack_defaults = (ROOT / "ops" / "config" / "stack-defaults.env").read_text(encoding="utf-8")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        portable_env = (ROOT / "ops" / "portable.env.example").read_text(encoding="utf-8")

        self.assertIn('enabled="${BDAG_CHAIN_STATE_SELF_HEAL_ENABLED:-0}"', script)
        self.assertIn("BDAG_CHAIN_STATE_SELF_HEAL_ENABLED=0", stack_defaults)
        self.assertIn("BDAG_CHAIN_STATE_SELF_HEAL_ENABLED=0", env_example)
        self.assertIn("BDAG_CHAIN_STATE_SELF_HEAL_ENABLED=0", portable_env)
        self.assertIn("BDAG_CHAIN_STATE_REUSE_EXISTING_SNAPSHOT=0", stack_defaults)
        self.assertIn("BDAG_CHAIN_STATE_SELF_HEAL_ALLOW_LOCAL_CANDIDATES=0", stack_defaults)

    def test_self_heal_rejects_sealed_sidecar_artifacts_as_raw_datadirs(self) -> None:
        script = (ROOT / "ops" / "chain-state-self-heal.sh").read_text(encoding="utf-8")

        self.assertNotIn("data-restore/rawdatadir-sidecar-content/current", script)
        self.assertIn("reject_sealed_artifact_source", script)
        self.assertIn("rawdatadir-sidecar-content", script)
        self.assertIn("DO_NOT_PUBLISH.txt", script)
        self.assertIn('"raw_datadir_checkpoint"', script)
        self.assertIn('[[ -d "$source/chunks" && -f "$source/manifest.json" ]]', script)

        pre_restore_start = script.split('json_state "started" "chain-state restore started"', 1)[0]
        self.assertNotIn('stop_service_best_effort "$POOL_SERVICE"', pre_restore_start)


if __name__ == "__main__":
    unittest.main()
