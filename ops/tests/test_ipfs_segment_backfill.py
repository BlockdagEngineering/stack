import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "ipfs_segment_backfill.py"
SPEC = importlib.util.spec_from_file_location("ipfs_segment_backfill", MODULE_PATH)
ipfs_segment_backfill = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(ipfs_segment_backfill)


class IPFSSegmentBackfillTest(unittest.TestCase):
    def test_next_start_order_uses_genesis_default_for_empty_index(self) -> None:
        self.assertEqual(ipfs_segment_backfill.next_start_order({}, 1), 1)

    def test_next_start_order_resumes_after_current_head(self) -> None:
        index = {
            "current_head": {"end_order": 600},
            "segments": [{"segment_id": 1, "start_order": 301, "end_order": 600}],
        }
        self.assertEqual(ipfs_segment_backfill.next_start_order(index, 1), 601)

    def test_main_requires_bounded_stop_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            with mock.patch.object(ipfs_segment_backfill.ipfs_segment_writer, "load_env", return_value={}):
                rc = ipfs_segment_backfill.main(["--status-file", str(status), "--json"])
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 1)
        self.assertEqual(payload["state"], "blocked")
        self.assertEqual(payload["reason"], "stop_order_required")


if __name__ == "__main__":
    unittest.main()
