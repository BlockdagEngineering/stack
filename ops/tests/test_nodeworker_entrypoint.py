from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "docker" / "entrypoint-nodeworker.sh"


class NodeworkerEntrypointTest(unittest.TestCase):
    def run_entrypoint(
        self,
        extra_env: dict[str, str],
        *,
        supported_node_flags: tuple[str, ...] = ("--nofastsyncserve",),
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "BDAG_ENTRYPOINT_PRINT_NODE_FLAGS": "1",
        }
        env.update(extra_env)
        with tempfile.TemporaryDirectory() as tmp:
            fake_node = Path(tmp) / "fake-node"
            fake_node.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        'if [ "${1:-}" = "--help" ]; then',
                        *[
                            f"  printf '%s\\n' {shlex.quote(flag)}"
                            for flag in supported_node_flags
                        ],
                        "  exit 0",
                        "fi",
                        "exit 0",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            fake_node.chmod(0o755)
            return subprocess.run(
                [
                    "bash",
                    str(ENTRYPOINT),
                    "nodeworker",
                    f"--node-binary={fake_node}",
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

    def test_print_mode_enables_native_metrics_collection_by_default(self) -> None:
        result = self.run_entrypoint({})

        self.assert_stdout_contains(result, "--metrics")

    def test_print_mode_preserves_explicit_metrics_flag(self) -> None:
        result = self.run_entrypoint({"NODE_ARGS_APPEND": "--metrics=false"})

        self.assert_stdout_contains(result, "NODE_ARGS_APPEND=--metrics=false")

    def test_print_mode_does_not_emit_removed_sync_flags(self) -> None:
        result = self.run_entrypoint(
            {
                "NODE_ARGS_APPEND": "--cache=1024",
            }
        )

        self.assert_stdout_contains(result, "NODE_ARGS_APPEND=--cache=1024")
        combined = result.stdout + result.stderr
        self.assertNotIn("FAST", combined.upper())
        self.assertEqual("", result.stderr)

    def test_bootstrap_peers_seed_addpeer_and_native_bootstrapnode(self) -> None:
        peer_a = "/ip4/10.0.0.2/tcp/8150/p2p/16Uiu2HAm11111111111111111111111111111111111111111"
        peer_b = "/ip4/10.0.0.3/tcp/8150/p2p/16Uiu2HAm22222222222222222222222222222222222222222"
        result = self.run_entrypoint({"BOOTSTRAP_PEER_ADDRESSES": f"{peer_a},{peer_b}"})

        self.assert_stdout_contains(result, f"--addpeer={peer_a}")
        self.assert_stdout_contains(result, f"--addpeer={peer_b}")
        self.assert_stdout_contains(result, f"--bootstrapnode={peer_a}")

    def test_existing_bootstrapnode_policy_is_preserved(self) -> None:
        peer = "/ip4/10.0.0.2/tcp/8150/p2p/16Uiu2HAm11111111111111111111111111111111111111111"
        explicit = "/ip4/10.0.0.9/tcp/8150/p2p/16Uiu2HAm99999999999999999999999999999999999999999"
        result = self.run_entrypoint(
            {
                "BOOTSTRAP_PEER_ADDRESSES": peer,
                "NODE_ARGS_APPEND": f"--bootstrapnode={explicit}",
            }
        )

        self.assert_stdout_contains(result, f"--addpeer={peer}")
        self.assert_stdout_contains(result, f"--bootstrapnode={explicit}")
        self.assertNotIn(f"--bootstrapnode={peer}", result.stdout)

    def test_node_mining_env_appends_guard_args_without_forcing_rpc_module(self) -> None:
        result = self.run_entrypoint(
            {
                "BDAG_ENABLE_NODE_MINING": "1",
                "BDAG_NODE_MINING_ARGS": (
                    "--miner --miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
                ),
            }
        )

        self.assert_stdout_contains(result, "--miner")
        self.assert_stdout_contains(result, "--miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc")
        self.assertNotIn("--allowminingwhennearlysynced", result.stdout)
        self.assertNotIn("--allowsubmitwhennotsynced", result.stdout)

    def test_node_mining_env_derives_args_from_pool_address_when_blank(self) -> None:
        address = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        result = self.run_entrypoint(
            {
                "BDAG_ENABLE_NODE_MINING": "1",
                "BDAG_NODE_MINING_ARGS": "",
                "MINING_POOL_ADDRESS": address,
            }
        )

        self.assert_stdout_contains(result, "--miner")
        self.assert_stdout_contains(result, f"--miningaddr={address}")
        self.assertNotIn("--allowminingwhennearlysynced", result.stdout)
        self.assertNotIn("--allowsubmitwhennotsynced", result.stdout)

    def test_nodeworker_enables_mining_readiness_recovery_by_default(self) -> None:
        result = self.run_entrypoint({})

        self.assert_stdout_contains(result, "--health.mining-readiness-timeout=30m")
        self.assert_stdout_contains(result, "--health.mining-readiness-grace=20m")

    def test_node_mining_env_enables_no_pending_templates_by_default(self) -> None:
        result = self.run_entrypoint(
            {
                "BDAG_ENABLE_NODE_MINING": "1",
                "BDAG_NODE_MINING_ARGS": "",
                "MINING_POOL_ADDRESS": "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc",
            }
        )

        self.assert_stdout_contains(result, "--miningnopendingtx")

    def test_node_mining_env_can_disable_no_pending_templates(self) -> None:
        result = self.run_entrypoint(
            {
                "BDAG_ENABLE_NODE_MINING": "1",
                "BDAG_NODE_MINING_ARGS": "",
                "MINING_POOL_ADDRESS": "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc",
                "BDAG_NODE_MINING_NO_PENDING_TX": "0",
            }
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--miningnopendingtx", result.stdout)

    def test_node_mining_env_rejects_zero_derived_address(self) -> None:
        result = self.run_entrypoint(
            {
                "BDAG_ENABLE_NODE_MINING": "1",
                "BDAG_NODE_MINING_ARGS": "",
                "MINING_POOL_ADDRESS": "0x0000000000000000000000000000000000000000",
                "MINING_ADDRESS": "",
                "POOL_COINBASE_ADDRESS": "",
            }
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no valid non-zero mining address", result.stderr)

    def test_node_mining_env_allows_blockdag_and_miner_rpc_modules(self) -> None:
        result = self.run_entrypoint(
            {
                "BDAG_ENABLE_NODE_MINING": "1",
                "BDAG_NODE_MODULES": "Blockdag,miner",
                "BDAG_NODE_MINING_ARGS": (
                    "--miner --miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
                ),
            }
        )

        self.assert_stdout_contains(result, "--modules=Blockdag")
        self.assert_stdout_contains(result, "--modules=miner")

    def test_entrypoint_prepares_runtime_config_before_privilege_drop(self) -> None:
        entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
        prepare_index = entrypoint.index("prepare_runtime_configfile \"$@\"")
        runuser_index = entrypoint.index("exec runuser -u bdagStack -g bdagStack -- \"$@\"")

        self.assertIn("rewrite_node_args_configfile", entrypoint)
        self.assertIn("runuser -u bdagStack -g bdagStack -- test -r \"$config_file\"", entrypoint)
        self.assertIn("chown bdagStack:bdagStack \"$runtime_config\"", entrypoint)
        self.assertIn("chmod 0600 \"$runtime_config\"", entrypoint)
        self.assertLess(prepare_index, runuser_index)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
