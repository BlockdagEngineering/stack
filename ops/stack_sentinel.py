#!/usr/bin/env python3
"""Last-resort liveness sentinel for the local BlockDAG mining stack."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from incident_journal import append_incident
from pool_ops import (
    LOG_DIR,
    NODES,
    POOL_CONTAINER,
    POOL_ENV_FILE,
    PROJECT_ROOT,
    RUNTIME_DIR,
    SERVICES,
    docker_inspect,
    ensure_runtime,
    now_iso,
    run_logged,
)


STATE_FILE = RUNTIME_DIR / "stack-sentinel-state.json"
LOG_FILE = LOG_DIR / "stack-sentinel.log"
LOCK_FILE = RUNTIME_DIR / "stack-sentinel.lock"
DASHBOARD_URL = os.environ.get("BDAG_SENTINEL_DASHBOARD_URL", "http://127.0.0.1:8088/api/status")
DASHBOARD_TIMEOUT = float(os.environ.get("BDAG_SENTINEL_DASHBOARD_TIMEOUT", "20"))
INCIDENT_COOLDOWN_SECONDS = int(os.environ.get("BDAG_SENTINEL_INCIDENT_COOLDOWN_SECONDS", "300"))
SHARE_STALE_SECONDS = int(os.environ.get("BDAG_SENTINEL_SHARE_STALE_SECONDS", "180"))
NODE_LOG_LOOKBACK_SECONDS = int(os.environ.get("BDAG_SENTINEL_NODE_LOG_LOOKBACK_SECONDS", "300"))
ZERO_STATE_ROOT_WARN_COUNT = int(os.environ.get("BDAG_SENTINEL_ZERO_STATE_ROOT_WARN_COUNT", "3"))
ZERO_STATE_ROOT_CRITICAL_COUNT = int(os.environ.get("BDAG_SENTINEL_ZERO_STATE_ROOT_CRITICAL_COUNT", "20"))
FAILURE_AGE_RE = re.compile(r"\bfor \d+s\b")
DESKTOP_NOTIFY_ENABLED = os.environ.get("BDAG_SENTINEL_DESKTOP_NOTIFY", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

USER_SERVICES = [
    "bdag-dashboard.service",
    "bdag-watchdog.service",
    "bdag-p2p-guard.service",
]
USER_TIMERS = [
    "bdag-watchdog-guard.timer",
    "bdag-sync-coordinator.timer",
    "bdag-chain-restore-guard.timer",
    "bdag-chain-presync.timer",
    "bdag-hourly-snapshot.timer",
    "bdag-local-peers.timer",
]


def log(message: str) -> None:
    ensure_runtime()
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message}\n")


def read_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict[str, Any]) -> None:
    ensure_runtime()
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def should_emit(state: dict[str, Any], key: str, signature: str, now: int) -> bool:
    last_signature = str(state.get(f"{key}_signature") or "")
    last_epoch = int(state.get(f"{key}_epoch", 0) or 0)
    if last_signature == signature and now - last_epoch < INCIDENT_COOLDOWN_SECONDS:
        return False
    state[f"{key}_signature"] = signature
    state[f"{key}_epoch"] = now
    state[f"{key}_at"] = now_iso()
    return True


def stable_failure_signature(failures: list[Any]) -> str:
    parts = []
    for item in failures[:8]:
        parts.append(FAILURE_AGE_RE.sub("for Ns", str(item)))
    return " | ".join(parts) or "overall-down"


def systemctl_user(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def unit_active(unit: str) -> bool:
    result = systemctl_user("is-active", unit)
    return result.returncode == 0 and result.stdout.strip() == "active"


def start_unit(unit: str, state: dict[str, Any], now: int) -> None:
    if unit_active(unit):
        return
    result = systemctl_user("start", unit)
    details = {
        "unit": unit,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
    event_type = "sentinel_started_unit" if result.returncode == 0 else "sentinel_unit_start_failed"
    severity = "warning" if result.returncode == 0 else "critical"
    message = f"Stack sentinel {'started' if result.returncode == 0 else 'could not start'} {unit}"
    if should_emit(state, event_type + "_" + unit.replace(".", "_"), str(result.returncode), now):
        append_incident(event_type, severity, "stack-sentinel", message, details)
    log(f"{message} rc={result.returncode}")


def status_api() -> tuple[dict[str, Any] | None, str]:
    try:
        with urllib.request.urlopen(DASHBOARD_URL, timeout=DASHBOARD_TIMEOUT) as response:
            return json.loads(response.read(4_000_000).decode("utf-8", "replace")), ""
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, str(exc)


def compose_command(*args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(POOL_ENV_FILE),
        "-f",
        str(PROJECT_ROOT / "docker-compose.yml"),
        *args,
    ]


def start_container(service: str, reason: str, state: dict[str, Any], now: int) -> bool:
    log_path = LOG_DIR / f"sentinel-start-{service}-{now}.log"
    result = run_logged(compose_command("start", service), log_path, timeout=120)
    details = {"service": service, "reason": reason, "log_path": str(log_path), **result.as_dict()}
    if result.ok:
        append_incident(
            "sentinel_started_container",
            "warning",
            "stack-sentinel",
            f"Stack sentinel started {service}: {reason}",
            details,
        )
        log(f"started {service}: {reason}")
        return True
    append_incident(
        "sentinel_container_start_failed",
        "critical",
        "stack-sentinel",
        f"Stack sentinel could not start {service}: {reason}",
        details,
    )
    log(f"failed to start {service}: {reason} rc={result.returncode}")
    return False


def notify_user(title: str, body: str) -> None:
    if not DESKTOP_NOTIFY_ENABLED:
        log(f"desktop notification suppressed title={title!r}")
        return
    command = ["notify-send", title, body]
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    subprocess.run(command, env=env, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def inspect_and_repair_containers(status: dict[str, Any] | None, state: dict[str, Any], now: int) -> None:
    inspected = docker_inspect(SERVICES)
    stopped = [name for name in [POOL_CONTAINER, *NODES, "rpc-failover", "pool-db"] if not inspected.get(name, {}).get("running")]
    state["stopped_containers"] = stopped
    if stopped and should_emit(state, "stopped_containers", ",".join(stopped), now):
        append_incident(
            "sentinel_stopped_containers",
            "critical",
            "stack-sentinel",
            "Critical BlockDAG container(s) are stopped",
            {"stopped": stopped, "containers": inspected},
        )
        notify_user("BlockDAG mining stack needs attention", f"Stopped containers: {', '.join(stopped)}")

    if "pool-db" in stopped:
        start_container("pool-db", "database container is stopped", state, now)
    for node in NODES:
        if node in stopped:
            start_container(node, "node container is stopped", state, now)
    if "rpc-failover" in stopped:
        start_container("rpc-failover", "RPC failover container is stopped", state, now)
    if POOL_CONTAINER in stopped:
        start_container(POOL_CONTAINER, "ASIC pool container is stopped", state, now)

    if not status:
        return
    pool_health = status.get("pool_health") if isinstance(status.get("pool_health"), dict) else {}
    miner_health = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}
    last_share_age = pool_health.get("last_valid_share_age_seconds")
    connected = int(miner_health.get("connected_count") or 0)
    if connected > 0 and isinstance(last_share_age, int) and last_share_age > SHARE_STALE_SECONDS:
        signature = f"{connected}:{last_share_age // 60}"
        if should_emit(state, "share_stale", signature, now):
            append_incident(
                "sentinel_share_stale",
                "critical",
                "stack-sentinel",
                f"No accepted pool share for {last_share_age}s while {connected} miner(s) are connected",
                {"last_valid_share_age_seconds": last_share_age, "connected_miners": connected},
            )
            notify_user("BlockDAG share flow stalled", f"No accepted share for {last_share_age}s")


def check_node_log_red_flags(state: dict[str, Any], now: int) -> None:
    for node in NODES:
        result = subprocess.run(
            ["docker", "logs", "--since", f"{NODE_LOG_LOOKBACK_SECONDS}s", node],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=12,
            check=False,
        )
        text = f"{result.stdout}\n{result.stderr}"
        zero_state_count = text.count("Zero state root hash")
        if zero_state_count < ZERO_STATE_ROOT_WARN_COUNT:
            continue
        severity = "critical" if zero_state_count >= ZERO_STATE_ROOT_CRITICAL_COUNT else "warning"
        # The count changes minute to minute during a reorg storm; rate-limit by node
        # and severity so the incident log stays useful instead of becoming the fault.
        signature = f"{node}:{severity}"
        if should_emit(state, f"zero_state_root_{node}", signature, now):
            message = f"{node} logged {zero_state_count} zero-state-root warning(s) in the last {NODE_LOG_LOOKBACK_SECONDS}s"
            details = {
                "node": node,
                "zero_state_root_count": zero_state_count,
                "lookback_seconds": NODE_LOG_LOOKBACK_SECONDS,
                "returncode": result.returncode,
            }
            append_incident("node_zero_state_root_warnings", severity, "stack-sentinel", message, details)
            log(message)
            if severity == "critical":
                notify_user("BlockDAG node red-flag logs", message)


def main() -> int:
    ensure_runtime()
    now = int(time.time())
    state = read_state()

    for unit in [*USER_SERVICES, *USER_TIMERS]:
        start_unit(unit, state, now)

    status, error = status_api()
    state["dashboard_status_ok"] = status is not None
    state["dashboard_status_error"] = error
    if status is None and should_emit(state, "dashboard_status_unavailable", error or "unknown", now):
        append_incident(
            "sentinel_dashboard_status_unavailable",
            "critical",
            "stack-sentinel",
            "Dashboard status API is unavailable to the stack sentinel",
            {"url": DASHBOARD_URL, "error": error},
        )
        notify_user("BlockDAG dashboard status unavailable", error[:160] or "status API timed out")
    elif status is not None:
        overall = str(status.get("overall") or "")
        failures = status.get("failures") if isinstance(status.get("failures"), list) else []
        if overall == "down":
            signature = stable_failure_signature(failures)
            if should_emit(state, "dashboard_overall_down", signature, now):
                append_incident(
                    "sentinel_dashboard_overall_down",
                    "critical",
                    "stack-sentinel",
                    "Dashboard status is down",
                    {
                        "overall": overall,
                        "failures": failures,
                        "miner_failures": status.get("miner_failures"),
                        "stack_failures": status.get("stack_failures"),
                    },
                )
                notify_user("BlockDAG mining degradation", signature[:220])

    inspect_and_repair_containers(status, state, now)
    check_node_log_red_flags(state, now)
    state["updated_at"] = now_iso()
    write_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
