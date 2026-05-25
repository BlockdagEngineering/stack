#!/usr/bin/env python3
"""Temporary A/B test isolation profile for the live BlockDAG mining host."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(os.environ.get("BDAG_PROJECT_ROOT", Path(__file__).resolve().parents[1]))
RUNTIME_DIR = Path(os.environ.get("BDAG_RUNTIME_DIR", PROJECT_ROOT / "ops" / "runtime"))
STATE_ROOT = RUNTIME_DIR / "ab-test-environment"
ACTIVE_STATE = STATE_ROOT / "active.json"

DEFAULT_TIMERS = [
    "bdag-hourly-snapshot.timer",
    "bdag-chain-presync.timer",
    "bdag-chain-restore-guard.timer",
    "bdag-sync-coordinator.timer",
    "bdag-local-peers.timer",
    "bdag-stack-sentinel.timer",
    "bdag-watchdog-guard.timer",
]

DEFAULT_ONESHOTS = [
    "bdag-hourly-snapshot.service",
    "bdag-chain-presync.service",
    "bdag-chain-restore-guard.service",
    "bdag-sync-coordinator.service",
    "bdag-local-peers.service",
    "bdag-stack-sentinel.service",
    "bdag-watchdog-guard.service",
]

OPTIONAL_AGGRESSIVE_SERVICES = [
    "bdag-release-seeder.service",
]

MUST_KEEP_ACTIVE = [
    "bdag-dashboard.service",
    "bdag-watchdog.service",
    "bdag-p2p-guard.service",
]


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def stamp() -> str:
    return dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def run(cmd: list[str], *, timeout: int = 60) -> dict[str, Any]:
    started = time.time()
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "ok": proc.returncode == 0,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "elapsed_seconds": round(time.time() - started, 3),
        "at": now_iso(),
    }


def systemctl(*args: str, timeout: int = 60) -> dict[str, Any]:
    return run(["systemctl", "--user", *args], timeout=timeout)


def unit_active(unit: str) -> str:
    result = systemctl("is-active", unit, timeout=10)
    return (result["stdout"] or result["stderr"] or "unknown").strip() or "unknown"


def unit_enabled(unit: str) -> str:
    result = systemctl("is-enabled", unit, timeout=10)
    return (result["stdout"] or result["stderr"] or "unknown").strip() or "unknown"


def unit_state(unit: str) -> dict[str, Any]:
    return {
        "unit": unit,
        "active": unit_active(unit),
        "enabled": unit_enabled(unit),
    }


def capture_units(units: list[str]) -> dict[str, dict[str, Any]]:
    return {unit: unit_state(unit) for unit in units}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def load_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def wait_inactive(units: list[str], timeout_seconds: int, log_path: Path) -> tuple[bool, list[str]]:
    deadline = time.monotonic() + max(0, timeout_seconds)
    active: list[str] = []
    while True:
        active = [unit for unit in units if unit_active(unit) == "active"]
        append_jsonl(log_path, {"at": now_iso(), "event": "wait_inactive_poll", "active": active})
        if not active:
            return True, []
        if time.monotonic() >= deadline:
            return False, active
        time.sleep(10)


def stop_and_verify_inactive(units: list[str], *, timeout: int, log_path: Path, event: str) -> list[str]:
    still_active: list[str] = []
    for unit in units:
        result = systemctl("stop", unit, timeout=timeout)
        append_jsonl(log_path, {"at": now_iso(), "unit": unit, "action": event, "result": result})
    time.sleep(1)
    still_active = [unit for unit in units if unit_active(unit) == "active"]
    if still_active:
        append_jsonl(log_path, {"at": now_iso(), "event": f"{event}_retry", "still_active": still_active})
        for unit in still_active:
            result = systemctl("stop", unit, timeout=timeout)
            append_jsonl(log_path, {"at": now_iso(), "unit": unit, "action": f"{event}_retry", "result": result})
        time.sleep(1)
        still_active = [unit for unit in units if unit_active(unit) == "active"]
    append_jsonl(log_path, {"at": now_iso(), "event": f"{event}_verify", "still_active": still_active})
    return still_active


def restore_from_state(state: dict[str, Any], *, log_path: Path | None = None) -> dict[str, Any]:
    log_path = log_path or (Path(state["run_dir"]) / "restore-events.jsonl")
    results: list[dict[str, Any]] = []
    units = state.get("units") or {}
    aggressive_services = set(state.get("aggressive_services") or [])

    # Restore timers first so normal periodic maintenance resumes.
    for unit, before in units.items():
        if not unit.endswith(".timer"):
            continue
        if before.get("active") == "active":
            result = systemctl("start", unit, timeout=30)
            results.append({"unit": unit, "action": "start", "result": result})
            append_jsonl(log_path, {"at": now_iso(), "unit": unit, "action": "start", "result": result})

    # Only restart long-running optional services that this tool intentionally stopped.
    for unit in aggressive_services:
        before = units.get(unit) or {}
        if before.get("active") == "active":
            result = systemctl("start", unit, timeout=60)
            results.append({"unit": unit, "action": "start", "result": result})
            append_jsonl(log_path, {"at": now_iso(), "unit": unit, "action": "start", "result": result})

    state["restored_at"] = now_iso()
    state["restore_results"] = results
    write_json(Path(state["run_dir"]) / "state.restored.json", state)
    if ACTIVE_STATE.exists() and load_state(ACTIVE_STATE).get("run_dir") == state.get("run_dir"):
        ACTIVE_STATE.unlink()
    return state


def enter(args: argparse.Namespace) -> int:
    if ACTIVE_STATE.exists() and not args.force:
        print(f"active A/B isolation state already exists: {ACTIVE_STATE}", file=sys.stderr)
        return 2

    run_dir = Path(args.run_dir) if args.run_dir else STATE_ROOT / f"ab-env-{stamp()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "events.jsonl"

    timers = list(DEFAULT_TIMERS)
    oneshots = list(DEFAULT_ONESHOTS)
    aggressive_services = list(OPTIONAL_AGGRESSIVE_SERVICES) if args.profile == "aggressive" else []
    all_units = sorted(set(timers + oneshots + aggressive_services + MUST_KEEP_ACTIVE))
    state = {
        "document_type": "bdag_ab_test_environment_state",
        "entered_at": now_iso(),
        "run_dir": str(run_dir),
        "profile": args.profile,
        "reason": args.reason,
        "timers_paused": timers,
        "oneshots_quieted": oneshots,
        "aggressive_services": aggressive_services,
        "must_keep_active": MUST_KEEP_ACTIVE,
        "units": capture_units(all_units),
        "restore_command": f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} exit --state {shlex.quote(str(run_dir / 'state.json'))}",
    }
    write_json(run_dir / "state.before.json", state)

    not_running = [unit for unit in MUST_KEEP_ACTIVE if unit_active(unit) != "active"]
    if not_running and not args.allow_missing_guards:
        state["enter_failed_at"] = now_iso()
        state["enter_failure"] = f"required guard service inactive: {', '.join(not_running)}"
        write_json(run_dir / "state.failed.json", state)
        print(state["enter_failure"], file=sys.stderr)
        return 3

    still_active_timers = stop_and_verify_inactive(timers, timeout=30, log_path=log_path, event="stop_timer")
    if still_active_timers:
        state["enter_failed_at"] = now_iso()
        state["enter_failure"] = f"supporting timers still active after stop: {', '.join(still_active_timers)}"
        write_json(run_dir / "state.failed.json", state)
        restore_from_state(state, log_path=run_dir / "restore-after-failed-enter.jsonl")
        print(state["enter_failure"], file=sys.stderr)
        return 5

    quiet, still_active = wait_inactive(oneshots, args.wait_quiet_seconds, log_path)
    if not quiet:
        if args.stop_active_oneshots:
            for unit in still_active:
                result = systemctl("stop", unit, timeout=60)
                append_jsonl(log_path, {"at": now_iso(), "unit": unit, "action": "force_stop_active_oneshot", "result": result})
        else:
            state["enter_failed_at"] = now_iso()
            state["enter_failure"] = f"supporting one-shot services still active after wait: {', '.join(still_active)}"
            write_json(run_dir / "state.failed.json", state)
            restore_from_state(state, log_path=run_dir / "restore-after-failed-enter.jsonl")
            print(state["enter_failure"], file=sys.stderr)
            return 4

    still_active_aggressive = stop_and_verify_inactive(aggressive_services, timeout=60, log_path=log_path, event="stop_aggressive_service")
    if still_active_aggressive:
        state["enter_failed_at"] = now_iso()
        state["enter_failure"] = f"aggressive services still active after stop: {', '.join(still_active_aggressive)}"
        write_json(run_dir / "state.failed.json", state)
        restore_from_state(state, log_path=run_dir / "restore-after-failed-enter.jsonl")
        print(state["enter_failure"], file=sys.stderr)
        return 6

    state["entered_ok_at"] = now_iso()
    state["current_units_after_enter"] = capture_units(all_units)
    write_json(run_dir / "state.json", state)
    write_json(ACTIVE_STATE, state)
    restore_script = run_dir / "restore.sh"
    restore_script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} exit --state {shlex.quote(str(run_dir / 'state.json'))}\n",
        encoding="utf-8",
    )
    restore_script.chmod(0o755)
    print(json.dumps({"run_dir": str(run_dir), "restore_script": str(restore_script), "state": str(run_dir / "state.json")}, indent=2))
    return 0


def exit_env(args: argparse.Namespace) -> int:
    state_path = Path(args.state) if args.state else ACTIVE_STATE
    if not state_path.exists():
        print(f"state file not found: {state_path}", file=sys.stderr)
        return 2
    state = load_state(state_path)
    restored = restore_from_state(state)
    print(json.dumps({"restored_at": restored.get("restored_at"), "run_dir": restored.get("run_dir")}, indent=2))
    return 0


def status(args: argparse.Namespace) -> int:
    state = load_state(ACTIVE_STATE) if ACTIVE_STATE.exists() else {}
    units = sorted(set(DEFAULT_TIMERS + DEFAULT_ONESHOTS + OPTIONAL_AGGRESSIVE_SERVICES + MUST_KEEP_ACTIVE))
    payload = {
        "generated_at": now_iso(),
        "active_state_path": str(ACTIVE_STATE) if ACTIVE_STATE.exists() else "",
        "active_state": state,
        "current_units": capture_units(units),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def run_command(args: argparse.Namespace) -> int:
    if not args.command:
        print("missing command after --", file=sys.stderr)
        return 2
    enter_args = argparse.Namespace(
        run_dir=args.run_dir,
        profile=args.profile,
        reason=args.reason,
        force=args.force,
        allow_missing_guards=args.allow_missing_guards,
        wait_quiet_seconds=args.wait_quiet_seconds,
        stop_active_oneshots=args.stop_active_oneshots,
    )
    rc = enter(enter_args)
    if rc != 0:
        return rc
    state = load_state(ACTIVE_STATE)
    run_dir = Path(state["run_dir"])
    command_log = run_dir / "command.json"
    try:
        started = now_iso()
        proc = subprocess.run(args.command, text=True, cwd=PROJECT_ROOT, check=False)
        command_result = {
            "cmd": args.command,
            "returncode": proc.returncode,
            "started_at": started,
            "finished_at": now_iso(),
        }
        write_json(command_log, command_result)
        return_code = proc.returncode
    finally:
        restore_from_state(state)
    return return_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command_name", required=True)

    enter_parser = sub.add_parser("enter", help="enter an isolated A/B test environment")
    enter_parser.add_argument("--run-dir", default="")
    enter_parser.add_argument("--profile", choices=["balanced", "aggressive"], default="balanced")
    enter_parser.add_argument("--reason", default="manual A/B test")
    enter_parser.add_argument("--wait-quiet-seconds", type=int, default=1800)
    enter_parser.add_argument("--stop-active-oneshots", action="store_true", help="stop active one-shot maintenance jobs instead of aborting")
    enter_parser.add_argument("--allow-missing-guards", action="store_true", help="allow missing watchdog/dashboard/p2p guard")
    enter_parser.add_argument("--force", action="store_true")
    enter_parser.set_defaults(func=enter)

    exit_parser = sub.add_parser("exit", help="restore the environment")
    exit_parser.add_argument("--state", default="")
    exit_parser.set_defaults(func=exit_env)

    status_parser = sub.add_parser("status", help="show current isolation/unit state")
    status_parser.set_defaults(func=status)

    run_parser = sub.add_parser("run", help="run a command inside an isolated A/B test environment")
    run_parser.add_argument("--run-dir", default="")
    run_parser.add_argument("--profile", choices=["balanced", "aggressive"], default="balanced")
    run_parser.add_argument("--reason", default="manual A/B test")
    run_parser.add_argument("--wait-quiet-seconds", type=int, default=1800)
    run_parser.add_argument("--stop-active-oneshots", action="store_true")
    run_parser.add_argument("--allow-missing-guards", action="store_true")
    run_parser.add_argument("--force", action="store_true")
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    run_parser.set_defaults(func=run_command)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "command", None) and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
