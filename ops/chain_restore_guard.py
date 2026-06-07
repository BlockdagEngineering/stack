#!/usr/bin/env python3
"""IPFS/raw-datadir restore-point freshness guard for BlockDAG chain data."""

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
RESTORE_ROOT = Path(os.environ.get("BDAG_RESTORE_GUARD_ROOT", str(PROJECT_ROOT / "data-restore")))
CONTENT_CURRENT = Path(
    os.environ.get(
        "BDAG_RESTORE_GUARD_CONTENT_CURRENT",
        str(RESTORE_ROOT / "rawdatadir-sidecar-content" / "current"),
    )
)
MIRROR_CURRENT = Path(
    os.environ.get(
        "BDAG_RESTORE_GUARD_MIRROR_CURRENT",
        str(RESTORE_ROOT / "rawdatadir" / "current"),
    )
)
DASHBOARD_URL = os.environ.get("BDAG_RESTORE_GUARD_STATUS_URL", "http://127.0.0.1:8088/api/status")
DASHBOARD_TIMEOUT = float(os.environ.get("BDAG_RESTORE_GUARD_STATUS_TIMEOUT", "20"))
MAX_PUBLISHED_AGE_SECONDS = int(os.environ.get("BDAG_RESTORE_POINT_MAX_AGE_SECONDS", str(6 * 3600)))
MAX_STAGE_AGE_SECONDS = int(os.environ.get("BDAG_RESTORE_STAGE_MAX_AGE_SECONDS", str(90 * 60)))
INCIDENT_COOLDOWN_SECONDS = int(os.environ.get("BDAG_RESTORE_GUARD_INCIDENT_COOLDOWN_SECONDS", "1800"))
RESTORE_REFRESH_COOLDOWN_SECONDS = int(os.environ.get("BDAG_RESTORE_GUARD_REFRESH_COOLDOWN_SECONDS", "3600"))
IPFS_CONTENT_UNIT = os.environ.get("BDAG_RESTORE_GUARD_IPFS_CONTENT_UNIT", "bdag-ipfs-content-sidecar.service")
RAWDATADIR_SIDECAR_UNIT = os.environ.get("BDAG_RESTORE_GUARD_RAWDATADIR_UNIT", "bdag-rawdatadir-sidecar.service")
REQUIRED_TIMERS = tuple(
    item.strip()
    for item in os.environ.get(
        "BDAG_RESTORE_GUARD_REQUIRED_TIMERS",
        "bdag-rawdatadir-sidecar.timer,bdag-rawdatadir-source.timer,"
        "bdag-ipfs-content-sidecar.timer,bdag-ipfs-segment-writer.timer",
    ).split(",")
    if item.strip()
)


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


def start_unit_no_block(unit: str) -> subprocess.CompletedProcess[str]:
    return systemctl_user("start", "--no-block", unit)


def status_api() -> tuple[dict[str, Any] | None, str]:
    try:
        with urllib.request.urlopen(DASHBOARD_URL, timeout=DASHBOARD_TIMEOUT) as response:
            return json.loads(response.read(4_000_000).decode("utf-8", "replace")), ""
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, str(exc)


def newest_file_mtime(path: Path) -> int | None:
    if not path.exists():
        return None
    newest = int(path.stat().st_mtime)
    for root, _, files in os.walk(path):
        for name in files:
            try:
                mtime = int((Path(root) / name).stat().st_mtime)
            except OSError:
                continue
            if mtime > newest:
                newest = mtime
    return newest


