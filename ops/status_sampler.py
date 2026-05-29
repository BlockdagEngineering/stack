#!/usr/bin/env python3
"""Write a shared atomic BlockDAG stack status sample for local agents."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import time
from typing import Any

from incident_journal import append_incident
from pool_ops import (
    EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS,
    LOG_DIR,
    POOL_CONTAINER,
    STATUS_SAMPLER_FILE,
    collect_status_cached,
    docker_compose_command,
    ensure_runtime,
    env_bool,
    now_iso,
    read_neighbor_macs,
    read_latest_earnings_snapshot_info,
    record_earnings_snapshot,
    run,
    split_env_list,
    write_json_file,
    write_status_sampler_payload,
)


def env_float(name: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


DEFAULT_INTERVAL_SECONDS = env_float("BDAG_STATUS_SAMPLER_INTERVAL_SECONDS", 10.0, minimum=1.0)
DEFAULT_EARNINGS_SNAPSHOT_INTERVAL_SECONDS = env_float(
    "BDAG_STATUS_SAMPLER_EARNINGS_SNAPSHOT_INTERVAL_SECONDS",
    float(EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS),
    minimum=0.0,
)
MINING_IMPERATIVE_REPAIR_ENABLED = env_bool("BDAG_MINING_IMPERATIVE_REPAIR_ENABLED", True)
MINING_IMPERATIVE_REPAIR_INTERVAL_SECONDS = env_float(
    "BDAG_MINING_IMPERATIVE_REPAIR_INTERVAL_SECONDS",
    30.0,
    minimum=5.0,
)
MINING_IMPERATIVE_GUARD_UNITS = split_env_list(
    "BDAG_MINING_IMPERATIVE_GUARD_UNITS",
    "bdag-stack-sentinel.timer,bdag-watchdog.service",
)
MINING_IMPERATIVE_START_POOL_ENABLED = env_bool("BDAG_MINING_IMPERATIVE_START_POOL_ENABLED", True)
MINING_IMPERATIVE_START_IDLE_SYNCED_POOL = env_bool("BDAG_MINING_IMPERATIVE_START_IDLE_SYNCED_POOL", True)
LOG_FILE = LOG_DIR / "status-sampler.log"


def log(message: str) -> None:
    ensure_runtime()
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message}\n")


def record_incident(
    event_type: str,
    severity: str,
    message: str,
    details: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    try:
        append_incident(
            event_type,
            severity,
            "status-sampler",
            message,
            details,
            status=payload,
            action=details,
        )
    except Exception as exc:  # noqa: BLE001 - repair must not fail because incident logging failed.
        log(f"mining imperative incident logging failed event={event_type} error={exc}")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def mining_imperative_enabled() -> bool:
    return MINING_IMPERATIVE_REPAIR_ENABLED and env_bool("BDAG_MINING_IMPERATIVE_REPAIR_ENABLED", True)


def systemctl_user(*args: str):
    return run(["systemctl", "--user", *args], timeout=30)


def ensure_user_unit(unit: str, payload: dict[str, Any]) -> bool:
    if not unit:
        return False
    enabled = systemctl_user("is-enabled", unit)
    active = systemctl_user("is-active", unit)
    enabled_text = enabled.stdout.strip()
    active_text = active.stdout.strip()
    if enabled.ok and enabled_text in {"enabled", "static", "generated", "linked"} and active.ok and active_text == "active":
        return False

    action = ["enable", "--now", unit] if not enabled.ok or enabled_text in {"", "disabled", "indirect"} else ["start", unit]
    result = systemctl_user(*action)
    details = {
        "unit": unit,
        "action": " ".join(action),
        "enabled_before": enabled_text,
        "active_before": active_text,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
    if result.ok:
        log(
            "mining imperative repaired user unit "
            f"unit={unit} action={' '.join(action)} enabled_before={enabled_text} active_before={active_text}"
        )
        record_incident(
            "mining_imperative_user_unit_repaired",
            "warning",
            f"Mining imperative guard repaired {unit}",
            details,
            payload,
        )
        return True
    log(f"mining imperative could not repair user unit unit={unit} rc={result.returncode} stderr={result.stderr.strip()}")
    record_incident(
        "mining_imperative_user_unit_repair_failed",
        "critical",
        f"Mining imperative guard could not repair {unit}",
        details,
        payload,
    )
    return False


def chain_ready_for_mining(payload: dict[str, Any]) -> bool:
    sync = dict_value(payload.get("sync_progress"))
    if str(sync.get("status") or "").lower() == "synced":
        return True
    remaining = sync.get("remaining_blocks")
    if remaining is not None and safe_int(remaining, 1) <= 0 and sync.get("chain_block_count") is not None:
        return True
    return payload.get("overall") == "ok" and not payload.get("sync_warnings")


def status_payload_has_miner_demand(payload: dict[str, Any]) -> bool:
    miner_health = dict_value(payload.get("miner_health"))
    if safe_int(miner_health.get("connected_count")) > 0 or safe_int(miner_health.get("managed_count")) > 0:
        return True

    pool = dict_value(payload.get("pool"))
    pool_metrics = dict_value(payload.get("pool_metrics")) or dict_value(pool.get("metrics"))
    if safe_float(pool_metrics.get("active_connections")) > 0:
        return True

    source_job_health = dict_value(pool.get("source_job_health")) or dict_value(pool_metrics.get("source_job_health"))
    return (
        safe_int(source_job_health.get("authorized_miners")) > 0
        or safe_int(source_job_health.get("ready_miners")) > 0
    )


def asic_lan_neighbor_present() -> bool:
    cidrs = split_env_list("BDAG_ASIC_LAN_CIDRS", "")
    if not cidrs:
        target = os.environ.get("BDAG_MINER_SCAN_TARGET", "")
        cidrs = [target] if "/" in target else []
    networks = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            log(f"mining imperative ignored invalid ASIC LAN CIDR {cidr!r}")
    if not networks:
        return False

    for ip_text, mac in read_neighbor_macs().items():
        if not mac:
            continue
        try:
            address = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if any(address in network for network in networks):
            return True
    return False


def pool_container_running(payload: dict[str, Any]) -> bool:
    containers = dict_value(payload.get("containers"))
    container = dict_value(containers.get(POOL_CONTAINER))
    return bool(container.get("running"))


def start_pool_container(payload: dict[str, Any], reason: str) -> bool:
    start = run(["docker", "start", POOL_CONTAINER], timeout=60)
    action = {
        "reason": reason,
        "container": POOL_CONTAINER,
        "method": "docker start",
        **start.as_dict(),
    }
    if start.ok:
        log(f"mining imperative started {POOL_CONTAINER}: {reason}")
        record_incident(
            "mining_imperative_started_pool",
            "critical",
            f"Mining imperative started {POOL_CONTAINER}: {reason}",
            action,
            payload,
        )
        return True

    compose = run(docker_compose_command("up", "-d", "--no-deps", POOL_CONTAINER), timeout=180)
    action = {
        "reason": reason,
        "container": POOL_CONTAINER,
        "method": "docker compose up --no-deps",
        "docker_start": start.as_dict(),
        "compose": compose.as_dict(),
    }
    if compose.ok:
        log(f"mining imperative recreated {POOL_CONTAINER} without dependencies: {reason}")
        record_incident(
            "mining_imperative_recreated_pool",
            "critical",
            f"Mining imperative recreated {POOL_CONTAINER}: {reason}",
            action,
            payload,
        )
        return True

    log(
        f"mining imperative could not start {POOL_CONTAINER}: {reason}; "
        f"docker_start_rc={start.returncode} compose_rc={compose.returncode}"
    )
    record_incident(
        "mining_imperative_pool_start_failed",
        "critical",
        f"Mining imperative could not start {POOL_CONTAINER}: {reason}",
        action,
        payload,
    )
    return False


def mining_imperative_repair(payload: dict[str, Any]) -> dict[str, Any]:
    if not mining_imperative_enabled():
        return {"enabled": False, "actions": []}

    actions: list[str] = []
    for unit in MINING_IMPERATIVE_GUARD_UNITS:
        if ensure_user_unit(unit, payload):
            actions.append(f"repaired_unit:{unit}")

    if MINING_IMPERATIVE_START_POOL_ENABLED and not pool_container_running(payload):
        miner_demand = status_payload_has_miner_demand(payload)
        lan_candidate = asic_lan_neighbor_present()
        chain_ready = chain_ready_for_mining(payload)
        should_start = miner_demand or lan_candidate or (MINING_IMPERATIVE_START_IDLE_SYNCED_POOL and chain_ready)
        if should_start:
            reasons = []
            if miner_demand:
                reasons.append("miner demand is visible in status metrics")
            if lan_candidate:
                reasons.append("ASIC LAN neighbor is present")
            if chain_ready:
                reasons.append("chain is ready")
            if start_pool_container(payload, "; ".join(reasons) or "mining service is required"):
                actions.append(f"started_container:{POOL_CONTAINER}")
        else:
            log(
                f"mining imperative left {POOL_CONTAINER} stopped: "
                "no miner demand, no ASIC LAN neighbor, and chain is not ready"
            )

    return {"enabled": True, "actions": actions}


def write_error_state(error: Exception) -> None:
    write_json_file(
        STATUS_SAMPLER_FILE,
        {
            "schema_version": 1,
            "updated_at": now_iso(),
            "epoch": time.time(),
            "status": "failed",
            "error": str(error),
        },
        mode=0o600,
    )


def sample_once(include_logs: bool) -> dict[str, Any]:
    # max_age_seconds=0 is the explicit hard-bypass path: do not read either
    # the shared sampler file or the short shared cache while producing a sample.
    payload = collect_status_cached(include_logs=include_logs, max_age_seconds=0)
    write_status_sampler_payload(payload, include_logs=include_logs)
    log(
        "sampled "
        f"overall={payload.get('overall')} mode={payload.get('mode')} "
        f"fresh={payload.get('fresh')} include_logs={include_logs}"
    )
    return payload


def maybe_record_earnings_snapshot(
    now_epoch: float,
    last_attempt_epoch: float,
    interval_seconds: float,
    enabled: bool,
) -> float:
    if not enabled or interval_seconds <= 0:
        return last_attempt_epoch
    if last_attempt_epoch and now_epoch - last_attempt_epoch < interval_seconds:
        return last_attempt_epoch

    info = read_latest_earnings_snapshot_info()
    latest_epoch = info.get("latest_epoch")
    try:
        latest_age = now_epoch - float(latest_epoch) if latest_epoch is not None else None
    except (TypeError, ValueError):
        latest_age = None
    if latest_age is not None and latest_age < interval_seconds:
        return last_attempt_epoch

    try:
        snapshot = record_earnings_snapshot()
    except Exception as exc:  # noqa: BLE001 - status sampling must not die on plot history failures.
        log(f"earnings snapshot failed: {exc}")
        return now_epoch
    miners = snapshot.get("miner_estimates")
    miner_count = len(miners) if isinstance(miners, list) else 0
    log(f"earnings snapshot recorded generated_at={snapshot.get('generated_at')} miners={miner_count}")
    return now_epoch


def run_loop(interval_seconds: float, include_logs: bool, earnings_snapshot_interval_seconds: float, record_earnings: bool) -> int:
    ensure_runtime()
    last_earnings_attempt_epoch = 0.0
    last_mining_repair_epoch = 0.0
    while True:
        started = time.time()
        try:
            payload = sample_once(include_logs=include_logs)
            now_epoch = time.time()
            if now_epoch - last_mining_repair_epoch >= MINING_IMPERATIVE_REPAIR_INTERVAL_SECONDS:
                repair = mining_imperative_repair(payload)
                if repair.get("actions"):
                    log(f"mining imperative repair actions={','.join(repair['actions'])}")
                last_mining_repair_epoch = now_epoch
            last_earnings_attempt_epoch = maybe_record_earnings_snapshot(
                time.time(),
                last_earnings_attempt_epoch,
                earnings_snapshot_interval_seconds,
                record_earnings,
            )
        except Exception as exc:  # noqa: BLE001 - sampler must keep trying.
            log(f"sample failed: {exc}")
            try:
                write_error_state(exc)
            except Exception as write_exc:  # noqa: BLE001
                log(f"failed to write error state: {write_exc}")
        elapsed = time.time() - started
        time.sleep(max(1.0, interval_seconds - elapsed))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="keep sampling until the service is stopped")
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument(
        "--earnings-snapshot-interval-seconds",
        type=float,
        default=DEFAULT_EARNINGS_SNAPSHOT_INTERVAL_SECONDS,
        help="append miner/earnings plot snapshots when the valid history is older than this interval; 0 disables",
    )
    parser.add_argument(
        "--no-earnings-snapshots",
        action="store_true",
        help="do not append miner/earnings plot snapshots from the status sampler",
    )
    parser.add_argument("--no-logs", action="store_true", help="omit container log tails from each sample")
    parser.add_argument("--json", action="store_true", help="print the sampled payload")
    args = parser.parse_args()

    include_logs = not args.no_logs
    if args.loop:
        return run_loop(
            max(1.0, args.interval_seconds),
            include_logs,
            max(0.0, args.earnings_snapshot_interval_seconds),
            not args.no_earnings_snapshots,
        )
    try:
        payload = sample_once(include_logs=include_logs)
    except Exception as exc:  # noqa: BLE001
        log(f"sample failed: {exc}")
        write_error_state(exc)
        raise
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
