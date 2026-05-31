import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "ipfs_content_sidecar.py"
SPEC = importlib.util.spec_from_file_location("ipfs_content_sidecar", MODULE_PATH)
ipfs_content_sidecar = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(ipfs_content_sidecar)


class IPFSContentSidecarTest(unittest.TestCase):
    def test_parse_cid_uses_final_ipfs_add_line(self) -> None:
        self.assertEqual(
            ipfs_content_sidecar.parse_cid("bafy-child file\nbafy-root dir\n"),
            "bafy-root",
        )

    def test_do_not_publish_marker_blocks_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "artifact"
            artifact.mkdir()
            manifest = artifact / "manifest.json"
            manifest.write_text(json.dumps({"signatures": [{"signature": "abcd"}]}), encoding="utf-8")
            (artifact / "DO_NOT_PUBLISH.txt").write_text("unsafe\n", encoding="utf-8")

            blockers = ipfs_content_sidecar.artifact_publish_blockers(
                artifact,
                manifest,
                json.loads(manifest.read_text(encoding="utf-8")),
                {},
            )

        self.assertTrue(any(item.startswith("do_not_publish_marker:") for item in blockers))

    def test_unsigned_manifest_blocks_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "artifact"
            artifact.mkdir()
            manifest = artifact / "manifest.json"
            manifest.write_text(json.dumps({"artifact_type": "raw_datadir_checkpoint"}), encoding="utf-8")

            blockers = ipfs_content_sidecar.artifact_publish_blockers(
                artifact,
                manifest,
                json.loads(manifest.read_text(encoding="utf-8")),
                {},
            )

        self.assertIn("manifest_unsigned", blockers)

    def test_dry_run_ready_requires_eligible_signed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            artifact = base / "current"
            artifact.mkdir()
            manifest = artifact / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "artifact_type": "raw_datadir_checkpoint",
                        "network": "mainnet",
                        "block_total": 123,
                        "signatures": [{"key_id": "test", "signature": "abcd"}],
                    }
                ),
                encoding="utf-8",
            )
            status = base / "status.json"
            env = {
                "BDAG_PROJECT_ROOT": str(ROOT),
                "BDAG_IPFS_CONTENT_SIDECAR_MODE": "auto",
                "BDAG_RAWDATADIR_SIDECAR_CONTENT_BASE": str(base.parent),
                "BDAG_IPFS_CONTENT_ARTIFACT_DIR": str(artifact),
                "BDAG_IPFS_CONTENT_ARTIFACT_MANIFEST": str(manifest),
                "BDAG_IPFS_CONTENT_STATUS_FILE": str(status),
                "BDAG_IPFS_CONTENT_SKIP_MAINTENANCE_DECISION": "1",
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                ipfs_content_sidecar,
                "source_eligibility",
                return_value={"eligible": True, "publish_allowed": False, "reasons": []},
            ):
                rc = ipfs_content_sidecar.main(["--dry-run"])

            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["action"], "dry_run")
        self.assertEqual(payload["eligibility"]["publish_allowed"], False)


if __name__ == "__main__":
    unittest.main()
