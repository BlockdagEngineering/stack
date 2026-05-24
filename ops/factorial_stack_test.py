#!/usr/bin/env python3
"""Long-running factorial stack experiment for the BlockDAG mining pool.

This runner is intentionally conservative: it only changes the pool/node image
set and HAProxy RPC primary, records each phase, and keeps the watchdog online.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import datetime as dt
import fcntl
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from pool_ops import RUNTIME_DIR, collect_status, now_iso
from rpc_router import current_rpc_primary
from stack_ab_test import (
    LABELS,
    LOCAL_MINER,
    NEW_NODE_IMAGE,
    NEW_POOL_IMAGE,
    OLD_NODE_IMAGE,
    OLD_POOL_IMAGE,
    current_stack_name,
    iso,
    rpc,
    switch_stack,
    block_number,
    block_timestamp,
)
from watchdog import run_rpc_failover_switch


ROOT = Path(__file__).resolve().parents[1]
POOL_ENV = ROOT / "asic-pool" / ".env"
ROOT_ENV = ROOT / ".env"
LOCK_FILE = RUNTIME_DIR / "factorial-stack-test.lock"
NODES = ("bdag-miner-node-1", "bdag-miner-node-2")
LOG_PATTERNS = {
    "valid_shares": "valid share accepted",
    "submit_ok": "Block submitted successfully",
    "too_late": "Block submission too late",
    "submit_errors": "submit error",
    "block_submit_errors": "block submit error",
    "template_errors": "template fetch error",
    "gbt_errors_lower": "getBlockTemplate",
    "stale_jobs": "stale job",
    "stale_jobs_upper": "STALE JOB",
    "duplicates": "duplicate",
    "overdue": "overdue",
}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def run(cmd: list[str], *, check: bool = True, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
        timeout=timeout,
    )


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def log(run_dir: Path, message: str) -> None:
    line = f"{now_iso()} {message}"
    print(line, flush=True)
    with (run_dir / "runner.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def other_node(node: str) -> str:
    return "bdag-miner-node-2" if node == "bdag-miner-node-1" else "bdag-miner-node-1"


def normalize_stack(stack: str | None) -> str:
    return stack if stack in {"old", "new"} else "new"


def normalize_primary(primary: str | None) -> str:
    return primary if primary in NODES else "bdag-miner-node-1"


def phase_plan(phase_count: int, start_stack: str, start_primary: str) -> list[dict[str, Any]]:
    stack_order = [normalize_stack(start_stack), "old" if normalize_stack(start_stack) == "new" else "new"]
    primary_order = [normalize_primary(start_primary), other_node(normalize_primary(start_primary))]
    block = [
        {"stack": stack_order[0], "target_primary": primary_order[0]},
        {"stack": stack_order[1], "target_primary": primary_order[0]},
        {"stack": stack_order[0], "target_primary": primary_order[1]},
        {"stack": stack_order[1], "target_primary": primary_order[1]},
    ]
    return [{**block[index % len(block)], "phase": index + 1} for index in range(phase_count)]


def node_rpc_url(node: str) -> str:
    ip = run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", node]).stdout.strip()
    if not ip:
        raise RuntimeError(f"{node} has no container IP")
    return f"http://{ip}:18545"


def rpc_sources() -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    for node in NODES:
        try:
            sources.append((node, node_rpc_url(node)))
        except Exception:
            continue
    if not sources:
        raise RuntimeError("no node RPC sources available")
    return sources


def rpc_any(method: str, params: list[Any], sources: list[tuple[str, str]]) -> tuple[str, Any]:
    last_error: Exception | None = None
    for name, url in sources:
        try:
            return name, rpc(method, params, url)
        except Exception as exc:  # noqa: BLE001 - try the other node.
            last_error = exc
    raise RuntimeError(str(last_error or "all RPC sources failed"))


def find_height_at_or_after(target_ts: int, sources: list[tuple[str, str]]) -> int:
    _, latest_block = rpc_any("eth_getBlockByNumber", ["latest", False], sources)
    latest = block_number(latest_block)
    lo, hi = 0, latest
    while lo < hi:
        mid = (lo + hi) // 2
        _, block = rpc_any("eth_getBlockByNumber", [hex(mid), False], sources)
        if block_timestamp(block) < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def find_height_at_or_before(target_ts: int, sources: list[tuple[str, str]]) -> int:
    _, latest_block = rpc_any("eth_getBlockByNumber", ["latest", False], sources)
    latest = block_number(latest_block)
    lo, hi = 0, latest
    while lo < hi:
        mid = (lo + hi + 1) // 2
        _, block = rpc_any("eth_getBlockByNumber", [hex(mid), False], sources)
        if block_timestamp(block) <= target_ts:
            lo = mid
        else:
            hi = mid - 1
    return lo


def db_block_counts(start_ts: int, end_ts: int) -> dict[str, Any]:
    start = dt.datetime.fromtimestamp(start_ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end = dt.datetime.fromtimestamp(end_ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = f"""
    WITH status_rows AS (
      SELECT status, count(*)::int AS status_count
      FROM blocks
      WHERE created_at >= '{start}'::timestamp AND created_at < '{end}'::timestamp
      GROUP BY status
    )
    SELECT json_build_object(
      'total', COALESCE((SELECT sum(status_count)::int FROM status_rows), 0),
      'valid', COALESCE((SELECT sum(status_count)::int FROM status_rows WHERE status IN ('PENDING','MATURE')), 0),
      'status_counts', COALESCE((SELECT json_object_agg(status, status_count) FROM status_rows), '{{}}'::json)
    );
    """
    out = run(["docker", "exec", "pool-db", "psql", "-U", "test", "-d", "pool", "-Atc", sql], timeout=30).stdout.strip()
    return json.loads(out or "{}")


def log_counts(start_iso: str, end_iso: str) -> dict[str, int]:
    proc = run(["docker", "logs", "--since", start_iso, "--until", end_iso, "asic-pool"], check=False, timeout=90)
    text = proc.stdout or ""
    return {key: text.count(pattern) for key, pattern in LOG_PATTERNS.items()}


def fetch_status() -> dict[str, Any]:
    try:
        return collect_status()
    except Exception as exc:  # noqa: BLE001
        return {"overall": "unknown", "error": str(exc)}


def fetch_router() -> dict[str, Any]:
    path = RUNTIME_DIR / "rpc-router-state.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def snapshot_context() -> dict[str, Any]:
    status = fetch_status()
    router = fetch_router()
    pool = status.get("pool") if isinstance(status.get("pool"), dict) else {}
    miner_health = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}
    sync_health = status.get("sync_health") if isinstance(status.get("sync_health"), dict) else {}
    return {
        "generated_at": now_iso(),
        "stack": current_stack_name(),
        "rpc_primary": current_rpc_primary(),
        "status_overall": status.get("overall"),
        "status_reason": status.get("status_reason"),
        "connected_miners": miner_health.get("connected_count"),
        "node_lag": sync_health.get("block_lag"),
        "pool": {
            key: pool.get(key)
            for key in [
                "valid_share_count",
                "block_submit_success_count",
                "block_submit_error_count",
                "stale_job_candidate_count",
                "tip_overdue_count",
                "gbt_errors",
                "last_valid_share_age_seconds",
                "last_block_submit_age_seconds",
            ]
        },
        "router": {
            "current_primary": router.get("current_primary"),
            "recommended_primary": router.get("recommended_primary"),
            "should_switch": router.get("should_switch"),
            "reason": router.get("reason"),
            "score_delta": router.get("score_delta"),
            "pool_pressure": router.get("pool_pressure"),
            "scores": router.get("scores"),
        },
    }


def scan_window(phase: dict[str, Any], measured_start_ts: int, measured_end_ts: int) -> dict[str, Any]:
    sources = rpc_sources()
    start_height = find_height_at_or_after(measured_start_ts, sources)
    end_height = find_height_at_or_before(measured_end_ts, sources)
    heights = list(range(start_height, end_height + 1)) if end_height >= start_height else []
    miners: collections.Counter[str] = collections.Counter()
    difficulties: collections.Counter[str] = collections.Counter()
    source_counts: collections.Counter[str] = collections.Counter()
    errors: list[str] = []

    def load(height: int) -> tuple[str, dict[str, Any]]:
        return rpc_any("eth_getBlockByNumber", [hex(height), False], sources)

    scan_started = time.time()
    if heights:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(heights))) as pool:
            future_map = {pool.submit(load, height): height for height in heights}
            for future in concurrent.futures.as_completed(future_map):
                height = future_map[future]
                try:
                    source, block = future.result()
                    timestamp = block_timestamp(block)
                    if measured_start_ts <= timestamp < measured_end_ts:
                        miners[(block.get("miner") or "").lower()] += 1
                        difficulties[block.get("difficulty") or ""] += 1
                        source_counts[source] += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{height}: {exc}")

    seconds = max(1, measured_end_ts - measured_start_ts)
    duration_hours = seconds / 3600
    chain_blocks = sum(miners.values())
    local_blocks = miners[LOCAL_MINER]
    db_counts = db_block_counts(measured_start_ts, measured_end_ts)
    logs = log_counts(iso(measured_start_ts), iso(measured_end_ts))
    submit_ok = int(logs.get("submit_ok") or 0)
    submit_errors = int(logs.get("submit_errors") or 0) + int(logs.get("block_submit_errors") or 0)
    too_late = int(logs.get("too_late") or 0) + int(logs.get("overdue") or 0)
    stale_jobs = int(logs.get("stale_jobs") or 0) + int(logs.get("stale_jobs_upper") or 0)
    template_errors = int(logs.get("template_errors") or 0) + int(logs.get("gbt_errors_lower") or 0)
    duplicates = int(logs.get("duplicates") or 0)

    return {
        **phase,
        "measured_start_utc": iso(measured_start_ts),
        "measured_end_utc": iso(measured_end_ts),
        "measured_seconds": seconds,
        "start_height": start_height,
        "end_height": end_height,
        "scanned_heights": len(heights),
        "scan_seconds": round(time.time() - scan_started, 3),
        "rpc_sources_used": dict(source_counts),
        "chain_blocks": chain_blocks,
        "chain_blocks_per_hour": round(chain_blocks / duration_hours, 3) if duration_hours else None,
        "local_blocks": local_blocks,
        "local_blocks_per_hour": round(local_blocks / duration_hours, 3) if duration_hours else None,
        "local_chain_share_pct": round(local_blocks * 100 / chain_blocks, 4) if chain_blocks else None,
        "db_blocks": int(db_counts.get("valid") or 0),
        "db_total_blocks": int(db_counts.get("total") or 0),
        "db_status_counts": db_counts.get("status_counts") or {},
        "db_blocks_per_hour": round(int(db_counts.get("valid") or 0) / duration_hours, 3) if duration_hours else None,
        "db_share_of_chain_pct": round(int(db_counts.get("valid") or 0) * 100 / chain_blocks, 4) if chain_blocks else None,
        "unique_miners": len(miners),
        "difficulty_values": dict(difficulties.most_common()),
        "top_miners": [
            {
                "address": address,
                "label": LABELS.get(address, ""),
                "blocks": count,
                "share_pct": round(count * 100 / chain_blocks, 4) if chain_blocks else None,
            }
            for address, count in miners.most_common(12)
        ],
        "log_counts": logs,
        "derived": {
            "valid_shares_per_min": round(int(logs.get("valid_shares") or 0) * 60 / seconds, 4),
            "submit_ok_per_min": round(submit_ok * 60 / seconds, 4),
            "submit_errors_per_min": round(submit_errors * 60 / seconds, 4),
            "too_late_per_min": round(too_late * 60 / seconds, 4),
            "stale_jobs_per_min": round(stale_jobs * 60 / seconds, 4),
            "template_errors_per_min": round(template_errors * 60 / seconds, 4),
            "duplicates_per_min": round(duplicates * 60 / seconds, 4),
            "submit_errors_per_ok": round(submit_errors / max(1, submit_ok), 4),
            "too_late_per_ok": round(too_late / max(1, submit_ok), 4),
            "stale_jobs_per_ok": round(stale_jobs / max(1, submit_ok), 4),
            "template_errors_per_ok": round(template_errors / max(1, submit_ok), 4),
            "duplicates_per_ok": round(duplicates / max(1, submit_ok), 4),
        },
        "scan_errors": errors[:20],
        "scan_error_count": len(errors),
        "status_at_end": snapshot_context(),
    }


def mean(values: list[float | int | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return round(sum(clean) / len(clean), 6) if clean else None


def stdev(values: list[float | int | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return round(statistics.stdev(clean), 6) if len(clean) > 1 else None


def grouped_stats(rows: list[dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        groups[tuple(row.get(key) for key in keys)].append(row)
    out: list[dict[str, Any]] = []
    for group_key, items in sorted(groups.items(), key=lambda item: str(item[0])):
        record = {key: value for key, value in zip(keys, group_key, strict=True)}
        record.update(
            {
                "phase_count": len(items),
                "avg_local_chain_share_pct": mean([item.get("local_chain_share_pct") for item in items]),
                "std_local_chain_share_pct": stdev([item.get("local_chain_share_pct") for item in items]),
                "avg_local_blocks_per_hour": mean([item.get("local_blocks_per_hour") for item in items]),
                "avg_db_blocks_per_hour": mean([item.get("db_blocks_per_hour") for item in items]),
                "avg_chain_blocks_per_hour": mean([item.get("chain_blocks_per_hour") for item in items]),
                "avg_submit_errors_per_ok": mean([(item.get("derived") or {}).get("submit_errors_per_ok") for item in items]),
                "avg_too_late_per_ok": mean([(item.get("derived") or {}).get("too_late_per_ok") for item in items]),
                "avg_stale_jobs_per_ok": mean([(item.get("derived") or {}).get("stale_jobs_per_ok") for item in items]),
                "avg_template_errors_per_ok": mean([(item.get("derived") or {}).get("template_errors_per_ok") for item in items]),
                "avg_duplicates_per_ok": mean([(item.get("derived") or {}).get("duplicates_per_ok") for item in items]),
                "total_local_blocks": sum(int(item.get("local_blocks") or 0) for item in items),
                "total_db_blocks": sum(int(item.get("db_blocks") or 0) for item in items),
                "confounded_phase_count": sum(1 for item in items if item.get("target_primary") != item.get("actual_primary_end")),
            }
        )
        out.append(record)
    return out


def solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    n = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-12:
            return None
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        divisor = augmented[col][col]
        augmented[col] = [value / divisor for value in augmented[col]]
        for row in range(n):
            if row == col:
                continue
            factor = augmented[row][col]
            augmented[row] = [value - factor * augmented[col][idx] for idx, value in enumerate(augmented[row])]
    return [augmented[row][-1] for row in range(n)]


def ols(rows: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    usable = [row for row in rows if row.get(metric) is not None and row.get("chain_blocks_per_hour") is not None]
    if len(usable) < 5:
        return {"metric": metric, "status": "insufficient-data", "rows": len(usable)}
    chain_mean = mean([row.get("chain_blocks_per_hour") for row in usable]) or 0.0
    x_rows: list[list[float]] = []
    y_values: list[float] = []
    for row in usable:
        stack_new = 1.0 if row.get("stack") == "new" else 0.0
        primary_node2 = 1.0 if row.get("target_primary") == "bdag-miner-node-2" else 0.0
        chain_centered = float(row.get("chain_blocks_per_hour") or 0.0) - chain_mean
        x_rows.append([1.0, stack_new, primary_node2, stack_new * primary_node2, chain_centered])
        y_values.append(float(row[metric]))
    n_cols = len(x_rows[0])
    xtx = [[0.0 for _ in range(n_cols)] for _ in range(n_cols)]
    xty = [0.0 for _ in range(n_cols)]
    for row, y_value in zip(x_rows, y_values, strict=True):
        for i in range(n_cols):
            xty[i] += row[i] * y_value
            for j in range(n_cols):
                xtx[i][j] += row[i] * row[j]
    coeffs = solve_linear_system(xtx, xty)
    if coeffs is None:
        return {"metric": metric, "status": "singular", "rows": len(usable)}
    labels = ["intercept_old_node1", "stack_new_effect", "node2_primary_effect", "new_x_node2_interaction", "chain_bph_covariate"]
    predictions = [sum(value * coeffs[index] for index, value in enumerate(row)) for row in x_rows]
    residuals = [y_value - prediction for y_value, prediction in zip(y_values, predictions, strict=True)]
    ss_res = sum(value * value for value in residuals)
    y_mean = sum(y_values) / len(y_values)
    ss_tot = sum((value - y_mean) ** 2 for value in y_values)
    return {
        "metric": metric,
        "status": "ok",
        "rows": len(usable),
        "chain_blocks_per_hour_mean": chain_mean,
        "coefficients": {label: round(coeffs[index], 6) for index, label in enumerate(labels)},
        "r_squared": round(1 - ss_res / ss_tot, 6) if ss_tot else None,
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    complete_rows = [row for row in rows if row.get("status") == "measured"]
    combo = grouped_stats(complete_rows, ["stack", "target_primary"])
    stack = grouped_stats(complete_rows, ["stack"])
    primary = grouped_stats(complete_rows, ["target_primary"])
    best = sorted(
        combo,
        key=lambda row: (
            row.get("avg_local_chain_share_pct") is not None,
            row.get("avg_local_chain_share_pct") or -1,
            row.get("avg_db_blocks_per_hour") or -1,
        ),
        reverse=True,
    )
    return {
        "generated_at": now_iso(),
        "phase_count": len(rows),
        "measured_phase_count": len(complete_rows),
        "by_combo": combo,
        "by_stack": stack,
        "by_target_primary": primary,
        "best_combo_so_far": best[0] if best else None,
        "models": [
            ols(complete_rows, "local_chain_share_pct"),
            ols(complete_rows, "local_blocks_per_hour"),
            ols(complete_rows, "db_blocks_per_hour"),
        ],
    }


def fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}{suffix}"
    return f"{value}{suffix}"


def render_report(run_dir: Path, rows: list[dict[str, Any]], summary: dict[str, Any], complete: bool) -> str:
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    lines = [
        "# Fifteen-Hour Factorial Stack Experiment",
        "",
        f"Generated: {now_iso()}",
        f"Complete: `{str(complete).lower()}`",
        f"Run directory: `{run_dir}`",
        "",
        "## Design",
        "",
        f"- Duration target: `{config['total_hours']}` hours.",
        f"- Phase length: `{config['phase_minutes']}` minutes.",
        f"- Warmup excluded from each phase: `{config['warmup_seconds']}` seconds.",
        "- Factors: stack image set (`old` rollback vs `new` WebSocket) and HAProxy RPC primary (`node1` vs `node2`).",
        "- The watchdog stays enabled. If it overrides a target primary for health reasons, the phase is marked as confounded.",
        "- Primary metrics: local on-chain share, local blocks/hour, DB blocks/hour, submit/stale/template error ratios.",
        "",
        "## Current Summary",
        "",
        "| Stack | Target Primary | Phases | Avg Share | Std Share | Local b/h | DB b/h | Chain b/h | Submit Err/OK | Late/OK | Stale/OK | Template/OK | Confounded |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary.get("by_combo") or []:
        lines.append(
            "| "
            f"{row.get('stack')} | "
            f"{row.get('target_primary')} | "
            f"{row.get('phase_count')} | "
            f"{fmt(row.get('avg_local_chain_share_pct'), '%')} | "
            f"{fmt(row.get('std_local_chain_share_pct'))} | "
            f"{fmt(row.get('avg_local_blocks_per_hour'))} | "
            f"{fmt(row.get('avg_db_blocks_per_hour'))} | "
            f"{fmt(row.get('avg_chain_blocks_per_hour'))} | "
            f"{fmt(row.get('avg_submit_errors_per_ok'))} | "
            f"{fmt(row.get('avg_too_late_per_ok'))} | "
            f"{fmt(row.get('avg_stale_jobs_per_ok'))} | "
            f"{fmt(row.get('avg_template_errors_per_ok'))} | "
            f"{row.get('confounded_phase_count')} |"
        )
    lines.extend(
        [
            "",
            "## Main Effects",
            "",
            "| Factor | Level | Phases | Avg Share | Local b/h | DB b/h | Chain b/h | Template/OK | Late/OK |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in summary.get("by_stack") or []:
        lines.append(
            f"| stack | {row.get('stack')} | {row.get('phase_count')} | {fmt(row.get('avg_local_chain_share_pct'), '%')} | "
            f"{fmt(row.get('avg_local_blocks_per_hour'))} | {fmt(row.get('avg_db_blocks_per_hour'))} | "
            f"{fmt(row.get('avg_chain_blocks_per_hour'))} | {fmt(row.get('avg_template_errors_per_ok'))} | {fmt(row.get('avg_too_late_per_ok'))} |"
        )
    for row in summary.get("by_target_primary") or []:
        lines.append(
            f"| target_primary | {row.get('target_primary')} | {row.get('phase_count')} | {fmt(row.get('avg_local_chain_share_pct'), '%')} | "
            f"{fmt(row.get('avg_local_blocks_per_hour'))} | {fmt(row.get('avg_db_blocks_per_hour'))} | "
            f"{fmt(row.get('avg_chain_blocks_per_hour'))} | {fmt(row.get('avg_template_errors_per_ok'))} | {fmt(row.get('avg_too_late_per_ok'))} |"
        )
    lines.extend(["", "## Linear Model"])
    for model in summary.get("models") or []:
        lines.append("")
        lines.append(f"### {model.get('metric')}")
        if model.get("status") != "ok":
            lines.append(f"- Status: `{model.get('status')}`, rows `{model.get('rows')}`.")
            continue
        coefficients = model.get("coefficients") or {}
        lines.append(f"- Rows: `{model.get('rows')}`, R^2: `{model.get('r_squared')}`.")
        lines.append(f"- New WebSocket effect: `{coefficients.get('stack_new_effect')}`.")
        lines.append(f"- Node2 primary effect: `{coefficients.get('node2_primary_effect')}`.")
        lines.append(f"- New x node2 interaction: `{coefficients.get('new_x_node2_interaction')}`.")
        lines.append(f"- Chain b/h covariate: `{coefficients.get('chain_bph_covariate')}`.")
    best = summary.get("best_combo_so_far") or {}
    if best:
        lines.extend(
            [
                "",
                "## Best So Far",
                "",
                f"- Stack: `{best.get('stack')}`",
                f"- Target primary: `{best.get('target_primary')}`",
                f"- Avg local chain share: `{best.get('avg_local_chain_share_pct')}`",
                f"- Avg DB blocks/hour: `{best.get('avg_db_blocks_per_hour')}`",
            ]
        )
    lines.extend(
        [
            "",
            "## Phase Results",
            "",
            "| Phase | Stack | Target | Actual End | Measured UTC | Share | Local b/h | DB b/h | Chain b/h | Template/OK | Late/OK | Status |",
            "| ---: | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in rows:
        derived = row.get("derived") or {}
        status = (row.get("status_at_end") or {}).get("status_overall") or row.get("status")
        measured = f"{row.get('measured_start_utc')} to {row.get('measured_end_utc')}" if row.get("measured_start_utc") else ""
        lines.append(
            "| "
            f"{row.get('phase')} | {row.get('stack')} | {row.get('target_primary')} | {row.get('actual_primary_end')} | "
            f"{measured} | {fmt(row.get('local_chain_share_pct'), '%')} | {fmt(row.get('local_blocks_per_hour'))} | "
            f"{fmt(row.get('db_blocks_per_hour'))} | {fmt(row.get('chain_blocks_per_hour'))} | "
            f"{fmt(derived.get('template_errors_per_ok'))} | {fmt(derived.get('too_late_per_ok'))} | {status} |"
        )
    lines.extend(
        [
            "",
            "## Files",
            "",
            f"- Schedule: `{run_dir / 'phase-schedule.jsonl'}`",
            f"- Results: `{run_dir / 'phase-results.jsonl'}`",
            f"- Samples: `{run_dir / 'samples.jsonl'}`",
            f"- Summary: `{run_dir / 'summary.json'}`",
            f"- Runner log: `{run_dir / 'runner.log'}`",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(run_dir: Path, complete: bool) -> dict[str, Any]:
    rows = read_jsonl(run_dir / "phase-results.jsonl")
    summary = aggregate(rows)
    write_json(run_dir / "summary.json", summary)
    (run_dir / "report.md").write_text(render_report(run_dir, rows, summary, complete), encoding="utf-8")
    return summary


def wait_until(run_dir: Path, deadline: dt.datetime, sample_interval_seconds: int, phase: dict[str, Any]) -> bool:
    next_sample = utc_now()
    while utc_now() < deadline:
        if (run_dir / "STOP").exists():
            log(run_dir, "STOP file detected; ending experiment after current partial phase")
            return False
        now = utc_now()
        if now >= next_sample:
            append_jsonl(run_dir / "samples.jsonl", {**phase, **snapshot_context()})
            next_sample = now + dt.timedelta(seconds=sample_interval_seconds)
        time.sleep(min(30, max(1, (deadline - utc_now()).total_seconds())))
    return True


def apply_phase_config(run_dir: Path, phase: dict[str, Any]) -> dict[str, Any]:
    started = utc_now()
    switch_info: dict[str, Any] = {"skipped": True}
    target_stack = phase["stack"]
    target_primary = phase["target_primary"]
    if current_stack_name() != target_stack:
        log(run_dir, f"phase {phase['phase']}: switching stack to {target_stack}")
        switch_info = switch_stack(target_stack, run_dir)
    else:
        log(run_dir, f"phase {phase['phase']}: stack already {target_stack}")

    primary_before = current_rpc_primary()
    primary_switch = {"skipped": True, "primary_before": primary_before}
    if primary_before != target_primary:
        log(run_dir, f"phase {phase['phase']}: switching RPC primary to {target_primary}")
        ok = run_rpc_failover_switch(target_primary, f"factorial stack experiment phase {phase['phase']}")
        primary_switch = {"skipped": False, "ok": ok, "primary_before": primary_before, "primary_after": current_rpc_primary()}
    else:
        log(run_dir, f"phase {phase['phase']}: RPC primary already {target_primary}")

    return {
        "config_started_utc": iso(started),
        "config_finished_utc": iso(utc_now()),
        "switch": switch_info,
        "primary_switch": primary_switch,
        "actual_stack_start": current_stack_name(),
        "actual_primary_start": current_rpc_primary(),
        "context_start": snapshot_context(),
    }


def choose_best_combo(summary: dict[str, Any]) -> dict[str, Any] | None:
    best = summary.get("best_combo_so_far")
    if isinstance(best, dict) and best.get("stack") in {"old", "new"} and best.get("target_primary") in NODES:
        return best
    return None


def apply_final_policy(run_dir: Path, policy: str, initial_stack: str, initial_primary: str, summary: dict[str, Any]) -> None:
    if policy == "leave":
        log(run_dir, "final policy leave: keeping last tested stack/primary")
        return
    target_stack = initial_stack
    target_primary = initial_primary
    if policy == "best":
        best = choose_best_combo(summary)
        if best:
            target_stack = str(best["stack"])
            target_primary = str(best["target_primary"])
            log(run_dir, f"final policy best: selecting stack={target_stack} primary={target_primary}")
        else:
            log(run_dir, "final policy best: no best combo available, restoring initial config")
    else:
        log(run_dir, f"final policy initial: restoring stack={target_stack} primary={target_primary}")

    if current_stack_name() != target_stack:
        switch_stack(target_stack, run_dir)
    if current_rpc_primary() != target_primary:
        run_rpc_failover_switch(target_primary, f"factorial stack experiment final policy {policy}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--total-hours", type=float, default=float(os.environ.get("BDAG_FACTORIAL_TOTAL_HOURS", "15")))
    parser.add_argument("--phase-minutes", type=int, default=int(os.environ.get("BDAG_FACTORIAL_PHASE_MINUTES", "45")))
    parser.add_argument("--warmup-seconds", type=int, default=int(os.environ.get("BDAG_FACTORIAL_WARMUP_SECONDS", "180")))
    parser.add_argument("--sample-interval-seconds", type=int, default=int(os.environ.get("BDAG_FACTORIAL_SAMPLE_INTERVAL_SECONDS", "300")))
    parser.add_argument("--final-policy", choices=["best", "initial", "leave"], default=os.environ.get("BDAG_FACTORIAL_FINAL_POLICY", "best"))
    parser.add_argument("--run-dir", type=Path, default=None)
    args = parser.parse_args()

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_FILE.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"another factorial stack test is already running; lock={LOCK_FILE}", file=sys.stderr)
        return 2

    initial_stack = normalize_stack(current_stack_name())
    initial_primary = normalize_primary(current_rpc_primary())
    phase_count = max(1, round(args.total_hours * 60 / args.phase_minutes))
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = args.run_dir or RUNTIME_DIR / f"factorial-stack-test-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (RUNTIME_DIR / "latest-factorial-stack-test-dir.txt").write_text(str(run_dir) + "\n", encoding="utf-8")
    if POOL_ENV.exists():
        run(["cp", str(POOL_ENV), str(run_dir / "asic-pool.env.before")])
    if ROOT_ENV.exists():
        run(["cp", str(ROOT_ENV), str(run_dir / "root.env.before")])
    if (ROOT / "haproxy.cfg").exists():
        run(["cp", str(ROOT / "haproxy.cfg"), str(run_dir / "haproxy.cfg.before")])

    schedule = phase_plan(phase_count, initial_stack, initial_primary)
    config = {
        "created_at": now_iso(),
        "total_hours": args.total_hours,
        "phase_minutes": args.phase_minutes,
        "phase_count": phase_count,
        "warmup_seconds": args.warmup_seconds,
        "sample_interval_seconds": args.sample_interval_seconds,
        "final_policy": args.final_policy,
        "initial_stack": initial_stack,
        "initial_primary": initial_primary,
        "old_pool_image": OLD_POOL_IMAGE,
        "old_node_image": OLD_NODE_IMAGE,
        "new_pool_image": NEW_POOL_IMAGE,
        "new_node_image": NEW_NODE_IMAGE,
        "schedule": schedule,
    }
    write_json(run_dir / "config.json", config)
    log(run_dir, f"starting factorial stack test: run_dir={run_dir}")

    complete = False
    fatal_error = ""
    try:
        for phase in schedule:
            phase_started = utc_now()
            log(run_dir, f"phase {phase['phase']}/{phase_count} starting: stack={phase['stack']} primary={phase['target_primary']}")
            phase_record = {**phase, "phase_started_utc": iso(phase_started), "status": "configuring"}
            try:
                phase_record.update(apply_phase_config(run_dir, phase))
                configured_at = utc_now()
                phase_end = configured_at + dt.timedelta(minutes=args.phase_minutes)
                measured_start = int(configured_at.timestamp()) + args.warmup_seconds
                measured_end = int(phase_end.timestamp())
                phase_record.update(
                    {
                        "status": "scheduled",
                        "phase_configured_utc": iso(configured_at),
                        "phase_end_utc": iso(phase_end),
                        "measured_start_utc": iso(measured_start),
                        "measured_end_utc": iso(measured_end),
                        "warmup_seconds": args.warmup_seconds,
                    }
                )
                append_jsonl(run_dir / "phase-schedule.jsonl", phase_record)
                if not wait_until(run_dir, phase_end, args.sample_interval_seconds, phase_record):
                    break
                result = scan_window(phase_record, measured_start, measured_end)
                result["status"] = "measured"
                result["actual_stack_end"] = current_stack_name()
                result["actual_primary_end"] = current_rpc_primary()
                result["confounded"] = result.get("target_primary") != result.get("actual_primary_end")
                append_jsonl(run_dir / "phase-results.jsonl", result)
                summary = write_outputs(run_dir, complete=False)
                log(
                    run_dir,
                    "phase "
                    f"{phase['phase']} measured: share={result.get('local_chain_share_pct')} "
                    f"db_bph={result.get('db_blocks_per_hour')} best={summary.get('best_combo_so_far')}",
                )
            except Exception as exc:  # noqa: BLE001
                error_row = {
                    **phase_record,
                    "status": "failed",
                    "error": str(exc),
                    "failed_at": now_iso(),
                    "actual_stack_end": current_stack_name(),
                    "actual_primary_end": current_rpc_primary(),
                }
                append_jsonl(run_dir / "phase-results.jsonl", error_row)
                write_outputs(run_dir, complete=False)
                log(run_dir, f"phase {phase['phase']} failed: {exc}")
                raise
        else:
            complete = True
    except Exception as exc:  # noqa: BLE001
        fatal_error = str(exc)
        log(run_dir, f"fatal experiment error: {fatal_error}")
    finally:
        summary = write_outputs(run_dir, complete=complete)
        if fatal_error:
            (run_dir / "fatal-error.txt").write_text(fatal_error + "\n", encoding="utf-8")
            try:
                apply_final_policy(run_dir, "initial", initial_stack, initial_primary, summary)
            except Exception as exc:  # noqa: BLE001
                log(run_dir, f"failed to restore initial config after fatal error: {exc}")
        elif complete:
            try:
                apply_final_policy(run_dir, args.final_policy, initial_stack, initial_primary, summary)
                write_outputs(run_dir, complete=True)
            except Exception as exc:  # noqa: BLE001
                log(run_dir, f"final policy failed: {exc}")
        log(run_dir, f"experiment finished complete={complete} fatal={bool(fatal_error)}")

    print(run_dir)
    return 1 if fatal_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
