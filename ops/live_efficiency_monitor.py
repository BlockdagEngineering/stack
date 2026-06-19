#!/usr/bin/env python3
"""Wait for real pool mining readiness, then record lane efficiency windows."""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from typing import Any

import lane_efficiency_snapshot
import pool_timing_calibrator
from pool_ops import RUNTIME_DIR, now_iso


DEFAULT_METRICS_URL = "http://127.0.0.1:9090/metrics"
DEFAULT_JOB_STATE_URL = "http://127.0.0.1:9090/health/job-state"
DEFAULT_STATE_PATH = RUNTIME_DIR / "live-efficiency-monitor.jsonl"
REPORT_DIR = RUNTIME_DIR / "reports"


def fetch_text(url: str, timeout: float = 5.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def fetch_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    return json.loads(fetch_text(url, timeout=timeout))


def append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def readiness_state(metrics_text: str, job_state: dict[str, Any]) -> dict[str, Any]:
    metrics = pool_timing_calibrator.parse_metrics(metrics_text)
    ready, reason = pool_timing_calibrator.calibration_gate(metrics, job_state)
    return {
        "ready": ready,
        "reason": reason,
        "active_connections": job_state.get("active_connections"),
        "authorized_connections": job_state.get("authorized_connections"),
        "ready_connections": job_state.get("ready_connections"),
        "job_reason_code": job_state.get("reason_code"),
        "mineable": pool_timing_calibrator.metric_value(metrics, "pool_rpc_backend_node_health_mineable"),
        "submit_ready": pool_timing_calibrator.metric_value(metrics, "pool_rpc_backend_node_health_submit_ready"),
        "p2p_fresh": pool_timing_calibrator.metric_value(metrics, "pool_rpc_backend_node_health_p2p_mining_fresh"),
    }


def run_efficiency_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    snapshot_args = Namespace(
        metrics_url=args.metrics_url,
        timeout=args.timeout,
        duration=args.snapshot_duration,
    )
    return lane_efficiency_snapshot.run_snapshot(snapshot_args)


def write_report(summary: dict[str, Any], args: argparse.Namespace) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = REPORT_DIR / f"live-efficiency-monitor-{stamp}.json"
    html_path = REPORT_DIR / f"live-efficiency-monitor-{stamp}.html"
    metadata = {
        "document_type": "bdag_live_efficiency_monitor",
        "metrics_url": args.metrics_url,
        "duration_seconds": args.snapshot_duration,
        "target_waste_ratio": args.target_waste_ratio,
        "resource_note": "read-only direct Prometheus scrape after backend and miner readiness gates pass",
    }
    json_path.write_text(json.dumps({"metadata": metadata, "summary": summary}, indent=2, sort_keys=True), encoding="utf-8")
    html_path.write_text(lane_efficiency_snapshot.render_html(summary, metadata), encoding="utf-8")
    return str(html_path)


def sleep_bounded(seconds: float, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return
    time.sleep(max(0.0, min(seconds, remaining)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics-url", default=DEFAULT_METRICS_URL)
    parser.add_argument("--job-state-url", default=DEFAULT_JOB_STATE_URL)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--duration", type=float, default=7 * 60 * 60)
    parser.add_argument("--snapshot-duration", type=float, default=180.0)
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--target-waste-ratio", type=float, default=0.05)
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()

    deadline = time.monotonic() + max(0.0, args.duration)
    append_jsonl(
        args.state_path,
        {
            "event": "start",
            "generated_at": now_iso(),
            "metrics_url": args.metrics_url,
            "job_state_url": args.job_state_url,
            "snapshot_duration": args.snapshot_duration,
            "target_waste_ratio": args.target_waste_ratio,
        },
    )

    while time.monotonic() < deadline:
        try:
            metrics_text = fetch_text(args.metrics_url, timeout=args.timeout)
            job_state = fetch_json(args.job_state_url, timeout=args.timeout)
            readiness = readiness_state(metrics_text, job_state)
        except Exception as exc:  # noqa: BLE001
            append_jsonl(args.state_path, {"event": "wait", "generated_at": now_iso(), "ready": False, "reason": "fetch-error", "error": str(exc)})
            sleep_bounded(args.poll_seconds, deadline)
            continue

        if not readiness["ready"]:
            append_jsonl(args.state_path, {"event": "wait", "generated_at": now_iso(), **readiness})
            sleep_bounded(args.poll_seconds, deadline)
            continue

        summary = run_efficiency_snapshot(args)
        waste = float(summary.get("block_waste_ratio") or 0.0)
        entry = {
            "event": "snapshot",
            "generated_at": now_iso(),
            "target_met": waste <= args.target_waste_ratio,
            "readiness": readiness,
            "summary": summary,
        }
        if args.write_report:
            entry["report"] = write_report(summary, args)
        append_jsonl(args.state_path, entry)
        print(json.dumps(entry, sort_keys=True), flush=True)
        sleep_bounded(args.poll_seconds, deadline)

    append_jsonl(args.state_path, {"event": "stop", "generated_at": now_iso()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
