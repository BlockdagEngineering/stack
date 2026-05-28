#!/usr/bin/env python3
"""Automatic repair worker for the BlockDAG pool stack."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
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
    action_log_path,
    collect_status_cached,
    configure_miner,
    default_miner_pool_settings,
    ensure_runtime,
    is_lan_ipv4,
    now_iso,
    record_earnings_snapshot,
    read_miner_admin_password,
    restore_clean,
    restart_miner,
    restart_miner_open,
    restart_stack,
    run_logged,
    start_stack,
    write_action_state,
)
from rpc_router import recommend_rpc_primary, write_rpc_router_state


STATE_FILE = RUNTIME_DIR / "watchdog-state.json"
WATCHDOG_LOG = LOG_DIR / "watchdog.log"
EFFICIENCY_EVENTS_FILE = LOG_DIR / "efficiency-events.jsonl"
LOCK_FILE = RUNTIME_DIR / "repair.lock"
DIRTY_SHUTDOWN_MARKER = RUNTIME_DIR / "dirty-shutdown.marker"
HOURLY_SNAPSHOT_LOCK_FILE = RUNTIME_DIR / "hourly-chain-snapshot.lock"
AUTONOMOUS_STACK_LAB_LOCK_FILE = RUNTIME_DIR / "autonomous-stack-lab.lock"

DEFAULT_INTERVAL_SECONDS = int(os.environ.get("BDAG_WATCHDOG_INTERVAL", "60"))
DEFAULT_FAILURE_THRESHOLD = int(os.environ.get("BDAG_WATCHDOG_FAILURE_THRESHOLD", "3"))
DEFAULT_CLEAN_RESTORE_COOLDOWN = int(os.environ.get("BDAG_CLEAN_RESTORE_COOLDOWN", "1800"))
DEFAULT_SYNCING_THRESHOLD = int(os.environ.get("BDAG_WATCHDOG_SYNCING_THRESHOLD", "5"))
DEFAULT_SYNCING_RESTART_COOLDOWN = int(os.environ.get("BDAG_SYNCING_RESTART_COOLDOWN", "900"))
DEFAULT_ACTIVE_SYNC_IMPORT_GRACE_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_ACTIVE_SYNC_IMPORT_GRACE_SECONDS", "300")
)
DEFAULT_SHARE_STALL_THRESHOLD = int(os.environ.get("BDAG_WATCHDOG_SHARE_STALL_THRESHOLD", "2"))
DEFAULT_SHARE_STALL_RESTART_COOLDOWN = int(
    os.environ.get("BDAG_WATCHDOG_SHARE_STALL_RESTART_COOLDOWN", os.environ.get("BDAG_SYNCING_RESTART_COOLDOWN", "900"))
)
DEFAULT_SUBMIT_PATH_STALL_THRESHOLD = int(os.environ.get("BDAG_WATCHDOG_SUBMIT_PATH_STALL_THRESHOLD", "1"))
DEFAULT_SUBMIT_PATH_REPAIR_COOLDOWN = int(os.environ.get("BDAG_WATCHDOG_SUBMIT_PATH_REPAIR_COOLDOWN", "120"))
DEFAULT_SUBMIT_PATH_SELF_RECOVERY_GRACE_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_SUBMIT_PATH_SELF_RECOVERY_GRACE_SECONDS", "90")
)
DEFAULT_MINER_DOWN_RESTART_SECONDS = int(os.environ.get("BDAG_WATCHDOG_MINER_DOWN_RESTART_SECONDS", "120"))
DEFAULT_MINER_RESTART_COOLDOWN = int(os.environ.get("BDAG_WATCHDOG_MINER_RESTART_COOLDOWN", "300"))
DEFAULT_ASIC_DEGRADED_SECONDS = int(os.environ.get("BDAG_WATCHDOG_ASIC_DEGRADED_SECONDS", "120"))
DEFAULT_ASIC_DEGRADED_REPAIR_COOLDOWN = int(os.environ.get("BDAG_WATCHDOG_ASIC_DEGRADED_REPAIR_COOLDOWN", "180"))
DEFAULT_ASIC_HASHRATE_MIN_GHS = float(os.environ.get("BDAG_WATCHDOG_ASIC_HASHRATE_MIN_GHS", "180"))
DEFAULT_ASIC_HASHRATE_STALE_SECONDS = int(os.environ.get("BDAG_WATCHDOG_ASIC_HASHRATE_STALE_SECONDS", "120"))
DEFAULT_ASIC_HASHRATE_CONFIRM_SECONDS = int(os.environ.get("BDAG_WATCHDOG_ASIC_HASHRATE_CONFIRM_SECONDS", "90"))
DEFAULT_ASIC_HASHRATE_REPAIR_COOLDOWN = int(
    os.environ.get("BDAG_WATCHDOG_ASIC_HASHRATE_REPAIR_COOLDOWN", str(DEFAULT_MINER_RESTART_COOLDOWN))
)
DEFAULT_ASIC_HASHRATE_STARTUP_GRACE_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_ASIC_HASHRATE_STARTUP_GRACE_SECONDS", "180")
)
DEFAULT_MINER_USEFUL_WORK_STALL_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_MINER_USEFUL_WORK_STALL_SECONDS", "150")
)
DEFAULT_MINER_USEFUL_WORK_STALL_CONFIRM_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_MINER_USEFUL_WORK_STALL_CONFIRM_SECONDS", "60")
)
DEFAULT_MINER_USEFUL_WORK_STALL_REPAIR_COOLDOWN = int(
    os.environ.get("BDAG_WATCHDOG_MINER_USEFUL_WORK_STALL_REPAIR_COOLDOWN", "600")
)
DEFAULT_MINER_USEFUL_WORK_MIN_HEALTHY_PEERS = int(
    os.environ.get("BDAG_WATCHDOG_MINER_USEFUL_WORK_MIN_HEALTHY_PEERS", "2")
)
DEFAULT_MINER_USEFUL_WORK_MIN_POOL_VALID_SHARES = int(
    os.environ.get("BDAG_WATCHDOG_MINER_USEFUL_WORK_MIN_POOL_VALID_SHARES", "5")
)
DEFAULT_POOL_RESTART_GRACE_SECONDS = int(os.environ.get("BDAG_WATCHDOG_POOL_RESTART_GRACE_SECONDS", "90"))
DEFAULT_EARNINGS_SNAPSHOT_INTERVAL_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_EARNINGS_SNAPSHOT_INTERVAL_SECONDS", "120")
)
DEFAULT_NODE_TEMPLATE_RESTART_COOLDOWN = int(os.environ.get("BDAG_WATCHDOG_NODE_TEMPLATE_RESTART_COOLDOWN", "180"))
DEFAULT_NODE_ORPHAN_STORM_RESTART_COOLDOWN = int(
    os.environ.get("BDAG_WATCHDOG_NODE_ORPHAN_STORM_RESTART_COOLDOWN", "300")
)
DEFAULT_RPC_FAILOVER_SWITCH_COOLDOWN = int(os.environ.get("BDAG_WATCHDOG_RPC_FAILOVER_SWITCH_COOLDOWN", "180"))
DEFAULT_RPC_FAILOVER_URGENT_SWITCH_COOLDOWN = int(
    os.environ.get("BDAG_WATCHDOG_RPC_FAILOVER_URGENT_SWITCH_COOLDOWN", "60")
)
DEFAULT_OPTIMUM_STATE_EVENT_COOLDOWN = int(os.environ.get("BDAG_WATCHDOG_OPTIMUM_STATE_EVENT_COOLDOWN", "300"))
RPC_FAILOVER_SERVICE = os.environ.get("BDAG_RPC_FAILOVER_SERVICE", "rpc-failover")
RPC_FAILOVER_ENABLED = os.environ.get("BDAG_RPC_FAILOVER_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
HAPROXY_CFG = PROJECT_ROOT / "haproxy.cfg"
HAPROXY_RUNTIME_DNS_OPTIONS = "resolvers docker init-addr libc,none"
NODE_TO_HAPROXY_SERVER = {
    "bdag-miner-node-1": "node1",
    "bdag-miner-node-2": "node2",
}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


AUTOMATIC_CLEAN_RESTORE_ENABLED = env_bool("BDAG_ENABLE_AUTOMATIC_CLEAN_RESTORE", False)
BOOT_REPAIR_DIRTY_POLICY = os.environ.get("BDAG_BOOT_REPAIR_DIRTY_POLICY", "start").strip().lower()
BOOT_REPAIR_CRITICAL_POLICY = os.environ.get("BDAG_BOOT_REPAIR_CRITICAL_POLICY", "restart").strip().lower()


def log(message: str) -> None:
    ensure_runtime()
    with WATCHDOG_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message}\n")


def ensure_efficiency_event_log() -> None:
    ensure_runtime()
    EFFICIENCY_EVENTS_FILE.touch(exist_ok=True)


def record_efficiency_event(event_type: str, severity: str, message: str, details: dict[str, Any] | None = None) -> None:
    try:
        ensure_efficiency_event_log()
        payload = {
            "generated_at": now_iso(),
            "event_type": event_type,
            "severity": severity,
            "message": message,
            "details": details or {},
        }
        with EFFICIENCY_EVENTS_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
        append_incident(event_type, severity, "watchdog", message, details or {})
    except Exception as exc:  # noqa: BLE001 - monitoring must never crash repair.
        try:
            log(f"failed to record efficiency event type={event_type}: {exc}")
        except Exception:
            pass


def record_failed_repair(action: str, reason: str, details: dict[str, Any] | None = None) -> None:
    payload = {"reason": reason}
    if details:
        payload.update(details)
    record_efficiency_event("repair_failed", "critical", f"{action} failed", payload)
    append_incident("repair_failed", "critical", "repair", f"{action} failed", payload)


def miner_label(row: dict[str, Any]) -> str:
    name = str(row.get("display_name") or row.get("ip") or "miner")
    ip = str(row.get("ip") or "")
    return f"{name} {ip}" if ip and ip not in name else name


def container_started_age_seconds(status: dict[str, Any], container_name: str, now: int) -> int | None:
    containers = status.get("containers") if isinstance(status.get("containers"), dict) else {}
    container = containers.get(container_name) if isinstance(containers, dict) else None
    if not isinstance(container, dict):
        return None
    started_at = str(container.get("started_at") or "")
    if not started_at or started_at.startswith("0001-"):
        return None
    try:
        # Docker timestamps include nanoseconds; seconds precision is enough for watchdog grace windows.
        started = datetime.strptime(started_at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, now - int(started.timestamp()))


def is_primary_pool_identity(row: dict[str, Any], mining_address: str) -> bool:
    defaults = default_miner_pool_settings()
    expected_url = str(row.get("expected_pool_url") or "")
    expected = str(row.get("expected_worker_user") or "").lower()
    workers = [str(item).lower() for item in row.get("workers", []) if item]
    if expected_url == defaults["pool_url"] and (
        re.fullmatch(r"0x[a-f0-9]{40}", expected)
        or any(re.fullmatch(r"0x[a-f0-9]{40}", worker) for worker in workers)
    ):
        return True
    address = mining_address.lower()
    if address:
        return expected == address or address in workers
    return row.get("device_type") in {"asic", "stratum"} and row.get("display_name") != "Nestor"


def is_primary_pool_miner(row: dict[str, Any], mining_address: str) -> bool:
    if not is_primary_pool_identity(row, mining_address):
        return False
    if row.get("work_pool_active") is not None:
        return bool(row.get("work_pool_active"))
    return bool(
        row.get("connected")
        and (
            row.get("managed")
            or row.get("configured")
            or int(row.get("submits") or 0) > 0
            or int(row.get("shares") or 0) > 0
            or int(row.get("blocks_found") or 0) > 0
        )
    )


def int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def pool_initial_download_effective(status: dict[str, Any]) -> bool:
    pool_health = status.get("pool_health") if isinstance(status.get("pool_health"), dict) else {}
    if not pool_health.get("initial_download"):
        return False
    sync_health = status.get("sync_health") if isinstance(status.get("sync_health"), dict) else {}
    if sync_health.get("pool_initial_download_transient"):
        return False
    sync_progress = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    remaining = int_or_none(sync_progress.get("remaining_blocks"))
    connected = int_or_none(((status.get("miner_health") or {}).get("connected_count") if isinstance(status.get("miner_health"), dict) else 0)) or 0
    share_age = int_or_none(pool_health.get("last_valid_share_age_seconds"))
    job_age = int_or_none(pool_health.get("last_job_notify_age_seconds"))
    fresh_mining = connected > 0 and (
        (share_age is not None and share_age <= DEFAULT_ASIC_HASHRATE_STALE_SECONDS)
        or (job_age is not None and job_age <= DEFAULT_ASIC_HASHRATE_STALE_SECONDS)
    )
    if sync_progress.get("status") == "synced" and (remaining is None or remaining == 0) and fresh_mining:
        return False
    return True


def pool_has_recent_mining_work(status: dict[str, Any], freshness_seconds: int = 60) -> bool:
    sync_health = status.get("sync_health") if isinstance(status.get("sync_health"), dict) else {}
    if sync_health.get("pool_has_recent_mining"):
        return True
    pool_health = status.get("pool_health") if isinstance(status.get("pool_health"), dict) else {}
    share_age = int_or_none(pool_health.get("last_valid_share_age_seconds"))
    block_age = int_or_none(pool_health.get("last_block_submit_age_seconds"))
    valid_shares = int_or_none(pool_health.get("valid_share_count")) or 0
    accepted_blocks = int_or_none(pool_health.get("block_submit_success_count")) or 0
    return bool(
        (valid_shares > 0 and share_age is not None and share_age <= freshness_seconds)
        or (accepted_blocks > 0 and block_age is not None and block_age <= freshness_seconds)
    )


def miner_debug_hashrate_ghs(row: dict[str, Any]) -> float | None:
    debug = row.get("debug") if isinstance(row.get("debug"), dict) else {}
    return float_or_none(debug.get("hashrate")) or float_or_none(debug.get("av_hashrate"))


def asic_hashrate_issue_primary_miners(
    status: dict[str, Any],
    min_hashrate_ghs: float,
    stale_seconds: int,
) -> list[dict[str, Any]]:
    if min_hashrate_ghs <= 0 and stale_seconds <= 0:
        return []
    pool_health = status.get("pool_health") if isinstance(status.get("pool_health"), dict) else {}
    if pool_initial_download_effective(status) or int(pool_health.get("job_notify_count") or 0) <= 0:
        return []
    mining_address = str(status.get("mining_address") or "")
    miners = ((status.get("miner_health") or {}).get("miners") or [])
    affected: list[dict[str, Any]] = []
    for row in miners:
        if not isinstance(row, dict) or not is_primary_pool_miner(row, mining_address):
            continue
        if row.get("device_type") != "asic" or not is_lan_ipv4(str(row.get("ip", ""))):
            continue
        debug = row.get("debug") if isinstance(row.get("debug"), dict) else {}
        uptime = int_or_none(debug.get("uptime_seconds"))
        if uptime is not None and uptime < DEFAULT_ASIC_HASHRATE_STARTUP_GRACE_SECONDS:
            continue
        hashrate = miner_debug_hashrate_ghs(row)
        share_age = int_or_none(row.get("last_share_age_seconds"))
        submit_age = int_or_none(row.get("last_submit_age_seconds"))
        api_unavailable = (
            debug.get("available") is False
            or bool(row.get("debug_error"))
            or (hashrate is None and row.get("status") == "degraded")
        )
        low_hashrate = hashrate is not None and hashrate < min_hashrate_ghs
        telemetry_blind_and_stale = (
            api_unavailable
            and hashrate is None
            and share_age is not None
            and share_age >= stale_seconds
            and (submit_age is None or submit_age <= stale_seconds * 2)
        )
        if not low_hashrate and not telemetry_blind_and_stale:
            continue
        item = dict(row)
        item["hashrate_ghs"] = hashrate
        item["hashrate_min_ghs"] = min_hashrate_ghs
        item["telemetry_blind"] = api_unavailable and hashrate is None
        item["last_share_age_seconds"] = share_age
        item["last_submit_age_seconds"] = submit_age
        affected.append(item)
    return affected


def degraded_primary_miners(status: dict[str, Any], stale_seconds: int) -> list[dict[str, Any]]:
    pool_health = status.get("pool_health") if isinstance(status.get("pool_health"), dict) else {}
    if pool_initial_download_effective(status) or int(pool_health.get("job_notify_count") or 0) <= 0:
        return []
    miner_health = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}
    lane_balance = miner_health.get("lane_balance") if isinstance(miner_health.get("lane_balance"), dict) else {}
    expected_lane_count = int_or_none(lane_balance.get("expected_lane_count"))
    imbalanced_count = int_or_none(lane_balance.get("imbalanced_count"))
    if expected_lane_count == 0 or imbalanced_count == 0:
        return []
    now = int(time.time())
    mining_address = str(status.get("mining_address") or "")
    miners = miner_health.get("miners") or []
    degraded: list[dict[str, Any]] = []
    for row in miners:
        if not isinstance(row, dict) or not is_primary_pool_miner(row, mining_address):
            continue
        if not is_lan_ipv4(str(row.get("ip", ""))):
            continue
        lane_status = str(row.get("lane_status") or "")
        if lane_status in {"balanced", "high", "no-window-work", "not-tracked"}:
            continue
        if lane_status and lane_status not in {"low", "no-work"}:
            continue
        submits = int(row.get("submits") or 0)
        shares = int(row.get("shares") or 0)
        blocks = int(row.get("blocks_found") or 0)
        last_submit_epoch = int(row.get("last_submit_epoch") or 0)
        if not last_submit_epoch and submits > 0:
            last_submit_epoch = int(row.get("last_pool_seen_epoch") or 0)
        last_share_epoch = int(row.get("last_share_epoch") or 0)
        recently_submitted = bool(last_submit_epoch and now - last_submit_epoch <= stale_seconds * 2)
        share_age = now - last_share_epoch if last_share_epoch else None
        if recently_submitted and blocks == 0 and (shares == 0 or share_age is None or share_age >= stale_seconds):
            item = dict(row)
            item["last_share_age_seconds"] = share_age
            item["last_submit_age_seconds"] = now - last_submit_epoch if last_submit_epoch else None
            degraded.append(item)
    return degraded


def low_difficulty_primary_miners(status: dict[str, Any]) -> list[dict[str, Any]]:
    mining_address = str(status.get("mining_address") or "")
    miners = ((status.get("miner_health") or {}).get("miners") or [])
    return [
        dict(row)
        for row in miners
        if isinstance(row, dict)
        and is_primary_pool_identity(row, mining_address)
        and row.get("low_difficulty_flood")
        and is_lan_ipv4(str(row.get("ip", "")))
    ]


def useful_work_stalled_primary_miners(
    status: dict[str, Any],
    stall_seconds: int = DEFAULT_MINER_USEFUL_WORK_STALL_SECONDS,
) -> list[dict[str, Any]]:
    pool_health = status.get("pool_health", status.get("pool", {}))
    miner_health = status.get("miner_health", {})
    mining_address = str(status.get("mining_address") or "")
    miners = miner_health.get("miners", []) if isinstance(miner_health.get("miners"), list) else []
    primary_rows = [
        row
        for row in miners
        if isinstance(row, dict)
        and is_primary_pool_miner(row, mining_address)
        and row.get("device_type") in {"asic", "stratum"}
        and is_lan_ipv4(str(row.get("ip", "")))
    ]
    if len(primary_rows) <= DEFAULT_MINER_USEFUL_WORK_MIN_HEALTHY_PEERS:
        return []

    # Do not reboot one ASIC for a pool-wide or backend-wide problem.
    if any(
        bool(pool_health.get(key))
        for key in (
            "share_stall",
            "job_stall",
            "pool_template_frozen",
            "duplicate_block_storm",
            "stale_job_candidate_storm",
            "block_submit_error_storm",
            "accepted_job_expired_storm",
            "block_submit_zero_success_storm",
            "initial_download",
            "rpc_refused",
        )
    ):
        return []
    if template_failing_nodes(status) or active_rpc_template_failing(status):
        return []
    if int(pool_health.get("valid_share_count") or 0) < DEFAULT_MINER_USEFUL_WORK_MIN_POOL_VALID_SHARES:
        return []

    healthy_peers = 0
    for row in primary_rows:
        shares = int(row.get("shares") or 0)
        blocks = int(row.get("blocks_found") or 0)
        share_age = row.get("last_share_age_seconds")
        if shares > 0 or blocks > 0:
            healthy_peers += 1
        elif share_age is not None and int(share_age) < stall_seconds:
            healthy_peers += 1
    if healthy_peers < DEFAULT_MINER_USEFUL_WORK_MIN_HEALTHY_PEERS:
        return []

    stalled: list[dict[str, Any]] = []
    for row in primary_rows:
        ip = str(row.get("ip") or "")
        shares = int(row.get("shares") or 0)
        blocks = int(row.get("blocks_found") or 0)
        share_age = row.get("last_share_age_seconds")
        pool_seen_age = row.get("last_pool_seen_age_seconds")
        if share_age is None or int(share_age) < stall_seconds:
            continue
        if shares > 0 or blocks > 0:
            continue
        if not row.get("connected"):
            continue
        if pool_seen_age is not None and int(pool_seen_age) > stall_seconds * 2:
            continue
        if row.get("status") == "down":
            continue
        # The goal is pre-failure recovery. If the miner API is already unreachable,
        # let the hard-down miner repair path handle it.
        if row.get("api_error") and not row.get("pool_active"):
            continue
        item = dict(row)
        item["useful_work_stall_age_seconds"] = int(share_age)
        item["pool_seen_age_seconds"] = int(pool_seen_age) if pool_seen_age is not None else None
        item["healthy_peer_count"] = healthy_peers
        item["pool_valid_share_count"] = int(pool_health.get("valid_share_count") or 0)
        stalled.append(item)
    return stalled


def template_failing_nodes(status: dict[str, Any]) -> list[str]:
    nodes = status.get("nodes", {}) or {}
    return [
        node
        for node in NODES
        if (nodes.get(node, {}) or {}).get("mining_template_failing")
    ]


def orphan_storm_nodes(status: dict[str, Any]) -> list[str]:
    nodes = status.get("nodes", {}) or {}
    return [
        node
        for node in NODES
        if (nodes.get(node, {}) or {}).get("orphan_block_error_storm")
    ]


def active_rpc_template_failing(status: dict[str, Any]) -> bool:
    probe = (status.get("rpc_template_health") or {}).get("rpc_failover")
    return bool(isinstance(probe, dict) and probe.get("failing"))


def choose_template_probe_repair_node(status: dict[str, Any], current_primary: str | None) -> str | None:
    nodes = status.get("nodes", {}) or {}
    failing = [
        node
        for node in NODES
        if (nodes.get(node, {}) or {}).get("template_probe_failing")
    ]
    if not failing:
        return None

    candidates = list(failing)
    if current_primary in candidates and len(candidates) > 1:
        candidates = [node for node in candidates if node != current_primary]

    def sort_key(node: str) -> tuple[float, int, str]:
        info = nodes.get(node, {}) or {}
        return (
            float(info.get("template_probe_error_ratio") or 0.0),
            int(info.get("template_probe_error_count") or 0),
            node,
        )

    candidates.sort(key=sort_key, reverse=True)
    return candidates[0]


def rpc_probe_failing_nodes(status: dict[str, Any]) -> list[str]:
    health = status.get("rpc_template_health") if isinstance(status.get("rpc_template_health"), dict) else {}
    probes = health.get("nodes") if isinstance(health.get("nodes"), dict) else {}
    return [
        node
        for node in NODES
        if isinstance(probes.get(node), dict) and probes[node].get("failing")
    ]


def choose_active_rpc_repair_node(status: dict[str, Any], current_primary: str | None) -> str | None:
    failing = rpc_probe_failing_nodes(status)
    if current_primary in failing:
        return current_primary
    return choose_template_probe_repair_node(status, current_primary)


def read_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "consecutive_failures": 0,
            "consecutive_syncing": 0,
            "consecutive_share_stalls": 0,
            "consecutive_submit_path_stalls": 0,
            "last_repair_at": 0,
            "last_sync_repair_at": 0,
            "last_share_repair_at": 0,
            "last_submit_path_repair_at": 0,
            "last_clean_restore_at": 0,
            "last_status": "unknown",
        }
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {
            "consecutive_failures": 0,
            "consecutive_syncing": 0,
            "consecutive_share_stalls": 0,
            "consecutive_submit_path_stalls": 0,
            "last_repair_at": 0,
            "last_sync_repair_at": 0,
            "last_share_repair_at": 0,
            "last_submit_path_repair_at": 0,
            "last_clean_restore_at": 0,
            "last_status": "unknown",
        }


def write_state(state: dict[str, Any]) -> None:
    ensure_runtime()
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def read_dirty_shutdown_marker() -> dict[str, Any] | None:
    if not DIRTY_SHUTDOWN_MARKER.exists():
        return None
    try:
        return json.loads(DIRTY_SHUTDOWN_MARKER.read_text())
    except json.JSONDecodeError:
        return {"raw": DIRTY_SHUTDOWN_MARKER.read_text(errors="replace")}


def clear_dirty_shutdown_marker() -> None:
    try:
        DIRTY_SHUTDOWN_MARKER.unlink()
    except FileNotFoundError:
        pass


def write_dirty_shutdown_marker(reason: str) -> None:
    ensure_runtime()
    payload = {
        "reason": reason,
        "written_at": now_iso(),
        "pid": os.getpid(),
    }
    DIRTY_SHUTDOWN_MARKER.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def acquire_lock(blocking: bool = False):
    ensure_runtime()
    lock_handle = LOCK_FILE.open("w")
    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    try:
        fcntl.flock(lock_handle.fileno(), flags)
        return lock_handle
    except BlockingIOError:
        lock_handle.close()
        return None


def lock_is_held(path: Path) -> bool:
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return True
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()
    return False


def refresh_maintenance_state(state: dict, snapshot_active: bool, autonomous_lab_active: bool) -> None:
    previous = state.get("maintenance") if isinstance(state.get("maintenance"), dict) else {}
    previous_active = bool(previous.get("active"))
    previous_reason = str(previous.get("reason") or "")

    reason = ""
    if snapshot_active:
        reason = "hourly snapshot lock is held"
    elif autonomous_lab_active:
        reason = "autonomous stack lab lock is held"

    if reason:
        state["maintenance"] = {
            "active": True,
            "reason": reason,
            "updated_at": now_iso(),
        }
        return

    if previous_active:
        log(f"maintenance guard cleared: {previous_reason or 'unknown reason'}")
    state["maintenance"] = {
        "active": False,
        "reason": "",
        "updated_at": now_iso(),
    }


def run_repair(mode: str, reason: str) -> bool:
    lock_handle = acquire_lock(blocking=False)
    if lock_handle is None:
        log(f"repair skipped because another repair is running; requested={mode} reason={reason}")
        return False

    started = time.time()
    action_name = f"{mode}-repair"
    log_path = action_log_path(action_name)
    state_payload = {
        "name": action_name,
        "mode": mode,
        "reason": reason,
        "status": "running",
        "started_at": now_iso(),
        "finished_at": None,
        "log_path": str(log_path),
    }
    write_action_state(state_payload)
    log(f"starting {mode} repair: {reason}; log={log_path}")

    try:
        if mode == "start":
            ok = start_stack(log_path)
        elif mode == "restart":
            ok = restart_stack(log_path)
        elif mode == "clean":
            ok = restore_clean(log_path)
        else:
            raise ValueError(f"unknown repair mode: {mode}")
    except Exception as exc:  # noqa: BLE001 - keep watchdog alive and record failure.
        ok = False
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[{now_iso()}] repair crashed: {exc}\n")

    state_payload.update(
        {
            "status": "ok" if ok else "failed",
            "finished_at": now_iso(),
            "elapsed": round(time.time() - started, 3),
        }
    )
    write_action_state(state_payload)
    log(f"finished {mode} repair status={state_payload['status']} elapsed={state_payload['elapsed']}s")
    if not ok:
        record_failed_repair(f"{mode} stack repair", reason, {"log_path": str(log_path)})
    lock_handle.close()
    return ok


def run_node_restart(node_service: str, reason: str) -> bool:
    if node_service not in NODES:
        log(f"targeted node restart skipped for unknown node={node_service} reason={reason}")
        return False

    lock_handle = acquire_lock(blocking=False)
    if lock_handle is None:
        log(f"targeted node restart skipped because another repair is running; node={node_service} reason={reason}")
        return False

    started = time.time()
    action_name = f"restart-{node_service}"
    log_path = action_log_path(action_name)
    state_payload = {
        "name": action_name,
        "mode": "restart-node",
        "node": node_service,
        "reason": reason,
        "status": "running",
        "started_at": now_iso(),
        "finished_at": None,
        "log_path": str(log_path),
    }
    write_action_state(state_payload)
    log(f"starting targeted restart for {node_service}: {reason}; log={log_path}")

    command = [
        "docker",
        "compose",
        "--env-file",
        str(POOL_ENV_FILE),
        "-f",
        str(PROJECT_ROOT / "docker-compose.yml"),
        "restart",
        node_service,
    ]
    result = run_logged(command, log_path, timeout=180)
    ok = result.ok

    state_payload.update(
        {
            "status": "ok" if ok else "failed",
            "finished_at": now_iso(),
            "elapsed": round(time.time() - started, 3),
        }
    )
    write_action_state(state_payload)
    log(f"finished targeted restart for {node_service} status={state_payload['status']} elapsed={state_payload['elapsed']}s")
    if not ok:
        record_failed_repair(
            f"targeted node restart for {node_service}",
            reason,
            {"node": node_service, "log_path": str(log_path)},
        )
    lock_handle.close()
    return ok


def current_rpc_primary() -> str | None:
    if not RPC_FAILOVER_ENABLED:
        return None
    try:
        lines = HAPROXY_CFG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        match = re.match(r"\s*server\s+(node[12])\s+(bdag-miner-node-[12]):38131\b(.*)$", line)
        if not match:
            continue
        options = match.group(3)
        if " backup" not in f" {options} ":
            node = match.group(2)
            return node if node in NODES else None
    return None


def render_rpc_primary_config(primary_node: str) -> str:
    server_name = NODE_TO_HAPROXY_SERVER.get(primary_node)
    if not server_name:
        raise ValueError(f"unknown rpc primary node: {primary_node}")

    lines = HAPROXY_CFG.read_text(encoding="utf-8").splitlines()
    rendered: list[str] = []
    seen = set()
    for line in lines:
        match = re.match(r"(\s*)server\s+(node[12])\s+(bdag-miner-node-[12]):38131\b.*$", line)
        if not match:
            rendered.append(line)
            continue
        indent, haproxy_name, node_service = match.groups()
        options = f"check inter 5s fall 3 rise 2 {HAPROXY_RUNTIME_DNS_OPTIONS}"
        if node_service != primary_node:
            options += " backup"
        rendered.append(f"{indent}server {haproxy_name} {node_service}:38131 {options}")
        seen.add(node_service)

    if not set(NODES).issubset(seen):
        raise ValueError("haproxy config does not contain all BlockDAG node backends")
    return "\n".join(rendered) + "\n"


def healthy_rpc_alternate(status: dict[str, Any], failing_nodes: list[str], current_primary: str | None) -> str | None:
    decision = recommend_rpc_primary(status, current_primary=current_primary, failing_nodes=failing_nodes)
    write_rpc_router_state(status, decision)
    recommended = decision.get("recommended_primary")
    if decision.get("should_switch") and recommended in NODES and recommended != current_primary:
        return str(recommended)
    return None


def run_rpc_failover_switch(primary_node: str, reason: str) -> bool:
    lock_handle = acquire_lock(blocking=False)
    if lock_handle is None:
        log(f"rpc failover switch skipped because another repair is running; primary={primary_node} reason={reason}")
        return False

    started = time.time()
    action_name = f"switch-{RPC_FAILOVER_SERVICE}"
    log_path = action_log_path(action_name)
    previous_primary = current_rpc_primary()
    state_payload = {
        "name": action_name,
        "mode": "switch-rpc-primary",
        "service": RPC_FAILOVER_SERVICE,
        "previous_primary": previous_primary,
        "new_primary": primary_node,
        "reason": reason,
        "status": "running",
        "started_at": now_iso(),
        "finished_at": None,
        "log_path": str(log_path),
    }
    write_action_state(state_payload)
    log(
        f"starting rpc primary switch {previous_primary or 'unknown'} -> {primary_node}: "
        f"{reason}; log={log_path}"
    )

    old_text = HAPROXY_CFG.read_text(encoding="utf-8")
    backup_path = RUNTIME_DIR / f"haproxy.cfg.{int(time.time())}.bak"
    backup_path.write_text(old_text, encoding="utf-8")
    ok = False
    error = ""
    try:
        new_text = render_rpc_primary_config(primary_node)
        HAPROXY_CFG.write_text(new_text, encoding="utf-8")
        validate = run_logged(
            [
                "docker",
                "exec",
                RPC_FAILOVER_SERVICE,
                "haproxy",
                "-c",
                "-f",
                "/usr/local/etc/haproxy/haproxy.cfg",
            ],
            log_path,
            timeout=60,
        )
        if not validate.ok:
            raise RuntimeError("HAProxy config validation failed")
        result = run_logged(
            [
                "docker",
                "compose",
                "--env-file",
                str(POOL_ENV_FILE),
                "-f",
                str(PROJECT_ROOT / "docker-compose.yml"),
                "restart",
                RPC_FAILOVER_SERVICE,
            ],
            log_path,
            timeout=120,
        )
        ok = result.ok
        if not ok:
            raise RuntimeError(f"{RPC_FAILOVER_SERVICE} restart failed")
    except Exception as exc:  # noqa: BLE001 - restore previous routing on any failure.
        error = str(exc)
        HAPROXY_CFG.write_text(old_text, encoding="utf-8")

    state_payload.update(
        {
            "status": "ok" if ok else "failed",
            "finished_at": now_iso(),
            "elapsed": round(time.time() - started, 3),
            "backup_path": str(backup_path),
        }
    )
    if error:
        state_payload["error"] = error
    write_action_state(state_payload)
    log(
        f"finished rpc primary switch status={state_payload['status']} "
        f"primary={primary_node} elapsed={state_payload['elapsed']}s"
    )
    if not ok:
        record_failed_repair("rpc primary switch", reason, {"service": RPC_FAILOVER_SERVICE, "error": error})
    else:
        append_incident(
            "rpc_primary_switch",
            "warning",
            "rpc-failover",
            f"rpc primary switched to {primary_node}",
            {
                "previous_primary": previous_primary,
                "new_primary": primary_node,
                "reason": reason,
                "log_path": str(log_path),
            },
        )
    lock_handle.close()
    return ok


def rpc_router_node_score(decision: dict[str, Any], node: str | None) -> float:
    if not node:
        return 0.0
    scores = decision.get("scores") if isinstance(decision.get("scores"), dict) else {}
    node_score = scores.get(node) if isinstance(scores.get(node), dict) else {}
    return float(node_score.get("score") or 0.0)


def rpc_router_switch_cooldown(decision: dict[str, Any]) -> int:
    pressure = decision.get("pool_pressure") if isinstance(decision.get("pool_pressure"), dict) else {}
    current_score = rpc_router_node_score(decision, str(decision.get("current_primary") or ""))
    urgent_pressure = any(
        bool(pressure.get(key))
        for key in (
            "initial_download",
            "rpc_refused",
            "share_stall",
            "job_stall",
            "rpc_template_failing",
            "node_template_probe_failing",
            "pool_template_frozen",
            "duplicate_block_storm",
            "stale_job_candidate_storm",
            "block_submit_error_storm",
            "accepted_job_expired_storm",
            "block_submit_zero_success_storm",
        )
    )
    if float(decision.get("score_delta") or 0.0) >= 40.0 or current_score <= 30.0 or urgent_pressure:
        return min(DEFAULT_RPC_FAILOVER_SWITCH_COOLDOWN, DEFAULT_RPC_FAILOVER_URGENT_SWITCH_COOLDOWN)
    return DEFAULT_RPC_FAILOVER_SWITCH_COOLDOWN


def record_optimum_state_observation(
    status: dict[str, Any],
    state: dict[str, Any],
    decision: dict[str, Any] | None,
    now: int,
) -> None:
    if not isinstance(decision, dict):
        return

    current_primary = str(decision.get("current_primary") or current_rpc_primary() or "")
    if not current_primary:
        return
    scores = decision.get("scores") if isinstance(decision.get("scores"), dict) else {}
    current_score = scores.get(current_primary) if isinstance(scores.get(current_primary), dict) else {}
    pressure = decision.get("pool_pressure") if isinstance(decision.get("pool_pressure"), dict) else {}
    current_state = str(current_score.get("state") or "unknown")
    current_score_value = float(current_score.get("score") or 0.0)
    quality_reasons = [str(item) for item in pressure.get("pool_quality_reasons") or [] if item]
    router_reasons = [item.strip() for item in str(decision.get("reason") or "").split(",") if item.strip()]

    watched = bool(
        decision.get("should_switch")
        or decision.get("current_primary_suboptimal")
        or current_score_value < 95
        or pressure.get("hard_pool_pressure")
        or pressure.get("pool_quality_pressure")
    )
    if not watched:
        return

    signature = json.dumps(
        {
            "current_primary": current_primary,
            "current_haproxy_primary": decision.get("current_haproxy_primary"),
            "pool_selected_backend": decision.get("pool_selected_backend"),
            "pool_selected_backend_node": decision.get("pool_selected_backend_node"),
            "routing_alignment": decision.get("routing_alignment"),
            "recommended_primary": decision.get("recommended_primary"),
            "current_score": round(current_score_value, 1),
            "current_state": current_state,
            "router_reasons": router_reasons,
            "quality_reasons": quality_reasons,
            "should_switch": bool(decision.get("should_switch")),
        },
        sort_keys=True,
    )
    last_signature = str(state.get("last_optimum_state_signature") or "")
    last_event_at = int(state.get("last_optimum_state_event_epoch", 0) or 0)
    if signature == last_signature and now - last_event_at < DEFAULT_OPTIMUM_STATE_EVENT_COOLDOWN:
        return

    state["last_optimum_state_signature"] = signature
    state["last_optimum_state_event_epoch"] = now
    state["last_optimum_state_event_at"] = now_iso()
    state["last_optimum_state"] = {
        "current_primary": current_primary,
        "current_haproxy_primary": decision.get("current_haproxy_primary"),
        "pool_selected_backend": decision.get("pool_selected_backend"),
        "pool_selected_backend_node": decision.get("pool_selected_backend_node"),
        "routing_alignment": decision.get("routing_alignment"),
        "recommended_primary": decision.get("recommended_primary"),
        "current_score": current_score_value,
        "current_state": current_state,
        "router_reasons": router_reasons,
        "quality_reasons": quality_reasons,
        "should_switch": bool(decision.get("should_switch")),
        "score_delta": decision.get("score_delta"),
        "pool_pressure": pressure,
    }
    severity = "critical" if decision.get("should_switch") or pressure.get("hard_pool_pressure") else "warning"
    if decision.get("should_switch"):
        message = (
            f"optimum-state repair needed: switch {current_primary} -> "
            f"{decision.get('recommended_primary')} ({decision.get('reason')})"
        )
    else:
        message = (
            f"optimum-state watch: {current_primary} score={current_score_value:.1f} "
            f"state={current_state}; {decision.get('reason')}"
        )
    log(message)
    record_efficiency_event(
        "optimum_state_watch",
        severity,
        message,
        {
            "current_primary": current_primary,
            "current_haproxy_primary": decision.get("current_haproxy_primary"),
            "pool_selected_backend": decision.get("pool_selected_backend"),
            "pool_selected_backend_node": decision.get("pool_selected_backend_node"),
            "routing_alignment": decision.get("routing_alignment"),
            "recommended_primary": decision.get("recommended_primary"),
            "current_score": current_score_value,
            "current_state": current_state,
            "score_delta": decision.get("score_delta"),
            "router_reasons": router_reasons,
            "quality_reasons": quality_reasons,
            "pool_pressure": pressure,
            "status_overall": status.get("overall"),
        },
    )


def apply_watchdog_rpc_router_decision(
    status: dict[str, Any],
    state: dict[str, Any],
    decision: dict[str, Any] | None,
    now: int,
    snapshot_active: bool,
    autonomous_lab_active: bool,
    pool_in_startup_grace: bool,
    pool_started_age_seconds: int | None,
    repair: bool,
) -> tuple[bool, bool]:
    if not isinstance(decision, dict):
        return False, False
    if not RPC_FAILOVER_ENABLED:
        state["last_rpc_router_decision"] = {
            "generated_at": decision.get("generated_at"),
            "current_primary": decision.get("current_primary"),
            "current_haproxy_primary": decision.get("current_haproxy_primary"),
            "pool_selected_backend": decision.get("pool_selected_backend"),
            "pool_selected_backend_node": decision.get("pool_selected_backend_node"),
            "routing_alignment": decision.get("routing_alignment"),
            "recommended_primary": decision.get("recommended_primary"),
            "should_switch": False,
            "reason": "rpc failover disabled",
            "score_delta": decision.get("score_delta"),
            "scores": decision.get("scores"),
            "pool_pressure": decision.get("pool_pressure"),
        }
        return False, False

    state["last_rpc_router_decision"] = {
        "generated_at": decision.get("generated_at"),
        "current_primary": decision.get("current_primary"),
        "current_haproxy_primary": decision.get("current_haproxy_primary"),
        "pool_selected_backend": decision.get("pool_selected_backend"),
        "pool_selected_backend_node": decision.get("pool_selected_backend_node"),
        "routing_alignment": decision.get("routing_alignment"),
        "recommended_primary": decision.get("recommended_primary"),
        "should_switch": bool(decision.get("should_switch")),
        "reason": decision.get("reason"),
        "score_delta": decision.get("score_delta"),
        "scores": decision.get("scores"),
        "pool_pressure": decision.get("pool_pressure"),
    }
    observed_primary = str(decision.get("current_primary") or current_rpc_primary() or "")
    if observed_primary:
        state["last_rpc_primary"] = observed_primary
    if not decision.get("should_switch") or not repair:
        return False, False

    target = str(decision.get("recommended_primary") or "")
    current_primary = observed_primary
    if target not in NODES or target == current_primary:
        return False, False

    details = {
        "current_primary": current_primary,
        "current_haproxy_primary": decision.get("current_haproxy_primary"),
        "pool_selected_backend": decision.get("pool_selected_backend"),
        "pool_selected_backend_node": decision.get("pool_selected_backend_node"),
        "routing_alignment": decision.get("routing_alignment"),
        "recommended_primary": target,
        "reason": decision.get("reason"),
        "score_delta": decision.get("score_delta"),
        "scores": decision.get("scores"),
        "pool_pressure": decision.get("pool_pressure"),
        "status_overall": status.get("overall"),
    }
    if autonomous_lab_active:
        log(f"rpc router switch suppressed during autonomous stack lab current={current_primary} target={target}")
        record_efficiency_event(
            "repair_suppressed",
            "warning",
            "rpc router switch suppressed during autonomous stack lab",
            details,
        )
        state["last_rpc_router_suppressed_at"] = now_iso()
        state["last_rpc_router_suppressed_reason"] = "autonomous stack lab lock is held"
        return False, True

    if snapshot_active:
        log(f"rpc router switch suppressed during hourly snapshot current={current_primary} target={target}")
        record_efficiency_event(
            "repair_suppressed",
            "warning",
            "rpc router switch suppressed during hourly snapshot",
            details,
        )
        state["last_rpc_router_suppressed_at"] = now_iso()
        state["last_rpc_router_suppressed_reason"] = "hourly snapshot lock is held"
        return False, True

    current_score_row = (
        (decision.get("scores") or {}).get(current_primary)
        if isinstance(decision.get("scores"), dict)
        else {}
    )
    current_score_value = float((current_score_row or {}).get("score") or 0.0)
    router_reason = str(decision.get("reason") or "")
    hard_node_problem = bool(
        "current-primary-hard-problem" in router_reason
        or (current_score_row or {}).get("state") == "down"
        or current_score_value <= 30.0
    )
    if pool_in_startup_grace and not hard_node_problem:
        log(
            "rpc router switch suppressed during pool startup grace "
            f"age={pool_started_age_seconds}s current={current_primary} target={target} "
            f"reason={decision.get('reason')}"
        )
        record_efficiency_event(
            "repair_suppressed",
            "warning",
            "rpc router switch suppressed during pool startup grace",
            {
                **details,
                "pool_started_age_seconds": pool_started_age_seconds,
                "grace_seconds": DEFAULT_POOL_RESTART_GRACE_SECONDS,
            },
        )
        state["last_rpc_router_suppressed_at"] = now_iso()
        state["last_rpc_router_suppressed_reason"] = "pool startup grace"
        return False, True

    cooldown = rpc_router_switch_cooldown(decision)
    cooldown_remaining = cooldown - (now - int(state.get("last_rpc_primary_switch_at", 0) or 0))
    if cooldown_remaining > 0:
        log(
            f"rpc router switch suppressed by cooldown_remaining={cooldown_remaining}s "
            f"current={current_primary} target={target} reason={decision.get('reason')}"
        )
        record_efficiency_event(
            "repair_suppressed",
            "warning",
            "rpc router switch suppressed by cooldown",
            {**details, "cooldown_remaining_seconds": cooldown_remaining, "cooldown_seconds": cooldown},
        )
        state["last_rpc_router_suppressed_at"] = now_iso()
        state["last_rpc_router_suppressed_reason"] = "cooldown"
        return False, True

    reason = (
        f"watchdog optimum-state RPC correction current={current_primary} target={target}; "
        f"router={decision.get('reason')} score_delta={decision.get('score_delta')}"
    )
    ok = run_rpc_failover_switch(target, reason)
    if ok:
        switched_at = int(time.time())
        state["last_rpc_primary_switch_at"] = switched_at
        state["last_rpc_primary"] = target
        state["last_repair_at"] = switched_at
        state["last_rpc_router_applied_at"] = now_iso()
        record_efficiency_event(
            "rpc_router_switch",
            "warning",
            f"rpc primary switched to {target} for optimum runtime state",
            details,
        )
    else:
        state["last_rpc_router_failed_at"] = now_iso()
    return ok, False


def run_pool_restart(reason: str) -> bool:
    lock_handle = acquire_lock(blocking=False)
    if lock_handle is None:
        log(f"pool restart skipped because another repair is running; reason={reason}")
        return False

    started = time.time()
    action_name = f"restart-{POOL_CONTAINER}"
    log_path = action_log_path(action_name)
    state_payload = {
        "name": action_name,
        "mode": "restart-pool",
        "service": POOL_CONTAINER,
        "reason": reason,
        "status": "running",
        "started_at": now_iso(),
        "finished_at": None,
        "log_path": str(log_path),
    }
    write_action_state(state_payload)
    log(f"starting targeted pool restart: {reason}; log={log_path}")

    command = [
        "docker",
        "compose",
        "--env-file",
        str(POOL_ENV_FILE),
        "-f",
        str(PROJECT_ROOT / "docker-compose.yml"),
        "restart",
        POOL_CONTAINER,
    ]
    result = run_logged(command, log_path, timeout=180)
    ok = result.ok

    state_payload.update(
        {
            "status": "ok" if ok else "failed",
            "finished_at": now_iso(),
            "elapsed": round(time.time() - started, 3),
        }
    )
    write_action_state(state_payload)
    log(f"finished targeted pool restart status={state_payload['status']} elapsed={state_payload['elapsed']}s")
    if not ok:
        record_failed_repair("targeted pool restart", reason, {"service": POOL_CONTAINER, "log_path": str(log_path)})
    lock_handle.close()
    return ok


def run_miner_restarts(targets: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    password = read_miner_admin_password()

    lock_handle = acquire_lock(blocking=False)
    if lock_handle is None:
        log(f"miner restart skipped because another repair is running; reason={reason}")
        return {"status": "skipped", "reason": "another repair is running", "target_count": len(targets), "results": []}

    started = time.time()
    action_name = "restart-miners"
    log_path = action_log_path(action_name)
    state_payload = {
        "name": action_name,
        "mode": "restart-miners",
        "reason": reason,
        "targets": [item.get("ip") for item in targets],
        "status": "running",
        "started_at": now_iso(),
        "finished_at": None,
        "log_path": str(log_path),
    }
    write_action_state(state_payload)
    log(f"starting miner restarts targets={state_payload['targets']} reason={reason}; log={log_path}")

    results: list[dict[str, Any]] = []
    defaults = default_miner_pool_settings()
    restart_after_configure = "low-difficulty" in reason.lower()
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] miner restart reason: {reason}\n")
        for target in targets:
            ip = str(target.get("ip") or "")
            if not is_lan_ipv4(ip):
                result = {"ip": ip, "status": "skipped", "error": "not a LAN IPv4 address"}
            else:
                try:
                    if target.get("configured") is False and password:
                        result = configure_miner(
                            ip=ip,
                            admin_password=password,
                            pool_url=target.get("expected_pool_url") or defaults["pool_url"],
                            worker_user=target.get("expected_worker_user") or defaults["worker_user"],
                            pool_password=defaults["pool_password"],
                            replace_existing=True,
                        )
                        result["action"] = "configure"
                        if restart_after_configure:
                            try:
                                restart_result = restart_miner(ip, password)
                            except Exception as exc:  # noqa: BLE001 - try unauthenticated fallback before failing.
                                try:
                                    restart_result = {
                                        **restart_miner_open(ip),
                                        "fallback": "open",
                                        "auth_restart_error": str(exc),
                                    }
                                except Exception as fallback_exc:  # noqa: BLE001
                                    restart_result = {
                                        "ip": ip,
                                        "status": "failed",
                                        "error": str(fallback_exc),
                                        "auth_restart_error": str(exc),
                                    }
                            result["restart"] = restart_result
                            result["action"] = "configure-restart"
                            if restart_result.get("status") == "failed":
                                result["status"] = "partial"
                    elif target.get("configured") is False:
                        result = {
                            **restart_miner_open(ip),
                            "action": "restart-open-no-password",
                            "note": "configuration could not be repaired without a saved admin password",
                        }
                    else:
                        if password:
                            result = {**restart_miner(ip, password), "action": "restart"}
                        else:
                            result = {**restart_miner_open(ip), "action": "restart-open-no-password"}
                except Exception as exc:  # noqa: BLE001 - keep restarting other down miners.
                    if target.get("configured") is False:
                        try:
                            result = {
                                **restart_miner_open(ip),
                                "action": "restart-open-fallback",
                                "configure_error": str(exc),
                            }
                        except Exception as fallback_exc:  # noqa: BLE001 - keep restarting other down miners.
                            result = {
                                "ip": ip,
                                "status": "failed",
                                "action": "configure",
                                "error": str(exc),
                                "fallback_error": str(fallback_exc),
                            }
                    else:
                        result = {"ip": ip, "status": "failed", "action": "restart", "error": str(exc)}
            results.append(result)
            handle.write(json.dumps(result, default=str) + "\n")

    failed = [item for item in results if item.get("status") == "failed"]
    state_payload.update(
        {
            "status": "failed" if failed else "ok",
            "finished_at": now_iso(),
            "elapsed": round(time.time() - started, 3),
            "results": results,
        }
    )
    write_action_state(state_payload)
    log(
        "finished miner restarts "
        f"status={state_payload['status']} targets={len(targets)} failed={len(failed)} elapsed={state_payload['elapsed']}s"
    )
    if failed:
        record_failed_repair(
            "miner restart/configure",
            reason,
            {"target_count": len(targets), "failed": failed, "log_path": str(log_path)},
        )
    lock_handle.close()
    return {
        "status": state_payload["status"],
        "reason": reason,
        "target_count": len(targets),
        "results": results,
    }


def choose_lagging_node(status: dict[str, Any]) -> str | None:
    nodes = status.get("nodes", {}) or {}
    progress_nodes = (status.get("sync_progress", {}) or {}).get("nodes", {}) or {}
    sync_health = status.get("sync_health", {}) or {}
    import_stale_seconds = int(sync_health.get("import_stale_seconds") or 180)
    latest_values = [
        int(info.get("latest_block") or 0)
        for info in nodes.values()
        if int(info.get("latest_block") or 0) > 0
    ]
    max_latest = max(latest_values) if latest_values else 0
    candidates: list[tuple[int, str]] = []
    for node in NODES:
        node_info = nodes.get(node, {}) or {}
        progress = progress_nodes.get(node, {}) or {}
        lag = int(progress.get("remaining_blocks") or node_info.get("peer_ahead_blocks") or 0)
        latest = int(node_info.get("latest_block") or 0)
        if max_latest and latest:
            lag = max(lag, max_latest - latest)
        if progress.get("status") == "unknown" or progress.get("error"):
            lag = max(lag, 1_000_000)
        last_import_age = int(node_info.get("last_import_age_seconds") or 0)
        if last_import_age > import_stale_seconds:
            lag = max(lag, last_import_age)
        if progress.get("status") == "syncing" or lag > 0:
            candidates.append((lag, node))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def active_sync_import_nodes(
    status: dict[str, Any],
    state: dict[str, Any] | None = None,
    now: int | None = None,
    grace_seconds: int = DEFAULT_ACTIVE_SYNC_IMPORT_GRACE_SECONDS,
) -> list[str]:
    nodes = status.get("nodes", {}) if isinstance(status.get("nodes"), dict) else {}
    progress_nodes = (status.get("sync_progress", {}) or {}).get("nodes", {}) or {}
    height_changed_at = (
        state.get("last_sync_height_changed_at_by_node")
        if state is not None and isinstance(state.get("last_sync_height_changed_at_by_node"), dict)
        else {}
    )
    current_time = int(time.time()) if now is None else now
    active: list[str] = []
    for node in NODES:
        info = nodes.get(node, {}) if isinstance(nodes.get(node), dict) else {}
        if not info.get("child_running"):
            continue
        progress = progress_nodes.get(node, {}) if isinstance(progress_nodes.get(node), dict) else {}
        latest = max(int(info.get("latest_block") or 0), int(progress.get("current_block") or 0))
        if info.get("importing") and latest > 0:
            active.append(node)
            continue
        changed_at = int(height_changed_at.get(node) or 0)
        if latest > 0 and changed_at and current_time - changed_at <= grace_seconds:
            active.append(node)
            continue
        raw_age = info.get("last_import_age_seconds")
        if raw_age is None or latest <= 0:
            continue
        try:
            age = int(float(raw_age))
        except (TypeError, ValueError):
            continue
        if age <= grace_seconds:
            active.append(node)
    return active


def observe_sync_progress(status: dict[str, Any], state: dict[str, Any], now: int) -> None:
    nodes = status.get("nodes", {}) if isinstance(status.get("nodes"), dict) else {}
    progress_nodes = (status.get("sync_progress", {}) or {}).get("nodes", {}) or {}
    previous = state.get("last_sync_height_by_node") if isinstance(state.get("last_sync_height_by_node"), dict) else {}
    changed_at = (
        state.get("last_sync_height_changed_at_by_node")
        if isinstance(state.get("last_sync_height_changed_at_by_node"), dict)
        else {}
    )
    observed: dict[str, int] = {}
    updated_changed_at = dict(changed_at)
    for node in NODES:
        info = nodes.get(node, {}) if isinstance(nodes.get(node), dict) else {}
        progress = progress_nodes.get(node, {}) if isinstance(progress_nodes.get(node), dict) else {}
        height = max(int(info.get("latest_block") or 0), int(progress.get("current_block") or 0))
        observed[node] = height
        if height > int(previous.get(node) or 0):
            updated_changed_at[node] = now
    state["last_sync_height_by_node"] = observed
    state["last_sync_height_changed_at_by_node"] = updated_changed_at


def suppress_sync_restart_for_active_import(
    status: dict[str, Any],
    state: dict[str, Any],
    reason: str,
    target_node: str | None = None,
) -> bool:
    active_nodes = active_sync_import_nodes(status, state=state)
    if not active_nodes:
        return False
    if target_node and target_node not in active_nodes:
        return False

    pool_health = status.get("pool_health", status.get("pool", {}))
    sync_health = status.get("sync_health", {}) if isinstance(status.get("sync_health"), dict) else {}
    expected_sync_wait = bool(
        pool_health.get("initial_download")
        or sync_health.get("needs_fast_sync_repair")
        or "waiting for node sync" in reason.lower()
        or "initial download" in reason.lower()
    )
    if not expected_sync_wait:
        return False

    state["last_sync_repair_at"] = int(time.time())
    state["last_sync_repair_suppressed_at"] = now_iso()
    state["last_sync_repair_suppressed_reason"] = "active block import"
    details = {
        "active_nodes": active_nodes,
        "target_node": target_node,
        "reason": reason,
        "grace_seconds": DEFAULT_ACTIVE_SYNC_IMPORT_GRACE_SECONDS,
    }
    log(
        "sync restart suppressed while block import is active "
        f"target={target_node or 'stack'} active_nodes={','.join(active_nodes)} reason={reason}"
    )
    record_efficiency_event(
        "repair_suppressed",
        "warning",
        "sync restart suppressed while block import is active",
        details,
    )
    return True


def should_clean_restore(state: dict[str, Any], status: dict[str, Any], threshold: int, cooldown: int) -> bool:
    if not AUTOMATIC_CLEAN_RESTORE_ENABLED:
        return False
    if state.get("consecutive_failures", 0) < threshold:
        return False

    now = int(time.time())
    if now - int(state.get("last_clean_restore_at", 0) or 0) < cooldown:
        return False

    hard_failure = any("critical log entries" in item or "bdag child is not running" in item for item in status["failures"])
    return hard_failure


def should_restart_for_syncing(state: dict[str, Any], threshold: int, cooldown: int) -> bool:
    if int(state.get("consecutive_syncing", 0) or 0) < threshold:
        return False
    now = int(time.time())
    return now - int(state.get("last_sync_repair_at", 0) or 0) >= cooldown


def should_restart_for_share_stall(state: dict[str, Any], threshold: int, cooldown: int) -> bool:
    if int(state.get("consecutive_share_stalls", 0) or 0) < threshold:
        return False
    now = int(time.time())
    return now - int(state.get("last_share_repair_at", 0) or 0) >= cooldown


def boot_repair_mode(policy: str, failures: list[Any] | None = None) -> str:
    if policy == "clean" and AUTOMATIC_CLEAN_RESTORE_ENABLED:
        return "clean"
    if policy == "clean":
        log("boot-repair clean restore policy ignored because BDAG_ENABLE_AUTOMATIC_CLEAN_RESTORE is not enabled")
        return "restart"
    if policy in {"start", "restart", "check"}:
        return policy
    text = "; ".join(str(item) for item in failures or [])
    return "restart" if "bdag child is not running" in text else "start"


def run_boot_repair_mode(
    mode: str,
    reason: str,
    threshold: int,
    clean_restore_cooldown: int,
    syncing_threshold: int,
    syncing_restart_cooldown: int,
) -> tuple[bool, dict[str, Any] | None]:
    if mode == "check":
        result = check_once(
            threshold,
            clean_restore_cooldown,
            syncing_threshold,
            syncing_restart_cooldown,
            repair=True,
        )
        return True, result
    return run_repair(mode, reason), None


def boot_repair(
    threshold: int,
    clean_restore_cooldown: int,
    syncing_threshold: int,
    syncing_restart_cooldown: int,
) -> dict[str, Any]:
    marker = read_dirty_shutdown_marker()
    state = read_state()
    if marker is not None:
        reason = str(marker.get("reason") or "dirty shutdown marker detected")
        log(f"boot-repair found dirty shutdown marker: {reason}")
        try:
            collect_status_cached(include_logs=True)
        except Exception as exc:  # noqa: BLE001 - boot repair should still attempt the conservative repair.
            log(f"boot-repair preflight status check failed: {exc}")

        mode = boot_repair_mode(BOOT_REPAIR_DIRTY_POLICY)
        ok, result = run_boot_repair_mode(
            mode,
            f"boot repair after dirty shutdown marker: {reason}",
            threshold,
            clean_restore_cooldown,
            syncing_threshold,
            syncing_restart_cooldown,
        )
        state = (result or {}).get("watchdog_state") if isinstance(result, dict) else read_state()
        if not isinstance(state, dict):
            state = read_state()
        state["boot_repair_at"] = now_iso()
        state["boot_repair_status"] = f"dirty_{mode}_{'repaired' if ok else 'failed'}"
        state["boot_repair_dirty_policy"] = BOOT_REPAIR_DIRTY_POLICY
        state["automatic_clean_restore_enabled"] = AUTOMATIC_CLEAN_RESTORE_ENABLED
        state["last_status"] = "boot_repaired" if ok else "boot_repair_failed"
        state["consecutive_failures"] = 0 if ok else int(state.get("consecutive_failures", 0) or 0)
        state["consecutive_syncing"] = 0 if ok else int(state.get("consecutive_syncing", 0) or 0)
        state["consecutive_share_stalls"] = 0 if ok else int(state.get("consecutive_share_stalls", 0) or 0)
        state["updated_at"] = now_iso()
        if ok:
            clear_dirty_shutdown_marker()
        write_state(state)
        payload = {
            "boot_repair": state["boot_repair_status"],
            "repair_mode": mode,
            "dirty_shutdown_marker": marker,
            "watchdog_state": state,
        }
        if result is not None:
            payload["repair_result"] = result
        return payload

    try:
        boot_status = collect_status_cached(include_logs=True)
    except Exception as exc:  # noqa: BLE001 - boot repair should degrade gracefully on a bad status probe.
        log(f"boot-repair status check failed: {exc}")
        boot_status = {"stack_failures": [str(exc)], "failures": [str(exc)]}
    boot_stack_failures = boot_status.get("stack_failures", boot_status.get("failures", []))
    if any("critical log entries" in item or "bdag child is not running" in item for item in boot_stack_failures):
        reason = "; ".join(boot_stack_failures) or "critical boot-time stack failure"
        mode = boot_repair_mode(BOOT_REPAIR_CRITICAL_POLICY, boot_stack_failures)
        log(f"boot-repair handling critical boot-time stack failure with {mode}: {reason}")
        ok, result = run_boot_repair_mode(
            mode,
            f"boot repair after critical stack failure: {reason}",
            threshold,
            clean_restore_cooldown,
            syncing_threshold,
            syncing_restart_cooldown,
        )
        state = (result or {}).get("watchdog_state") if isinstance(result, dict) else read_state()
        if not isinstance(state, dict):
            state = read_state()
        state["boot_repair_at"] = now_iso()
        state["boot_repair_status"] = f"critical_{mode}_{'repaired' if ok else 'failed'}"
        state["boot_repair_critical_policy"] = BOOT_REPAIR_CRITICAL_POLICY
        state["automatic_clean_restore_enabled"] = AUTOMATIC_CLEAN_RESTORE_ENABLED
        state["last_status"] = "boot_repaired" if ok else "boot_repair_failed"
        state["updated_at"] = now_iso()
        if ok:
            clear_dirty_shutdown_marker()
        write_state(state)
        payload = {
            "boot_repair": state["boot_repair_status"],
            "repair_mode": mode,
            "boot_status": boot_status,
            "watchdog_state": state,
        }
        if result is not None:
            payload["repair_result"] = result
        return payload

    try:
        result = check_once(
            threshold,
            clean_restore_cooldown,
            syncing_threshold,
            syncing_restart_cooldown,
            repair=True,
        )
    except Exception as exc:  # noqa: BLE001 - keep boot repair from crashing on a bad probe.
        log(f"boot-repair check failed: {exc}")
        state["boot_repair_at"] = now_iso()
        state["boot_repair_status"] = "failed"
        state["last_status"] = "boot_repair_failed"
        state["updated_at"] = now_iso()
        write_state(state)
        return {
            "boot_repair": "failed",
            "error": str(exc),
            "watchdog_state": state,
        }
    state = result["watchdog_state"]
    state["boot_repair_at"] = now_iso()
    state["boot_repair_status"] = "checked"
    state["updated_at"] = now_iso()
    write_state(state)
    result["watchdog_state"] = state
    result["boot_repair"] = "checked"
    return result


def check_once(
    threshold: int,
    clean_restore_cooldown: int,
    syncing_threshold: int,
    syncing_restart_cooldown: int,
    miner_down_restart_seconds: int = DEFAULT_MINER_DOWN_RESTART_SECONDS,
    miner_restart_cooldown: int = DEFAULT_MINER_RESTART_COOLDOWN,
    repair: bool = True,
) -> dict[str, Any]:
    state = read_state()
    status = collect_status_cached(include_logs=True)
    router_decision = None
    try:
        router_decision = write_rpc_router_state(status)
    except Exception as exc:  # noqa: BLE001 - router state must not block repair.
        log(f"rpc router state update failed: {exc}")
    stack_failures = status.get("stack_failures", status["failures"])
    miner_failures = status.get("miner_failures", [])
    failures = stack_failures + miner_failures
    pool_health = status.get("pool_health", status.get("pool", {}))
    miner_health = status.get("miner_health", {})
    miner_rows = miner_health.get("miners", []) if isinstance(miner_health.get("miners"), list) else []
    mining_address = str(status.get("mining_address") or "")
    down_miners = [
        item
        for item in miner_rows
        if (item.get("managed") or item.get("configured"))
        and item.get("device_type") in {"asic", "stratum"}
        and item.get("status") == "down"
        and is_lan_ipv4(str(item.get("ip", "")))
    ]
    down_ips = {str(item.get("ip")) for item in down_miners}
    miner_down_since = state.get("miner_down_since") if isinstance(state.get("miner_down_since"), dict) else {}
    miner_restart_by_ip = (
        state.get("last_miner_restart_at_by_ip") if isinstance(state.get("last_miner_restart_at_by_ip"), dict) else {}
    )
    now = int(time.time())
    observe_sync_progress(status, state, now)
    pool_started_age_seconds = container_started_age_seconds(status, POOL_CONTAINER, now)
    pool_in_startup_grace = bool(
        pool_started_age_seconds is not None
        and pool_started_age_seconds < DEFAULT_POOL_RESTART_GRACE_SECONDS
    )
    for ip in list(miner_down_since):
        if ip not in down_ips:
            miner_down_since.pop(ip, None)
    for ip in sorted(down_ips):
        miner_down_since.setdefault(ip, now)
    state["miner_down_since"] = miner_down_since
    state["last_miner_restart_at_by_ip"] = miner_restart_by_ip
    share_stall = bool(pool_health.get("share_stall")) and int(miner_health.get("connected_count", 0) or 0) > 0
    pool_template_frozen = bool(pool_health.get("pool_template_frozen")) and int(miner_health.get("connected_count", 0) or 0) > 0
    duplicate_block_storm = bool(pool_health.get("duplicate_block_storm")) and int(miner_health.get("connected_count", 0) or 0) > 0
    submit_path_zero_success_storm = (
        bool(pool_health.get("block_submit_zero_success_storm"))
        and int(miner_health.get("connected_count", 0) or 0) > 0
    )
    accepted_job_expired_storm = (
        bool(pool_health.get("accepted_job_expired_storm"))
        and int(miner_health.get("connected_count", 0) or 0) > 0
    )
    submit_path_recovery_recent = bool(pool_health.get("submit_stall_recovery_recent"))
    submit_path_self_healed_recently = bool(pool_health.get("submit_stall_self_healed_recently"))
    submit_path_recovery_age = pool_health.get("submit_stall_last_recovery_age_seconds")
    low_diff_asics = low_difficulty_primary_miners(status)
    useful_work_stalled_asics = useful_work_stalled_primary_miners(status)
    hashrate_issue_asics = asic_hashrate_issue_primary_miners(
        status,
        DEFAULT_ASIC_HASHRATE_MIN_GHS,
        DEFAULT_ASIC_HASHRATE_STALE_SECONDS,
    )
    degraded_asics = degraded_primary_miners(status, DEFAULT_ASIC_DEGRADED_SECONDS)
    primary_miner_count = sum(
        1
        for item in miner_rows
        if isinstance(item, dict) and is_primary_pool_miner(item, mining_address)
    )
    template_nodes = template_failing_nodes(status)
    orphan_nodes = orphan_storm_nodes(status)
    node_template_restart_by_node = (
        state.get("last_node_template_restart_at_by_node")
        if isinstance(state.get("last_node_template_restart_at_by_node"), dict)
        else {}
    )
    node_orphan_restart_by_node = (
        state.get("last_node_orphan_restart_at_by_node")
        if isinstance(state.get("last_node_orphan_restart_at_by_node"), dict)
        else {}
    )
    rpc_primary_switch_at = int(state.get("last_rpc_primary_switch_at", 0) or 0)
    docker_access_error = status.get("docker_access_error")
    snapshot_active = lock_is_held(HOURLY_SNAPSHOT_LOCK_FILE)
    autonomous_lab_active = lock_is_held(AUTONOMOUS_STACK_LAB_LOCK_FILE)
    refresh_maintenance_state(state, snapshot_active, autonomous_lab_active)
    useful_work_stall_since = (
        state.get("miner_useful_work_stall_since")
        if isinstance(state.get("miner_useful_work_stall_since"), dict)
        else {}
    )
    useful_work_stall_ips = {str(item.get("ip")) for item in useful_work_stalled_asics if item.get("ip")}
    for ip in list(useful_work_stall_since):
        if ip not in useful_work_stall_ips:
            useful_work_stall_since.pop(ip, None)
    for ip in sorted(useful_work_stall_ips):
        useful_work_stall_since.setdefault(ip, now)
    state["miner_useful_work_stall_since"] = useful_work_stall_since
    asic_hashrate_issue_since = (
        state.get("asic_hashrate_issue_since")
        if isinstance(state.get("asic_hashrate_issue_since"), dict)
        else {}
    )
    asic_hashrate_issue_ips = {str(item.get("ip")) for item in hashrate_issue_asics if item.get("ip")}
    for ip in list(asic_hashrate_issue_since):
        if ip not in asic_hashrate_issue_ips:
            asic_hashrate_issue_since.pop(ip, None)
    for ip in sorted(asic_hashrate_issue_ips):
        asic_hashrate_issue_since.setdefault(ip, now)
    state["asic_hashrate_issue_since"] = asic_hashrate_issue_since
    if not docker_access_error:
        router_switched, router_suppressed = apply_watchdog_rpc_router_decision(
            status,
            state,
            router_decision,
            now,
            snapshot_active,
            autonomous_lab_active,
            pool_in_startup_grace,
            pool_started_age_seconds,
            repair,
        )
        if router_switched:
            rpc_primary_switch_at = int(state.get("last_rpc_primary_switch_at", 0) or 0)
        elif not router_suppressed:
            record_optimum_state_observation(status, state, router_decision, now)

    last_earnings_snapshot_epoch = int(state.get("last_earnings_snapshot_epoch", 0) or 0)
    if now - last_earnings_snapshot_epoch >= DEFAULT_EARNINGS_SNAPSHOT_INTERVAL_SECONDS:
        try:
            snapshot = record_earnings_snapshot()
            state["last_earnings_snapshot_at"] = snapshot.get("generated_at")
            state["last_earnings_snapshot_epoch"] = now
        except Exception as exc:  # noqa: BLE001 - earnings logging should not stop repairs.
            log(f"earnings snapshot failed: {exc}")

    if docker_access_error:
        failure = f"docker access unavailable: {docker_access_error}"
        state["consecutive_failures"] = 1
        state["consecutive_syncing"] = 0
        state["consecutive_share_stalls"] = 0
        state["last_status"] = "docker_unavailable"
        state["last_failures"] = [failure]
        state["last_sync_warnings"] = []
        state["last_share_warnings"] = []
        log(failure)
        record_efficiency_event("docker_unavailable", "critical", failure)
        state["updated_at"] = now_iso()
        write_state(state)
        return {"status": status, "watchdog_state": state}

    if stack_failures and snapshot_active:
        state["consecutive_failures"] = 0
        state["consecutive_syncing"] = 0
        state["consecutive_share_stalls"] = 0
        state["last_status"] = "maintenance"
        state["last_failures"] = []
        state["last_sync_warnings"] = []
        state["last_share_warnings"] = []
        state["maintenance"] = {
            "active": True,
            "reason": "hourly snapshot lock is held",
            "stack_failures_suppressed": stack_failures,
            "updated_at": now_iso(),
        }
        log("stack repair suppressed during hourly snapshot: " + "; ".join(stack_failures))
    elif stack_failures:
        state["consecutive_failures"] = int(state.get("consecutive_failures", 0) or 0) + 1
        state["consecutive_syncing"] = 0
        state["last_status"] = "down"
        state["last_failures"] = stack_failures
        log(f"stack=down consecutive={state['consecutive_failures']} failures={'; '.join(stack_failures)}")
        record_efficiency_event(
            "stack_down",
            "critical",
            "; ".join(stack_failures),
            {"consecutive_failures": state["consecutive_failures"]},
        )
        if repair:
            if should_clean_restore(state, status, threshold, clean_restore_cooldown):
                ok = run_repair("clean", "; ".join(stack_failures))
                state["last_repair_at"] = int(time.time())
                if ok:
                    state["last_clean_restore_at"] = int(time.time())
                    state["consecutive_failures"] = 0
            else:
                mode = "restart" if any("bdag child is not running" in item for item in stack_failures) else "start"
                ok = run_repair(mode, "; ".join(stack_failures))
                state["last_repair_at"] = int(time.time())
                if ok:
                    state["consecutive_failures"] = 0
    elif active_rpc_template_failing(status) and int(miner_health.get("connected_count", 0) or 0) > 0:
        current_primary = current_rpc_primary()
        active_probe = ((status.get("rpc_template_health") or {}).get("rpc_failover") or {})
        recent_mining_work = pool_has_recent_mining_work(status)
        reason = (
            "active RPC template path is refusing getBlockTemplate "
            f"({active_probe.get('error_count')}/{active_probe.get('sample_count')} failed)"
        )
        if active_probe.get("last_error"):
            reason += f": {active_probe.get('last_error')}"
        state["consecutive_failures"] = 0
        if recent_mining_work:
            state["consecutive_syncing"] = 0
        else:
            state["consecutive_syncing"] = int(state.get("consecutive_syncing", 0) or 0) + 1
        state["consecutive_share_stalls"] = 0
        state["last_status"] = "rpc_template_degraded"
        state["last_failures"] = []
        state["last_sync_warnings"] = [reason]
        log(f"rpc_template_degraded consecutive={state['consecutive_syncing']} primary={current_primary} reason={reason}")
        record_efficiency_event(
            "rpc_template_degraded",
            "critical",
            reason,
            {
                "current_primary": current_primary,
                "rpc_template_health": status.get("rpc_template_health"),
                "connected_miners": miner_health.get("connected_count"),
                "recent_mining_work": recent_mining_work,
            },
        )
        if repair:
            ok = False
            active_failing_nodes = rpc_probe_failing_nodes(status) or template_nodes
            alternate_primary = (
                healthy_rpc_alternate(status, active_failing_nodes, current_primary)
                if current_primary in active_failing_nodes
                else None
            )
            cooldown_remaining = DEFAULT_RPC_FAILOVER_URGENT_SWITCH_COOLDOWN - (now - rpc_primary_switch_at)
            if alternate_primary and cooldown_remaining <= 0:
                ok = run_rpc_failover_switch(
                    alternate_primary,
                    "active RPC template probe failure: " + reason,
                )
                if ok:
                    state["last_rpc_primary_switch_at"] = int(time.time())
                    state["last_rpc_primary"] = alternate_primary
            elif alternate_primary:
                log(
                    f"rpc primary switch suppressed by urgent cooldown_remaining={cooldown_remaining}s "
                    f"current_primary={current_primary} alternate={alternate_primary}"
                )
                record_efficiency_event(
                    "repair_suppressed",
                    "warning",
                    "rpc primary switch suppressed by urgent cooldown",
                    {
                        "cooldown_remaining_seconds": cooldown_remaining,
                        "current_primary": current_primary,
                        "alternate_primary": alternate_primary,
                        "reason": reason,
                    },
                )

            repair_node = choose_active_rpc_repair_node(status, current_primary)
            if not ok and repair_node:
                node_cooldown_remaining = DEFAULT_NODE_TEMPLATE_RESTART_COOLDOWN - (
                    now - int(node_template_restart_by_node.get(repair_node, 0) or 0)
                )
                if recent_mining_work:
                    pool_health = status.get("pool_health") if isinstance(status.get("pool_health"), dict) else {}
                    log(
                        f"node template restart for {repair_node} suppressed because pool mining work is fresh "
                        f"last_share_age={pool_health.get('last_valid_share_age_seconds')}s "
                        f"last_block_submit_age={pool_health.get('last_block_submit_age_seconds')}s"
                    )
                    record_efficiency_event(
                        "repair_suppressed",
                        "warning",
                        f"node template restart for {repair_node} suppressed because pool mining work is fresh",
                        {
                            "reason": reason,
                            "last_valid_share_age_seconds": pool_health.get("last_valid_share_age_seconds"),
                            "last_block_submit_age_seconds": pool_health.get("last_block_submit_age_seconds"),
                        },
                    )
                elif state["consecutive_syncing"] >= 2 and node_cooldown_remaining <= 0:
                    ok = run_node_restart(repair_node, "active RPC template probe failure: " + reason)
                    node_template_restart_by_node[repair_node] = int(time.time())
                    state["last_node_template_restart_at_by_node"] = node_template_restart_by_node
                elif node_cooldown_remaining > 0:
                    log(
                        f"node template restart for {repair_node} suppressed by cooldown_remaining="
                        f"{node_cooldown_remaining}s"
                    )
                    record_efficiency_event(
                        "repair_suppressed",
                        "warning",
                        f"node template restart for {repair_node} suppressed by cooldown",
                        {"cooldown_remaining_seconds": node_cooldown_remaining, "reason": reason},
                    )
            if ok:
                state["last_repair_at"] = int(time.time())
                state["last_sync_repair_at"] = int(time.time())
                state["consecutive_syncing"] = 0
    elif orphan_nodes:
        nodes = status.get("nodes", {}) if isinstance(status.get("nodes"), dict) else {}
        current_primary = current_rpc_primary()
        target_nodes = [node for node in orphan_nodes if node != current_primary] or orphan_nodes
        target_node = target_nodes[0]
        target_info = nodes.get(target_node, {}) if isinstance(nodes.get(target_node), dict) else {}
        reason = (
            f"{target_node} is logging repeated already-have-block orphan sync errors "
            f"({target_info.get('orphan_block_errors')} recent errors, no recent imports)"
        )
        state["consecutive_failures"] = 0
        state["consecutive_syncing"] = int(state.get("consecutive_node_orphan_storm", 0) or 0) + 1
        state["consecutive_node_orphan_storm"] = state["consecutive_syncing"]
        state["consecutive_share_stalls"] = 0
        state["last_status"] = "node_orphan_error_storm"
        state["last_failures"] = []
        state["last_sync_warnings"] = [reason]
        log(
            "node_orphan_error_storm "
            f"consecutive={state['consecutive_node_orphan_storm']} "
            f"current_primary={current_primary or 'unknown'} affected={orphan_nodes} target={target_node}"
        )
        record_efficiency_event(
            "node_orphan_error_storm",
            "warning",
            reason,
            {
                "affected_nodes": orphan_nodes,
                "target_node": target_node,
                "current_primary": current_primary,
                "target_node_status": target_info,
            },
        )
        if repair:
            cooldown_remaining = DEFAULT_NODE_ORPHAN_STORM_RESTART_COOLDOWN - (
                now - int(node_orphan_restart_by_node.get(target_node, 0) or 0)
            )
            if snapshot_active:
                log(f"node orphan storm repair for {target_node} suppressed during hourly snapshot")
                record_efficiency_event(
                    "repair_suppressed",
                    "warning",
                    f"node orphan storm repair for {target_node} suppressed during hourly snapshot",
                    {"reason": reason, "target_node": target_node},
                )
            elif autonomous_lab_active:
                log(f"node orphan storm repair for {target_node} suppressed during autonomous stack lab")
                record_efficiency_event(
                    "repair_suppressed",
                    "warning",
                    f"node orphan storm repair for {target_node} suppressed during autonomous stack lab",
                    {"reason": reason, "target_node": target_node},
                )
            elif pool_in_startup_grace and target_node == current_primary:
                log(
                    "node orphan storm repair suppressed during pool startup grace for active primary "
                    f"node={target_node} age={pool_started_age_seconds}s"
                )
                record_efficiency_event(
                    "repair_suppressed",
                    "warning",
                    "node orphan storm repair suppressed during pool startup grace for active primary",
                    {
                        "reason": reason,
                        "target_node": target_node,
                        "pool_started_age_seconds": pool_started_age_seconds,
                    },
                )
            elif int(state["consecutive_node_orphan_storm"]) < 2:
                log(f"node orphan storm repair for {target_node} waiting for confirmation")
            elif cooldown_remaining > 0:
                log(
                    f"node orphan storm restart for {target_node} suppressed by "
                    f"cooldown_remaining={cooldown_remaining}s"
                )
                record_efficiency_event(
                    "repair_suppressed",
                    "warning",
                    f"node orphan storm restart for {target_node} suppressed by cooldown",
                    {"cooldown_remaining_seconds": cooldown_remaining, "reason": reason},
                )
            else:
                ok = run_node_restart(target_node, "node orphan sync error storm: " + reason)
                node_orphan_restart_by_node[target_node] = int(time.time())
                state["last_node_orphan_restart_at_by_node"] = node_orphan_restart_by_node
                state["last_repair_at"] = int(time.time())
                state["last_sync_repair_at"] = int(time.time())
                if ok:
                    state["consecutive_syncing"] = 0
                    state["consecutive_node_orphan_storm"] = 0
    elif useful_work_stalled_asics:
        affected = [
            {
                "ip": item.get("ip"),
                "name": item.get("display_name"),
                "status": item.get("status"),
                "configured": item.get("configured"),
                "pool_active": item.get("pool_active"),
                "submits": item.get("submits"),
                "shares": item.get("shares"),
                "blocks_found": item.get("blocks_found"),
                "last_share_age_seconds": item.get("last_share_age_seconds"),
                "last_submit_age_seconds": item.get("last_submit_age_seconds"),
                "last_pool_seen_age_seconds": item.get("last_pool_seen_age_seconds"),
                "last_difficulty": item.get("last_difficulty"),
                "healthy_peer_count": item.get("healthy_peer_count"),
                "pool_valid_share_count": item.get("pool_valid_share_count"),
            }
            for item in useful_work_stalled_asics
        ]
        eligible_miners = []
        waiting = []
        for item in useful_work_stalled_asics:
            ip = str(item.get("ip"))
            stalled_for = now - int(useful_work_stall_since.get(ip, now) or now)
            cooldown_remaining = DEFAULT_MINER_USEFUL_WORK_STALL_REPAIR_COOLDOWN - (
                now - int(miner_restart_by_ip.get(ip, 0) or 0)
            )
            if stalled_for >= DEFAULT_MINER_USEFUL_WORK_STALL_CONFIRM_SECONDS and cooldown_remaining <= 0:
                eligible_miners.append(item)
            else:
                waiting.append(
                    f"{ip} stalled_for={stalled_for}s "
                    f"confirm={DEFAULT_MINER_USEFUL_WORK_STALL_CONFIRM_SECONDS}s "
                    f"cooldown_remaining={max(cooldown_remaining, 0)}s"
                )
        reason = (
            f"{len(useful_work_stalled_asics)} primary ASIC miner(s) are connected/API-visible "
            "but have stopped producing useful accepted work while peer miners are healthy"
        )
        state["consecutive_failures"] = 0
        state["consecutive_syncing"] = 0
        state["consecutive_node_orphan_storm"] = 0
        state["consecutive_miner_useful_work_stalls"] = int(
            state.get("consecutive_miner_useful_work_stalls", 0) or 0
        ) + 1
        state["last_status"] = "miner_useful_work_stall"
        state["last_failures"] = []
        state["last_share_warnings"] = [reason]
        state["last_useful_work_stalled_asics"] = affected
        log(
            "miner_useful_work_stall "
            f"affected={affected} eligible={[item.get('ip') for item in eligible_miners]} "
            f"waiting={'; '.join(waiting) or 'none'}"
        )
        record_efficiency_event(
            "miner_useful_work_stall",
            "warning",
            reason,
            {
                "affected_miners": affected,
                "eligible": [item.get("ip") for item in eligible_miners],
                "waiting": waiting,
                "primary_miner_count": primary_miner_count,
                "pool_valid_share_count": pool_health.get("valid_share_count"),
                "pool_submit_count": pool_health.get("submit_count"),
            },
        )
        if repair and eligible_miners:
            repair_targets = eligible_miners[:1]
            result = run_miner_restarts(repair_targets, "miner useful-work stall: " + reason)
            state["last_miner_repair_at"] = now
            state["last_miner_repair"] = result
            for item in repair_targets:
                miner_restart_by_ip[str(item.get("ip"))] = now
                useful_work_stall_since.pop(str(item.get("ip")), None)
            state["last_miner_restart_at_by_ip"] = miner_restart_by_ip
            state["miner_useful_work_stall_since"] = useful_work_stall_since
    elif hashrate_issue_asics:
        affected = [
            {
                "ip": item.get("ip"),
                "name": item.get("display_name"),
                "status": item.get("status"),
                "configured": item.get("configured"),
                "pool_active": item.get("pool_active"),
                "hashrate_ghs": item.get("hashrate_ghs"),
                "hashrate_min_ghs": item.get("hashrate_min_ghs"),
                "telemetry_blind": item.get("telemetry_blind"),
                "last_share_age_seconds": item.get("last_share_age_seconds"),
                "last_submit_age_seconds": item.get("last_submit_age_seconds"),
                "debug_error": item.get("debug_error"),
            }
            for item in hashrate_issue_asics
        ]
        eligible_miners = []
        waiting = []
        for item in hashrate_issue_asics:
            ip = str(item.get("ip"))
            issue_for = now - int(asic_hashrate_issue_since.get(ip, now) or now)
            cooldown_remaining = DEFAULT_ASIC_HASHRATE_REPAIR_COOLDOWN - (
                now - int(miner_restart_by_ip.get(ip, 0) or 0)
            )
            if issue_for >= DEFAULT_ASIC_HASHRATE_CONFIRM_SECONDS and cooldown_remaining <= 0:
                eligible_miners.append(item)
            else:
                waiting.append(
                    f"{ip} issue_for={issue_for}s "
                    f"confirm={DEFAULT_ASIC_HASHRATE_CONFIRM_SECONDS}s "
                    f"cooldown_remaining={max(cooldown_remaining, 0)}s"
                )
        reason = (
            f"{len(hashrate_issue_asics)} primary ASIC miner(s) have sustained low or unprovable hashrate "
            f"(threshold={DEFAULT_ASIC_HASHRATE_MIN_GHS:g} GH/s, stale={DEFAULT_ASIC_HASHRATE_STALE_SECONDS}s)"
        )
        state["consecutive_failures"] = 0
        state["consecutive_syncing"] = 0
        state["consecutive_node_orphan_storm"] = 0
        state["consecutive_share_stalls"] = 0
        state["last_status"] = "asic_hashrate_issue"
        state["last_failures"] = []
        state["last_share_warnings"] = [reason]
        state["last_asic_hashrate_issue"] = affected
        log(
            "asic_hashrate_issue "
            f"affected={affected} eligible={[item.get('ip') for item in eligible_miners]} "
            f"waiting={'; '.join(waiting) or 'none'}"
        )
        record_efficiency_event(
            "asic_hashrate_issue",
            "warning",
            reason,
            {"affected_miners": affected, "eligible": [item.get("ip") for item in eligible_miners], "waiting": waiting},
        )
        if repair and eligible_miners:
            repair_targets = eligible_miners[:1]
            result = run_miner_restarts(repair_targets, "ASIC hashrate watchdog: " + reason)
            state["last_miner_repair_at"] = now
            state["last_miner_repair"] = result
            for item in repair_targets:
                ip = str(item.get("ip"))
                miner_restart_by_ip[ip] = now
                asic_hashrate_issue_since.pop(ip, None)
            state["last_miner_restart_at_by_ip"] = miner_restart_by_ip
            state["asic_hashrate_issue_since"] = asic_hashrate_issue_since
    elif miner_failures:
        state["consecutive_failures"] = 0
        state["consecutive_syncing"] = 0
        state["consecutive_share_stalls"] = 0
        state["consecutive_miner_useful_work_stalls"] = 0
        state["last_status"] = "miner_down"
        state["last_failures"] = miner_failures
        eligible_miners = []
        waiting = []
        for item in down_miners:
            ip = str(item.get("ip"))
            down_for = now - int(miner_down_since.get(ip, now) or now)
            cooldown_remaining = miner_restart_cooldown - (now - int(miner_restart_by_ip.get(ip, 0) or 0))
            if down_for >= miner_down_restart_seconds and cooldown_remaining <= 0:
                eligible_miners.append(item)
            else:
                waiting.append(
                    f"{ip} down_for={down_for}s "
                    f"threshold={miner_down_restart_seconds}s cooldown_remaining={max(cooldown_remaining, 0)}s"
                )
        log(
            "miner=down "
            f"failures={'; '.join(miner_failures)} "
            f"eligible={[item.get('ip') for item in eligible_miners]} waiting={'; '.join(waiting) or 'none'}"
        )
        record_efficiency_event(
            "miner_down",
            "warning",
            "; ".join(miner_failures),
            {
                "eligible": [item.get("ip") for item in eligible_miners],
                "waiting": waiting,
            },
        )
        if repair and eligible_miners:
            repair_targets = eligible_miners[:1]
            result = run_miner_restarts(repair_targets, "; ".join(miner_failures))
            state["last_miner_repair_at"] = now
            state["last_miner_repair"] = result
            for item in repair_targets:
                miner_restart_by_ip[str(item.get("ip"))] = now
            state["last_miner_restart_at_by_ip"] = miner_restart_by_ip
    elif low_diff_asics:
        affected = [
            {
                "ip": item.get("ip"),
                "name": item.get("display_name"),
                "last_difficulty": item.get("last_difficulty"),
                "submits": item.get("submits"),
                "shares": item.get("shares"),
            }
            for item in low_diff_asics
        ]
        eligible_miners = []
        waiting = []
        for item in low_diff_asics:
            ip = str(item.get("ip"))
            cooldown_remaining = miner_restart_cooldown - (now - int(miner_restart_by_ip.get(ip, 0) or 0))
            if cooldown_remaining <= 0:
                eligible_miners.append(item)
            else:
                waiting.append(f"{ip} cooldown_remaining={max(cooldown_remaining, 0)}s")
        reason = f"{len(low_diff_asics)} primary ASIC miner(s) are submitting low-difficulty work"
        state["consecutive_failures"] = 0
        state["consecutive_syncing"] = 0
        state["consecutive_node_orphan_storm"] = 0
        state["consecutive_share_stalls"] = 0
        state["last_status"] = "asic_low_difficulty"
        state["last_failures"] = []
        state["last_share_warnings"] = [reason]
        state["last_low_difficulty_asics"] = affected
        log(
            "asic_low_difficulty "
            f"affected={affected} eligible={[item.get('ip') for item in eligible_miners]} "
            f"waiting={'; '.join(waiting) or 'none'}"
        )
        record_efficiency_event(
            "asic_low_difficulty",
            "warning",
            reason,
            {"affected_miners": affected, "eligible": [item.get("ip") for item in eligible_miners], "waiting": waiting},
        )
        if repair and eligible_miners:
            repair_targets = eligible_miners[:1]
            result = run_miner_restarts(repair_targets, reason)
            state["last_miner_repair_at"] = now
            state["last_miner_repair"] = result
            for item in repair_targets:
                miner_restart_by_ip[str(item.get("ip"))] = now
            state["last_miner_restart_at_by_ip"] = miner_restart_by_ip
    elif submit_path_self_healed_recently:
        recovery = (
            pool_health.get("submit_stall_last_recovery")
            if isinstance(pool_health.get("submit_stall_last_recovery"), dict)
            else {}
        )
        accepted_age = pool_health.get("last_block_submit_age_seconds")
        reason = (
            "pool submit path self-healed before watchdog restart was needed "
            f"(backend={recovery.get('backend_to') or pool_health.get('selected_backend') or 'unknown'}, "
            f"reason={recovery.get('reason') or pool_health.get('submit_stall_last_reason') or 'unknown'}, "
            f"accepted_age={accepted_age}s)"
        )
        state["consecutive_failures"] = 0
        state["consecutive_syncing"] = 0
        state["consecutive_node_orphan_storm"] = 0
        state["consecutive_share_stalls"] = 0
        state["consecutive_submit_path_stalls"] = 0
        state["last_status"] = "pool_submit_path_self_healed"
        state["last_failures"] = []
        state["last_share_warnings"] = [reason]
        signature = json.dumps(
            {
                "at": pool_health.get("submit_stall_last_recovery_at"),
                "backend": recovery.get("backend_to"),
                "reason": recovery.get("reason"),
            },
            sort_keys=True,
        )
        if state.get("last_submit_path_self_heal_signature") != signature:
            state["last_submit_path_self_heal_signature"] = signature
            state["last_submit_path_self_heal_at"] = now_iso()
            log(reason)
            record_efficiency_event(
                "pool_submit_path_self_healed",
                "warning",
                reason,
                {
                    "recovery": recovery,
                    "pool_submit_stall_recoveries_total": pool_health.get("metrics_submit_stall_recoveries_total"),
                    "last_block_submit_age_seconds": accepted_age,
                    "selected_backend": pool_health.get("selected_backend"),
                },
            )
    elif submit_path_zero_success_storm or accepted_job_expired_storm:
        failure_count = int(pool_health.get("block_submit_failure_count") or 0)
        duplicate_count = int(pool_health.get("duplicate_block_count") or 0)
        submit_errors = int(pool_health.get("block_submit_error_count") or 0)
        overdue_count = int(pool_health.get("tip_overdue_count") or 0)
        stale_job_count = int(pool_health.get("stale_job_candidate_count") or 0)
        expired_submit_count = int(pool_health.get("stale_submit_count") or 0)
        valid_share_count = int(pool_health.get("valid_share_count") or 0)
        if accepted_job_expired_storm:
            reason = (
                "pool acceptedJobs cache is rejecting expired jobs while miners are connected "
                f"(expired_job_submits={expired_submit_count}, valid_shares={valid_share_count}, "
                f"threshold={pool_health.get('accepted_job_expired_storm_threshold')}, "
                f"ratio={pool_health.get('accepted_job_expired_storm_ratio')})"
            )
        else:
            reason = (
                "pool submit path has zero accepted block submissions while miners are producing candidates "
                f"(failures={failure_count}, duplicate={duplicate_count}, submit_errors={submit_errors}, "
                f"overdue={overdue_count}, stale_job_candidates={stale_job_count})"
            )
        if pool_in_startup_grace:
            reason += (
                f"; pool started {pool_started_age_seconds}s ago "
                f"(grace {DEFAULT_POOL_RESTART_GRACE_SECONDS}s)"
            )
        state["consecutive_failures"] = 0
        state["consecutive_syncing"] = 0
        state["consecutive_node_orphan_storm"] = 0
        state["consecutive_share_stalls"] = 0
        state["consecutive_submit_path_stalls"] = int(state.get("consecutive_submit_path_stalls", 0) or 0) + 1
        state["last_status"] = "pool_accepted_job_expired_storm" if accepted_job_expired_storm else "pool_submit_path_stall"
        state["last_failures"] = []
        state["last_share_warnings"] = [reason]
        log(f"pool_submit_path_stall consecutive={state['consecutive_submit_path_stalls']} reason={reason}")
        record_efficiency_event(
            "pool_accepted_job_expired_storm" if accepted_job_expired_storm else "pool_submit_path_stall",
            "critical",
            reason,
            {
                "connected_miners": miner_health.get("connected_count"),
                "accepted_job_expired_storm": accepted_job_expired_storm,
                "expired_job_submit_count": expired_submit_count,
                "valid_share_count": valid_share_count,
                "block_submit_failure_count": failure_count,
                "duplicate_block_count": duplicate_count,
                "block_submit_error_count": submit_errors,
                "tip_overdue_count": overdue_count,
                "stale_job_candidate_count": stale_job_count,
                "pool_started_age_seconds": pool_started_age_seconds,
            },
        )
        if repair:
            cooldown_remaining = DEFAULT_SUBMIT_PATH_REPAIR_COOLDOWN - (
                now - int(state.get("last_submit_path_repair_at", 0) or 0)
            )
            recovery_grace_remaining = 0
            if submit_path_recovery_recent and not submit_path_self_healed_recently:
                try:
                    recovery_grace_remaining = DEFAULT_SUBMIT_PATH_SELF_RECOVERY_GRACE_SECONDS - int(
                        submit_path_recovery_age or 0
                    )
                except (TypeError, ValueError):
                    recovery_grace_remaining = DEFAULT_SUBMIT_PATH_SELF_RECOVERY_GRACE_SECONDS
            if snapshot_active:
                log("pool submit-path restart suppressed during hourly snapshot")
                record_efficiency_event(
                    "repair_suppressed",
                    "warning",
                    "pool submit-path restart suppressed during hourly snapshot",
                    {"reason": reason},
                )
            elif autonomous_lab_active:
                log("pool submit-path restart suppressed during autonomous stack lab")
                record_efficiency_event(
                    "repair_suppressed",
                    "warning",
                    "pool submit-path restart suppressed during autonomous stack lab",
                    {"reason": reason},
                )
            elif pool_in_startup_grace:
                log(
                    "pool submit-path restart suppressed during startup grace "
                    f"age={pool_started_age_seconds}s threshold={DEFAULT_POOL_RESTART_GRACE_SECONDS}s"
                )
                record_efficiency_event(
                    "repair_suppressed",
                    "warning",
                    "pool submit-path restart suppressed during startup grace",
                    {
                        "pool_started_age_seconds": pool_started_age_seconds,
                        "grace_seconds": DEFAULT_POOL_RESTART_GRACE_SECONDS,
                        "reason": reason,
                    },
                )
            elif recovery_grace_remaining > 0:
                log(
                    "pool submit-path restart suppressed while in-process recovery is active "
                    f"grace_remaining={recovery_grace_remaining}s reason={reason}"
                )
                record_efficiency_event(
                    "repair_suppressed",
                    "warning",
                    "pool submit-path restart suppressed while in-process recovery is active",
                    {
                        "grace_remaining_seconds": recovery_grace_remaining,
                        "grace_seconds": DEFAULT_SUBMIT_PATH_SELF_RECOVERY_GRACE_SECONDS,
                        "submit_stall_last_recovery": pool_health.get("submit_stall_last_recovery"),
                        "reason": reason,
                    },
                )
            elif state["consecutive_submit_path_stalls"] < DEFAULT_SUBMIT_PATH_STALL_THRESHOLD:
                log(
                    "pool submit-path restart waiting for confirmation "
                    f"consecutive={state['consecutive_submit_path_stalls']} "
                    f"threshold={DEFAULT_SUBMIT_PATH_STALL_THRESHOLD}"
                )
            elif cooldown_remaining > 0:
                log(f"pool submit-path restart suppressed by cooldown_remaining={cooldown_remaining}s")
                record_efficiency_event(
                    "repair_suppressed",
                    "warning",
                    "pool submit-path restart suppressed by cooldown",
                    {"cooldown_remaining_seconds": cooldown_remaining, "reason": reason},
                )
            else:
                prefix = (
                    "pool acceptedJobs expired storm: "
                    if accepted_job_expired_storm
                    else "pool submit-path zero-success storm: "
                )
                ok = run_pool_restart(prefix + reason)
                state["last_repair_at"] = int(time.time())
                state["last_share_repair_at"] = int(time.time())
                state["last_submit_path_repair_at"] = int(time.time())
                if ok:
                    state["consecutive_submit_path_stalls"] = 0
    elif degraded_asics:
        affected = [
            {
                "ip": item.get("ip"),
                "name": item.get("display_name"),
                "submits": item.get("submits"),
                "shares": item.get("shares"),
                "last_share_age_seconds": item.get("last_share_age_seconds"),
                "last_submit_age_seconds": item.get("last_submit_age_seconds"),
            }
            for item in degraded_asics
        ]
        global_degradation = len(degraded_asics) >= max(2, (primary_miner_count + 1) // 2)
        reason = (
            f"{len(degraded_asics)}/{primary_miner_count} active miner source(s) are connected/submitting "
            f"but not receiving accepted shares"
        )
        if template_nodes:
            reason += f"; failing template node(s): {', '.join(template_nodes)}"
        if duplicate_block_storm:
            reason += f"; duplicate block storm count={pool_health.get('duplicate_block_count')}"
        if pool_template_frozen:
            reason += f"; pool template frozen for {pool_health.get('template_freeze_age_seconds')}s"
        if pool_in_startup_grace:
            reason += (
                f"; pool started {pool_started_age_seconds}s ago "
                f"(grace {DEFAULT_POOL_RESTART_GRACE_SECONDS}s)"
            )
        sync_repair_needed = bool(
            (status.get("sync_health") or {}).get("needs_fast_sync_repair")
            or pool_health.get("initial_download")
        )
        state["consecutive_failures"] = 0
        state["consecutive_syncing"] = 0
        state["consecutive_share_stalls"] = int(state.get("consecutive_share_stalls", 0) or 0) + 1
        state["last_status"] = "asic_degraded"
        state["last_failures"] = []
        state["last_share_warnings"] = [reason]
        state["last_degraded_asics"] = affected
        state["last_node_template_restart_at_by_node"] = node_template_restart_by_node
        log(f"asic_degraded consecutive={state['consecutive_share_stalls']} reason={reason}")
        record_efficiency_event(
            "asic_degraded",
            "critical" if global_degradation else "warning",
            reason,
            {
                "affected_miners": affected,
                "primary_miner_count": primary_miner_count,
                "template_nodes": template_nodes,
                "duplicate_block_storm": duplicate_block_storm,
                "pool_template_frozen": pool_template_frozen,
            },
        )
        if repair and should_restart_for_share_stall(
            state,
            1 if global_degradation else DEFAULT_SHARE_STALL_THRESHOLD,
            DEFAULT_ASIC_DEGRADED_REPAIR_COOLDOWN,
        ):
            ok = False
            if pool_in_startup_grace:
                log(
                    "ASIC degradation repair suppressed during pool startup grace "
                    f"age={pool_started_age_seconds}s threshold={DEFAULT_POOL_RESTART_GRACE_SECONDS}s"
                )
                record_efficiency_event(
                    "repair_suppressed",
                    "warning",
                    "ASIC degradation repair suppressed during pool startup grace",
                    {
                        "pool_started_age_seconds": pool_started_age_seconds,
                        "grace_seconds": DEFAULT_POOL_RESTART_GRACE_SECONDS,
                        "reason": reason,
                    },
                )
                sync_repair_needed = False
                template_nodes = []
                duplicate_block_storm = False
                pool_template_frozen = False
                global_degradation = False
            current_primary = current_rpc_primary()
            alternate_primary = (
                healthy_rpc_alternate(status, template_nodes, current_primary)
                if current_primary in template_nodes
                else None
            )
            if alternate_primary:
                cooldown_remaining = DEFAULT_RPC_FAILOVER_SWITCH_COOLDOWN - (now - rpc_primary_switch_at)
                if cooldown_remaining <= 0:
                    ok = run_rpc_failover_switch(
                        alternate_primary,
                        "ASIC mining degraded by current RPC primary template failure: " + reason,
                    )
                    if ok:
                        state["last_rpc_primary_switch_at"] = int(time.time())
                        state["last_rpc_primary"] = alternate_primary
                else:
                    log(
                        f"rpc primary switch suppressed by cooldown_remaining={cooldown_remaining}s "
                        f"current_primary={current_primary} alternate={alternate_primary}"
                    )
                    record_efficiency_event(
                        "repair_suppressed",
                        "warning",
                        "rpc primary switch suppressed by cooldown",
                        {
                            "cooldown_remaining_seconds": cooldown_remaining,
                            "current_primary": current_primary,
                            "alternate_primary": alternate_primary,
                            "reason": reason,
                        },
                    )
            if sync_repair_needed:
                node = template_nodes[0] if template_nodes else choose_lagging_node(status) or NODES[0]
                cooldown_remaining = DEFAULT_SYNCING_RESTART_COOLDOWN - (
                    now - int(state.get("last_sync_repair_at", 0) or 0)
                )
                if not ok and cooldown_remaining <= 0:
                    if suppress_sync_restart_for_active_import(status, state, reason, node):
                        ok = False
                    else:
                        ok = run_node_restart(node, "ASIC mining degraded by backend sync/template state: " + reason)
                        state["last_sync_repair_at"] = int(time.time())
                elif not ok:
                    log(f"sync repair restart for {node} suppressed by cooldown_remaining={cooldown_remaining}s")
                    record_efficiency_event(
                        "repair_suppressed",
                        "warning",
                        f"sync repair restart for {node} suppressed by cooldown",
                        {"cooldown_remaining_seconds": cooldown_remaining, "reason": reason},
                    )
            elif template_nodes:
                node = template_nodes[0]
                cooldown_remaining = DEFAULT_NODE_TEMPLATE_RESTART_COOLDOWN - (
                    now - int(node_template_restart_by_node.get(node, 0) or 0)
                )
                if not ok and cooldown_remaining <= 0:
                    ok = run_node_restart(node, "ASIC mining degraded by backend template failure: " + reason)
                    node_template_restart_by_node[node] = int(time.time())
                    state["last_node_template_restart_at_by_node"] = node_template_restart_by_node
                elif not ok:
                    log(f"node template restart for {node} suppressed by cooldown_remaining={cooldown_remaining}s")
                    record_efficiency_event(
                        "repair_suppressed",
                        "warning",
                        f"node template restart for {node} suppressed by cooldown",
                        {"cooldown_remaining_seconds": cooldown_remaining, "reason": reason},
                    )
            elif not ok and (global_degradation or duplicate_block_storm or pool_template_frozen):
                if pool_in_startup_grace:
                    log(
                        "pool restart suppressed during startup grace "
                        f"age={pool_started_age_seconds}s threshold={DEFAULT_POOL_RESTART_GRACE_SECONDS}s"
                    )
                    record_efficiency_event(
                        "repair_suppressed",
                        "warning",
                        "pool restart suppressed during startup grace",
                        {
                            "pool_started_age_seconds": pool_started_age_seconds,
                            "grace_seconds": DEFAULT_POOL_RESTART_GRACE_SECONDS,
                            "reason": reason,
                        },
                    )
                else:
                    ok = run_pool_restart("ASIC mining degraded by pool template behavior: " + reason)
            if ok:
                state["last_repair_at"] = int(time.time())
                state["last_share_repair_at"] = int(time.time())
                state["consecutive_share_stalls"] = 0
    elif share_stall or pool_template_frozen or duplicate_block_storm:
        share_warnings = []
        if pool_template_frozen:
            share_warnings.append(
                f"pool mining template frozen for {pool_health.get('template_freeze_age_seconds')}s"
            )
        if duplicate_block_storm:
            share_warnings.append(f"duplicate block storm={pool_health.get('duplicate_block_count')}")
        if pool_health.get("last_valid_share_age_seconds") is not None:
            share_warnings.append(
                f"pool has not accepted a valid share for {pool_health['last_valid_share_age_seconds']}s"
            )
        if pool_health.get("stale_submit_count") is not None:
            share_warnings.append(f"stale submits={pool_health['stale_submit_count']}")
        if pool_in_startup_grace:
            share_warnings.append(
                f"pool started {pool_started_age_seconds}s ago "
                f"(grace {DEFAULT_POOL_RESTART_GRACE_SECONDS}s)"
            )
        state["consecutive_failures"] = 0
        state["consecutive_syncing"] = 0
        state["consecutive_share_stalls"] = int(state.get("consecutive_share_stalls", 0) or 0) + 1
        state["last_status"] = "pool_template_frozen" if pool_template_frozen else "share_stall"
        state["last_failures"] = []
        state["last_share_warnings"] = share_warnings
        log(f"pool_stall consecutive={state['consecutive_share_stalls']} warnings={'; '.join(share_warnings) or 'none'}")
        record_efficiency_event(
            "pool_stall",
            "critical",
            "; ".join(share_warnings) or "pool stall detected",
            {
                "pool_template_frozen": pool_template_frozen,
                "duplicate_block_storm": duplicate_block_storm,
                "share_stall": share_stall,
            },
        )
        if repair and should_restart_for_share_stall(state, DEFAULT_SHARE_STALL_THRESHOLD, DEFAULT_SHARE_STALL_RESTART_COOLDOWN):
            if template_nodes:
                ok = run_node_restart(template_nodes[0], "persistent pool stall from template failure: " + "; ".join(share_warnings))
            elif pool_in_startup_grace:
                ok = False
                log(
                    "pool stall restart suppressed during startup grace "
                    f"age={pool_started_age_seconds}s threshold={DEFAULT_POOL_RESTART_GRACE_SECONDS}s"
                )
                record_efficiency_event(
                    "repair_suppressed",
                    "warning",
                    "pool stall restart suppressed during startup grace",
                    {
                        "pool_started_age_seconds": pool_started_age_seconds,
                        "grace_seconds": DEFAULT_POOL_RESTART_GRACE_SECONDS,
                        "warnings": share_warnings,
                    },
                )
            else:
                ok = run_pool_restart("persistent pool stall: " + "; ".join(share_warnings))
            state["last_repair_at"] = int(time.time())
            state["last_share_repair_at"] = int(time.time())
            if ok:
                state["consecutive_share_stalls"] = 0
    elif status.get("sync_health", {}).get("needs_fast_sync_repair"):
        sync_warnings = status.get("sync_warnings", status.get("warnings", []))
        recent_mining_work = pool_has_recent_mining_work(status)
        state["consecutive_failures"] = 0
        if recent_mining_work:
            state["consecutive_syncing"] = 0
        else:
            state["consecutive_syncing"] = int(state.get("consecutive_syncing", 0) or 0) + 1
        state["consecutive_share_stalls"] = 0
        state["last_status"] = "syncing"
        state["last_failures"] = []
        state["last_sync_warnings"] = sync_warnings
        log(
            f"syncing consecutive={state['consecutive_syncing']} "
            f"recent_mining_work={recent_mining_work} "
            f"warnings={'; '.join(sync_warnings) or 'none'}"
        )
        record_efficiency_event(
            "syncing",
            "warning",
            "; ".join(sync_warnings) or "sync repair needed",
            {
                "consecutive_syncing": state["consecutive_syncing"],
                "recent_mining_work": recent_mining_work,
            },
        )
        if repair and recent_mining_work:
            log("sync repair suppressed because pool mining work is fresh")
            record_efficiency_event(
                "repair_suppressed",
                "warning",
                "sync repair suppressed because pool mining work is fresh",
                {
                    "sync_warnings": sync_warnings,
                    "freshness_seconds": 60,
                },
            )
        elif repair and template_nodes:
            current_primary = current_rpc_primary()
            alternate_primary = (
                healthy_rpc_alternate(status, template_nodes, current_primary)
                if current_primary in template_nodes
                else None
            )
            if alternate_primary:
                cooldown_remaining = DEFAULT_RPC_FAILOVER_SWITCH_COOLDOWN - (now - rpc_primary_switch_at)
                if cooldown_remaining <= 0:
                    ok = run_rpc_failover_switch(
                        alternate_primary,
                        "sync warning from current RPC primary template failure: " + "; ".join(sync_warnings),
                    )
                    if ok:
                        state["last_rpc_primary_switch_at"] = int(time.time())
                        state["last_rpc_primary"] = alternate_primary
                        state["last_repair_at"] = int(time.time())
                        state["consecutive_syncing"] = 0
                else:
                    log(
                        f"rpc primary switch suppressed during syncing by cooldown_remaining={cooldown_remaining}s "
                        f"current_primary={current_primary} alternate={alternate_primary}"
                    )
                    record_efficiency_event(
                        "repair_suppressed",
                        "warning",
                        "rpc primary switch suppressed during syncing by cooldown",
                        {
                            "cooldown_remaining_seconds": cooldown_remaining,
                            "current_primary": current_primary,
                            "alternate_primary": alternate_primary,
                            "sync_warnings": sync_warnings,
                        },
                    )
        if repair and state["consecutive_syncing"] and should_restart_for_syncing(state, syncing_threshold, syncing_restart_cooldown):
            restart_node = template_nodes[0] if template_nodes else choose_lagging_node(status)
            if suppress_sync_restart_for_active_import(status, state, "; ".join(sync_warnings), restart_node):
                ok = False
            elif restart_node:
                ok = run_node_restart(restart_node, "persistent syncing: " + "; ".join(sync_warnings))
            else:
                ok = run_repair("restart", "persistent syncing: " + "; ".join(sync_warnings))
            state["last_repair_at"] = int(time.time())
            state["last_sync_repair_at"] = int(time.time())
            if ok:
                state["consecutive_syncing"] = 0
    else:
        if state.get("last_status") != status["overall"]:
            log(f"status={status['overall']} warnings={'; '.join(status['warnings']) or 'none'}")
        state["consecutive_failures"] = 0
        state["consecutive_syncing"] = 0
        state["consecutive_share_stalls"] = 0
        state["consecutive_submit_path_stalls"] = 0
        state["last_status"] = status["overall"]
        state["last_failures"] = []
        state["last_sync_warnings"] = []
        state["last_share_warnings"] = []

    state["updated_at"] = now_iso()
    write_state(state)
    return {"status": status, "watchdog_state": state}


def loop(
    interval: int,
    threshold: int,
    clean_restore_cooldown: int,
    syncing_threshold: int,
    syncing_restart_cooldown: int,
    miner_down_restart_seconds: int,
    miner_restart_cooldown: int,
) -> None:
    ensure_efficiency_event_log()
    write_dirty_shutdown_marker("watchdog loop running")
    log(
        "watchdog started "
        f"interval={interval}s threshold={threshold} clean_restore_cooldown={clean_restore_cooldown}s "
        f"syncing_threshold={syncing_threshold} syncing_restart_cooldown={syncing_restart_cooldown}s "
        f"miner_down_restart_seconds={miner_down_restart_seconds}s miner_restart_cooldown={miner_restart_cooldown}s "
        f"miner_useful_work_stall_seconds={DEFAULT_MINER_USEFUL_WORK_STALL_SECONDS}s "
        f"miner_useful_work_confirm={DEFAULT_MINER_USEFUL_WORK_STALL_CONFIRM_SECONDS}s "
        f"miner_useful_work_cooldown={DEFAULT_MINER_USEFUL_WORK_STALL_REPAIR_COOLDOWN}s "
        f"asic_hashrate_min={DEFAULT_ASIC_HASHRATE_MIN_GHS:g}GH/s "
        f"asic_hashrate_stale={DEFAULT_ASIC_HASHRATE_STALE_SECONDS}s "
        f"asic_hashrate_confirm={DEFAULT_ASIC_HASHRATE_CONFIRM_SECONDS}s "
        f"asic_hashrate_cooldown={DEFAULT_ASIC_HASHRATE_REPAIR_COOLDOWN}s "
        f"earnings_snapshot_interval={DEFAULT_EARNINGS_SNAPSHOT_INTERVAL_SECONDS}s"
    )
    while True:
        try:
            check_once(
                threshold,
                clean_restore_cooldown,
                syncing_threshold,
                syncing_restart_cooldown,
                miner_down_restart_seconds,
                miner_restart_cooldown,
                repair=True,
            )
        except Exception as exc:  # noqa: BLE001 - watchdog should keep running.
            log(f"watchdog check crashed: {exc}")
            record_efficiency_event("watchdog_check_crashed", "critical", str(exc))
        time.sleep(interval)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="BlockDAG pool watchdog")
    parser.add_argument("--loop", action="store_true", help="run continuously")
    parser.add_argument("--once", action="store_true", help="run one check")
    parser.add_argument("--boot-repair", action="store_true", help="run boot-time recovery")
    parser.add_argument("--repair", choices=["start", "restart", "clean"], help="run a repair immediately")
    parser.add_argument("--reason", default="manual request", help="repair reason")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--threshold", type=int, default=DEFAULT_FAILURE_THRESHOLD)
    parser.add_argument("--clean-restore-cooldown", type=int, default=DEFAULT_CLEAN_RESTORE_COOLDOWN)
    parser.add_argument("--syncing-threshold", type=int, default=DEFAULT_SYNCING_THRESHOLD)
    parser.add_argument("--syncing-restart-cooldown", type=int, default=DEFAULT_SYNCING_RESTART_COOLDOWN)
    parser.add_argument("--miner-down-restart-seconds", type=int, default=DEFAULT_MINER_DOWN_RESTART_SECONDS)
    parser.add_argument("--miner-restart-cooldown", type=int, default=DEFAULT_MINER_RESTART_COOLDOWN)
    args = parser.parse_args(argv)

    ensure_runtime()
    ensure_efficiency_event_log()
    if args.boot_repair:
        result = boot_repair(
            args.threshold,
            args.clean_restore_cooldown,
            args.syncing_threshold,
            args.syncing_restart_cooldown,
        )
        print(json.dumps(result, indent=2))
        return 0 if result.get("boot_repair") != "failed" else 1
    if args.repair:
        return 0 if run_repair(args.repair, args.reason) else 1
    if args.loop:
        loop(
            args.interval,
            args.threshold,
            args.clean_restore_cooldown,
            args.syncing_threshold,
            args.syncing_restart_cooldown,
            args.miner_down_restart_seconds,
            args.miner_restart_cooldown,
        )
        return 0

    result = check_once(
        args.threshold,
        args.clean_restore_cooldown,
        args.syncing_threshold,
        args.syncing_restart_cooldown,
        args.miner_down_restart_seconds,
        args.miner_restart_cooldown,
        repair=args.once or not args.loop,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
