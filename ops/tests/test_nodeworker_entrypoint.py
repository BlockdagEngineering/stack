from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "docker" / "entrypoint-nodeworker.sh"


class NodeworkerEntrypointTest(unittest.TestCase):
    def run_entrypoint(self, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "BDAG_ENTRYPOINT_PRINT_NODE_FLAGS": "1",
            "BDAG_FASTSYNC_PEER_ORDERING": "off",
            "BDAG_FASTARTIFACTSYNC_ENABLED": "1",
        }
        env.update(extra_env)
        with tempfile.TemporaryDirectory() as tmp:
            return subprocess.run(
                [
                    "bash",
                    str(ENTRYPOINT),
                    "/bin/true",
                    f"--node-args=--datadir={tmp}",
                ],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

    def assert_stdout_contains(self, result: subprocess.CompletedProcess[str], needle: str) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(needle, result.stdout)

    def test_sync_source_node_zero_keeps_fastartifact_startup_on_single_device(self) -> None:
        result = self.run_entrypoint(
            {
                "SYNC_SOURCE_NODE": "0",
                "BDAG_NO_FASTSYNC_SERVE": "auto",
                "BDAG_STORAGE_PROFILE": "single-device",
            }
        )

        self.assert_stdout_contains(result, "BDAG_FASTARTIFACTSYNC_ENABLED=1")
        self.assert_stdout_contains(result, "NODE_ARGS_APPEND=--fastartifactsync")
        self.assertIn("disables raw datadir source publishing only", result.stderr)

    def test_usb_storage_profile_disables_fastartifact_startup_by_default(self) -> None:
        result = self.run_entrypoint(
            {
                "SYNC_SOURCE_NODE": "1",
                "BDAG_NO_FASTSYNC_SERVE": "auto",
                "BDAG_STORAGE_PROFILE": "single-usb-constrained",
                "NODE_ARGS_APPEND": "--fastartifactsync",
            }
        )

        self.assert_stdout_contains(result, "BDAG_FASTARTIFACTSYNC_ENABLED=0")
        self.assert_stdout_contains(result, "NODE_ARGS_APPEND=")
        self.assertIn("BDAG_STORAGE_PROFILE=single-usb-constrained", result.stderr)

    def test_explicit_no_serve_zero_overrides_auto_usb_storage_guard(self) -> None:
        result = self.run_entrypoint(
            {
                "SYNC_SOURCE_NODE": "0",
                "BDAG_NO_FASTSYNC_SERVE": "0",
                "BDAG_STORAGE_PROFILE": "single-usb-constrained",
            }
        )

        self.assert_stdout_contains(result, "BDAG_FASTARTIFACTSYNC_ENABLED=1")
        self.assert_stdout_contains(result, "NODE_ARGS_APPEND=--fastartifactsync")

    def test_node_mining_env_appends_guard_args_without_forcing_rpc_module(self) -> None:
        result = self.run_entrypoint(
            {
                "BDAG_ENABLE_NODE_MINING": "1",
                "BDAG_NODE_MINING_ARGS": (
                    "--miner --miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
                ),
            }
        )

        self.assert_stdout_contains(result, "BDAG_FASTARTIFACTSYNC_ENABLED=1")
        self.assert_stdout_contains(result, "--fastartifactsync")
        self.assert_stdout_contains(result, "--miner")
        self.assert_stdout_contains(result, "--miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc")
        self.assertNotIn("--allowminingwhennearlysynced", result.stdout)
        self.assertNotIn("--allowsubmitwhennotsynced", result.stdout)
        self.assertNotIn("--modules=Blockdag,miner", result.stdout)

    def test_mainnet_bdag_network_env_is_normalized_to_implicit_default(self) -> None:
        result = self.run_entrypoint({"BDAG_NETWORK": "mainnet"})

        self.assert_stdout_contains(result, "BDAG_NETWORK=\n")
        self.assertIn("BDAG_NETWORK=mainnet is the implicit default", result.stderr)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
