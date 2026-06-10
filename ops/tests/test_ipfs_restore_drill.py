import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "ipfs_restore_drill.py"
SPEC = importlib.util.spec_from_file_location("ipfs_restore_drill", MODULE_PATH)
ipfs_restore_drill = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(ipfs_restore_drill)

SIGNING_KEY_HEX = "22" * 32
SIGNING_PUBLIC_HEX = ipfs_restore_drill.ipfs_segment_trust.public_key_hex(
    ipfs_restore_drill.ipfs_segment_trust.load_private_key(SIGNING_KEY_HEX)
)
SIGNING_ENV = {
    "BDAG_IPFS_SEGMENT_WRITER_ID": "writer-a",
    "BDAG_IPFS_SEGMENT_SIGNING_KEY_HEX": SIGNING_KEY_HEX,
    "BDAG_IPFS_SEGMENT_TRUSTED_SIGNERS": f"writer-a={SIGNING_PUBLIC_HEX}",
    "BDAG_IPFS_RESTORE_REQUIRE_SIGNATURES": "1",
}


def canonical(payload: Any) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def block(order: int) -> dict[str, Any]:
    raw_hex = f"0bad{order:04x}"
    return {
        "order": order,
        "hash": f"0x{order:064x}",
        "header": {"timestamp": 1_700_000_000 + order},
        "raw_block_hex": raw_hex,
        "raw_block_sha256": hashlib.sha256(raw_hex.encode("ascii")).hexdigest(),
    }


def segment_fixture(segment_id: int, start: int, end: int, previous_manifest_cid: str | None) -> tuple[dict[str, Any], dict[str, bytes]]:
    payload_cid = f"baf-payload-{segment_id}"
    manifest_cid = f"baf-manifest-{segment_id}"
    blocks = [block(order) for order in range(start, end + 1)]
    payload = {
        "document_type": "bdag_chain_order_segment_payload_v1",
        "network": "mainnet",
        "segment_id": segment_id,
        "start_order": start,
        "end_order": end,
        "block_count": len(blocks),
        "build_algorithm": "getBlockByOrder_verbose_header_plus_raw_block_hex_v1",
        "blocks": blocks,
    }
    payload_raw = canonical(payload)
    manifest = {
        "document_type": "bdag_ipfs_segment_manifest_v1",
        "network": "mainnet",
        "generated_at": "2026-06-09T00:00:00+0200",
        "segment_id": segment_id,
        "start_order": start,
        "end_order": end,
        "block_count": len(blocks),
        "start_hash": blocks[0]["hash"],
        "end_hash": blocks[-1]["hash"],
        "start_timestamp": blocks[0]["header"]["timestamp"],
        "end_timestamp": blocks[-1]["header"]["timestamp"],
        "previous_segment_manifest_cid": previous_manifest_cid,
        "base_anchor_order": start - 1,
        "base_anchor_hash": None,
        "payload_cid": payload_cid,
        "payload_sha256": sha256(payload_raw),
        "payload_size_bytes": len(payload_raw),
        "payload_format": "bdag_chain_order_segment_payload_v1",
        "source": {"rpc_source": "unit", "rpc_method": "getBlockByOrder"},
        "writer": {"mode": "local_writer", "kubo_peer_id": "peer", "ipns_name": ""},
        "election": {"phase": "local_writer", "rule": "unit", "fallback": "unit"},
        "trust_model": "unit",
    }
    manifest = ipfs_restore_drill.ipfs_segment_trust.sign_payload(
        manifest,
        SIGNING_ENV,
        signature_field="manifest_signatures",
    )
    manifest_raw = canonical(manifest)
    record = {
        "segment_id": segment_id,
        "start_order": start,
        "end_order": end,
        "block_count": len(blocks),
        "start_hash": blocks[0]["hash"],
        "end_hash": blocks[-1]["hash"],
        "payload_cid": payload_cid,
        "payload_sha256": sha256(payload_raw),
        "payload_size_bytes": len(payload_raw),
        "manifest_cid": manifest_cid,
        "manifest_sha256": sha256(manifest_raw),
    }
    return record, {payload_cid: payload_raw, manifest_cid: manifest_raw}


