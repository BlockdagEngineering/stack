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

    def test_print_mode_reports_node_args_append(self) -> None:
        result = self.run_entrypoint({"NODE_ARGS_APPEND": "--miner --maxpeers=160"})

        self.assert_stdout_contains(result, "NODE_ARGS_APPEND=--miner --maxpeers=160")

    def test_print_mode_reports_empty_node_args_append(self) -> None:
        result = self.run_entrypoint({})

        self.assert_stdout_contains(result, "NODE_ARGS_APPEND=")

    def test_print_mode_preserves_operator_node_args(self) -> None:
        result = self.run_entrypoint(
            {
                "SYNC_SOURCE_NODE": "1",
                "NODE_ARGS_APPEND": "--cache=1024",
            }
        )

        self.assert_stdout_contains(result, "NODE_ARGS_APPEND=--cache=1024")
        self.assertEqual("", result.stderr)

    def test_no_miner_mode_keeps_blockdag_rpc_module_without_miner_arg(self) -> None:
        result = self.run_entrypoint(
            {
                "BDAG_ENABLE_NODE_MINING": "0",
                "BDAG_NODE_MODULES": "Blockdag",
                "BDAG_NODE_MINING_ARGS": "",
            }
        )

        self.assert_stdout_contains(result, "--modules=Blockdag")
        self.assertNotIn("--miner", result.stdout)
        self.assertNotIn("--miningaddr=", result.stdout)

    def test_node_mining_env_appends_guard_args_without_forcing_rpc_module(self) -> None:
        result = self.run_entrypoint(
            {
                "BDAG_ENABLE_NODE_MINING": "1",
                "BDAG_NODE_MINING_ARGS": (
                    "--miner --miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
                ),
            }
        )

        self.assertNotIn("--fastartifactsync", result.stdout)
        self.assert_stdout_contains(result, "--miner")
        self.assert_stdout_contains(result, "--miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc")
        self.assertNotIn("--allowminingwhennearlysynced", result.stdout)
        self.assertNotIn("--allowsubmitwhennotsynced", result.stdout)
        self.assertNotIn("--modules=Blockdag,miner", result.stdout)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
