#!/usr/bin/env python3
"""Shared status source for local BlockDAG stack agents.

This module keeps status acquisition behind one interface. Repair actors should
consume stack status here instead of each choosing between collector HTTP,
status-sampler reuse, and direct in-process collection on their own.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from pool_ops import POOL_CONTAINERS, collect_pool_prometheus_metrics, collect_status_cached


DEFAULT_COLLECTOR_STATUS_URL = "http://127.0.0.1:9280/api/status"
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("BDAG_STATUS_SOURCE_TIMEOUT", "20"))
POOL_METRIC_ENRICHMENT_KEYS = (
    "stratum_no_request_disconnects",
    "stratum_no_request_disconnects_total",
    "stratum_server_first_difficulty_probes",
    "stratum_server_first_difficulty_probes_total",
)
ASIC_TELEMETRY_ERROR_FIELDS = ("pools", "cgminer_devs", "status")


class StackStatusUnavailable(RuntimeError):
    """Raised when every status adapter fails."""


def _env_urls() -> list[str]:
    raw = (
        os.environ.get("BDAG_STATUS_SOURCE_URLS")
        or os.environ.get("BDAG_STATUS_SOURCE_URL")
        or os.environ.get("BDAG_COLLECTOR_STATUS_URL")
        or DEFAULT_COLLECTOR_STATUS_URL
    )
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def _fixture_payload() -> dict[str, Any] | None:
    raw = os.environ.get("BDAG_STATUS_SOURCE_FIXTURE") or os.environ.get("BDAG_STATUS_SOURCE_FIXTURE_FILE")
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    try:
        if candidate.exists():
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        else:
            payload = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        raise StackStatusUnavailable(f"fixture status payload is unreadable: {raw}")
    if not isinstance(payload, dict):
        raise StackStatusUnavailable("fixture status payload must be a JSON object")
    return payload


def _annotate(payload: dict[str, Any], source: str, errors: list[str]) -> dict[str, Any]:
    result = dict(payload)
    result["stack_status_source"] = {
        "source": source,
        "errors": list(errors),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    return result


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _normalize_mac(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith("mac:"):
        raw = raw[4:]
    compact = "".join(ch for ch in raw if ch in "0123456789abcdef")
    if len(compact) != 12:
        return raw
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _int_value(value: Any) -> int:
    try:
        return int(float(str(value or "0").strip()))
    except (TypeError, ValueError):
        return 0


def _optional_int_value(*values: Any) -> int | None:
    for value in values:
        if value is None:
            continue
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            continue
    return None


def _active_pool_job_macs(payload: dict[str, Any]) -> set[str]:
    pool_job_state = payload.get("pool_job_state") if isinstance(payload.get("pool_job_state"), dict) else {}
    clients = pool_job_state.get("clients") if isinstance(pool_job_state.get("clients"), list) else []
    active: set[str] = set()
    for client in clients:
        if not isinstance(client, dict):
            continue
        mac = _normalize_mac(client.get("asic_mac") or client.get("mac"))
        if not mac:
            continue
        if client.get("authorized") or client.get("current_job_id") or client.get("ready"):
            active.add(mac)
    return active


def _pool_has_no_active_clients(payload: dict[str, Any]) -> bool:
    pool_health = payload.get("pool_health") if isinstance(payload.get("pool_health"), dict) else {}
    pool_job_state = payload.get("pool_job_state") if isinstance(payload.get("pool_job_state"), dict) else {}
    pool_metrics = payload.get("pool_metrics") if isinstance(payload.get("pool_metrics"), dict) else {}

    clients = pool_job_state.get("clients") if isinstance(pool_job_state.get("clients"), list) else []
    if any(
        isinstance(client, dict) and (client.get("authorized") or client.get("ready") or client.get("current_job_id"))
        for client in clients
    ):
        return False

    active = _optional_int_value(pool_job_state.get("active_connections"), pool_metrics.get("active_connections"))
    authorized = _optional_int_value(
        pool_job_state.get("authorized_connections"),
        pool_metrics.get("authorized_connections"),
        pool_metrics.get("authorized_miners"),
    )
    ready = _optional_int_value(
        pool_job_state.get("ready_connections"),
        pool_metrics.get("ready_connections"),
        pool_metrics.get("ready_miners"),
    )
    counts = [value for value in (active, authorized, ready) if value is not None]
    if counts:
        return all(value <= 0 for value in counts)

    reason = str(pool_job_state.get("reason_code") or pool_health.get("job_state_reason") or "").strip().lower()
    return reason in {"no_active_miners", "no-active-miners", "no_clients", "no-clients"}


def _asic_telemetry_issue(device: dict[str, Any]) -> str:
    errors = device.get("errors") if isinstance(device.get("errors"), dict) else {}
    parts: list[str] = []
    for key in ASIC_TELEMETRY_ERROR_FIELDS:
        value = errors.get(key)
        if value:
            parts.append(f"{key}: {value}")
    status = str(device.get("status") or "").strip().lower()
    if status in {"degraded", "stale", "error"} and not parts:
        parts.append(f"telemetry status={status}")
    return "; ".join(parts)


def _enrich_dashboard_miner_rows(
    rows: Any,
    telemetry_by_mac: dict[str, dict[str, Any]],
    active_macs: set[str],
    mining_address: str,
    pool_has_no_active_clients: bool,
) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    enriched: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        mac = _normalize_mac(row.get("mac") or row.get("identity") or row.get("device_id"))
        if row.get("configured_pool_url") and not row.get("expected_pool_url"):
            row["expected_pool_url"] = row.get("configured_pool_url")
        if not row.get("expected_worker_user"):
            row["expected_worker_user"] = row.get("intended_wallet") or mining_address

        if mac and mac in active_macs:
            row["connected"] = True
            row["pool_active"] = True
            row["work_pool_active"] = True
            if row.get("device_telemetry_status") == "ok" and str(row.get("status") or "") == "configured":
                row["status"] = "ok"

        device = telemetry_by_mac.get(mac)
        if device:
            issue = _asic_telemetry_issue(device)
            if issue:
                row["api_error"] = issue
                row["debug_error"] = issue
                row["issue"] = issue
                row["debug"] = {
                    "available": False,
                    "error": issue,
                }
                if pool_has_no_active_clients and mac not in active_macs:
                    row["status"] = "down"
                    row["health"] = "down"
                    row["connected"] = False
                    row["pool_active"] = False
                    row["work_pool_active"] = False
                elif not _truthy(row.get("connected")) and not _truthy(row.get("pool_active")):
                    row["status"] = "down"
                    row["health"] = "down"
                    row["connected"] = False
                    row["pool_active"] = False
                    row["work_pool_active"] = False
        enriched.append(row)
    return enriched


def _with_dashboard_asic_telemetry_enrichment(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize Redis dashboard ASIC telemetry into watchdog-compatible miner rows.

    Redis-dashboard status already knows when a Goldshell responds on /mcb/status
    but its pool/cgminer endpoints are wedged. The watchdog repair path expects
    that same fact as miner_health status/api_error fields, so bridge the schema
    here instead of duplicating ASIC polling in every repair actor.
    """

    if not _env_bool("BDAG_STATUS_SOURCE_ASIC_TELEMETRY_ENRICHMENT", True):
        return payload

    telemetry = payload.get("asic_telemetry") if isinstance(payload.get("asic_telemetry"), dict) else {}
    devices = telemetry.get("devices") if isinstance(telemetry.get("devices"), list) else []
    if not devices:
        return payload

    telemetry_by_mac = {
        _normalize_mac(device.get("mac")): device
        for device in devices
        if isinstance(device, dict) and _normalize_mac(device.get("mac"))
    }
    if not telemetry_by_mac:
        return payload

    miner_health = payload.get("miner_health") if isinstance(payload.get("miner_health"), dict) else {}
    active_macs = _active_pool_job_macs(payload)
    pool_has_no_active_clients = _pool_has_no_active_clients(payload)
    mining_address = str(payload.get("mining_address") or "")
    rows = _enrich_dashboard_miner_rows(
        miner_health.get("miners"),
        telemetry_by_mac,
        active_macs,
        mining_address,
        pool_has_no_active_clients,
    )
    managed_rows = _enrich_dashboard_miner_rows(
        miner_health.get("managed_miners"),
        telemetry_by_mac,
        active_macs,
        mining_address,
        pool_has_no_active_clients,
    )
    if not rows and not managed_rows:
        return payload

    result = dict(payload)
    updated_miner_health = dict(miner_health)
    if rows:
        updated_miner_health["miners"] = rows
    if managed_rows:
        updated_miner_health["managed_miners"] = managed_rows
    connected_count = sum(1 for row in rows if _truthy(row.get("connected")))
    if pool_has_no_active_clients:
        updated_miner_health["connected_count"] = 0
        updated_miner_health["connected_count_effective"] = 0
    elif connected_count:
        updated_miner_health["connected_count"] = max(int(updated_miner_health.get("connected_count") or 0), connected_count)
        updated_miner_health["connected_count_effective"] = max(
            int(updated_miner_health.get("connected_count_effective") or 0),
            connected_count,
        )
    result["miner_health"] = updated_miner_health

    pool_health = result.get("pool_health") if isinstance(result.get("pool_health"), dict) else {}
    pool_job_state = result.get("pool_job_state") if isinstance(result.get("pool_job_state"), dict) else {}
    if not pool_health.get("job_notify_count") and (
        _int_value(pool_job_state.get("active_connections")) > 0
        or _int_value(pool_job_state.get("authorized_connections")) > 0
        or _int_value(pool_job_state.get("ready_connections")) > 0
    ):
        updated_pool_health = dict(pool_health)
        updated_pool_health["job_notify_count"] = 1
        result["pool_health"] = updated_pool_health
    result["asic_telemetry_enriched"] = True
    return result