def published_restore_info(now: int) -> dict[str, Any]:
    if not CONTENT_CURRENT.exists():
        target = ""
        if CONTENT_CURRENT.is_symlink():
            try:
                target = os.readlink(CONTENT_CURRENT)
            except OSError:
                target = ""
        return {
            "exists": False,
            "path": str(CONTENT_CURRENT),
            "broken_symlink": bool(CONTENT_CURRENT.is_symlink()),
            "target": target,
        }
    resolved = CONTENT_CURRENT.resolve()
    manifest_path = resolved / "manifest.json"
    manifest = read_json(manifest_path)
    source_epoch = newest_file_mtime(resolved) or int(resolved.stat().st_mtime)
    return {
        "exists": True,
        "path": str(resolved),
        "age_seconds": max(0, now - source_epoch),
        "manifest_path": str(manifest_path),
        "manifest_exists": manifest_path.exists(),
        "manifest": manifest,
    }


def stage_info(now: int) -> dict[str, Any]:
    mtime = newest_file_mtime(MIRROR_CURRENT)
    return {
        "rawdatadir_mirror": {
            "exists": MIRROR_CURRENT.exists(),
            "path": str(MIRROR_CURRENT),
            "latest_file_epoch": mtime,
            "latest_file_age_seconds": max(0, now - mtime) if mtime else None,
        }
    }


def stack_is_safe_for_restore_refresh(status: dict[str, Any] | None) -> tuple[bool, str]:
    if not isinstance(status, dict):
        return False, "status API unavailable"
    failures = status.get("failures")
    if failures:
        return False, f"stack failures are present: {failures}"
    sync = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    if sync.get("status") != "synced":
        return False, f"sync status is {sync.get('status')}"
    nodes = status.get("nodes") if isinstance(status.get("nodes"), dict) else {}
    heights = [
        int(info.get("latest_block"))
        for info in nodes.values()
        if isinstance(info, dict) and info.get("latest_block") is not None
    ]
    if len(heights) >= 2 and max(heights) - min(heights) > 5:
        return False, f"node height gap is {max(heights) - min(heights)} blocks"
    return True, "stack synced and safe"


def maybe_start_restore_refresh(
    state: dict[str, Any],
    now: int,
    published: dict[str, Any],
    status: dict[str, Any] | None,
) -> dict[str, Any]:
    latest_age = published.get("age_seconds")
    if isinstance(latest_age, int) and latest_age <= MAX_PUBLISHED_AGE_SECONDS:
        return {"started": False, "reason": "published restore point is fresh"}
    safe, reason = stack_is_safe_for_restore_refresh(status)
    if not safe:
        return {"started": False, "reason": f"restore refresh not safe now: {reason}"}
    if unit_active(RAWDATADIR_SIDECAR_UNIT) or unit_active(IPFS_CONTENT_UNIT):
        return {"started": False, "reason": "restore refresh service already active"}
    last_start = int(state.get("last_restore_refresh_start_epoch", 0) or 0)
    if now - last_start < RESTORE_REFRESH_COOLDOWN_SECONDS:
        return {
            "started": False,
            "reason": f"restore refresh cooldown {RESTORE_REFRESH_COOLDOWN_SECONDS - (now - last_start)}s",
        }
    sidecar_result = start_unit_no_block(RAWDATADIR_SIDECAR_UNIT)
    content_result = start_unit_no_block(IPFS_CONTENT_UNIT)
    state["last_restore_refresh_start_epoch"] = now
    state["last_restore_refresh_start_at"] = now_iso()
    details = {
        "latest_age_seconds": latest_age,
        "rawdatadir_unit": RAWDATADIR_SIDECAR_UNIT,
        "rawdatadir_returncode": sidecar_result.returncode,
        "rawdatadir_stdout": sidecar_result.stdout.strip(),
        "rawdatadir_stderr": sidecar_result.stderr.strip(),
        "ipfs_content_unit": IPFS_CONTENT_UNIT,
        "ipfs_content_returncode": content_result.returncode,
        "ipfs_content_stdout": content_result.stdout.strip(),
        "ipfs_content_stderr": content_result.stderr.strip(),
    }
    if sidecar_result.returncode == 0 and content_result.returncode == 0:
        append_incident(
            "restore_guard_started_ipfs_refresh",
            "warning",
            "chain-restore-guard",
            "Restore guard started IPFS/raw-datadir refresh because the latest published restore point is stale",
            details,
        )
        log(f"started {RAWDATADIR_SIDECAR_UNIT} and {IPFS_CONTENT_UNIT} because restore point is stale")
        return {"started": True, "reason": "latest published restore point stale", **details}
    append_incident(
        "restore_guard_ipfs_refresh_start_failed",
        "critical",
        "chain-restore-guard",
        "Restore guard could not start IPFS/raw-datadir refresh services",
        details,
    )
    log(
        f"failed to start restore refresh services rawdatadir_rc={sidecar_result.returncode} "
        f"ipfs_content_rc={content_result.returncode}"
    )
    return {"started": False, "reason": "restore refresh start failed", **details}


