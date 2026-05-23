#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "build-fastsnap-seed.sh"


class FastSnapSeedScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_restores_pool_redundancy_before_heavy_verify(self) -> None:
        restore_idx = self.source.index("restore_export_backend_before_verify")
        verify_idx = self.source.index("log \"verifying exported FastSnap archive\"")
        self.assertLess(restore_idx, verify_idx)
        self.assertIn("Verification is a heavy sequential read", self.source)

    def test_cleanup_is_idempotent(self) -> None:
        self.assertIn("CLEANUP_DONE=0", self.source)
        self.assertIn('if [[ "$CLEANUP_DONE" == "1" ]]', self.source)
        self.assertIn("CLEANUP_DONE=1", self.source)

    def test_verify_can_be_deferred_without_promoting_temp_archive(self) -> None:
        defer_idx = self.source.index('if [[ "$VERIFY_AFTER_EXPORT" != "1" ]]')
        promote_idx = self.source.index('mv -f "$SNAP_TMP" "$SNAP_FINAL"', defer_idx)
        self.assertLess(defer_idx, promote_idx)
        self.assertIn("exported but not verified/promoted", self.source)

    def test_existing_temp_archive_can_be_verified_without_re_export(self) -> None:
        verify_existing_idx = self.source.index('if [[ "$VERIFY_EXISTING" == "1" ]]')
        delete_tmp_idx = self.source.index('rm -f "$SNAP_TMP" "$MANIFEST_TMP"')
        export_idx = self.source.index("log \"exporting FastSnap archive")
        self.assertLess(verify_existing_idx, delete_tmp_idx)
        self.assertLess(verify_existing_idx, export_idx)
        self.assertIn("verify_existing_snapshot", self.source)

    def test_snapshot_container_runs_with_low_cpu_and_io_weight(self) -> None:
        self.assertIn("docker_run_low_priority", self.source)
        self.assertIn("--cpu-shares", self.source)
        self.assertIn("--blkio-weight", self.source)


if __name__ == "__main__":
    unittest.main()
