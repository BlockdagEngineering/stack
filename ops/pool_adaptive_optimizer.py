#!/usr/bin/env python3
"""Guarded real-time optimizer for BlockDAG pool job timing.

The controller is intentionally conservative. It optimizes only pool-local
knobs, uses source-truth block outcomes as the primary signal, and treats share
rate as telemetry/load control. It can run in advisory mode without any runtime
admin endpoint; live mutation requires both --apply and --yes.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pool_job_timing_optimizer as timing


DEFAULT_SAFE_TEMPLATE_TTL_MS = 1000
DEFAULT_SAFE_BLOCK_CANDIDATE_JOB_AGE_MS = 1200
DEFAULT_SAFE_VARDIFF_TARGET_SHARE_SECONDS = 3.0
DEFAULT_SAFE_VARDIFF_WINDOW_SECONDS = 60
MIN_TEMPLATE_TTL_MS = 500
MAX_TEMPLATE_TTL_MS = 1500
MIN_BLOCK_CANDIDATE_JOB_AGE_MS = 800
MAX_BLOCK_CANDIDATE_JOB_AGE_MS = 1800
MIN_TARGET_SHARE_SECONDS = 2.0
MAX_TARGET_SHARE_SECONDS = 6.0


@dataclass(frozen=True)
class AdaptiveConfig:
    template_ttl_ms: int = DEFAULT_SAFE_TEMPLATE_TTL_MS
    block_candidate_job_age_ms: int = DEFAULT_SAFE_BLOCK_CANDIDATE_JOB_AGE_MS
    vardiff_target_share_seconds: float = DEFAULT_SAFE_VARDIFF_TARGET_SHARE_SECONDS
    vardiff_window_seconds: int = DEFAULT_SAFE_VARDIFF_WINDOW_SECONDS

    def to_candidate(self, name: str) -> timing.Candidate:
        return timing.Candidate(
            name=name,
            template_ttl_ms=self.template_ttl_ms,
            block_candidate_job_age_ms=self.block_candidate_job_age_ms,
            vardiff_target_share_seconds=self.vardiff_target_share_seconds,
            vardiff_window_seconds=self.vardiff_window_seconds,
        )


@dataclass(frozen=True)
class LaneSnapshot:
    identity_key: str
    mac: str
    label: str
    connected: bool
    work_percent: float | None
    expected_work_percent: float | None
    shares: int
    blocks_found: int
    avg_hashrate: float | None
    hwerr_ratio: float | None
    valid_chips: int | None


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    next_config: AdaptiveConfig
    safety: dict[str, Any]
    score: float
    mode: str


def clamp_int(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def clamp_float(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = value.strip().rstrip("%")
        return float(value)
    except (TypeError, ValueError):
        return None


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_config(raw: dict[str, Any] | None) -> AdaptiveConfig:
    raw = raw or {}
    return AdaptiveConfig(
        template_ttl_ms=clamp_int(int(raw.get("template_ttl_ms") or DEFAULT_SAFE_TEMPLATE_TTL_MS), MIN_TEMPLATE_TTL_MS, MAX_TEMPLATE_TTL_MS),
        block_candidate_job_age_ms=clamp_int(
            int(raw.get("block_candidate_job_age_ms") or DEFAULT_SAFE_BLOCK_CANDIDATE_JOB_AGE_MS),
            MIN_BLOCK_CANDIDATE_JOB_AGE_MS,
            MAX_BLOCK_CANDIDATE_JOB_AGE_MS,
        ),
        vardiff_target_share_seconds=clamp_float(
            float(raw.get("vardiff_target_share_seconds") or DEFAULT_SAFE_VARDIFF_TARGET_SHARE_SECONDS),
            MIN_TARGET_SHARE_SECONDS,
            MAX_TARGET_SHARE_SECONDS,
        ),
        vardiff_window_seconds=max(10, int(raw.get("vardiff_window_seconds") or DEFAULT_SAFE_VARDIFF_WINDOW_SECONDS)),
    )


def extract_lanes(status: dict[str, Any]) -> list[LaneSnapshot]:
    miner_health = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}
    rows = miner_health.get("miners") if isinstance(miner_health.get("miners"), list) else []
    lanes: list[LaneSnapshot] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mac = str(row.get("mac") or "")
        identity = str(row.get("identity_key") or row.get("device_id") or (f"mac:{mac}" if mac else row.get("ip") or "unknown"))
        debug = row.get("debug") if isinstance(row.get("debug"), dict) else {}
        lanes.append(
            LaneSnapshot(
                identity_key=identity,
                mac=mac,
                label=str(row.get("display_label") or row.get("display_name") or identity),
                connected=bool(row.get("connected") or row.get("pool_active") or row.get("work_pool_active")),
                work_percent=float_or_none(row.get("work_percent")),
                expected_work_percent=float_or_none(row.get("expected_work_percent")),
                shares=int(row.get("shares") or 0),
                blocks_found=int(row.get("blocks_found") or 0),
                avg_hashrate=float_or_none(debug.get("av_hashrate") or debug.get("hashrate")),
                hwerr_ratio=float_or_none(debug.get("hwerr_ratio")),
                valid_chips=int_or_none(debug.get("valid")),
            )
        )
    return lanes


def lane_health(status: dict[str, Any]) -> dict[str, Any]:
    lanes = extract_lanes(status)
    connected = [lane for lane in lanes if lane.connected]
    miner_health = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}
    lane_balance = miner_health.get("lane_balance") if isinstance(miner_health.get("lane_balance"), dict) else {}
    imbalances: list[float] = []
    for lane in connected:
        if lane.work_percent is not None and lane.expected_work_percent is not None:
            imbalances.append(abs(lane.work_percent - lane.expected_work_percent))
    max_imbalance = max(imbalances) if imbalances else 0.0
    weak_lanes = [
        asdict(lane)
        for lane in connected
        if (lane.valid_chips is not None and lane.valid_chips < 6)
        or (lane.hwerr_ratio is not None and lane.hwerr_ratio > 0.02)
    ]
    return {
        "identity_basis": lane_balance.get("identity_basis"),
        "connected_count": len(connected),
        "lane_count": len(lanes),
        "max_work_imbalance_percent": round(max_imbalance, 6),
        "weak_lanes": weak_lanes,
        "lanes": [asdict(lane) for lane in lanes],
    }


def admin_available(admin_url: str, timeout: float = 3.0) -> bool:
    try:
        request = urllib.request.Request(admin_url.rstrip("/") + "/admin/vardiff", headers={"user-agent": "bdag-pool-adaptive-optimizer/1"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return False


def host_pressure(status: dict[str, Any]) -> dict[str, Any]:
    pressure = status.get("host_pressure") if isinstance(status.get("host_pressure"), dict) else {}
    return {
        "iowait_percent": float_or_none(pressure.get("iowait_percent")),
        "cpu_busy_percent": float_or_none(pressure.get("cpu_busy_percent")),
        "iowait_warning_active": bool(pressure.get("iowait_warning_active")),
    }


def reason_token(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_") or "unknown"


def summarize_safety(
    summary: dict[str, Any],
    status: dict[str, Any],
    args: argparse.Namespace,
    metrics: dict[str, Any] | None = None,
    abort_reason: str = "",
) -> dict[str, Any]:
    shares = summary.get("shares") if isinstance(summary.get("shares"), dict) else {}
    blocks = summary.get("blocks") if isinstance(summary.get("blocks"), dict) else {}
    templates = summary.get("templates") if isinstance(summary.get("templates"), dict) else {}
    metrics = metrics or {}
    pressure = host_pressure(status)
    lanes = lane_health(status)
    share_accept_ratio = shares.get("accept_ratio")
    block_rejects_per_hour = float(blocks.get("rejected_per_hour") or 0.0)
    stale_rejects_per_minute = float(shares.get("stale_rejects_per_minute") or 0.0)
    accepted_blocks_per_hour = float(blocks.get("accepted_per_hour") or 0.0)
    fetch_avg = templates.get("fetch_avg_seconds")
    violations: list[str] = []

    if abort_reason:
        violations.append(f"runtime_abort_{reason_token(abort_reason)}")
    if float(metrics.get("pool_job_health_ok", 1.0) or 0.0) <= 0.0:
        violations.append("pool_job_health_not_ok")
    if status.get("overall") not in {"ok", None}:
        violations.append(f"dashboard_overall_{status.get('overall')}")
    if status.get("can_mine") is False:
        violations.append("dashboard_can_mine_false")
    if status.get("can_submit_blocks") is False:
        violations.append("dashboard_can_submit_blocks_false")
    if pressure["iowait_percent"] is not None and pressure["iowait_percent"] >= args.max_iowait_percent:
        violations.append("host_iowait_high")
    if lanes["weak_lanes"]:
        violations.append("asic_lane_hardware_weak")
    if lanes["connected_count"] > 1 and lanes["max_work_imbalance_percent"] > args.max_lane_imbalance_percent:
        violations.append("mac_lane_imbalance")
    if share_accept_ratio is not None and share_accept_ratio < args.min_share_accept_ratio:
        violations.append("share_acceptance_below_floor")
    if block_rejects_per_hour > args.max_block_rejects_per_hour:
        violations.append("block_reject_rate_high")
    if fetch_avg is not None and fetch_avg > args.max_template_fetch_avg_seconds:
        violations.append("template_fetch_slow")

    return {
        "ok": not violations,
        "violations": violations,
        "share_accept_ratio": share_accept_ratio,
        "accepted_blocks_per_hour": accepted_blocks_per_hour,
        "block_rejects_per_hour": block_rejects_per_hour,
        "stale_rejects_per_minute": stale_rejects_per_minute,
        "template_fetch_avg_seconds": fetch_avg,
        "host_pressure": pressure,
        "lane_health": lanes,
    }


def score_window(summary: dict[str, Any], status: dict[str, Any]) -> float:
    shares = summary.get("shares") if isinstance(summary.get("shares"), dict) else {}
    blocks = summary.get("blocks") if isinstance(summary.get("blocks"), dict) else {}
    templates = summary.get("templates") if isinstance(summary.get("templates"), dict) else {}
    pressure = host_pressure(status)
    lanes = lane_health(status)

    accepted = float(blocks.get("accepted_per_hour") or 0.0)
    rejected = float(blocks.get("rejected_per_hour") or 0.0)
    stale_rejects = float(shares.get("stale_rejects_per_minute") or 0.0)
    share_accept = shares.get("accept_ratio")
    fetch_avg = templates.get("fetch_avg_seconds")
    iowait = pressure.get("iowait_percent") or 0.0

    score = accepted * 1000.0
    score -= rejected * 1400.0
    score -= stale_rejects * 25.0
    if share_accept is not None:
        score -= max(0.0, 0.90 - float(share_accept)) * 500.0
    if fetch_avg is not None:
        score -= max(0.0, float(fetch_avg) - 0.25) * 100.0
    score -= max(0.0, iowait - 15.0) * 5.0
    score -= float(lanes["max_work_imbalance_percent"]) * 10.0
    if status.get("can_submit_blocks") is False:
        score -= 10000.0
    return round(score, 6)


def choose_next_config(current: AdaptiveConfig, summary: dict[str, Any], status: dict[str, Any], args: argparse.Namespace) -> tuple[AdaptiveConfig, str]:
    shares = summary.get("shares") if isinstance(summary.get("shares"), dict) else {}
    blocks = summary.get("blocks") if isinstance(summary.get("blocks"), dict) else {}
    templates = summary.get("templates") if isinstance(summary.get("templates"), dict) else {}
    rejected_by_reason = shares.get("rejected_by_reason") if isinstance(shares.get("rejected_by_reason"), dict) else {}
    block_reasons = blocks.get("by_outcome_reason") if isinstance(blocks.get("by_outcome_reason"), dict) else {}
    pressure = host_pressure(status)

    stale_rejects = float(shares.get("stale_rejects_per_minute") or 0.0)
    invalidated = float(rejected_by_reason.get("invalidated_job", 0.0))
    non_current = float(rejected_by_reason.get("non_current_job", 0.0))
    old_template_age = sum(float(value) for key, value in block_reasons.items() if "old-template-age" in key)
    fetch_avg = templates.get("fetch_avg_seconds")

    if pressure["iowait_percent"] is not None and pressure["iowait_percent"] >= args.high_iowait_percent:
        next_share = clamp_float(current.vardiff_target_share_seconds + 1.0, MIN_TARGET_SHARE_SECONDS, MAX_TARGET_SHARE_SECONDS)
        next_ttl = clamp_int(current.template_ttl_ms + 250, MIN_TEMPLATE_TTL_MS, MAX_TEMPLATE_TTL_MS)
        return (
            AdaptiveConfig(next_ttl, current.block_candidate_job_age_ms, next_share, current.vardiff_window_seconds),
            "host iowait is high; reduce share/control-plane pressure and avoid faster template polling",
        )

    if old_template_age > 0:
        next_age = clamp_int(current.block_candidate_job_age_ms - 150, MIN_BLOCK_CANDIDATE_JOB_AGE_MS, MAX_BLOCK_CANDIDATE_JOB_AGE_MS)
        return (
            AdaptiveConfig(current.template_ttl_ms, next_age, current.vardiff_target_share_seconds, current.vardiff_window_seconds),
            "node rejected old-template-age candidates; lower local candidate age cliff one step",
        )

    if stale_rejects > args.high_stale_rejects_per_minute or invalidated + non_current > 0:
        next_share = clamp_float(current.vardiff_target_share_seconds + 1.0, MIN_TARGET_SHARE_SECONDS, MAX_TARGET_SHARE_SECONDS)
        if not math.isclose(next_share, current.vardiff_target_share_seconds):
            return (
                AdaptiveConfig(current.template_ttl_ms, current.block_candidate_job_age_ms, next_share, current.vardiff_window_seconds),
                "stale non-block share rejects dominate; reduce telemetry share load before changing block-candidate policy",
            )
        next_ttl = clamp_int(current.template_ttl_ms - 250, MIN_TEMPLATE_TTL_MS, MAX_TEMPLATE_TTL_MS)
        return (
            AdaptiveConfig(next_ttl, current.block_candidate_job_age_ms, current.vardiff_target_share_seconds, current.vardiff_window_seconds),
            "stale rejects persist at max share interval; refresh same-parent templates faster",
        )

    if fetch_avg is not None and float(fetch_avg) > args.slow_template_fetch_avg_seconds:
        next_ttl = clamp_int(current.template_ttl_ms + 250, MIN_TEMPLATE_TTL_MS, MAX_TEMPLATE_TTL_MS)
        return (
            AdaptiveConfig(next_ttl, current.block_candidate_job_age_ms, current.vardiff_target_share_seconds, current.vardiff_window_seconds),
            "template fetch latency is elevated; reduce TTL refresh pressure",
        )

    accepted_blocks = float(blocks.get("accepted") or 0.0)
    rejected_blocks = float(blocks.get("rejected") or 0.0)
    share_accept = shares.get("accept_ratio")
    if accepted_blocks > 0 and rejected_blocks == 0 and (share_accept is None or float(share_accept) >= args.good_share_accept_ratio):
        next_age = clamp_int(current.block_candidate_job_age_ms + 100, MIN_BLOCK_CANDIDATE_JOB_AGE_MS, MAX_BLOCK_CANDIDATE_JOB_AGE_MS)
        if next_age != current.block_candidate_job_age_ms:
            return (
                AdaptiveConfig(current.template_ttl_ms, next_age, current.vardiff_target_share_seconds, current.vardiff_window_seconds),
                "window was healthy; cautiously preserve more same-parent candidate opportunity",
            )

    return current, "hold: no safe improvement signal exceeded the deadband"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def build_report(run_dir: Path, payload: dict[str, Any]) -> Path:
    rows = []
    for row in payload.get("iterations", []):
        safety = row.get("decision", {}).get("safety", {})
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('iteration')))}</td>"
            f"<td>{html.escape(row.get('decision', {}).get('action', ''))}</td>"
            f"<td>{html.escape(row.get('decision', {}).get('reason', ''))}</td>"
            f"<td>{html.escape(str(row.get('decision', {}).get('score')))}</td>"
            f"<td>{html.escape(str(safety.get('accepted_blocks_per_hour')))}</td>"
            f"<td>{html.escape(str(safety.get('block_rejects_per_hour')))}</td>"
            f"<td>{html.escape(str(safety.get('share_accept_ratio')))}</td>"
            f"<td>{html.escape(', '.join(safety.get('violations') or []))}</td>"
            "</tr>"
        )
    report = run_dir / "pool-adaptive-optimizer-report.html"
    report.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pool Adaptive Optimizer Report</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 0; background: #11161b; color: #e8edf2; }}
    main {{ max-width: 1220px; margin: 0 auto; padding: 28px; }}
    h1 {{ margin: 0 0 8px; font-size: 30px; }}
    .sub {{ color: #9fb0c0; margin-bottom: 22px; }}
    table {{ width: 100%; border-collapse: collapse; background: #151d24; border: 1px solid #26313b; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid #26313b; text-align: left; vertical-align: top; }}
    th {{ color: #a9bac9; font-size: 12px; text-transform: uppercase; }}
    code {{ color: #8bd5ff; }}
  </style>
  <script type="application/json" id="agent-metadata">{html.escape(json.dumps({"run_dir": str(run_dir)}, sort_keys=True))}</script>
</head>
<body><main>
  <h1>Pool Adaptive Optimizer Report</h1>
  <div class="sub">Generated {html.escape(payload.get('finished_at') or timing.utc_iso())}. Mode: <code>{html.escape(payload.get('mode', ''))}</code>.</div>
  <table>
    <thead><tr><th>#</th><th>Action</th><th>Reason</th><th>Score</th><th>Accepted blocks/h</th><th>Rejected blocks/h</th><th>Share accept</th><th>Violations</th></tr></thead>
    <tbody>{''.join(rows) if rows else '<tr><td colspan="8">No iterations recorded.</td></tr>'}</tbody>
  </table>
</main></body></html>
""",
        encoding="utf-8",
    )
    return report