def _with_direct_pool_metric_enrichment(payload: dict[str, Any]) -> dict[str, Any]:
    """Add locally parsed pool metrics when an external status payload is older than ops.

    Dashboard status is the preferred repair status source in the Redis-dashboard
    stack, but repair actors may learn about new pool metrics before the
    dashboard payload exposes them. In that case, enrich the payload from the
    pool metrics endpoint without replacing existing dashboard fields.
    """

    if not _env_bool("BDAG_STATUS_SOURCE_POOL_METRIC_ENRICHMENT", True):
        return payload

    pool_metrics = payload.get("pool_metrics") if isinstance(payload.get("pool_metrics"), dict) else {}
    if all(key in pool_metrics for key in POOL_METRIC_ENRICHMENT_KEYS):
        return payload

    containers = dict(payload.get("containers") or {}) if isinstance(payload.get("containers"), dict) else {}
    for name in POOL_CONTAINERS:
        info = dict(containers.get(name) or {})
        if info.get("running") is False:
            continue
        if not info.get("network_ips") and not info.get("network_mode"):
            info["running"] = True
            info["network_mode"] = "host"
        containers[name] = info

    try:
        direct = collect_pool_prometheus_metrics(containers)
    except Exception:  # noqa: BLE001 - enrichment must not break status collection.
        return payload
    if direct.get("status") != "ok":
        return payload

    result = dict(payload)
    merged_pool_metrics = dict(pool_metrics)
    for key, value in direct.items():
        merged_pool_metrics.setdefault(key, value)
    result["pool_metrics"] = merged_pool_metrics
    result["pool_metrics_enriched"] = True
    result["pool_metrics_enrichment_source"] = "direct-pool-prometheus"
    return result


