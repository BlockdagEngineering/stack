#!/usr/bin/env python3
"""Wait for the primary node to finish syncing and print gap updates."""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


OPS_DIR = Path(__file__).resolve().parent
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


STATE_FILE = Path(os.environ.get("BDAG_SYNC_WAIT_STATE_FILE") or (OPS_DIR / "runtime" / "release-sync-wait-state.json"))
DEFAULT_INTERVAL_SECONDS = float(os.environ.get("BDAG_SYNC_WAIT_INTERVAL_SECONDS", "10"))
LOG_CONTAINER = os.environ.get("BDAG_SYNC_WAIT_LOG_CONTAINER", "node")
LOG_LINES = int(os.environ.get("BDAG_SYNC_WAIT_LOG_LINES", "2000"))
EVM_RPC_URL = os.environ.get("BDAG_SYNC_WAIT_EVM_RPC_URL") or (
    f"http://127.0.0.1:{os.environ.get('EVM_HTTP_PORT') or os.environ.get('BDAG_NODE_EVM_RPC_PORT') or '18545'}"
)
EVM_RPC_TIMEOUT_SECONDS = float(os.environ.get("BDAG_SYNC_WAIT_EVM_RPC_TIMEOUT_SECONDS", "4"))
REQUIRE_ETH_SYNC = pool_ops.env_bool("BDAG_SYNC_WAIT_REQUIRE_ETH_SYNC", True)
SYNC_GRAPH_RE = re.compile(r"Syncing graph state.*?cur=\(([^)]*)\).*?target=\(([^)]*)\)")
STARTUP_STATE_RE = re.compile(
    r"Start to find cur block state.*?state\.order=(\d+).*?evm\.Number=(\d+).*?cur\.number=(\d+)"
)
PROCESSED_BLOCKS_RE = re.compile(r"Processed\s+([0-9,]+)\s+blocks\s+in\s+the\s+last\s+([0-9.]+)s")
SYNC_ENDED_RE = re.compile(r"The sync of graph state has ended")
NODE_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\|(\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?")
SYNC_HINT_RE = re.compile(
    r"(Syncing graph state ETA|Syncing graph state|Start to find cur block state|Imported new chain segment|The sync of graph state has ended)"
)


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
    if hours:
        if secs:
            return f"{hours}h{minutes}m{secs}s"
        return f"{hours}h{minutes}m"
    if minutes:
        if secs:
            return f"{minutes}m{secs}s"
        return f"{minutes}m"
    return f"{secs}s"


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


def parse_tuple(raw: str) -> tuple[int, ...]:
    values: list[int] = []
    for item in raw.split(","):
        value = safe_int(item.strip())
        if value is None:
            return ()
        values.append(value)
    return tuple(values)


def node_log_epoch(line: str | None) -> float | None:
    if not line:
        return None
    match = NODE_LOG_TS_RE.match(line)
    if not match:
        return None
    date_part, time_part, micros_part = match.groups()
    try:
        tm = time.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    seconds = time.mktime(tm)
    micros = int((micros_part or "0").ljust(6, "0")[:6])
    return seconds + micros / 1_000_000


def log_rate_from_text(text: str) -> float | None:
    matches = list(PROCESSED_BLOCKS_RE.finditer(text))
    if not matches:
        return None
    blocks = safe_float(matches[-1].group(1).replace(",", ""))
    seconds = safe_float(matches[-1].group(2))
    if blocks is None or seconds is None or seconds <= 0:
        return None
    return blocks / seconds


def log_snapshot() -> dict[str, Any]:
    try:
        text = pool_ops.docker_logs(LOG_CONTAINER, lines=LOG_LINES)
    except Exception as exc:  # noqa: BLE001 - installers need a single status line, not a traceback.
        return {
            "source": "logs",
            "status": "unknown",
            "error": str(exc),
            "current_block": None,
            "highest_block": None,
            "remaining_blocks": None,
            "processed_rate_blocks_per_second": None,
            "sync_ended": False,
        }

    lines = [line for line in text.splitlines() if line.strip()]
    parsed = pool_ops.parse_node_log(text)
    sync_line = ""
    for line in reversed(lines):
        if SYNC_GRAPH_RE.search(line) or STARTUP_STATE_RE.search(line):
            sync_line = line
            break

    snapshot: dict[str, Any] = {
        "source": "logs",
        "status": "unknown",
        "current_block": None,
        "highest_block": None,
        "remaining_blocks": None,
        "processed_rate_blocks_per_second": log_rate_from_text(text),
        "sync_ended": bool(SYNC_ENDED_RE.search(text)),
        "last_log_update_seconds": None,
    }
    if sync_line:
        match = SYNC_GRAPH_RE.search(sync_line)
        if match:
            current_tuple = parse_tuple(match.group(1))
            target_tuple = parse_tuple(match.group(2))
            current = current_tuple[0] if current_tuple else None
            highest = target_tuple[0] if target_tuple else None
            remaining = None
            if current is not None and highest is not None:
                remaining = max(0, highest - current)
            snapshot.update(
                {
                    "status": "synced" if remaining == 0 else "syncing",
                    "current_block": current,
                    "highest_block": highest,
                    "remaining_blocks": remaining,
                    "sync_line": sync_line,
                }
            )
        else:
            match = STARTUP_STATE_RE.search(sync_line)
            if match:
                highest = safe_int(match.group(1))
                evm_number = safe_int(match.group(2))
                current = safe_int(match.group(3))
                remaining = None
                if current is not None and highest is not None:
                    remaining = max(0, highest - current)
                snapshot.update(
                    {
                        "status": "synced" if remaining == 0 else "syncing",
                        "current_block": current,
                        "highest_block": highest,
                        "remaining_blocks": remaining,
                        "sync_line": sync_line,
                        "evm_number": evm_number,
                    }
                )
        snapshot["last_log_update_seconds"] = max(
            0,
            int(round(time.time() - node_log_epoch(sync_line)))
        ) if node_log_epoch(sync_line) is not None else None
    elif safe_int(parsed.get("last_import_age_seconds")) is not None:
        snapshot["last_log_update_seconds"] = safe_int(parsed.get("last_import_age_seconds"))
    return snapshot


def describe_progress(progress: dict[str, Any], previous: dict[str, Any], now: float) -> tuple[str, dict[str, Any]]:
    status = str(progress.get("status") or "unknown").lower()
    current = safe_int(progress.get("current_block"))
    highest = safe_int(progress.get("highest_block"))
    remaining = safe_int(progress.get("remaining_blocks"))
    if remaining is None and current is not None and highest is not None and highest >= current:
        remaining = highest - current
    rate = safe_float(progress.get("processed_rate_blocks_per_second"))
    log_age = safe_int(progress.get("last_log_update_seconds"))
    if rate is None:
        prev_remaining = safe_int(previous.get("remaining_blocks"))
        prev_epoch = safe_float(previous.get("epoch"))
        if prev_remaining is not None and prev_epoch is not None and remaining is not None:
            elapsed = now - prev_epoch
            if elapsed >= 5 and prev_remaining > remaining:
                rate = (prev_remaining - remaining) / elapsed

    if remaining is None:
        if status == "unknown":
            return "waiting for node logs", {"status": status, "epoch": now}
        return f"waiting for sync status ({status})", {"status": status, "epoch": now}
    if status == "synced" or remaining <= 0:
        return "sync complete", {"status": "synced", "remaining_blocks": 0, "epoch": now}

    eta_seconds = remaining / rate if rate and rate > 0 else None
    eta_text = fmt_duration(eta_seconds)
    stale_text = ""
    prev_remaining = safe_int(previous.get("remaining_blocks"))
    prev_current = safe_int(previous.get("current_block"))
    prev_highest = safe_int(previous.get("highest_block"))
    if (
        log_age is not None
        and log_age >= max(1.0, float(previous.get("poll_interval") or DEFAULT_INTERVAL_SECONDS))
        and prev_remaining == remaining
        and prev_current == current
        and prev_highest == highest
    ):
        stale_text = f", unchanged for {fmt_duration(log_age)}"
    if current is not None and highest is not None:
        message = f"syncing: gap {remaining:,} blocks ({current:,} -> {highest:,}), ETA {eta_text}{stale_text}"
    else:
        message = f"syncing: gap {remaining:,} blocks, ETA {eta_text}{stale_text}"
    state = {
        "status": status,
        "remaining_blocks": remaining,
        "current_block": current,
        "highest_block": highest,
        "epoch": now,
        "poll_interval": previous.get("poll_interval") or DEFAULT_INTERVAL_SECONDS,
    }
    return message, state


def eth_sync_progress(url: str, timeout: float) -> dict[str, Any]:
    details = pool_ops.eth_syncing_details(url, timeout=timeout)
    progress: dict[str, Any] = {
        "source": "eth_syncing",
        "status": "unknown",
        "current_block": None,
        "highest_block": None,
        "remaining_blocks": None,
        "rpc_url": url,
    }
    error = str(details.get("eth_syncing_error") or "")
    if error:
        progress["error"] = error
        return progress
    if details.get("eth_syncing") is False and details.get("chain_syncing") is False:
        progress.update({"status": "synced", "remaining_blocks": 0})
        return progress

    current = safe_int(details.get("sync_current_block"))
    highest = safe_int(details.get("sync_highest_block"))
    remaining = None
    if current is not None and highest is not None:
        remaining = max(0, highest - current)
    progress.update(
        {
            "status": "syncing" if details.get("chain_syncing") else "unknown",
            "current_block": current,
            "highest_block": highest,
            "remaining_blocks": remaining,
        }
    )
    return progress


def describe_eth_sync_progress(
    progress: dict[str, Any],
    previous: dict[str, Any],
    now: float,
) -> tuple[str, dict[str, Any]]:
    status = str(progress.get("status") or "unknown").lower()
    current = safe_int(progress.get("current_block"))
    highest = safe_int(progress.get("highest_block"))
    remaining = safe_int(progress.get("remaining_blocks"))
    if remaining is None and current is not None and highest is not None and highest >= current:
        remaining = highest - current
    error = str(progress.get("error") or "")
    state = {
        "status": status,
        "remaining_blocks": remaining,
        "current_block": current,
        "highest_block": highest,
        "epoch": now,
        "poll_interval": previous.get("poll_interval") or DEFAULT_INTERVAL_SECONDS,
    }

    if status == "synced":
        state["status"] = "synced"
        state["remaining_blocks"] = 0
        return "EVM import sync complete", state
    if error:
        return f"waiting for EVM RPC eth_syncing ({error})", state
    if remaining is None:
        return f"waiting for EVM import sync status ({status})", state
    if remaining <= 0:
        return "waiting for EVM import to finish reporting eth_syncing", state
    if current is not None and highest is not None:
        return f"EVM import syncing: gap {remaining:,} blocks ({current:,} -> {highest:,})", state
    return f"EVM import syncing: gap {remaining:,} blocks", state


def seconds_until_deadline(deadline: float | None) -> float:
    if deadline is None:
        return 0.0
    return max(0.001, deadline - time.time())


def wait_for_eth_sync(
    url: str,
    interval: float,
    rpc_timeout: float,
    deadline: float | None = None,
) -> int:
    previous: dict[str, Any] = {}
    while True:
        if deadline is not None and time.time() >= deadline:
            print("node sync wait timed out before EVM import completed", flush=True)
            return 1
        now = time.time()
        progress = eth_sync_progress(url, timeout=rpc_timeout)
        message, previous = describe_eth_sync_progress(progress, previous, now)
        print(message, flush=True)
        if previous.get("status") == "synced":
            return 0
        sleep_seconds = max(0.1, interval)
        if deadline is not None:
            sleep_seconds = min(sleep_seconds, max(0.0, deadline - time.time()))
            if sleep_seconds <= 0:
                continue
        time.sleep(sleep_seconds)


def stream_node_logs(timeout: float = 0.0) -> int:
    cmd = ["docker", "logs", "-f", "--tail", str(LOG_LINES), LOG_CONTAINER]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        print(f"waiting for node logs failed: {exc}", flush=True)
        return 1

    assert proc.stdout is not None
    start = time.time()
    seen_hint = False
    try:
        while True:
            if timeout and time.time() - start >= timeout:
                print("node sync wait timed out before completion", flush=True)
                return 1
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            line = line.rstrip("\n")
            if SYNC_HINT_RE.search(line):
                seen_hint = True
                print(line, flush=True)
                if SYNC_ENDED_RE.search(line):
                    return 0
                continue
            if not seen_hint:
                print("waiting for node logs", flush=True)
                seen_hint = True
        if proc.returncode not in (0, None):
            print("waiting for node logs ended unexpectedly", flush=True)
            return 1
        return 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS, help="poll interval in seconds")
    parser.add_argument("--timeout", type=float, default=0.0, help="maximum wait time in seconds; 0 waits forever")
    parser.add_argument("--evm-rpc-url", default=EVM_RPC_URL, help="EVM JSON-RPC URL used for eth_syncing")
    parser.add_argument(
        "--evm-rpc-timeout",
        type=float,
        default=EVM_RPC_TIMEOUT_SECONDS,
        help="EVM JSON-RPC timeout in seconds",
    )
    parser.add_argument("--skip-eth-sync", action="store_true", help="do not wait for eth_syncing to return false")
    args = parser.parse_args(argv)

    try:
        STATE_FILE.unlink()
    except OSError:
        pass

    deadline = time.time() + args.timeout if args.timeout and args.timeout > 0 else None
    result = stream_node_logs(timeout=seconds_until_deadline(deadline))
    if result != 0:
        return result
    if args.skip_eth_sync or not REQUIRE_ETH_SYNC:
        return 0
    return wait_for_eth_sync(
        args.evm_rpc_url,
        interval=args.interval,
        rpc_timeout=args.evm_rpc_timeout,
        deadline=deadline,
    )


if __name__ == "__main__":
    raise SystemExit(main())
