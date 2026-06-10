#!/usr/bin/env python3
"""Verify BlockDAG IPFS segment archive data without mutating chain state.

The restore drill treats IPFS/IPNS as untrusted byte transport. It fetches a
segment index and its manifests/payloads, verifies their hashes and append-only
order continuity, then writes a status file that installers and recovery tools
can consume before any destructive restore step is considered.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(os.environ.get("BDAG_PROJECT_ROOT") or Path(__file__).resolve().parents[1]).resolve()
ENV_FILE = Path(os.environ.get("BDAG_ENV_FILE") or ROOT / ".env")
OPS_DIR = ROOT / "ops"
MAINNET_NETWORK = "mainnet"
sys.path.insert(0, str(OPS_DIR))
import ipfs_segment_trust  # type: ignore  # noqa: E402

FALSE_VALUES = {"0", "false", "no", "off", "disabled"}
TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


class VerificationError(RuntimeError):
    """Raised when an IPFS archive object fails deterministic verification."""


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    env.update({key: value for key, value in os.environ.items() if key.startswith("BDAG_") or key in {"IPFS_PATH"}})
    return env


def env_bool(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    value = str(env.get(key, "")).strip().lower()
    if not value:
        return default
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return default


def env_int(env: Mapping[str, str], key: str, default: int) -> int:
    value = str(env.get(key, "")).strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def resolve_path(value: str | None, default: Path) -> Path:
    if not value:
        return default.resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def canonical_json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        tmp = Path(handle.name)
    tmp.replace(path)


def load_json_file(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise VerificationError(f"{path} did not contain a JSON object")
    return data


def ipfs_binary(env: Mapping[str, str]) -> str:
    return str(env.get("BDAG_IPFS_BINARY") or "ipfs")


def run_command(command: list[str], timeout: int, env: Mapping[str, str]) -> subprocess.CompletedProcess[bytes]:
    child_env = os.environ.copy()
    child_env.update(env)
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=child_env,
        check=False,
    )


def cid_filename(cid: str) -> str:
    value = cid.strip().removeprefix("ipfs://").removeprefix("/ipfs/")
    if not value or "/" in value or "\\" in value:
        raise VerificationError(f"unsafe or empty CID value: {cid!r}")
    return value


def ipfs_cat(cid: str, env: Mapping[str, str]) -> bytes:
    clean = cid_filename(cid)
    result = run_command(
        [ipfs_binary(env), "cat", f"/ipfs/{clean}"],
        env_int(env, "BDAG_IPFS_RESTORE_IPFS_TIMEOUT", 600),
        env,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or b"ipfs cat failed").decode("utf-8", errors="replace").strip()
        raise VerificationError(f"ipfs cat failed for {clean}: {message}")
    return result.stdout


def fixture_cat(cid: str, cid_dir: Path) -> bytes:
    clean = cid_filename(cid)
    for candidate in (cid_dir / clean, cid_dir / f"{clean}.json"):
        if candidate.exists():
            return candidate.read_bytes()
    raise VerificationError(f"fixture CID {clean} not found under {cid_dir}")


def load_json_from_bytes(raw: bytes, label: str) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise VerificationError(f"{label} did not contain a JSON object")
    return data


def fetch_json_by_cid(cid: str, env: Mapping[str, str], cid_dir: Path | None = None) -> tuple[dict[str, Any], bytes]:
    raw = fixture_cat(cid, cid_dir) if cid_dir else ipfs_cat(cid, env)
    return load_json_from_bytes(raw, cid_filename(cid)), raw


def status_path(env: Mapping[str, str]) -> Path:
    return resolve_path(env.get("BDAG_IPFS_RESTORE_STATUS_FILE"), ROOT / "ops/runtime/ipfs-content/restore-drill-status.json")


def candidate_dir(env: Mapping[str, str]) -> Path:
    return resolve_path(env.get("BDAG_IPFS_RESTORE_CANDIDATE_DIR"), ROOT / "ops/runtime/ipfs-content/restore-candidate")


def discovery_path(env: Mapping[str, str]) -> Path:
    return resolve_path(env.get("BDAG_IPFS_CONTENT_DISCOVERY_FILE"), ROOT / "ops/ipfs-content-discovery.json")


def latest_index_path(env: Mapping[str, str]) -> Path:
    return resolve_path(env.get("BDAG_IPFS_SEGMENT_INDEX_PATH") or env.get("BDAG_IPFS_CONTENT_LATEST_INDEX_PATH"), ROOT / "ops/runtime/ipfs-content/latest-index.json")


def index_cid_from_discovery(path: Path) -> str:
    data = load_json_file(path)
    for key in ("current_latest_index_cid", "latest_index_cid"):
        value = str(data.get(key) or "").strip()
        if value:
            return cid_filename(value)
    uri = str(data.get("current_latest_index_uri") or "").strip()
    if uri.startswith("ipfs://"):
        return cid_filename(uri)
    raise VerificationError(f"{path} does not contain current_latest_index_cid")


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def int_value(value: Any, field: str, errors: list[str]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{field} must be an integer")
        return None
    return value


def str_value(value: Any, field: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field} must be a non-empty string")
        return ""
    return value


def index_segments(index: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = index.get("segments")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def verify_index(index: Mapping[str, Any], network: str, env: Mapping[str, str]) -> list[dict[str, Any]]:
    errors: list[str] = []
    require(index.get("document_type") == "bdag_ipfs_segment_index_v1", "index document_type mismatch", errors)
    require(index.get("network") == network, f"index network must be {network}", errors)
    segments = index_segments(index)
    require(bool(segments), "index has no segment records", errors)
    previous_end: int | None = None
    previous_segment_id: int | None = None
    for idx, record in enumerate(segments):
        prefix = f"segments[{idx}]"
        segment_id = int_value(record.get("segment_id"), f"{prefix}.segment_id", errors)
        start = int_value(record.get("start_order"), f"{prefix}.start_order", errors)
        end = int_value(record.get("end_order"), f"{prefix}.end_order", errors)
        require(bool(str_value(record.get("manifest_cid"), f"{prefix}.manifest_cid", errors)), f"{prefix}.manifest_cid missing", errors)
        require(bool(str_value(record.get("payload_cid"), f"{prefix}.payload_cid", errors)), f"{prefix}.payload_cid missing", errors)
        require(bool(str_value(record.get("payload_sha256"), f"{prefix}.payload_sha256", errors)), f"{prefix}.payload_sha256 missing", errors)
        require(bool(str_value(record.get("manifest_sha256"), f"{prefix}.manifest_sha256", errors)), f"{prefix}.manifest_sha256 missing", errors)
        if start is not None and end is not None:
            require(start <= end, f"{prefix}.start_order must be <= end_order", errors)
            if previous_end is not None:
                require(start == previous_end + 1, f"{prefix} is not contiguous after order {previous_end}", errors)
        if segment_id is not None and previous_segment_id is not None:
            require(segment_id == previous_segment_id + 1, f"{prefix}.segment_id is not monotonic", errors)
        previous_end = end if end is not None else previous_end
        previous_segment_id = segment_id if segment_id is not None else previous_segment_id
    head = index.get("current_head")
    if isinstance(head, dict) and segments:
        last = segments[-1]
        require(head.get("end_order") == last.get("end_order"), "current_head.end_order does not match last segment", errors)
        require(head.get("manifest_cid") == last.get("manifest_cid"), "current_head.manifest_cid does not match last segment", errors)
    if errors:
        raise VerificationError("; ".join(errors))
    try:
        ipfs_segment_trust.verify_payload_signature(
            index,
            env,
            signature_field="index_signatures",
            context="segment index",
        )
    except RuntimeError as exc:
        raise VerificationError(str(exc)) from exc
    return segments


def verify_index_lineage(index: Mapping[str, Any], env: Mapping[str, str], cid_dir: Path | None) -> dict[str, Any]:
    if not env_bool(env, "BDAG_IPFS_RESTORE_VERIFY_INDEX_LINEAGE", True):
        return {"index_lineage_verified": False, "index_lineage_reason": "disabled_by_policy", "index_lineage_depth": 0}

    max_depth = max(0, env_int(env, "BDAG_IPFS_RESTORE_MAX_INDEX_LINEAGE_DEPTH", 256))
    current = index
    previous_cid = str(current.get("previous_index_cid") or "").strip()
    seen: set[str] = set()
    depth = 0
    verified_links: list[dict[str, Any]] = []
    while previous_cid:
        if max_depth and depth >= max_depth:
            raise VerificationError(f"index lineage exceeds BDAG_IPFS_RESTORE_MAX_INDEX_LINEAGE_DEPTH={max_depth}")
        if previous_cid in seen:
            raise VerificationError(f"index lineage cycle detected at {previous_cid}")
        seen.add(previous_cid)
        link = current.get("previous_index_link")
        if not isinstance(link, Mapping):
            raise VerificationError("index has previous_index_cid but missing previous_index_link")
        if str(link.get("index_cid") or "").strip() != previous_cid:
            raise VerificationError("previous_index_link.index_cid does not match previous_index_cid")
        previous, _previous_raw = fetch_json_by_cid(previous_cid, env, cid_dir)
        verify_index(previous, MAINNET_NETWORK, env)
        link_head = link.get("previous_current_head")
        previous_head = previous.get("current_head")
        if isinstance(link_head, Mapping) and link_head and dict(link_head) != dict(previous_head or {}):
            raise VerificationError("previous_index_link.previous_current_head does not match fetched previous index")

        reason = str(link.get("reason") or "")
        if reason != "stale_head_live_tail_reset":
            previous_segments = index_segments(previous)
            current_segments = index_segments(current)
            if previous_segments and current_segments[: len(previous_segments)] != previous_segments:
                raise VerificationError("previous index segments are not an immutable prefix of the current index")

        verified_links.append(
            {
                "previous_index_cid": previous_cid,
                "reason": reason or "segment_append",
                "previous_end_order": (previous_head or {}).get("end_order") if isinstance(previous_head, Mapping) else None,
            }
        )
        current = previous
        previous_cid = str(current.get("previous_index_cid") or "").strip()
        depth += 1
    return {
        "index_lineage_verified": True,
        "index_lineage_depth": depth,
        "index_lineage_links": verified_links,
        "index_lineage_max_depth": max_depth,
    }


def verify_manifest(
    record: Mapping[str, Any],
    manifest: Mapping[str, Any],
    raw: bytes,
    network: str,
    previous_manifest_cid: str | None,
    env: Mapping[str, str],
) -> None:
    errors: list[str] = []
    require(manifest.get("document_type") == "bdag_ipfs_segment_manifest_v1", "manifest document_type mismatch", errors)
    require(manifest.get("network") == network, f"manifest network must be {network}", errors)
    for field in ("segment_id", "start_order", "end_order", "block_count"):
        require(manifest.get(field) == record.get(field), f"manifest {field} does not match index record", errors)
    for field in ("payload_cid", "payload_sha256", "start_hash", "end_hash"):
        if field in record:
            require(manifest.get(field) == record.get(field), f"manifest {field} does not match index record", errors)
    expected_manifest_sha = str(record.get("manifest_sha256") or "")
    require(bool(expected_manifest_sha), "index record missing manifest_sha256", errors)
    if expected_manifest_sha:
        require(sha256_bytes(raw) == expected_manifest_sha, "manifest sha256 mismatch", errors)
    expected_previous = previous_manifest_cid if previous_manifest_cid else None
    require(manifest.get("previous_segment_manifest_cid") == expected_previous, "manifest previous_segment_manifest_cid mismatch", errors)
    require(manifest.get("payload_format") == "bdag_chain_order_segment_payload_v1", "manifest payload_format mismatch", errors)
    if errors:
        raise VerificationError("; ".join(errors))
    try:
        ipfs_segment_trust.verify_payload_signature(
            manifest,
            env,
            signature_field="manifest_signatures",
            context=f"segment manifest {record.get('segment_id')}",
        )
    except RuntimeError as exc:
        raise VerificationError(str(exc)) from exc


def verify_payload(record: Mapping[str, Any], manifest: Mapping[str, Any], payload: Mapping[str, Any], raw: bytes, network: str) -> dict[str, Any]:
    errors: list[str] = []
    require(payload.get("document_type") == "bdag_chain_order_segment_payload_v1", "payload document_type mismatch", errors)
    require(payload.get("network") == network, f"payload network must be {network}", errors)
    for field in ("segment_id", "start_order", "end_order", "block_count"):
        require(payload.get(field) == manifest.get(field), f"payload {field} does not match manifest", errors)
    expected_payload_sha = str(manifest.get("payload_sha256") or "")
    require(bool(expected_payload_sha), "manifest missing payload_sha256", errors)
    if expected_payload_sha:
        require(sha256_bytes(raw) == expected_payload_sha, "payload sha256 mismatch", errors)
    blocks = payload.get("blocks")
    require(isinstance(blocks, list), "payload blocks must be a list", errors)
    block_records = blocks if isinstance(blocks, list) else []
    require(len(block_records) == manifest.get("block_count"), "payload block_count does not match blocks length", errors)
    start = manifest.get("start_order")
    end = manifest.get("end_order")
    if isinstance(start, int) and isinstance(end, int):
        expected_order = start
        for idx, block in enumerate(block_records):
            prefix = f"blocks[{idx}]"
            if not isinstance(block, dict):
                errors.append(f"{prefix} must be an object")
                continue
            require(block.get("order") == expected_order, f"{prefix}.order must be {expected_order}", errors)
            if block.get("order") == start:
                require(block.get("hash") == manifest.get("start_hash"), f"{prefix}.hash does not match start_hash", errors)
            if block.get("order") == end:
                require(block.get("hash") == manifest.get("end_hash"), f"{prefix}.hash does not match end_hash", errors)
            raw_hex = block.get("raw_block_hex")
            raw_sha = block.get("raw_block_sha256")
            if isinstance(raw_hex, str) and raw_hex:
                require(hashlib.sha256(raw_hex.encode("ascii")).hexdigest() == raw_sha, f"{prefix}.raw_block_sha256 mismatch", errors)
            else:
                errors.append(f"{prefix}.raw_block_hex must be a non-empty string")
            expected_order += 1
    if errors:
        raise VerificationError("; ".join(errors))
    return {
        "segment_id": record.get("segment_id"),
        "start_order": record.get("start_order"),
        "end_order": record.get("end_order"),
        "block_count": record.get("block_count"),
        "manifest_cid": record.get("manifest_cid"),
        "payload_cid": record.get("payload_cid"),
    }


def write_candidate_object(base: Path, cid: str, raw: bytes) -> Path:
    path = base / f"{cid_filename(cid)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def verify_archive(
    *,
    index: Mapping[str, Any],
    env: Mapping[str, str],
    cid_dir: Path | None,
    max_segments: int,
    materialize_dir: Path | None,
) -> dict[str, Any]:
    network = str(env.get("BDAG_NETWORK") or MAINNET_NETWORK).strip().lower()
    if network != MAINNET_NETWORK:
        raise VerificationError(f"IPFS restore drill refuses non-mainnet network: {network}")
    records = verify_index(index, MAINNET_NETWORK, env)
    lineage = verify_index_lineage(index, env, cid_dir)
    if max_segments > 0:
        records = records[-max_segments:]
    verified: list[dict[str, Any]] = []
    previous_manifest_cid: str | None = None
    if records:
        first_record = records[0]
        first_index = index_segments(index).index(first_record)
        if first_index > 0:
            previous_manifest_cid = str(index_segments(index)[first_index - 1].get("manifest_cid") or "")
    for record in records:
        manifest_cid = str(record.get("manifest_cid") or "")
        payload_cid = str(record.get("payload_cid") or "")
        manifest, manifest_raw = fetch_json_by_cid(manifest_cid, env, cid_dir)
        verify_manifest(record, manifest, manifest_raw, MAINNET_NETWORK, previous_manifest_cid or None, env)
        payload, payload_raw = fetch_json_by_cid(payload_cid, env, cid_dir)
        verified.append(verify_payload(record, manifest, payload, payload_raw, MAINNET_NETWORK))
        if materialize_dir:
            write_candidate_object(materialize_dir / "manifests", manifest_cid, manifest_raw)
            write_candidate_object(materialize_dir / "payloads", payload_cid, payload_raw)
        previous_manifest_cid = manifest_cid
    return {
        "segments_verified": len(verified),
        "verified_segments": verified,
        "first_verified_order": verified[0]["start_order"] if verified else None,
        "last_verified_order": verified[-1]["end_order"] if verified else None,
        **lineage,
    }


def load_index(args: argparse.Namespace, env: Mapping[str, str]) -> tuple[dict[str, Any], bytes | None, str]:
    if args.index:
        path = resolve_path(args.index, Path(args.index))
        data = load_json_file(path)
        return data, canonical_json_bytes(data), f"file:{path}"
    index_cid = str(args.index_cid or "").strip()
    if not index_cid:
        discovery = resolve_path(args.discovery, discovery_path(env)) if args.discovery else discovery_path(env)
        index_cid = index_cid_from_discovery(discovery)
    cid_dir = Path(args.cid_dir).expanduser().resolve() if args.cid_dir else None
    data, raw = fetch_json_by_cid(index_cid, env, cid_dir)
    return data, raw, f"ipfs:{cid_filename(index_cid)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", default="", help="local segment-index JSON path; bypasses IPFS for the index only")
    parser.add_argument("--index-cid", default="", help="segment-index CID to fetch through IPFS or --cid-dir")
    parser.add_argument("--discovery", default="", help="discovery JSON path containing current_latest_index_cid")
    parser.add_argument("--cid-dir", default="", help="test/offline directory containing <cid>.json fixtures instead of calling ipfs cat")
    parser.add_argument("--max-segments", type=int, default=-1, help="limit verification to latest N segments; 0 verifies all")
    parser.add_argument("--materialize", action="store_true", help="write verified index/manifests/payloads into the restore candidate dir")
    parser.add_argument("--status-file", default="", help="override status JSON path")
    parser.add_argument("--json", action="store_true", help="print status JSON")
    args = parser.parse_args(argv)

    env = load_env()
    if args.status_file:
        env["BDAG_IPFS_RESTORE_STATUS_FILE"] = args.status_file
    mode = str(env.get("BDAG_IPFS_RESTORE_MODE") or "verify").strip().lower()
    if mode in FALSE_VALUES:
        payload = {
            "generated_at": now_iso(),
            "state": "disabled",
            "mode": mode,
            "project_root": str(ROOT),
            "reasons": ["mode_disabled"],
        }
        atomic_write_json(status_path(env), payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    try:
        max_segments = args.max_segments if args.max_segments >= 0 else env_int(env, "BDAG_IPFS_RESTORE_MAX_SEGMENTS", 16)
        cid_dir = Path(args.cid_dir).expanduser().resolve() if args.cid_dir else None
        materialize_dir = candidate_dir(env) if args.materialize or env_bool(env, "BDAG_IPFS_RESTORE_MATERIALIZE", False) else None
        index, index_raw, index_source = load_index(args, env)
        verified = verify_archive(
            index=index,
            env=env,
            cid_dir=cid_dir,
            max_segments=max_segments,
            materialize_dir=materialize_dir,
        )
        if materialize_dir and index_raw is not None:
            index_name = (
                index_source.removeprefix("ipfs:")
                if index_source.startswith("ipfs:")
                else f"local-index-{sha256_bytes(index_raw)}"
            )
            write_candidate_object(materialize_dir / "indexes", index_name, index_raw)
        history = index.get("history_completeness") if isinstance(index.get("history_completeness"), dict) else {}
        backfill_before = history.get("backfill_required_before_order") if isinstance(history, dict) else None
        payload = {
            "generated_at": now_iso(),
            "state": "verified",
            "mode": mode,
            "project_root": str(ROOT),
            "index_source": index_source,
            "index_sha256": sha256_bytes(index_raw) if index_raw is not None else None,
            "network": MAINNET_NETWORK,
            "max_segments": max_segments,
            "materialized": bool(materialize_dir),
            "candidate_dir": str(materialize_dir) if materialize_dir else "",
            "history_complete": backfill_before in (None, 0, 1),
            "backfill_required_before_order": backfill_before,
            "usable_for_destructive_restore": False,
            "restore_policy": "verification_only_no_chain_datadir_mutation",
            "trust_model": "IPFS/IPNS are byte transport only; CID bytes, sha256, manifest links, order continuity, and chain consensus must verify before use.",
        }
        payload.update(verified)
    except Exception as exc:  # noqa: BLE001 - report all verifier failures in status JSON.
        payload = {
            "generated_at": now_iso(),
            "state": "failed",
            "mode": env.get("BDAG_IPFS_RESTORE_MODE", "verify"),
            "project_root": str(ROOT),
            "reasons": [str(exc)],
            "exception_type": type(exc).__name__,
            "usable_for_destructive_restore": False,
            "restore_policy": "verification_failed_no_chain_datadir_mutation",
        }
        atomic_write_json(status_path(env), payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 1

    atomic_write_json(status_path(env), payload)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
