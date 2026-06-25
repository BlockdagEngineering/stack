#!/usr/bin/env python3
"""Automatic repair worker for the BlockDAG pool stack."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import automation_control
from incident_journal import append_incident
from guard_core import automation_mutation_allowed
import pool_start_gate
from mining_health_triage import build_mining_health_triage
from stack_status_source import collect_stack_status
from pool_ops import (
    LOG_DIR,
    NODES,
    POOL_CONTAINER,
    POOL_ENV_FILE,
    PROJECT_ROOT,
    RUNTIME_DIR,
    action_log_path,
    compose_service_name,
    configure_miner,
    default_miner_pool_settings,
    docker_compose_command,
    docker_env_value,
    ensure_runtime,
    is_lan_ipv4,
    now_iso,
    record_earnings_snapshot,
    read_env_file_value,
    read_miner_admin_password,
    restart_miner,
    restart_miner_open,
    restore_clean,
    restart_stack,
    run_logged,
    start_stack,
    write_action_state,
)
from status_sampler import (
    fastsync_peer_quarantine_should_repair,
    node_mining_template_support_should_repair,
    repair_missing_tracked_miners,
    repair_node_mining_template_support,
    status_payload_has_tracking_gap,
)


STATE_FILE = RUNTIME_DIR / "watchdog-state.json"
WATCHDOG_LOG = LOG_DIR / "watchdog.log"
EFFICIENCY_EVENTS_FILE = LOG_DIR / "efficiency-events.jsonl"
LOCK_FILE = RUNTIME_DIR / "repair.lock"
DIRTY_SHUTDOWN_MARKER = RUNTIME_DIR / "dirty-shutdown.marker"
AUTONOMOUS_STACK_LAB_LOCK_FILE = RUNTIME_DIR / "autonomous-stack-lab.lock"

DEFAULT_INTERVAL_SECONDS = int(os.environ.get("BDAG_WATCHDOG_INTERVAL", "5"))
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
DEFAULT_ASIC_API_STALL_STALE_SECONDS = int(os.environ.get("BDAG_WATCHDOG_ASIC_API_STALL_STALE_SECONDS", "180"))
DEFAULT_ASIC_API_STALL_CONFIRM_SECONDS = int(os.environ.get("BDAG_WATCHDOG_ASIC_API_STALL_CONFIRM_SECONDS", "120"))
DEFAULT_ASIC_API_STALL_NO_ACTIVE_CONFIRM_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_ASIC_API_STALL_NO_ACTIVE_CONFIRM_SECONDS", "60")
)
DEFAULT_ASIC_API_STALL_REPAIR_COOLDOWN = int(
    os.environ.get("BDAG_WATCHDOG_ASIC_API_STALL_REPAIR_COOLDOWN", str(DEFAULT_MINER_RESTART_COOLDOWN))
)
DEFAULT_ASIC_STAGED_AUTH_RETRY_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_ASIC_STAGED_AUTH_RETRY_SECONDS", "300")
)
DEFAULT_ASIC_STAGED_POWER_CYCLE_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_ASIC_STAGED_POWER_CYCLE_SECONDS", "600")
)
DEFAULT_ASIC_REMOTE_POWER_CYCLE_COOLDOWN_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_ASIC_REMOTE_POWER_CYCLE_COOLDOWN_SECONDS", "1800")
)
DEFAULT_ASIC_REMOTE_POWER_CYCLE_TIMEOUT_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_ASIC_REMOTE_POWER_CYCLE_TIMEOUT_SECONDS", "120")
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
DEFAULT_STRATUM_NO_REQUEST_EVENT_THRESHOLD = int(
    os.environ.get("BDAG_WATCHDOG_STRATUM_NO_REQUEST_EVENT_THRESHOLD", "5")
)
DEFAULT_POOL_RESTART_GRACE_SECONDS = int(os.environ.get("BDAG_WATCHDOG_POOL_RESTART_GRACE_SECONDS", "90"))
DEFAULT_EARNINGS_SNAPSHOT_INTERVAL_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_EARNINGS_SNAPSHOT_INTERVAL_SECONDS", "60")
)
DEFAULT_NODE_TEMPLATE_RESTART_COOLDOWN = int(os.environ.get("BDAG_WATCHDOG_NODE_TEMPLATE_RESTART_COOLDOWN", "180"))
DEFAULT_NODE_ORPHAN_STORM_RESTART_COOLDOWN = int(
    os.environ.get("BDAG_WATCHDOG_NODE_ORPHAN_STORM_RESTART_COOLDOWN", "300")
)
DEFAULT_NODE_RPC_REFUSED_CONFIRM_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_NODE_RPC_REFUSED_CONFIRM_SECONDS", "60")
)
DEFAULT_NODE_RPC_REFUSED_REPAIR_COOLDOWN = int(
    os.environ.get("BDAG_WATCHDOG_NODE_RPC_REFUSED_REPAIR_COOLDOWN", "300")
)
DEFAULT_NODE_RPC_REFUSED_POOL_RESTART_GRACE_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_NODE_RPC_REFUSED_POOL_RESTART_GRACE_SECONDS", "30")
)
DEFAULT_NODE_PEER_LEAD_STALL_CONFIRM_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_NODE_PEER_LEAD_STALL_CONFIRM_SECONDS", "120")
)
DEFAULT_NODE_PEER_LEAD_HARD_STALL_CONFIRM_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_NODE_PEER_LEAD_HARD_STALL_CONFIRM_SECONDS", "10")
)
DEFAULT_NODE_PEER_LEAD_HARD_STALL_TEMPLATE_AGE_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_NODE_PEER_LEAD_HARD_STALL_TEMPLATE_AGE_SECONDS", "30")
)
DEFAULT_NODE_PEER_LEAD_HARD_STALL_JOB_AGE_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_NODE_PEER_LEAD_HARD_STALL_JOB_AGE_SECONDS", "12")
)
DEFAULT_NODE_PEER_LEAD_HARD_STALL_RECENT_WORK_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_NODE_PEER_LEAD_HARD_STALL_RECENT_WORK_SECONDS", "20")
)
DEFAULT_NODE_PEER_STARVATION_MIN_FRESH_PEERS = int(
    os.environ.get("BDAG_WATCHDOG_NODE_PEER_STARVATION_MIN_FRESH_PEERS", "2")
)
DEFAULT_NODE_PEER_LEAD_STALL_REPAIR_COOLDOWN = int(
    os.environ.get("BDAG_WATCHDOG_NODE_PEER_LEAD_STALL_REPAIR_COOLDOWN", "900")
)
DEFAULT_NODE_PEER_LEAD_HARD_STALL_REPAIR_COOLDOWN = int(
    os.environ.get("BDAG_WATCHDOG_NODE_PEER_LEAD_HARD_STALL_REPAIR_COOLDOWN", "300")
)
DEFAULT_NODE_PEER_LEAD_HARD_STALL_RETRY_COOLDOWN = int(
    os.environ.get("BDAG_WATCHDOG_NODE_PEER_LEAD_HARD_STALL_RETRY_COOLDOWN", "90")
)
DEFAULT_NODE_PEER_LEAD_ACTIVE_IMPORT_SUPPRESS_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_NODE_PEER_LEAD_ACTIVE_IMPORT_SUPPRESS_SECONDS", "180")
)
DEFAULT_NODE_PEER_LEAD_ACTIVE_IMPORT_WORSEN_BLOCKS = int(
    os.environ.get("BDAG_WATCHDOG_NODE_PEER_LEAD_ACTIVE_IMPORT_WORSEN_BLOCKS", "100")
)
DEFAULT_NODE_PEER_LEAD_ACTIVE_IMPORT_MAX_LEAD_BLOCKS = int(
    os.environ.get("BDAG_WATCHDOG_NODE_PEER_LEAD_ACTIVE_IMPORT_MAX_LEAD_BLOCKS", "120")
)
DEFAULT_NODE_PEER_LEAD_HARD_STALL_ACTIVE_IMPORT_MAX_WAIT_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_NODE_PEER_LEAD_HARD_STALL_ACTIVE_IMPORT_MAX_WAIT_SECONDS", "60")
)
DEFAULT_NODE_TEMPLATE_SYNC_WEDGE_CONFIRM_SECONDS = int(
    os.environ.get("BDAG_WATCHDOG_NODE_TEMPLATE_SYNC_WEDGE_CONFIRM_SECONDS", "45")
)
DEFAULT_NODE_TEMPLATE_SYNC_WEDGE_REPAIR_COOLDOWN = int(
    os.environ.get(
        "BDAG_WATCHDOG_NODE_TEMPLATE_SYNC_WEDGE_REPAIR_COOLDOWN",
        str(DEFAULT_NODE_PEER_LEAD_STALL_REPAIR_COOLDOWN),
    )
)
DEFAULT_FRESH_PRODUCTION_TEMPLATE_FLICKER_MAX_TEMPLATE_AGE_SECONDS = int(
    os.environ.get("BDAG_FRESH_PRODUCTION_TEMPLATE_FLICKER_MAX_TEMPLATE_AGE_SECONDS", "10")
)
DEFAULT_NODE_DAG_TIP_CLEANUP_COOLDOWN = int(
    os.environ.get("BDAG_WATCHDOG_NODE_DAG_TIP_CLEANUP_COOLDOWN", "1800")
)
DEFAULT_OPTIMUM_STATE_EVENT_COOLDOWN = int(os.environ.get("BDAG_WATCHDOG_OPTIMUM_STATE_EVENT_COOLDOWN", "300"))


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


AUTOMATIC_CLEAN_RESTORE_ENABLED = env_bool("BDAG_ENABLE_AUTOMATIC_CLEAN_RESTORE", False)
BOOT_REPAIR_DIRTY_POLICY = os.environ.get("BDAG_BOOT_REPAIR_DIRTY_POLICY", "start").strip().lower()
BOOT_REPAIR_CRITICAL_POLICY = os.environ.get("BDAG_BOOT_REPAIR_CRITICAL_POLICY", "restart").strip().lower()
PAUSE_POOL_DURING_NODE_RESTART = env_bool("BDAG_WATCHDOG_PAUSE_POOL_DURING_NODE_RESTART", True)
NODE_RECREATE_ENV_KEYS = (
    "BOOTSTRAP_PEER_ADDRESSES",
    "BDAG_ENABLE_NODE_MINING",
    "BDAG_NODE_MODULES",
    "BDAG_NODE_MINING_NO_PENDING_TX",
    "NODE_ARGS_APPEND",
    "MINING_ADDRESS",
    "MINING_POOL_ADDRESS",
    "POOL_COINBASE_ADDRESS",
)


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


def container_running(status: dict[str, Any], container_name: str) -> bool:
    containers = status.get("containers") if isinstance(status.get("containers"), dict) else {}
    container = containers.get(container_name) if isinstance(containers, dict) else None
    return bool(container.get("running")) if isinstance(container, dict) else False


def sync_progress_pool_pause_reason(status: dict[str, Any]) -> str:
    if sync_progress_is_advisory_with_ready_mining_pipeline(status):
        return ""
    if fresh_paid_work_bridges_status_backend_readiness_flicker(status):
        return ""

    sync = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    sync_status = str(sync.get("status") or "").strip().lower()
    lag = 0
    for key in ("remaining_blocks", "peer_ahead_blocks"):
        value = int_or_none(sync.get(key))
        if value is not None and value >= 0:
            lag = max(lag, value)
    node_sync_statuses: set[str] = set()
    nodes = sync.get("nodes")
    if isinstance(nodes, dict):
        for node in nodes.values():
            if not isinstance(node, dict):
                continue
            node_status = str(node.get("status") or "").strip().lower()
            if node_status in {"syncing", "catchup_pause"}:
                node_sync_statuses.add(node_status)
            for key in ("remaining_blocks", "peer_ahead_blocks"):
                value = int_or_none(node.get(key))
                if value is not None and value >= 0:
                    lag = max(lag, value)
    if sync_status in {"syncing", "catchup_pause"}:
        if lag > 0:
            return f"sync progress is {sync_status} with {lag} block(s) remaining"
        return f"sync progress is {sync_status}"
    if node_sync_statuses:
        node_status = "catchup_pause" if "catchup_pause" in node_sync_statuses else "syncing"
        if lag > 0:
            return f"node sync progress is {node_status} with {lag} block(s) remaining"
        return f"node sync progress is {node_status}"

    pool_health = status.get("pool_health", status.get("pool", {}))
    if isinstance(pool_health, dict) and pool_health.get("initial_download"):
        return "pool is waiting for node sync; initial download is active"
    return ""


def node_rpc_refused_text_matches(text: str, *, trusted_pool_context: bool = False) -> bool:
    text = str(text or "").lower()
    if not text:
        return False
    if any(
        fragment in text
        for fragment in (
            "dial tcp 127.0.0.1:38131",
            "dial tcp node:",
            "node-health-transport",
            "rpc transport",
        )
    ):
        return True
    refused = "connection refused" in text or "connect: connection refused" in text
    if not refused:
        return False
    if trusted_pool_context:
        return True
    return any(
        fragment in text
        for fragment in (
            "rpc",
            "node rpc",
            "node-health",
            "node transport",
            "template",
            "backend",
            "block template",
            "getblocktemplate",
            "submitblock",
        )
    )


def node_rpc_refused_evidence(status: dict[str, Any]) -> dict[str, Any]:
    pool_health = status.get("pool_health", status.get("pool", {}))
    if not isinstance(pool_health, dict):
        pool_health = {}
    pool_metrics = status.get("pool_metrics") if isinstance(status.get("pool_metrics"), dict) else {}
    source_job_health = pool_health.get("source_job_health")
    if not isinstance(source_job_health, dict):
        source_job_health = pool_metrics.get("source_job_health") if isinstance(pool_metrics.get("source_job_health"), dict) else {}
    source_backend_health = pool_health.get("source_backend_health")
    if not isinstance(source_backend_health, dict):
        source_backend_health = (
            pool_metrics.get("source_backend_health")
            if isinstance(pool_metrics.get("source_backend_health"), dict)
            else {}
        )
    pool_job_state = status.get("pool_job_state") if isinstance(status.get("pool_job_state"), dict) else {}
    warnings = [
        *[str(item) for item in status.get("sync_warnings", []) if item],
        *[str(item) for item in status.get("warnings", []) if item],
        *[str(item) for item in status.get("failures", []) if item],
    ]
    pool_text = " ".join(
        str(value or "")
        for value in (
            pool_health.get("last_rpc_refused_line"),
            pool_health.get("source_job_reason"),
            pool_health.get("source_job_status"),
            source_job_health.get("reason"),
            source_job_health.get("reason_code"),
            source_job_health.get("last_error"),
            source_job_health.get("error"),
            source_backend_health.get("reason"),
            source_backend_health.get("reason_code"),
            source_backend_health.get("last_error"),
            source_backend_health.get("error"),
            pool_job_state.get("reason_code"),
        )
    ).lower()
    warning_text = " ".join(item for item in warnings if node_rpc_refused_text_matches(item))
    text = f"{pool_text} {warning_text}".strip().lower()
    refused_recent = bool(
        pool_health.get("rpc_refused_recent")
        or pool_health.get("rpc_refused")
        or (
            pool_health.get("last_rpc_refused_age_seconds") is not None
            and int_or_none(pool_health.get("last_rpc_refused_age_seconds")) is not None
            and (int_or_none(pool_health.get("last_rpc_refused_age_seconds")) or 0)
            <= (int_or_none(pool_health.get("rpc_refused_warn_seconds")) or 120)
        )
    )
    transport_failure = (
        node_rpc_refused_text_matches(pool_text, trusted_pool_context=True)
        or bool(warning_text)
    )
    if not (refused_recent or transport_failure):
        return {"active": False}
    return {
        "active": True,
        "rpc_refused_recent": refused_recent,
        "transport_failure": transport_failure,
        "last_rpc_refused_age_seconds": pool_health.get("last_rpc_refused_age_seconds"),
        "pool_job_reason": pool_job_state.get("reason_code"),
        "source_job_reason": source_job_health.get("reason") or source_job_health.get("reason_code"),
        "source_backend_reason": source_backend_health.get("reason") or source_backend_health.get("reason_code"),
        "text": text[:1000],
    }


def pool_needs_restart_after_node_rpc_recovery(status: dict[str, Any]) -> bool:
    pool_job_state = status.get("pool_job_state") if isinstance(status.get("pool_job_state"), dict) else {}
    miner_health = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}
    connected = (
        int_or_none(pool_job_state.get("active_connections"))
        or int_or_none(miner_health.get("connected_count_effective"))
        or int_or_none(miner_health.get("connected_count"))
        or 0
    )
    authorized = int_or_none(pool_job_state.get("authorized_connections")) or 0
    ready = int_or_none(pool_job_state.get("ready_connections")) or 0
    without_job = int_or_none(pool_job_state.get("connections_without_current_job")) or 0
    template_seq = int_or_none(pool_job_state.get("current_template_seq")) or 0
    last_broadcast_age = int_or_none(pool_job_state.get("last_broadcast_age_ms"))
    reason = str(pool_job_state.get("reason_code") or "").strip().lower()
    return bool(
        connected > 0
        and (
            without_job > 0
            or (authorized > 0 and ready <= 0)
            or template_seq <= 0
            or last_broadcast_age is None
            or reason in {"miners_without_current_job", "no_current_template", "template_unavailable"}
        )
    )


def first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def selected_backend_peer_lead_stall_evidence(status: dict[str, Any]) -> dict[str, Any]:
    pool_health = status.get("pool_health", status.get("pool", {}))
    if not isinstance(pool_health, dict):
        pool_health = {}
    pool_metrics = status.get("pool_metrics") if isinstance(status.get("pool_metrics"), dict) else {}
    pool_job_state = status.get("pool_job_state") if isinstance(status.get("pool_job_state"), dict) else {}
    miner_health = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}
    source_job_health = pool_health.get("source_job_health")
    if not isinstance(source_job_health, dict):
        source_job_health = (
            pool_metrics.get("source_job_health")
            if isinstance(pool_metrics.get("source_job_health"), dict)
            else {}
        )

    selected_backend = str(
        pool_health.get("selected_backend")
        or pool_metrics.get("selected_backend")
        or ""
    )
    selected_health = pool_health.get("selected_backend_source_health")
    if not isinstance(selected_health, dict):
        selected_health = (
            pool_metrics.get("selected_backend_source_health")
            if isinstance(pool_metrics.get("selected_backend_source_health"), dict)
            else {}
        )
    source_backend_health = pool_health.get("source_backend_health")
    if not isinstance(source_backend_health, dict):
        source_backend_health = (
            pool_metrics.get("source_backend_health")
            if isinstance(pool_metrics.get("source_backend_health"), dict)
            else {}
        )
    if not selected_health and isinstance(source_backend_health, dict):
        if selected_backend and isinstance(source_backend_health.get(selected_backend), dict):
            selected_health = source_backend_health[selected_backend]
        else:
            selected_rows = [
                row
                for row in source_backend_health.values()
                if isinstance(row, dict) and row.get("selected") is True
            ]
            if selected_rows:
                selected_health = selected_rows[0]
    metrics_selected_health = pool_metrics.get("selected_backend_source_health")
    if isinstance(metrics_selected_health, dict):
        selected_health = {**metrics_selected_health, **selected_health}

    template_health = pool_health.get("template_health")
    if not isinstance(template_health, dict):
        template_health = status.get("template_health") if isinstance(status.get("template_health"), dict) else {}

    active_count = (
        int_or_none(pool_job_state.get("active_connections"))
        or int_or_none(pool_metrics.get("active_connections"))
        or int_or_none(miner_health.get("connected_count_effective"))
        or int_or_none(miner_health.get("connected_count"))
        or int_or_none(miner_health.get("managed_count"))
        or 0
    )
    authorized = (
        int_or_none(pool_job_state.get("authorized_connections"))
        or int_or_none(source_job_health.get("authorized_miners"))
        or 0
    )
    ready = (
        int_or_none(pool_job_state.get("ready_connections"))
        or int_or_none(source_job_health.get("ready_miners"))
        or int_or_none(pool_metrics.get("ready_connections"))
        or 0
    )
    without_job = int_or_none(pool_job_state.get("connections_without_current_job")) or 0
    miner_demand = active_count > 0 or authorized > 0

    lead = int_or_none(
        first_present(
            selected_health.get("node_p2p_best_peer_lead_blocks") if isinstance(selected_health, dict) else None,
            selected_health.get("p2p_best_peer_lead_blocks") if isinstance(selected_health, dict) else None,
            pool_health.get("source_selected_backend_p2p_best_peer_lead_blocks"),
            template_health.get("p2p_best_peer_lead_blocks"),
        )
    )
    tolerance = int_or_none(
        first_present(
            selected_health.get("node_p2p_peer_lead_tolerance_blocks") if isinstance(selected_health, dict) else None,
            selected_health.get("p2p_peer_lead_tolerance_blocks") if isinstance(selected_health, dict) else None,
            pool_health.get("source_selected_backend_p2p_peer_lead_tolerance_blocks"),
            template_health.get("p2p_peer_lead_tolerance_blocks"),
        )
    )
    if tolerance is None:
        tolerance = 10

    fresh_peer_count = int_or_none(
        first_present(
            selected_health.get("node_p2p_fresh_consensus_peer_count") if isinstance(selected_health, dict) else None,
            selected_health.get("p2p_fresh_consensus_peer_count") if isinstance(selected_health, dict) else None,
            template_health.get("p2p_fresh_consensus_peer_count"),
        )
    )
    consensus_peer_count = int_or_none(
        first_present(
            selected_health.get("node_p2p_consensus_peer_count") if isinstance(selected_health, dict) else None,
            selected_health.get("p2p_consensus_peer_count") if isinstance(selected_health, dict) else None,
            template_health.get("p2p_consensus_peer_count"),
        )
    )
    p2p_fresh_value = first_present(
        selected_health.get("node_p2p_mining_fresh") if isinstance(selected_health, dict) else None,
        selected_health.get("p2p_mining_fresh") if isinstance(selected_health, dict) else None,
        pool_health.get("source_selected_backend_p2p_fresh"),
        template_health.get("p2p_mining_fresh"),
    )
    p2p_fresh = None if p2p_fresh_value is None else boolish(p2p_fresh_value)
    submit_ready_value = first_present(
        selected_health.get("node_submit_ready") if isinstance(selected_health, dict) else None,
        selected_health.get("submit_ready") if isinstance(selected_health, dict) else None,
        pool_health.get("source_selected_backend_submit_ready"),
        template_health.get("submit_ready"),
    )
    submit_ready = None if submit_ready_value is None else boolish(submit_ready_value)
    mineable_value = first_present(
        selected_health.get("node_mineable") if isinstance(selected_health, dict) else None,
        selected_health.get("mineable") if isinstance(selected_health, dict) else None,
        selected_health.get("mineable_now") if isinstance(selected_health, dict) else None,
        pool_health.get("source_selected_backend_mineable"),
        template_health.get("mineable_now"),
    )
    mineable = None if mineable_value is None else boolish(mineable_value)
    template_age_seconds = int_or_none(
        first_present(
            selected_health.get("node_template_age_seconds") if isinstance(selected_health, dict) else None,
            selected_health.get("template_age_seconds") if isinstance(selected_health, dict) else None,
            template_health.get("template_age_seconds"),
        )
    )
    max_current_job_age_seconds = float_or_none(
        first_present(
            source_job_health.get("max_current_job_age_seconds"),
            pool_job_state.get("max_current_job_age_seconds"),
        )
    )
    last_broadcast_age_ms = int_or_none(pool_job_state.get("last_broadcast_age_ms"))
    if last_broadcast_age_ms is not None:
        last_broadcast_age_seconds = max(0.0, last_broadcast_age_ms / 1000.0)
    else:
        last_broadcast_age_seconds = None
    pool_job_age_candidates = [
        value
        for value in (max_current_job_age_seconds, last_broadcast_age_seconds)
        if value is not None
    ]
    pool_job_age_seconds = max(pool_job_age_candidates) if pool_job_age_candidates else None
    reason_values = [
        selected_health.get("node_p2p_mining_fresh_reason_code") if isinstance(selected_health, dict) else None,
        selected_health.get("p2p_mining_fresh_reason_code") if isinstance(selected_health, dict) else None,
        selected_health.get("node_reason_code") if isinstance(selected_health, dict) else None,
        selected_health.get("reason_code") if isinstance(selected_health, dict) else None,
        template_health.get("p2p_mining_fresh_reason_code"),
        template_health.get("reason_code"),
        pool_job_state.get("reason_code"),
    ]
    reason_text = " ".join(str(value or "") for value in reason_values).lower()
    lead_exceeds_tolerance = bool(lead is not None and lead > tolerance)
    peer_lead_reason = "peer_lead_exceeds_tolerance" in reason_text or "peer-lead-exceeds-tolerance" in reason_text
    p2p_peer_lead_unfresh = bool(p2p_fresh is False and (lead_exceeds_tolerance or peer_lead_reason))
    fresh_peer_starved = bool(
        fresh_peer_count is not None
        and fresh_peer_count < DEFAULT_NODE_PEER_STARVATION_MIN_FRESH_PEERS
    )
    consensus_peer_starved = bool(
        consensus_peer_count is not None
        and consensus_peer_count < DEFAULT_NODE_PEER_STARVATION_MIN_FRESH_PEERS
    )
    p2p_peer_starvation = bool(p2p_fresh is False and (fresh_peer_starved or consensus_peer_starved))
    backend_blocks_mining = bool(
        submit_ready is False
        or mineable is False
        or p2p_fresh is False
        or lead_exceeds_tolerance
        or ready <= 0
        or without_job > 0
        or pool_health.get("initial_download")
    )
    peer_lead_stall_active = bool(
        miner_demand
        and (lead_exceeds_tolerance or p2p_peer_lead_unfresh or p2p_peer_starvation)
        and backend_blocks_mining
    )
    if not peer_lead_stall_active:
        return {
            "active": False,
            "miner_demand": miner_demand,
            "lead": lead,
            "tolerance": tolerance,
            "p2p_mining_fresh": p2p_fresh,
            "fresh_consensus_peer_count": fresh_peer_count,
            "consensus_peer_count": consensus_peer_count,
            "peer_starvation": p2p_peer_starvation,
        }

    return {
        "active": True,
        "selected_backend": selected_backend,
        "lead": lead,
        "tolerance": tolerance,
        "p2p_mining_fresh": p2p_fresh,
        "fresh_consensus_peer_count": fresh_peer_count,
        "consensus_peer_count": consensus_peer_count,
        "peer_starvation": p2p_peer_starvation,
        "submit_ready": submit_ready,
        "mineable": mineable,
        "template_age_seconds": template_age_seconds,
        "pool_job_age_seconds": round(pool_job_age_seconds, 3) if pool_job_age_seconds is not None else None,
        "last_broadcast_age_seconds": (
            round(last_broadcast_age_seconds, 3) if last_broadcast_age_seconds is not None else None
        ),
        "active_miners": active_count,
        "authorized_miners": authorized,
        "ready_miners": ready,
        "connections_without_current_job": without_job,
        "reason_text": reason_text[:1000],
    }


def source_job_health_for_status(status: dict[str, Any]) -> dict[str, Any]:
    pool_health = status.get("pool_health", status.get("pool", {}))
    if not isinstance(pool_health, dict):
        pool_health = {}
    pool_metrics = status.get("pool_metrics") if isinstance(status.get("pool_metrics"), dict) else {}
    source_job_health = pool_health.get("source_job_health")
    if not isinstance(source_job_health, dict):
        source_job_health = (
            pool_metrics.get("source_job_health")
            if isinstance(pool_metrics.get("source_job_health"), dict)
            else {}
        )
    return dict(source_job_health) if isinstance(source_job_health, dict) else {}


def template_health_for_status(status: dict[str, Any]) -> dict[str, Any]:
    pool_health = status.get("pool_health", status.get("pool", {}))
    if not isinstance(pool_health, dict):
        pool_health = {}
    template_health = pool_health.get("template_health")
    if not isinstance(template_health, dict):
        template_health = status.get("template_health") if isinstance(status.get("template_health"), dict) else {}
    return dict(template_health) if isinstance(template_health, dict) else {}


def selected_backend_template_sync_wedge_evidence(status: dict[str, Any]) -> dict[str, Any]:
    pool_health = status.get("pool_health", status.get("pool", {}))
    if not isinstance(pool_health, dict):
        pool_health = {}
    pool_metrics = status.get("pool_metrics") if isinstance(status.get("pool_metrics"), dict) else {}
    pool_job_state = status.get("pool_job_state") if isinstance(status.get("pool_job_state"), dict) else {}
    miner_health = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}
    selected_health = selected_backend_health_for_status(status)
    source_job_health = source_job_health_for_status(status)
    template_health = template_health_for_status(status)

    selected_backend = str(pool_health.get("selected_backend") or pool_metrics.get("selected_backend") or "")
    active_count = (
        int_or_none(pool_job_state.get("active_connections"))
        or int_or_none(pool_metrics.get("active_connections"))
        or int_or_none(miner_health.get("connected_count_effective"))
        or int_or_none(miner_health.get("connected_count"))
        or int_or_none(miner_health.get("managed_count"))
        or 0
    )
    authorized = (
        int_or_none(pool_job_state.get("authorized_connections"))
        or int_or_none(source_job_health.get("authorized_miners"))
        or 0
    )
    ready = (
        int_or_none(pool_job_state.get("ready_connections"))
        or int_or_none(source_job_health.get("ready_miners"))
        or int_or_none(pool_metrics.get("ready_connections"))
        or 0
    )
    without_job = int_or_none(pool_job_state.get("connections_without_current_job")) or 0
    miner_demand = active_count > 0 or authorized > 0
    job_starved = bool(ready <= 0 and (without_job > 0 or miner_demand))

    lead = int_or_none(
        first_present(
            selected_health.get("node_p2p_best_peer_lead_blocks"),
            selected_health.get("p2p_best_peer_lead_blocks"),
            pool_health.get("source_selected_backend_p2p_best_peer_lead_blocks"),
            template_health.get("p2p_best_peer_lead_blocks"),
        )
    )
    tolerance = int_or_none(
        first_present(
            selected_health.get("node_p2p_peer_lead_tolerance_blocks"),
            selected_health.get("p2p_peer_lead_tolerance_blocks"),
            pool_health.get("source_selected_backend_p2p_peer_lead_tolerance_blocks"),
            template_health.get("p2p_peer_lead_tolerance_blocks"),
        )
    )
    if tolerance is None:
        tolerance = 10
    p2p_fresh_value = first_present(
        selected_health.get("node_p2p_mining_fresh"),
        selected_health.get("p2p_mining_fresh"),
        pool_health.get("source_selected_backend_p2p_fresh"),
        template_health.get("p2p_mining_fresh"),
    )
    p2p_fresh = None if p2p_fresh_value is None else boolish(p2p_fresh_value)
    fresh_peer_count = int_or_none(
        first_present(
            selected_health.get("node_p2p_fresh_consensus_peer_count"),
            selected_health.get("p2p_fresh_consensus_peer_count"),
            template_health.get("p2p_fresh_consensus_peer_count"),
        )
    )
    submit_ready_value = first_present(
        selected_health.get("node_submit_ready"),
        selected_health.get("submit_ready"),
        pool_health.get("source_selected_backend_submit_ready"),
        template_health.get("submit_ready"),
    )
    submit_ready = None if submit_ready_value is None else boolish(submit_ready_value)
    mineable_value = first_present(
        selected_health.get("node_mineable"),
        selected_health.get("mineable"),
        selected_health.get("mineable_now"),
        pool_health.get("source_selected_backend_mineable"),
        template_health.get("mineable_now"),
    )
    mineable = None if mineable_value is None else boolish(mineable_value)
    gbt_ready_value = first_present(
        selected_health.get("node_get_block_template_ready"),
        selected_health.get("get_block_template_ready"),
        template_health.get("get_block_template_ready"),
    )
    get_block_template_ready = None if gbt_ready_value is None else boolish(gbt_ready_value)
    template_age_seconds = int_or_none(
        first_present(
            selected_health.get("node_template_age_seconds"),
            selected_health.get("template_age_seconds"),
            template_health.get("template_age_seconds"),
        )
    )
    coinbase_valid_value = first_present(
        selected_health.get("node_template_coinbase_valid"),
        selected_health.get("template_coinbase_valid"),
        template_health.get("template_coinbase_valid"),
    )
    coinbase_valid = None if coinbase_valid_value is None else boolish(coinbase_valid_value)
    sync = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    reason_values = [
        selected_health.get("node_reason_code"),
        selected_health.get("reason_code"),
        selected_health.get("node_p2p_mining_fresh_reason_code"),
        selected_health.get("p2p_mining_fresh_reason_code"),
        selected_health.get("node_last_template_build_error"),
        selected_health.get("last_template_build_error"),
        template_health.get("reason_code"),
        template_health.get("p2p_mining_fresh_reason_code"),
        template_health.get("last_template_build_error"),
        pool_job_state.get("reason_code"),
        source_job_health.get("reason"),
        source_job_health.get("reason_code"),
        pool_health.get("source_job_reason"),
        pool_health.get("source_job_status"),
        pool_health.get("last_rpc_refused_line"),
        sync.get("source"),
        sync.get("error"),
        *(status.get("warnings", []) if isinstance(status.get("warnings"), list) else []),
    ]
    reason_text = " ".join(str(value or "") for value in reason_values).lower()
    lead_safe = lead is None or lead <= tolerance
    fresh_peers_safe = fresh_peer_count is None or fresh_peer_count >= 2
    p2p_safe = bool(p2p_fresh is True and lead_safe and fresh_peers_safe)
    template_blocked = bool(
        submit_ready is False
        or mineable is False
        or get_block_template_ready is False
    )
    syncing_reason = any(
        fragment in reason_text
        for fragment in (
            "node_syncing",
            "node-syncing",
            "node is syncing",
            "pending-template backend is syncing",
            "backend is syncing",
            "template_parent_stale",
            "template-parent-stale",
            "parents no longer match current mining tips",
        )
    )
    active = bool(
        miner_demand
        and job_starved
        and p2p_safe
        and template_blocked
        and syncing_reason
        and coinbase_valid is not False
    )
    return {
        "active": active,
        "selected_backend": selected_backend,
        "lead": lead,
        "tolerance": tolerance,
        "p2p_mining_fresh": p2p_fresh,
        "fresh_consensus_peer_count": fresh_peer_count,
        "submit_ready": submit_ready,
        "mineable": mineable,
        "get_block_template_ready": get_block_template_ready,
        "template_coinbase_valid": coinbase_valid,
        "template_age_seconds": template_age_seconds,
        "active_miners": active_count,
        "authorized_miners": authorized,
        "ready_miners": ready,
        "connections_without_current_job": without_job,
        "job_starved": job_starved,
        "template_blocked": template_blocked,
        "syncing_reason": syncing_reason,
        "reason_text": reason_text[:1000],
    }


def template_sync_wedge_hard_mining_outage(status: dict[str, Any], evidence: dict[str, Any]) -> bool:
    if not evidence.get("active"):
        return False
    ready_miners = int_or_none(evidence.get("ready_miners"))
    template_age = float_or_none(evidence.get("template_age_seconds"))
    template_expired = bool(
        template_age is not None
        and template_age >= DEFAULT_NODE_PEER_LEAD_HARD_STALL_TEMPLATE_AGE_SECONDS
    )
    return bool(
        ready_miners == 0
        and template_expired
        and not pool_has_recent_mining_work(status, DEFAULT_NODE_PEER_LEAD_HARD_STALL_RECENT_WORK_SECONDS)
    )


def peer_lead_hard_mining_outage(status: dict[str, Any], evidence: dict[str, Any]) -> bool:
    if not evidence.get("active"):
        return False
    active_miners = int_or_none(evidence.get("active_miners")) or 0
    authorized_miners = int_or_none(evidence.get("authorized_miners")) or 0
    ready_miners = int_or_none(evidence.get("ready_miners"))
    without_job = int_or_none(evidence.get("connections_without_current_job")) or 0
    miner_demand = active_miners > 0 or authorized_miners > 0
    if not miner_demand or ready_miners is None or ready_miners > 0:
        return False

    lead = int_or_none(evidence.get("lead"))
    tolerance = int_or_none(evidence.get("tolerance")) or 10
    peer_starvation = bool(evidence.get("peer_starvation"))
    template_age = float_or_none(evidence.get("template_age_seconds"))
    pool_job_age = float_or_none(evidence.get("pool_job_age_seconds"))
    p2p_fresh = evidence.get("p2p_mining_fresh")
    submit_ready = evidence.get("submit_ready")
    mineable = evidence.get("mineable")
    reason_text = str(evidence.get("reason_text") or "").lower()
    backend_blocked = bool(
        p2p_fresh is False
        and (submit_ready is False or mineable is False)
        and (lead is None or lead > tolerance or peer_starvation)
    )
    hard_peer_lead = bool(lead is not None and lead > tolerance)
    template_expired = bool(
        template_age is not None
        and template_age >= DEFAULT_NODE_PEER_LEAD_HARD_STALL_TEMPLATE_AGE_SECONDS
    )
    all_jobs_invalidated_long_enough = bool(
        pool_job_age is not None
        and pool_job_age >= DEFAULT_NODE_PEER_LEAD_HARD_STALL_JOB_AGE_SECONDS
    )
    node_syncing = "node_syncing" in reason_text or "node-syncing" in reason_text
    job_starved = ready_miners == 0 and (without_job > 0 or active_miners > 0 or authorized_miners > 0)
    return bool(
        job_starved
        and (template_expired or all_jobs_invalidated_long_enough)
        and backend_blocked
        and (hard_peer_lead or node_syncing or peer_starvation)
        and not pool_has_recent_mining_work(status, DEFAULT_NODE_PEER_LEAD_HARD_STALL_RECENT_WORK_SECONDS)
    )


def hard_peer_lead_outage_allows_active_import_wait(
    evidence: dict[str, Any],
    active_import_details: dict[str, Any],
) -> bool:
    if not active_import_details.get("active_import"):
        return False
    if active_import_details.get("suppression_expired"):
        return False
    if evidence.get("peer_starvation"):
        return False
    fresh_peer_count = int_or_none(evidence.get("fresh_consensus_peer_count"))
    consensus_peer_count = int_or_none(evidence.get("consensus_peer_count"))
    if fresh_peer_count is None or fresh_peer_count < DEFAULT_NODE_PEER_STARVATION_MIN_FRESH_PEERS:
        return False
    if consensus_peer_count is not None and consensus_peer_count < DEFAULT_NODE_PEER_STARVATION_MIN_FRESH_PEERS:
        return False
    if (
        active_import_details.get("lead_over_hard_limit")
        or active_import_details.get("worsened_from_best")
        or active_import_details.get("worsened_from_first")
    ):
        return False
    age_seconds = int_or_none(active_import_details.get("age_seconds"))
    if (
        age_seconds is not None
        and age_seconds >= DEFAULT_NODE_PEER_LEAD_HARD_STALL_ACTIVE_IMPORT_MAX_WAIT_SECONDS
    ):
        return False
    lead = int_or_none(evidence.get("lead"))
    tolerance = int_or_none(evidence.get("tolerance")) or 10
    max_lead = int_or_none(active_import_details.get("max_lead")) or max(
        DEFAULT_NODE_PEER_LEAD_ACTIVE_IMPORT_MAX_LEAD_BLOCKS,
        tolerance * 6,
    )
    return bool(lead is None or lead <= max_lead)


def is_primary_pool_identity(row: dict[str, Any], mining_address: str) -> bool:
    defaults = default_miner_pool_settings()
    expected_url = str(row.get("expected_pool_url") or row.get("configured_pool_url") or "")
    expected = str(
        row.get("expected_worker_user")
        or row.get("intended_wallet")
        or row.get("configured_pool_user")
        or row.get("active_pool_user")
        or ""
    ).lower()
    workers = [
        str(item).lower()
        for item in [
            *(row.get("workers", []) if isinstance(row.get("workers"), list) else []),
            row.get("intended_wallet"),
            row.get("configured_pool_user"),
            row.get("active_pool_user"),
        ]
        if item
    ]
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


def miner_stall_identity_key(row: dict[str, Any]) -> str:
    device_id = str(row.get("device_id") or "").strip().lower()
    if device_id.startswith("mac:"):
        return device_id
    mac = str(row.get("mac") or "").strip().lower()
    if mac:
        return f"mac:{mac}"
    ip = str(row.get("ip") or "").strip()
    if ip:
        return f"ip:{ip}"
    return ""


def normalise_mac(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("mac:"):
        text = text.split(":", 1)[1]
    compact = re.sub(r"[^0-9a-f]", "", text)
    if len(compact) == 12:
        return ":".join(compact[index : index + 2] for index in range(0, 12, 2))
    return text


def update_useful_work_stall_since(
    state: dict[str, Any],
    useful_work_stalled_asics: list[dict[str, Any]],
    degraded_asics: list[dict[str, Any]],
    now: int,
) -> dict[str, int]:
    previous = (
        state.get("miner_useful_work_stall_since")
        if isinstance(state.get("miner_useful_work_stall_since"), dict)
        else {}
    )
    tracked_rows: dict[str, dict[str, Any]] = {}
    for item in [*degraded_asics, *useful_work_stalled_asics]:
        key = miner_stall_identity_key(item)
        if key:
            tracked_rows[key] = item

    updated: dict[str, int] = {}
    for key, item in tracked_rows.items():
        old_value = previous.get(key)
        ip_key = f"ip:{item.get('ip')}" if item.get("ip") else ""
        if old_value is None and ip_key:
            old_value = previous.get(ip_key) or previous.get(str(item.get("ip")))
        try:
            updated[key] = int(old_value if old_value is not None else now)
        except (TypeError, ValueError):
            updated[key] = now
    state["miner_useful_work_stall_since"] = updated
    return updated


def update_asic_api_stall_since(
    state: dict[str, Any],
    api_stalled_asics: list[dict[str, Any]],
    now: int,
) -> dict[str, int]:
    previous = state.get("asic_api_stall_since") if isinstance(state.get("asic_api_stall_since"), dict) else {}
    updated: dict[str, int] = {}
    for item in api_stalled_asics:
        key = miner_stall_identity_key(item)
        if not key:
            continue
        old_value = previous.get(key)
        ip_key = f"ip:{item.get('ip')}" if item.get("ip") else ""
        if old_value is None and ip_key:
            old_value = previous.get(ip_key) or previous.get(str(item.get("ip")))
        try:
            updated[key] = int(old_value if old_value is not None else now)
        except (TypeError, ValueError):
            updated[key] = now
    state["asic_api_stall_since"] = updated
    return updated


def normalise_status_text(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "-")


def asic_api_stall_issue_text(row: dict[str, Any]) -> str:
    debug = row.get("debug") if isinstance(row.get("debug"), dict) else {}
    values: list[Any] = [
        row.get("issue"),
        row.get("debug_error"),
        row.get("api_error"),
        row.get("device_telemetry_errors"),
        row.get("telemetry_errors"),
        row.get("device_telemetry_status"),
        row.get("telemetry_status"),
        row.get("status"),
        row.get("health"),
        debug.get("error"),
        debug.get("debug_error"),
    ]
    return " ".join(str(value or "") for value in values).lower()


def asic_api_stall_evidence(row: dict[str, Any]) -> tuple[bool, str]:
    debug = row.get("debug") if isinstance(row.get("debug"), dict) else {}
    issue_text = asic_api_stall_issue_text(row)
    telemetry_status = normalise_status_text(
        row.get("device_telemetry_status") or row.get("telemetry_status") or ""
    )
    api_unavailable = debug.get("available") is False or bool(row.get("debug_error"))
    fragment_match = any(fragment in issue_text for fragment in ASIC_API_STALL_TEXT_FRAGMENTS)
    return bool(api_unavailable or telemetry_status == "degraded" or fragment_match), issue_text.strip()


def get_asic_staged_recovery(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    staged = (
        state.get("asic_staged_recovery_by_identity")
        if isinstance(state.get("asic_staged_recovery_by_identity"), dict)
        else {}
    )
    cleaned: dict[str, dict[str, Any]] = {}
    for key, value in staged.items():
        if not key:
            continue
        cleaned[str(key)] = dict(value) if isinstance(value, dict) else {}
    state["asic_staged_recovery_by_identity"] = cleaned
    return cleaned


def prune_asic_staged_recovery(state: dict[str, Any], active_keys: set[str]) -> dict[str, dict[str, Any]]:
    staged = get_asic_staged_recovery(state)
    state["asic_staged_recovery_by_identity"] = {
        key: value for key, value in staged.items() if key in active_keys
    }
    return state["asic_staged_recovery_by_identity"]


def seed_asic_staged_recovery(
    record: dict[str, Any],
    item: dict[str, Any],
    miner_restart_by_ip: dict[str, Any],
    now: int,
) -> None:
    record.setdefault("first_seen_at", now)
    record["last_seen_at"] = now
    ip = str(item.get("ip") or "")
    if ip:
        record["ip"] = ip
    if item.get("mac"):
        record["mac"] = item.get("mac")
    if not record.get("open_restart_at") and ip:
        old_restart = int_or_none(miner_restart_by_ip.get(ip))
        if old_restart and old_restart > 0:
            record["open_restart_at"] = old_restart
            record.setdefault("last_stage", "open-restart")


def asic_staged_recovery_stage(record: dict[str, Any], now: int) -> tuple[str, int]:
    if record.get("power_cycle_required_at"):
        return "hardware-power-cycle-required", 0
    open_at = int_or_none(record.get("open_restart_at"))
    if not open_at:
        return "open-restart", 0
    auth_at = int_or_none(record.get("auth_retry_at"))
    if not auth_at:
        wait = DEFAULT_ASIC_STAGED_AUTH_RETRY_SECONDS - (now - open_at)
        if wait <= 0:
            return "auth-restart-configure", 0
        return "waiting-auth-retry", wait
    wait = DEFAULT_ASIC_STAGED_POWER_CYCLE_SECONDS - (now - auth_at)
    if wait <= 0:
        return "hardware-power-cycle-required", 0
    return "waiting-power-cycle", wait


def mark_asic_hardware_power_cycle_required(
    state: dict[str, Any],
    miners: list[dict[str, Any]],
    reason: str,
    now: int,
) -> list[dict[str, Any]]:
    staged = get_asic_staged_recovery(state)
    marked: list[dict[str, Any]] = []
    for item in miners:
        key = miner_stall_identity_key(item)
        if not key:
            continue
        record = staged.setdefault(key, {})
        if record.get("power_cycle_required_at"):
            continue
        record["power_cycle_required_at"] = now
        record["last_stage"] = "hardware-power-cycle-required"
        record["last_seen_at"] = now
        marked.append(
            {
                "identity_key": key,
                "ip": item.get("ip"),
                "mac": item.get("mac"),
                "status": item.get("status"),
                "api_stall_issue": item.get("api_stall_issue"),
                "api_stall_no_active_pool": item.get("api_stall_no_active_pool"),
                "api_stall_no_request_pressure": item.get("api_stall_no_request_pressure"),
            }
        )
    state["asic_staged_recovery_by_identity"] = staged
    if marked:
        state["last_status"] = "asic_hardware_power_cycle_required"
        state["last_asic_hardware_power_cycle_required"] = marked
        record_efficiency_event(
            "asic_hardware_power_cycle_required",
            "critical",
            reason,
            {"affected_miners": marked},
        )
    return marked


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


def boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "ok", "ready"}


def sync_current_block_is_evm(sync: dict[str, Any]) -> bool:
    current = int_or_none(sync.get("current_block"))
    sync_current = int_or_none(sync.get("sync_current_block"))
    chain_current = int_or_none(sync.get("chain_block_count"))
    if current is None:
        return False
    if sync_current is not None and current == sync_current:
        return True
    return bool(
        sync.get("eth_syncing")
        and sync.get("evm_chain_syncing")
        and chain_current is not None
        and current < chain_current
    )


def sync_progress_is_advisory_to_mining(status: dict[str, Any]) -> bool:
    sync = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    if boolish(sync.get("mining_advisory_sync")) or bool(sync.get("evm_sync_advisory")):
        return True
    if boolish(sync.get("evm_chain_syncing")) and (
        boolish(sync.get("native_is_current")) or int_or_none(sync.get("p2p_network_gap")) == 0
    ):
        return True
    return sync_current_block_is_evm(sync)


def pool_mining_pipeline_ready(status: dict[str, Any]) -> bool:
    pool_job_state = status.get("pool_job_state") if isinstance(status.get("pool_job_state"), dict) else {}
    pool_health = status.get("pool_health", status.get("pool", {}))
    if not isinstance(pool_health, dict):
        pool_health = {}
    pool_metrics = status.get("pool_metrics") if isinstance(status.get("pool_metrics"), dict) else {}

    active = (
        int_or_none(pool_job_state.get("active_connections"))
        or int_or_none(pool_metrics.get("active_connections"))
        or 0
    )
    ready = (
        int_or_none(pool_job_state.get("ready_connections"))
        or int_or_none(pool_metrics.get("ready_connections"))
        or 0
    )
    without_job = int_or_none(pool_job_state.get("connections_without_current_job"))
    state_text = normalise_status_text(pool_job_state.get("status"))
    reason_text = normalise_status_text(pool_job_state.get("reason_code"))
    jobs_ready = bool(
        ready > 0
        and (active <= 0 or active >= ready)
        and (without_job is None or without_job == 0)
        and (not state_text or state_text in {"ok", "ready", "mining"})
        and (not reason_text or reason_text == "ok")
    )

    if not jobs_ready:
        return False
    if status.get("can_submit_blocks") is True:
        return True
    if fresh_paid_work_bridges_status_backend_readiness_flicker(status):
        return True

    source_backend_health = pool_health.get("source_backend_health")
    if not isinstance(source_backend_health, dict):
        source_backend_health = (
            pool_metrics.get("source_backend_health")
            if isinstance(pool_metrics.get("source_backend_health"), dict)
            else {}
        )
    backend_submit_ready = (
        boolish(pool_health.get("source_selected_backend_submit_ready"))
        or boolish(source_backend_health.get("submit_ready"))
    )
    backend_mineable = (
        boolish(pool_health.get("source_selected_backend_mineable"))
        or boolish(source_backend_health.get("mineable"))
        or boolish(source_backend_health.get("mineable_now"))
    )
    backend_p2p_fresh = (
        boolish(pool_health.get("source_selected_backend_p2p_fresh"))
        or boolish(source_backend_health.get("p2p_mining_fresh"))
        or int_or_none(pool_health.get("source_selected_backend_p2p_best_peer_lead_blocks")) == 0
    )
    return bool(backend_submit_ready and backend_mineable and backend_p2p_fresh)


def selected_backend_health_for_status(status: dict[str, Any]) -> dict[str, Any]:
    pool_health = status.get("pool_health", status.get("pool", {}))
    if not isinstance(pool_health, dict):
        pool_health = {}
    pool_metrics = status.get("pool_metrics") if isinstance(status.get("pool_metrics"), dict) else {}
    selected_backend = str(pool_health.get("selected_backend") or pool_metrics.get("selected_backend") or "")
    selected_health = pool_health.get("selected_backend_source_health")
    if not isinstance(selected_health, dict):
        selected_health = (
            pool_metrics.get("selected_backend_source_health")
            if isinstance(pool_metrics.get("selected_backend_source_health"), dict)
            else {}
        )
    source_backend_health = pool_health.get("source_backend_health")
    if not isinstance(source_backend_health, dict):
        source_backend_health = (
            pool_metrics.get("source_backend_health")
            if isinstance(pool_metrics.get("source_backend_health"), dict)
            else {}
        )
    if not selected_health and source_backend_health:
        if selected_backend and isinstance(source_backend_health.get(selected_backend), dict):
            selected_health = source_backend_health[selected_backend]
        else:
            rows = [
                row
                for row in source_backend_health.values()
                if isinstance(row, dict) and row.get("selected") is True
            ]
            if rows:
                selected_health = rows[0]
    metrics_selected_health = pool_metrics.get("selected_backend_source_health")
    if isinstance(metrics_selected_health, dict):
        selected_health = {**metrics_selected_health, **selected_health}
    return dict(selected_health) if isinstance(selected_health, dict) else {}


def fresh_paid_work_bridges_status_backend_readiness_flicker(status: dict[str, Any]) -> bool:
    if not pool_has_recent_mining_work(status):
        return False
    pool_job_state = status.get("pool_job_state") if isinstance(status.get("pool_job_state"), dict) else {}
    active = int_or_none(pool_job_state.get("active_connections")) or 0
    authorized = int_or_none(pool_job_state.get("authorized_connections")) or 0
    ready = int_or_none(pool_job_state.get("ready_connections")) or 0
    without_job = int_or_none(pool_job_state.get("connections_without_current_job")) or 0
    state_text = normalise_status_text(pool_job_state.get("status"))
    reason_text = normalise_status_text(pool_job_state.get("reason_code"))
    if not (
        authorized > 0
        and ready >= authorized
        and (active <= 0 or active >= authorized)
        and without_job == 0
        and (not state_text or state_text in {"ok", "ready", "mining"})
        and (not reason_text or reason_text == "ok")
    ):
        return False

    pool_health = status.get("pool_health", status.get("pool", {}))
    if not isinstance(pool_health, dict):
        pool_health = {}
    template_health = pool_health.get("template_health")
    if not isinstance(template_health, dict):
        template_health = status.get("template_health") if isinstance(status.get("template_health"), dict) else {}
    selected_health = selected_backend_health_for_status(status)
    if not selected_health and not template_health:
        return False

    mineable_value = first_present(
        selected_health.get("node_mineable"),
        selected_health.get("mineable"),
        selected_health.get("mineable_now"),
        pool_health.get("source_selected_backend_mineable"),
        template_health.get("mineable_now"),
    )
    submit_ready_value = first_present(
        selected_health.get("node_submit_ready"),
        selected_health.get("submit_ready"),
        pool_health.get("source_selected_backend_submit_ready"),
        template_health.get("submit_ready"),
    )
    p2p_fresh_value = first_present(
        selected_health.get("node_p2p_mining_fresh"),
        selected_health.get("p2p_mining_fresh"),
        pool_health.get("source_selected_backend_p2p_fresh"),
        template_health.get("p2p_mining_fresh"),
    )
    lead = int_or_none(
        first_present(
            selected_health.get("node_p2p_best_peer_lead_blocks"),
            selected_health.get("p2p_best_peer_lead_blocks"),
            pool_health.get("source_selected_backend_p2p_best_peer_lead_blocks"),
            template_health.get("p2p_best_peer_lead_blocks"),
        )
    )
    tolerance = int_or_none(
        first_present(
            selected_health.get("node_p2p_peer_lead_tolerance_blocks"),
            selected_health.get("p2p_peer_lead_tolerance_blocks"),
            pool_health.get("source_selected_backend_p2p_peer_lead_tolerance_blocks"),
            template_health.get("p2p_peer_lead_tolerance_blocks"),
        )
    )
    if tolerance is None:
        tolerance = 10
    age = float_or_none(
        first_present(
            selected_health.get("node_template_age_seconds"),
            selected_health.get("template_age_seconds"),
            template_health.get("template_age_seconds"),
        )
    )
    coinbase_valid_value = first_present(
        selected_health.get("node_template_coinbase_valid"),
        selected_health.get("template_coinbase_valid"),
        template_health.get("template_coinbase_valid"),
    )
    build_error_blocking = first_present(
        selected_health.get("node_last_template_build_error_blocking"),
        selected_health.get("last_template_build_error_blocking"),
        template_health.get("last_template_build_error_blocking"),
    )
    blocking_reasons = [
        str(item)
        for item in (template_health.get("blocking_reasons") or [])
        if item
    ]
    allowed_reasons = {"mineable=false", "submit_ready=false"}
    if blocking_reasons and any(reason not in allowed_reasons for reason in blocking_reasons):
        return False
    backend_flicker = bool(
        mineable_value is not None
        and submit_ready_value is not None
        and (not boolish(mineable_value) or not boolish(submit_ready_value))
    )
    return bool(
        backend_flicker
        and boolish(p2p_fresh_value)
        and (lead is None or lead <= tolerance)
        and (age is None or age <= DEFAULT_FRESH_PRODUCTION_TEMPLATE_FLICKER_MAX_TEMPLATE_AGE_SECONDS)
        and coinbase_valid_value is not False
        and build_error_blocking is not True
    )


def sync_progress_is_advisory_with_ready_mining_pipeline(status: dict[str, Any]) -> bool:
    return bool(sync_progress_is_advisory_to_mining(status) and pool_mining_pipeline_ready(status))


def primary_native_sync_height(status: dict[str, Any], node_info: dict[str, Any] | None = None) -> int:
    sync = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    values: list[int | None] = []
    if isinstance(node_info, dict):
        values.append(int_or_none(node_info.get("latest_block")))
    values.extend(
        [
            int_or_none(sync.get("chain_block_count")),
            int_or_none(sync.get("p2p_network_height")),
            int_or_none(sync.get("best_peer_mainorder")),
        ]
    )
    if not sync_current_block_is_evm(sync):
        values.append(int_or_none(sync.get("current_block")))
    return max([value for value in values if value is not None] or [0])


def primary_node_name() -> str:
    return NODES[0] if NODES else "node"


def sync_progress_for_node(status: dict[str, Any], node: str) -> dict[str, Any]:
    sync = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    progress_nodes = sync.get("nodes") if isinstance(sync.get("nodes"), dict) else {}
    progress = progress_nodes.get(node) if isinstance(progress_nodes.get(node), dict) else {}
    if node != primary_node_name():
        return dict(progress)

    # Redis-dashboard exposes the active node's sync state directly at
    # sync_progress.* while the in-process sampler exposes sync_progress.nodes.
    # Watchdog repairs must understand both shapes or a stalled catch-up can be
    # mistaken for a healthy pool-side pause.
    merged = dict(progress)
    for key in (
        "current_block",
        "highest_block",
        "remaining_blocks",
        "peer_ahead_blocks",
        "status",
        "error",
    ):
        if merged.get(key) is None and sync.get(key) is not None:
            merged[key] = sync.get(key)
    if sync_progress_is_advisory_with_ready_mining_pipeline(status):
        native_height = primary_native_sync_height(status)
        if native_height > 0:
            merged["current_block"] = native_height
            merged["highest_block"] = max(
                native_height,
                int_or_none(sync.get("p2p_network_height")) or 0,
                int_or_none(sync.get("best_peer_mainorder")) or 0,
            )
        if boolish(sync.get("native_is_current")) or int_or_none(sync.get("p2p_network_gap")) == 0:
            merged["remaining_blocks"] = 0
            merged["peer_ahead_blocks"] = 0
            merged["status"] = "synced"
    return merged


def node_running_for_sync(status: dict[str, Any], node: str, node_info: dict[str, Any]) -> bool:
    if node_info.get("child_running") is True:
        return True
    containers = status.get("containers") if isinstance(status.get("containers"), dict) else {}
    container = containers.get(node) if isinstance(containers.get(node), dict) else {}
    if container:
        return bool(container.get("running"))
    progress = sync_progress_for_node(status, node)
    return node == primary_node_name() and bool(progress)


def node_sync_height(status: dict[str, Any], node: str) -> int:
    nodes = status.get("nodes") if isinstance(status.get("nodes"), dict) else {}
    node_info = nodes.get(node) if isinstance(nodes.get(node), dict) else {}
    if node == primary_node_name() and sync_progress_is_advisory_with_ready_mining_pipeline(status):
        return primary_native_sync_height(status, node_info)
    progress = sync_progress_for_node(status, node)
    values = [
        int_or_none(node_info.get("latest_block")),
        int_or_none(progress.get("current_block")),
    ]
    return max([value for value in values if value is not None] or [0])


def asic_has_recent_useful_work(
    row: dict[str, Any],
    max_age_seconds: int = DEFAULT_ASIC_API_STALL_STALE_SECONDS,
) -> bool:
    if row.get("ready") is True or row.get("pool_active") is True or row.get("work_pool_active") is True:
        return True
    status_text = normalise_status_text(row.get("status") or row.get("health") or row.get("lane_status") or "")
    if row.get("connected") is True and status_text in {"ok", "ready", "connected", "mining", "active"}:
        return True

    recent_age = None
    for key in ("last_share_age_seconds", "last_submit_age_seconds", "last_pool_seen_age_seconds"):
        value = int_or_none(row.get(key))
        if value is not None:
            recent_age = value if recent_age is None else min(recent_age, value)
    if recent_age is None or recent_age > max_age_seconds:
        return False

    for key in (
        "shares",
        "submits",
        "blocks_found",
        "device_accepted",
        "accepted",
        "accepted_shares",
    ):
        value = int_or_none(row.get(key))
        if value is not None and value > 0:
            return True
    return bool(row.get("connected")) and recent_age <= min(max_age_seconds, 60)


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
    fresh_paid_work = connected > 0 and pool_has_recent_mining_work(
        status,
        DEFAULT_ASIC_HASHRATE_STALE_SECONDS,
    )
    if sync_progress.get("status") == "synced" and (remaining is None or remaining == 0) and fresh_paid_work:
        return False
    return True


def pool_has_recent_mining_work(status: dict[str, Any], freshness_seconds: int = 60) -> bool:
    """Return true only for recent accepted block submissions, not accepted shares."""
    pool_health = status.get("pool_health") if isinstance(status.get("pool_health"), dict) else {}
    block_age = int_or_none(pool_health.get("last_block_submit_age_seconds"))
    accepted_blocks = int_or_none(pool_health.get("block_submit_success_count")) or 0
    if block_age is not None:
        return bool(accepted_blocks > 0 and block_age <= freshness_seconds)
    sync_health = status.get("sync_health") if isinstance(status.get("sync_health"), dict) else {}
    if sync_health.get("pool_has_recent_paid_work"):
        return True
    return False


def pool_has_unpaid_template_loss(status: dict[str, Any]) -> bool:
    pool_health = status.get("pool_health") if isinstance(status.get("pool_health"), dict) else {}
    miner_health = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}
    if int_or_none(miner_health.get("connected_count_effective") or miner_health.get("connected_count")) in (None, 0):
        return False
    if pool_has_recent_mining_work(status):
        return False
    backend_unready = any(
        pool_health.get(key) is False
        for key in (
            "source_selected_backend_submit_ready",
            "source_selected_backend_mineable",
            "source_selected_backend_p2p_fresh",
        )
    )
    loss_ledger = pool_health.get("loss_ledger") if isinstance(pool_health.get("loss_ledger"), dict) else {}
    block_outcomes = (
        loss_ledger.get("block_outcomes")
        if isinstance(loss_ledger.get("block_outcomes"), dict)
        else {}
    )
    block_total = int_or_none(block_outcomes.get("total")) or 0
    block_accepted = int_or_none(block_outcomes.get("accepted")) or 0
    share_outcomes = (
        loss_ledger.get("share_outcomes")
        if isinstance(loss_ledger.get("share_outcomes"), dict)
        else {}
    )
    stale_rejects = int_or_none(share_outcomes.get("stale_job_rejects")) or 0
    return bool(
        backend_unready
        and (
            pool_health.get("initial_download")
            or pool_health.get("block_submit_zero_success_storm")
            or (block_total >= 5 and block_accepted == 0)
            or stale_rejects >= 10
        )
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


ASIC_API_STALL_TEXT_FRAGMENTS = (
    "/mcb/cgminer",
    "/mcb/pools",
    "cgminer_devs",
    "cgminercmd=devs",
    "context deadline exceeded",
    "miner request failed",
    "timed out",
    "timeout",
    "http 500",
    "server error",
    "connection refused",
    "connection reset",
    "remote end closed",
)
ASIC_API_STALL_STATUSES = {
    "api-degraded",
    "api_degraded",
    "degraded",
    "down",
    "no-stratum",
    "no_stratum",
    "not-mining",
    "not_mining",
    "offline",
    "unknown",
}


def asic_api_stall_no_active_pool_evidence(status: dict[str, Any]) -> bool:
    pool_health = status.get("pool_health", status.get("pool", {}))
    if not isinstance(pool_health, dict):
        pool_health = {}
    pool_job_state = status.get("pool_job_state") if isinstance(status.get("pool_job_state"), dict) else {}
    pool_metrics = status.get("pool_metrics") if isinstance(status.get("pool_metrics"), dict) else {}
    miner_health = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}

    managed_count = int_or_none(miner_health.get("managed_count")) or 0
    active_connections = (
        int_or_none(pool_job_state.get("active_connections"))
        or int_or_none(pool_metrics.get("active_connections"))
        or 0
    )
    authorized_connections = (
        int_or_none(pool_job_state.get("authorized_connections"))
        or int_or_none(pool_metrics.get("authorized_connections"))
        or 0
    )
    ready_connections = (
        int_or_none(pool_job_state.get("ready_connections"))
        or int_or_none(pool_metrics.get("ready_connections"))
        or 0
    )
    reason = str(pool_job_state.get("reason_code") or pool_health.get("job_state_reason") or "").strip().lower()
    no_request_total = int_or_none(pool_metrics.get("stratum_no_request_disconnects_total")) or 0
    return (
        managed_count > 0
        and active_connections <= 0
        and authorized_connections <= 0
        and ready_connections <= 0
        and (reason in {"no_active_miners", "no-active-miners", "no_clients", "no-clients"} or no_request_total > 0)
    )


def asic_api_stall_primary_miners(
    status: dict[str, Any],
    stale_seconds: int = DEFAULT_ASIC_API_STALL_STALE_SECONDS,
) -> list[dict[str, Any]]:
    pool_health = status.get("pool_health", status.get("pool", {}))
    if not isinstance(pool_health, dict):
        pool_health = {}
    no_active_pool_evidence = asic_api_stall_no_active_pool_evidence(status)
    if pool_initial_download_effective(status):
        return []
    if (int_or_none(pool_health.get("job_notify_count")) or 0) <= 0 and not no_active_pool_evidence:
        return []
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
            "expired_job_reconnect_failed_no_share",
            "block_submit_zero_success_storm",
            "initial_download",
            "rpc_refused",
        )
    ):
        return []
    if template_failing_nodes(status) or active_rpc_template_failing(status):
        return []

    mining_address = str(status.get("mining_address") or "")
    miners = ((status.get("miner_health") or {}).get("miners") or [])
    affected: list[dict[str, Any]] = []
    for row in miners:
        if not isinstance(row, dict) or not row.get("managed"):
            continue
        if row.get("device_type") != "asic" or not is_lan_ipv4(str(row.get("ip", ""))):
            continue
        if not is_primary_pool_identity(row, mining_address):
            continue
        api_stall_evidence, issue_text = asic_api_stall_evidence(row)
        status_text = normalise_status_text(row.get("status") or row.get("health") or "")
        row_connected = bool(
            row.get("connected") or row.get("pool_active") is True or row.get("work_pool_active") is True
        )
        if asic_has_recent_useful_work(row, stale_seconds):
            continue
        if row_connected and not (api_stall_evidence and no_active_pool_evidence):
            continue
        if status_text not in ASIC_API_STALL_STATUSES and not (api_stall_evidence and no_active_pool_evidence):
            continue
        stale_age = (
            int_or_none(row.get("last_pool_seen_age_seconds"))
            or int_or_none(row.get("last_share_age_seconds"))
            or int_or_none(row.get("last_submit_age_seconds"))
        )
        if stale_age is not None and stale_age < stale_seconds and not no_active_pool_evidence:
            continue
        if not api_stall_evidence:
            continue
        item = dict(row)
        item["api_stall_issue"] = issue_text
        item["api_stall_stale_age_seconds"] = stale_age
        item["api_stall_no_active_pool"] = no_active_pool_evidence
        item["restart_open_first"] = True
        affected.append(item)
    return affected


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


def pool_start_blocked_by_status(status: dict[str, Any]) -> tuple[bool, str]:
    decision = pool_start_gate.pool_start_decision(status)
    return (not decision.allowed), decision.reason


def pool_stopped_is_only_stack_failure(stack_failures: list[Any]) -> bool:
    if not stack_failures:
        return False
    return all(
        POOL_CONTAINER in str(item) and "not running" in str(item)
        for item in stack_failures
    )


def active_rpc_template_failing(status: dict[str, Any]) -> bool:
    return False


def choose_template_probe_repair_node(status: dict[str, Any], active_node: str | None) -> str | None:
    nodes = status.get("nodes", {}) or {}
    failing = [
        node
        for node in NODES
        if (nodes.get(node, {}) or {}).get("template_probe_failing")
    ]
    if not failing:
        return None

    candidates = list(failing)
    if active_node in candidates and len(candidates) > 1:
        candidates = [node for node in candidates if node != active_node]

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


def choose_active_rpc_repair_node(status: dict[str, Any], active_node: str | None) -> str | None:
    failing = rpc_probe_failing_nodes(status)
    if active_node in failing:
        return active_node
    return choose_template_probe_repair_node(status, active_node)


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


def refresh_maintenance_state(state: dict, autonomous_lab_active: bool) -> None:
    previous = state.get("maintenance") if isinstance(state.get("maintenance"), dict) else {}
    previous_active = bool(previous.get("active"))
    previous_reason = str(previous.get("reason") or "")

    reason = ""
    if autonomous_lab_active:
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
    action = {
        "start": automation_control.ACTION_STACK_START,
        "restart": automation_control.ACTION_STACK_RESTART,
        "clean": automation_control.ACTION_STACK_CLEAN_RESTORE,
    }.get(mode, f"stack_{mode}")
    if not automation_mutation_allowed(
        actor="watchdog",
        action=action,
        target="stack",
        reason=reason,
        log=log,
        incident_source="watchdog",
    ):
        return False

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


def normalize_node_recreate_env_value(name: str, value: str | None) -> str:
    text = (value or "").strip()
    if name == "BOOTSTRAP_PEER_ADDRESSES":
        peers = sorted({item.strip() for item in text.split(",") if item.strip()})
        return ",".join(peers)
    return text


def node_env_recreate_mismatches(node_service: str, log_path: Path) -> list[dict[str, str]]:
    result = run_logged(
        ["docker", "inspect", "-f", "{{json .Config.Env}}", node_service],
        log_path,
        timeout=30,
    )
    if not result.ok or not result.stdout.strip():
        log(
            f"node env drift check skipped for {node_service}: "
            f"inspect failed stdout={str(getattr(result, 'stdout', '')).strip()} "
            f"stderr={str(getattr(result, 'stderr', '')).strip()}"
        )
        return []
    try:
        env_rows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        log(f"node env drift check skipped for {node_service}: invalid inspect JSON: {exc}")
        return []
    mismatches: list[dict[str, str]] = []
    for key in NODE_RECREATE_ENV_KEYS:
        desired = read_env_file_value(POOL_ENV_FILE, key)
        if desired is None:
            continue
        actual = docker_env_value(env_rows, key)
        normalized_desired = normalize_node_recreate_env_value(key, desired)
        normalized_actual = normalize_node_recreate_env_value(key, actual)
        if normalized_desired == normalized_actual:
            continue
        mismatches.append(
            {
                "key": key,
                "desired": normalized_desired,
                "actual": normalized_actual,
            }
        )
    return mismatches


def run_node_restart(node_service: str, reason: str) -> bool:
    if node_service not in NODES:
        log(f"targeted node restart skipped for unknown node={node_service} reason={reason}")
        return False
    if not automation_mutation_allowed(
        actor="watchdog",
        action=automation_control.ACTION_NODE_RESTART,
        target=node_service,
        reason=reason,
        log=log,
        incident_source="watchdog",
    ):
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

    # NODES contains runtime container names. The Compose service may be named
    # differently after topology migration. A plain docker restart preserves the
    # original container environment, so recreate through Compose when .env has
    # drifted from the running container.
    env_mismatches = node_env_recreate_mismatches(node_service, log_path)
    restart_method = "compose-recreate" if env_mismatches else "docker-restart"
    if env_mismatches:
        mismatch_keys = ",".join(item["key"] for item in env_mismatches)
        log(
            f"targeted node repair will recreate {node_service} to apply env drift keys={mismatch_keys}"
        )
    pool_was_running = False
    pool_stop_ok = True
    pool_start_ok = True
    pool_stop_result = None
    pool_start_result = None
    if PAUSE_POOL_DURING_NODE_RESTART and POOL_CONTAINER:
        inspect_result = run_logged(
            ["docker", "inspect", "-f", "{{.State.Running}}", POOL_CONTAINER],
            log_path,
            timeout=30,
        )
        pool_was_running = inspect_result.ok and str(getattr(inspect_result, "stdout", "")).strip().lower() == "true"
        if pool_was_running:
            pool_stop_result = run_logged(["docker", "stop", POOL_CONTAINER], log_path, timeout=120)
            pool_stop_ok = pool_stop_result.ok

    if pool_stop_ok:
        if env_mismatches:
            command = docker_compose_command(
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--no-build",
                "--pull",
                "never",
                compose_service_name(node_service),
            )
        else:
            command = ["docker", "restart", node_service]
        result = run_logged(command, log_path, timeout=180)
    else:
        result = pool_stop_result

    if pool_was_running and pool_stop_ok:
        pool_start_result = run_logged(["docker", "start", POOL_CONTAINER], log_path, timeout=120)
        pool_start_ok = pool_start_result.ok

    ok = bool(result and result.ok and pool_stop_ok and pool_start_ok)

    state_payload.update(
        {
            "status": "ok" if ok else "failed",
            "finished_at": now_iso(),
            "elapsed": round(time.time() - started, 3),
            "pool_paused": pool_was_running,
            "pool_stop_ok": pool_stop_ok,
            "pool_start_ok": pool_start_ok,
            "restart_method": restart_method,
            "env_recreate_mismatches": env_mismatches,
        }
    )
    write_action_state(state_payload)
    log(f"finished targeted restart for {node_service} status={state_payload['status']} elapsed={state_payload['elapsed']}s")
    if not ok:
        record_failed_repair(
            f"targeted node restart for {node_service}",
            reason,
            {
                "node": node_service,
                "log_path": str(log_path),
                "pool_paused": pool_was_running,
                "pool_stop_ok": pool_stop_ok,
                "pool_start_ok": pool_start_ok,
            },
        )
    lock_handle.close()
    return ok


def run_node_dag_tip_cleanup(node_service: str, reason: str) -> bool:
    if node_service not in NODES:
        log(f"node DAG tip cleanup skipped for unknown node={node_service} reason={reason}")
        return False
    if not automation_mutation_allowed(
        actor="watchdog",
        action=automation_control.ACTION_NODE_RESTART,
        target=node_service,
        reason=reason,
        log=log,
        incident_source="watchdog",
    ):
        return False

    lock_handle = acquire_lock(blocking=False)
    if lock_handle is None:
        log(f"node DAG tip cleanup skipped because another repair is running; node={node_service} reason={reason}")
        return False

    started = time.time()
    action_name = f"cleanuptips-{node_service}"
    log_path = action_log_path(action_name)
    state_payload = {
        "name": action_name,
        "mode": "node-cleanuptips",
        "node": node_service,
        "reason": reason,
        "status": "running",
        "started_at": now_iso(),
        "finished_at": None,
        "log_path": str(log_path),
    }
    write_action_state(state_payload)
    log(f"starting node DAG tip cleanup for {node_service}: {reason}; log={log_path}")

    node_arg = shlex.quote(node_service)
    script = f"""set -euo pipefail
