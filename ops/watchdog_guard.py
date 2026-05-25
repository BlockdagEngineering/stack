#!/usr/bin/env python3
"""Lightweight guard for the BlockDAG watchdog and plot sampler."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from incident_journal import append_incident
from pool_ops import (
    EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS,
    LOG_DIR,
    RUNTIME_DIR,
    ensure_runtime,
    now_iso,
    read_latest_earnings_snapshot_info,
)


WATCHDOG_SERVICE = os.environ.get("BDAG_WATCHDOG_SERVICE", "bdag-watchdog.service")
STATE_FILE = RUNTIME_DIR / "watchdog-guard-state.json"
LOG_FILE = LOG_DIR / "watchdog-guard.log"
INCIDENT_COOLDOWN_SECONDS = int(os.environ.get("BDAG_WATCHDOG_GUARD_INCIDENT_COOLDOWN_SECONDS", "300"))
STALE_THRESHOLD_SECONDS = int(
    os.environ.get(
        "BDAG_EARNINGS_SAMPLER_STALE_SECONDS",
        str(EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS * 3),
    )
)
TAIL_BYTES = int(os.environ.get("BDAG_WATCHDOG_GUARD_EARNINGS_TAIL_BYTES", str(2 * 1024 * 1024)))


def log(message: str) -> None:
    ensure_runtime()
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message}\n")


def read_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict[str, Any]) -> None:
    ensure_runtime()
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def systemctl_user(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def service_is_active() -> tuple[bool, str]:
    result = systemctl_user("is-active", WATCHDOG_SERVICE)
    state = (result.stdout or result.stderr or "").strip()
    return result.returncode == 0 and state == "active", state or f"exit-{result.returncode}"


def should_emit(state: dict[str, Any], key: str, signature: str, now: int) -> bool:
    last_signature = str(state.get(f"{key}_signature") or "")
    last_epoch = int(state.get(f"{key}_epoch", 0) or 0)
    if signature == last_signature and now - last_epoch < INCIDENT_COOLDOWN_SECONDS:
        return False
    state[f"{key}_signature"] = signature
    state[f"{key}_epoch"] = now
    state[f"{key}_at"] = now_iso()
    return True


def check_sampler(state: dict[str, Any], now: int) -> dict[str, Any]:
    info = read_latest_earnings_snapshot_info(max_tail_bytes=TAIL_BYTES)
    latest_epoch = info.get("latest_epoch")
    latest_age = int(now - float(latest_epoch)) if latest_epoch is not None else None
    stale = latest_age is None or latest_age > STALE_THRESHOLD_SECONDS
    details = {
        "snapshot_path": info.get("path"),
        "latest_snapshot_at": info.get("latest_at"),
        "latest_snapshot_age_seconds": latest_age,
        "expected_interval_seconds": EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS,
        "stale_threshold_seconds": STALE_THRESHOLD_SECONDS,
        "latest_any_snapshot_at": info.get("latest_any_at"),
        "tail_lines_scanned": info.get("tail_lines_scanned"),
        "file_size_bytes": info.get("file_size_bytes"),
        "error": info.get("error"),
    }
    state["sampler_latest_snapshot_at"] = info.get("latest_at")
    state["sampler_latest_snapshot_age_seconds"] = latest_age
    state["sampler_stale"] = stale
    state["sampler_checked_at"] = now_iso()
    if stale:
        signature = str(info.get("latest_at") or info.get("latest_any_at") or "missing")
        if should_emit(state, "earnings_sampler_stale", signature, now):
            message = "Earnings/miner plot sampler is stale"
            if latest_age is not None:
                message = f"{message}: no valid snapshot for {latest_age}s"
            append_incident("earnings_sampler_stale", "warning", "watchdog-guard", message, details)
            log(f"{message}; latest={info.get('latest_at') or 'missing'}")
    elif state.get("sampler_was_stale"):
        append_incident(
            "earnings_sampler_recovered",
            "info",
            "watchdog-guard",
            "Earnings/miner plot sampler recovered",
            details,
        )
        log(f"earnings/miner plot sampler recovered; latest={info.get('latest_at')}")
    state["sampler_was_stale"] = stale
    return details


def check_watchdog_service(state: dict[str, Any], now: int) -> None:
    active, service_state = service_is_active()
    state["watchdog_service_state"] = service_state
    state["watchdog_service_checked_at"] = now_iso()
    if active:
        return

    details = {"service": WATCHDOG_SERVICE, "service_state": service_state}
    if should_emit(state, "watchdog_inactive", service_state, now):
        append_incident(
            "watchdog_inactive",
            "critical",
            "watchdog-guard",
            f"{WATCHDOG_SERVICE} is inactive; starting it to restore repair monitoring and plot sampling",
            details,
        )
    log(f"{WATCHDOG_SERVICE} inactive state={service_state}; attempting start")
    start = systemctl_user("start", WATCHDOG_SERVICE)
    details.update(
        {
            "start_returncode": start.returncode,
            "start_stdout": start.stdout.strip(),
            "start_stderr": start.stderr.strip(),
        }
    )
    if start.returncode == 0:
        append_incident(
            "watchdog_guard_started_watchdog",
            "info",
            "watchdog-guard",
            f"{WATCHDOG_SERVICE} was started by watchdog guard",
            details,
        )
        log(f"{WATCHDOG_SERVICE} start ok")
    else:
        append_incident(
            "watchdog_guard_start_failed",
            "critical",
            "watchdog-guard",
            f"{WATCHDOG_SERVICE} could not be started by watchdog guard",
            details,
        )
        log(f"{WATCHDOG_SERVICE} start failed rc={start.returncode} stderr={start.stderr.strip()}")


def main() -> int:
    ensure_runtime()
    now = int(time.time())
    state = read_state()
    check_sampler(state, now)
    check_watchdog_service(state, now)
    state["updated_at"] = now_iso()
    write_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
