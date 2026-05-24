#!/usr/bin/env python3
"""Read-only miner-normalized A/B evidence harness for the BlockDAG pool."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from pool_log_summary import summarize_logs
from pool_ops import RUNTIME_DIR, now_iso, run


STATUS_URL = "http://127.0.0.1:8088/api/status"
ROUTER_URL = "http://127.0.0.1:8088/api/router"
P2P_URL = "http://127.0.0.1:8088/api/p2p"
GLOBAL_URL = "http://127.0.0.1:8088/api/global"
AB_DIR = RUNTIME_DIR / "ab-harness"
MARKERS_FILE = AB_DIR / "markers.jsonl"
STACK_CONTAINERS = ["asic-pool", "pool-db", "rpc-failover", "bdag-miner-node-1", "bdag-miner-node-2"]
DEFAULT_MIN_COMPARE_SAMPLES = 3
DEFAULT_MIN_COMPARE_SECONDS = 30.0
DEFAULT_MIN_COMPARE_MINER_HOURS = 0.01
DEFAULT_MIN_COMPARE_OK_RATIO = 0.98

PROM_LINE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eE]+)\s*$")
PROM_KEEP_PREFIXES = (
    "pool_block_submit_outcomes_total",
    "pool_rpc_backend_template_age_seconds",
    "pool_rpc_backend_template_errors_total",
    "pool_rpc_backend_template_fetch_duration_seconds_sum",
    "pool_rpc_backend_template_fetch_duration_seconds_count",
    "pool_rpc_backend_submit_total",
    "pool_rpc_backend_submit_duration_seconds_sum",
    "pool_rpc_backend_submit_duration_seconds_count",
	"pool_rpc_backend_switches_total",
	"pool_jobs_marked_stale_total",
    "pool_template_broadcasts_total",
    "pool_template_fanin_",
    "pool_expired_job_recoveries_total",
    "pool_stale_job_race_recoveries_total",
    "pool_duplicate_block_candidates_rejected_local_total",
    "pool_stale_block_candidates",
)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_z(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_duration(value: str) -> float:
    raw = str(value).strip().lower()
    if not raw:
        raise argparse.ArgumentTypeError("empty duration")
    multipliers = {"s": 1, "m": 60, "h": 3600}
    suffix = raw[-1]
    if suffix in multipliers:
        number = raw[:-1]
        try:
            return float(number) * multipliers[suffix]
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid duration {value!r}") from exc
    try:
        return float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid duration {value!r}") from exc


def fetch_json(url: str, timeout: float = 4.0) -> tuple[dict[str, Any], str]:
    try:
        req = urllib.request.Request(url, headers={"accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")), ""
    except Exception as exc:  # noqa: BLE001 - harness should keep sampling.
        return {}, str(exc)


def fetch_text(url: str, timeout: float = 4.0) -> tuple[str, str]:
    try:
        req = urllib.request.Request(url, headers={"accept": "text/plain"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace"), ""
    except Exception as exc:  # noqa: BLE001 - harness should keep sampling.
        return "", str(exc)


def metric_key(name: str, labels: str | None) -> str:
    return f"{name}{labels or ''}"


def parse_prometheus(text: str) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = PROM_LINE_RE.match(line)
        if not match:
            continue
        name, labels, value = match.groups()
        if not name.startswith(PROM_KEEP_PREFIXES):
            continue
        try:
            metrics[metric_key(name, labels)] = float(value)
        except ValueError:
            continue
    return metrics


def pool_metrics_endpoint(status: dict[str, Any]) -> str | None:
    containers = ((status.get("pool_metrics") or {}).get("containers") or {})
    info = containers.get("asic-pool") or {}
    endpoint = info.get("endpoint")
    if endpoint:
        return f"http://{endpoint}/metrics"
    return None


def docker_stats() -> dict[str, Any]:
    command = ["docker", "stats", "--no-stream", "--format", "{{json .}}", *STACK_CONTAINERS]
    result = run(command, timeout=15)
    rows: dict[str, Any] = {}
    text = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if not result.ok:
        return {"error": text[-1000:]}
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = row.get("Name") or row.get("Container")
        if name:
            rows[name] = row
    return rows


def collect_sample(variant: str, label: str, include_global: bool = False) -> dict[str, Any]:
    status, status_error = fetch_json(STATUS_URL, timeout=15)
    router, router_error = fetch_json(ROUTER_URL, timeout=6)
    p2p, p2p_error = fetch_json(P2P_URL, timeout=6)
    if include_global:
        global_state, global_error = fetch_json(GLOBAL_URL, timeout=15)
    else:
        global_state, global_error = {}, "skipped; pass --include-global"
    metrics_url = pool_metrics_endpoint(status)
    metrics_text, metrics_error = ("", "pool metrics endpoint unavailable")
    if metrics_url:
        metrics_text, metrics_error = fetch_text(metrics_url)
    return {
        "sampled_at": iso_z(utc_now()),
        "sampled_at_epoch": time.time(),
        "variant": variant,
        "label": label,
        "status": status,
        "status_error": status_error,
        "router": router,
        "router_error": router_error,
        "p2p": p2p,
        "p2p_error": p2p_error,
        "global": global_state,
        "global_error": global_error,
        "pool_metrics_url": metrics_url,
        "pool_metrics_error": metrics_error,
        "pool_metrics": parse_prometheus(metrics_text),
        "docker_stats": docker_stats(),
    }


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def connected_miners(sample: dict[str, Any]) -> int:
    return int(((sample.get("status") or {}).get("miner_health") or {}).get("connected_count") or 0)


def status_ok(sample: dict[str, Any]) -> bool:
    return (sample.get("status") or {}).get("overall") == "ok"


def metric_delta(first: dict[str, float], last: dict[str, float], contains: str) -> float:
    total = 0.0
    for key, end_value in last.items():
        if contains not in key:
            continue
        start_value = first.get(key, 0.0)
        delta = end_value - start_value
        if delta > 0:
            total += delta
    return total


def metric_delta_by_reason(first: dict[str, float], last: dict[str, float], prefix: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, end_value in last.items():
        if not key.startswith(prefix):
            continue
        start_value = first.get(key, 0.0)
        delta = end_value - start_value
        if delta > 0:
            out[key] = round(delta, 6)
    return out


def average_template_age(samples: list[dict[str, Any]]) -> dict[str, float]:
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for sample in samples:
        for key, value in (sample.get("pool_metrics") or {}).items():
            if not key.startswith("pool_rpc_backend_template_age_seconds"):
                continue
            sums[key] = sums.get(key, 0.0) + float(value)
            counts[key] = counts.get(key, 0) + 1
    return {key: round(sums[key] / counts[key], 6) for key in sorted(sums) if counts.get(key)}


def miner_hours(samples: list[dict[str, Any]]) -> float:
    if len(samples) < 2:
        return 0.0
    total = 0.0
    for before, after in zip(samples, samples[1:]):
        seconds = max(0.0, float(after["sampled_at_epoch"]) - float(before["sampled_at_epoch"]))
        total += connected_miners(before) * seconds
    return total / 3600.0


def safe_rate(value: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round(value / denominator, 6)


def evaluate_summary_quality(
    summary: dict[str, Any],
    *,
    min_samples: int = DEFAULT_MIN_COMPARE_SAMPLES,
    min_seconds: float = DEFAULT_MIN_COMPARE_SECONDS,
    min_miner_hours: float = DEFAULT_MIN_COMPARE_MINER_HOURS,
    min_ok_ratio: float = DEFAULT_MIN_COMPARE_OK_RATIO,
) -> tuple[bool, list[str]]:
    flags: list[str] = []
    samples_included = int(summary.get("samples_included") or 0)
    measured_seconds = float(summary.get("measured_seconds") or 0)
    measured_miner_hours = float(summary.get("miner_hours") or 0)
    ok_ratio = float(summary.get("ok_sample_ratio") or 0)
    connected_min = int(summary.get("connected_miners_min") or 0)
    connected_max = int(summary.get("connected_miners_max") or 0)

    if samples_included < min_samples:
        flags.append(f"samples_included<{min_samples}")
    if measured_seconds < min_seconds:
        flags.append(f"measured_seconds<{min_seconds:g}")
    if measured_miner_hours < min_miner_hours:
        flags.append(f"miner_hours<{min_miner_hours:g}")
    if ok_ratio < min_ok_ratio:
        flags.append(f"ok_sample_ratio<{min_ok_ratio:g}")
    if connected_min <= 0:
        flags.append("no_connected_miners")
    if connected_min != connected_max:
        flags.append("connected_miner_count_changed")
    if not summary.get("outcome_deltas"):
        flags.append("missing_pool_outcome_metrics")

    return not flags, flags


def summarize_run(run_dir: Path, variant: str, label: str, warmup_seconds: float) -> dict[str, Any]:
    samples = load_jsonl(run_dir / "samples.jsonl")
    if not samples:
        raise RuntimeError("no samples captured")
    first_epoch = float(samples[0]["sampled_at_epoch"])
    included = [s for s in samples if float(s["sampled_at_epoch"]) >= first_epoch + warmup_seconds]
    if len(included) < 2:
        included = samples
    first_metrics = included[0].get("pool_metrics") or {}
    last_metrics = included[-1].get("pool_metrics") or {}
    seconds = float(included[-1]["sampled_at_epoch"]) - float(included[0]["sampled_at_epoch"])
    mh = miner_hours(included)
    accepted = metric_delta(first_metrics, last_metrics, 'pool_block_submit_outcomes_total{outcome="accepted"')
    rejected = metric_delta(first_metrics, last_metrics, 'pool_block_submit_outcomes_total{outcome="rejected"')
    rejected_local = metric_delta(first_metrics, last_metrics, 'pool_block_submit_outcomes_total{outcome="rejected-local"')
    submit_ok = metric_delta(first_metrics, last_metrics, 'pool_rpc_backend_submit_total')
    switches = metric_delta(first_metrics, last_metrics, "pool_rpc_backend_switches_total")
    duplicate_local = metric_delta(first_metrics, last_metrics, "pool_duplicate_block_candidates_rejected_local_total")
    expired_recoveries = metric_delta(first_metrics, last_metrics, "pool_expired_job_recoveries_total")
    log_summary = summarize_logs(included[0]["sampled_at"], included[-1]["sampled_at"])
    ok_samples = sum(1 for sample in included if status_ok(sample))
    connected_values = [connected_miners(sample) for sample in included]
    summary = {
        "generated_at": now_iso(),
        "run_dir": str(run_dir),
        "variant": variant,
        "label": label,
        "warmup_seconds": warmup_seconds,
        "samples_total": len(samples),
        "samples_included": len(included),
        "start_utc": included[0]["sampled_at"],
        "end_utc": included[-1]["sampled_at"],
        "measured_seconds": round(seconds, 3),
        "miner_hours": round(mh, 6),
        "ok_sample_ratio": round(ok_samples / max(1, len(included)), 6),
        "connected_miners_min": min(connected_values) if connected_values else 0,
        "connected_miners_max": max(connected_values) if connected_values else 0,
        "connected_miners_avg": round(sum(connected_values) / max(1, len(connected_values)), 3),
        "accepted_blocks": round(accepted, 6),
        "accepted_blocks_per_miner_hour": safe_rate(accepted, mh),
        "submit_total_delta": round(submit_ok, 6),
        "submit_total_per_miner_hour": safe_rate(submit_ok, mh),
        "rejected_submit_outcomes": round(rejected, 6),
        "rejected_local_submit_outcomes": round(rejected_local, 6),
        "rejected_per_accepted": safe_rate(rejected, accepted),
        "rejected_local_per_accepted": safe_rate(rejected_local, accepted),
        "duplicate_local_delta": round(duplicate_local, 6),
        "duplicate_local_per_accepted": safe_rate(duplicate_local, accepted),
        "backend_switches": round(switches, 6),
        "expired_job_recoveries": round(expired_recoveries, 6),
        "avg_template_age_seconds": average_template_age(included),
        "outcome_deltas": metric_delta_by_reason(first_metrics, last_metrics, "pool_block_submit_outcomes_total"),
        "backend_submit_deltas": metric_delta_by_reason(first_metrics, last_metrics, "pool_rpc_backend_submit_total"),
        "switch_deltas": metric_delta_by_reason(first_metrics, last_metrics, "pool_rpc_backend_switches_total"),
        "log_summary": log_summary,
    }
    eligible, flags = evaluate_summary_quality(summary)
    summary["eligible_for_compare"] = eligible
    summary["quality_flags"] = flags
    return summary


def html_escape(value: Any) -> str:
    text = str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def render_summary_html(summary: dict[str, Any]) -> str:
    cards = [
        ("Variant", summary["variant"]),
        ("Compare Eligible", "yes" if summary.get("eligible_for_compare") else "no"),
        ("Miner Hours", summary["miner_hours"]),
        ("Accepted / Miner Hour", summary["accepted_blocks_per_miner_hour"]),
        ("Rejects / Accepted", summary["rejected_per_accepted"]),
        ("Local Rejects / Accepted", summary["rejected_local_per_accepted"]),
        ("Backend Switches", summary["backend_switches"]),
    ]
    rows = "\n".join(
        f"<tr><td>{html_escape(key)}</td><td><code>{html_escape(value)}</code></td></tr>"
        for key, value in summary.items()
        if key not in {"log_summary", "outcome_deltas", "backend_submit_deltas", "switch_deltas", "avg_template_age_seconds"}
    )
    card_html = "\n".join(
        f"<article class='card'><div class='label'>{html_escape(label)}</div><div class='value'>{html_escape(value)}</div></article>"
        for label, value in cards
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Miner-Normalized A/B Run - {html_escape(summary['variant'])}</title>
  <style>
    body {{ margin:0; background:#0d1117; color:#eef3f8; font:14px/1.55 system-ui,sans-serif; }}
    main {{ max-width:1180px; margin:0 auto; padding:28px 24px 56px; }}
    h1 {{ margin:0 0 8px; }}
    .muted {{ color:#a8b3c4; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin:18px 0; }}
    .card {{ background:#151b24; border:1px solid #303b4d; border-radius:8px; padding:14px; }}
    .label {{ color:#a8b3c4; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
    .value {{ font-size:22px; font-weight:750; margin-top:4px; }}
    table {{ width:100%; border-collapse:collapse; background:#151b24; border:1px solid #303b4d; }}
    td, th {{ border-bottom:1px solid #303b4d; padding:8px 10px; vertical-align:top; }}
    th {{ background:#1d2633; text-align:left; }}
    code, pre {{ background:#090d13; color:#d7f5ff; border:1px solid #263142; border-radius:6px; }}
    code {{ padding:1px 5px; }}
    pre {{ padding:12px; overflow:auto; }}
  </style>
  <script type="application/json" id="agent-metadata">{json.dumps(summary, sort_keys=True)}</script>
</head>
<body>
<main>
  <h1>Miner-Normalized A/B Run</h1>
  <p class="muted">Read-only evidence capture. Raw totals are context; headline rates are normalized by connected miner-hours.</p>
  <section class="grid">{card_html}</section>
  <h2>Summary</h2>
  <table><tbody>{rows}</tbody></table>
  <h2>Outcome Deltas</h2>
  <pre>{html_escape(json.dumps(summary.get('outcome_deltas', {}), indent=2, sort_keys=True))}</pre>
  <h2>Quality Flags</h2>
  <pre>{html_escape(json.dumps(summary.get('quality_flags', []), indent=2, sort_keys=True))}</pre>
  <h2>Template Age</h2>
  <pre>{html_escape(json.dumps(summary.get('avg_template_age_seconds', {}), indent=2, sort_keys=True))}</pre>
  <h2>Log Summary</h2>
  <pre>{html_escape(json.dumps(summary.get('log_summary', {}), indent=2, sort_keys=True))}</pre>
</main>
</body>
</html>
"""


