#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "ops" / "fastartifact_sidecar.py"


spec = importlib.util.spec_from_file_location("fastartifact_sidecar", SIDECAR)
assert spec and spec.loader
sidecar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sidecar)


class FastArtifactSidecarTests(unittest.TestCase):
    def test_fresh_seed_skips_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed_dir = Path(tmp)
            (seed_dir / "snapshot.bdsnap").write_bytes(b"archive")
            (seed_dir / "snapshot.bdsnap.manifest.json").write_text(
                json.dumps({"tip_order": 1000}), encoding="utf-8"
            )
            old_env = os.environ.copy()
            try:
                os.environ["BDAG_FASTARTIFACT_SIDECAR_MAX_SEED_LAG"] = "10000"
                os.environ["BDAG_FASTARTIFACT_SIDECAR_MAX_ARCHIVE_AGE_SECONDS"] = "7200"
                export, reason = sidecar.should_export(seed_dir, 1200)
            finally:
                os.environ.clear()
                os.environ.update(old_env)
            self.assertFalse(export, reason)
            self.assertIn("fresh", reason)

    def test_missing_seed_requests_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export, reason = sidecar.should_export(Path(tmp), 1200)
        self.assertTrue(export)
        self.assertIn("missing", reason)

    def test_default_seed_lag_threshold_is_ten_thousand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed_dir = Path(tmp)
            (seed_dir / "snapshot.bdsnap").write_bytes(b"archive")
            (seed_dir / "snapshot.bdsnap.manifest.json").write_text(
                json.dumps({"tip_order": 1000}), encoding="utf-8"
            )
            old_env = os.environ.copy()
            try:
                os.environ.pop("BDAG_FASTARTIFACT_SIDECAR_MAX_SEED_LAG", None)
                os.environ["BDAG_FASTARTIFACT_SIDECAR_MAX_ARCHIVE_AGE_SECONDS"] = "7200"
                export, reason = sidecar.should_export(seed_dir, 11000)
                self.assertFalse(export, reason)
                export, reason = sidecar.should_export(seed_dir, 11001)
                self.assertTrue(export)
            finally:
                os.environ.clear()
                os.environ.update(old_env)

    def test_manifest_tip_accepts_legacy_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.bdsnap.manifest.json"
            path.write_text(json.dumps({"TipOrder": "42"}), encoding="utf-8")
            self.assertEqual(sidecar.manifest_tip(path), 42)


if __name__ == "__main__":
    unittest.main()