def main() -> int:
    ensure_runtime()
    now = int(time.time())
    state = read_json(STATE_FILE)

    for timer in REQUIRED_TIMERS:
        if not unit_active(timer):
            result = start_unit(timer)
            if should_emit(state, f"{timer}_inactive", str(result.returncode), now):
                append_incident(
                    "restore_guard_started_timer" if result.returncode == 0 else "restore_guard_timer_start_failed",
                    "warning" if result.returncode == 0 else "critical",
                    "chain-restore-guard",
                    f"Restore guard {'started' if result.returncode == 0 else 'could not start'} {timer}",
                    {"timer": timer, "returncode": result.returncode, "stderr": result.stderr.strip()},
                )

    status, status_error = status_api()
    published = published_restore_info(now)
    stage = stage_info(now)
    action = maybe_start_restore_refresh(state, now, published, status)

    if published.get("broken_symlink") and should_emit(
        state, "published_restore_point_broken", str(published.get("target") or ""), now
    ):
        append_incident(
            "restore_point_broken",
            "critical",
            "chain-restore-guard",
            "Latest published chain restore point symlink is broken",
            {"published": published},
        )
        log(f"latest published restore symlink is broken: {published.get('path')} -> {published.get('target')}")

    stale_stage = {
        node: info
        for node, info in stage.items()
        if not isinstance(info.get("latest_file_age_seconds"), int)
        or int(info["latest_file_age_seconds"]) > MAX_STAGE_AGE_SECONDS
    }
    latest_age = published.get("age_seconds")
    if isinstance(latest_age, int) and latest_age > MAX_PUBLISHED_AGE_SECONDS:
        hours = round(latest_age / 3600, 2)
        if should_emit(state, "published_restore_point_stale", str(published.get("path") or ""), now):
            append_incident(
                "restore_point_stale",
                "critical",
                "chain-restore-guard",
                f"Latest published chain restore point is stale ({hours}h old)",
                {"published": published, "max_age_seconds": MAX_PUBLISHED_AGE_SECONDS},
            )
    if stale_stage and should_emit(state, "stage_stale", ",".join(sorted(stale_stage)), now):
        append_incident(
            "restore_stage_stale",
            "warning",
            "chain-restore-guard",
            "Warm chain restore stage is stale or missing for at least one node",
            {"stale_stage": stale_stage, "max_age_seconds": MAX_STAGE_AGE_SECONDS},
        )

    health = {
        "generated_at": now_iso(),
        "dashboard_status_ok": status is not None,
        "dashboard_status_error": status_error,
        "stack_overall": status.get("overall") if isinstance(status, dict) else None,
        "sync_progress": status.get("sync_progress") if isinstance(status, dict) else None,
        "published_restore_point": published,
        "stage": stage,
        "action": action,
        "thresholds": {
            "max_published_age_seconds": MAX_PUBLISHED_AGE_SECONDS,
            "max_stage_age_seconds": MAX_STAGE_AGE_SECONDS,
        },
    }
    write_json(HEALTH_FILE, health)
    state["updated_at"] = now_iso()
    state["last_health_file"] = str(HEALTH_FILE)
    write_json(STATE_FILE, state)
    log(
        "restore health checked: "
        f"published_age={published.get('age_seconds')} action={action.get('reason')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