node={node_arg}
image=$(docker inspect "$node" --format '{{{{.Config.Image}}}}')
uid=$(docker exec "$node" id -u bdagStack 2>/dev/null || printf '999')
gid=$(docker exec "$node" id -g bdagStack 2>/dev/null || printf '999')
restart_node() {{
    docker start "$node" >/dev/null 2>&1 || true
}}
trap restart_node EXIT
docker stop "$node" || true
docker run --rm --volumes-from "$node" --user "${{uid}}:${{gid}}" --entrypoint /usr/local/bin/blockdag-node "$image" --configfile /etc/bdagStack/node.conf --cleanuptips
trap - EXIT
docker start "$node"
"""
    result = run_logged(["bash", "-lc", script], log_path, timeout=600)
    ok = result.ok

    state_payload.update(
        {
            "status": "ok" if ok else "failed",
            "finished_at": now_iso(),
            "elapsed": round(time.time() - started, 3),
        }
    )
    write_action_state(state_payload)
    log(f"finished node DAG tip cleanup for {node_service} status={state_payload['status']} elapsed={state_payload['elapsed']}s")
    if not ok:
        record_failed_repair(
            f"node DAG tip cleanup for {node_service}",
            reason,
            {"node": node_service, "log_path": str(log_path)},
        )
    lock_handle.close()
    return ok


def run_pool_restart(reason: str) -> bool:
    if not automation_mutation_allowed(
        actor="watchdog",
        action=automation_control.ACTION_ASIC_POOL_RESTART,
        target=POOL_CONTAINER,
        reason=reason,
        log=log,
        incident_source="watchdog",
    ):
        return False
    gate = pool_start_gate.pool_start_decision(pool_start_gate.read_latest_status_payload())
    if not gate.allowed:
        log(f"pool restart blocked by pool start gate: {gate.reason}; reason={reason}")
        record_efficiency_event(
            "pool_restart_blocked",
            "warning",
            "pool restart blocked by pool start gate",
            {"reason": reason, "blocked_reason": gate.reason, "pool_container": POOL_CONTAINER},
        )
        return False

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
    target_label = ",".join(str(item.get("ip") or "") for item in targets) or "asic-miners"
    staged_auth_recovery = any(
        str(item.get("staged_recovery_stage") or "") == "auth-restart-configure" for item in targets
    )
    open_restart_only = bool(targets) and all(
        bool(item.get("restart_open_first")) or "api-stall" in reason.lower() for item in targets
    ) and not staged_auth_recovery
    mutation_action = (
        automation_control.ACTION_ASIC_MINER_OPEN_RESTART
        if open_restart_only
        else automation_control.ACTION_ASIC_MINER_RESTART
    )
    if not automation_mutation_allowed(
        actor="watchdog",
        action=mutation_action,
        target=target_label,
        reason=reason,
        log=log,
        incident_source="watchdog",
    ):
        return {
            "status": "suppressed",
            "reason": "automation control denied ASIC miner restart",
            "target_count": len(targets),
            "results": [],
        }

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
                open_restart_first = bool(target.get("restart_open_first")) or "api-stall" in reason.lower()
                staged_recovery_stage = str(target.get("staged_recovery_stage") or "")
                try:
                    if staged_recovery_stage == "auth-restart-configure":
                        if password:
                            configure_result: dict[str, Any]
                            try:
                                configure_result = configure_miner(
                                    ip=ip,
                                    admin_password=password,
                                    pool_url=target.get("expected_pool_url") or defaults["pool_url"],
                                    worker_user=target.get("expected_worker_user") or defaults["worker_user"],
                                    pool_password=defaults["pool_password"],
                                    replace_existing=True,
                                )
                            except Exception as exc:  # noqa: BLE001 - still try the restart path.
                                configure_result = {
                                    "ip": ip,
                                    "status": "failed",
                                    "error": str(exc),
                                }
                            try:
                                restart_result = restart_miner(ip, password)
                            except Exception as exc:  # noqa: BLE001 - open restart may still recover the controller.
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
                            failed_steps = [
                                name
                                for name, step in (("configure", configure_result), ("restart", restart_result))
                                if step.get("status") == "failed"
                            ]
                            result = {
                                "ip": ip,
                                "status": "failed" if "restart" in failed_steps else ("partial" if failed_steps else "ok"),
                                "action": "auth-configure-restart",
                                "configure": configure_result,
                                "restart": restart_result,
                            }
                        else:
                            result = {
                                **restart_miner_open(ip),
                                "action": "restart-open-auth-stage-no-password",
                                "note": (
                                    "authenticated staged recovery could not rewrite config without a saved admin "
                                    "password"
                                ),
                            }
                            if result.get("status") == "ok":
                                result["status"] = "partial"
                    elif open_restart_first:
                        try:
                            result = {
                                **restart_miner_open(ip),
                                "action": "restart-open-api-stall",
                                "note": "ASIC API/cgminer endpoint was stalled; open restart is preferred over config rewrite",
                            }
                        except Exception as exc:  # noqa: BLE001 - try authenticated restart before failing.
                            if password:
                                try:
                                    result = {
                                        **restart_miner(ip, password),
                                        "action": "restart-auth-fallback",
                                        "open_restart_error": str(exc),
                                    }
                                except Exception as auth_exc:  # noqa: BLE001
                                    result = {
                                        "ip": ip,
                                        "status": "failed",
                                        "action": "restart-open-api-stall",
                                        "error": str(exc),
                                        "auth_restart_error": str(auth_exc),
                                    }
                            else:
                                result = {
                                    "ip": ip,
                                    "status": "failed",
                                    "action": "restart-open-api-stall",
                                    "error": str(exc),
                                }
                    elif target.get("configured") is False and password:
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


def asic_power_cycle_command_map() -> dict[str, str]:
    commands: dict[str, str] = {}
    raw_json = os.environ.get("BDAG_ASIC_POWER_CYCLE_COMMANDS_JSON", "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            log(f"invalid BDAG_ASIC_POWER_CYCLE_COMMANDS_JSON: {exc}")
            parsed = {}
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                command = str(value or "").strip()
                if command:
                    commands[normalise_mac(key) or str(key).strip().lower()] = command

    raw_by_mac = os.environ.get("BDAG_ASIC_POWER_CYCLE_COMMAND_BY_MAC", "")
    for line in raw_by_mac.splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, command = text.split("=", 1)
        command = command.strip()
        if command:
            commands[normalise_mac(key) or key.strip().lower()] = command

    fallback = os.environ.get("BDAG_ASIC_POWER_CYCLE_COMMAND", "").strip()
    if fallback:
        commands.setdefault("*", fallback)
    return commands


def asic_power_cycle_command_for(item: dict[str, Any]) -> str:
    commands = asic_power_cycle_command_map()
    if not commands:
        return ""
    mac = normalise_mac(item.get("mac"))
    identity = miner_stall_identity_key(item).lower()
    for key in (mac, identity, identity.removeprefix("mac:"), str(item.get("ip") or "").strip(), "*"):
        if key and key in commands:
            return commands[key]
    return ""


def render_asic_power_cycle_command(template: str, item: dict[str, Any], reason: str) -> str:
    mac = normalise_mac(item.get("mac"))
    identity = miner_stall_identity_key(item)
    values = {
        "ip": str(item.get("ip") or ""),
        "mac": mac,
        "identity": identity,
        "reason": reason.replace("\n", " ")[:240],
    }
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def run_asic_remote_power_cycles(
    targets: list[dict[str, Any]],
    reason: str,
    state: dict[str, Any],
    now: int,
) -> dict[str, Any]:
    power_cycle_at = (
        state.get("last_asic_power_cycle_at_by_identity")
        if isinstance(state.get("last_asic_power_cycle_at_by_identity"), dict)
        else {}
    )
    results: list[dict[str, Any]] = []
    runnable: list[tuple[dict[str, Any], str, str]] = []
    for item in targets:
        identity = miner_stall_identity_key(item)
        command_template = asic_power_cycle_command_for(item)
        base = {
            "identity_key": identity,
            "ip": item.get("ip"),
            "mac": item.get("mac"),
        }
        if not command_template:
            results.append({**base, "status": "not_configured", "action": "remote-power-cycle"})
            continue
        last_at = int_or_none(power_cycle_at.get(identity)) or 0
        cooldown_remaining = DEFAULT_ASIC_REMOTE_POWER_CYCLE_COOLDOWN_SECONDS - (now - last_at)
        if cooldown_remaining > 0:
            results.append(
                {
                    **base,
                    "status": "skipped",
                    "action": "remote-power-cycle",
                    "reason": "cooldown",
                    "cooldown_remaining_seconds": cooldown_remaining,
                }
            )
            continue
        runnable.append((item, identity, render_asic_power_cycle_command(command_template, item, reason)))

    if not runnable:
        status = "not_configured" if results and all(item["status"] == "not_configured" for item in results) else "skipped"
        payload = {
            "status": status,
            "reason": reason,
            "target_count": len(targets),
            "results": results,
            "updated_at": now_iso(),
        }
        state["last_asic_power_cycle"] = payload
        state["last_asic_power_cycle_at_by_identity"] = power_cycle_at
        return payload

    target_label = ",".join(identity for _item, identity, _command in runnable if identity) or "asic-miners"
    if not automation_mutation_allowed(
        actor="watchdog",
        action=automation_control.ACTION_ASIC_POWER_CYCLE,
        target=target_label,
        reason=reason,
        log=log,
        incident_source="watchdog",
    ):
        payload = {
            "status": "suppressed",
            "reason": "automation control denied ASIC power-cycle",
            "target_count": len(targets),
            "results": [
                *results,
                *[
                    {
                        "identity_key": identity,
                        "ip": item.get("ip"),
                        "mac": item.get("mac"),
                        "status": "suppressed",
                        "action": "remote-power-cycle",
                    }
                    for item, identity, _command in runnable
                ],
            ],
            "updated_at": now_iso(),
        }
        state["last_asic_power_cycle"] = payload
        state["last_asic_power_cycle_at_by_identity"] = power_cycle_at
        return payload

    lock_handle = acquire_lock(blocking=False)
    if lock_handle is None:
        payload = {
            "status": "skipped",
            "reason": "another repair is running",
            "target_count": len(targets),
            "results": [
                *results,
                *[
                    {
                        "identity_key": identity,
                        "ip": item.get("ip"),
                        "mac": item.get("mac"),
                        "status": "skipped",
                        "action": "remote-power-cycle",
                        "reason": "another repair is running",
                    }
                    for item, identity, _command in runnable
                ],
            ],
            "updated_at": now_iso(),
        }
        state["last_asic_power_cycle"] = payload
        state["last_asic_power_cycle_at_by_identity"] = power_cycle_at
        return payload

    started = time.time()
    action_name = "power-cycle-asic"
    log_path = action_log_path(action_name)
    state_payload = {
        "name": action_name,
        "mode": "power-cycle-asic",
        "reason": reason,
        "targets": [item.get("ip") or identity for item, identity, _command in runnable],
        "status": "running",
        "started_at": now_iso(),
        "finished_at": None,
        "log_path": str(log_path),
    }
    write_action_state(state_payload)
    log(f"starting ASIC remote power-cycle targets={state_payload['targets']} reason={reason}; log={log_path}")
    try:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] ASIC remote power-cycle reason: {reason}\n")
            for item, identity, command in runnable:
                result = {
                    "identity_key": identity,
                    "ip": item.get("ip"),
                    "mac": item.get("mac"),
                    "status": "running",
                    "action": "remote-power-cycle",
                }
                handle.write(json.dumps({**result, "command": command}, default=str) + "\n")
                handle.flush()
                command_result = run_logged(
                    ["/bin/sh", "-c", command],
                    log_path,
                    timeout=DEFAULT_ASIC_REMOTE_POWER_CYCLE_TIMEOUT_SECONDS,
                )
                result["status"] = "ok" if command_result.ok else "failed"
                result["returncode"] = command_result.returncode
                result["elapsed"] = command_result.elapsed
                if command_result.ok and identity:
                    power_cycle_at[identity] = now
                results.append(result)
    finally:
        lock_handle.close()

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
    payload = {
        "status": state_payload["status"],
        "reason": reason,
        "target_count": len(targets),
        "results": results,
        "updated_at": now_iso(),
        "log_path": str(log_path),
    }
    state["last_asic_power_cycle"] = payload
    state["last_asic_power_cycle_at_by_identity"] = power_cycle_at
    if failed:
        record_failed_repair("ASIC remote power-cycle", reason, {"failed": failed, "log_path": str(log_path)})
    else:
        record_efficiency_event("asic_remote_power_cycle", "warning", reason, {"results": results})
    return payload


def choose_lagging_node(status: dict[str, Any]) -> str | None:
    nodes = status.get("nodes", {}) or {}
    sync_health = status.get("sync_health", {}) or {}
    import_stale_seconds = int(sync_health.get("import_stale_seconds") or 180)
    latest_values = [
        node_sync_height(status, node)
        for node in NODES
        if node_sync_height(status, node) > 0
    ]
    max_latest = max(latest_values) if latest_values else 0
    candidates: list[tuple[int, str]] = []
    for node in NODES:
        node_info = nodes.get(node, {}) or {}
        progress = sync_progress_for_node(status, node)
        lag = int(progress.get("remaining_blocks") or node_info.get("peer_ahead_blocks") or 0)
        latest = node_sync_height(status, node)
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
    height_changed_at = (
        state.get("last_sync_height_changed_at_by_node")
        if state is not None and isinstance(state.get("last_sync_height_changed_at_by_node"), dict)
        else {}
    )
    current_time = int(time.time()) if now is None else now
    active: list[str] = []
    for node in NODES:
        info = nodes.get(node, {}) if isinstance(nodes.get(node), dict) else {}
        if not node_running_for_sync(status, node, info):
            continue
        progress = sync_progress_for_node(status, node)
        latest = node_sync_height(status, node)
        raw_age = info.get("last_import_age_seconds")
        import_age: int | None = None
        if raw_age is not None:
            try:
                import_age = int(float(raw_age))
            except (TypeError, ValueError):
                import_age = None
        if info.get("importing") and latest > 0 and (import_age is None or import_age <= grace_seconds):
            active.append(node)
            continue
        changed_at = int(height_changed_at.get(node) or 0)
        if latest > 0 and changed_at and current_time - changed_at <= grace_seconds:
            active.append(node)
            continue
        if import_age is None or latest <= 0:
            continue
        if import_age <= grace_seconds:
            active.append(node)
    return active


def peer_lead_active_import_suppression(
    status: dict[str, Any],
    state: dict[str, Any],
    now: int,
    target_node: str,
    evidence: dict[str, Any],
) -> tuple[bool, list[str], dict[str, Any]]:
    active_nodes = active_sync_import_nodes(status, state=state, now=now)
    if target_node not in active_nodes:
        return False, active_nodes, {"active_import": False}

    lead = int_or_none(evidence.get("lead"))
    tolerance = int_or_none(evidence.get("tolerance")) or 10
    max_lead = max(DEFAULT_NODE_PEER_LEAD_ACTIVE_IMPORT_MAX_LEAD_BLOCKS, tolerance * 6)
    worsen_blocks = max(DEFAULT_NODE_PEER_LEAD_ACTIVE_IMPORT_WORSEN_BLOCKS, tolerance * 5)

    by_node = state.get("node_peer_lead_active_import_by_node")
    if not isinstance(by_node, dict):
        by_node = {}
    row = by_node.get(target_node) if isinstance(by_node.get(target_node), dict) else {}
    since = int_or_none(row.get("since")) or now
    first_lead = int_or_none(row.get("first_lead"))
    if first_lead is None:
        first_lead = lead
    best_lead = int_or_none(row.get("best_lead"))
    if lead is not None:
        best_lead = lead if best_lead is None else min(best_lead, lead)
    worst_lead = int_or_none(row.get("worst_lead"))
    if lead is not None:
        worst_lead = lead if worst_lead is None else max(worst_lead, lead)

    age = max(0, now - since)
    lead_over_hard_limit = bool(lead is not None and lead > max_lead)
    worsened_from_best = bool(lead is not None and best_lead is not None and lead - best_lead >= worsen_blocks)
    worsened_from_first = bool(lead is not None and first_lead is not None and lead - first_lead >= worsen_blocks)
    too_long = age >= DEFAULT_NODE_PEER_LEAD_ACTIVE_IMPORT_SUPPRESS_SECONDS
    suppression_expired = bool(
        too_long
        or (
            age >= DEFAULT_NODE_PEER_LEAD_STALL_CONFIRM_SECONDS
            and (lead_over_hard_limit or worsened_from_best or worsened_from_first)
        )
    )

    by_node[target_node] = {
        "since": since,
        "last_seen": now,
        "first_lead": first_lead,
        "last_lead": lead,
        "best_lead": best_lead,
        "worst_lead": worst_lead,
        "tolerance": tolerance,
        "max_lead": max_lead,
        "worsen_blocks": worsen_blocks,
        "age_seconds": age,
        "suppression_expired": suppression_expired,
        "too_long": too_long,
        "lead_over_hard_limit": lead_over_hard_limit,
        "worsened_from_best": worsened_from_best,
        "worsened_from_first": worsened_from_first,
    }
    state["node_peer_lead_active_import_by_node"] = by_node
    details = dict(by_node[target_node])
    details["active_import"] = True
    details["active_nodes"] = active_nodes
    return not suppression_expired, active_nodes, details


def observe_sync_progress(status: dict[str, Any], state: dict[str, Any], now: int) -> None:
    nodes = status.get("nodes", {}) if isinstance(status.get("nodes"), dict) else {}
    previous = state.get("last_sync_height_by_node") if isinstance(state.get("last_sync_height_by_node"), dict) else {}
    changed_at = (
        state.get("last_sync_height_changed_at_by_node")
        if isinstance(state.get("last_sync_height_changed_at_by_node"), dict)
        else {}
    )
    observed: dict[str, int] = {}
    updated_changed_at = dict(changed_at)
    for node in NODES:
        height = node_sync_height(status, node)
        observed[node] = height
        previous_height = int(previous.get(node) or 0)
        if previous_height > 0 and height > previous_height:
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

    state["last_sync_repair_suppressed_epoch"] = int(time.time())
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


def dag_tip_damage_nodes(status: dict[str, Any]) -> list[str]:
    nodes = status.get("nodes", {}) if isinstance(status.get("nodes"), dict) else {}
    damaged: list[str] = []
    for node in NODES:
        info = nodes.get(node, {}) if isinstance(nodes.get(node), dict) else {}
        if info.get("dag_tip_damage"):
            damaged.append(node)
    return damaged


def should_cleanup_dag_tips(state: dict[str, Any], node: str, cooldown: int | None = None) -> bool:
    cooldown_seconds = DEFAULT_NODE_DAG_TIP_CLEANUP_COOLDOWN if cooldown is None else cooldown
    by_node = (
        state.get("last_node_dag_tip_cleanup_at_by_node")
        if isinstance(state.get("last_node_dag_tip_cleanup_at_by_node"), dict)
        else {}
    )
    now = int(time.time())
    return now - int(by_node.get(node, 0) or 0) >= cooldown_seconds


def mark_dag_tip_cleanup_attempt(state: dict[str, Any], node: str) -> None:
    by_node = (
        state.get("last_node_dag_tip_cleanup_at_by_node")
        if isinstance(state.get("last_node_dag_tip_cleanup_at_by_node"), dict)
        else {}
    )
    updated = dict(by_node)
    updated[node] = int(time.time())
    state["last_node_dag_tip_cleanup_at_by_node"] = updated
    state["last_node_dag_tip_cleanup_node"] = node
    state["last_node_dag_tip_cleanup_at"] = now_iso()


def should_clean_restore(state: dict[str, Any], status: dict[str, Any], threshold: int, cooldown: int) -> bool:
    if not AUTOMATIC_CLEAN_RESTORE_ENABLED:
        return False
    if state.get("consecutive_failures", 0) < threshold:
        return False

    now = int(time.time())
    if now - int(state.get("last_clean_restore_at", 0) or 0) < cooldown:
        return False

    failures = status.get("failures") if isinstance(status.get("failures"), list) else []
    hard_failure = any("critical log entries" in str(item) or "bdag child is not running" in str(item) for item in failures)
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
            collect_stack_status(include_logs=True)
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
        boot_status = collect_stack_status(include_logs=True)
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
    status = collect_stack_status(include_logs=True)
    status_failures = status.get("failures") if isinstance(status.get("failures"), list) else []
    stack_failures = status.get("stack_failures") if isinstance(status.get("stack_failures"), list) else status_failures
    miner_failures = status.get("miner_failures") if isinstance(status.get("miner_failures"), list) else []
    failures = stack_failures + miner_failures
    status_overall = str(status.get("overall") or status.get("status") or "unknown")
    status_warnings = (
        status.get("warnings")
        if isinstance(status.get("warnings"), list)
        else status.get("degraded_reasons")
        if isinstance(status.get("degraded_reasons"), list)
        else []
    )
    pool_health = status.get("pool_health", status.get("pool", {}))
    pool_metrics = status.get("pool_metrics") if isinstance(status.get("pool_metrics"), dict) else {}
    if not pool_metrics and isinstance(pool_health, dict) and isinstance(pool_health.get("metrics"), dict):
        pool_metrics = pool_health.get("metrics")
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
    stratum_no_request_total = int_or_none(pool_metrics.get("stratum_no_request_disconnects_total")) or 0
    previous_no_request_total = int_or_none(state.get("last_stratum_no_request_disconnects_total"))
    if previous_no_request_total is None or previous_no_request_total > stratum_no_request_total:
        stratum_no_request_delta = 0
    else:
        stratum_no_request_delta = stratum_no_request_total - previous_no_request_total
    state["last_stratum_no_request_disconnects_total"] = stratum_no_request_total
    state["last_stratum_no_request_disconnects_delta"] = stratum_no_request_delta
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
    connected_miner_count = int_or_none(
        miner_health.get("connected_count_effective") or miner_health.get("connected_count")
    ) or 0
    expired_job_reconnect_failed = bool(
        pool_health.get("expired_job_reconnect_failed_no_share")
        and connected_miner_count == 0
    )
    submit_path_recovery_recent = bool(pool_health.get("submit_stall_recovery_recent"))
    submit_path_self_healed_recently = bool(pool_health.get("submit_stall_self_healed_recently"))
    submit_path_recovery_age = pool_health.get("submit_stall_last_recovery_age_seconds")
    low_diff_asics = low_difficulty_primary_miners(status)
    api_stall_asics = asic_api_stall_primary_miners(status, DEFAULT_ASIC_API_STALL_STALE_SECONDS)
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
    stratum_no_request_by_reason = (
        pool_metrics.get("stratum_no_request_disconnects")
        if isinstance(pool_metrics.get("stratum_no_request_disconnects"), dict)
        else {}
    )
    stratum_no_request_has_mac_source = any(
        str(key).split(":", 1)[0] == "mac" and (int_or_none(value) or 0) > 0
        for key, value in stratum_no_request_by_reason.items()
    )
    if (
        stratum_no_request_delta >= DEFAULT_STRATUM_NO_REQUEST_EVENT_THRESHOLD
        and (primary_miner_count > 0 or stratum_no_request_has_mac_source)
    ):
        message = (
            f"Stratum accepted {stratum_no_request_delta} connection(s) since the last watchdog sample "
            "that closed before any mining.subscribe or mining.authorize request"
        )
        state["last_stratum_no_request_warning"] = {
            "at": now_iso(),
            "delta": stratum_no_request_delta,
            "total": stratum_no_request_total,
            "by_reason": stratum_no_request_by_reason,
        }
        log(f"stratum_no_request_disconnects delta={stratum_no_request_delta} total={stratum_no_request_total}")
        record_efficiency_event(
            "stratum_no_request_disconnects",
            "warning",
            message,
            {
                "delta": stratum_no_request_delta,
                "total": stratum_no_request_total,
                "by_reason": stratum_no_request_by_reason,
                "mac_source": stratum_no_request_has_mac_source,
                "primary_miner_count": primary_miner_count,
            },
        )
    template_nodes = template_failing_nodes(status)
    orphan_nodes = orphan_storm_nodes(status)
    dag_tip_nodes = dag_tip_damage_nodes(status)
    pool_start_blocked, pool_start_blocked_reason = pool_start_blocked_by_status(status)
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
    docker_access_error = status.get("docker_access_error")
    autonomous_lab_active = lock_is_held(AUTONOMOUS_STACK_LAB_LOCK_FILE)
    refresh_maintenance_state(state, autonomous_lab_active)
    triage = build_mining_health_triage(
        status=status,
        now=now,
        stack_failures=stack_failures,
        miner_failures=miner_failures,
        failures=failures,
        pool_health=pool_health,
        miner_health=miner_health,
        miner_rows=miner_rows,
        mining_address=mining_address,
        down_miners=down_miners,
        down_ips=sorted(down_ips),
        pool_started_age_seconds=pool_started_age_seconds,
        pool_in_startup_grace=pool_in_startup_grace,
        share_stall=share_stall,
        pool_template_frozen=pool_template_frozen,
        duplicate_block_storm=duplicate_block_storm,
        submit_path_zero_success_storm=submit_path_zero_success_storm,
        accepted_job_expired_storm=accepted_job_expired_storm,
        expired_job_reconnect_failed=expired_job_reconnect_failed,
        submit_path_recovery_recent=submit_path_recovery_recent,
        submit_path_self_healed_recently=submit_path_self_healed_recently,
        submit_path_recovery_age=submit_path_recovery_age,
        low_diff_asics=low_diff_asics,
        api_stall_asics=api_stall_asics,
        useful_work_stalled_asics=useful_work_stalled_asics,
        hashrate_issue_asics=hashrate_issue_asics,
        degraded_asics=degraded_asics,
        primary_miner_count=primary_miner_count,
        template_nodes=template_nodes,
        orphan_nodes=orphan_nodes,
        dag_tip_nodes=dag_tip_nodes,
        pool_start_blocked=pool_start_blocked,
        pool_start_blocked_reason=pool_start_blocked_reason,
        docker_access_error=docker_access_error,
    )
    stack_failures = triage["stack_failures"]
    miner_failures = triage["miner_failures"]
    failures = triage["failures"]
    pool_health = triage["pool_health"]
    miner_health = triage["miner_health"]
    miner_rows = triage["miner_rows"]
    mining_address = triage["mining_address"]
    down_miners = triage["down_miners"]
    down_ips = set(triage["down_ips"])
    pool_started_age_seconds = triage["pool_started_age_seconds"]
    pool_in_startup_grace = triage["pool_in_startup_grace"]
    share_stall = triage["share_stall"]
    pool_template_frozen = triage["pool_template_frozen"]
    duplicate_block_storm = triage["duplicate_block_storm"]
    submit_path_zero_success_storm = triage["submit_path_zero_success_storm"]
    accepted_job_expired_storm = triage["accepted_job_expired_storm"]
    expired_job_reconnect_failed = triage["expired_job_reconnect_failed"]
    submit_path_recovery_recent = triage["submit_path_recovery_recent"]
    submit_path_self_healed_recently = triage["submit_path_self_healed_recently"]
    submit_path_recovery_age = triage["submit_path_recovery_age"]
    low_diff_asics = triage["low_diff_asics"]
    api_stall_asics = triage["api_stall_asics"]
    useful_work_stalled_asics = triage["useful_work_stalled_asics"]
    hashrate_issue_asics = triage["hashrate_issue_asics"]
    degraded_asics = triage["degraded_asics"]
    primary_miner_count = triage["primary_miner_count"]
    template_nodes = triage["template_nodes"]
    orphan_nodes = triage["orphan_nodes"]
    dag_tip_nodes = triage["dag_tip_nodes"]
    docker_access_error = triage["docker_access_error"]
    stratum_no_request_pressure = bool(
        stratum_no_request_delta >= DEFAULT_STRATUM_NO_REQUEST_EVENT_THRESHOLD
        and (primary_miner_count > 0 or stratum_no_request_has_mac_source)
    )
    if stratum_no_request_pressure:
        for item in api_stall_asics:
            if not asic_has_recent_useful_work(item):
                item["api_stall_no_request_pressure"] = True
    useful_work_stall_since = update_useful_work_stall_since(
        state,
        useful_work_stalled_asics,
        degraded_asics,
        now,
    )
    asic_api_stall_since = update_asic_api_stall_since(state, api_stall_asics, now)
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

    last_earnings_snapshot_epoch = int(state.get("last_earnings_snapshot_epoch", 0) or 0)
    if now - last_earnings_snapshot_epoch >= DEFAULT_EARNINGS_SNAPSHOT_INTERVAL_SECONDS:
        try:
            snapshot = record_earnings_snapshot()
            state["last_earnings_snapshot_at"] = snapshot.get("generated_at")
            state["last_earnings_snapshot_epoch"] = now
        except Exception as exc:  # noqa: BLE001 - earnings logging should not stop repairs.
            log(f"earnings snapshot failed: {exc}")

    if triage["docker_access_error"]:
        failure = f"docker access unavailable: {triage['docker_access_error']}"
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

    node_rpc_refused = node_rpc_refused_evidence(status)
    rpc_pool_pending = (
        state.get("node_rpc_refused_pool_restart_pending")
        if isinstance(state.get("node_rpc_refused_pool_restart_pending"), dict)
        else {}
    )
    if rpc_pool_pending and not node_rpc_refused.get("active"):
        pending_since = int_or_none(rpc_pool_pending.get("since")) or now
        grace_remaining = DEFAULT_NODE_RPC_REFUSED_POOL_RESTART_GRACE_SECONDS - (now - pending_since)
        if repair and grace_remaining <= 0 and pool_needs_restart_after_node_rpc_recovery(status):
            reason = (
                "pool still has miners without current work after node RPC transport recovered "
                f"(prior evidence={rpc_pool_pending.get('evidence') or 'unknown'})"
            )
            ok = run_pool_restart(reason)
            state["last_repair_at"] = int(time.time())
            state["last_pool_repair_at"] = int(time.time())
            state["last_node_rpc_refused_pool_restart"] = {"ok": ok, "reason": reason, "at": now_iso()}
            if ok:
                state.pop("node_rpc_refused_pool_restart_pending", None)
                state["consecutive_syncing"] = 0
                state["last_status"] = "pool_restarted_after_node_rpc_refused"
                state["last_sync_warnings"] = [reason]
                state["updated_at"] = now_iso()
                write_state(state)
                return {"status": status, "watchdog_state": state}
        elif pool_needs_restart_after_node_rpc_recovery(status):
            state["last_status"] = "node_rpc_refused_pool_restart_pending"
            state["last_sync_warnings"] = [
                "waiting to restart pool after node RPC recovery "
                f"grace_remaining={max(grace_remaining, 0)}s"
            ]
            state["updated_at"] = now_iso()
            write_state(state)
            return {"status": status, "watchdog_state": state}
        else:
            state.pop("node_rpc_refused_pool_restart_pending", None)

    if node_rpc_refused.get("active") and container_running(status, NODES[0] if NODES else "node"):
        since = int_or_none(state.get("node_rpc_refused_since")) or now
        state["node_rpc_refused_since"] = since
        refused_for = now - since
        last_restart = int_or_none(state.get("last_node_rpc_refused_restart_at")) or 0
        cooldown_remaining = DEFAULT_NODE_RPC_REFUSED_REPAIR_COOLDOWN - (now - last_restart)
        node_service = NODES[0] if NODES else "node"
        node_age = container_started_age_seconds(status, node_service, now)
        startup_remaining = (
            DEFAULT_NODE_RPC_REFUSED_CONFIRM_SECONDS - node_age
            if node_age is not None and node_age < DEFAULT_NODE_RPC_REFUSED_CONFIRM_SECONDS
            else 0
        )
        reason = (
            "node RPC refused pool template requests or reported transport failure "
            f"for {refused_for}s"
        )
        state["consecutive_failures"] = 0
        state["consecutive_syncing"] = int(state.get("consecutive_syncing", 0) or 0) + 1
        state["consecutive_share_stalls"] = 0
        state["last_status"] = "node_rpc_refused"
        state["last_failures"] = []
        state["last_sync_warnings"] = [reason]
        state["last_node_rpc_refused_evidence"] = node_rpc_refused
        log(
            "node_rpc_refused "
            f"refused_for={refused_for}s cooldown_remaining={max(cooldown_remaining, 0)}s "
            f"startup_remaining={max(startup_remaining, 0)}s evidence={node_rpc_refused}"
        )
        record_efficiency_event(
            "node_rpc_refused",
            "critical",
            reason,
            {
                "evidence": node_rpc_refused,
                "refused_for_seconds": refused_for,
                "cooldown_remaining_seconds": max(cooldown_remaining, 0),
                "startup_remaining_seconds": max(startup_remaining, 0),
            },
        )
        if (
            repair
            and refused_for >= DEFAULT_NODE_RPC_REFUSED_CONFIRM_SECONDS
            and cooldown_remaining <= 0
            and startup_remaining <= 0
        ):
            ok = run_node_restart(node_service, reason)
            state["last_repair_at"] = int(time.time())
            state["last_sync_repair_at"] = int(time.time())
            state["last_node_rpc_refused_restart_at"] = now
            state["node_rpc_refused_pool_restart_pending"] = {
                "since": now,
                "node": node_service,
                "evidence": node_rpc_refused,
            }
            if ok:
                state["consecutive_syncing"] = 0
        state["updated_at"] = now_iso()
        write_state(state)
        return {"status": status, "watchdog_state": state}
    else:
        state.pop("node_rpc_refused_since", None)

    peer_lead_stall = selected_backend_peer_lead_stall_evidence(status)
    primary_node = primary_node_name()
    if peer_lead_stall.get("active") and container_running(status, primary_node):
        since = int_or_none(state.get("node_peer_lead_stall_since")) or now
        state["node_peer_lead_stall_since"] = since
        stalled_for = now - since
        hard_mining_outage = peer_lead_hard_mining_outage(status, peer_lead_stall)
        recent_mining_work = pool_has_recent_mining_work(
            status,
            DEFAULT_NODE_PEER_LEAD_HARD_STALL_RECENT_WORK_SECONDS if hard_mining_outage else 60,
        )
        restart_node = choose_lagging_node(status) or primary_node
        active_import_suppresses, active_import_nodes, active_import_details = peer_lead_active_import_suppression(
            status,
            state,
            now,
            restart_node,
            peer_lead_stall,
        )
        active_import = bool(active_import_details.get("active_import"))
        active_import_can_wait = hard_peer_lead_outage_allows_active_import_wait(
            peer_lead_stall,
            active_import_details,
        )
        effective_active_import_suppresses = active_import_suppresses and (
            not hard_mining_outage or active_import_can_wait
        )
        confirm_seconds = (
            DEFAULT_NODE_PEER_LEAD_HARD_STALL_CONFIRM_SECONDS
            if hard_mining_outage
            else DEFAULT_NODE_PEER_LEAD_STALL_CONFIRM_SECONDS
        )
        startup_grace_seconds = max(confirm_seconds, DEFAULT_NODE_PEER_LEAD_STALL_CONFIRM_SECONDS)
        last_restart = int_or_none(state.get("last_node_peer_lead_stall_restart_at")) or 0
        base_repair_cooldown = (
            DEFAULT_NODE_PEER_LEAD_HARD_STALL_REPAIR_COOLDOWN
            if hard_mining_outage
            else DEFAULT_NODE_PEER_LEAD_STALL_REPAIR_COOLDOWN
        )
        retry_pool_job_age = float_or_none(peer_lead_stall.get("pool_job_age_seconds"))
        retry_template_age = float_or_none(peer_lead_stall.get("template_age_seconds"))
        retry_stale_work = bool(
            (
                retry_pool_job_age is not None
                and retry_pool_job_age >= DEFAULT_NODE_PEER_LEAD_HARD_STALL_JOB_AGE_SECONDS
            )
            or (
                retry_template_age is not None
                and retry_template_age >= DEFAULT_NODE_PEER_LEAD_HARD_STALL_TEMPLATE_AGE_SECONDS
            )
        )
        retry_cooldown_applies = bool(
            hard_mining_outage
            and not recent_mining_work
            and last_restart > 0
            and int_or_none(peer_lead_stall.get("ready_miners")) == 0
            and retry_stale_work
        )
        repair_cooldown = (
            min(base_repair_cooldown, DEFAULT_NODE_PEER_LEAD_HARD_STALL_RETRY_COOLDOWN)
            if retry_cooldown_applies
            else base_repair_cooldown
        )
        cooldown_remaining = repair_cooldown - (now - last_restart)
        node_age = container_started_age_seconds(status, restart_node, now)
        startup_remaining = (
            startup_grace_seconds - node_age
            if node_age is not None and node_age < startup_grace_seconds
            else 0
        )
        reason = (
            "selected pool backend has a hard peer-lead mining outage "
            if hard_mining_outage
            else "selected pool backend has a sustained peer-lead mining stall "
        ) + (
            f"for {stalled_for}s "
            f"(lead={peer_lead_stall.get('lead')} "
            f"tolerance={peer_lead_stall.get('tolerance')} "
            f"p2p_fresh={peer_lead_stall.get('p2p_mining_fresh')} "
            f"fresh_peers={peer_lead_stall.get('fresh_consensus_peer_count')} "
            f"peer_starvation={peer_lead_stall.get('peer_starvation')} "
            f"submit_ready={peer_lead_stall.get('submit_ready')} "
            f"mineable={peer_lead_stall.get('mineable')} "
            f"ready_miners={peer_lead_stall.get('ready_miners')})"
        )
        if recent_mining_work or effective_active_import_suppresses:
            state["consecutive_syncing"] = 0
        else:
            state["consecutive_syncing"] = int(state.get("consecutive_syncing", 0) or 0) + 1
        state["consecutive_failures"] = 0
        state["consecutive_share_stalls"] = 0
        state["last_status"] = (
            "node_peer_lead_stall_observing"
            if recent_mining_work or effective_active_import_suppresses
            else "node_peer_lead_stall"
        )
        state["last_failures"] = []
        state["last_sync_warnings"] = [reason]
        state["last_share_warnings"] = []
        state["last_node_peer_lead_stall_evidence"] = peer_lead_stall
        state["last_node_peer_lead_hard_mining_outage"] = hard_mining_outage
        log(
            "node_peer_lead_stall "
            f"stalled_for={stalled_for}s recent_mining_work={recent_mining_work} "
            f"hard_mining_outage={hard_mining_outage} confirm_seconds={confirm_seconds} "
            f"startup_grace_seconds={startup_grace_seconds} "
            f"active_import={active_import} active_import_suppresses={active_import_suppresses} "
            f"active_import_can_wait={active_import_can_wait} "
            f"active_import_nodes={active_import_nodes} "
            f"repair_cooldown={repair_cooldown}s "
            f"retry_cooldown_applies={retry_cooldown_applies} "
            f"retry_stale_work={retry_stale_work} "
            f"cooldown_remaining={max(cooldown_remaining, 0)}s "
            f"startup_remaining={max(startup_remaining, 0)}s "
            f"active_import_details={active_import_details} evidence={peer_lead_stall}"
        )
        record_efficiency_event(
            "node_peer_lead_hard_mining_outage" if hard_mining_outage else "node_peer_lead_stall",
            "warning" if recent_mining_work or effective_active_import_suppresses else "critical",
            reason,
            {
                "evidence": peer_lead_stall,
                "stalled_for_seconds": stalled_for,
                "recent_mining_work": recent_mining_work,
                "hard_mining_outage": hard_mining_outage,
                "confirm_seconds": confirm_seconds,
                "startup_grace_seconds": startup_grace_seconds,
                "active_import_nodes": active_import_nodes,
                "active_import_suppresses": effective_active_import_suppresses,
                "raw_active_import_suppresses": active_import_suppresses,
                "active_import_details": active_import_details,
                "active_import_can_wait": active_import_can_wait,
                "restart_node": restart_node,
                "cooldown_remaining_seconds": max(cooldown_remaining, 0),
                "repair_cooldown_seconds": repair_cooldown,
                "base_repair_cooldown_seconds": base_repair_cooldown,
                "retry_cooldown_applies": retry_cooldown_applies,
                "retry_stale_work": retry_stale_work,
                "startup_remaining_seconds": max(startup_remaining, 0),
            },
        )
        if repair and recent_mining_work:
            log("peer-lead stall repair suppressed because paid block submission is fresh")
            record_efficiency_event(
                "repair_suppressed",
                "warning",
                "peer-lead stall repair suppressed because paid block submission is fresh",
                {
                    "evidence": peer_lead_stall,
                    "freshness_seconds": DEFAULT_NODE_PEER_LEAD_HARD_STALL_RECENT_WORK_SECONDS
                    if hard_mining_outage
                    else 60,
                },
            )
        elif repair and effective_active_import_suppresses:
            log(
                "peer-lead stall repair suppressed while block import is active "
                f"target={restart_node} active_nodes={','.join(active_import_nodes)} details={active_import_details}"
            )
            record_efficiency_event(
                "repair_suppressed",
                "warning",
                "peer-lead stall repair suppressed while block import is active",
                {
                    "evidence": peer_lead_stall,
                    "active_nodes": active_import_nodes,
                    "target_node": restart_node,
                    "active_import_details": active_import_details,
                    "active_import_can_wait": active_import_can_wait,
                },
            )
        elif (
            repair
            and stalled_for >= confirm_seconds
            and cooldown_remaining <= 0
            and startup_remaining <= 0
        ):
            restart_prefix = "hard peer-lead mining outage: " if hard_mining_outage else "peer-lead exceeds tolerance: "
            if hard_mining_outage and peer_lead_stall.get("peer_starvation"):
                restart_prefix = "hard peer-starvation mining outage: "
            ok = run_node_restart(restart_node, restart_prefix + reason)
            state["last_repair_at"] = int(time.time())
            state["last_sync_repair_at"] = int(time.time())
            state["last_node_peer_lead_stall_restart_at"] = now
            state["last_node_peer_lead_stall_restart"] = {
                "node": restart_node,
                "ok": ok,
                "reason": reason,
                "at": now_iso(),
                "evidence": peer_lead_stall,
            }
            if ok:
                state["consecutive_syncing"] = 0
                state.pop("node_peer_lead_stall_since", None)
        state["updated_at"] = now_iso()
        write_state(state)
        return {"status": status, "watchdog_state": state}
    else:
        state.pop("node_peer_lead_stall_since", None)
        state.pop("node_peer_lead_active_import_by_node", None)

    template_sync_wedge = selected_backend_template_sync_wedge_evidence(status)
    primary_node = primary_node_name()
    if template_sync_wedge.get("active") and container_running(status, primary_node):
        since = int_or_none(state.get("node_template_sync_wedge_since")) or now
        state["node_template_sync_wedge_since"] = since
        wedged_for = now - since
        hard_mining_outage = template_sync_wedge_hard_mining_outage(status, template_sync_wedge)
        recent_mining_work = pool_has_recent_mining_work(
            status,
            DEFAULT_NODE_PEER_LEAD_HARD_STALL_RECENT_WORK_SECONDS if hard_mining_outage else 60,
        )
        restart_node = primary_node
        active_import_nodes = active_sync_import_nodes(status, state=state, now=now)
        active_import = restart_node in active_import_nodes
        effective_active_import_suppresses = active_import and not hard_mining_outage
        confirm_seconds = DEFAULT_NODE_TEMPLATE_SYNC_WEDGE_CONFIRM_SECONDS
        last_restart = int_or_none(state.get("last_node_template_sync_wedge_restart_at")) or 0
        cooldown_remaining = DEFAULT_NODE_TEMPLATE_SYNC_WEDGE_REPAIR_COOLDOWN - (now - last_restart)
        node_age = container_started_age_seconds(status, restart_node, now)
        startup_remaining = (
            confirm_seconds - node_age
            if node_age is not None and node_age < confirm_seconds
            else 0
        )
        reason = (
            "selected pool backend has a native-current template-sync mining wedge "
            f"for {wedged_for}s "
            f"(p2p_fresh={template_sync_wedge.get('p2p_mining_fresh')} "
            f"lead={template_sync_wedge.get('lead')} "
            f"tolerance={template_sync_wedge.get('tolerance')} "
            f"fresh_peers={template_sync_wedge.get('fresh_consensus_peer_count')} "
            f"submit_ready={template_sync_wedge.get('submit_ready')} "
            f"mineable={template_sync_wedge.get('mineable')} "
            f"gbt_ready={template_sync_wedge.get('get_block_template_ready')} "
            f"template_age={template_sync_wedge.get('template_age_seconds')} "
            f"ready_miners={template_sync_wedge.get('ready_miners')})"
        )
        if recent_mining_work or effective_active_import_suppresses:
            state["consecutive_syncing"] = 0
        else:
            state["consecutive_syncing"] = int(state.get("consecutive_syncing", 0) or 0) + 1
        state["consecutive_failures"] = 0
        state["consecutive_share_stalls"] = 0
        state["last_status"] = (
            "node_template_sync_wedge_observing"
            if recent_mining_work or effective_active_import_suppresses
            else "node_template_sync_wedge"
        )
        state["last_failures"] = []
        state["last_sync_warnings"] = [reason]
        state["last_share_warnings"] = []
        state["last_node_template_sync_wedge_evidence"] = template_sync_wedge
        state["last_node_template_sync_wedge_hard_mining_outage"] = hard_mining_outage
        log(
            "node_template_sync_wedge "
            f"wedged_for={wedged_for}s recent_mining_work={recent_mining_work} "
            f"active_import={active_import} active_import_suppresses={effective_active_import_suppresses} "
            f"active_import_nodes={active_import_nodes} "
            f"hard_mining_outage={hard_mining_outage} confirm_seconds={confirm_seconds} "
            f"cooldown_remaining={max(cooldown_remaining, 0)}s "
            f"startup_remaining={max(startup_remaining, 0)}s evidence={template_sync_wedge}"
        )
        record_efficiency_event(
            "node_template_sync_wedge_hard_mining_outage" if hard_mining_outage else "node_template_sync_wedge",
            "warning" if recent_mining_work or active_import else "critical",
            reason,
            {
                "evidence": template_sync_wedge,
                "wedged_for_seconds": wedged_for,
                "recent_mining_work": recent_mining_work,
                "active_import": active_import,
                "active_import_nodes": active_import_nodes,
                "active_import_suppresses": effective_active_import_suppresses,
                "raw_active_import": active_import,
                "hard_mining_outage": hard_mining_outage,
                "confirm_seconds": confirm_seconds,
                "restart_node": restart_node,
                "cooldown_remaining_seconds": max(cooldown_remaining, 0),
                "startup_remaining_seconds": max(startup_remaining, 0),
            },
        )
        if repair and recent_mining_work:
            log("template-sync wedge repair suppressed because paid block submission is fresh")
            record_efficiency_event(
                "repair_suppressed",
                "warning",
                "template-sync wedge repair suppressed because paid block submission is fresh",
                {"evidence": template_sync_wedge},
            )
        elif repair and effective_active_import_suppresses:
            log(
                "template-sync wedge repair suppressed while block import is active "
                f"target={restart_node} active_nodes={','.join(active_import_nodes)}"
            )
            record_efficiency_event(
                "repair_suppressed",
                "warning",
                "template-sync wedge repair suppressed while block import is active",
                {
                    "evidence": template_sync_wedge,
                    "active_nodes": active_import_nodes,
                    "target_node": restart_node,
                },
            )
        elif (
            repair
            and hard_mining_outage
            and wedged_for >= confirm_seconds
            and cooldown_remaining <= 0
            and startup_remaining <= 0
        ):
            ok = run_node_restart(restart_node, "native-current template-sync mining wedge: " + reason)
            state["last_repair_at"] = int(time.time())
            state["last_sync_repair_at"] = int(time.time())
            state["last_node_template_sync_wedge_restart_at"] = now
            state["last_node_template_sync_wedge_restart"] = {
                "node": restart_node,
                "ok": ok,
                "reason": reason,
                "at": now_iso(),
                "evidence": template_sync_wedge,
            }
            if ok:
                state["consecutive_syncing"] = 0
                state.pop("node_template_sync_wedge_since", None)
        state["updated_at"] = now_iso()
        write_state(state)
        return {"status": status, "watchdog_state": state}
    else:
        state.pop("node_template_sync_wedge_since", None)

    sync_pause_reason = sync_progress_pool_pause_reason(status)
    if sync_pause_reason and container_running(status, POOL_CONTAINER):
        recent_mining_work = pool_has_recent_mining_work(status)
        restart_node = choose_lagging_node(status) or primary_node_name()
        active_import_nodes = active_sync_import_nodes(status, state=state, now=now)
        active_import = restart_node in active_import_nodes
        if recent_mining_work or active_import:
            state["consecutive_syncing"] = 0
        else:
            state["consecutive_syncing"] = int(state.get("consecutive_syncing", 0) or 0) + 1
        state["consecutive_failures"] = 0
        state["consecutive_share_stalls"] = 0
        state["last_status"] = (
            "pool_sync_template_pause"
            if recent_mining_work or active_import
            else "pool_sync_template_pause_stalled"
        )
        state["last_failures"] = []
        state["last_sync_warnings"] = [sync_pause_reason]
        state["last_share_warnings"] = []
        state["last_pool_sync_pause_at"] = now_iso()
        state["last_pool_sync_pause_reason"] = sync_pause_reason
        state["last_pool_sync_pause_active_import_nodes"] = active_import_nodes
        state["last_pool_sync_pause_recent_mining_work"] = recent_mining_work
        state["last_pool_sync_pause_restart_candidate"] = restart_node
        state["last_pool_sync_pause_active"] = True
        log(
            "pool sync template pause active; leaving "
            f"{POOL_CONTAINER} running: {sync_pause_reason}; "
            f"active_import_nodes={','.join(active_import_nodes) or 'none'} "
            f"recent_mining_work={recent_mining_work} "
            f"consecutive_syncing={state['consecutive_syncing']}"
        )
        if (
            repair
            and state["consecutive_syncing"]
            and should_restart_for_syncing(state, syncing_threshold, syncing_restart_cooldown)
        ):
            if suppress_sync_restart_for_active_import(status, state, sync_pause_reason, restart_node):
                repair_attempted = False
            else:
                ok = run_node_restart(restart_node, "stalled catch-up during pool sync pause: " + sync_pause_reason)
                repair_attempted = True
            if repair_attempted:
                state["last_repair_at"] = int(time.time())
                state["last_sync_repair_at"] = int(time.time())
                state["last_pool_sync_pause_repair_at"] = now_iso()
                if ok:
                    state["consecutive_syncing"] = 0
        state["updated_at"] = now_iso()
        write_state(state)
        return {"status": status, "watchdog_state": state}

    if status_payload_has_tracking_gap(status):
        message = "tracked miner registry is empty while miner demand or ASIC LAN evidence is present"
        log(message)
        if repair and repair_missing_tracked_miners(status):
            state["last_miner_tracking_repair_at"] = now_iso()
            record_efficiency_event(
                "watchdog_repaired_tracked_miners",
                "critical",
                message,
                {"tracked_count_before": int(miner_health.get("tracked_count", 0) or 0)},
            )
        elif repair:
            record_failed_repair(
                "watchdog_repair_tracked_miners",
                message,
                {"tracked_count_before": int(miner_health.get("tracked_count", 0) or 0)},
            )

    if node_mining_template_support_should_repair(status):
        message = "miner demand exists but node miner/template support is disabled or missing miningaddr"
        log(message)
        if repair and repair_node_mining_template_support(status):
            state["last_node_mining_template_support_repair_at"] = now_iso()
            record_efficiency_event(
                "watchdog_enabled_node_mining_template_support",
                "critical",
                message,
                {"mode": status.get("mode"), "overall": status.get("overall")},
            )
        elif repair:
            record_failed_repair("watchdog_enable_node_mining_template_support", message)

    if stack_failures:
        if pool_start_blocked and pool_stopped_is_only_stack_failure(stack_failures):
            state["consecutive_failures"] = 0
            state["consecutive_syncing"] = 0
            state["last_status"] = "pool_start_blocked"
            state["last_failures"] = []
            state["last_sync_warnings"] = [pool_start_blocked_reason]
            log(
                "stack start suppressed for stopped pool: "
                f"{pool_start_blocked_reason}; failures={'; '.join(stack_failures)}"
            )
            record_efficiency_event(
                "pool_start_blocked",
                "warning",
                f"Watchdog left {POOL_CONTAINER} stopped: {pool_start_blocked_reason}",
                {"failures": stack_failures, "reason": pool_start_blocked_reason},
            )
        else:
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
        if repair and not (pool_start_blocked and pool_stopped_is_only_stack_failure(stack_failures)):
            dag_tip_node = dag_tip_nodes[0] if dag_tip_nodes else ""
            if dag_tip_node and should_cleanup_dag_tips(state, dag_tip_node):
                reason = (
                    "node DAG tips reference missing block data; running narrow --cleanuptips repair before restart: "
                    + "; ".join(stack_failures)
                )
                ok = run_node_dag_tip_cleanup(dag_tip_node, reason)
                state["last_repair_at"] = int(time.time())
                mark_dag_tip_cleanup_attempt(state, dag_tip_node)
                if ok:
                    state["consecutive_failures"] = 0
            elif dag_tip_node:
                log(f"node DAG tip cleanup for {dag_tip_node} suppressed by cooldown; failures={'; '.join(stack_failures)}")
                record_efficiency_event(
                    "repair_suppressed",
                    "warning",
                    f"node DAG tip cleanup for {dag_tip_node} suppressed by cooldown",
                    {"node": dag_tip_node, "failures": stack_failures},
                )
            elif should_clean_restore(state, status, threshold, clean_restore_cooldown):
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
    elif orphan_nodes:
        nodes = status.get("nodes", {}) if isinstance(status.get("nodes"), dict) else {}
        active_node = NODES[0] if NODES else ""
        target_nodes = orphan_nodes
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
            f"active_node={active_node or 'unknown'} affected={orphan_nodes} target={target_node}"
        )
        record_efficiency_event(
            "node_orphan_error_storm",
            "warning",
            reason,
            {
                "affected_nodes": orphan_nodes,
                "target_node": target_node,
                "active_node": active_node,
                "target_node_status": target_info,
            },
        )
        if repair:
            cooldown_remaining = DEFAULT_NODE_ORPHAN_STORM_RESTART_COOLDOWN - (
                now - int(node_orphan_restart_by_node.get(target_node, 0) or 0)
            )
            if autonomous_lab_active:
                log(f"node orphan storm repair for {target_node} suppressed during autonomous stack lab")
                record_efficiency_event(
                    "repair_suppressed",
                    "warning",
                    f"node orphan storm repair for {target_node} suppressed during autonomous stack lab",
                    {"reason": reason, "target_node": target_node},
                )
            elif pool_in_startup_grace and target_node == active_node:
                log(
                    "node orphan storm repair suppressed during pool startup grace for active node "
                    f"node={target_node} age={pool_started_age_seconds}s"
                )
                record_efficiency_event(
                    "repair_suppressed",
                    "warning",
                    "node orphan storm repair suppressed during pool startup grace for active node",
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
    elif api_stall_asics:
        active_api_keys = {miner_stall_identity_key(item) for item in api_stall_asics if miner_stall_identity_key(item)}
        staged_recovery = prune_asic_staged_recovery(state, active_api_keys)
        for item in api_stall_asics:
            identity_key = miner_stall_identity_key(item)
            if not identity_key:
                continue
            record = staged_recovery.setdefault(identity_key, {})
            seed_asic_staged_recovery(record, item, miner_restart_by_ip, now)
            stage, wait_remaining = asic_staged_recovery_stage(record, now)
            item["staged_recovery_stage"] = stage
            item["staged_recovery_wait_remaining_seconds"] = max(int(wait_remaining), 0)

        affected = [
            {
                "identity_key": miner_stall_identity_key(item),
                "ip": item.get("ip"),
                "mac": item.get("mac"),
                "name": item.get("display_name"),
                "status": item.get("status"),
                "configured": item.get("configured"),
                "connected": item.get("connected"),
                "pool_active": item.get("pool_active"),
                "work_pool_active": item.get("work_pool_active"),
                "last_pool_seen_age_seconds": item.get("last_pool_seen_age_seconds"),
                "last_share_age_seconds": item.get("last_share_age_seconds"),
                "last_submit_age_seconds": item.get("last_submit_age_seconds"),
                "api_stall_stale_age_seconds": item.get("api_stall_stale_age_seconds"),
                "api_stall_no_active_pool": item.get("api_stall_no_active_pool"),
                "api_stall_no_request_pressure": item.get("api_stall_no_request_pressure"),
                "issue": item.get("issue"),
                "debug_error": item.get("debug_error"),
                "device_telemetry_status": item.get("device_telemetry_status"),
                "device_telemetry_errors": item.get("device_telemetry_errors"),
                "staged_recovery_stage": item.get("staged_recovery_stage"),
                "staged_recovery_wait_remaining_seconds": item.get(
                    "staged_recovery_wait_remaining_seconds"
                ),
            }
            for item in api_stall_asics
        ]
        eligible_miners = []
        hardware_required_miners = []
        waiting = []
        for item in api_stall_asics:
            ip = str(item.get("ip"))
            identity_key = miner_stall_identity_key(item)
            stalled_for = now - int(asic_api_stall_since.get(identity_key, now) or now)
            confirm_seconds = (
                DEFAULT_ASIC_API_STALL_NO_ACTIVE_CONFIRM_SECONDS
                if item.get("api_stall_no_active_pool") or item.get("api_stall_no_request_pressure")
                else DEFAULT_ASIC_API_STALL_CONFIRM_SECONDS
            )
            cooldown_remaining = DEFAULT_ASIC_API_STALL_REPAIR_COOLDOWN - (
                now - int(miner_restart_by_ip.get(ip, 0) or 0)
            )
            recovery_stage = str(item.get("staged_recovery_stage") or "open-restart")
            recovery_wait_remaining = int_or_none(item.get("staged_recovery_wait_remaining_seconds")) or 0
            if recovery_stage == "hardware-power-cycle-required":
                hardware_required_miners.append(item)
            elif (
                recovery_stage in {"open-restart", "auth-restart-configure"}
                and stalled_for >= confirm_seconds
                and cooldown_remaining <= 0
            ):
                eligible_miners.append(item)
            else:
                waiting.append(
                    f"{identity_key or ip} ip={ip} stalled_for={stalled_for}s "
                    f"confirm={confirm_seconds}s "
                    f"cooldown_remaining={max(cooldown_remaining, 0)}s "
                    f"stage={recovery_stage} stage_wait_remaining={recovery_wait_remaining}s"
                )
        reason = (
            f"{len(api_stall_asics)} managed ASIC miner(s) have a sustained local API/cgminer stall "
            "while pool-wide backend/template failure checks are clear"
        )
        if stratum_no_request_delta > 0:
            reason += (
                f"; Stratum also saw {stratum_no_request_delta} no-request disconnect(s) "
                "since the last watchdog sample"
            )
        if any(item.get("api_stall_no_request_pressure") for item in api_stall_asics):
            reason += "; no-request EOF pressure matches the stalled managed ASIC signature"
        state["consecutive_failures"] = 0
        state["consecutive_syncing"] = 0
        state["consecutive_node_orphan_storm"] = 0
        state["consecutive_share_stalls"] = 0
        state["consecutive_miner_useful_work_stalls"] = 0
        state["last_status"] = "asic_api_stall"
        state["last_failures"] = []
        state["last_share_warnings"] = [reason]
        state["last_asic_api_stall"] = affected
        if hardware_required_miners:
            hardware_reason = (
                reason
                + "; soft restart and authenticated config/restart retry windows were exhausted, "
                "hardware power-cycle required"
            )
            state["last_status"] = "asic_hardware_power_cycle_required"
            state["last_share_warnings"] = [hardware_reason]
            state["last_asic_hardware_power_cycle_required"] = [
                {
                    "identity_key": miner_stall_identity_key(item),
                    "ip": item.get("ip"),
                    "mac": item.get("mac"),
                    "status": item.get("status"),
                    "api_stall_issue": item.get("api_stall_issue"),
                    "api_stall_no_active_pool": item.get("api_stall_no_active_pool"),
                    "api_stall_no_request_pressure": item.get("api_stall_no_request_pressure"),
                }
                for item in hardware_required_miners
            ]
            mark_asic_hardware_power_cycle_required(state, hardware_required_miners, hardware_reason, now)
            if repair:
                run_asic_remote_power_cycles(hardware_required_miners, hardware_reason, state, now)
        log(
            "asic_api_stall "
            f"affected={affected} eligible={[item.get('ip') for item in eligible_miners]} "
            f"hardware_required={[item.get('ip') for item in hardware_required_miners]} "
            f"waiting={'; '.join(waiting) or 'none'}"
        )
        record_efficiency_event(
            "asic_api_stall",
            "critical" if hardware_required_miners else "warning",
            state["last_share_warnings"][0],
            {
                "affected_miners": affected,
                "eligible": [item.get("ip") for item in eligible_miners],
                "hardware_required": [item.get("ip") for item in hardware_required_miners],
                "waiting": waiting,
                "primary_miner_count": primary_miner_count,
                "stratum_no_request_delta": stratum_no_request_delta,
                "stratum_no_request_total": stratum_no_request_total,
            },
        )
        if repair and eligible_miners:
            repair_limit = (
                len(eligible_miners)
                if all(item.get("api_stall_no_active_pool") for item in eligible_miners)
                else 1
            )
            repair_targets = []
            for item in eligible_miners[:repair_limit]:
                target = dict(item)
                if target.get("staged_recovery_stage") == "auth-restart-configure":
                    target["restart_open_first"] = False
                else:
                    target["staged_recovery_stage"] = "open-restart"
                    target["restart_open_first"] = True
                repair_targets.append(target)
            result = run_miner_restarts(repair_targets, "ASIC API-stall watchdog: " + reason)
            state["last_miner_repair_at"] = now
            state["last_miner_repair"] = result
            for item in repair_targets:
                ip = str(item.get("ip"))
                miner_restart_by_ip[ip] = now
                identity_key = miner_stall_identity_key(item)
                if identity_key:
                    record = staged_recovery.setdefault(identity_key, {})
                    seed_asic_staged_recovery(record, item, miner_restart_by_ip, now)
                    if item.get("staged_recovery_stage") == "auth-restart-configure":
                        record["auth_retry_at"] = now
                        record["last_stage"] = "auth-restart-configure"
                    else:
                        record["open_restart_at"] = now
                        record["last_stage"] = "open-restart"
            state["last_miner_restart_at_by_ip"] = miner_restart_by_ip
            state["asic_staged_recovery_by_identity"] = staged_recovery
            state["asic_api_stall_since"] = asic_api_stall_since
    elif useful_work_stalled_asics:
        affected = [
            {
                "identity_key": miner_stall_identity_key(item),
                "ip": item.get("ip"),
                "mac": item.get("mac"),
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
            identity_key = miner_stall_identity_key(item)
            stalled_for = now - int(useful_work_stall_since.get(identity_key, now) or now)
            cooldown_remaining = DEFAULT_MINER_USEFUL_WORK_STALL_REPAIR_COOLDOWN - (
                now - int(miner_restart_by_ip.get(ip, 0) or 0)
            )
            if stalled_for >= DEFAULT_MINER_USEFUL_WORK_STALL_CONFIRM_SECONDS and cooldown_remaining <= 0:
                eligible_miners.append(item)
            else:
                waiting.append(
                    f"{identity_key or ip} ip={ip} stalled_for={stalled_for}s "
                    f"confirm={DEFAULT_MINER_USEFUL_WORK_STALL_CONFIRM_SECONDS}s "
                    f"cooldown_remaining={max(cooldown_remaining, 0)}s"
                )
        reason = (
            f"{len(useful_work_stalled_asics)} primary ASIC miner(s) are connected/API-visible "
            "but have stopped producing useful accepted work or solved on-chain blocks while peer miners are healthy"
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
                useful_work_stall_since.pop(miner_stall_identity_key(item), None)
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
    elif submit_path_zero_success_storm or accepted_job_expired_storm or expired_job_reconnect_failed:
        failure_count = int(pool_health.get("block_submit_failure_count") or 0)
        duplicate_count = int(pool_health.get("duplicate_block_count") or 0)
        submit_errors = int(pool_health.get("block_submit_error_count") or 0)
        overdue_count = int(pool_health.get("tip_overdue_count") or 0)
        stale_job_count = int(pool_health.get("stale_job_candidate_count") or 0)
        expired_submit_count = int(pool_health.get("stale_submit_count") or 0)
        valid_share_count = int(pool_health.get("valid_share_count") or 0)
        if expired_job_reconnect_failed:
            reason = (
                "pool expired-job stale-client reconnect recovery exhausted: "
                "a miner re-authorized after forced reconnect, produced no valid shares, "
                "then timed out leaving zero active Stratum connections "
                f"(reconnects={pool_health.get('expired_job_reconnect_count')}, "
                f"reauthorize_after_reconnect={pool_health.get('expired_job_reauthorize_after_reconnect_count')}, "
                f"timeouts_after_reconnect={pool_health.get('expired_job_client_timeout_after_reconnect_count')}, "
                f"last_timeout={pool_health.get('expired_job_client_timeout_last_at')})"
            )
        elif accepted_job_expired_storm:
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
        state["last_status"] = (
            "pool_expired_job_reconnect_exhausted"
            if expired_job_reconnect_failed
            else ("pool_accepted_job_expired_storm" if accepted_job_expired_storm else "pool_submit_path_stall")
        )
        state["last_failures"] = []
        state["last_share_warnings"] = [reason]
        log(f"pool_submit_path_stall consecutive={state['consecutive_submit_path_stalls']} reason={reason}")
        record_efficiency_event(
            (
                "pool_expired_job_reconnect_exhausted"
                if expired_job_reconnect_failed
                else ("pool_accepted_job_expired_storm" if accepted_job_expired_storm else "pool_submit_path_stall")
            ),
            "critical",
            reason,
            {
                "connected_miners": connected_miner_count,
                "accepted_job_expired_storm": accepted_job_expired_storm,
                "expired_job_reconnect_failed": expired_job_reconnect_failed,
                "expired_job_submit_count": expired_submit_count,
                "valid_share_count": valid_share_count,
                "block_submit_failure_count": failure_count,
                "duplicate_block_count": duplicate_count,
                "block_submit_error_count": submit_errors,
                "tip_overdue_count": overdue_count,
                "stale_job_candidate_count": stale_job_count,
                "pool_started_age_seconds": pool_started_age_seconds,
                "expired_job_reconnect_last_at": pool_health.get("expired_job_reconnect_last_at"),
                "expired_job_reconnect_last_line": pool_health.get("expired_job_reconnect_last_line"),
                "expired_job_client_timeout_last_at": pool_health.get("expired_job_client_timeout_last_at"),
                "expired_job_client_timeout_last_line": pool_health.get("expired_job_client_timeout_last_line"),
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
            if autonomous_lab_active:
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
                if expired_job_reconnect_failed:
                    prefix = "pool expired-job reconnect exhausted: "
                elif accepted_job_expired_storm:
                    prefix = "pool acceptedJobs expired storm: "
                else:
                    prefix = "pool submit-path zero-success storm: "
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
    elif status.get("sync_health", {}).get("needs_fast_sync_repair") and not fresh_paid_work_bridges_status_backend_readiness_flicker(status):
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
            log("sync repair suppressed because paid block submission is fresh")
            record_efficiency_event(
                "repair_suppressed",
                "warning",
                "sync repair suppressed because paid block submission is fresh",
                {
                    "sync_warnings": sync_warnings,
                    "freshness_seconds": 60,
                },
            )
        if repair and state["consecutive_syncing"] and should_restart_for_syncing(state, syncing_threshold, syncing_restart_cooldown):
            restart_node = template_nodes[0] if template_nodes else choose_lagging_node(status)
            if suppress_sync_restart_for_active_import(status, state, "; ".join(sync_warnings), restart_node):
                ok = False
                repair_attempted = False
            elif restart_node:
                ok = run_node_restart(restart_node, "persistent syncing: " + "; ".join(sync_warnings))
                repair_attempted = True
            else:
                ok = run_repair("restart", "persistent syncing: " + "; ".join(sync_warnings))
                repair_attempted = True
            if repair_attempted:
                state["last_repair_at"] = int(time.time())
                state["last_sync_repair_at"] = int(time.time())
                if ok:
                    state["consecutive_syncing"] = 0
    else:
        if state.get("last_status") != status_overall:
            log(f"status={status_overall} warnings={'; '.join(str(item) for item in status_warnings) or 'none'}")
        state["consecutive_failures"] = 0
        state["consecutive_syncing"] = 0
        state["consecutive_share_stalls"] = 0
        state["consecutive_submit_path_stalls"] = 0
        state["last_status"] = status_overall
        state["last_failures"] = []
        state["last_sync_warnings"] = []
        state["last_share_warnings"] = []
        state["last_pool_sync_pause_active"] = False

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
    repair: bool = True,
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
        f"asic_api_stall_stale={DEFAULT_ASIC_API_STALL_STALE_SECONDS}s "
        f"asic_api_stall_confirm={DEFAULT_ASIC_API_STALL_CONFIRM_SECONDS}s "
        f"asic_api_stall_cooldown={DEFAULT_ASIC_API_STALL_REPAIR_COOLDOWN}s "
        f"node_rpc_refused_confirm={DEFAULT_NODE_RPC_REFUSED_CONFIRM_SECONDS}s "
        f"node_rpc_refused_cooldown={DEFAULT_NODE_RPC_REFUSED_REPAIR_COOLDOWN}s "
        f"node_rpc_refused_pool_grace={DEFAULT_NODE_RPC_REFUSED_POOL_RESTART_GRACE_SECONDS}s "
        f"node_peer_lead_stall_confirm={DEFAULT_NODE_PEER_LEAD_STALL_CONFIRM_SECONDS}s "
        f"node_peer_lead_stall_cooldown={DEFAULT_NODE_PEER_LEAD_STALL_REPAIR_COOLDOWN}s "
        f"node_peer_lead_active_import_suppress={DEFAULT_NODE_PEER_LEAD_ACTIVE_IMPORT_SUPPRESS_SECONDS}s "
        f"node_peer_lead_active_import_worsen={DEFAULT_NODE_PEER_LEAD_ACTIVE_IMPORT_WORSEN_BLOCKS}blocks "
        f"node_peer_lead_active_import_max_lead={DEFAULT_NODE_PEER_LEAD_ACTIVE_IMPORT_MAX_LEAD_BLOCKS}blocks "
        f"node_peer_starvation_min_fresh_peers={DEFAULT_NODE_PEER_STARVATION_MIN_FRESH_PEERS} "
        f"node_peer_lead_hard_stall_job_age={DEFAULT_NODE_PEER_LEAD_HARD_STALL_JOB_AGE_SECONDS}s "
        f"node_peer_lead_hard_stall_retry_cooldown={DEFAULT_NODE_PEER_LEAD_HARD_STALL_RETRY_COOLDOWN}s "
        f"node_peer_lead_hard_stall_active_import_max_wait="
        f"{DEFAULT_NODE_PEER_LEAD_HARD_STALL_ACTIVE_IMPORT_MAX_WAIT_SECONDS}s "
        f"node_template_sync_wedge_confirm={DEFAULT_NODE_TEMPLATE_SYNC_WEDGE_CONFIRM_SECONDS}s "
        f"node_template_sync_wedge_cooldown={DEFAULT_NODE_TEMPLATE_SYNC_WEDGE_REPAIR_COOLDOWN}s "
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
                repair=repair,
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
    parser.add_argument("--dry-run", action="store_true", help="evaluate triage without performing repairs")
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
            repair=not args.dry_run,
        )
        return 0

    result = check_once(
        args.threshold,
        args.clean_restore_cooldown,
        args.syncing_threshold,
        args.syncing_restart_cooldown,
        args.miner_down_restart_seconds,
        args.miner_restart_cooldown,
        repair=(args.once or not args.loop) and not args.dry_run,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