def command_mark(args: argparse.Namespace) -> int:
    payload = {
        "created_at": now_iso(),
        "created_at_utc": iso_z(utc_now()),
        "variant": args.variant,
        "label": args.label,
        "note": args.note,
    }
    append_jsonl(MARKERS_FILE, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def command_sample(args: argparse.Namespace) -> int:
    started = utc_now()
    stamp = started.strftime("%Y%m%d-%H%M%S")
    safe_variant = re.sub(r"[^A-Za-z0-9_.-]+", "-", args.variant).strip("-") or "variant"
    run_dir = AB_DIR / f"{stamp}-{safe_variant}"
    run_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.duration
    count = 0
    while True:
        sample = collect_sample(args.variant, args.label, args.include_global)
        append_jsonl(run_dir / "samples.jsonl", sample)
        count += 1
        if args.once or time.monotonic() >= deadline:
            break
        sleep_for = min(args.interval, max(0.0, deadline - time.monotonic()))
        if sleep_for <= 0:
            break
        time.sleep(sleep_for)
    summary = summarize_run(run_dir, args.variant, args.label, args.warmup)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    (run_dir / "summary.html").write_text(render_summary_html(summary), encoding="utf-8")
    (AB_DIR / "latest-run.txt").write_text(str(run_dir) + "\n", encoding="utf-8")
    print(json.dumps({"run_dir": str(run_dir), "samples": count, "summary": summary}, indent=2, sort_keys=True))
    return 0


def load_summary(path: str) -> dict[str, Any]:
    p = Path(path)
    if p.is_dir():
        p = p / "summary.json"
    return json.loads(p.read_text(encoding="utf-8"))


def command_compare(args: argparse.Namespace) -> int:
    summaries = [load_summary(path) for path in args.runs]
    baseline = summaries[0] if summaries else {}
    rows: list[dict[str, Any]] = []
    fields = [
        "accepted_blocks_per_miner_hour",
        "submit_total_per_miner_hour",
        "rejected_per_accepted",
        "rejected_local_per_accepted",
        "duplicate_local_per_accepted",
        "backend_switches",
        "ok_sample_ratio",
    ]
    thresholds = {
        "min_samples": args.min_samples,
        "min_seconds": args.min_seconds,
        "min_miner_hours": args.min_miner_hours,
        "min_ok_ratio": args.min_ok_ratio,
    }
    for summary in summaries:
        eligible, flags = evaluate_summary_quality(
            summary,
            min_samples=args.min_samples,
            min_seconds=args.min_seconds,
            min_miner_hours=args.min_miner_hours,
            min_ok_ratio=args.min_ok_ratio,
        )
        summary["eligible_for_compare"] = eligible
        summary["quality_flags"] = flags
        row = {
            "variant": summary.get("variant"),
            "run_dir": summary.get("run_dir"),
            "eligible_for_compare": eligible,
            "quality_flags": flags,
            "samples_included": summary.get("samples_included"),
            "measured_seconds": summary.get("measured_seconds"),
            "miner_hours": summary.get("miner_hours"),
            "connected_miners_min": summary.get("connected_miners_min"),
            "connected_miners_max": summary.get("connected_miners_max"),
        }
        for field in fields:
            value = float(summary.get(field) or 0)
            base = float(baseline.get(field) or 0)
            row[field] = value
            row[field + "_delta_vs_first"] = round(value - base, 6)
            row[field + "_pct_vs_first"] = None if base == 0 else round((value - base) * 100 / base, 3)
        rows.append(row)
    ineligible = [row for row in rows if not row["eligible_for_compare"]]
    payload = {
        "generated_at": now_iso(),
        "baseline_run": baseline.get("run_dir"),
        "eligible": not ineligible,
        "quality_thresholds": thresholds,
        "rows": rows,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    if args.write_json:
        AB_DIR.mkdir(parents=True, exist_ok=True)
        json_path = AB_DIR / f"compare-{utc_now().strftime('%Y%m%d-%H%M%S')}.json"
        json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(json_path)
    if args.write_html:
        AB_DIR.mkdir(parents=True, exist_ok=True)
        path = AB_DIR / f"compare-{utc_now().strftime('%Y%m%d-%H%M%S')}.html"
        visible_cols = [
            "variant",
            "eligible_for_compare",
            "quality_flags",
            "samples_included",
            "measured_seconds",
            "miner_hours",
            "connected_miners_min",
            "connected_miners_max",
            *fields,
            "run_dir",
        ]
        html_rows = "\n".join(
            "<tr>"
            + "".join(f"<td>{html_escape(row.get(col, ''))}</td>" for col in visible_cols)
            + "</tr>"
            for row in rows
        )
        path.write_text(
            f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Miner-Normalized A/B Compare</title>
<style>body{{background:#0d1117;color:#eef3f8;font:14px/1.55 system-ui,sans-serif;margin:24px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #303b4d;padding:8px}}th{{background:#1d2633}}</style>
<script type="application/json" id="agent-metadata">{json.dumps(payload, sort_keys=True)}</script></head><body>
<h1>Miner-Normalized A/B Compare</h1><p>Ineligible rows should not be used as evidence unless explicitly allowed.</p><table><thead><tr>{''.join(f'<th>{html_escape(col)}</th>' for col in visible_cols)}</tr></thead><tbody>{html_rows}</tbody></table></body></html>""",
            encoding="utf-8",
        )
        print(path)
    if ineligible and not args.allow_ineligible:
        print("ERROR: one or more runs are ineligible for comparison; pass --allow-ineligible to override", file=sys.stderr)
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    mark = sub.add_parser("mark", help="record a timestamped comparison marker")
    mark.add_argument("--variant", required=True)
    mark.add_argument("--label", default="")
    mark.add_argument("--note", default="")
    mark.set_defaults(func=command_mark)

    sample = sub.add_parser("sample", help="capture a read-only evidence window")
    sample.add_argument("--variant", required=True)
    sample.add_argument("--label", default="")
    sample.add_argument("--duration", type=parse_duration, default=300.0)
    sample.add_argument("--interval", type=parse_duration, default=30.0)
    sample.add_argument("--warmup", type=parse_duration, default=0.0)
    sample.add_argument("--once", action="store_true", help="capture one sample and summarize immediately")
    sample.add_argument("--include-global", action="store_true", help="also query /api/global on every sample")
    sample.set_defaults(func=command_sample)

    compare = sub.add_parser("compare", help="compare summary.json files or run directories")
    compare.add_argument("runs", nargs="+")
    compare.add_argument("--write-html", action="store_true")
    compare.add_argument("--write-json", action="store_true")
    compare.add_argument("--allow-ineligible", action="store_true")
    compare.add_argument("--min-samples", type=int, default=DEFAULT_MIN_COMPARE_SAMPLES)
    compare.add_argument("--min-seconds", type=float, default=DEFAULT_MIN_COMPARE_SECONDS)
    compare.add_argument("--min-miner-hours", type=float, default=DEFAULT_MIN_COMPARE_MINER_HOURS)
    compare.add_argument("--min-ok-ratio", type=float, default=DEFAULT_MIN_COMPARE_OK_RATIO)
    compare.set_defaults(func=command_compare)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "interval", 1.0) <= 0 and not getattr(args, "once", False):
        parser.error("--interval must be positive unless --once is used")
    if getattr(args, "duration", 1.0) < 0:
        parser.error("--duration must be non-negative")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
