#!/usr/bin/env python3
"""Portable runner for the guarded pool adaptive optimizer.

Systemd remains the Linux appliance path. This runner gives macOS, Windows
Docker Desktop, cron, launchd, and plain shell sessions the same capability
profile defaults without requiring systemd timer support.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path


OPS_DIR = Path(__file__).resolve().parent
ROOT = OPS_DIR.parent
sys.path.insert(0, str(OPS_DIR))

import capability_profile  # noqa: E402
import pool_adaptive_optimizer  # noqa: E402


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "apply"}


def parse_duration_seconds(value: str | None, default: int) -> int:
    text = (value or "").strip().lower()
    if not text:
        return default
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)?", text)
    if not match:
        try:
            return max(1, int(float(text)))
        except ValueError:
            return default
    amount = float(match.group(1))
    unit = match.group(2) or "s"
    if unit == "ms":
        return max(1, int(amount / 1000.0))
    if unit.startswith("m"):
        return max(1, int(amount * 60))
    if unit.startswith("h"):
        return max(1, int(amount * 3600))
    return max(1, int(amount))


def choose_env_file(root: Path, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    for candidate in (
        os.environ.get("BDAG_POOL_OPTIMIZER_ENV_FILE"),
        os.environ.get("BDAG_POOL_ENV_FILE"),
    ):
        if candidate:
            return Path(candidate).expanduser()
    ops_env = root / "ops" / "runtime" / "ops.env"
    return ops_env if ops_env.exists() else root / ".env"


def load_env_defaults(path: Path) -> None:
    for key, value in capability_profile.load_env_file(path).items():
        os.environ.setdefault(key, value)


def apply_capability_defaults(root: Path, env_file: Path, protected_env_keys: set[str]) -> dict[str, object]:
    env = os.environ.copy()
    env.update(capability_profile.load_env_file(env_file))
    payload = capability_profile.resolve(root, env)
    use_optimizer_profile_defaults = parse_bool(os.environ.get("BDAG_POOL_OPTIMIZER_USE_CAPABILITY_DEFAULTS"), True)
    for key, value in payload.get("recommendations", {}).items():
        env_key = str(key)
        if env_key.startswith("BDAG_POOL_OPTIMIZER_") and use_optimizer_profile_defaults and env_key not in protected_env_keys:
            os.environ[env_key] = str(value)
        else:
            os.environ.setdefault(env_key, str(value))
    os.environ["BDAG_CAPABILITY_PROFILE_RESOLVED"] = str(payload.get("capability_profile") or "")
    return payload


def build_optimizer_args(apply: bool, yes: bool) -> list[str]:
    args: list[str] = []
    if apply:
        args.append("--apply")
    if yes:
        args.append("--yes")
    return args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("BDAG_PROJECT_ROOT", str(ROOT)))
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--loop", action="store_true", help="Run one optimizer pass per interval until interrupted.")
    parser.add_argument("--once", action="store_false", dest="loop", help="Run one optimizer pass and exit. This is the default.")
    parser.add_argument("--interval-seconds", default=None, help="Loop interval. Accepts values such as 1200, 20m, or 1h.")
    parser.add_argument("--apply", action="store_true", help="Apply through runtime admin. Default is advisory unless BDAG_POOL_OPTIMIZER_APPLY=1.")
    parser.add_argument("--yes", action="store_true", help="Required with --apply or BDAG_POOL_OPTIMIZER_APPLY=1.")
    parser.set_defaults(loop=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    env_file = choose_env_file(root, args.env_file)
    protected_env_keys = set(os.environ)
    os.environ.setdefault("BDAG_PROJECT_ROOT", str(root))
    os.environ.setdefault("BDAG_RUNTIME_DIR", str(root / "ops" / "runtime"))
    load_env_defaults(env_file)
    capability_payload = apply_capability_defaults(root, env_file, protected_env_keys)

    apply = bool(args.apply or parse_bool(os.environ.get("BDAG_POOL_OPTIMIZER_APPLY")))
    yes = bool(args.yes or parse_bool(os.environ.get("BDAG_POOL_OPTIMIZER_YES")))
    interval = parse_duration_seconds(
        args.interval_seconds
        or os.environ.get("BDAG_POOL_OPTIMIZER_RUNNER_INTERVAL_SECONDS")
        or os.environ.get("BDAG_POOL_OPTIMIZER_TIMER_ON_UNIT_ACTIVE_SEC"),
        1800,
    )

    while True:
        optimizer_args = build_optimizer_args(apply, yes)
        payload = pool_adaptive_optimizer.run_controller(pool_adaptive_optimizer.build_parser().parse_args(optimizer_args))
        print(
            json.dumps(
                {
                    "capability_profile": capability_payload.get("capability_profile"),
                    "summary_path": payload.get("summary_path"),
                    "events_path": payload.get("events_path"),
                    "report_path": payload.get("report_path"),
                    "state_path": payload.get("state_path"),
                },
                indent=2,
            ),
            flush=True,
        )
        if not args.loop:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
