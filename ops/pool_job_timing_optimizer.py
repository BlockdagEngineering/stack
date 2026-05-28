#!/usr/bin/env python3
"""Measure and safely tune pool job freshness versus share difficulty.

The harness is deliberately conservative:

* observe-only by default;
* one candidate at a time when --apply is used;
* local runtime-admin endpoints only;
* JSONL evidence plus an HTML report for later comparison.

The primary success signal is accepted block candidates per miner-hour. Share
acceptance is a telemetry and fairness signal, not proof that the ASIC has a
higher chance of finding a network-valid block.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / "ops" / "runtime"
DEFAULT_METRICS_URL = "http://127.0.0.1:9092/metrics"
DEFAULT_ADMIN_URL = "http://127.0.0.1:9092"
DEFAULT_DASHBOARD_STATUS_URL = "http://127.0.0.1:8088/api/status"
PROM_SAMPLE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)$")
PROM_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


@dataclass(frozen=True)
class Candidate:
    name: str
    template_ttl_ms: int | None = None
    block_candidate_job_age_ms: int | None = None
    vardiff_target_share_seconds: float | None = None
    vardiff_window_seconds: int | None = None
    vardiff_tolerance: float | None = None
    vardiff_step: float | None = None

    def params(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if key != "name" and value is not None}


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def local_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def fetch_text(url: str, timeout: float = 5.0) -> str:
    request = urllib.request.Request(url, headers={"user-agent": "bdag-pool-job-timing-optimizer/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def fetch_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    try:
        return json.loads(fetch_text(url, timeout=timeout))
    except Exception as exc:  # noqa: BLE001 - status is advisory in the harness.
        return {"overall": "unknown", "error": str(exc)}


def post_admin(admin_url: str, path: str, params: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
    url = admin_url.rstrip("/") + path + ("?" + query if query else "")
    request = urllib.request.Request(url, method="POST", headers={"user-agent": "bdag-pool-job-timing-optimizer/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", "replace")
    return json.loads(text) if text.strip() else {"ok": True}


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def parse_labels(raw: str | None) -> dict[str, str]:
    labels: dict[str, str] = {}
    for match in PROM_LABEL_RE.finditer(raw or ""):
        labels[match.group(1)] = match.group(2).replace(r"\"", '"').replace(r"\\", "\\")
    return labels


def counter_key(labels: dict[str, str], *names: str) -> str:
    return ":".join(labels.get(name, "") for name in names)


def parse_prometheus(text: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "raw": {},
        "share_rejected_by_reason": {},
        "stale_shares_acked_by_reason": {},
        "block_submit_by_outcome_reason": {},
        "blocks_rejected_by_node": {},
        "backend_submit_by_result": {},
    }
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = PROM_SAMPLE_RE.match(line)
        if not match:
            continue
        name, label_text, raw_value = match.groups()
        labels = parse_labels(label_text)
        try:
            value = float(raw_value)
        except ValueError:
            continue
        metrics["raw"][name] = value

        if name == "pool_shares_accepted_total":
            metrics["shares_accepted"] = metrics.get("shares_accepted", 0.0) + value
        elif name == "pool_shares_rejected_total":
            reason = labels.get("reason", "unknown")
            metrics["share_rejected_by_reason"][reason] = metrics["share_rejected_by_reason"].get(reason, 0.0) + value
        elif name == "pool_stale_shares_acked_total":
            reason = labels.get("reason", "unknown")
            metrics["stale_shares_acked_by_reason"][reason] = metrics["stale_shares_acked_by_reason"].get(reason, 0.0) + value
        elif name == "pool_block_submit_outcomes_total":
            key = counter_key(labels, "outcome", "reason")
            metrics["block_submit_by_outcome_reason"][key] = metrics["block_submit_by_outcome_reason"].get(key, 0.0) + value
        elif name == "pool_blocks_rejected_by_node_total":
            reason = labels.get("reason", "unknown")
            metrics["blocks_rejected_by_node"][reason] = metrics["blocks_rejected_by_node"].get(reason, 0.0) + value
        elif name == "pool_rpc_backend_submit_total":
            key = counter_key(labels, "backend", "result")
            metrics["backend_submit_by_result"][key] = metrics["backend_submit_by_result"].get(key, 0.0) + value
        elif name in {
            "pool_template_broadcasts_total",
            "pool_jobs_marked_stale_total",
            "pool_job_health_ok",
            "pool_job_health_ready_miners",
            "pool_job_health_current_job_stale_miners",
            "pool_job_health_current_job_invalidated_miners",
            "pool_rpc_backend_node_health_submit_ready",
            "pool_template_fetch_duration_seconds_sum",
            "pool_template_fetch_duration_seconds_count",
            "pool_rpc_backend_submit_duration_seconds_sum",
            "pool_rpc_backend_submit_duration_seconds_count",
        }:
            metrics[name] = metrics.get(name, 0.0) + value
    metrics.setdefault("shares_accepted", 0.0)
    return metrics


def numeric_delta(before: dict[str, Any], after: dict[str, Any], key: str) -> float:
    return max(0.0, float(after.get(key, 0.0) or 0.0) - float(before.get(key, 0.0) or 0.0))


def dict_delta(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    keys = set(before) | set(after)
    return {key: max(0.0, float(after.get(key, 0.0)) - float(before.get(key, 0.0))) for key in sorted(keys)}


def summarize_window(before: dict[str, Any], after: dict[str, Any], seconds: float, status: dict[str, Any]) -> dict[str, Any]:
    share_rejects = dict_delta(before.get("share_rejected_by_reason", {}), after.get("share_rejected_by_reason", {}))
    stale_acks = dict_delta(before.get("stale_shares_acked_by_reason", {}), after.get("stale_shares_acked_by_reason", {}))
    block_outcomes = dict_delta(before.get("block_submit_by_outcome_reason", {}), after.get("block_submit_by_outcome_reason", {}))
    node_rejects = dict_delta(before.get("blocks_rejected_by_node", {}), after.get("blocks_rejected_by_node", {}))
    accepted_blocks = sum(value for key, value in block_outcomes.items() if key.startswith("accepted:"))
    rejected_blocks = sum(value for key, value in block_outcomes.items() if key.startswith("rejected:") or key.startswith("rejected-local:"))
    shares_accepted = numeric_delta(before, after, "shares_accepted")
    shares_rejected = sum(share_rejects.values())
    shares_total = shares_accepted + shares_rejected
    stale_share_rejects = sum(
        share_rejects.get(reason, 0.0)
        for reason in ("invalidated_job", "non_current_job", "stale_block_candidate", "stale_parent", "stale_job")
    )
    template_fetch_count = numeric_delta(before, after, "pool_template_fetch_duration_seconds_count")
    template_fetch_sum = numeric_delta(before, after, "pool_template_fetch_duration_seconds_sum")
    submit_count = numeric_delta(before, after, "pool_rpc_backend_submit_duration_seconds_count")
    submit_sum = numeric_delta(before, after, "pool_rpc_backend_submit_duration_seconds_sum")
    minutes = max(seconds / 60.0, 1e-9)
    hours = max(seconds / 3600.0, 1e-9)
    share_accept_ratio = shares_accepted / shares_total if shares_total > 0 else None
    block_accept_ratio = accepted_blocks / (accepted_blocks + rejected_blocks) if accepted_blocks + rejected_blocks > 0 else None
    score = score_summary(
        accepted_blocks_per_hour=accepted_blocks / hours,
        rejected_blocks_per_hour=rejected_blocks / hours,
        stale_share_rejects_per_minute=stale_share_rejects / minutes,
        share_accept_ratio=share_accept_ratio,
        status=status,
    )
    return {
        "seconds": round(seconds, 3),
        "shares": {
            "accepted": round(shares_accepted, 6),
            "rejected": round(shares_rejected, 6),
            "total": round(shares_total, 6),
            "accept_ratio": round(share_accept_ratio, 6) if share_accept_ratio is not None else None,
            "rejected_by_reason": share_rejects,
            "stale_acked_by_reason": stale_acks,
            "stale_rejects_per_minute": round(stale_share_rejects / minutes, 6),
        },
        "blocks": {
            "accepted": round(accepted_blocks, 6),
            "rejected": round(rejected_blocks, 6),
            "accept_ratio": round(block_accept_ratio, 6) if block_accept_ratio is not None else None,
            "accepted_per_hour": round(accepted_blocks / hours, 6),
            "rejected_per_hour": round(rejected_blocks / hours, 6),
            "by_outcome_reason": block_outcomes,
            "rejected_by_node": node_rejects,
        },
        "templates": {
            "broadcasts": round(numeric_delta(before, after, "pool_template_broadcasts_total"), 6),
            "jobs_marked_stale": round(numeric_delta(before, after, "pool_jobs_marked_stale_total"), 6),
            "fetch_avg_seconds": round(template_fetch_sum / template_fetch_count, 6) if template_fetch_count > 0 else None,
        },
        "submit": {
            "avg_seconds": round(submit_sum / submit_count, 6) if submit_count > 0 else None,
        },
        "status": {
            "overall": status.get("overall"),
            "mode": status.get("mode"),
            "can_mine": status.get("can_mine"),
            "can_submit_blocks": status.get("can_submit_blocks"),
            "degraded_reasons": status.get("degraded_reasons") or [],
        },
        "score": round(score, 6),
    }


def score_summary(
    accepted_blocks_per_hour: float,
    rejected_blocks_per_hour: float,
    stale_share_rejects_per_minute: float,
    share_accept_ratio: float | None,
    status: dict[str, Any],
) -> float:
    score = accepted_blocks_per_hour * 1000.0
    score -= rejected_blocks_per_hour * 1200.0
    score -= stale_share_rejects_per_minute * 20.0
    if share_accept_ratio is not None:
        score -= max(0.0, 0.95 - share_accept_ratio) * 250.0
    if status.get("overall") not in {"ok", None}:
        score -= 10000.0
    if status.get("can_submit_blocks") is False:
        score -= 10000.0
    return score


def default_candidates() -> list[Candidate]:
    return [
        Candidate("baseline-observe"),
        Candidate("fresh-500ms-share-2s", template_ttl_ms=500, block_candidate_job_age_ms=900, vardiff_target_share_seconds=2.0, vardiff_window_seconds=60),
        Candidate("fresh-750ms-share-3s", template_ttl_ms=750, block_candidate_job_age_ms=1200, vardiff_target_share_seconds=3.0, vardiff_window_seconds=60),
        Candidate("fresh-1000ms-share-3s", template_ttl_ms=1000, block_candidate_job_age_ms=1200, vardiff_target_share_seconds=3.0, vardiff_window_seconds=60),
        Candidate("fresh-1000ms-share-5s", template_ttl_ms=1000, block_candidate_job_age_ms=1500, vardiff_target_share_seconds=5.0, vardiff_window_seconds=60),
    ]


def load_candidates(path: Path | None) -> list[Candidate]:
    if path is None:
        return default_candidates()
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Candidate(**row) for row in data]


def apply_candidate(admin_url: str, candidate: Candidate) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    if candidate.template_ttl_ms is not None:
        responses.append({"template_ttl": post_admin(admin_url, "/admin/template-ttl-refresh", {"ms": candidate.template_ttl_ms})})
    if candidate.block_candidate_job_age_ms is not None:
        responses.append({"block_candidate_job_age": post_admin(admin_url, "/admin/block-candidate-job-age", {"ms": candidate.block_candidate_job_age_ms})})
    vardiff_params: dict[str, Any] = {}
    if candidate.vardiff_target_share_seconds is not None:
        vardiff_params["target_share_seconds"] = candidate.vardiff_target_share_seconds
    if candidate.vardiff_window_seconds is not None:
        vardiff_params["window_seconds"] = candidate.vardiff_window_seconds
    if candidate.vardiff_tolerance is not None:
        vardiff_params["tolerance"] = candidate.vardiff_tolerance
    if candidate.vardiff_step is not None:
        vardiff_params["step"] = candidate.vardiff_step
    if vardiff_params:
        responses.append({"vardiff": post_admin(admin_url, "/admin/vardiff", vardiff_params)})
    return responses


def observe_metrics(metrics_url: str) -> dict[str, Any]:
    return parse_prometheus(fetch_text(metrics_url, timeout=5.0))


def abort_reason(status: dict[str, Any], before: dict[str, Any], after: dict[str, Any]) -> str:
    if status.get("overall") not in {"ok", None}:
        return f"dashboard overall={status.get('overall')}"
    if status.get("can_submit_blocks") is False:
        return "dashboard says can_submit_blocks=false"
    if float(after.get("pool_job_health_ok", 1.0) or 0.0) <= 0:
        return "pool_job_health_ok=0"
    if numeric_delta(before, after, "pool_job_health_current_job_stale_miners") > 0:
        return "stale current job miner observed"
    if numeric_delta(before, after, "pool_job_health_current_job_invalidated_miners") > 0:
        return "invalidated current job miner observed"
    return ""


def build_report(run_dir: Path, payload: dict[str, Any]) -> Path:
    rows = []
    for result in payload.get("results", []):
        summary = result.get("summary") or {}
        blocks = summary.get("blocks") or {}
        shares = summary.get("shares") or {}
        rows.append(
            "<tr>"
            f"<td>{html.escape(result.get('candidate', ''))}</td>"
            f"<td>{html.escape(result.get('mode', ''))}</td>"
            f"<td>{html.escape(str(round(float(summary.get('score') or 0.0), 2)))}</td>"
            f"<td>{html.escape(str(blocks.get('accepted_per_hour')))}</td>"
            f"<td>{html.escape(str(blocks.get('rejected_per_hour')))}</td>"
            f"<td>{html.escape(str(shares.get('accept_ratio')))}</td>"
            f"<td>{html.escape(result.get('abort_reason') or '')}</td>"
            "</tr>"
        )
    report = run_dir / "pool-job-timing-optimizer-report.html"
    report.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pool Job Timing Optimizer Report</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 0; background: #11161b; color: #e8edf2; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px; }}
    h1 {{ margin: 0 0 8px; font-size: 30px; }}
    .sub {{ color: #9fb0c0; margin-bottom: 22px; }}
    table {{ width: 100%; border-collapse: collapse; background: #151d24; border: 1px solid #26313b; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #26313b; text-align: left; vertical-align: top; }}
    th {{ color: #a9bac9; font-size: 12px; text-transform: uppercase; }}
    code {{ color: #8bd5ff; }}
  </style>
  <script type="application/json" id="agent-metadata">{html.escape(json.dumps({"run_dir": str(run_dir)}, sort_keys=True))}</script>
</head>
<body><main>
  <h1>Pool Job Timing Optimizer Report</h1>
  <div class="sub">Generated {html.escape(payload.get('finished_at') or utc_iso())}. Run directory: <code>{html.escape(str(run_dir))}</code></div>
  <table>
    <thead><tr><th>Candidate</th><th>Mode</th><th>Score</th><th>Accepted blocks/h</th><th>Rejected blocks/h</th><th>Share accept ratio</th><th>Abort</th></tr></thead>
    <tbody>{''.join(rows) if rows else '<tr><td colspan="7">No completed windows.</td></tr>'}</tbody>
  </table>
</main></body></html>
""",
        encoding="utf-8",
    )
    return report


