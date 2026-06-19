#!/usr/bin/env python3
"""Self-calibrate pool timing from live Prometheus deltas.

The loop intentionally tunes only runtime-admin knobs. Code and compose changes
are still deployed separately, but the calibration state survives restarts in
the runtime JSONL file so A/B windows can be compared without resetting totals.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any


METRICS_URL = os.environ.get("POOL_TIMING_METRICS_URL", "http://127.0.0.1:9090/metrics")
ADMIN_BASE = os.environ.get("POOL_TIMING_ADMIN_BASE", "http://127.0.0.1:9090")
JOB_STATE_URL = os.environ.get("POOL_TIMING_JOB_STATE_URL", "http://127.0.0.1:9090/health/job-state")
INTERVAL_SECONDS = int(os.environ.get("POOL_TIMING_INTERVAL_SECONDS", "300"))
DURATION_SECONDS = int(os.environ.get("POOL_TIMING_DURATION_SECONDS", str(7 * 60 * 60)))
TARGET_WASTE_RATIO = float(os.environ.get("POOL_TIMING_TARGET_WASTE_RATIO", "0.05"))
DEFAULT_STATE_PATH = Path(__file__).resolve().parent / "runtime" / "pool-timing-calibrator.jsonl"
STATE_PATH = Path(os.environ.get("POOL_TIMING_STATE_PATH", str(DEFAULT_STATE_PATH)))
DECISION_WINDOWS = max(1, int(os.environ.get("POOL_TIMING_DECISION_WINDOWS", "4")))
CHANGE_COOLDOWN_WINDOWS = max(0, int(os.environ.get("POOL_TIMING_CHANGE_COOLDOWN_WINDOWS", "2")))
TARGET_MARGIN_RATIO = max(0.0, float(os.environ.get("POOL_TIMING_TARGET_MARGIN_RATIO", "0.02")))

MIN_BLOCK_CANDIDATES = int(os.environ.get("POOL_TIMING_MIN_BLOCK_CANDIDATES", "12"))
MIN_AGE_MS = int(os.environ.get("POOL_TIMING_MIN_JOB_AGE_MS", "750"))
MAX_AGE_MS = int(os.environ.get("POOL_TIMING_MAX_JOB_AGE_MS", "8000"))
DEFAULT_AGE_MS = int(os.environ.get("POOL_TIMING_START_JOB_AGE_MS", "2500"))
MIN_TTL_MS = int(os.environ.get("POOL_TIMING_MIN_TEMPLATE_TTL_MS", "100"))
MAX_TTL_MS = int(os.environ.get("POOL_TIMING_MAX_TEMPLATE_TTL_MS", "1000"))
DEFAULT_TTL_MS = int(os.environ.get("POOL_TIMING_START_TEMPLATE_TTL_MS", "500"))
DEFAULT_ALLOW_MULTIPLE = os.environ.get("POOL_TIMING_START_ALLOW_MULTIPLE", "true").lower() not in {"0", "false", "no", "off"}

METRIC_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)$")
LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')

running = True


@dataclass
class Knobs:
    age_ms: int = DEFAULT_AGE_MS
    ttl_ms: int = DEFAULT_TTL_MS
    allow_multiple: bool = DEFAULT_ALLOW_MULTIPLE


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def fetch_text(url: str, timeout: float = 8.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def post_admin(path: str, timeout: float = 5.0) -> dict[str, Any]:
    req = urllib.request.Request(ADMIN_BASE + path, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}


def parse_labels(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    return {match.group(1): bytes(match.group(2), "utf-8").decode("unicode_escape") for match in LABEL_RE.finditer(raw)}


def parse_metrics(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    out: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = METRIC_RE.match(line)
        if not match:
            continue
        name, raw_labels, raw_value = match.groups()
        labels = tuple(sorted(parse_labels(raw_labels).items()))
        out[(name, labels)] = float(raw_value)
    return out


def labels_dict(labels: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return dict(labels)


def summarize(metrics: dict[tuple[str, tuple[tuple[str, str], ...]], float]) -> dict[str, Any]:
    block: dict[str, float] = {}
    shares_rejected: dict[str, float] = {}
    shares_accepted = 0.0
    active_connections = 0.0
    for (name, labels), value in metrics.items():
        data = labels_dict(labels)
        if name == "pool_block_submit_outcomes_total":
            key = f"{data.get('outcome', '')}:{data.get('reason', '')}"
            block[key] = block.get(key, 0.0) + value
        elif name == "pool_shares_rejected_total":
            reason = data.get("reason", "")
            shares_rejected[reason] = shares_rejected.get(reason, 0.0) + value
        elif name == "pool_shares_accepted_total":
            shares_accepted += value
        elif name == "pool_active_connections":
            active_connections += value
    return {
        "block": block,
        "shares_rejected": shares_rejected,
        "shares_accepted": shares_accepted,
        "active_connections": active_connections,
    }


def delta_dict(now: dict[str, float], before: dict[str, float]) -> dict[str, float]:
    return {key: max(0.0, now.get(key, 0.0) - before.get(key, 0.0)) for key in set(now) | set(before)}


def summarize_delta(now: dict[str, Any], before: dict[str, Any] | None) -> dict[str, Any]:
    if before is None:
        return {"ready": False, "reason": "baseline"}
    block = delta_dict(now["block"], before["block"])
    shares_rejected = delta_dict(now["shares_rejected"], before["shares_rejected"])
    shares_accepted = max(0.0, now["shares_accepted"] - before["shares_accepted"])
    accepted = block.get("accepted:ok", 0.0)
    total_block = sum(block.values())
    lost_block = max(0.0, total_block - accepted)
    total_shares = shares_accepted + sum(shares_rejected.values())
    return {
        "ready": True,
        "block": block,
        "shares_rejected": shares_rejected,
        "shares_accepted": shares_accepted,
        "block_total": total_block,
        "block_accepted": accepted,
        "block_lost": lost_block,
        "block_waste_ratio": lost_block / total_block if total_block > 0 else 0.0,
        "share_waste_ratio": sum(shares_rejected.values()) / total_shares if total_shares > 0 else 0.0,
        "tip_overdue_ratio": block.get("rejected:tip-overdue", 0.0) / total_block if total_block > 0 else 0.0,
        "node_syncing_ratio": block.get("rejected:node-syncing", 0.0) / total_block if total_block > 0 else 0.0,
        "expired_ratio": block.get("rejected-local:expired", 0.0) / total_block if total_block > 0 else 0.0,
        "stale_local_ratio": block.get("rejected-local:stale-job", 0.0) / total_block if total_block > 0 else 0.0,
        "stale_parent_ratio": block.get("rejected-local:stale-parent", 0.0) / total_block if total_block > 0 else 0.0,
        "duplicate_ratio": block.get("rejected-local:duplicate-block", 0.0) / total_block if total_block > 0 else 0.0,
        "invalidated_share_ratio": shares_rejected.get("invalidated_job", 0.0) / total_shares if total_shares > 0 else 0.0,
    }


def combine_deltas(windows: list[dict[str, Any]]) -> dict[str, Any]:
    block: dict[str, float] = {}
    shares_rejected: dict[str, float] = {}
    shares_accepted = 0.0

    for window in windows:
        for key, value in window.get("block", {}).items():
            block[key] = block.get(key, 0.0) + float(value)
        for key, value in window.get("shares_rejected", {}).items():
            shares_rejected[key] = shares_rejected.get(key, 0.0) + float(value)
        shares_accepted += float(window.get("shares_accepted", 0.0))

    accepted = block.get("accepted:ok", 0.0)
    total_block = sum(block.values())
    lost_block = max(0.0, total_block - accepted)
    total_shares = shares_accepted + sum(shares_rejected.values())
    return {
        "ready": bool(windows),
        "windows": len(windows),
        "block": block,
        "shares_rejected": shares_rejected,
        "shares_accepted": shares_accepted,
        "block_total": total_block,
        "block_accepted": accepted,
        "block_lost": lost_block,
        "block_waste_ratio": lost_block / total_block if total_block > 0 else 0.0,
        "share_waste_ratio": sum(shares_rejected.values()) / total_shares if total_shares > 0 else 0.0,
        "tip_overdue_ratio": block.get("rejected:tip-overdue", 0.0) / total_block if total_block > 0 else 0.0,
        "node_syncing_ratio": block.get("rejected:node-syncing", 0.0) / total_block if total_block > 0 else 0.0,
        "expired_ratio": block.get("rejected-local:expired", 0.0) / total_block if total_block > 0 else 0.0,
        "stale_local_ratio": block.get("rejected-local:stale-job", 0.0) / total_block if total_block > 0 else 0.0,
        "stale_parent_ratio": block.get("rejected-local:stale-parent", 0.0) / total_block if total_block > 0 else 0.0,
        "duplicate_ratio": block.get("rejected-local:duplicate-block", 0.0) / total_block if total_block > 0 else 0.0,
        "invalidated_share_ratio": shares_rejected.get("invalidated_job", 0.0) / total_shares if total_shares > 0 else 0.0,
    }


def fetch_job_state() -> dict[str, Any]:
    try:
        return json.loads(fetch_text(JOB_STATE_URL, timeout=4.0))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def apply_knobs(knobs: Knobs) -> dict[str, Any]:
    results: dict[str, Any] = {"target": knobs.__dict__.copy()}
    results["age"] = post_admin(f"/admin/block-candidate-job-age?ms={knobs.age_ms}")
    results["ttl"] = post_admin(f"/admin/template-ttl-refresh?ms={knobs.ttl_ms}")
    enabled = "true" if knobs.allow_multiple else "false"
    results["allow_multiple"] = post_admin(f"/admin/allow-multiple-block-candidates?enabled={enabled}")
    return results


def choose_next(knobs: Knobs, delta: dict[str, Any], job_state: dict[str, Any]) -> tuple[Knobs, str]:
    if not delta.get("ready"):
        return knobs, str(delta.get("reason", "baseline"))

    if delta.get("block_total", 0.0) < MIN_BLOCK_CANDIDATES:
        return knobs, "insufficient-block-candidates"

    next_knobs = Knobs(knobs.age_ms, knobs.ttl_ms, knobs.allow_multiple)
    waste = float(delta.get("block_waste_ratio", 0.0))
    tip = float(delta.get("tip_overdue_ratio", 0.0))
    expired = float(delta.get("expired_ratio", 0.0))
    stale = float(delta.get("stale_local_ratio", 0.0))
    stale_parent = float(delta.get("stale_parent_ratio", 0.0))
    duplicate = float(delta.get("duplicate_ratio", 0.0))
    node_sync = float(delta.get("node_syncing_ratio", 0.0))
    invalidated = float(delta.get("invalidated_share_ratio", 0.0))
    local_late = expired + stale + stale_parent

    if waste <= TARGET_WASTE_RATIO and invalidated <= TARGET_WASTE_RATIO:
        return next_knobs, "target-met"

    ready = int(job_state.get("ready_connections") or 0)
    active = int(job_state.get("active_connections") or 0)
    if active > 0 and ready == 0:
        next_knobs.ttl_ms = MIN_TTL_MS
        if local_late < 0.05:
            next_knobs.age_ms = clamp(next_knobs.age_ms - 250, MIN_AGE_MS, MAX_AGE_MS)
        return next_knobs, "no-ready-miners-tighten-refresh"

    if node_sync > 0.05:
        next_knobs.ttl_ms = MIN_TTL_MS
        if local_late > tip + 0.05:
            next_knobs.age_ms = clamp(next_knobs.age_ms + 250, MIN_AGE_MS, MAX_AGE_MS)
        return next_knobs, "node-syncing-fast-refresh"

    if duplicate > 0.08 and not next_knobs.allow_multiple and tip < 0.08 and node_sync < 0.05:
        next_knobs.allow_multiple = True
        return next_knobs, "ab-enable-multiple-candidates"

    if tip > 0.05 and local_late > 0.05:
        net_tip = tip - local_late
        next_knobs.ttl_ms = MIN_TTL_MS
        if net_tip > 0.04:
            next_knobs.age_ms = clamp(next_knobs.age_ms - 250, MIN_AGE_MS, MAX_AGE_MS)
            return next_knobs, "tip-expired-balance-tighten"
        if net_tip < -0.04:
            next_knobs.age_ms = clamp(next_knobs.age_ms + 250, MIN_AGE_MS, MAX_AGE_MS)
            return next_knobs, "tip-expired-balance-relax"
        return next_knobs, "tip-expired-balanced-hold"

    if tip > 0.05:
        next_knobs.ttl_ms = clamp(next_knobs.ttl_ms - 100, MIN_TTL_MS, MAX_TTL_MS)
        next_knobs.age_ms = clamp(next_knobs.age_ms - 250, MIN_AGE_MS, MAX_AGE_MS)
        return next_knobs, "tip-overdue-tighten"

    if local_late > 0.05 and tip < 0.03:
        next_knobs.age_ms = clamp(next_knobs.age_ms + 250, MIN_AGE_MS, MAX_AGE_MS)
        next_knobs.ttl_ms = MIN_TTL_MS
        return next_knobs, "local-late-relax-age"

    if stale > 0.05 or invalidated > 0.05:
        next_knobs.ttl_ms = clamp(next_knobs.ttl_ms - 100, MIN_TTL_MS, MAX_TTL_MS)
        return next_knobs, "stale-invalidated-tighten-refresh"

    return next_knobs, "hold"


def append_state(entry: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def handle_signal(signum: int, _frame: Any) -> None:
    global running
    running = False
    print(f"received signal {signum}; stopping after current window", flush=True)


def main() -> int:
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    knobs = Knobs()
    started = time.time()
    previous_summary: dict[str, Any] | None = None
    history: deque[dict[str, Any]] = deque(maxlen=DECISION_WINDOWS)
    cooldown_windows = 0
    apply_result = apply_knobs(knobs)
    append_state({"ts": time.time(), "event": "start", "knobs": knobs.__dict__, "apply": apply_result})
    print(json.dumps({"event": "start", "knobs": knobs.__dict__, "state_path": str(STATE_PATH)}), flush=True)

    while running and time.time() - started < DURATION_SECONDS:
        metrics = parse_metrics(fetch_text(METRICS_URL))
        summary = summarize(metrics)
        delta = summarize_delta(summary, previous_summary)
        job_state = fetch_job_state()

        decision_delta = delta
        if delta.get("ready") and delta.get("block_total", 0.0) > 0:
            history.append(delta)

        if not delta.get("ready"):
            next_knobs, reason = knobs, str(delta.get("reason", "baseline"))
        elif len(history) < DECISION_WINDOWS:
            next_knobs, reason = knobs, f"warming-history-{len(history)}/{DECISION_WINDOWS}"
        else:
            decision_delta = combine_deltas(list(history))
            waste = float(decision_delta.get("block_waste_ratio", 0.0))
            invalidated = float(decision_delta.get("invalidated_share_ratio", 0.0))
            if cooldown_windows > 0:
                next_knobs, reason = knobs, f"cooldown-{cooldown_windows}"
                cooldown_windows -= 1
            elif waste <= TARGET_WASTE_RATIO and invalidated <= TARGET_WASTE_RATIO:
                next_knobs, reason = knobs, "rolling-target-met"
            elif waste <= TARGET_WASTE_RATIO + TARGET_MARGIN_RATIO and invalidated <= TARGET_WASTE_RATIO + TARGET_MARGIN_RATIO:
                next_knobs, reason = knobs, "rolling-target-band"
            else:
                next_knobs, reason = choose_next(knobs, decision_delta, job_state)

        changed = next_knobs != knobs
        apply_result = apply_knobs(next_knobs) if changed else {}
        if changed:
            cooldown_windows = CHANGE_COOLDOWN_WINDOWS
            history.clear()
        entry = {
            "ts": time.time(),
            "event": "window",
            "reason": reason,
            "changed": changed,
            "knobs": next_knobs.__dict__,
            "delta": delta,
            "decision_delta": decision_delta,
            "decision_windows": len(history),
            "cooldown_windows": cooldown_windows,
            "job_state": {
                "status": job_state.get("status"),
                "active_connections": job_state.get("active_connections"),
                "authorized_connections": job_state.get("authorized_connections"),
                "ready_connections": job_state.get("ready_connections"),
                "reason_code": job_state.get("reason_code"),
            },
            "apply": apply_result,
        }
        append_state(entry)
        print(json.dumps(entry, sort_keys=True), flush=True)
        knobs = next_knobs
        previous_summary = summary
        sleep_until = time.time() + INTERVAL_SECONDS
        while running and time.time() < sleep_until and time.time() - started < DURATION_SECONDS:
            time.sleep(min(5, sleep_until - time.time()))

    append_state({"ts": time.time(), "event": "stop", "knobs": knobs.__dict__})
    print(json.dumps({"event": "stop", "knobs": knobs.__dict__}), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (urllib.error.URLError, TimeoutError) as exc:
        print(json.dumps({"event": "fatal", "error": str(exc)}), file=sys.stderr, flush=True)
        raise SystemExit(2)
