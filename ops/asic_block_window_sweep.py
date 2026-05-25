#!/usr/bin/env python3
"""Run long ASIC profile windows and choose the setting with most blocks."""

from __future__ import annotations

import argparse
import html
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
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
    current_setting,
    dashboard_status,
    local_stamp,
    plan_text,
    pool_window_for_miner,
    probe,
    restore_setting,
    restart_miner_process,
    write_json,
)
from pool_ops import read_miner_admin_password  # noqa: E402


RUNTIME_DIR = ROOT / "ops" / "runtime"
MINER_IP = os.environ.get("BDAG_ASIC_BLOCK_WINDOW_MINER_IP", "192.168.1.50")
STOCK_PLAN = "775 MHz 0.75 V 15 RPM 9.4 RPM"


@dataclass(frozen=True)
class Profile:
    name: str
    manual: bool
    freq_mhz: int | None
    volts: str | None

    @property
    def label(self) -> str:
        if not self.manual:
            return self.name
        return f"{self.freq_mhz}MHz-{self.volts}V"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc() -> str:
    return now_utc().isoformat(timespec="seconds")


def parse_profiles(text: str) -> list[Profile]:
    profiles: list[Profile] = []
    for raw in text.split(","):
        part = raw.strip()
        if not part:
            continue
        if part.lower() in {"stock", "original", "775"}:
            profiles.append(Profile("stock-775MHz-0.75V", False, None, None))
            continue
        freq, volts = part.split(":", 1)
        profiles.append(Profile(f"{int(freq)}MHz-{volts.strip()}V", True, int(freq), volts.strip()))
    if not profiles:
        raise ValueError("no profiles configured")
    return profiles


def should_stop(run_dir: Path) -> bool:
    return (run_dir / "STOP").exists()


def profile_payload(profile: Profile, base: dict[str, Any]) -> dict[str, Any]:
    payload = dict(base)
    if profile.manual:
        payload["manual"] = True
        payload["manualPowerplan"] = plan_text(profile.freq_mhz or 0, profile.volts or "0", str(base.get("manualPowerplan") or ""))
        payload["select"] = int(payload.get("select", 0) or 0)
    else:
        payload["manual"] = False
        payload["manualPowerplan"] = STOCK_PLAN
        payload["select"] = 0
    return payload


def apply_profile(ip: str, password: str, profile: Profile, base: dict[str, Any]) -> dict[str, Any]:
    return restore_setting(ip, password, profile_payload(profile, base))


def wait_for_dashboard_ok(run_dir: Path, events: Path, timeout_seconds: int) -> bool:
    started = time.monotonic()
    while True:
        if should_stop(run_dir):
            return False
        status = dashboard_status()
        if status.get("overall") == "ok":
            return True
        append_jsonl(events, {"time": iso_utc(), "event": "dashboard-wait", "status": status})
        if timeout_seconds and time.monotonic() - started > timeout_seconds:
            return False
        time.sleep(20)


def summarize_window(
    *,
    profile: Profile,
    started_at: str,
    finished_at: str,
    seconds: float,
    before: dict[str, Any],
    after: dict[str, Any],
    samples: list[dict[str, Any]],
    pool: dict[str, Any],
    guard_reasons: list[str],
) -> dict[str, Any]:
    before_devs = before.get("devs") or {}
    after_devs = after.get("devs") or {}
    accepted_delta = max(0, int(after_devs.get("accepted") or 0) - int(before_devs.get("accepted") or 0))
    rejected_delta = max(0, int(after_devs.get("rejected") or 0) - int(before_devs.get("rejected") or 0))
    hwerrors_delta = max(0, int(after_devs.get("hwerrors") or 0) - int(before_devs.get("hwerrors") or 0))
    hashes = [as_float((item.get("devs") or {}).get("hashrate")) for item in samples]
    hashes = [item for item in hashes if item is not None]
    av_hashes = [as_float((item.get("devs") or {}).get("av_hashrate")) for item in samples]
    av_hashes = [item for item in av_hashes if item is not None]
    temps = [as_float((item.get("devs") or {}).get("temp_c")) for item in samples]
    temps = [item for item in temps if item is not None]
    blocks = int(pool.get("blocks_found") or 0)
    shares = int(pool.get("shares") or 0)
    share_work = int(pool.get("share_work") or 0)
    total_delta = accepted_delta + rejected_delta
    reject_ratio = rejected_delta / total_delta if total_delta else 0.0
    return {
        "profile": profile.label,
        "manual": profile.manual,
        "started_at": started_at,
        "finished_at": finished_at,
        "seconds": round(seconds, 3),
        "safe": not guard_reasons,
        "guard_reasons": guard_reasons,
        "blocks_found": blocks,
        "blocks_per_hour": round(blocks / max(seconds / 3600.0, 1e-9), 6),
        "shares": shares,
        "share_work": share_work,
        "share_work_per_second": round(share_work / max(seconds, 1.0), 3),
        "submits": int(pool.get("submits") or 0),
        "accepted_delta": accepted_delta,
        "rejected_delta": rejected_delta,
        "reject_delta_ratio": round(reject_ratio, 6),
        "hwerrors_delta": hwerrors_delta,
        "hwerr_ratio_end": after_devs.get("hwerr_ratio"),
        "hashrate_avg_mhs": round(sum(hashes) / len(hashes), 3) if hashes else None,
        "hashrate_max_mhs": round(max(hashes), 3) if hashes else None,
        "av_hashrate_avg_mhs": round(sum(av_hashes) / len(av_hashes), 3) if av_hashes else None,
        "av_hashrate_end_mhs": after_devs.get("av_hashrate"),
        "temp_max_c": round(max(temps), 2) if temps else None,
        "temp_avg_c": round(sum(temps) / len(temps), 2) if temps else None,
        "valid_end": after_devs.get("valid"),
        "pool": pool,
    }