def fetch_collector_status(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read(8_000_000).decode("utf-8", "replace"))
    if not isinstance(payload, dict):
        raise StackStatusUnavailable(f"collector returned non-object payload from {url}")
    return payload


def collect_stack_status(
    *,
    include_logs: bool = True,
    max_age_seconds: float | None = None,
    collector_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    prefer_collector: bool = True,
) -> dict[str, Any]:
    """Return the best available stack status payload.

    Adapter order:
    1. Collector HTTP, unless the caller explicitly requests a live local sample
       with max_age_seconds <= 0.
    2. In-process collect_status_cached, which already reuses the status sampler
       and short shared status cache when they are fresh.
    """

    errors: list[str] = []
    force_live_local = max_age_seconds is not None and max_age_seconds <= 0

    fixture = _fixture_payload()
    if fixture is not None:
        return _annotate(fixture, "fixture", errors)

    if prefer_collector and not force_live_local:
        urls = [collector_url] if collector_url else _env_urls()
        for url in [item for item in urls if item]:
            try:
                payload = _with_dashboard_asic_telemetry_enrichment(
                    _with_direct_pool_metric_enrichment(fetch_collector_status(url, timeout=timeout))
                )
                return _annotate(payload, "collector-http", errors)
            except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, StackStatusUnavailable) as exc:
                errors.append(f"collector {url}: {exc}")

    try:
        return _annotate(
            collect_status_cached(include_logs=include_logs, max_age_seconds=max_age_seconds),
            "in-process",
            errors,
        )
    except Exception as exc:  # noqa: BLE001 - callers need a single status-source failure.
        errors.append(f"in-process collect_status_cached: {exc}")

    raise StackStatusUnavailable("; ".join(errors) or "stack status unavailable")