def run_controller(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.output_dir or (timing.RUNTIME_DIR / "reports" / f"pool-adaptive-optimizer-{timing.local_stamp()}"))
    run_dir.mkdir(parents=True, exist_ok=True)
    events_path = run_dir / "events.jsonl"
    state_path = Path(args.state_file or (timing.RUNTIME_DIR / "pool-adaptive-optimizer-state.json"))
    state = load_state(state_path)
    safe_config = AdaptiveConfig(
        template_ttl_ms=args.safe_template_ttl_ms,
        block_candidate_job_age_ms=args.safe_block_candidate_job_age_ms,
        vardiff_target_share_seconds=args.safe_vardiff_target_share_seconds,
        vardiff_window_seconds=args.safe_vardiff_window_seconds,
    )
    current = parse_config(state.get("current_config")) if state else safe_config
    admin_ok = admin_available(args.admin_url)
    apply_enabled = bool(args.apply and args.yes and admin_ok)
    mode = "apply" if apply_enabled else "advisory"
    if args.apply and not args.yes:
        raise SystemExit("--apply requires --yes")

    payload: dict[str, Any] = {
        "started_at": timing.utc_iso(),
        "mode": mode,
        "admin_available": admin_ok,
        "metrics_url": args.metrics_url,
        "dashboard_status_url": args.dashboard_status_url,
        "safe_config": asdict(safe_config),
        "starting_config": asdict(current),
        "iterations": [],
    }
    timing.append_jsonl(events_path, {"time": timing.utc_iso(), "event": "start", "mode": mode, "admin_available": admin_ok})

    if apply_enabled and args.apply_initial_config:
        response = timing.apply_candidate(args.admin_url, current.to_candidate("adaptive-current"))
        timing.append_jsonl(events_path, {"time": timing.utc_iso(), "event": "apply-initial", "config": asdict(current), "response": response})

    iteration = 0
    while args.iterations <= 0 or iteration < args.iterations:
        iteration += 1
        before = timing.observe_metrics(args.metrics_url)
        window_start = time.monotonic()
        abort = ""
        while time.monotonic() - window_start < args.window_seconds:
            time.sleep(args.sample_interval_seconds)
            current_metrics = timing.observe_metrics(args.metrics_url)
            status = timing.fetch_json(args.dashboard_status_url)
            abort = timing.abort_reason(status, before, current_metrics)
            timing.append_jsonl(events_path, {"time": timing.utc_iso(), "event": "sample", "iteration": iteration, "abort_reason": abort, "status": status.get("overall")})
            if abort and args.stop_on_abort:
                break

        after = timing.observe_metrics(args.metrics_url)
        status_after = timing.fetch_json(args.dashboard_status_url)
        seconds = max(1.0, time.monotonic() - window_start)
        summary = timing.summarize_window(before, after, seconds, status_after)
        safety = summarize_safety(summary, status_after, args, after, abort)
        score = score_window(summary, status_after)

        if not safety["ok"]:
            next_config = safe_config
            action = "revert" if current != safe_config else "hold"
            reason = "safety violation: " + ", ".join(safety["violations"])
        else:
            next_config, reason = choose_next_config(current, summary, status_after, args)
            action = "apply" if next_config != current else "hold"

        decision = Decision(action=action, reason=reason, next_config=next_config, safety=safety, score=score, mode=mode)
        apply_response: list[dict[str, Any]] = []
        if apply_enabled and action in {"apply", "revert"}:
            apply_response = timing.apply_candidate(args.admin_url, next_config.to_candidate(f"adaptive-{action}-{iteration}"))
            current = next_config
        elif not apply_enabled and action in {"apply", "revert"}:
            action = "recommend-" + action
            decision = Decision(action=action, reason=reason, next_config=next_config, safety=safety, score=score, mode=mode)
        elif action == "hold":
            next_config = current

        state = {
            "updated_at": timing.utc_iso(),
            "current_config": asdict(current),
            "recommended_config": asdict(next_config),
            "last_decision": {
                "action": decision.action,
                "reason": decision.reason,
                "score": score,
                "next_config": asdict(next_config),
                "mode": mode,
            },
        }
        save_state(state_path, state)

        row = {
            "iteration": iteration,
            "started_at": payload["started_at"],
            "finished_at": timing.utc_iso(),
            "seconds": round(seconds, 3),
            "abort_reason": abort,
            "summary": summary,
            "decision": {
                "action": decision.action,
                "reason": decision.reason,
                "next_config": asdict(next_config),
                "safety": safety,
                "score": score,
                "mode": mode,
            },
            "apply_response": apply_response,
        }
        payload["iterations"].append(row)
        timing.append_jsonl(events_path, {"time": timing.utc_iso(), "event": "iteration", **row})
        if abort and args.stop_on_abort:
            break

    payload["finished_at"] = timing.utc_iso()
    payload["state_path"] = str(state_path)
    payload["events_path"] = str(events_path)
    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    report_path = build_report(run_dir, payload)
    payload["summary_path"] = str(summary_path)
    payload["report_path"] = str(report_path)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics-url", default=os.environ.get("BDAG_POOL_OPTIMIZER_METRICS_URL", timing.DEFAULT_METRICS_URL))
    parser.add_argument("--admin-url", default=os.environ.get("BDAG_POOL_OPTIMIZER_ADMIN_URL", timing.DEFAULT_ADMIN_URL))
    parser.add_argument("--dashboard-status-url", default=os.environ.get("BDAG_POOL_OPTIMIZER_DASHBOARD_STATUS_URL", timing.DEFAULT_DASHBOARD_STATUS_URL))
    parser.add_argument("--output-dir", default=os.environ.get("BDAG_POOL_OPTIMIZER_OUTPUT_DIR"))
    parser.add_argument("--state-file", default=os.environ.get("BDAG_POOL_OPTIMIZER_STATE_FILE"))
    parser.add_argument("--window-seconds", type=float, default=300)
    parser.add_argument("--sample-interval-seconds", type=float, default=15)
    parser.add_argument("--iterations", type=int, default=1, help="0 means run forever")
    parser.add_argument("--apply", action="store_true", help="Apply decisions through runtime admin endpoints. Default is advisory.")
    parser.add_argument("--yes", action="store_true", help="Required with --apply.")
    parser.add_argument("--apply-initial-config", action="store_true")
    parser.add_argument("--stop-on-abort", action="store_true", default=True)
    parser.add_argument("--continue-on-abort", action="store_false", dest="stop_on_abort")
    parser.add_argument("--safe-template-ttl-ms", type=int, default=DEFAULT_SAFE_TEMPLATE_TTL_MS)
    parser.add_argument("--safe-block-candidate-job-age-ms", type=int, default=DEFAULT_SAFE_BLOCK_CANDIDATE_JOB_AGE_MS)
    parser.add_argument("--safe-vardiff-target-share-seconds", type=float, default=DEFAULT_SAFE_VARDIFF_TARGET_SHARE_SECONDS)
    parser.add_argument("--safe-vardiff-window-seconds", type=int, default=DEFAULT_SAFE_VARDIFF_WINDOW_SECONDS)
    parser.add_argument("--min-share-accept-ratio", type=float, default=0.40)
    parser.add_argument("--good-share-accept-ratio", type=float, default=0.90)
    parser.add_argument("--high-stale-rejects-per-minute", type=float, default=1.0)
    parser.add_argument("--max-block-rejects-per-hour", type=float, default=120.0)
    parser.add_argument("--max-iowait-percent", type=float, default=35.0)
    parser.add_argument("--high-iowait-percent", type=float, default=25.0)
    parser.add_argument("--max-lane-imbalance-percent", type=float, default=20.0)
    parser.add_argument("--max-template-fetch-avg-seconds", type=float, default=2.0)
    parser.add_argument("--slow-template-fetch-avg-seconds", type=float, default=0.5)
    return parser


def main() -> int:
    payload = run_controller(build_parser().parse_args())
    print(json.dumps({key: payload[key] for key in ("summary_path", "events_path", "report_path", "state_path")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
