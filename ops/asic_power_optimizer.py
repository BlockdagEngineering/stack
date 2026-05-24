#!/usr/bin/env python3
"""Safely tune X100 ASIC manual power plans one miner at a time.

The optimizer intentionally changes only one ASIC at a time. It backs up each
device's current /mcb/setting payload, applies a candidate manualPowerplan,
then observes both ASIC-side telemetry and pool-side useful work before moving
to the next step.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "ops") not in sys.path:
    sys.path.insert(0, str(ROOT / "ops"))

from pool_ops import (  # noqa: E402
    MinerAPIError,
    get_miner_cgminer_devs,
    get_miner_status,
    is_lan_ipv4,
    miner_login,
    miner_put_auth,
    miner_request,
    now_iso,
    parse_pool_activity,
    read_miner_admin_password,
    read_miner_registry,
    restart_miner,
    restart_miner_open,
)


RUNTIME_DIR = ROOT / "ops" / "runtime"
POOL_CONTAINER = os.environ.get("BDAG_POOL_CONTAINER", "asic-pool")
DEFAULT_STEPS = [
    (800, "0.77"),
    (825, "0.79"),
    (850, "0.81"),
    (875, "0.83"),
    (900, "0.85"),
]
POWERPLAN_RE = re.compile(
    r"(?P<freq>[0-9.]+)\s*MHz\s+(?P<volts>[0-9.]+)\s*V\s+(?P<fan1>[0-9.]+)\s*RPM\s+(?P<fan2>[0-9.]+)\s*RPM",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PowerStep:
    freq_mhz: int
    volts: str

    @property
    def label(self) -> str:
        return f"{self.freq_mhz}MHz-{self.volts}V"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


def local_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    match = re.search(r"-?[0-9]+(?:\.[0-9]+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def as_int(value: Any) -> int | None:
    parsed = as_float(value)
    return int(parsed) if parsed is not None else None


def plan_text(freq_mhz: int, volts: str, current: str | None) -> str:
    fan1 = "15"
    fan2 = "9.4"
    if current:
        match = POWERPLAN_RE.search(current)
        if match:
            fan1 = match.group("fan1")
            fan2 = match.group("fan2")
    return f"{freq_mhz} MHz {volts} V {fan1} RPM {fan2} RPM"


def current_setting(ip: str) -> dict[str, Any]:
    response = miner_request(ip, "/mcb/setting", timeout=4.0)
    body = response.get("body")
    if not isinstance(body, dict):
        raise MinerAPIError(f"{ip} returned invalid /mcb/setting payload")
    return body


def apply_powerplan(ip: str, password: str, freq_mhz: int, volts: str, base_setting: dict[str, Any]) -> dict[str, Any]:
    payload = dict(base_setting)
    payload["manual"] = True
    payload["manualPowerplan"] = plan_text(freq_mhz, volts, str(base_setting.get("manualPowerplan") or ""))
    payload["select"] = int(payload.get("select", 0) or 0)
    if not password:
        return miner_request(ip, "/mcb/setting", method="PUT", payload=payload)
    token = miner_login(ip, password)
    return miner_put_auth(ip, "/mcb/setting", payload, token)


def restore_setting(ip: str, password: str, setting: dict[str, Any]) -> dict[str, Any]:
    if not password:
        return miner_request(ip, "/mcb/setting", method="PUT", payload=dict(setting))
    token = miner_login(ip, password)
    return miner_put_auth(ip, "/mcb/setting", dict(setting), token)


def restart_miner_process(ip: str, password: str) -> dict[str, Any]:
    if not password:
        return restart_miner_open(ip)
    return restart_miner(ip, password)


def probe(ip: str) -> dict[str, Any]:
    status = get_miner_status(ip, timeout=3.0)
    devs = get_miner_cgminer_devs(ip, timeout=3.0)
    setting = current_setting(ip)
    return {
        "timestamp": utc_iso(),
        "status": {
            "model": status.get("model"),
            "hardware": status.get("hardware"),
            "mcbversion": status.get("mcbversion"),
            "firmware": status.get("firmware"),
        },
        "setting": {
            "manual": setting.get("manual"),
            "select": setting.get("select"),
            "manualPowerplan": setting.get("manualPowerplan"),
        },
        "devs": {
            "hashrate": as_float(devs.get("hashrate")),
            "av_hashrate": as_float(devs.get("av_hashrate")),
            "temp_c": as_float(devs.get("temp")),
            "fanspeed": devs.get("fanspeed"),
            "accepted": as_int(devs.get("accepted")) or 0,
            "rejected": as_int(devs.get("rejected")) or 0,
            "hwerrors": as_int(devs.get("hwerrors")) or 0,
            "hwerr_ratio": as_float(devs.get("hwerr_ration")),
            "valid": as_int(devs.get("valid")),
            "uptime_seconds": as_int(devs.get("time")),
            "powerplan": devs.get("powerplan"),
            "minerstatus": devs.get("minerstatus"),
        },
    }


def docker_logs_between(start: str, end: str) -> str:
    command = ["docker", "logs", "--since", start, "--until", end, POOL_CONTAINER]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    return "\n".join(part for part in (proc.stdout, proc.stderr) if part)


def dashboard_status() -> dict[str, Any]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8088/api/status", timeout=20.0) as response:
            body = response.read(2_000_000).decode("utf-8", "replace")
        payload = json.loads(body)
        return {
            "overall": payload.get("overall"),
            "status_reason": payload.get("status_reason") or "",
            "generated_at": payload.get("generated_at") or "",
        }
    except Exception as exc:  # noqa: BLE001 - caller decides if this is fatal.
        return {"overall": "unknown", "status_reason": str(exc), "generated_at": ""}


def wait_for_stack_ok(run_dir: Path, events: Path, timeout_seconds: int) -> bool:
    started = time.monotonic()
    last_log = 0.0
    while True:
        if should_stop(run_dir):
            append_jsonl(events, {"time": utc_iso(), "event": "stack-wait-stopped"})
            return False
        status = dashboard_status()
        if status.get("overall") == "ok":
            append_jsonl(events, {"time": utc_iso(), "event": "stack-ok", "status": status})
            return True
        now = time.monotonic()
        if now - last_log >= 60 or last_log == 0:
            append_jsonl(events, {"time": utc_iso(), "event": "stack-not-ok-waiting", "status": status})
            last_log = now
        if timeout_seconds and now - started > timeout_seconds:
            append_jsonl(events, {"time": utc_iso(), "event": "stack-ok-timeout", "status": status})
            return False
        time.sleep(20)


def pool_window_for_miner(ip: str, start: str, end: str) -> dict[str, Any]:
    text = docker_logs_between(start, end)
    activity = parse_pool_activity(text)
    for item in activity.get("miners", []):
        if str(item.get("ip") or "") == ip:
            return {
                "jobs": int(item.get("jobs", 0) or 0),
                "submits": int(item.get("submits", 0) or 0),
                "shares": int(item.get("shares", 0) or 0),
                "share_work": int(item.get("share_work", 0) or 0),
                "share_difficulty": item.get("share_difficulty", 0),
                "blocks_found": int(item.get("blocks_found", 0) or 0),
                "last_share_at": item.get("last_share_at"),
                "last_block_at": item.get("last_block_at"),
            }
    return {
        "jobs": 0,
        "submits": 0,
        "shares": 0,
        "share_work": 0,
        "share_difficulty": 0,
        "blocks_found": 0,
        "last_share_at": None,
        "last_block_at": None,
    }


def counter_delta(before: dict[str, Any], after: dict[str, Any], key: str) -> int:
    a = int((before.get("devs") or {}).get(key) or 0)
    b = int((after.get("devs") or {}).get(key) or 0)
    return b - a if b >= a else b


def summarize_samples(samples: list[dict[str, Any]], before: dict[str, Any], end_probe: dict[str, Any], pool: dict[str, Any], seconds: float) -> dict[str, Any]:
    temps = [float((s.get("devs") or {}).get("temp_c")) for s in samples if (s.get("devs") or {}).get("temp_c") is not None]
    hashes = [float((s.get("devs") or {}).get("hashrate")) for s in samples if (s.get("devs") or {}).get("hashrate") is not None]
    av_hashes = [float((s.get("devs") or {}).get("av_hashrate")) for s in samples if (s.get("devs") or {}).get("av_hashrate") is not None]
    accepted_delta = counter_delta(before, end_probe, "accepted")
    rejected_delta = counter_delta(before, end_probe, "rejected")
    hw_delta = counter_delta(before, end_probe, "hwerrors")
    reject_ratio = rejected_delta / max(1, accepted_delta + rejected_delta)
    return {
        "seconds": round(seconds, 3),
        "sample_count": len(samples),
        "hashrate_avg_mhs": round(sum(hashes) / len(hashes), 3) if hashes else None,
        "hashrate_max_mhs": round(max(hashes), 3) if hashes else None,
        "av_hashrate_end_mhs": (end_probe.get("devs") or {}).get("av_hashrate"),
        "av_hashrate_avg_mhs": round(sum(av_hashes) / len(av_hashes), 3) if av_hashes else None,
        "temp_max_c": round(max(temps), 2) if temps else None,
        "temp_avg_c": round(sum(temps) / len(temps), 2) if temps else None,
        "accepted_delta": accepted_delta,
        "rejected_delta": rejected_delta,
        "reject_delta_ratio": round(reject_ratio, 5),
        "hwerrors_delta": hw_delta,
        "hwerr_ratio_end": (end_probe.get("devs") or {}).get("hwerr_ratio"),
        "valid_end": (end_probe.get("devs") or {}).get("valid"),
        "uptime_end_seconds": (end_probe.get("devs") or {}).get("uptime_seconds"),
        "pool": pool,
        "pool_share_work_per_second": round(int(pool.get("share_work", 0) or 0) / max(1.0, seconds), 3),
        "pool_blocks_per_second": round(int(pool.get("blocks_found", 0) or 0) / max(1.0, seconds), 6),
    }


def stability_reasons(
    summary: dict[str, Any],
    baseline: dict[str, Any],
    max_temp_c: float,
    max_reject_ratio: float,
    max_hwerr_ratio: float,
) -> list[str]:
    reasons: list[str] = []
    temp = summary.get("temp_max_c")
    if temp is None:
        reasons.append("no-temperature-telemetry")
    elif float(temp) > max_temp_c:
        reasons.append(f"temp>{max_temp_c}C")
    if summary.get("valid_end") is not None and int(summary["valid_end"]) < 6:
        reasons.append("valid-chip-count-below-6")
    if int(summary.get("accepted_delta") or 0) <= 0:
        reasons.append("no-accepted-work-during-observation")
    if float(summary.get("reject_delta_ratio") or 0) > max_reject_ratio:
        reasons.append(f"reject-ratio>{max_reject_ratio}")
    hw_end = summary.get("hwerr_ratio_end")
    baseline_hw = (baseline.get("devs") or {}).get("hwerr_ratio") or 0
    dynamic_hw_limit = min(max_hwerr_ratio, max(0.012, float(baseline_hw) * 2.5 + 0.002))
    if hw_end is not None and float(hw_end) > dynamic_hw_limit:
        reasons.append(f"hwerr-ratio>{dynamic_hw_limit:.4f}")
    if int((summary.get("pool") or {}).get("shares", 0) or 0) <= 0:
        reasons.append("no-pool-accepted-share-lines")
    return reasons


def score(summary: dict[str, Any]) -> float:
    pool = summary.get("pool") or {}
    share_work_rate = float(summary.get("pool_share_work_per_second") or 0)
    hash_rate = float(summary.get("hashrate_avg_mhs") or 0)
    blocks = float(pool.get("blocks_found") or 0)
    return share_work_rate + (hash_rate * 1000.0) + (blocks * 100000.0)


def build_report(run_dir: Path, state: dict[str, Any]) -> None:
    rows = []
    for result in state.get("results", []):
        steps = result.get("steps", [])
        best = result.get("best_step") or {}
        rows.append(
            "<tr>"
            f"<td>{html.escape(result.get('name', ''))}</td>"
            f"<td>{html.escape(result.get('ip', ''))}</td>"
            f"<td>{html.escape(str(best.get('manualPowerplan') or 'unchanged'))}</td>"
            f"<td>{html.escape(result.get('status', ''))}</td>"
            f"<td>{len(steps)}</td>"
            f"<td>{html.escape('; '.join(result.get('notes', [])))}</td>"
            "</tr>"
        )
    events_path = run_dir / "events.jsonl"
    summary_path = run_dir / "summary.json"
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ASIC Power Optimization Report</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 0; background: #101418; color: #e8edf2; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px; }}
    h1 {{ margin: 0 0 8px; font-size: 30px; }}
    .sub {{ color: #9fb0c0; margin-bottom: 22px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin: 18px 0; }}
    .card {{ border: 1px solid #2d3945; border-radius: 8px; padding: 14px; background: #151c23; }}
    .label {{ color: #91a3b5; font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
    .value {{ font-size: 20px; margin-top: 6px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 18px; background: #151c23; border-radius: 8px; overflow: hidden; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #26323d; text-align: left; vertical-align: top; }}
    th {{ color: #a9bac9; font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
    code {{ color: #8bd5ff; }}
  </style>
  <script type="application/json" id="agent-metadata">{html.escape(json.dumps({"summary_json": str(summary_path), "events_jsonl": str(events_path)}, sort_keys=True))}</script>
</head>
<body>
<main>
  <h1>ASIC Power Optimization Report</h1>
  <div class="sub">Started {html.escape(state.get('started_at', ''))}. One miner at a time; each accepted step was kept only if telemetry and pool useful work stayed stable.</div>
  <div class="grid">
    <div class="card"><div class="label">Run Directory</div><div class="value"><code>{html.escape(str(run_dir))}</code></div></div>
    <div class="card"><div class="label">Status</div><div class="value">{html.escape(state.get('status', ''))}</div></div>
    <div class="card"><div class="label">Miners</div><div class="value">{len(state.get('miners', []))}</div></div>
    <div class="card"><div class="label">Completed</div><div class="value">{len(state.get('results', []))}</div></div>
  </div>
  <table>
    <thead><tr><th>Miner</th><th>IP</th><th>Kept Setting</th><th>Status</th><th>Steps Tested</th><th>Notes</th></tr></thead>
    <tbody>{''.join(rows) if rows else '<tr><td colspan="6">No miner completed yet.</td></tr>'}</tbody>
  </table>
  <p>Detailed machine-readable data: <code>{html.escape(str(summary_path))}</code> and <code>{html.escape(str(events_path))}</code>.</p>
</main>
</body>
</html>
"""
    (run_dir / "report.html").write_text(html_text, encoding="utf-8")


