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
        self.assertEqual(decision["matched_by"], "ip-observation")
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

    def test_ip_without_mac_is_observation_only(self) -> None:
        self.write_retirement()
        item = {"ip": "192.168.1.102"}

        decision = pool_ops.retired_miner_identity_decision(item, item["ip"], "")

        self.assertFalse(decision["retired"])
        self.assertTrue(decision["conflict"])
        self.assertEqual(decision["matched_by"], "ip-observation")

    def test_worker_match_without_mac_is_observation_only(self) -> None:
        self.write_retirement()
        item = {
            "ip": "192.168.1.200",
            "workers": ["0x1719E0ee598c15957448D5E568948101DF78e7A0"],
            "shares": 2,
        }

        decision = pool_ops.retired_miner_identity_decision(item, item["ip"], "")

        self.assertFalse(decision["retired"])
        self.assertFalse(decision["conflict"])
        self.assertEqual(decision["matched_by"], "")


class MinerHealthCountTests(unittest.TestCase):
    def test_ok_count_includes_unmanaged_tracked_miners(self) -> None:
        health = [
            {"managed": False, "status": "ok", "connected": True, "device_type": "stratum"},
            {"managed": False, "status": "ok", "connected": True, "device_type": "stratum"},
            {"managed": True, "status": "degraded", "connected": True, "device_type": "asic"},
        ]

        counts = pool_ops.miner_health_count_summary(health)

        self.assertEqual(counts["tracked_count"], 3)
        self.assertEqual(counts["connected_count"], 3)
        self.assertEqual(counts["managed_count"], 1)
        self.assertEqual(counts["managed_ok_count"], 0)
        self.assertEqual(counts["ok_count"], 2)


class PoolActivityAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_read_miner_registry = pool_ops.read_miner_registry
        self.addCleanup(self.restore_registry)

    def restore_registry(self) -> None:
        pool_ops.read_miner_registry = self.old_read_miner_registry

    def test_shared_worker_without_job_mapping_is_not_assigned_to_one_miner(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {"ip": "192.168.1.14", "mac": "28:e2:97:3e:39:63", "expected_worker_user": worker},
                {"ip": "192.168.1.103", "mac": "28:e2:97:1e:c0:b5", "expected_worker_user": worker},
            ]
        }
        log = "\n".join(
            [
                f"2026/05/26 06:20:00 [192.168.1.14:40541] authorize accepted user={worker}",
                f"2026/05/26 06:20:00 [192.168.1.103:45403] authorize accepted user={worker}",
                f"2026/05/26 06:20:01 valid share accepted 100.0 -> 500 worker={worker} job=missing-notify",
            ]
        )

        miners = {item["ip"]: item for item in pool_ops.parse_pool_activity(log)["miners"]}

        self.assertEqual(miners["192.168.1.14"]["shares"], 0)
        self.assertEqual(miners["192.168.1.103"]["shares"], 0)

    def test_shared_worker_with_job_mapping_uses_job_client(self) -> None:
        worker = "0x05518E03e148C56e426ff9e1CBdB962B4FC5250A"
        pool_ops.read_miner_registry = lambda: {
            "miners": [
                {"ip": "192.168.1.14", "mac": "28:e2:97:3e:39:63", "expected_worker_user": worker},
                {"ip": "192.168.1.103", "mac": "28:e2:97:1e:c0:b5", "expected_worker_user": worker},
            ]
        }
        log = "\n".join(
            [
                "2026/05/26 06:20:00 Sending to 192.168.1.14:40541: jobID=job-1",
                f"2026/05/26 06:20:01 valid share accepted 100.0 -> 500 worker={worker} job=job-1",
            ]
        )

        miners = {item["ip"]: item for item in pool_ops.parse_pool_activity(log)["miners"]}

        self.assertEqual(miners["192.168.1.14"]["shares"], 1)
        self.assertEqual(miners["192.168.1.14"]["share_work"], 500)
        self.assertNotIn("192.168.1.103", miners)


if __name__ == "__main__":
    unittest.main()
