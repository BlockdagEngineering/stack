#!/usr/bin/env python3
"""Apply one ASIC power profile to selected miners and guard them together.

This is for short controlled soak tests where a small set of miners should be
tested at the same profile during the same wall-clock window. It backs up each
miner setting, applies the requested manualPowerplan, samples ASIC telemetry
and dashboard health, and restores all selected miners if any guard trips.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "ops") not in sys.path:
    sys.path.insert(0, str(ROOT / "ops"))

from asic_power_optimizer import (  # noqa: E402
    append_jsonl,
    as_float,
    as_int,
    build_report,
    current_setting,
    dashboard_status,
    docker_logs_between,
    local_stamp,
    parse_steps,
    plan_text,
    probe,
    restore_setting,
    write_json,
)
from pool_ops import (  # noqa: E402
    get_miner_cgminer_devs,
    miner_login,
    miner_put_auth,
    parse_pool_activity,
    read_miner_admin_password,
    read_miner_registry,
)


RUNTIME_DIR = ROOT / "ops" / "runtime"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_selected(selectors: set[str]) -> list[dict[str, Any]]:
    registry = read_miner_registry()
    miners: list[dict[str, Any]] = []
    for miner in registry.get("miners", []):
        name = str(miner.get("name") or miner.get("display_name") or "")
        ip = str(miner.get("ip") or "")
        if not ip:
            continue
        if not selectors or name in selectors or ip in selectors:
            miners.append(
                {
                    "name": name or ip,
                    "ip": ip,
                    "mac": miner.get("mac"),
                    "worker": miner.get("worker"),
                }
            )
    return miners


def apply_profile(ip: str, password: str, setting: dict[str, Any], plan: str) -> None:
    token = miner_login(ip, password)
    payload = dict(setting)
    payload["manual"] = True
    payload["manualPowerplan"] = plan
    payload["select"] = int(payload.get("select", 0) or 0)
    miner_put_auth(ip, "/mcb/setting", payload, token)


def sample_miner(ip: str) -> dict[str, Any]:
    devs = get_miner_cgminer_devs(ip, timeout=4.0)
    return {
        "valid": as_int(devs.get("valid")),
        "av_hashrate": as_float(devs.get("av_hashrate")),
        "hashrate": as_float(devs.get("hashrate")),
        "temp_c": as_float(devs.get("temp")),
        "fanspeed": devs.get("fanspeed"),
        "accepted": as_int(devs.get("accepted")) or 0,
        "rejected": as_int(devs.get("rejected")) or 0,
        "hwerrors": as_int(devs.get("hwerrors")) or 0,
        "hwerr_ratio": as_float(devs.get("hwerr_ration")),
        "uptime_seconds": as_int(devs.get("time")),
        "minerstatus": devs.get("minerstatus"),
    }


def pool_window(ip: str, start: str, end: str) -> dict[str, Any]:
    text = docker_logs_between(start, end)
    activity = parse_pool_activity(text)
    for item in activity.get("miners", []):
        if str(item.get("ip") or "") == ip:
            return item
    return {
        "submits": 0,
        "shares": 0,
        "share_work": 0,
        "share_difficulty": 0,
        "blocks_found": 0,
        "jobs": 0,
    }


def evaluate(
    baseline: dict[str, dict[str, Any]],
    sample: dict[str, Any],
    *,
    max_temp_c: float,
    max_reject_ratio: float,
    max_hwerr_delta: int,
) -> list[str]:
    reasons: list[str] = []
    name = sample["name"]
    before = baseline[name]["devs"]
    current = sample["devs"]
    if current.get("valid") is not None and current.get("valid") < 6:
        reasons.append(f"{name}: valid chips dropped to {current.get('valid')}")
    if current.get("temp_c") is not None and current["temp_c"] > max_temp_c:
        reasons.append(f"{name}: temperature {current['temp_c']} C above {max_temp_c} C")
    accepted_delta = int(current.get("accepted") or 0) - int(before.get("accepted") or 0)
    rejected_delta = int(current.get("rejected") or 0) - int(before.get("rejected") or 0)
    total = accepted_delta + rejected_delta
    if total > 0 and rejected_delta / total > max_reject_ratio:
        reasons.append(f"{name}: reject delta ratio {rejected_delta / total:.3f} above {max_reject_ratio:.3f}")
    hw_delta = int(current.get("hwerrors") or 0) - int(before.get("hwerrors") or 0)
    if hw_delta > max_hwerr_delta:
        reasons.append(f"{name}: hardware errors increased by {hw_delta}")
    if current.get("minerstatus") not in (0, "0", None):
        reasons.append(f"{name}: minerstatus {current.get('minerstatus')}")
    return reasons


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--miner", action="append", required=True, help="Miner display name or IP. Repeatable.")
    parser.add_argument("--step", required=True, help="Power step as freq:volts, for example 950:0.89")
    parser.add_argument("--observe-seconds", type=int, default=600)
    parser.add_argument("--sample-interval", type=int, default=15)
    parser.add_argument("--settle-seconds", type=int, default=45)
    parser.add_argument("--max-temp-c", type=float, default=78.0)
    parser.add_argument("--max-reject-ratio", type=float, default=0.20)
    parser.add_argument("--max-hwerr-delta", type=int, default=10)
    parser.add_argument("--restore-on-failure", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    steps = parse_steps(args.step)
    if len(steps) != 1:
        raise SystemExit("--step must contain exactly one freq:volts value")
    step = steps[0]
    password = read_miner_admin_password()
    if not password:
        raise SystemExit("No saved miner admin password found; cannot apply power plans.")

    miners = load_selected(set(args.miner))
    if len(miners) != len(set(args.miner)):
        found = {m["name"] for m in miners} | {m["ip"] for m in miners}
        missing = sorted(set(args.miner) - found)
        raise SystemExit(f"Missing miners: {', '.join(missing)}")

    run_dir = RUNTIME_DIR / f"asic-power-batch-guard-{local_stamp()}"
    run_dir.mkdir(parents=True, exist_ok=False)
    events = run_dir / "events.jsonl"

    state: dict[str, Any] = {
        "status": "running",
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "step": step.label,
        "manualPowerplan": "",
        "miners": miners,
        "settings": vars(args),
        "baselines": {},
        "samples": [],
        "results": [],
    }
    write_json(run_dir / "summary.json", state)

    try:
        for miner in miners:
            baseline = probe(miner["ip"])
            setting = current_setting(miner["ip"])
            profile = plan_text(step.freq_mhz, step.volts, str(setting.get("manualPowerplan") or ""))
            state["manualPowerplan"] = profile
            state["baselines"][miner["name"]] = baseline
            backup_dir = run_dir / "settings-backups"
            backup_dir.mkdir(exist_ok=True)
            write_json(backup_dir / f"{miner['name']}-{miner['ip']}-setting-original.json", setting)
            append_jsonl(events, {"time": now_iso(), "event": "baseline", "miner": miner, "baseline": baseline})
            apply_profile(miner["ip"], password, setting, profile)
            append_jsonl(events, {"time": now_iso(), "event": "apply", "miner": miner, "manualPowerplan": profile})

        time.sleep(args.settle_seconds)
        start = now_iso()
        deadline = time.monotonic() + args.observe_seconds
        failed_reasons: list[str] = []

        while time.monotonic() < deadline:
            status = dashboard_status()
            if status.get("overall") != "ok":
                failed_reasons.append(f"dashboard: {status.get('overall')} {status.get('status_reason')}")
            for miner in miners:
                sample = {
                    "time": now_iso(),
                    "name": miner["name"],
                    "ip": miner["ip"],
                    "devs": sample_miner(miner["ip"]),
                    "dashboard": status,
                }
                state["samples"].append(sample)
                append_jsonl(events, {"event": "sample", **sample})
                failed_reasons.extend(
                    evaluate(
                        state["baselines"],
                        sample,
                        max_temp_c=args.max_temp_c,
                        max_reject_ratio=args.max_reject_ratio,
                        max_hwerr_delta=args.max_hwerr_delta,
                    )
                )
            write_json(run_dir / "summary.json", state)
            if failed_reasons:
                break
            time.sleep(args.sample_interval)

        end = now_iso()
        for miner in miners:
            samples = [s for s in state["samples"] if s["name"] == miner["name"]]
            if not samples:
                continue
            first = state["baselines"][miner["name"]]["devs"]
            last = samples[-1]["devs"]
            pool = pool_window(miner["ip"], start, end)
            result = {
                "name": miner["name"],
                "ip": miner["ip"],
                "stable": not failed_reasons,
                "reasons": failed_reasons,
                "summary": {
                    "sample_count": len(samples),
                    "hashrate_avg_mhs": round(
                        sum(s["devs"].get("hashrate") or 0 for s in samples) / max(1, len(samples)),
                        3,
                    ),
                    "hashrate_max_mhs": max(s["devs"].get("hashrate") or 0 for s in samples),
                    "av_hashrate_end_mhs": last.get("av_hashrate"),
                    "temp_max_c": max(s["devs"].get("temp_c") or 0 for s in samples),
                    "accepted_delta": int(last.get("accepted") or 0) - int(first.get("accepted") or 0),
                    "rejected_delta": int(last.get("rejected") or 0) - int(first.get("rejected") or 0),
                    "hwerrors_delta": int(last.get("hwerrors") or 0) - int(first.get("hwerrors") or 0),
                    "valid_end": last.get("valid"),
                    "pool": pool,
                },
            }
            state["results"].append(result)

        if failed_reasons and args.restore_on_failure:
            for miner in miners:
                backup = run_dir / "settings-backups" / f"{miner['name']}-{miner['ip']}-setting-original.json"
                restore_setting(miner["ip"], password, json.loads(backup.read_text(encoding="utf-8")))
                append_jsonl(events, {"time": now_iso(), "event": "restore", "miner": miner, "reasons": failed_reasons})

        state["status"] = "failed-restored" if failed_reasons and args.restore_on_failure else "completed"
        state["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        state["failure_reasons"] = failed_reasons
        write_json(run_dir / "summary.json", state)
        build_report(run_dir, state)
        print(run_dir)
        return 1 if failed_reasons else 0
    except Exception as exc:  # noqa: BLE001 - restore first, then surface.
        append_jsonl(events, {"time": now_iso(), "event": "exception", "error": str(exc)})
        for miner in miners:
            backup = run_dir / "settings-backups" / f"{miner['name']}-{miner['ip']}-setting-original.json"
            if backup.exists():
                restore_setting(miner["ip"], password, json.loads(backup.read_text(encoding="utf-8")))
        state["status"] = "exception-restored"
        state["error"] = str(exc)
        state["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        write_json(run_dir / "summary.json", state)
        build_report(run_dir, state)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