class IPFSRestoreDrillTest(unittest.TestCase):
    def write_fixtures(self, cid_dir: Path, fixtures: dict[str, bytes]) -> None:
        cid_dir.mkdir(parents=True, exist_ok=True)
        for cid, raw in fixtures.items():
            (cid_dir / f"{cid}.json").write_bytes(raw)

    def build_index(
        self,
        records: list[dict[str, Any]],
        *,
        previous_index_cid: str = "",
        previous_index: dict[str, Any] | None = None,
        previous_head_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        index = {
            "document_type": "bdag_ipfs_segment_index_v1",
            "network": "mainnet",
            "status": "active_single_writer_segments",
            "segments": records,
            "current_head": {
                "segment_id": records[-1]["segment_id"],
                "start_order": records[-1]["start_order"],
                "end_order": records[-1]["end_order"],
                "end_hash": records[-1]["end_hash"],
                "manifest_cid": records[-1]["manifest_cid"],
                "payload_cid": records[-1]["payload_cid"],
            },
            "history_completeness": {
                "complete_from_order": records[0]["start_order"],
                "backfill_required_before_order": None,
            },
        }
        if previous_index_cid:
            previous_head = (
                previous_head_override
                if previous_head_override is not None
                else dict((previous_index or {}).get("current_head") or {})
            )
            index["previous_index_cid"] = previous_index_cid
            index["previous_index_link"] = {
                "document_type": "bdag_ipfs_segment_previous_index_link_v1",
                "index_cid": previous_index_cid,
                "linked_at": "2026-06-09T00:01:00+0200",
                "reason": "segment_append",
                "previous_current_head": previous_head,
            }
            index["append_only_index_policy"] = {
                "immutable_index_cids": True,
                "latest_pointer_is_mutable_discovery_only": True,
            }
        index = ipfs_restore_drill.ipfs_segment_trust.sign_payload(
            index,
            SIGNING_ENV,
            signature_field="index_signatures",
        )
        return index

    def write_index(self, base: Path, records: list[dict[str, Any]]) -> Path:
        index = self.build_index(records)
        path = base / "latest-index.json"
        path.write_text(json.dumps(index), encoding="utf-8")
        return path

    def test_verify_local_index_and_fixture_cids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record1, fixture1 = segment_fixture(1, 1, 2, None)
            record2, fixture2 = segment_fixture(2, 3, 4, record1["manifest_cid"])
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(cid_dir, {**fixture1, **fixture2})
            index = self.write_index(base, [record1, record2])
            status = base / "status.json"

            with mock.patch.dict(os.environ, SIGNING_ENV, clear=False):
                rc = ipfs_restore_drill.main(
                    [
                        "--index",
                        str(index),
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                        "--max-segments",
                        "0",
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(payload["state"], "verified")
        self.assertEqual(payload["segments_verified"], 2)
        self.assertEqual(payload["first_verified_order"], 1)
        self.assertEqual(payload["last_verified_order"], 4)
        self.assertTrue(payload["index_lineage_verified"])
        self.assertEqual(payload["index_lineage_depth"], 0)
        self.assertFalse(payload["usable_for_destructive_restore"])

    def test_verifies_recursive_previous_index_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record1, fixture1 = segment_fixture(1, 1, 2, None)
            record2, fixture2 = segment_fixture(2, 3, 4, record1["manifest_cid"])
            previous_index = self.build_index([record1])
            previous_index_cid = "baf-index-previous"
            current_index = self.build_index(
                [record1, record2],
                previous_index_cid=previous_index_cid,
                previous_index=previous_index,
            )
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(
                cid_dir,
                {
                    **fixture1,
                    **fixture2,
                    previous_index_cid: canonical(previous_index),
                },
            )
            index = base / "latest-index.json"
            index.write_text(json.dumps(current_index), encoding="utf-8")
            status = base / "status.json"

            with mock.patch.dict(os.environ, SIGNING_ENV, clear=False):
                rc = ipfs_restore_drill.main(
                    [
                        "--index",
                        str(index),
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                        "--max-segments",
                        "0",
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertTrue(payload["index_lineage_verified"])
        self.assertEqual(payload["index_lineage_depth"], 1)
        self.assertEqual(payload["index_lineage_links"][0]["previous_index_cid"], previous_index_cid)

    def test_rejects_previous_index_lineage_head_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record1, fixture1 = segment_fixture(1, 1, 2, None)
            record2, fixture2 = segment_fixture(2, 3, 4, record1["manifest_cid"])
            previous_index = self.build_index([record1])
            previous_index_cid = "baf-index-previous"
            current_index = self.build_index(
                [record1, record2],
                previous_index_cid=previous_index_cid,
                previous_index=previous_index,
                previous_head_override={"end_order": 999, "manifest_cid": "wrong"},
            )
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(
                cid_dir,
                {
                    **fixture1,
                    **fixture2,
                    previous_index_cid: canonical(previous_index),
                },
            )
            index = base / "latest-index.json"
            index.write_text(json.dumps(current_index), encoding="utf-8")
            status = base / "status.json"

            with mock.patch.dict(os.environ, SIGNING_ENV, clear=False):
                rc = ipfs_restore_drill.main(
                    [
                        "--index",
                        str(index),
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 1)
        self.assertEqual(payload["state"], "failed")
        self.assertTrue(any("previous_current_head does not match" in reason for reason in payload["reasons"]))

    def test_rejects_non_contiguous_index_before_fetching_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record1, fixture1 = segment_fixture(1, 1, 2, None)
            record2, fixture2 = segment_fixture(2, 4, 5, record1["manifest_cid"])
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(cid_dir, {**fixture1, **fixture2})
            index = self.write_index(base, [record1, record2])
            status = base / "status.json"

            with mock.patch.dict(os.environ, SIGNING_ENV, clear=False):
                rc = ipfs_restore_drill.main(
                    [
                        "--index",
                        str(index),
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 1)
        self.assertEqual(payload["state"], "failed")
        self.assertTrue(any("not contiguous" in reason for reason in payload["reasons"]))

    def test_rejects_tampered_payload_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            record, fixtures = segment_fixture(1, 1, 2, None)
            payload = json.loads(fixtures[record["payload_cid"]].decode("utf-8"))
            payload["blocks"][0]["raw_block_hex"] = "ffff"
            fixtures[record["payload_cid"]] = canonical(payload)
            cid_dir = base / "cid-fixtures"
            self.write_fixtures(cid_dir, fixtures)
            index = self.write_index(base, [record])
            status = base / "status.json"

            with mock.patch.dict(os.environ, SIGNING_ENV, clear=False):
                rc = ipfs_restore_drill.main(
                    [
                        "--index",
                        str(index),
                        "--cid-dir",
                        str(cid_dir),
                        "--status-file",
                        str(status),
                    ]
                )
            payload = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(rc, 1)
        self.assertEqual(payload["state"], "failed")
        self.assertTrue(any("payload sha256 mismatch" in reason for reason in payload["reasons"]))


if __name__ == "__main__":
    unittest.main()
