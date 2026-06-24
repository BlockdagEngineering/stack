import os
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = ROOT / "ops"


class PoolOpsEnvBootstrapTest(unittest.TestCase):
    def test_project_env_overrides_stack_defaults_seeded_at_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            defaults = root / "stack-defaults.env"
            env_file = root / ".env"
            defaults.write_text(
                "BDAG_ENABLE_NODE_MINING=0\nBDAG_NODE_MODULES=Blockdag\n",
                encoding="utf-8",
            )
            env_file.write_text(
                "BDAG_ENABLE_NODE_MINING=1\nBDAG_NODE_MODULES=Blockdag,miner\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env.pop("BDAG_ENABLE_NODE_MINING", None)
            env.pop("BDAG_NODE_MODULES", None)
            env.update(
                {
                    "BDAG_PROJECT_ROOT": str(root),
                    "BDAG_POOL_ENV_FILE": str(env_file),
                    "BDAG_RUNTIME_DIR": str(root / "runtime"),
                    "BDAG_STACK_DEFAULTS_FILE": str(defaults),
                    "PYTHONPATH": str(OPS_DIR),
                }
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os, pool_ops; "
                        "print(os.environ['BDAG_ENABLE_NODE_MINING']); "
                        "print(os.environ['BDAG_NODE_MODULES'])"
                    ),
                ],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(["1", "Blockdag,miner"], proc.stdout.strip().splitlines())

    def test_explicit_process_env_overrides_project_env_at_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            defaults = root / "stack-defaults.env"
            env_file = root / ".env"
            defaults.write_text("BDAG_ENABLE_NODE_MINING=0\n", encoding="utf-8")
            env_file.write_text("BDAG_ENABLE_NODE_MINING=1\n", encoding="utf-8")

            env = os.environ.copy()
            env.update(
                {
                    "BDAG_ENABLE_NODE_MINING": "0",
                    "BDAG_PROJECT_ROOT": str(root),
                    "BDAG_POOL_ENV_FILE": str(env_file),
                    "BDAG_RUNTIME_DIR": str(root / "runtime"),
                    "BDAG_STACK_DEFAULTS_FILE": str(defaults),
                    "PYTHONPATH": str(OPS_DIR),
                }
            )

            proc = subprocess.run(
                [sys.executable, "-c", "import os, pool_ops; print(os.environ['BDAG_ENABLE_NODE_MINING'])"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual("0", proc.stdout.strip())

    def test_unreadable_pool_env_file_does_not_abort_import(self) -> None:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root can read mode-000 files")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_file = root / ".env"
            env_file.write_text("BDAG_POOL_HOST=192.0.2.10\n", encoding="utf-8")
            env_file.chmod(0)

            env = os.environ.copy()
            env.update(
                {
                    "BDAG_PROJECT_ROOT": str(root),
                    "BDAG_POOL_ENV_FILE": str(env_file),
                    "BDAG_RUNTIME_DIR": str(root / "runtime"),
                    "PYTHONPATH": str(OPS_DIR),
                }
            )

            proc = subprocess.run(
                [sys.executable, "-c", "import pool_ops; print('import-ok')"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("import-ok", proc.stdout)
        self.assertIn("skipping unreadable env file", proc.stderr)


if __name__ == "__main__":
    unittest.main()