def load_miners(target_names: set[str] | None = None) -> list[dict[str, Any]]:
    registry = read_miner_registry()
    miners = []
    for item in registry.get("miners", []):
        ip = str(item.get("ip") or "")
        name = str(item.get("display_name") or ip)
        if not is_lan_ipv4(ip):
            continue
        if target_names and name not in target_names and ip not in target_names:
            continue
        miners.append(
            {
                "name": name,
                "ip": ip,
                "mac": item.get("mac"),
                "worker": item.get("expected_worker_user"),
            }
        )
    miners.sort(key=lambda row: tuple(int(part) for part in row["ip"].split(".")))
    return miners


def parse_steps(text: str) -> list[PowerStep]:
    steps: list[PowerStep] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            freq, volts = part.split(":", 1)
            steps.append(PowerStep(int(freq.strip()), volts.strip()))
        else:
            freq = int(part)
            match = next((item for item in DEFAULT_STEPS if item[0] == freq), None)
            if not match:
                raise ValueError(f"no default voltage for step {freq}")
            steps.append(PowerStep(match[0], match[1]))
    return steps


def should_stop(run_dir: Path) -> bool:
    return (run_dir / "STOP").exists()


def optimize_miner(
    miner: dict[str, Any],
    password: str,
    steps: list[PowerStep],
    run_dir: Path,
    settle_seconds: int,
    observe_seconds: int,
    sample_interval: int,
    max_temp_c: float,
    max_reject_ratio: float,
    max_hwerr_ratio: float,
    require_stack_ok: bool,
    stack_ok_timeout: int,
    restart_after_apply: bool,
    restart_settle_seconds: int,
    dry_run: bool,
) -> dict[str, Any]:
    ip = miner["ip"]
    name = miner["name"]
    events = run_dir / "events.jsonl"
    notes: list[str] = []
    result: dict[str, Any] = {"name": name, "ip": ip, "steps": [], "notes": notes, "status": "running"}

    backup_dir = run_dir / "settings-backups"
    backup_dir.mkdir(exist_ok=True)
    original_setting = current_setting(ip)
    write_json(backup_dir / f"{name}-{ip}-setting-original.json", original_setting)
    baseline = probe(ip)
    write_json(backup_dir / f"{name}-{ip}-baseline-probe.json", baseline)
    best_setting = dict(original_setting)
    best_summary = {
        "hashrate_avg_mhs": (baseline.get("devs") or {}).get("hashrate"),
        "pool_share_work_per_second": 0,
        "pool_blocks_per_second": 0,
    }
    best_score = score(best_summary)
    result["baseline"] = baseline
    result["best_step"] = {
        "manualPowerplan": original_setting.get("manualPowerplan"),
        "manual": original_setting.get("manual"),
        "reason": "original",
    }

    append_jsonl(events, {"time": utc_iso(), "event": "miner-start", "miner": name, "ip": ip, "baseline": baseline})

    if dry_run:
        result["status"] = "dry-run"
        return result

    for step in steps:
        if should_stop(run_dir):
            notes.append("STOP file detected; restoring best known setting")
            break
        if require_stack_ok and not wait_for_stack_ok(run_dir, events, stack_ok_timeout):
            notes.append("stack did not reach dashboard overall=ok before applying next step")
            break
        step_start = utc_iso()
        before = probe(ip)
        candidate_setting = dict(best_setting)
        candidate_setting["manual"] = True
        candidate_setting["manualPowerplan"] = plan_text(step.freq_mhz, step.volts, str(best_setting.get("manualPowerplan") or ""))
        append_jsonl(
            events,
            {
                "time": utc_iso(),
                "event": "apply-step",
                "miner": name,
                "ip": ip,
                "step": step.label,
                "manualPowerplan": candidate_setting["manualPowerplan"],
            },
        )
        apply_powerplan(ip, password, step.freq_mhz, step.volts, best_setting)
        if restart_after_apply:
            append_jsonl(
                events,
                {
                    "time": utc_iso(),
                    "event": "restart-after-apply",
                    "miner": name,
                    "ip": ip,
                    "step": step.label,
                },
            )
            restart_miner_process(ip, password)
            time.sleep(restart_settle_seconds)
        else:
            time.sleep(settle_seconds)

        samples: list[dict[str, Any]] = []
        failed_reason: str | None = None
        last_stack_check = 0.0
        observe_start_mono = time.monotonic()
        while time.monotonic() - observe_start_mono < observe_seconds:
            if should_stop(run_dir):
                failed_reason = "STOP file detected"
                break
            try:
                if require_stack_ok and time.monotonic() - last_stack_check >= 60:
                    stack = dashboard_status()
                    last_stack_check = time.monotonic()
                    if stack.get("overall") == "unknown":
                        append_jsonl(
                            events,
                            {
                                "time": utc_iso(),
                                "event": "stack-status-probe-warning",
                                "miner": name,
                                "ip": ip,
                                "step": step.label,
                                "status": stack,
                            },
                        )
                    elif stack.get("overall") not in {"ok", None}:
                        failed_reason = f"stack health changed during test: {stack.get('overall')} {stack.get('status_reason')}"
                        break
                sample = probe(ip)
                samples.append(sample)
                devs = sample.get("devs") or {}
                temp = devs.get("temp_c")
                if temp is not None and float(temp) > max_temp_c:
                    failed_reason = f"temperature exceeded {max_temp_c}C"
                    break
                if devs.get("valid") is not None and int(devs.get("valid")) < 6:
                    failed_reason = "valid chip count fell below 6"
                    break
            except Exception as exc:  # noqa: BLE001 - a lost API is instability.
                failed_reason = f"telemetry probe failed: {exc}"
                break
            time.sleep(sample_interval)

        step_end = utc_iso()
        end_probe = samples[-1] if samples else probe(ip)
        pool = pool_window_for_miner(ip, step_start, step_end)
        summary = summarize_samples(samples, before, end_probe, pool, max(1.0, time.monotonic() - observe_start_mono))
        reasons = stability_reasons(summary, baseline, max_temp_c, max_reject_ratio, max_hwerr_ratio)
        if failed_reason:
            reasons.insert(0, failed_reason)
        stable = not reasons
        step_record = {
            "step": step.label,
            "manualPowerplan": candidate_setting["manualPowerplan"],
            "started_at": step_start,
            "finished_at": step_end,
            "stable": stable,
            "reasons": reasons,
            "summary": summary,
        }
        result["steps"].append(step_record)
        append_jsonl(events, {"time": utc_iso(), "event": "step-result", "miner": name, "ip": ip, **step_record})

        if not stable:
            notes.append(f"{step.label} rejected: {', '.join(reasons)}")
            restore_setting(ip, password, best_setting)
            if restart_after_apply:
                append_jsonl(
                    events,
                    {
                        "time": utc_iso(),
                        "event": "restart-after-reject-restore",
                        "miner": name,
                        "ip": ip,
                        "step": step.label,
                    },
                )
                restart_miner_process(ip, password)
                time.sleep(restart_settle_seconds)
            else:
                time.sleep(settle_seconds)
            break

        candidate_score = score(summary)
        current_hash = float(summary.get("hashrate_avg_mhs") or 0)
        best_hash = float(best_summary.get("hashrate_avg_mhs") or 0)
        if candidate_score >= best_score * 0.98 or current_hash >= best_hash * 1.01:
            best_setting = candidate_setting
            best_score = candidate_score
            best_summary = summary
            result["best_step"] = {
                "manual": True,
                "manualPowerplan": candidate_setting["manualPowerplan"],
                "score": best_score,
                "summary": best_summary,
                "reason": "stable-best-so-far",
            }
            append_jsonl(events, {"time": utc_iso(), "event": "keep-step", "miner": name, "ip": ip, "step": step.label})
        else:
            notes.append(f"{step.label} stable but not better than prior best; restoring prior best")
            restore_setting(ip, password, best_setting)
            if restart_after_apply:
                append_jsonl(
                    events,
                    {
                        "time": utc_iso(),
                        "event": "restart-after-not-better-restore",
                        "miner": name,
                        "ip": ip,
                        "step": step.label,
                    },
                )
                restart_miner_process(ip, password)
                time.sleep(restart_settle_seconds)
            else:
                time.sleep(settle_seconds)
            break

    restore_setting(ip, password, best_setting)
    if restart_after_apply:
        append_jsonl(events, {"time": utc_iso(), "event": "restart-after-final-restore", "miner": name, "ip": ip})
        restart_miner_process(ip, password)
        time.sleep(restart_settle_seconds)
    result["status"] = "completed"
    append_jsonl(events, {"time": utc_iso(), "event": "miner-finished", "miner": name, "ip": ip, "best": result.get("best_step")})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", default="800,825,850,875,900", help="Comma list, e.g. 800,825 or 800:0.77,825:0.79")
    parser.add_argument("--miner", action="append", default=[], help="Miner display name or IP to tune. Repeatable. Default: all live miners.")
    parser.add_argument("--settle-seconds", type=int, default=45)
    parser.add_argument("--observe-seconds", type=int, default=180)
    parser.add_argument("--sample-interval", type=int, default=15)
    parser.add_argument("--max-temp-c", type=float, default=78.0)
    parser.add_argument("--max-reject-ratio", type=float, default=0.20)
    parser.add_argument("--max-hwerr-ratio", type=float, default=0.03)
    parser.add_argument("--no-stack-health-gate", action="store_true", help="Do not require dashboard overall=ok before and during each step.")
    parser.add_argument("--stack-ok-timeout", type=int, default=0, help="Seconds to wait for dashboard overall=ok. Default 0 waits indefinitely.")
    parser.add_argument("--restart-after-apply", action="store_true", help="Restart the ASIC miner process after applying each manual profile.")
    parser.add_argument("--restart-settle-seconds", type=int, default=150, help="Seconds to wait after a miner-process restart before observing.")
    parser.add_argument("--dry-run", action="store_true", help="Only probe and back up settings; do not change miners.")
    args = parser.parse_args()

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = RUNTIME_DIR / f"asic-power-optimization-{local_stamp()}"
    run_dir.mkdir()
    active = RUNTIME_DIR / "active-asic-power-optimization"
    if active.exists() or active.is_symlink():
        active.unlink()
    active.symlink_to(run_dir)
    events = run_dir / "events.jsonl"
    stop_note = run_dir / "README.STOP.txt"
    stop_note.write_text(
        f"Create {run_dir / 'STOP'} to stop after the current observation and restore the best known setting.\n",
        encoding="utf-8",
    )

    password = read_miner_admin_password()
    if not password and not args.dry_run:
        append_jsonl(
            events,
            {
                "time": utc_iso(),
                "event": "no-saved-password",
                "note": "using unauthenticated LAN /mcb/setting PUT supported by this firmware",
            },
        )

    steps = parse_steps(args.steps)
    miners = load_miners(set(args.miner) if args.miner else None)
    if not miners:
        raise SystemExit("No miners matched the requested selection.")

    state = {
        "status": "running",
        "started_at": now_iso(),
        "run_dir": str(run_dir),
        "miners": miners,
        "steps": [step.label for step in steps],
        "settings": {
            "settle_seconds": args.settle_seconds,
            "observe_seconds": args.observe_seconds,
            "sample_interval": args.sample_interval,
            "max_temp_c": args.max_temp_c,
            "max_reject_ratio": args.max_reject_ratio,
            "max_hwerr_ratio": args.max_hwerr_ratio,
            "require_stack_ok": not args.no_stack_health_gate,
            "stack_ok_timeout": args.stack_ok_timeout,
            "restart_after_apply": args.restart_after_apply,
            "restart_settle_seconds": args.restart_settle_seconds,
            "dry_run": args.dry_run,
        },
        "results": [],
    }
    write_json(run_dir / "summary.json", state)
    append_jsonl(events, {"time": utc_iso(), "event": "run-start", "run_dir": str(run_dir), "miners": miners, "steps": state["steps"]})
    build_report(run_dir, state)

    def handle_signal(signum: int, _frame: Any) -> None:
        append_jsonl(events, {"time": utc_iso(), "event": "signal", "signal": signum})
        (run_dir / "STOP").write_text(f"signal {signum} at {utc_iso()}\n", encoding="utf-8")

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        for miner in miners:
            if should_stop(run_dir):
                break
            result = optimize_miner(
                miner=miner,
                password=password or "",
                steps=steps,
                run_dir=run_dir,
                settle_seconds=args.settle_seconds,
                observe_seconds=args.observe_seconds,
                sample_interval=args.sample_interval,
                max_temp_c=args.max_temp_c,
                max_reject_ratio=args.max_reject_ratio,
                max_hwerr_ratio=args.max_hwerr_ratio,
                require_stack_ok=not args.no_stack_health_gate,
                stack_ok_timeout=args.stack_ok_timeout,
                restart_after_apply=args.restart_after_apply,
                restart_settle_seconds=args.restart_settle_seconds,
                dry_run=args.dry_run,
            )
            state["results"].append(result)
            write_json(run_dir / "summary.json", state)
            build_report(run_dir, state)
    finally:
        state["finished_at"] = now_iso()
        state["status"] = "stopped" if should_stop(run_dir) else "completed"
        write_json(run_dir / "summary.json", state)
        build_report(run_dir, state)
        append_jsonl(events, {"time": utc_iso(), "event": "run-finished", "status": state["status"]})
        latest = RUNTIME_DIR / "latest-asic-power-optimization"
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(run_dir)
        active = RUNTIME_DIR / "active-asic-power-optimization"
        if active.is_symlink() and active.resolve() == run_dir:
            active.unlink()

    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