def window_score(window: dict[str, Any]) -> tuple[float, float, float, float]:
    if not window.get("safe"):
        return (-1.0, -1.0, -1.0, 0.0)
    return (
        float(window.get("blocks_per_hour") or 0),
        float(window.get("share_work_per_second") or 0),
        float(window.get("hashrate_avg_mhs") or 0),
        -float(window.get("hwerr_ratio_end") or 0),
    )


def choose_best(windows: list[dict[str, Any]]) -> dict[str, Any] | None:
    safe = [item for item in windows if item.get("safe")]
    if not safe:
        return None
    return max(safe, key=window_score)


def build_report(run_dir: Path, state: dict[str, Any]) -> None:
    rows = []
    for item in state.get("windows", []):
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('index', '')))}</td>"
            f"<td>{html.escape(str(item.get('profile', '')))}</td>"
            f"<td>{html.escape(str(item.get('blocks_found', '')))}</td>"
            f"<td>{html.escape(str(item.get('blocks_per_hour', '')))}</td>"
            f"<td>{html.escape(str(item.get('share_work_per_second', '')))}</td>"
            f"<td>{html.escape(str(item.get('hashrate_avg_mhs', '')))}</td>"
            f"<td>{html.escape(str(item.get('hwerr_ratio_end', '')))}</td>"
            f"<td>{html.escape(str(item.get('reject_delta_ratio', '')))}</td>"
            f"<td>{html.escape(str(item.get('temp_max_c', '')))}</td>"
            f"<td>{html.escape('yes' if item.get('safe') else 'no')}</td>"
            f"<td>{html.escape('; '.join(item.get('guard_reasons') or []))}</td>"
            "</tr>"
        )
    best = state.get("best_window") or {}
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>ASIC Block Window Sweep</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 0; background: #101418; color: #e8edf2; }}
    main {{ max-width: 1280px; margin: 0 auto; padding: 28px; }}
    table {{ width: 100%; border-collapse: collapse; background: #151c23; }}
    th, td {{ border-bottom: 1px solid #2d3945; padding: 8px 10px; text-align: left; vertical-align: top; }}
    th {{ color: #a9bac9; font-size: 12px; text-transform: uppercase; }}
    code {{ color: #8bd5ff; }}
  </style>
</head>
<body><main>
  <h1>ASIC Block Window Sweep</h1>
  <p>Status: <strong>{html.escape(str(state.get('status')))}</strong>. Best profile: <code>{html.escape(str(best.get('profile') or 'pending'))}</code>.</p>
  <p>Primary metric is blocks per hour in each measurement window. Share work, hashrate, rejects, hardware errors, and temperature are diagnostics and safety guards.</p>
  <table>
    <thead><tr><th>#</th><th>Profile</th><th>Blocks</th><th>Blocks/hr</th><th>Share work/s</th><th>Hash avg</th><th>HW err</th><th>Reject</th><th>Temp</th><th>Safe</th><th>Reasons</th></tr></thead>
    <tbody>{''.join(rows) if rows else '<tr><td colspan="11">No completed windows yet.</td></tr>'}</tbody>
  </table>
  <p>Summary JSON: <code>{html.escape(str(run_dir / 'summary.json'))}</code></p>
</main></body></html>
"""
    (run_dir / "report.html").write_text(html_text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=9.0)
    parser.add_argument("--window-seconds", type=int, default=1800)
    parser.add_argument("--settle-seconds", type=int, default=180)
    parser.add_argument("--sample-interval", type=int, default=60)
    parser.add_argument("--dashboard-ok-timeout", type=int, default=600)
    parser.add_argument("--profiles", default="stock,800:0.77,825:0.79,840:0.80,845:0.805")
    parser.add_argument("--max-temp-c", type=float, default=76.0)
    parser.add_argument("--max-hwerr-ratio", type=float, default=0.010)
    parser.add_argument("--max-reject-ratio", type=float, default=0.55)
    parser.add_argument("--miner-ip", default=MINER_IP)
    args = parser.parse_args()

    profiles = parse_profiles(args.profiles)
    password = read_miner_admin_password() or ""
    run_dir = RUNTIME_DIR / f"asic-block-window-sweep-{local_stamp()}"
    run_dir.mkdir(parents=True, exist_ok=False)
    active = RUNTIME_DIR / "active-asic-block-window-sweep"
    if active.exists() or active.is_symlink():
        active.unlink()
    active.symlink_to(run_dir)
    events = run_dir / "events.jsonl"
    (run_dir / "README.STOP.txt").write_text(f"Create {run_dir / 'STOP'} to stop after the current sample.\n", encoding="utf-8")

    state: dict[str, Any] = {
        "status": "running",
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "miner_ip": args.miner_ip,
        "profiles": [item.label for item in profiles],
        "settings": vars(args),
        "windows": [],
        "best_window": None,
    }
    write_json(run_dir / "summary.json", state)
    build_report(run_dir, state)
    append_jsonl(events, {"time": iso_utc(), "event": "run-start", "state": state})

    def handle_signal(signum: int, _frame: Any) -> None:
        append_jsonl(events, {"time": iso_utc(), "event": "signal", "signal": signum})
        (run_dir / "STOP").write_text(f"signal {signum} at {iso_utc()}\n", encoding="utf-8")

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    base_setting = current_setting(args.miner_ip)
    deadline = time.monotonic() + (args.hours * 3600.0)
    index = 0

    try:
        while time.monotonic() < deadline and not should_stop(run_dir):
            profile = profiles[index % len(profiles)]
            index += 1
            append_jsonl(events, {"time": iso_utc(), "event": "apply-profile", "profile": profile.label, "index": index})
            apply_profile(args.miner_ip, password, profile, base_setting)
            restart_miner_process(args.miner_ip, password)
            time.sleep(args.settle_seconds)
            if not wait_for_dashboard_ok(run_dir, events, args.dashboard_ok_timeout):
                append_jsonl(events, {"time": iso_utc(), "event": "dashboard-not-ok", "profile": profile.label})
                continue

            started_at = iso_utc()
            before = probe(args.miner_ip)
            samples: list[dict[str, Any]] = []
            guard_reasons: list[str] = []
            window_deadline = min(time.monotonic() + args.window_seconds, deadline)

            while time.monotonic() < window_deadline and not should_stop(run_dir):
                try:
                    sample = probe(args.miner_ip)
                    samples.append(sample)
                    devs = sample.get("devs") or {}
                    temp = as_float(devs.get("temp_c"))
                    if temp is not None and temp > args.max_temp_c:
                        guard_reasons.append(f"temp>{args.max_temp_c}C")
                    valid = as_int(devs.get("valid"))
                    if valid is not None and valid < 6:
                        guard_reasons.append("valid-chip-count-below-6")
                    hw = as_float(devs.get("hwerr_ratio"))
                    if hw is not None and hw > args.max_hwerr_ratio:
                        guard_reasons.append(f"hwerr-ratio>{args.max_hwerr_ratio}")
                except Exception as exc:  # noqa: BLE001
                    guard_reasons.append(f"probe-failed:{exc}")
                if guard_reasons:
                    break
                time.sleep(args.sample_interval)

            finished_at = iso_utc()
            after = samples[-1] if samples else probe(args.miner_ip)
            seconds = max(1.0, (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds())
            pool = pool_window_for_miner(args.miner_ip, started_at, finished_at)
            window = summarize_window(
                profile=profile,
                started_at=started_at,
                finished_at=finished_at,
                seconds=seconds,
                before=before,
                after=after,
                samples=samples,
                pool=pool,
                guard_reasons=list(dict.fromkeys(guard_reasons)),
            )
            if window["reject_delta_ratio"] > args.max_reject_ratio:
                window["safe"] = False
                window["guard_reasons"].append(f"reject-ratio>{args.max_reject_ratio}")
            if window["blocks_found"] <= 0:
                window["guard_reasons"].append("no-blocks-produced")
            window["index"] = index
            state["windows"].append(window)
            state["best_window"] = choose_best(state["windows"])
            write_json(run_dir / "summary.json", state)
            build_report(run_dir, state)
            append_jsonl(events, {"time": iso_utc(), "event": "window-finished", "window": window, "best": state["best_window"]})
    finally:
        best = choose_best(state["windows"])
        state["best_window"] = best
        if best:
            best_profile = next(item for item in profiles if item.label == best["profile"])
            append_jsonl(events, {"time": iso_utc(), "event": "apply-best", "profile": best_profile.label})
            apply_profile(args.miner_ip, password, best_profile, base_setting)
            restart_miner_process(args.miner_ip, password)
            time.sleep(args.settle_seconds)
        state["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        state["status"] = "stopped" if should_stop(run_dir) else "completed"
        write_json(run_dir / "summary.json", state)
        build_report(run_dir, state)
        append_jsonl(events, {"time": iso_utc(), "event": "run-finished", "status": state["status"], "best": state.get("best_window")})
        latest = RUNTIME_DIR / "latest-asic-block-window-sweep"
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(run_dir)
        if active.is_symlink() and active.resolve() == run_dir:
            active.unlink()

    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