def run_harness(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.output_dir or (RUNTIME_DIR / "reports" / f"pool-job-timing-optimizer-{local_stamp()}"))
    run_dir.mkdir(parents=True, exist_ok=True)
    events = run_dir / "events.jsonl"
    candidates = load_candidates(Path(args.candidates_json) if args.candidates_json else None)
    payload: dict[str, Any] = {
        "started_at": utc_iso(),
        "mode": "apply" if args.apply else "observe",
        "metrics_url": args.metrics_url,
        "dashboard_status_url": args.dashboard_status_url,
        "duration_seconds": args.duration_seconds,
        "warmup_seconds": args.warmup_seconds,
        "candidates": [asdict(candidate) for candidate in candidates],
        "results": [],
    }
    append_jsonl(events, {"time": utc_iso(), "event": "start", "mode": payload["mode"], "candidate_count": len(candidates)})

    if args.apply and not args.yes:
        raise SystemExit("--apply requires --yes so live mutation cannot happen accidentally")

    baseline = Candidate(
        "revert-baseline",
        template_ttl_ms=args.revert_template_ttl_ms,
        block_candidate_job_age_ms=args.revert_block_candidate_job_age_ms,
        vardiff_target_share_seconds=args.revert_vardiff_target_share_seconds,
        vardiff_window_seconds=args.revert_vardiff_window_seconds,
    )
    try:
        for candidate in candidates:
            status_before = fetch_json(args.dashboard_status_url)
            if args.apply and status_before.get("overall") not in {"ok", None}:
                raise SystemExit(f"refusing apply while dashboard overall={status_before.get('overall')}")
            apply_responses: list[dict[str, Any]] = []
            if args.apply and candidate.params():
                apply_responses = apply_candidate(args.admin_url, candidate)
                append_jsonl(events, {"time": utc_iso(), "event": "applied-candidate", "candidate": candidate.name, "responses": apply_responses})
                time.sleep(args.warmup_seconds)

            before = observe_metrics(args.metrics_url)
            window_start = time.monotonic()
            abort = ""
            while time.monotonic() - window_start < args.duration_seconds:
                time.sleep(args.sample_interval_seconds)
                current = observe_metrics(args.metrics_url)
                status = fetch_json(args.dashboard_status_url)
                abort = abort_reason(status, before, current)
                append_jsonl(events, {"time": utc_iso(), "event": "sample", "candidate": candidate.name, "abort_reason": abort, "status": status.get("overall")})
                if abort:
                    break

            after = observe_metrics(args.metrics_url)
            status_after = fetch_json(args.dashboard_status_url)
            summary = summarize_window(before, after, max(1.0, time.monotonic() - window_start), status_after)
            result = {
                "candidate": candidate.name,
                "mode": payload["mode"],
                "params": candidate.params(),
                "apply_responses": apply_responses,
                "abort_reason": abort,
                "summary": summary,
                "finished_at": utc_iso(),
            }
            payload["results"].append(result)
            append_jsonl(events, {"time": utc_iso(), "event": "window-result", **result})
            if abort and args.stop_on_abort:
                break
    finally:
        if args.apply and args.revert:
            try:
                responses = apply_candidate(args.admin_url, baseline)
                append_jsonl(events, {"time": utc_iso(), "event": "revert", "responses": responses})
            except Exception as exc:  # noqa: BLE001 - report failed rollback explicitly.
                append_jsonl(events, {"time": utc_iso(), "event": "revert-failed", "error": str(exc)})
                payload["revert_error"] = str(exc)

    payload["finished_at"] = utc_iso()
    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    report_path = build_report(run_dir, payload)
    payload["summary_path"] = str(summary_path)
    payload["events_path"] = str(events)
    payload["report_path"] = str(report_path)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics-url", default=DEFAULT_METRICS_URL)
    parser.add_argument("--admin-url", default=DEFAULT_ADMIN_URL)
    parser.add_argument("--dashboard-status-url", default=DEFAULT_DASHBOARD_STATUS_URL)
    parser.add_argument("--candidates-json", help="JSON list of candidate objects; omit for the conservative built-in grid")
    parser.add_argument("--output-dir")
    parser.add_argument("--duration-seconds", type=float, default=900)
    parser.add_argument("--warmup-seconds", type=float, default=300)
    parser.add_argument("--sample-interval-seconds", type=float, default=30)
    parser.add_argument("--apply", action="store_true", help="Apply candidates through runtime-admin endpoints. Default is observe-only.")
    parser.add_argument("--yes", action="store_true", help="Required with --apply.")
    parser.add_argument("--revert", action="store_true", default=True)
    parser.add_argument("--no-revert", action="store_false", dest="revert")
    parser.add_argument("--stop-on-abort", action="store_true", default=True)
    parser.add_argument("--continue-on-abort", action="store_false", dest="stop_on_abort")
    parser.add_argument("--revert-template-ttl-ms", type=int, default=1000)
    parser.add_argument("--revert-block-candidate-job-age-ms", type=int, default=1200)
    parser.add_argument("--revert-vardiff-target-share-seconds", type=float, default=3.0)
    parser.add_argument("--revert-vardiff-window-seconds", type=int, default=60)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = run_harness(args)
    print(json.dumps({key: payload[key] for key in ("summary_path", "events_path", "report_path")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
