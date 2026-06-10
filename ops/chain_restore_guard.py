#!/usr/bin/env python3
"""IPFS restore-point freshness guard for BlockDAG chain data."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from incident_journal import append_incident
from pool_ops import LOG_DIR, PROJECT_ROOT, RUNTIME_DIR, ensure_runtime, now_iso


STATE_FILE = RUNTIME_DIR / "chain-restore-guard-state.json"
HEALTH_FILE = RUNTIME_DIR / "chain-restore-health.json"
LOG_FILE = LOG_DIR / "chain-restore-guard.log"
STATUS_URL = os.environ.get("BDAG_RESTORE_GUARD_STATUS_URL", "http://127.0.0.1:8088/api/status")
STATUS_TIMEOUT = float(os.environ.get("BDAG_RESTORE_GUARD_STATUS_TIMEOUT", "20"))
MAX_RESTORE_AGE_SECONDS = int(os.environ.get("BDAG_RESTORE_POINT_MAX_AGE_SECONDS", str(6 * 3600)))
INCIDENT_COOLDOWN_SECONDS = int(os.environ.get("BDAG_RESTORE_GUARD_INCIDENT_COOLDOWN_SECONDS", "1800"))


def resolve_path(value: str | None, default: Path) -> Path:
    if not value:
        return default
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def configured_status_files() -> dict[str, Path]:
    return {
        "ipfs_segment_writer": resolve_path(
            os.environ.get("BDAG_IPFS_SEGMENT_STATUS_FILE"),
            PROJECT_ROOT / "ops/runtime/ipfs-content/segment-writer-status.json",
        ),
        "ipfs_content_sidecar": resolve_path(
            os.environ.get("BDAG_IPFS_CONTENT_STATUS_FILE"),
            PROJECT_ROOT / "ops/runtime/ipfs-content-sidecar-status.json",
        ),
        "rawdatadir_sidecar_safe": resolve_path(
            os.environ.get("BDAG_RAWDATADIR_SIDECAR_SAFE_STATUS"),
            PROJECT_ROOT / "ops/runtime/rawdatadir-sidecar-safe-status.json",
        ),
        "rawdatadir_ipfs_restore": resolve_path(
            os.environ.get("BDAG_IPFS_RAWDATADIR_RESTORE_STATUS_FILE"),
            PROJECT_ROOT / "ops/runtime/ipfs-content/rawdatadir-restore-status.json",
        ),
        "ipfs_restore_drill": resolve_path(
            os.environ.get("BDAG_IPFS_RESTORE_STATUS_FILE"),
            PROJECT_ROOT / "ops/runtime/ipfs-content/restore-drill-status.json",
        ),
    }


def configured_timers() -> list[str]:
    raw = os.environ.get(
        "BDAG_RESTORE_GUARD_IPFS_TIMERS",
        "bdag-ipfs-content-sidecar.timer,bdag-ipfs-segment-writer.timer,bdag-rawdatadir-sidecar.timer",
    )
    return [item.strip() for item in raw.split(",") if item.strip()]


def log(message: str) -> None:
    ensure_runtime()
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message}\n")


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_runtime()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def should_emit(state: dict[str, Any], key: str, signature: str, now: int) -> bool:
    last_signature = str(state.get(f"{key}_signature") or "")
    last_epoch = int(state.get(f"{key}_epoch", 0) or 0)
    if signature == last_signature and now - last_epoch < INCIDENT_COOLDOWN_SECONDS:
        return False
    state[f"{key}_signature"] = signature
    state[f"{key}_epoch"] = now
    state[f"{key}_at"] = now_iso()
    return True


def systemctl_user(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def unit_active(unit: str) -> bool:
    result = systemctl_user("is-active", unit)
    return result.returncode == 0 and result.stdout.strip() == "active"


def start_unit(unit: str) -> subprocess.CompletedProcess[str]:
    return systemctl_user("start", unit)


def status_api() -> tuple[dict[str, Any] | None, str]:
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=STATUS_TIMEOUT) as response:
            return json.loads(response.read(4_000_000).decode("utf-8", "replace")), ""
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, str(exc)


def status_file_info(name: str, path: Path, now: int) -> dict[str, Any]:
    payload = read_json(path)
    exists = path.exists()
    mtime = int(path.stat().st_mtime) if exists else None
    result: dict[str, Any] = {
        "name": name,
        "path": str(path),
        "exists": exists,
        "age_seconds": max(0, now - mtime) if mtime else None,
        "state": payload.get("state") or payload.get("status"),
    }
    for key in (
        "latest_index_cid",
        "index_cid",
        "artifact_cid",
        "raw_artifact_cid",
        "accepted_head",
        "last_published_order",
        "last_order",
        "tip_order",
        "reason",
        "reasons",
    ):
        if key in payload:
            result[key] = payload[key]
    return result


def restore_status(now: int) -> dict[str, Any]:
    files = {
        name: status_file_info(name, path, now)
        for name, path in configured_status_files().items()
    }
    fresh = [
        name
        for name, info in files.items()
        if isinstance(info.get("age_seconds"), int) and int(info["age_seconds"]) <= MAX_RESTORE_AGE_SECONDS
    ]
    stale = {
        name: info
        for name, info in files.items()
        if not isinstance(info.get("age_seconds"), int) or int(info["age_seconds"]) > MAX_RESTORE_AGE_SECONDS
    }
    return {
        "fresh": bool(fresh),
        "fresh_sources": fresh,
        "stale_or_missing": stale,
        "files": files,
        "max_restore_age_seconds": MAX_RESTORE_AGE_SECONDS,
    }


def ensure_ipfs_timers(state: dict[str, Any], now: int) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for timer in configured_timers():
        if unit_active(timer):
            results[timer] = {"active": True}
            continue
        result = start_unit(timer)
        results[timer] = {
            "active": False,
            "started": result.returncode == 0,
            "returncode": result.returncode,
            "stderr": result.stderr.strip(),
        }
        if should_emit(state, f"{timer}_inactive", str(result.returncode), now):
            append_incident(
                "restore_guard_started_ipfs_timer" if result.returncode == 0 else "restore_guard_ipfs_timer_start_failed",
                "warning" if result.returncode == 0 else "critical",
                "chain-restore-guard",
                f"Restore guard {'started' if result.returncode == 0 else 'could not start'} {timer}",
                {"timer": timer, "returncode": result.returncode, "stderr": result.stderr.strip()},
            )
    return results


def main() -> int:
    ensure_runtime()
    now = int(time.time())
    state = read_json(STATE_FILE)

    status, status_error = status_api()
    ipfs_restore = restore_status(now)
    timers = ensure_ipfs_timers(state, now)

    stale = ipfs_restore["stale_or_missing"]
    if stale and should_emit(state, "ipfs_restore_status_stale", ",".join(sorted(stale)), now):
        append_incident(
            "restore_point_stale",
            "warning",
            "chain-restore-guard",
            "IPFS restore metadata is stale or missing",
            {"stale_or_missing": stale},
        )
        log(f"IPFS restore metadata stale or missing: {','.join(sorted(stale))}")

    payload = {
        "generated_at": now_iso(),
        "status_api_error": status_error,
        "sync_status": (status or {}).get("sync_progress", {}).get("status") if isinstance(status, dict) else None,
        "restore_transport": "ipfs",
        "ipfs_restore": ipfs_restore,
        "timers": timers,
    }
    write_json(HEALTH_FILE, payload)
    write_json(STATE_FILE, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
