#!/usr/bin/env python3
"""Compare BlockDAG pool production windows for mining efficiency."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pool_ops import POOL_CONTAINER, RUNTIME_DIR, collect_status, now_iso, pool_db_json, run


DEFAULT_WINDOWS = [
    ("baseline_single_pool", "2026-05-04T20:00:00Z", "2026-05-04T20:30:00Z"),
    ("transition", "2026-05-04T20:30:00Z", "2026-05-04T20:58:00Z"),
    ("seven_local_pools", "2026-05-04T20:58:00Z", "2026-05-04T21:31:10Z"),
    ("restored_single_pool", "2026-05-04T21:31:10Z", "now"),
]

LOG_PATTERNS = {
    "valid_shares": re.compile(r"\bvalid share\b", re.IGNORECASE),
    "blocks_found": re.compile(r"\bblock found\b", re.IGNORECASE),
    "submit_ok": re.compile(r"\bblock submitted\b|\bsubmit.*ok\b", re.IGNORECASE),
    "submit_error": re.compile(r"\bsubmit.*error\b|\bblock submission too late\b|you're overdue", re.IGNORECASE),
    "too_late": re.compile(r"too late|you're overdue", re.IGNORECASE),
    "stale": re.compile(r"\bstale\b", re.IGNORECASE),
    "gbt_errors": re.compile(r"getblocktemplate|gbt", re.IGNORECASE),
    "disconnects": re.compile(r"disconnect|read error|connection reset", re.IGNORECASE),
}


def parse_utc(value: str) -> datetime:
    if value == "now":
        return datetime.now(timezone.utc)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def query_windows(windows: list[tuple[str, datetime, datetime]]) -> list[dict[str, Any]]:
    values = ",\n".join(
        f"('{name}', '{start.isoformat()}'::timestamptz, '{end.isoformat()}'::timestamptz)"
        for name, start, end in windows
    )
    sql = f"""
    WITH windows(name, start_at, end_at) AS (
      VALUES
      {values}
    )
    SELECT COALESCE(json_agg(row_to_json(rows)), '[]'::json)
    FROM (
      SELECT
        w.name,
        w.start_at::text,
        w.end_at::text,
        EXTRACT(EPOCH FROM (w.end_at - w.start_at))::numeric AS seconds,
        (
          SELECT count(*)::int
          FROM blocks b
          WHERE b.created_at >= w.start_at AND b.created_at < w.end_at
        ) AS blocks,
        (
          SELECT count(*)::int
          FROM blocks b
          WHERE b.created_at >= w.start_at
            AND b.created_at < w.end_at
            AND b.status IN ('PENDING', 'MATURE')
        ) AS valid_blocks,
        COALESCE((
          SELECT json_object_agg(status_rows.status, status_rows.count)
          FROM (
            SELECT b.status, count(*)::int
            FROM blocks b
            WHERE b.created_at >= w.start_at AND b.created_at < w.end_at
            GROUP BY b.status
          ) status_rows
        ), '{{}}'::json) AS status_counts,
        (
          SELECT min(b.created_at)::text
          FROM blocks b
          WHERE b.created_at >= w.start_at AND b.created_at < w.end_at
        ) AS first_block_at,
        (
          SELECT max(b.created_at)::text
          FROM blocks b
          WHERE b.created_at >= w.start_at AND b.created_at < w.end_at
        ) AS last_block_at
      FROM windows w
      ORDER BY w.start_at
    ) rows;
    """
    rows = pool_db_json(sql) or []
    for row in rows:
        seconds = float(row.get("seconds") or 0)
        valid_blocks = int(row.get("valid_blocks") or 0)
        row["valid_blocks_per_hour"] = round(valid_blocks * 3600 / seconds, 2) if seconds else 0
        row["avg_seconds_per_valid_block"] = round(seconds / valid_blocks, 3) if valid_blocks else None
    return rows


def collect_log_counts(since: datetime, until: datetime | None = None) -> dict[str, int]:
    command = ["docker", "logs", f"--since={iso_z(since)}"]
    if until is not None:
        command.append(f"--until={iso_z(until)}")
    command.append(POOL_CONTAINER)
    result = run(command, timeout=30)
    counts = {name: 0 for name in LOG_PATTERNS}
    counts["log_lines"] = 0
    if not result.ok:
        counts["log_error"] = 1
        return counts
    text = "\n".join(part for part in (result.stdout, result.stderr) if part)
    for line in text.splitlines():
        counts["log_lines"] += 1
        for name, pattern in LOG_PATTERNS.items():
            if pattern.search(line):
                counts[name] += 1
    return counts


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Mining Configuration Performance Comparison",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "## Block Rate",
        "",
        "| Window | Start UTC | End UTC | Valid blocks | Blocks/hour | Seconds/block | Statuses |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for row in payload["windows"]:
        statuses = ", ".join(f"{key}:{value}" for key, value in sorted((row.get("status_counts") or {}).items()))
        lines.append(
            "| {name} | {start} | {end} | {blocks} | {rate:.2f} | {seconds} | {statuses} |".format(
                name=row["name"],
                start=row["start_at"],
                end=row["end_at"],
                blocks=row["valid_blocks"],
                rate=row["valid_blocks_per_hour"],
                seconds=row["avg_seconds_per_valid_block"] if row["avg_seconds_per_valid_block"] is not None else "",
                statuses=statuses,
            )
        )
    lines.extend(["", "## Current Single-Pool Log Counters", ""])
    log_counts = payload["current_log_counts"]
    lines.extend(
        [
            f"- Since UTC: `{payload['current_log_since']}`",
            f"- Log lines: `{log_counts.get('log_lines', 0)}`",
            f"- Valid shares: `{log_counts.get('valid_shares', 0)}`",
            f"- Blocks found: `{log_counts.get('blocks_found', 0)}`",
            f"- Submit errors: `{log_counts.get('submit_error', 0)}`",
            f"- Too-late/overdue: `{log_counts.get('too_late', 0)}`",
            f"- Disconnect/read errors: `{log_counts.get('disconnects', 0)}`",
        ]
    )
    lines.extend(["", "## Current Status", ""])
    status = payload["status"]
    pool = status.get("pool", {})
    miner_health = status.get("miner_health", {})
    sync_progress = status.get("sync_progress", {})
    sync_health = status.get("sync_health", {})
    lines.extend(
        [
            f"- Overall: `{status.get('overall')}`",
            f"- Connected miners: `{miner_health.get('connected_count')}`",
            f"- Pool containers tracked: `{', '.join(status.get('pool_containers') or [])}`",
            f"- Recent valid shares in status tail: `{pool.get('valid_share_count')}`",
            f"- Recent block submit errors in status tail: `{pool.get('block_submit_error_count')}`",
            f"- Sync status: `{sync_progress.get('status')}`, remaining blocks `{sync_progress.get('remaining_blocks')}`",
            f"- Node block lag: `{sync_health.get('block_lag')}`",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-report", action="store_true", help="write a markdown report under ops/runtime")
    parser.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    args = parser.parse_args()

    windows = [(name, parse_utc(start), parse_utc(end)) for name, start, end in DEFAULT_WINDOWS]
    restored_start = next(start for name, start, _ in windows if name == "restored_single_pool")
    payload: dict[str, Any] = {
        "generated_at": now_iso(),
        "windows": query_windows(windows),
        "current_log_since": iso_z(restored_start),
        "current_log_counts": collect_log_counts(restored_start),
        "status": collect_status(),
    }
    output = json.dumps(payload, indent=2, sort_keys=True, default=str) if args.json else render_markdown(payload)
    if args.write_report:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        path = RUNTIME_DIR / f"mining-config-performance-{stamp}.md"
        path.write_text(render_markdown(payload), encoding="utf-8")
        print(path)
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
