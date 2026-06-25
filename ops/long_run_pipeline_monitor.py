#!/usr/bin/env python3
"""Read-only long-run pipeline monitor for live BlockDAG mining stack.

The monitor writes minute-level JSONL samples and hourly raw bundles. It never
starts, stops, restarts, rebuilds, or edits live services.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_JOB_STATE_URL = "http://127.0.0.1:9090/health/job-state"
DEFAULT_METRICS_URL = "http://127.0.0.1:9090/metrics"
DEFAULT_DASHBOARD_STATUS_URL = "http://127.0.0.1:8088/api/status"
DEFAULT_NODE_RPC_URL = "http://127.0.0.1:38131"
DEFAULT_OUTPUT_ROOT = Path("ops/runtime/monitoring")
METRIC_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([-+0-9.eE]+)$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
IMPORTED_RE = re.compile(r"Imported new chain segment.*?number=([0-9,]+).*?age=([0-9hms]+)")
GRAPH_SYNC_START_RE = re.compile(r"Syncing graph state.*?peer=([^\s]+).*?processID=([0-9]+)")
GRAPH_SYNC_END_RE = re.compile(r"sync of graph state has ended.*?spend=([^\s]+).*?processID=([0-9]+)")

METRIC_NAMES = {
    "pool_block_submit_outcomes_total",
    "pool_block_submit_backend_outcomes_total",
    "pool_blocks_found_total",
    "pool_blocks_submitted_total",
    "pool_duplicate_block_candidates_rejected_local_total",
    "pool_job_health_authorized_miners",
    "pool_job_health_ready_miners",
    "pool_job_health_miners_without_current_job",
    "pool_job_health_ok",
    "pool_job_health_max_current_job_age_seconds",
    "pool_rpc_backend_node_health_mineable",
    "pool_rpc_backend_node_health_submit_ready",
    "pool_rpc_backend_node_health_p2p_mining_fresh",
    "pool_rpc_backend_node_health_p2p_consensus_peer_count",
    "pool_rpc_backend_node_health_p2p_fresh_consensus_peer_count",
    "pool_rpc_backend_node_health_p2p_best_peer_lead_blocks",
    "pool_rpc_backend_node_health_template_age_ms",
    "pool_rpc_backend_node_health_template_age_seconds",
    "pool_block_timing_controller_waste_ratio",
    "pool_block_timing_controller_job_age_ms",
    "pool_block_timing_controller_template_ttl_ms",
    "pool_block_timing_controller_recent_stale_grace_ms",
    "pool_template_broadcast_age_ms",
    "pool_valid_shares_total",
    "pool_rejected_shares_total",
}

SUMMARY_METRICS = {
    "accepted_blocks": "pool_block_submit_outcomes_total{outcome=accepted,pool_id=0,reason=ok}",
    "blocks_found": "pool_blocks_found_total{pool_id=0}",
    "blocks_submitted": "pool_blocks_submitted_total{pool_id=0}",
    "stale_job_rejects": "pool_block_submit_outcomes_total{outcome=rejected-local,pool_id=0,reason=stale-job}",
    "stale_parent_rejects": "pool_block_submit_outcomes_total{outcome=rejected-local,pool_id=0,reason=stale-parent}",
    "duplicate_rejects": "pool_block_submit_outcomes_total{outcome=rejected-local,pool_id=0,reason=duplicate-block}",
    "ready_miners": "pool_job_health_ready_miners{pool_id=0}",
    "authorized_miners": "pool_job_health_authorized_miners{pool_id=0}",
    "p2p_mining_fresh": "pool_rpc_backend_node_health_p2p_mining_fresh{node=node,pool_id=0}",
    "peer_lead_blocks": "pool_rpc_backend_node_health_p2p_best_peer_lead_blocks{node=node,pool_id=0}",
    "fresh_consensus_peers": "pool_rpc_backend_node_health_p2p_fresh_consensus_peer_count{node=node,pool_id=0}",
    "mineable": "pool_rpc_backend_node_health_mineable{node=node,pool_id=0}",
    "submit_ready": "pool_rpc_backend_node_health_submit_ready{node=node,pool_id=0}",
    "template_age_seconds": "pool_rpc_backend_node_health_template_age_seconds{node=node,pool_id=0}",
    "waste_ratio": "pool_block_timing_controller_waste_ratio{pool_id=0}",
}

SUMMARY_COUNTERS = (
    "accepted_blocks",
    "blocks_found",
    "blocks_submitted",
    "stale_job_rejects",
    "stale_parent_rejects",
    "duplicate_rejects",
)

SUMMARY_GAUGES = (
    "ready_miners",
    "authorized_miners",
    "p2p_mining_fresh",
    "peer_lead_blocks",
    "fresh_consensus_peers",
    "mineable",
    "submit_ready",
    "template_age_seconds",
    "waste_ratio",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def fetch_text(url: str, timeout: float) -> tuple[str | None, str | None, float]:
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
        return text, None, round((time.monotonic() - started) * 1000, 3)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, str(exc), round((time.monotonic() - started) * 1000, 3)


def fetch_json(url: str, timeout: float) -> tuple[dict[str, Any] | None, str | None, float]:
    text, error, latency = fetch_text(url, timeout)
    if error or text is None:
        return None, error, latency
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"json decode: {exc}", latency
    return payload if isinstance(payload, dict) else {}, None, latency


def basic_auth_header(user: str | None, password: str | None) -> str | None:
    if not user and not password:
        return None
    token = base64.b64encode(f"{user or ''}:{password or ''}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def json_rpc_call(
    url: str,
    method: str,
    *,
    params: list[Any] | None = None,
    timeout: float,
    user: str | None = None,
    password: str | None = None,
) -> tuple[Any | None, str | None, float]:
    started = time.monotonic()
    body = json.dumps({"jsonrpc": "2.0", "id": method, "method": method, "params": params or []}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    auth = basic_auth_header(user, password)
    if auth:
        headers["Authorization"] = auth
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            decoded = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - monitor must keep sampling after node stalls.
        return None, str(exc), round((time.monotonic() - started) * 1000, 3)
    if not isinstance(decoded, dict):
        return None, "invalid json-rpc response", round((time.monotonic() - started) * 1000, 3)
    if decoded.get("error"):
        return None, f"json-rpc error: {decoded.get('error')}", round((time.monotonic() - started) * 1000, 3)
    return decoded.get("result"), None, round((time.monotonic() - started) * 1000, 3)


def run_command(command: list[str], timeout: float = 8.0) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return {
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
        }
    except Exception as exc:  # noqa: BLE001 - monitoring must keep running.
        return {
            "command": command,
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
        }


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def labels_to_dict(labels: str | None) -> dict[str, str]:
    output: dict[str, str] = {}
    if not labels:
        return output
    for item in labels.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        output[key.strip()] = value.strip().strip('"')
    return output


def parse_metrics(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    parsed: dict[str, Any] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = METRIC_RE.match(line)
        if not match:
            continue
        name, raw_labels, raw_value = match.groups()
        if name not in METRIC_NAMES:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        labels = labels_to_dict(raw_labels)
        key = name
        if labels:
            label_suffix = ",".join(f"{item_key}={labels[item_key]}" for item_key in sorted(labels))
            key = f"{name}{{{label_suffix}}}"
        parsed[key] = value
    return parsed


def parse_compact_duration_seconds(raw: str | None) -> int | None:
    if not raw:
        return None
    total = 0
    matched = False
    for value, unit in re.findall(r"([0-9]+)([hms])", raw):
        matched = True
        number = int(value)
        if unit == "h":
            total += number * 3600
        elif unit == "m":
            total += number * 60
        else:
            total += number
    return total if matched else None


def clean_log_line(line: str) -> str:
    return ANSI_RE.sub("", line)


def summarize_node_log_tail(log_text: str | None) -> dict[str, Any]:
    if not log_text:
        return {}
    latest_import: dict[str, Any] = {}
    sync_starts: dict[str, dict[str, Any]] = {}
    sync_ends: dict[str, dict[str, Any]] = {}
    rewind_count = 0
    missing_tip_count = 0
    for raw_line in log_text.splitlines():
        line = clean_log_line(raw_line)
        imported = IMPORTED_RE.search(line)
        if imported:
            latest_import = {
                "number": int(imported.group(1).replace(",", "")),
                "age": imported.group(2),
                "age_seconds": parse_compact_duration_seconds(imported.group(2)),
            }
        start = GRAPH_SYNC_START_RE.search(line)
        if start:
            sync_starts[start.group(2)] = {"peer": start.group(1), "process_id": int(start.group(2))}
        end = GRAPH_SYNC_END_RE.search(line)
        if end:
            sync_ends[end.group(2)] = {
                "process_id": int(end.group(2)),
                "spend": end.group(1),
                "spend_seconds": parse_compact_duration_seconds(end.group(1)),
            }
        if "Rewinding blockchain to block" in line:
            rewind_count += 1
        if "Can't find tip" in line:
            missing_tip_count += 1
    open_ids = [process_id for process_id in sync_starts if process_id not in sync_ends]
    return {
        "latest_import": latest_import,
        "graph_sync_open": bool(open_ids),
        "graph_sync_open_process_ids": [int(process_id) for process_id in sorted(open_ids, key=int)],
        "graph_sync_last_open": sync_starts.get(open_ids[-1]) if open_ids else {},
        "graph_sync_last_end": sync_ends.get(sorted(sync_ends, key=int)[-1]) if sync_ends else {},
        "rewind_count_tail": rewind_count,
        "missing_tip_count_tail": missing_tip_count,
    }


def summarize_node_rpc(
    url: str,
    *,
    timeout: float,
    user: str | None,
    password: str | None,
) -> dict[str, Any]:
    health, health_error, health_latency = json_rpc_call(
        url,
        "getTemplateHealth",
        timeout=timeout,
        user=user,
        password=password,
    )
    block_count, block_error, block_latency = json_rpc_call(
        url,
        "getBlockCount",
        timeout=timeout,
        user=user,
        password=password,
    )
    summary: dict[str, Any] = {
        "url": url,
        "errors": {
            "getTemplateHealth": health_error,
            "getBlockCount": block_error,
        },
        "latency_ms": {
            "getTemplateHealth": health_latency,
            "getBlockCount": block_latency,
        },
        "block_count": block_count if isinstance(block_count, int) else None,
    }
    if isinstance(health, dict):
        summary.update(
            {
                "mineable_now": health.get("mineable_now"),
                "submit_ready": health.get("submit_ready"),
                "reason_code": health.get("reason_code"),
                "template_available": health.get("template_available"),
                "template_coinbase_valid": health.get("template_coinbase_valid"),
                "chain_current": health.get("chain_current"),
                "main_order": health.get("main_order"),
                "p2p_best_peer_main_order": health.get("p2p_best_peer_main_order"),
                "p2p_best_peer_lead_blocks": health.get("p2p_best_peer_lead_blocks"),
                "p2p_consensus_peer_count": health.get("p2p_consensus_peer_count"),
                "p2p_fresh_consensus_peer_count": health.get("p2p_fresh_consensus_peer_count"),
                "p2p_mining_fresh": health.get("p2p_mining_fresh"),
                "p2p_mining_fresh_reason_code": health.get("p2p_mining_fresh_reason_code"),
                "p2p_sync_peer_present": health.get("p2p_sync_peer_present"),
                "p2p_sync_peer_fresh": health.get("p2p_sync_peer_fresh"),
                "p2p_sync_peer_graph_state_age_ms": health.get("p2p_sync_peer_graph_state_age_ms"),
            }
        )
    return summary


def summarize_job_state(job_state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(job_state, dict):
        return {}
    router = job_state.get("router") if isinstance(job_state.get("router"), dict) else {}
    node = router.get("node") if isinstance(router.get("node"), dict) else {}
    clients = job_state.get("clients") if isinstance(job_state.get("clients"), list) else []
    return {
        "status": job_state.get("status"),
        "reason_code": job_state.get("reason_code"),
        "active_connections": job_state.get("active_connections"),
        "authorized_connections": job_state.get("authorized_connections"),
        "subscribed_connections": job_state.get("subscribed_connections"),
        "ready_connections": job_state.get("ready_connections"),
        "invalid_current_job_connections": job_state.get("invalid_current_job_connections"),
        "stale_current_job_connections": job_state.get("stale_current_job_connections"),
        "connections_without_current_job": job_state.get("connections_without_current_job"),
        "last_broadcast_age_ms": job_state.get("last_broadcast_age_ms"),
        "current_template_seq": job_state.get("current_template_seq"),
        "current_parent": job_state.get("current_parent"),
        "router_node": {
            "healthy": node.get("healthy"),
            "reason": node.get("reason"),
            "score": node.get("score"),
            "last_template_age_ms": node.get("last_template_age_ms"),
            "last_template_height": node.get("last_template_height"),
            "last_template_seq": node.get("last_template_seq"),
            "ws_connected": node.get("ws_connected"),
            "recent_submit_errors": node.get("recent_submit_errors"),
            "last_submit_age_ms": node.get("last_submit_age_ms"),
            "last_submit_error": node.get("last_submit_error"),
        },
        "clients": [
            {
                "remote_host": client.get("remote_host"),
                "asic_mac": client.get("asic_mac"),
                "lane_id": client.get("lane_id"),
                "ready": client.get("ready"),
                "reason_code": client.get("reason_code"),
                "current_job_age_ms": client.get("current_job_age_ms"),
                "template_seq": client.get("template_seq"),
                "pdiff": client.get("pdiff"),
                "expired_rejects": client.get("expired_rejects"),
                "expired_window_rejects": client.get("expired_window_rejects"),
                "low_diff_rejects": client.get("low_diff_rejects"),
            }
            for client in clients
            if isinstance(client, dict)
        ],
    }


def summarize_dashboard(status: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(status, dict):
        return {}
    sync = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    miner = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}
    template = status.get("template_health") if isinstance(status.get("template_health"), dict) else {}
    host = status.get("host_pressure") if isinstance(status.get("host_pressure"), dict) else {}
    return {
        "overall": status.get("overall"),
        "mode": status.get("mode"),
        "can_mine": status.get("can_mine"),
        "can_submit_blocks": status.get("can_submit_blocks"),
        "sync_status": sync.get("status"),
        "remaining_blocks": sync.get("remaining_blocks"),
        "native_is_current": sync.get("native_is_current"),
        "p2p_network_gap": sync.get("p2p_network_gap"),
        "connected_miners": miner.get("connected_count"),
        "managed_miners": miner.get("managed_count"),
        "template_reason_code": template.get("reason_code"),
        "mineable_now": template.get("mineable_now"),
        "submit_ready": template.get("submit_ready"),
        "template_coinbase_valid": template.get("template_coinbase_valid"),
        "iowait_percent": host.get("iowait_percent"),
        "cpu_busy_percent": host.get("cpu_busy_percent"),
        "io_some_avg10": host.get("io_some_avg10"),
        "cpu_some_avg10": host.get("cpu_some_avg10"),
        "memory_some_avg10": host.get("memory_some_avg10"),
    }


def sample_timestamp(sample: dict[str, Any]) -> datetime | None:
    raw = sample.get("sampled_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def sample_metric(sample: dict[str, Any], name: str) -> float | None:
    metrics = sample.get("metrics") if isinstance(sample.get("metrics"), dict) else {}
    key = SUMMARY_METRICS.get(name, name)
    value = metrics.get(key)
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def reset_aware_counter_delta(samples: list[dict[str, Any]], name: str) -> dict[str, Any]:
    previous: float | None = None
    delta = 0.0
    resets = 0
    first: float | None = None
    last: float | None = None
    for sample in samples:
        value = sample_metric(sample, name)
        if value is None:
            continue
        if first is None:
            first = value
        if previous is not None:
            if value >= previous:
                delta += value - previous
            else:
                resets += 1
                delta += max(value, 0.0)
        previous = value
        last = value
    return {
        "first": first,
        "last": last,
        "delta": round(delta, 6),
        "resets": resets,
    }


def gauge_summary(samples: list[dict[str, Any]], name: str) -> dict[str, Any]:
    values = [value for sample in samples if (value := sample_metric(sample, name)) is not None]
    if not values:
        return {"min": None, "max": None, "avg": None}
    return {
        "min": min(values),
        "max": max(values),
        "avg": round(sum(values) / len(values), 6),
    }


def sample_anomaly_reasons(sample: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    job_state = sample.get("pool_job_state") if isinstance(sample.get("pool_job_state"), dict) else {}
    errors = sample.get("errors") if isinstance(sample.get("errors"), dict) else {}
    ready = sample_metric(sample, "ready_miners")
    ready_state = job_state.get("ready_connections")
    try:
        ready_state_number = None if ready_state is None else int(float(ready_state))
    except (TypeError, ValueError):
        ready_state_number = None
    if (ready is not None and ready < 4) or (ready_state_number is not None and ready_state_number < 4):
        reasons.append("ready_miners_below_4")
    p2p = sample_metric(sample, "p2p_mining_fresh")
    if p2p is not None and p2p < 1:
        reasons.append("p2p_mining_not_fresh")
    lead = sample_metric(sample, "peer_lead_blocks")
    if lead is not None and lead > 10:
        reasons.append("peer_lead_exceeds_tolerance")
    template_age = sample_metric(sample, "template_age_seconds")
    if template_age is not None and template_age > 30:
        reasons.append("template_age_over_30s")
    mineable = sample_metric(sample, "mineable")
    submit_ready = sample_metric(sample, "submit_ready")
    if any(value for value in errors.values()):
        reasons.append("collector_error")
    core_pipeline_reasons = set(reasons)
    context_missing = all(
        value is None
        for value in (
            ready,
            ready_state_number,
            p2p,
            lead,
            template_age,
        )
    )
    if mineable is not None and mineable < 1 and (core_pipeline_reasons or context_missing):
        reasons.append("mineable_false")
    if submit_ready is not None and submit_ready < 1 and (core_pipeline_reasons or context_missing):
        reasons.append("submit_ready_false")
    return reasons


def window_anomaly_reasons(summary: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    duration_seconds = float(summary.get("duration_seconds") or 0.0)
    counters = summary.get("counters") if isinstance(summary.get("counters"), dict) else {}
    gauges = summary.get("gauges") if isinstance(summary.get("gauges"), dict) else {}
    accepted = counters.get("accepted_blocks") if isinstance(counters.get("accepted_blocks"), dict) else {}
    authorized = gauges.get("authorized_miners") if isinstance(gauges.get("authorized_miners"), dict) else {}
    ready = gauges.get("ready_miners") if isinstance(gauges.get("ready_miners"), dict) else {}
    p2p = gauges.get("p2p_mining_fresh") if isinstance(gauges.get("p2p_mining_fresh"), dict) else {}
    lead = gauges.get("peer_lead_blocks") if isinstance(gauges.get("peer_lead_blocks"), dict) else {}
    accepted_delta = float(accepted.get("delta") or 0.0)
    authorized_max = authorized.get("max")
    try:
        miner_demand = authorized_max is not None and float(authorized_max) > 0
    except (TypeError, ValueError):
        miner_demand = False
    if duration_seconds >= 300 and miner_demand and accepted_delta <= 0:
        reasons.append("accepted_blocks_not_advancing")
    if ready.get("min") is not None and ready.get("max") == 0:
        reasons.append("ready_miners_zero_for_window")
    if p2p.get("max") == 0:
        reasons.append("p2p_mining_not_fresh_for_window")
    if lead.get("max") is not None and lead.get("min") is not None and float(lead["max"]) > 10 and float(lead["min"]) > 10:
        reasons.append("peer_lead_exceeds_tolerance_for_window")
    return reasons


def summarize_sample_window(samples: list[dict[str, Any]]) -> dict[str, Any]:
    sample_times = [timestamp for sample in samples if (timestamp := sample_timestamp(sample)) is not None]
    started_at = min(sample_times).isoformat() if sample_times else None
    ended_at = max(sample_times).isoformat() if sample_times else None
    duration_seconds = (max(sample_times) - min(sample_times)).total_seconds() if len(sample_times) >= 2 else 0.0
    counters = {name: reset_aware_counter_delta(samples, name) for name in SUMMARY_COUNTERS}
    gauges = {name: gauge_summary(samples, name) for name in SUMMARY_GAUGES}
    local_reject_delta = sum(counters[name]["delta"] for name in ("stale_job_rejects", "stale_parent_rejects", "duplicate_rejects"))
    accepted_delta = counters["accepted_blocks"]["delta"]
    anomaly_samples: list[dict[str, Any]] = []
    for sample in samples:
        reasons = sample_anomaly_reasons(sample)
        if not reasons:
            continue
        anomaly_samples.append(
            {
                "sampled_at": sample.get("sampled_at"),
                "reasons": reasons,
                "accepted_blocks": sample_metric(sample, "accepted_blocks"),
                "ready_miners": sample_metric(sample, "ready_miners"),
                "p2p_mining_fresh": sample_metric(sample, "p2p_mining_fresh"),
                "peer_lead_blocks": sample_metric(sample, "peer_lead_blocks"),
                "mineable": sample_metric(sample, "mineable"),
                "submit_ready": sample_metric(sample, "submit_ready"),
                "template_age_seconds": sample_metric(sample, "template_age_seconds"),
            }
        )
    summary = {
        "sample_count": len(samples),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": round(duration_seconds, 3),
        "counters": counters,
        "gauges": gauges,
        "accepted_blocks_per_hour": round(accepted_delta / (duration_seconds / 3600), 6) if duration_seconds > 0 else None,
        "local_reject_delta": round(local_reject_delta, 6),
        "local_rejects_per_accepted": round(local_reject_delta / accepted_delta, 6) if accepted_delta > 0 else None,
        "counter_reset_count": sum(item["resets"] for item in counters.values()),
        "anomaly_count": len(anomaly_samples),
        "anomaly_samples": anomaly_samples[:25],
        "anomaly_samples_truncated": max(0, len(anomaly_samples) - 25),
    }
    summary["window_anomaly_reasons"] = window_anomaly_reasons(summary)
    return summary


def load_sample_events(samples_path: Path) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for line in samples_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("event") == "sample":
            samples.append(payload)
    return samples


def summarize_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        timestamp = sample_timestamp(sample)
        if timestamp is None:
            continue
        buckets[timestamp.strftime("%Y-%m-%dT%H:00%z")].append(sample)
    return {
        "generated_at": now_iso(),
        "total": summarize_sample_window(samples),
        "hourly": {
            hour: summarize_sample_window(bucket_samples)
            for hour, bucket_samples in sorted(buckets.items())
        },
    }


def write_summary_snapshot(samples_path: Path, output_path: Path) -> None:
    samples = load_sample_events(samples_path)
    append_jsonl(output_path, {"event": "summary", **summarize_samples(samples)})


def read_proc_file(path: str, limit: int = 200000) -> str:
    try:
        data = Path(path).read_text(encoding="utf-8", errors="replace")
        return data[:limit]
    except OSError as exc:
        return f"ERROR: {exc}"


def collect_sample(args: argparse.Namespace) -> dict[str, Any]:
    job_state, job_error, job_latency = fetch_json(args.job_state_url, args.timeout)
    metrics_text, metrics_error, metrics_latency = fetch_text(args.metrics_url, args.timeout)
    dashboard, dashboard_error, dashboard_latency = fetch_json(args.dashboard_status_url, args.timeout)
    docker_stats = run_command(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
        timeout=args.command_timeout,
    )
    docker_ps = run_command(
        ["docker", "ps", "--format", "{{json .}}"],
        timeout=args.command_timeout,
    )
    node_logs = run_command(
        ["docker", "logs", "--tail", str(args.node_log_tail_lines), args.node_container],
        timeout=args.command_timeout,
    )
    return {
        "sampled_at": now_iso(),
        "sampled_epoch": time.time(),
        "errors": {
            "job_state": job_error,
            "metrics": metrics_error,
            "dashboard": dashboard_error,
        },
        "latency_ms": {
            "job_state": job_latency,
            "metrics": metrics_latency,
            "dashboard": dashboard_latency,
        },
        "pool_job_state": summarize_job_state(job_state),
        "dashboard_status": summarize_dashboard(dashboard),
        "node_rpc": summarize_node_rpc(
            args.node_rpc_url,
            timeout=args.timeout,
            user=args.node_rpc_user,
            password=args.node_rpc_pass,
        ),
        "node_log_tail": summarize_node_log_tail(node_logs.get("stdout", "") + node_logs.get("stderr", "")),
        "metrics": parse_metrics(metrics_text),
        "docker_stats_lines": [
            line for line in docker_stats.get("stdout", "").splitlines() if line.strip()
        ],
        "docker_ps_lines": [
            line for line in docker_ps.get("stdout", "").splitlines() if line.strip()
        ],
        "proc": {
            "loadavg": read_proc_file("/proc/loadavg"),
            "meminfo": read_proc_file("/proc/meminfo"),
            "diskstats": read_proc_file("/proc/diskstats"),
            "net_dev": read_proc_file("/proc/net/dev"),
            "pressure_cpu": read_proc_file("/proc/pressure/cpu"),
            "pressure_io": read_proc_file("/proc/pressure/io"),
            "pressure_memory": read_proc_file("/proc/pressure/memory"),
        },
    }


def write_hourly_bundle(root: Path, args: argparse.Namespace, hour_index: int) -> None:
    bundle = root / "hourly" / f"hour-{hour_index:02d}-{utc_stamp()}"
    bundle.mkdir(parents=True, exist_ok=True)
    commands = {
        "docker-ps.jsonl": ["docker", "ps", "--format", "{{json .}}"],
        "docker-stats.jsonl": ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
        "docker-events-1h.txt": [
            "docker",
            "events",
            "--since",
            "1h",
            "--until",
            "0s",
            "--format",
            "{{json .}}",
        ],
        "df-h.txt": ["df", "-h"],
        "ss-tanp.txt": ["ss", "-tanp"],
    }
    for filename, command in commands.items():
        result = run_command(command, timeout=max(args.command_timeout, 20.0))
        (bundle / filename).write_text(result.get("stdout", "") + result.get("stderr", ""), encoding="utf-8")

    for name, url in {
        "pool-job-state.json": args.job_state_url,
        "pool-metrics.prom": args.metrics_url,
        "dashboard-status.json": args.dashboard_status_url,
    }.items():
        text, error, _ = fetch_text(url, args.timeout)
        (bundle / name).write_text(text if text is not None else f"ERROR: {error}\n", encoding="utf-8")

    for log_name, command in {
        "pool-last-1h.log": ["docker", "logs", "--since", "1h", "pool"],
        "node-last-1h.log": ["docker", "logs", "--since", "1h", "node"],
        "watchdog-tail.log": ["tail", "-n", "300", "ops/runtime/logs/watchdog.log"],
        "status-sampler-tail.log": ["tail", "-n", "300", "ops/runtime/logs/status-sampler.log"],
    }.items():
        result = run_command(command, timeout=max(args.command_timeout, 30.0))
        (bundle / log_name).write_text(result.get("stdout", "") + result.get("stderr", ""), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--duration-seconds", type=float, default=18 * 60 * 60)
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--hourly-seconds", type=float, default=60 * 60)
    parser.add_argument("--job-state-url", default=DEFAULT_JOB_STATE_URL)
    parser.add_argument("--metrics-url", default=DEFAULT_METRICS_URL)
    parser.add_argument("--dashboard-status-url", default=DEFAULT_DASHBOARD_STATUS_URL)
    parser.add_argument("--node-rpc-url", default=os.environ.get("BDAG_NODE_RPC_URL", DEFAULT_NODE_RPC_URL))
    parser.add_argument("--node-rpc-user", default=os.environ.get("NODE_RPC_USER", ""))
    parser.add_argument("--node-rpc-pass", default=os.environ.get("NODE_RPC_PASS", ""))
    parser.add_argument("--node-container", default="node")
    parser.add_argument("--node-log-tail-lines", type=int, default=400)
    parser.add_argument("--timeout", type=float, default=6.0)
    parser.add_argument("--command-timeout", type=float, default=10.0)
    parser.add_argument("--summarize-samples", type=Path, help="read an existing samples.jsonl and emit reset-aware summary JSON")
    parser.add_argument("--summary-output", type=Path, help="write --summarize-samples output to this path instead of stdout")
    args = parser.parse_args()

    if args.summarize_samples:
        summary = summarize_samples(load_sample_events(args.summarize_samples))
        payload = json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n"
        if args.summary_output:
            args.summary_output.parent.mkdir(parents=True, exist_ok=True)
            args.summary_output.write_text(payload, encoding="utf-8")
        else:
            print(payload, end="")
        return 0

    root = args.output_root / datetime.now().strftime("%Y%m%d-%H%M%S%z-18h-monitor")
    root.mkdir(parents=True, exist_ok=True)
    samples_path = root / "samples.jsonl"
    summaries_path = root / "hourly-summaries.jsonl"
    marker = {
        "event": "start",
        "started_at": now_iso(),
        "pid": os.getpid(),
        "duration_seconds": args.duration_seconds,
        "interval_seconds": args.interval_seconds,
        "hourly_seconds": args.hourly_seconds,
        "output_root": str(root),
    }
    append_jsonl(samples_path, marker)
    print(json.dumps(marker, sort_keys=True), flush=True)

    deadline = time.monotonic() + max(0.0, args.duration_seconds)
    next_sample = time.monotonic()
    next_hourly = time.monotonic()
    hour_index = 0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_sample:
            append_jsonl(samples_path, {"event": "sample", **collect_sample(args)})
            next_sample = now + max(1.0, args.interval_seconds)
        if now >= next_hourly:
            write_hourly_bundle(root, args, hour_index)
            write_summary_snapshot(samples_path, summaries_path)
            append_jsonl(samples_path, {"event": "hourly_bundle", "generated_at": now_iso(), "hour_index": hour_index})
            hour_index += 1
            next_hourly = now + max(args.interval_seconds, args.hourly_seconds)
        time.sleep(min(1.0, max(0.0, next_sample - time.monotonic())))

    write_summary_snapshot(samples_path, summaries_path)
    append_jsonl(samples_path, {"event": "stop", "stopped_at": now_iso()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
