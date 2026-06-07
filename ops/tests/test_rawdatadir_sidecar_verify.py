import importlib.util
import tempfile
from pathlib import Path
import unittest
import unittest.mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "verify-rawdatadir-sidecar.py"
spec = importlib.util.spec_from_file_location("verify_rawdatadir_sidecar", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RawdatadirSidecarVerifyTest(unittest.TestCase):
    def make_sidecar(self, root: Path) -> Path:
        sidecar = root / "sidecar" / "mainnet"
        chain = sidecar / "BdagChain"
        chain.mkdir(parents=True)
        (chain / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")
        (chain / "MANIFEST-000001").write_text("", encoding="utf-8")
        return sidecar

    def test_safe_sidecar_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self.make_sidecar(Path(tmp))
            payload = module.verify(sidecar, None, None)
        self.assertTrue(payload["safe"])
        self.assertEqual([], payload["reasons"])
        self.assertEqual(0, payload["unsafe_path_count"])

    def test_lock_and_node_state_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self.make_sidecar(Path(tmp))
            nodes = sidecar / "bdageth" / "nodes"
            nodes.mkdir(parents=True)
            (sidecar / "bdageth" / "LOCK").write_text("", encoding="utf-8")
            payload = module.verify(sidecar, None, None)
        self.assertFalse(payload["safe"])
        self.assertIn("unsafe_ephemeral_or_private_paths_present", payload["reasons"])
        self.assertIn("bdageth/LOCK", payload["unsafe_paths"])
        self.assertIn("bdageth/nodes", payload["unsafe_paths"])

    def test_missing_chain_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = Path(tmp) / "sidecar" / "mainnet"
            sidecar.mkdir(parents=True)
            payload = module.verify(sidecar, None, None)
        self.assertFalse(payload["safe"])
        self.assertIn("missing_BdagChain", payload["reasons"])

    def test_held_sidecar_lock_marks_copy_in_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sidecar = self.make_sidecar(root)
            lock_file = root / "runtime" / "rawdatadir-sidecar.lock"
            lock_file.parent.mkdir(parents=True)
            lock_file.write_text("", encoding="utf-8")
            with unittest.mock.patch.object(module, "fuser_holds", return_value=True):
                payload = module.verify(sidecar, None, None, lock_file)
        self.assertFalse(payload["safe"])
        self.assertTrue(payload["copy_in_progress"])
        self.assertIn("sidecar_sync_in_progress", payload["reasons"])

    def test_hidden_rsync_temp_file_is_unsafe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = self.make_sidecar(Path(tmp))
            (sidecar / "BdagChain" / ".003702.sst.tmp").write_text("", encoding="utf-8")
            payload = module.verify(sidecar, None, None)
        self.assertFalse(payload["safe"])
        self.assertIn("unsafe_ephemeral_or_private_paths_present", payload["reasons"])
        self.assertIn("BdagChain/.003702.sst.tmp", payload["unsafe_paths"])


if __name__ == "__main__":
    unittest.main()
