from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "mining-appliance-preflight.py"
SPEC = importlib.util.spec_from_file_location("mining_appliance_preflight", SCRIPT)
preflight = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = preflight
SPEC.loader.exec_module(preflight)


class MiningAppliancePreflightTest(unittest.TestCase):
    def test_load_env_file_strips_quotes_and_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "# comment",
                        "BDAG_NODE_MODE='single'",
                        'BDAG_NODE_CACHE_MB="1024"',
                        "EMPTY=",
                    ]
                ),
                encoding="utf-8",
            )
            env = preflight.load_env_file(env_file)
        self.assertEqual(env["BDAG_NODE_MODE"], "single")
        self.assertEqual(env["BDAG_NODE_CACHE_MB"], "1024")
        self.assertEqual(env["EMPTY"], "")

    def test_constrained_env_warnings_for_double_node_and_large_cache(self) -> None:
        profile = preflight.HostProfile(
            os_name="linux",
            arch="x86_64",
            cpu_count=2,
            memory_bytes=3 * preflight.GIB,
            profile="constrained",
            kernel="test",
        )
        checks = []
        preflight.check_env_defaults(
            checks,
            {
                "BDAG_NODE_MODE": "double",
                "BDAG_NODE_CACHE_MB": "4096",
                "NODE_MAX_PEERS": "512",
                "BDAG_FASTSYNC_PREPROCESS_WORKERS": "4",
                "BDAG_STATUS_SAMPLER_ENABLED": "0",
                "BDAG_ADAPTIVE_CONCURRENCY_ENABLED": "0",
                "BDAG_ENTRYPOINT_CHOWN_MODE": "always",
            },
            profile,
        )
        warnings = {check.name for check in checks if check.status == "warn"}
        self.assertIn("constrained_node_mode", warnings)
        self.assertIn("node_cache_budget", warnings)
        self.assertIn("peer_budget", warnings)
        self.assertIn("fastsync_preprocess_workers", warnings)
        self.assertIn("status_sampler", warnings)
        self.assertIn("adaptive_concurrency", warnings)
        self.assertIn("entrypoint_chown_mode", warnings)

    def test_single_node_duplicate_data_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data" / "node1" / "mainnet" / "BdagChain").mkdir(parents=True)
            (root / "data" / "node2" / "mainnet" / "BdagChain").mkdir(parents=True)
            checks = []
            preflight.check_node_data_layout(checks, root, {"BDAG_NODE_MODE": "single"})
        found = {check.name: check.status for check in checks}
        self.assertEqual(found["single_node_duplicate_data"], "warn")

    def test_compose_bind_mount_overrides_default_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.override.yml").write_text(
                "\n".join(
                    [
                        "services:",
                        "  node:",
                        "    volumes:",
                        "      - /srv/bdag-chain-usb:/data:ro",
                        "      - /srv/bdag-chain-usb/node-data:/var/lib/bdagStack/node",
                    ]
                ),
                encoding="utf-8",
            )
            data_dir = preflight.env_data_dir(root, {})
        self.assertEqual(data_dir, Path("/srv/bdag-chain-usb/node-data"))

    def test_live_node_child_passes_when_compose_node_is_absent(self) -> None:
        old_run = preflight.run

        def fake_run(command: list[str], timeout: float = 5.0, cwd: Path | None = None):
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return Result()

        try:
            preflight.run = fake_run
            checks = []
            preflight.check_live_node_child(checks, Path("/tmp"))
        finally:
            preflight.run = old_run

        self.assertEqual(checks[0].status, "pass")

    def test_live_node_child_fails_when_wrapper_has_no_child(self) -> None:
        old_run = preflight.run

        def fake_run(command: list[str], timeout: float = 5.0, cwd: Path | None = None):
            class Result:
                returncode = 0
                stdout = "container-id\n"
                stderr = ""

            result = Result()
            if "exec" in command:
                result.returncode = 1
                result.stdout = ""
            return result

        try:
            preflight.run = fake_run
            checks = []
            preflight.check_live_node_child(checks, Path("/tmp"))
        finally:
            preflight.run = old_run

        self.assertEqual(checks[0].status, "fail")


if __name__ == "__main__":
    unittest.main()
