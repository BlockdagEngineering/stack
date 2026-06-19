import os
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = ROOT / "ops"


class PoolOpsEnvBootstrapTest(unittest.TestCase):
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
