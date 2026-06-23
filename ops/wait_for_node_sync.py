#!/usr/bin/env python3
"""Wait for the primary node to finish syncing and print ETA updates."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


OPS_DIR = Path(__file__).resolve().parent
ROOT_DIR = OPS_DIR.parent
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


STATE_FILE = Path(os.environ.get("BDAG_SYNC_WAIT_STATE_FILE") or (OPS_DIR / "runtime" / "release-sync-wait-state.json"))
DEFAULT_INTERVAL_SECONDS = float(os.environ.get("BDAG_SYNC_WAIT_INTERVAL_SECONDS", "10"))


def safe_int(value: object) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def safe_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "estimating"
    total = max(0, int(round(float(seconds))))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    else:
        parts.append(f"{secs}s")
        return "".join(parts)
    if secs and not hours:
        parts.append(f"{secs}s")
    return "".join(parts)


def load_state() -> dict[str, Any]:
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_state(payload: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def choose_progress(payload: dict[str, Any]) -> dict[str, Any]:
    sync = payload.get("sync_progress")
    if not isinstance(sync, dict):
        return {}
    nodes = sync.get("nodes")
    if isinstance(nodes, dict) and nodes:
        for progress in nodes.values():
            if isinstance(progress, dict):
                return progress
    return sync


def describe_progress(progress: dict[str, Any], previous: dict[str, Any], now: float) -> tuple[str, dict[str, Any]]:
    status = str(progress.get("status") or "unknown").lower()
    current = safe_int(progress.get("current_block"))
    highest = safe_int(progress.get("highest_block"))
    remaining = safe_int(progress.get("remaining_blocks"))
    if remaining is None and current is not None and highest is not None and highest >= current:
        remaining = highest - current
    percent = safe_float(progress.get("percent"))
    if remaining is None:
        return f"waiting for sync status ({status})", {"status": status}
    if status == "synced" or remaining <= 0:
        return "sync complete", {"status": "synced", "remaining_blocks": 0, "percent": 100.0}

    prev_remaining = safe_int(previous.get("remaining_blocks"))
    prev_epoch = safe_float(previous.get("epoch"))
    rate = None
    if prev_remaining is not None and prev_epoch is not None:
        elapsed = now - prev_epoch
        if elapsed >= 5 and prev_remaining > remaining:
            rate = (prev_remaining - remaining) / elapsed
    eta_seconds = remaining / rate if rate and rate > 0 else None
    eta_text = fmt_duration(eta_seconds)
    percent_text = f"{percent:.2f}%" if percent is not None else "unknown%"
    message = f"syncing: {remaining:,} blocks remaining, ETA {eta_text}, {percent_text} complete"
    state = {
        "status": status,
        "remaining_blocks": remaining,
        "current_block": current,
        "highest_block": highest,
        "percent": percent,
        "epoch": now,
    }
    return message, state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS, help="poll interval in seconds")
    parser.add_argument("--timeout", type=float, default=0.0, help="maximum wait time in seconds; 0 waits forever")
    args = parser.parse_args(argv)

    start = time.time()
    previous: dict[str, Any] = {}
    try:
        STATE_FILE.unlink()
    except OSError:
        pass
    last_status = ""
    while True:
        try:
            status = pool_ops.collect_sync_progress()
        except Exception as exc:  # noqa: BLE001 - installers need a single status line, not a traceback.
            message = f"waiting for node RPC: {exc}"
            if message != last_status:
                print(message, flush=True)
                last_status = message
            if args.timeout and time.time() - start >= args.timeout:
                print("node sync wait timed out before RPC became available", flush=True)
                return 1
            time.sleep(max(1.0, args.interval))
            continue

        progress = choose_progress(status)
        message, state = describe_progress(progress, previous, time.time())
        if message != last_status:
            print(message, flush=True)
            last_status = message
        previous = state
        if state.get("status") == "synced":
            return 0
        if args.timeout and time.time() - start >= args.timeout:
            print("node sync wait timed out before completion", flush=True)
            return 1
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
