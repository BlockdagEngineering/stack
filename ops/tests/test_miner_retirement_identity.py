#!/usr/bin/env python3

import json
import pathlib
import sys
import tempfile
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class MinerRetirementIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.retirements_file = pathlib.Path(self.tmp.name) / "miner-retirements.json"
        self.old_retirements_file = pool_ops.MINER_RETIREMENTS_FILE
        pool_ops.MINER_RETIREMENTS_FILE = self.retirements_file
        self.addCleanup(self.restore_retirements_file)

    def restore_retirements_file(self) -> None:
        pool_ops.MINER_RETIREMENTS_FILE = self.old_retirements_file

    def write_retirement(self) -> None:
        self.retirements_file.write_text(
            json.dumps(
                {
                    "retired_miners": [
                        {
                            "display_name": "Athena",
                            "mac": "28:e2:97:4c:e4:0a",
                            "ips": ["192.168.1.102"],
                            "worker_user": "0x1719E0ee598c15957448D5E568948101DF78e7A0",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def test_active_different_identity_at_retired_ip_is_not_hidden(self) -> None:
        self.write_retirement()
        item = {
            "ip": "192.168.1.102",
            "mac": "2a:71:c7:f5:1f:1e",
            "workers": ["0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"],
            "submits": 4,
            "shares": 2,
        }

        decision = pool_ops.retired_miner_identity_decision(item, item["ip"], item["mac"])

        self.assertFalse(decision["retired"])
        self.assertTrue(decision["conflict"])
        self.assertEqual(decision["matched_by"], "ip-conflict")
        self.assertFalse(pool_ops.is_retired_miner_identity(item, item["ip"], item["mac"]))

    def test_same_mac_retirement_remains_authoritative(self) -> None:
        self.write_retirement()
        item = {
            "ip": "192.168.1.102",
            "mac": "28:e2:97:4c:e4:0a",
            "workers": ["0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"],
            "shares": 2,
        }

        decision = pool_ops.retired_miner_identity_decision(item, item["ip"], item["mac"])

        self.assertTrue(decision["retired"])
        self.assertFalse(decision["conflict"])
        self.assertEqual(decision["matched_by"], "mac")

    def test_ip_fallback_still_retired_without_new_active_identity(self) -> None:
        self.write_retirement()
        item = {"ip": "192.168.1.102"}

        decision = pool_ops.retired_miner_identity_decision(item, item["ip"], "")

        self.assertTrue(decision["retired"])
        self.assertFalse(decision["conflict"])
        self.assertEqual(decision["matched_by"], "ip")


if __name__ == "__main__":
    unittest.main()
