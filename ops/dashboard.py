#!/usr/bin/env python3
"""Local BlockDAG pool operations dashboard."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from incident_journal import read_recent_incidents
from pool_ops import (
    EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS,
    PROJECT_ROOT,
    RUNTIME_DIR,
    collect_global_blockchain,
    collect_earnings,
    collect_status,
    configure_miners,
    default_miner_pool_settings,
    ensure_runtime,
    make_handoff,
    mark_configured_miners,
    now_iso,
    read_latest_earnings_snapshot_info,
    read_miner_registry,
    save_miner_admin_password,
    scan_miners,
    upsert_miner_registry,
    write_action_state,
)


HOST = os.environ.get("BDAG_DASHBOARD_BIND", "127.0.0.1")
PORT = int(os.environ.get("BDAG_DASHBOARD_PORT", "8088"))
ACTION_TOKEN = os.environ.get("BDAG_DASHBOARD_TOKEN", "")
REQUIRE_TOKEN = os.environ.get("BDAG_DASHBOARD_REQUIRE_TOKEN", "auto")
WATCHDOG = PROJECT_ROOT / "ops" / "watchdog.py"
P2P_GUARD_STATE = RUNTIME_DIR / "p2p-health-state.json"
REPORTS_DIR = RUNTIME_DIR / "reports"
STATUS_CACHE_SECONDS = float(os.environ.get("BDAG_DASHBOARD_STATUS_CACHE_SECONDS", "10"))
EARNINGS_CACHE_SECONDS = float(os.environ.get("BDAG_DASHBOARD_EARNINGS_CACHE_SECONDS", "30"))
GLOBAL_CACHE_SECONDS = float(os.environ.get("BDAG_DASHBOARD_GLOBAL_CACHE_SECONDS", "60"))
SAMPLER_CACHE_SECONDS = float(os.environ.get("BDAG_DASHBOARD_SAMPLER_CACHE_SECONDS", "10"))
DASHBOARD_POOL_METRICS_TIMEOUT = float(os.environ.get("BDAG_DASHBOARD_POOL_METRICS_TIMEOUT", "1.5"))
TEMPLATE_BACKEND_STATE_CACHE_SECONDS = float(
    os.environ.get("BDAG_DASHBOARD_TEMPLATE_BACKEND_STATE_CACHE_SECONDS", str(STATUS_CACHE_SECONDS))
)
SYNC_ESTIMATE_STATE_FILE = RUNTIME_DIR / "dashboard-sync-estimate-state.json"
PROMETHEUS_SAMPLE_RE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|[-+]?Inf|NaN)$"
)
PROMETHEUS_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')
PROCESSED_BLOCKS_RE = re.compile(r"Processed\s+([0-9,]+)\s+blocks\s+in\s+the\s+last\s+([0-9.]+)s")
API_CACHE: dict[str, tuple[float, object]] = {}
API_CACHE_LOCK = threading.Lock()


def cached_payload(key: str, ttl: float, factory):
    now = time.time()
    with API_CACHE_LOCK:
        cached = API_CACHE.get(key)
        if cached and now - cached[0] < ttl:
            return cached[1]
    payload = factory()
    with API_CACHE_LOCK:
        API_CACHE[key] = (now, payload)
    return payload


def parse_prometheus_labels(label_text: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for match in PROMETHEUS_LABEL_RE.finditer(label_text or ""):
        labels[match.group(1)] = match.group(2).replace(r"\"", '"').replace(r"\\", "\\")
    return labels


def safe_int(value: object, default: int | None = None) -> int | None:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: object, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def read_json(path: Path, fallback: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def write_json(path: Path, payload: object) -> None:
    ensure_runtime()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def eta_iso(epoch: float | None) -> str:
    if epoch is None:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(epoch))


def node_processed_rate_from_tail(node: dict[str, object]) -> tuple[float | None, str]:
    tail = node.get("tail")
    if not isinstance(tail, list):
        return None, ""
    for line in reversed(tail):
        match = PROCESSED_BLOCKS_RE.search(str(line or ""))
        if not match:
            continue
        blocks = safe_float(match.group(1).replace(",", ""))
        seconds = safe_float(match.group(2))
        if blocks is None or seconds is None or seconds <= 0:
            continue
        return blocks / seconds, f"recent node log: {int(blocks)} blocks/{seconds:g}s"
    return None, ""


def sync_progress_for_node(payload: dict[str, object], node_name: str) -> dict[str, object]:
    sync_progress = payload.get("sync_progress") if isinstance(payload.get("sync_progress"), dict) else {}
    nodes = sync_progress.get("nodes") if isinstance(sync_progress.get("nodes"), dict) else {}
    node_progress = nodes.get(node_name) if isinstance(nodes, dict) else None
    if isinstance(node_progress, dict):
        return node_progress
    if sync_progress.get("source") == node_name:
        return sync_progress
    return {}


def choose_sync_leader(payload: dict[str, object]) -> str:
    coordinator = payload.get("sync_coordinator") if isinstance(payload.get("sync_coordinator"), dict) else {}
    leader = str(coordinator.get("leader") or "")
    if leader:
        return leader
    sync_progress = payload.get("sync_progress") if isinstance(payload.get("sync_progress"), dict) else {}
    nodes = sync_progress.get("nodes") if isinstance(sync_progress.get("nodes"), dict) else {}
    candidates: list[tuple[int, str]] = []
    if isinstance(nodes, dict):
        for name, progress in nodes.items():
            if not isinstance(progress, dict):
                continue
            current = safe_int(progress.get("current_block"), 0) or 0
            if current > 0:
                candidates.append((current, str(name)))
    candidates.sort(reverse=True)
    return candidates[0][1] if candidates else ""


def enrich_status_with_sync_estimate(payload: dict[str, object]) -> dict[str, object]:
    now = time.time()
    sync_progress = payload.get("sync_progress") if isinstance(payload.get("sync_progress"), dict) else {}
    sync_health = payload.get("sync_health") if isinstance(payload.get("sync_health"), dict) else {}
    coordinator = payload.get("sync_coordinator") if isinstance(payload.get("sync_coordinator"), dict) else {}
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
    managed_nodes = payload.get("managed_node_services") if isinstance(payload.get("managed_node_services"), list) else []
    single_active_node = len(managed_nodes) == 1
    leader = choose_sync_leader(payload)
    paused_follower = str(coordinator.get("paused_follower") or sync_health.get("planned_paused_follower") or "")
    mode = str(coordinator.get("mode") or ("leader_catchup" if paused_follower else "normal"))
    threshold = safe_int(((coordinator.get("last_decision") or {}).get("thresholds") or {}).get("leader_near_tip_blocks"), 5) or 5

    state = read_json(SYNC_ESTIMATE_STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    previous_nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    new_state = {"updated_at": eta_iso(now), "nodes": {}}
    estimate_nodes: dict[str, object] = {}

    progress_nodes = sync_progress.get("nodes") if isinstance(sync_progress.get("nodes"), dict) else {}
    node_names = sorted(set(list(nodes.keys()) + list(progress_nodes.keys() if isinstance(progress_nodes, dict) else [])))
    for name in node_names:
        progress = sync_progress_for_node(payload, name)
        current = safe_int(progress.get("current_block"))
        highest = safe_int(progress.get("highest_block"))
        remaining = safe_int(progress.get("remaining_blocks"))
        percent = safe_float(progress.get("percent"))
        node_info = nodes.get(name) if isinstance(nodes.get(name), dict) else {}
        log_rate, log_rate_source = node_processed_rate_from_tail(node_info)

        previous = previous_nodes.get(name) if isinstance(previous_nodes.get(name), dict) else {}
        previous_current = safe_int(previous.get("current_block"))
        previous_remaining = safe_int(previous.get("remaining_blocks"))
        previous_epoch = safe_float(previous.get("epoch"))
        observed_import_rate = None
        observed_net_rate = None
        if current is not None and previous_current is not None and previous_epoch is not None:
            elapsed = now - previous_epoch
            if 5 <= elapsed <= 7200 and current > previous_current:
                observed_import_rate = (current - previous_current) / elapsed
        if remaining is not None and previous_remaining is not None and previous_epoch is not None:
            elapsed = now - previous_epoch
            if 5 <= elapsed <= 7200 and previous_remaining > remaining:
                observed_net_rate = (previous_remaining - remaining) / elapsed

        rate = observed_net_rate or observed_import_rate or log_rate
        rate_source = (
            "net catch-up across dashboard samples"
            if observed_net_rate
            else "block import across dashboard samples"
            if observed_import_rate
            else log_rate_source
        )
        eta_seconds = remaining / rate if remaining is not None and rate and rate > 0 else None
        seed_remaining = max(0, remaining - threshold) if remaining is not None else None
        eta_to_seed_seconds = seed_remaining / rate if seed_remaining is not None and rate and rate > 0 else None
        estimate_nodes[name] = {
            "current_block": current,
            "highest_block": highest,
            "remaining_blocks": remaining,
            "percent": percent,
            "rate_blocks_per_second": round(rate, 3) if rate else None,
            "rate_source": rate_source,
            "eta_seconds": round(eta_seconds) if eta_seconds is not None else None,
            "eta_at": eta_iso(now + eta_seconds) if eta_seconds is not None else "",
            "eta_to_seed_seconds": round(eta_to_seed_seconds) if eta_to_seed_seconds is not None else None,
            "eta_to_seed_at": eta_iso(now + eta_to_seed_seconds) if eta_to_seed_seconds is not None else "",
            "planned_pause": bool(name == paused_follower),
            "leader": bool(name == leader),
        }
        if current is not None or remaining is not None:
            new_state["nodes"][name] = {
                "epoch": now,
                "current_block": current,
                "remaining_blocks": remaining,
                "highest_block": highest,
            }

    leader_estimate = estimate_nodes.get(leader) if isinstance(estimate_nodes.get(leader), dict) else {}
    remaining = safe_int(leader_estimate.get("remaining_blocks")) if leader_estimate else safe_int(sync_progress.get("remaining_blocks"))
    rate = safe_float(leader_estimate.get("rate_blocks_per_second")) if leader_estimate else None
    stage = (
        "Synced"
        if sync_progress.get("status") == "synced"
        else "Leader catch-up"
        if mode == "leader_catchup"
        else "Single-node catch-up"
        if single_active_node
        else "Dual-node sync"
    )
    if mode == "leader_catchup" and leader and paused_follower:
        narrative = (
            f"{leader} is syncing alone while {paused_follower} is paused to save bandwidth. "
            f"When the leader is within {threshold} block(s) of tip, the coordinator will copy the leader data to the follower and start both nodes."
        )
    elif sync_progress.get("status") == "synced":
        narrative = "Managed nodes are synced to the current network tip."
    elif single_active_node and leader:
        narrative = f"{leader} is the only active production node. The pool will wait for this node to finish sync before mining jobs are sent."
    else:
        narrative = "Managed nodes are syncing; the pool will wait for backend sync before mining jobs are sent."

    payload["sync_estimate"] = {
        "generated_at": eta_iso(now),
        "stage": stage,
        "mode": mode,
        "leader": leader,
        "paused_follower": paused_follower,
        "seed_threshold_blocks": threshold,
        "remaining_blocks": remaining,
        "rate_blocks_per_second": rate,
        "rate_source": leader_estimate.get("rate_source") if leader_estimate else "",
        "eta_seconds": leader_estimate.get("eta_seconds") if leader_estimate else None,
        "eta_at": leader_estimate.get("eta_at") if leader_estimate else "",
        "eta_to_seed_seconds": leader_estimate.get("eta_to_seed_seconds") if leader_estimate else None,
        "eta_to_seed_at": leader_estimate.get("eta_to_seed_at") if leader_estimate else "",
        "narrative": narrative,
        "nodes": estimate_nodes,
    }
    write_json(SYNC_ESTIMATE_STATE_FILE, new_state)
    return payload


def template_backend_state_from_metrics(text: str, source: str) -> dict[str, object]:
    state: dict[str, object] = {"source": source, "fan_in": {}, "backends": {}}
    fan_in = state["fan_in"]
    backends = state["backends"]
    assert isinstance(fan_in, dict)
    assert isinstance(backends, dict)

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = PROMETHEUS_SAMPLE_RE.match(line)
        if not match:
            continue
        metric_name, label_text, raw_value = match.groups()
        try:
            value = float(raw_value)
        except ValueError:
            continue
        labels = parse_prometheus_labels(label_text or "")
        if metric_name == "pool_template_fanin_enabled":
            fan_in["enabled"] = value > 0
            fan_in["enabled_value"] = value
        elif metric_name == "pool_template_fanin_backends":
            fan_in["backends"] = int(value) if value.is_integer() else value
        elif metric_name == "pool_template_fanin_mode":
            mode = labels.get("mode")
            if mode:
                modes = fan_in.setdefault("modes", {})
                if isinstance(modes, dict):
                    modes[mode] = value
                if value > 0:
                    fan_in["mode"] = mode
        elif metric_name == "pool_template_fanin_config_info":
            if value > 0:
                for key in (
                    "config_id",
                    "effective_mode",
                    "configured_mode",
                    "configured_backends",
                    "participant_backends",
                    "max_backends",
                    "reject_lag_blocks",
                    "accept_same_height",
                    "alt_takeover_min_age_ms",
                    "alt_takeover_lead_blocks",
                    "failover_template_max_age_ms",
                ):
                    if key in labels:
                        fan_in[key] = labels[key]
        elif metric_name in {
            "pool_rpc_backend_selected",
            "pool_rpc_backend_healthy",
            "pool_rpc_backend_score",
            "pool_rpc_backend_template_age_seconds",
            "pool_rpc_backend_ws_connected",
            "pool_template_fanin_backend_participant",
            "pool_template_fanin_backend_role",
            "pool_template_fanin_winner",
            "pool_template_fanin_best_height",
            "pool_template_fanin_observed_height",
        }:
            backend = labels.get("backend")
            if not backend:
                continue
            row = backends.setdefault(backend, {})
            if not isinstance(row, dict):
                continue
            if metric_name == "pool_rpc_backend_selected":
                row["selected"] = value > 0
                if value > 0:
                    state["selected_backend"] = backend
            elif metric_name == "pool_rpc_backend_healthy":
                row["healthy"] = value > 0
            elif metric_name == "pool_rpc_backend_score":
                row["score"] = value
            elif metric_name == "pool_rpc_backend_template_age_seconds":
                row["template_age_seconds"] = round(value, 3)
            elif metric_name == "pool_rpc_backend_ws_connected":
                row["ws_connected"] = value > 0
            elif metric_name == "pool_template_fanin_backend_participant":
                row["fan_in_participant"] = value > 0
            elif metric_name == "pool_template_fanin_backend_role":
                role = labels.get("role")
                if role and value > 0:
                    row["fan_in_role"] = role
            elif metric_name == "pool_template_fanin_winner":
                row["fan_in_winner"] = value > 0
            elif metric_name == "pool_template_fanin_best_height":
                row["fan_in_best_height"] = int(value) if value.is_integer() else value
            elif metric_name == "pool_template_fanin_observed_height":
                row["fan_in_observed_height"] = int(value) if value.is_integer() else value

    if backends:
        state["backend_count"] = len(backends)
        state["healthy_backend_count"] = sum(
            1 for row in backends.values() if isinstance(row, dict) and row.get("healthy") is True
        )
    return state


def collect_template_backend_states(endpoints: list[str]) -> tuple[list[dict[str, object]], list[str]]:
    states: list[dict[str, object]] = []
    errors: list[str] = []
    for endpoint in endpoints:
        url = f"http://{endpoint}/metrics"
        request = Request(url, headers={"accept": "text/plain", "user-agent": "BDAGDashboard/1.0"})
        try:
            with urlopen(request, timeout=DASHBOARD_POOL_METRICS_TIMEOUT) as response:
                metrics_text = response.read(1024 * 1024).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - advisory dashboard enrichment only.
            errors.append(f"{endpoint}: {exc}")
            continue
        state = template_backend_state_from_metrics(metrics_text, endpoint)
        if state.get("fan_in") or state.get("backends"):
            states.append(state)
    return states, errors


def enrich_status_with_template_backend_state(payload: dict[str, object]) -> dict[str, object]:
    pool_metrics = payload.get("pool_metrics")
    if not isinstance(pool_metrics, dict):
        return payload
    containers = pool_metrics.get("containers")
    if not isinstance(containers, dict):
        return payload

    endpoints: list[str] = []
    for info in containers.values():
        if not isinstance(info, dict):
            continue
        endpoint = str(info.get("endpoint") or "").strip()
        if not endpoint:
            continue
        endpoints.append(endpoint)
    if not endpoints:
        return payload

    cache_key = "template_backend_state:" + ",".join(sorted(endpoints))
    states, errors = cached_payload(
        cache_key,
        TEMPLATE_BACKEND_STATE_CACHE_SECONDS,
        lambda: collect_template_backend_states(endpoints),
    )

    if states:
        pool_metrics["template_backend_state"] = states[0] if len(states) == 1 else {"pools": states}
    if errors:
        pool_metrics["template_backend_state_error"] = "; ".join(errors[:2])
    return payload


def dashboard_status_payload() -> dict[str, object]:
    return enrich_status_with_template_backend_state(enrich_status_with_sync_estimate(collect_status(include_logs=True)))


def token_required() -> bool:
    if REQUIRE_TOKEN.lower() in {"1", "true", "yes"}:
        return True
    if REQUIRE_TOKEN.lower() in {"0", "false", "no"}:
        return False
    return HOST not in {"127.0.0.1", "localhost", "::1"}


def get_action_token() -> str:
    ensure_runtime()
    global ACTION_TOKEN
    if ACTION_TOKEN:
        return ACTION_TOKEN
    path = RUNTIME_DIR / "dashboard-token.txt"
    if path.exists():
        ACTION_TOKEN = path.read_text(encoding="utf-8").strip()
        return ACTION_TOKEN
    ACTION_TOKEN = secrets.token_urlsafe(24)
    path.write_text(ACTION_TOKEN + "\n", encoding="utf-8")
    path.chmod(0o600)
    return ACTION_TOKEN


def collect_sampler_status() -> dict[str, object]:
    now = int(time.time())
    threshold = EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS * 3
    info = read_latest_earnings_snapshot_info()
    latest_epoch = info.get("latest_epoch")
    latest_age = int(now - float(latest_epoch)) if latest_epoch is not None else None
    stale = latest_age is None or latest_age > threshold
    if stale:
        status = "stale"
        reason = "The earnings/miner plot sampler has not written a fresh valid snapshot."
    elif info.get("latest_at"):
        status = "ok"
        reason = ""
    else:
        status = "missing"
        reason = "No valid earnings/miner plot snapshot has been recorded yet."
    return {
        "generated_at": now_iso(),
        "status": status,
        "stale": stale,
        "reason": reason,
        "latest_at": info.get("latest_at"),
        "latest_age_seconds": latest_age,
        "expected_interval_seconds": EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS,
        "stale_threshold_seconds": threshold,
        "snapshot_info": info,
    }


def start_background_action(name: str, command: list[str], reason: str) -> dict[str, str]:
    ensure_runtime()
    log_path = RUNTIME_DIR / "logs" / f"dashboard-{name}-{int(time.time())}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "name": name,
        "reason": reason,
        "status": "running",
        "started_at": now_iso(),
        "finished_at": None,
        "log_path": str(log_path),
    }
    write_action_state(state)

    def runner() -> None:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"[{now_iso()}] $ {' '.join(command)}\n")
            log.flush()
            started = time.time()
            proc = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
            elapsed = round(time.time() - started, 3)
            log.write(f"\n[{now_iso()}] exit={proc.returncode} elapsed={elapsed}s\n")
        state.update(
            {
                "status": "ok" if proc.returncode == 0 else "failed",
                "finished_at": now_iso(),
                "elapsed": elapsed,
            }
        )
        write_action_state(state)

    threading.Thread(target=runner, daemon=True).start()
    return {"status": "started", "log_path": str(log_path)}


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BlockDAG Pool Operations</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f8;
      --panel: #ffffff;
      --panel-alt: #f8fafc;
      --line: #d7dbe0;
      --text: #16202a;
      --muted: #617181;
      --ok: #197a46;
      --warn: #a45b00;
      --down: #b3261e;
      --sync: #1d5f99;
      --button: #1c2b36;
      --button-text: #ffffff;
      --button-secondary-bg: #ffffff;
      --input-bg: #ffffff;
      --chart-bg: #fbfcfd;
      --pre-bg: #101820;
      --pre-text: #dfe7ef;
      --progress-bg: #e5e9ee;
      --shadow: rgba(0,0,0,0.05);
    }
    :root[data-theme="dark"] {
      color-scheme: dark;
      --bg: #0e141b;
      --panel: #151d26;
      --panel-alt: #111923;
      --line: #2b3948;
      --text: #e7edf4;
      --muted: #9aaaba;
      --ok: #48c684;
      --warn: #f2ae49;
      --down: #ff746c;
      --sync: #6cb7ff;
      --button: #d9e4ef;
      --button-text: #101820;
      --button-secondary-bg: #1b2632;
      --input-bg: #101820;
      --chart-bg: #101820;
      --pre-bg: #070b10;
      --pre-text: #dce7f2;
      --progress-bg: #22303d;
      --shadow: rgba(0,0,0,0.35);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }
    h1 { margin: 0; font-size: 20px; font-weight: 700; }
    main {
      padding: 18px 24px 28px;
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 16px;
      width: 100%;
      min-width: 0;
    }
    .toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: flex-end; }
    .tabs {
      display: flex;
      gap: 8px;
      padding: 10px 24px 0;
      background: var(--bg);
    }
    .tab-button {
      background: transparent;
      border-color: transparent;
      color: var(--muted);
      border-radius: 6px 6px 0 0;
    }
    .tab-button.active {
      background: var(--panel);
      border-color: var(--line);
      border-bottom-color: var(--panel);
      color: var(--text);
    }
    .tab-page {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 16px;
      width: 100%;
      min-width: 0;
    }
    .hidden { display: none; }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 16px; }
    .status-overview {
      display: grid;
      grid-template-columns: minmax(280px, 0.8fr) minmax(0, 2.2fr);
      gap: 16px;
      width: 100%;
      min-width: 0;
      justify-self: stretch;
      align-items: stretch;
    }
    .status-overview .panel,
    .status-overview .node-card-grid {
      grid-column: auto;
    }
    .status-overview .node-card-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      align-content: start;
      align-self: stretch;
      justify-self: stretch;
      width: 100%;
    }
    .status-overview .node-card-group {
      display: contents;
    }
    .status-overview .node-card-group-title {
      grid-column: 1 / -1;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-width: 0;
    }
    .span-2 { grid-column: span 2; }
    .span-3 { grid-column: span 3; }
    .span-4 { grid-column: span 4; }
    .span-6 { grid-column: span 6; }
    .span-8 { grid-column: span 8; }
    .span-12 { grid-column: span 12; }
    .kpi-label { color: var(--muted); font-size: 12px; text-transform: uppercase; font-weight: 700; }
    .stack-endpoint {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 10px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .stack-endpoint-value {
      color: var(--text);
      font-variant-numeric: tabular-nums;
      text-transform: none;
      white-space: nowrap;
    }
    .kpi-value {
      margin-top: 8px;
      font-size: 24px;
      font-weight: 750;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .status-reason {
      margin-top: 6px;
      min-height: 17px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .sampler-alert {
      margin-top: 10px;
      padding: 10px 12px;
      border: 1px solid rgba(201, 90, 0, 0.35);
      border-left: 4px solid var(--warn);
      border-radius: 6px;
      background: rgba(201, 90, 0, 0.08);
      color: var(--text);
      font-size: 13px;
      line-height: 1.4;
    }
    .subtle { color: var(--muted); font-size: 13px; }
    .ok { color: var(--ok); }
    .syncing { color: var(--sync); }
    .down { color: var(--down); }
    .warn { color: var(--warn); }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; }
    th, td { padding: 9px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; font-size: 13px; overflow-wrap: anywhere; }
    th { color: var(--muted); font-size: 12px; text-transform: uppercase; }
    tr:last-child td { border-bottom: 0; }
    button {
      border: 1px solid var(--button);
      background: var(--button);
      color: var(--button-text);
      border-radius: 6px;
      padding: 9px 12px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      min-height: 36px;
    }
    button.secondary { background: var(--button-secondary-bg); color: var(--button); }
    button.danger { background: var(--down); border-color: var(--down); }
    button:disabled { opacity: 0.55; cursor: wait; }
    input {
      border: 1px solid var(--line);
      background: var(--input-bg);
      color: var(--text);
      border-radius: 6px;
      padding: 9px 10px;
      min-height: 36px;
      min-width: 220px;
      font: inherit;
    }
    label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; }
    label input { color: var(--text); font-size: 13px; font-weight: 400; text-transform: none; }
    input[type="checkbox"] { min-width: 0; min-height: 0; width: 16px; height: 16px; padding: 0; }
    .form-grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 12px; align-items: end; }
    .field-span-2 { grid-column: span 2; }
    .field-span-3 { grid-column: span 3; }
    .field-span-4 { grid-column: span 4; }
    .field-span-6 { grid-column: span 6; }
    .button-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; }
    .checkbox-cell { width: 42px; }
    .right { text-align: right; }
    .nowrap { white-space: nowrap; }
    .table-scroll { overflow-x: auto; }
    .wide-table {
      width: max-content;
      min-width: 100%;
      table-layout: auto;
    }
    .chart-wrap {
      height: 280px;
      margin-top: 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--chart-bg);
      overflow: hidden;
    }
    .chart-head {
      display: flex;
      gap: 12px;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      margin-top: 8px;
    }
    .chart-controls {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .range-button.active,
    .global-range-button.active {
      background: var(--button);
      color: white;
    }
    canvas { display: block; width: 100%; height: 100%; }
    .chart-legend { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 8px; }
    .legend-key { display: inline-flex; gap: 6px; align-items: center; color: var(--muted); font-size: 12px; }
    .legend-key::before { content: ""; width: 10px; height: 10px; border-radius: 2px; background: var(--key-color); }
    .miner-row { background: var(--miner-row-color, transparent); }
    .pool-row { background: var(--pool-row-color, transparent); }
    .miner-dot {
      display: inline-block;
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--miner-color);
      margin-right: 8px;
      vertical-align: middle;
    }
    .pool-dot {
      display: inline-block;
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--pool-color);
      margin-right: 8px;
      vertical-align: middle;
    }
    .miner-name,
    .pool-name {
      font-weight: 700;
      color: var(--text);
    }
    pre {
      margin: 8px 0 0;
      padding: 12px;
      background: var(--pre-bg);
      color: var(--pre-text);
      border-radius: 6px;
      overflow: auto;
      max-height: 360px;
      font-size: 12px;
      line-height: 1.45;
    }
    .list { margin: 8px 0 0; padding-left: 18px; }
    .list li { margin: 4px 0; }
    .status-dot {
      display: inline-block;
      width: 9px;
      height: 9px;
      border-radius: 50%;
      margin-right: 6px;
      background: var(--muted);
    }
    .status-dot.ok { background: var(--ok); }
    .status-dot.syncing { background: var(--sync); }
    .status-dot.down { background: var(--down); }
    .sync-progress { margin-top: 12px; }
    .sync-progress-bar {
      height: 12px;
      border-radius: 999px;
      overflow: hidden;
      background: var(--progress-bg);
      border: 1px solid var(--line);
      box-shadow: inset 0 1px 2px var(--shadow);
    }
    .sync-progress-fill {
      height: 100%;
      width: 0%;
      border-radius: inherit;
      background: linear-gradient(90deg, #f5a623, #21a366);
      transition: width 350ms ease;
    }
    .sync-progress-meta {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      margin-top: 6px;
      font-size: 12px;
      color: var(--muted);
      flex-wrap: wrap;
    }
    .sync-narrative {
      margin-top: 10px;
      padding-top: 10px;
      border-top: 1px solid var(--line);
      color: var(--text);
      font-size: 13px;
      line-height: 1.4;
      overflow-wrap: anywhere;
    }
    .sync-detail-list {
      display: grid;
      grid-template-columns: max-content minmax(0, 1fr);
      gap: 5px 12px;
      margin-top: 10px;
      font-size: 12px;
      line-height: 1.35;
    }
    .sync-detail-list .label {
      color: var(--muted);
      font-weight: 700;
      text-transform: uppercase;
    }
    .sync-detail-list .value {
      color: var(--text);
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .sync-paused-note {
      color: var(--warn);
      font-size: 12px;
      font-weight: 700;
      line-height: 1.35;
    }
    .sync-progress-node {
      margin-top: 10px;
    }
    .sync-progress-node .sync-progress-bar {
      height: 8px;
    }
    .node-card-grid {
      grid-column: span 12;
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 12px;
      width: 100%;
      min-width: 0;
      justify-self: stretch;
    }
    .node-card-group {
      grid-column: 1 / -1;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr));
      gap: 12px;
      width: 100%;
      min-width: 0;
      justify-self: stretch;
    }
    .node-card-group-title {
      grid-column: 1 / -1;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0;
      text-transform: uppercase;
      color: var(--muted);
    }
    .node-card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-width: 0;
      width: 100%;
    }
    .node-card.observer {
      background: var(--panel-alt);
      border-style: dashed;
    }
    .node-card.observer .kpi-label::after {
      content: "not routed";
      display: inline-flex;
      align-items: center;
      margin-left: 6px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 1px 7px;
      font-size: 11px;
      color: var(--muted);
      text-transform: none;
      white-space: nowrap;
    }
    .node-card .kpi-value {
      font-size: 22px;
    }
    .node-card-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      min-width: 0;
    }
    .node-card-title {
      min-width: 0;
      overflow-wrap: anywhere;
      line-height: 1.35;
    }
    .node-badges {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 6px;
      min-width: 0;
    }
    .node-role {
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 1px 7px;
      margin-left: 6px;
      font-size: 11px;
      color: var(--muted);
      text-transform: capitalize;
      white-space: nowrap;
    }
    .node-badges .node-role {
      margin-left: 0;
    }
    .node-log-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
      margin-top: 10px;
    }
    .node-log-block {
      min-width: 0;
    }
    .node-log-block pre {
      max-height: 260px;
    }
    @media (max-width: 900px) {
      header { align-items: flex-start; flex-direction: column; }
      .toolbar { justify-content: flex-start; }
      .status-overview { grid-template-columns: minmax(0, 1fr); }
      .status-overview .node-card-grid {
        grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr));
      }
      .span-2, .span-3, .span-4, .span-6, .span-8, .node-card-grid { grid-column: span 12; }
      .field-span-2, .field-span-3, .field-span-4, .field-span-6 { grid-column: span 12; }
      main { padding: 14px; }
      .tabs { padding-left: 14px; padding-right: 14px; }
      input { min-width: 100%; }
      input[type="checkbox"] { min-width: 0; }
    }
  </style>
  <script>
    (() => {
      const stored = localStorage.getItem("bdag-dashboard-theme");
      const theme = stored || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      document.documentElement.dataset.theme = theme;
    })();
  </script>
</head>
<body>
  <header>
    <div>
      <h1>BlockDAG Pool Operations</h1>
      <div class="subtle" id="meta">Loading...</div>
    </div>
    <div class="toolbar">
      <input id="token" type="password" placeholder="Action token">
      <button id="themeToggle" class="secondary" type="button" onclick="toggleTheme()">Dark</button>
      <button class="secondary" onclick="refresh()">Refresh</button>
      <button onclick="action('start')">Start</button>
      <button onclick="action('restart')">Restart</button>
      <button class="danger" onclick="action('clean_restore')">Clean Restore</button>
      <button class="secondary" onclick="action('handoff')">Codex Handoff</button>
    </div>
  </header>
  <nav class="tabs">
    <button id="tabButton-status" class="tab-button active" onclick="showTab('status')">Pool Status</button>
    <button id="tabButton-miners" class="tab-button" onclick="showTab('miners')">Miners</button>
    <button id="tabButton-global" class="tab-button" onclick="showTab('global')">Global</button>
    <button id="tabButton-earnings" class="tab-button" onclick="showTab('earnings')">Earnings</button>
  </nav>
  <main>
    <section id="tab-status" class="tab-page">
    <section class="status-overview">
      <div class="panel">
        <div class="stack-endpoint">
          <span>Pool Endpoint</span>
          <span class="stack-endpoint-value" id="poolEndpoint">...</span>
        </div>
        <div class="kpi-label">Stack</div>
        <div class="kpi-value" id="overall">...</div>
        <div class="status-reason" id="statusReason"></div>
        <div class="sync-progress">
          <div class="sync-progress-bar" title="Node EVM sync progress">
            <div class="sync-progress-fill" id="syncProgressFill"></div>
          </div>
          <div class="sync-progress-meta">
            <span id="syncProgressPercent">...</span>
            <span id="syncProgressGap">...</span>
          </div>
        </div>
        <div id="syncNarrative" class="sync-narrative"></div>
        <div class="sync-detail-list">
          <span class="label">Mode</span><span class="value" id="syncMode">...</span>
          <span class="label">Active</span><span class="value" id="syncActiveNode">...</span>
          <span class="label">Rate</span><span class="value" id="syncRate">...</span>
          <span class="label">ETA</span><span class="value" id="syncEta">...</span>
          <span class="label">Next</span><span class="value" id="syncNextStep">...</span>
        </div>
      </div>
      <div id="nodeCards" class="node-card-grid"></div>
    </section>
    <section class="grid">
      <div class="panel span-8">
        <div class="kpi-label">Containers</div>
        <table>
          <thead><tr><th>Name</th><th>Status</th><th>Image</th><th>Restarts</th></tr></thead>
          <tbody id="containers"></tbody>
        </table>
      </div>
      <div class="panel span-4">
        <div class="kpi-label">Alerts</div>
        <ul class="list" id="alerts"></ul>
      </div>
    </section>
    <section class="grid">
      <div class="panel span-12">
        <div class="kpi-label">Node Logs</div>
        <div id="nodeLogsGrid" class="node-log-grid"></div>
      </div>
    </section>
    <section class="grid">
      <div class="panel span-6">
        <div class="kpi-label">Pool</div>
        <div id="poolSummary" class="subtle"></div>
        <pre id="poolLog"></pre>
      </div>
      <div class="panel span-6">
        <div class="kpi-label">Latest Action</div>
        <pre id="actionLog"></pre>
      </div>
    </section>
    </section>
    <section id="tab-miners" class="tab-page hidden">
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">Tracked Miner Health</div>
          <div id="minerHealthSummary" class="subtle" style="margin-top: 8px;"></div>
          <div class="table-scroll">
          <table class="wide-table">
            <thead><tr><th class="nowrap">Miner</th><th class="nowrap">Type</th><th>Status</th><th>Configured</th><th>Connected</th><th class="nowrap">Workers</th><th class="right">Shares</th><th class="right">Work %</th><th class="right">Work</th><th class="right">Found Blocks</th><th>Last Share</th><th>Issue</th></tr></thead>
            <tbody id="managedMinersTable"></tbody>
          </table>
          </div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">Miner Performance Trend</div>
          <div class="subtle" id="minerWorkChartMetricSummary" style="margin-top: 8px;">Accepted work percentage by miner</div>
          <div class="chart-head">
            <div class="chart-controls">
              <button class="secondary range-button miner-work-range-button active" data-range="1" onclick="setMinerWorkChartRange(1)">1h</button>
              <button class="secondary range-button miner-work-range-button" data-range="4" onclick="setMinerWorkChartRange(4)">4h</button>
              <button class="secondary range-button miner-work-range-button" data-range="12" onclick="setMinerWorkChartRange(12)">12h</button>
              <button class="secondary range-button miner-work-range-button" data-range="24" onclick="setMinerWorkChartRange(24)">24h</button>
              <button class="secondary range-button miner-work-range-button" data-range="72" onclick="setMinerWorkChartRange(72)">3d</button>
              <button class="secondary range-button miner-work-range-button" data-range="168" onclick="setMinerWorkChartRange(168)">Week</button>
              <button class="secondary range-button miner-work-range-button" data-range="720" onclick="setMinerWorkChartRange(720)">Month</button>
              <button class="secondary range-button miner-work-metric-button active" data-metric="work" onclick="setMinerWorkChartMetric('work')">Work %</button>
              <button class="secondary range-button miner-work-metric-button" data-metric="blocks" onclick="setMinerWorkChartMetric('blocks')">Blocks</button>
              <button class="secondary range-button miner-work-metric-button" data-metric="hashrate" onclick="setMinerWorkChartMetric('hashrate')">Hashrate</button>
            </div>
            <div class="subtle" id="minerWorkChartRangeLabel"></div>
          </div>
          <div id="minerWorkSamplerAlert" class="sampler-alert hidden"></div>
          <div class="chart-wrap"><canvas id="minerWorkChart"></canvas></div>
          <div class="chart-legend" id="minerWorkChartLegend"></div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">LAN Miner Configuration</div>
          <div class="form-grid" style="margin-top: 12px;">
            <label class="field-span-3">Scan Target
              <input id="minerScanTarget" placeholder="192.168.1.0/24">
            </label>
            <label class="field-span-3">Pool URL
              <input id="minerPoolUrl" placeholder="stratum+tcp://POOL_LAN_IP:3334">
            </label>
            <label class="field-span-3">Worker / Wallet
              <input id="minerWorkerUser" placeholder="0x...">
            </label>
            <label class="field-span-2">Pool Password
              <input id="minerPoolPassword" value="1234">
            </label>
            <label class="field-span-3">Admin Password
              <input id="minerAdminPassword" type="password" autocomplete="off" placeholder="ASIC admin password">
            </label>
            <div class="field-span-6 button-row">
              <button onclick="scanMinerLan()">Scan LAN</button>
              <button class="secondary" onclick="selectAllMiners(true)">Select All</button>
              <button class="secondary" onclick="selectAllMiners(false)">Clear</button>
              <button onclick="configureSelectedMiners()">Configure Selected</button>
              <button class="secondary" onclick="saveMinerAuth()">Save Password For Watchdog</button>
            </div>
          </div>
          <div class="subtle" style="margin-top: 10px;">Scans are limited to private LAN IPv4 targets. Existing miner pool lists are backed up before changes.</div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">Discovered Miners</div>
          <table>
            <thead><tr><th class="checkbox-cell"></th><th>Host</th><th>Model</th><th>Firmware</th><th>Current Pool</th><th>Active</th><th>Result</th></tr></thead>
            <tbody id="minersTable"></tbody>
          </table>
          <pre id="minersOutput">No scan has run yet.</pre>
        </div>
      </section>
    </section>
    <section id="tab-global" class="tab-page hidden">
      <section class="grid">
        <div class="panel span-2">
          <div class="kpi-label">Latest Block</div>
          <div class="kpi-value" id="globalLatestBlock">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Scanned Blocks</div>
          <div class="kpi-value" id="globalScannedBlocks">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Unique Miners</div>
          <div class="kpi-value" id="globalUniqueMiners">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Scan Window</div>
          <div class="kpi-value" id="globalScanWindow">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Avg Block Sec</div>
          <div class="kpi-value" id="globalAvgBlockSec">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Top Share</div>
          <div class="kpi-value" id="globalTopShare">...</div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">Estimated Earnings By Pool</div>
          <div class="subtle" style="margin-top: 8px;">Pool addresses are clustered from recent block headers and rendered like miner rows for consistency.</div>
          <div class="table-scroll" style="margin-top: 12px;">
            <table class="wide-table">
              <thead><tr><th class="nowrap">Pool</th><th class="nowrap">Nodes</th><th class="right">Shares</th><th class="right">Work %</th><th class="right">Credit Blocks</th><th class="right">Credited BDAG</th><th class="right">Found Blocks</th><th class="right">Est. Wallet BDAG</th><th class="right">Avg USD/h</th><th class="right">Wallet Avg BDAG/h</th><th class="right">USD Total</th><th class="right">ZAR Total</th><th>Last Seen</th></tr></thead>
              <tbody id="globalPoolsTable"></tbody>
            </table>
          </div>
          <div class="subtle" style="margin-top: 10px;">Per-pool earnings are estimated from recent block production share and current reward pricing.</div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">Pool Earnings Trend</div>
          <div class="subtle" id="globalChartMetricSummary" style="margin-top: 8px;">USD per pool per hour</div>
          <div class="chart-head">
            <div class="chart-controls">
              <button class="secondary global-range-button active" data-range="1" onclick="setGlobalChartRange(1)">1h</button>
              <button class="secondary global-range-button" data-range="4" onclick="setGlobalChartRange(4)">4h</button>
              <button class="secondary global-range-button" data-range="12" onclick="setGlobalChartRange(12)">12h</button>
              <button class="secondary global-range-button" data-range="24" onclick="setGlobalChartRange(24)">24h</button>
              <button class="secondary global-range-button" data-range="72" onclick="setGlobalChartRange(72)">3d</button>
              <button class="secondary global-range-button" data-range="168" onclick="setGlobalChartRange(168)">Week</button>
              <button class="secondary global-range-button" data-range="720" onclick="setGlobalChartRange(720)">Month</button>
              <button class="secondary range-button global-metric-button active" data-metric="usd" onclick="setGlobalChartMetric('usd')">USD/h</button>
              <button class="secondary range-button global-metric-button" data-metric="blocks" onclick="setGlobalChartMetric('blocks')">Blocks/h</button>
            </div>
            <div class="subtle" id="globalChartRangeLabel"></div>
          </div>
          <div class="chart-wrap"><canvas id="globalChart"></canvas></div>
          <div class="chart-legend" id="globalChartLegend"></div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">Observed Peer IPs</div>
          <div class="subtle" style="margin-top: 8px;">These are public P2P peers seen on the node sockets, geolocated by IP. They may be relays, VPS hosts, or NAT gateways rather than the physical miners.</div>
          <div class="table-scroll" style="margin-top: 12px;">
            <table class="wide-table">
              <thead><tr><th class="nowrap">IP</th><th>Guessed Location</th><th>Country</th><th>Region</th><th>City</th><th>ASN</th><th>Org</th><th class="right">Seen By</th></tr></thead>
              <tbody id="globalPeerIpsTable"></tbody>
            </table>
          </div>
        </div>
      </section>
    </section>
    <section id="tab-earnings" class="tab-page hidden">
      <section class="grid">
        <div class="panel span-2">
          <div class="kpi-label">Current Price ZAR</div>
          <div class="kpi-value" id="earnCurrentPriceZar">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">24h Avg BDAG/h</div>
          <div class="kpi-value" id="earnWalletAvgBdagHour">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Recent Earned BDAG/h</div>
          <div class="kpi-value" id="earnWalletRecentBdagHour">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">24h Earned ZAR</div>
          <div class="kpi-value" id="earnWallet24hZar">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">24h Earned USD</div>
          <div class="kpi-value" id="earnWallet24hUsd">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">24h Earned BDAG</div>
          <div class="kpi-value" id="earnWallet24hBdag">...</div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-2">
          <div class="kpi-label">Current Price USD</div>
          <div class="kpi-value" id="earnCurrentPriceUsd">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Avg Income USD/h</div>
          <div class="kpi-value" id="earnAvgIncomeUsdHour">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Avg Income BDAG/h</div>
          <div class="kpi-value" id="earnAvgIncomeBdagHour">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Payment Wallet ZAR</div>
          <div class="kpi-value" id="earnTotalZar">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Payment Wallet USD</div>
          <div class="kpi-value" id="earnTotalUsd">...</div>
        </div>
        <div class="panel span-2">
          <div class="kpi-label">Payment Wallet BDAG</div>
          <div class="kpi-value" id="earnWalletBdag">...</div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">Estimated Earnings By Miner</div>
          <div class="table-scroll">
            <table class="wide-table">
              <thead><tr><th class="nowrap">Miner</th><th class="nowrap">Workers</th><th class="right">Shares</th><th class="right">Work %</th><th class="right">Credit Blocks</th><th class="right">Credited BDAG</th><th class="right">Found Blocks</th><th class="right">Est. Wallet BDAG</th><th class="right">Wallet Recent BDAG/h</th><th class="right">Wallet Avg BDAG/h</th><th class="right">USD Total</th><th class="right">ZAR Total</th><th>Last Share</th></tr></thead>
              <tbody id="minerEarningsTable"></tbody>
            </table>
          </div>
          <div class="subtle" style="margin-top: 10px;">Per-miner wallet BDAG is estimated from accepted share work because rewards land at the wallet/worker address, not directly against each ASIC IP. Worker credits shared across miners.</div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-12">
          <div class="kpi-label">Miner Earnings Trend</div>
          <div class="subtle" id="earningsChartUnitSummary" style="margin-top: 8px;">USD per miner per hour</div>
          <div class="chart-head">
            <div class="chart-controls">
              <button class="secondary range-button earnings-range-button active" data-range="1" onclick="setEarningsChartRange(1)">1h</button>
              <button class="secondary range-button earnings-range-button" data-range="4" onclick="setEarningsChartRange(4)">4h</button>
              <button class="secondary range-button earnings-range-button" data-range="12" onclick="setEarningsChartRange(12)">12h</button>
              <button class="secondary range-button earnings-range-button" data-range="24" onclick="setEarningsChartRange(24)">24h</button>
              <button class="secondary range-button earnings-range-button" data-range="72" onclick="setEarningsChartRange(72)">3d</button>
              <button class="secondary range-button earnings-range-button" data-range="168" onclick="setEarningsChartRange(168)">Week</button>
              <button class="secondary range-button earnings-range-button" data-range="720" onclick="setEarningsChartRange(720)">Month</button>
              <button class="secondary range-button earnings-unit-button" data-unit="bdag" onclick="setEarningsChartUnit('bdag')">BDAG</button>
              <button class="secondary range-button earnings-unit-button active" data-unit="usd" onclick="setEarningsChartUnit('usd')">USD</button>
              <button class="secondary range-button earnings-unit-button" data-unit="zar" onclick="setEarningsChartUnit('zar')">ZAR</button>
            </div>
            <div class="subtle" id="earningsChartRangeLabel"></div>
          </div>
          <div id="earningsSamplerAlert" class="sampler-alert hidden"></div>
          <div class="chart-wrap"><canvas id="earningsChart"></canvas></div>
          <div class="chart-legend" id="earningsChartLegend"></div>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-6">
          <div class="kpi-label">Address Credits</div>
          <table>
            <thead><tr><th>Address</th><th class="right">Credits</th><th class="right">Total BDAG</th><th class="right">Pending</th><th>Last Credit</th></tr></thead>
            <tbody id="addressCreditsTable"></tbody>
          </table>
        </div>
        <div class="panel span-6">
          <div class="kpi-label">Payment Wallet Cross-Check</div>
          <table>
            <thead><tr><th>Source</th><th>Status</th><th class="right">BDAG</th><th>Detail</th></tr></thead>
            <tbody id="walletSourcesTable"></tbody>
          </table>
        </div>
      </section>
      <section class="grid">
        <div class="panel span-6">
          <div class="kpi-label">Price Feed</div>
          <pre id="priceFeedOutput"></pre>
        </div>
        <div class="panel span-6">
          <div class="kpi-label">Earnings Snapshot Log</div>
          <pre id="earningsHistoryOutput"></pre>
        </div>
      </section>
    </section>
  </main>
  <script>
    let busy = false;
    let miners = [];
    let minerResults = {};
    let minerDefaultsLoaded = false;
    let earningsLoaded = false;
    let lastEarningsData = null;
    let globalLoaded = false;
    let lastGlobalData = null;
    const defaultServiceOrder = ["pool-db", "bdag-miner-node-1", "bdag-miner-node-2", "rpc-failover", "asic-pool"];
    function text(id, value) { document.getElementById(id).textContent = value ?? ""; }
    function currentTheme() {
      return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
    }
    function setTheme(theme) {
      const normalized = theme === "dark" ? "dark" : "light";
      document.documentElement.dataset.theme = normalized;
      localStorage.setItem("bdag-dashboard-theme", normalized);
      const button = document.getElementById("themeToggle");
      if (button) {
        button.textContent = normalized === "dark" ? "Light" : "Dark";
        button.setAttribute("aria-pressed", normalized === "dark" ? "true" : "false");
      }
    }
    function toggleTheme() {
      setTheme(currentTheme() === "dark" ? "light" : "dark");
    }
    function fmt(value) { return value === null || value === undefined ? "n/a" : value.toLocaleString ? value.toLocaleString() : value; }
    function hasValue(value) { return value !== null && value !== undefined && value !== ""; }
    function firstPresent(...values) {
      for (const value of values) {
        if (hasValue(value)) return value;
      }
      return null;
    }
    function metricEnabled(value) {
      if (value === true || value === false) return value;
      const numeric = Number(value);
      if (Number.isFinite(numeric)) return numeric > 0;
      const textValue = String(value ?? "").toLowerCase();
      return ["true", "yes", "on", "enabled"].includes(textValue);
    }
    function templateBackendStates(data) {
      const metrics = data.pool_metrics || {};
      const rawState = metrics.template_backend_state || {};
      return Array.isArray(rawState.pools)
        ? rawState.pools
        : (rawState.fan_in || rawState.backends ? [rawState] : []);
    }
    function firstTemplateBackendState(data) {
      return templateBackendStates(data)[0] || {};
    }
    function backendKeyForNode(name) {
      const match = String(name || "").match(/(?:^|-)node-(\d+)$/) || String(name || "").match(/^node(\d+)$/);
      return match ? `node${match[1]}` : String(name || "");
    }
    function backendInfoForNode(name, backends) {
      const key = backendKeyForNode(name);
      return backends?.[name] || backends?.[key] || null;
    }
    function nodeRole(name, node, data) {
      if (node?.role) return String(node.role);
      const observers = data?.observer_node_services || [];
      return observers.includes(name) ? "observer" : "managed";
    }
    function nodeHealthScope(role) {
      return role === "observer" ? "advisory" : "production";
    }
    function templateBackendStatusText(data) {
      const metrics = data.pool_metrics || {};
      const state = firstTemplateBackendState(data);
      const fanIn = state.fan_in || {};
      const parts = [];
      const fanEnabled = firstPresent(fanIn.enabled, state.template_fanin_enabled, metrics.template_fanin_enabled);
      const fanBackends = firstPresent(fanIn.backends, state.template_fanin_backends, metrics.template_fanin_backends);
      const fanMode = firstPresent(fanIn.effective_mode, fanIn.mode);
      if (hasValue(fanEnabled) || hasValue(fanBackends)) {
        const enabledText = hasValue(fanEnabled) ? (metricEnabled(fanEnabled) ? "on" : "off") : "unknown";
        parts.push(`template_fanin=${enabledText}${hasValue(fanBackends) ? `(${fmt(fanBackends)})` : ""}${hasValue(fanMode) ? ` mode=${fanMode}` : ""}`);
      }

      const backends = state.backends || {};
      const backendNames = Object.keys(backends).sort();
      if (backendNames.length) {
        const healthy = backendNames.filter(name => metricEnabled(backends[name]?.healthy)).length;
        const wsBackends = backendNames.filter(name => metricEnabled(backends[name]?.ws_connected));
        parts.push(`template_backends=${healthy}/${backendNames.length}`);
        if (wsBackends.length) parts.push(`template_ws=${wsBackends.join(",")}`);
      } else {
        const probeNodes = data.rpc_template_health?.nodes || {};
        const probeNames = Object.keys(probeNodes).sort();
        if (probeNames.length) {
          const healthy = probeNames.filter(name => !probeNodes[name]?.failing).length;
          parts.push(`template_probes=${healthy}/${probeNames.length}`);
        }
      }
      return parts.join(" ");
    }
    function statusClass(overall) { return overall === "ok" ? "ok" : overall === "syncing" ? "syncing" : "down"; }
    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
    }
    function shortEth(value) {
      return String(value ?? "").replace(/0x[a-fA-F0-9]{40}/g, match => `${match.slice(0, 6)}...${match.slice(-4)}`);
    }
    function escapeShortEth(value) {
      return escapeHtml(shortEth(value));
    }
    const globalPoolNames = {};
    function globalPoolName(address) {
      return globalPoolNames[String(address || "").toLowerCase()] || "";
    }
    function globalPoolLabel(row) {
      const address = row?.address || row?.address_short || "";
      const name = row?.pool_name || globalPoolName(address);
      return name ? `${name} (${shortEth(address)})` : shortEth(address);
    }
    function globalNodesLabel(row) {
      const name = row?.pool_name || globalPoolName(row?.address);
      if (name) return name;
      return (row?.rpc_sources || []).join(", ");
    }
    function showTab(name) {
      for (const page of document.querySelectorAll(".tab-page")) page.classList.add("hidden");
      for (const button of document.querySelectorAll(".tab-button")) button.classList.remove("active");
      document.getElementById("tab-" + name).classList.remove("hidden");
      document.getElementById("tabButton-" + name).classList.add("active");
      if (name === "earnings") refreshEarnings();
      if (name === "miners") refreshEarnings();
      if (name === "global") refreshGlobal();
    }
    async function refresh() {
      try {
        const response = await fetch("/api/status", {cache: "no-store"});
        const data = await response.json();
        render(data);
      } catch (error) {
        text("meta", "Dashboard API unavailable: " + error);
        text("overall", "down");
        text("statusReason", "Dashboard API unavailable.");
        document.getElementById("overall").className = "kpi-value down";
      }
    }
    function render(data) {
      text("meta", data.generated_at + " | " + data.project_root);
      text("overall", data.overall);
      text("statusReason", data.overall === "ok" ? "" : (data.status_reason || "Reason unavailable."));
      document.getElementById("overall").className = "kpi-value " + statusClass(data.overall);
      const nodeNames = data.node_services || Object.keys(data.nodes || {});
      renderSyncProgress(data.sync_progress || {}, data);
      renderNodeCards(nodeNames, data.nodes || {}, data.sync_progress || {}, data);
      text("poolEndpoint", data.pool_endpoint || `127.0.0.1:${data.pool_port || "3334"}`);
      hydrateMinerDefaults(data);
      const tbody = document.getElementById("containers");
      tbody.innerHTML = "";
      const serviceOrder = data.stack_services || defaultServiceOrder;
      const extraServices = Object.keys(data.containers || {}).filter(name => !serviceOrder.includes(name));
      for (const name of [...serviceOrder, ...extraServices]) {
        const info = data.containers[name] || {};
        const tr = document.createElement("tr");
        const cls = info.running ? "ok" : "down";
        tr.innerHTML = `<td>${name}</td><td><span class="status-dot ${cls}"></span>${info.status || "missing"}</td><td>${info.image || ""}</td><td>${info.restart_count ?? ""}</td>`;
        tbody.appendChild(tr);
      }
      const alerts = document.getElementById("alerts");
      alerts.innerHTML = "";
      const messages = [...(data.failures || []), ...(data.warnings || [])];
      if (messages.length === 0) messages.push("No active alerts.");
      for (const message of messages) {
        const li = document.createElement("li");
        li.textContent = message;
        alerts.appendChild(li);
      }
      renderNodeLogs(nodeNames, data.nodes || {}, data);
      const poolHealth = data.pool_health || {};
      const submitRecovery = poolHealth.submit_stall_self_healed_recently
        ? `submit_recovery=self-healed accepted_age=${fmt(poolHealth.last_block_submit_age_seconds)}s`
        : (poolHealth.submit_stall_recovery_recent
          ? `submit_recovery=active recovery_age=${fmt(poolHealth.submit_stall_last_recovery_age_seconds)}s`
          : "submit_recovery=idle");
      const selectedBackend = poolHealth.selected_backend || data.pool_metrics?.selected_backend || "unknown";
      const templateBackendStatus = templateBackendStatusText(data);
      text(
        "poolSummary",
        `endpoint=${data.pool_endpoint || "unknown"} local_ips=${(data.local_ips || []).join(", ") || "none"} `
        + `initial_download=${data.pool.initial_download} gbt_errors=${data.pool.gbt_errors} rpc_refused=${data.pool.rpc_refused} `
        + `valid_shares=${fmt(poolHealth.valid_share_count)} stale_submits=${fmt(poolHealth.stale_submit_count)} `
        + `stale_jobs=${fmt(poolHealth.stale_job_candidate_count)} submit_errors=${fmt(poolHealth.block_submit_error_count)} `
        + `duplicate_blocks=${fmt(poolHealth.duplicate_block_count)} `
        + `last_valid_share_age=${fmt(poolHealth.last_valid_share_age_seconds)}s share_stall=${poolHealth.share_stall ? "yes" : "no"} `
        + `selected_backend=${selectedBackend}${templateBackendStatus ? ` ${templateBackendStatus}` : ""} ${submitRecovery}`
      );
      text("poolLog", (data.pool.tail || []).join("\n"));
      text("actionLog", data.latest_action ? JSON.stringify(data.latest_action, null, 2) : "No action has run yet.");
      renderManagedMiners(data.miner_health || {});
    }
    function syncProgressText(progress) {
      const percentValue = Number(progress.percent);
      const hasPercent = Number.isFinite(percentValue);
      const bounded = hasPercent ? Math.max(0, Math.min(100, percentValue)) : 0;
      if (progress.status === "synced") return `${bounded.toFixed(2)}% synced`;
      const remaining = Number(progress.remaining_blocks);
      const displayPercent = progress.status === "syncing" && Number.isFinite(remaining) && remaining > 0 && bounded >= 100
        ? 99.99
        : bounded;
      return hasPercent ? `${displayPercent.toFixed(2)}% ${progress.status || ""}` : `sync ${progress.status || "unknown"}`;
    }
    function syncGapText(progress) {
      if (progress.status === "synced") return "gap 0 blocks";
      if (progress.remaining_blocks !== null && progress.remaining_blocks !== undefined) {
        return `gap ${fmt(progress.remaining_blocks)} blocks`
          + (progress.current_block && progress.highest_block ? ` (${fmt(progress.current_block)} / ${fmt(progress.highest_block)})` : "");
      }
      return progress.error || "sync progress unavailable";
    }
    function durationText(seconds) {
      const value = Number(seconds);
      if (!Number.isFinite(value) || value < 0) return "estimating";
      if (value < 60) return "<1m";
      const minutes = Math.round(value / 60);
      if (minutes < 60) return `${minutes}m`;
      const hours = Math.floor(minutes / 60);
      const mins = minutes % 60;
      if (hours < 24) return mins ? `${hours}h ${mins}m` : `${hours}h`;
      const days = Math.floor(hours / 24);
      const remHours = hours % 24;
      return remHours ? `${days}d ${remHours}h` : `${days}d`;
    }
    function etaText(seconds, at) {
      const parsed = Number(seconds);
      if (!Number.isFinite(parsed) || parsed <= 0) return "estimating after the next progress sample";
      return `about ${durationText(parsed)}${at ? `, around ${formatDisplayTime(at)}` : ""}`;
    }
    function syncRateText(estimate) {
      const rate = Number(estimate?.rate_blocks_per_second);
      if (!Number.isFinite(rate) || rate <= 0) return "estimating from the next sample";
      const source = estimate.rate_source ? ` (${estimate.rate_source})` : "";
      return `${rate.toFixed(rate >= 10 ? 1 : 2)} blocks/s${source}`;
    }
    function renderSyncEstimate(data, progress) {
      const estimate = data.sync_estimate || {};
      const leader = estimate.leader || data.sync_health?.planned_pause_leader || "";
      const leaderNode = estimate.nodes?.[leader] || {};
      const remaining = firstPresent(leaderNode.remaining_blocks, estimate.remaining_blocks, progress.remaining_blocks);
      const current = firstPresent(leaderNode.current_block, progress.current_block);
      const highest = firstPresent(leaderNode.highest_block, progress.highest_block);
      const threshold = firstPresent(estimate.seed_threshold_blocks, data.sync_coordinator?.last_decision?.thresholds?.leader_near_tip_blocks, 5);
      text("syncNarrative", estimate.narrative || (progress.status === "synced" ? "Managed nodes are synced." : "Managed nodes are syncing."));
      text("syncMode", estimate.stage || progress.status || "unknown");
      text(
        "syncActiveNode",
        leader
          ? `${leader} ${fmt(current)} / ${fmt(highest)}; ${fmt(remaining)} block(s) remaining`
          : `${fmt(current)} / ${fmt(highest)}; ${fmt(remaining)} block(s) remaining`
      );
      text("syncRate", syncRateText(estimate));
      text("syncEta", etaText(estimate.eta_seconds, estimate.eta_at));
      if (estimate.mode === "leader_catchup" && estimate.paused_follower) {
        text(
          "syncNextStep",
          `copy ${leader || "leader"} data to ${estimate.paused_follower} when remaining is <= ${fmt(threshold)} block(s); seed ETA ${etaText(estimate.eta_to_seed_seconds, estimate.eta_to_seed_at)}`
        );
      } else if (progress.status === "synced") {
        text("syncNextStep", "pool can mine normally once backend template checks are healthy");
      } else {
        text("syncNextStep", "wait for nodes to finish syncing; the pool is holding mining jobs until backend sync is complete");
      }
    }
    function renderNodeSyncProgress(id, name, progress) {
      const nodeContainer = document.getElementById(id);
      nodeContainer.innerHTML = "";
      if (!name || !progress) return;
      nodeContainer.innerHTML = nodeSyncProgressHtml(name, progress);
    }
    function nodeSyncProgressHtml(name, progress, data = {}, node = {}) {
      const estimate = data.sync_estimate || {};
      if (node?.planned_sync_pause) {
        const leader = node.sync_pause_leader || estimate.leader || "leader";
        const leaderEstimate = estimate.nodes?.[leader] || {};
        const seedThreshold = firstPresent(estimate.seed_threshold_blocks, 5);
        return `
          <div class="sync-paused-note">Paused by sync coordinator while ${escapeHtml(leader)} catches up.</div>
          <div class="sync-progress-meta">
            <span>copy from ${escapeHtml(leader)} after sync</span>
            <span>seed at <= ${escapeHtml(fmt(seedThreshold))} remaining block(s)</span>
          </div>
          <div class="sync-progress-meta">
            <span>${escapeHtml(etaText(leaderEstimate.eta_to_seed_seconds, leaderEstimate.eta_to_seed_at))}</span>
          </div>`;
      }
      const nodePercent = Number(progress.percent);
      const nodeBounded = Number.isFinite(nodePercent) ? Math.max(0, Math.min(100, nodePercent)) : 0;
      const nodeEstimate = estimate.nodes?.[name] || {};
      const eta = nodeEstimate.eta_seconds ? ` | ETA ${etaText(nodeEstimate.eta_seconds, nodeEstimate.eta_at)}` : "";
      const rate = nodeEstimate.rate_blocks_per_second ? ` | ${syncRateText(nodeEstimate)}` : "";
      return `
        <div class="sync-progress-bar" title="${escapeHtml(name)} EVM sync progress">
          <div class="sync-progress-fill" style="width: ${nodeBounded}%"></div>
        </div>
        <div class="sync-progress-meta">
          <span>${escapeHtml(syncProgressText(progress))}</span>
          <span>${escapeHtml(syncGapText(progress))}</span>
        </div>
        <div class="sync-progress-meta">
          <span>${escapeHtml(`${eta}${rate}`.replace(/^ \\| /, ""))}</span>
        </div>`;
    }
    function renderSyncProgress(progress, data = {}) {
      const fill = document.getElementById("syncProgressFill");
      const percentValue = Number(progress.percent);
      const hasPercent = Number.isFinite(percentValue);
      const bounded = hasPercent ? Math.max(0, Math.min(100, percentValue)) : 0;
      fill.style.width = `${bounded}%`;
      text("syncProgressPercent", syncProgressText(progress));
      text("syncProgressGap", syncGapText(progress));
      renderSyncEstimate(data, progress);
    }
    function nodeSummaryText(node) {
      if (!node) return "node data unavailable";
      const chain = hasValue(node.chain_block_count) ? ` chain_blocks=${fmt(node.chain_block_count)} source=${node.chain_rpc_source || "getBlockCount"}` : " chain_blocks=n/a";
      return `child=${node.child_running}${chain} main_height=${fmt(node.chain_main_height)} best_main_order=${fmt(node.best_main_order)} import_age=${fmt(node.last_import_age_seconds)}s peer_ahead=${fmt(node.peer_ahead_blocks)} bad_peers=${fmt(node.invalid_peer_errors)} p2p_resets=${fmt(node.p2p_stream_errors)}`;
    }
    function nodeBlockHeight(name, node, syncNode, data) {
      if (node?.planned_sync_pause && !hasValue(node?.chain_block_count) && !hasValue(syncNode?.chain_block_count)) return "paused";
      return firstPresent(syncNode?.chain_block_count, node?.chain_block_count, null);
    }
    function renderNodeCards(nodeNames, nodes, syncProgress, data) {
      const container = document.getElementById("nodeCards");
      container.innerHTML = "";
      const syncNodes = syncProgress.nodes || {};
      const backendState = firstTemplateBackendState(data).backends || {};
      if (!nodeNames.length) {
        container.innerHTML = `<div class="node-card"><div class="kpi-label">Nodes</div><div class="kpi-value">n/a</div><div class="subtle">No node services reported.</div></div>`;
        return;
      }
      const managed = [];
      const observers = [];
      for (const name of nodeNames) {
        const node = nodes[name] || {};
        const roleValue = nodeRole(name, node, data);
        const healthScope = node.health_scope || nodeHealthScope(roleValue);
        const isObserver = roleValue === "observer" || healthScope === "advisory";
        (isObserver ? observers : managed).push({name, node, roleValue, healthScope});
      }
      function appendGroup(title, entries) {
        if (!entries.length) return;
        const group = document.createElement("div");
        group.className = "node-card-group";
        group.innerHTML = `<div class="node-card-group-title">${escapeHtml(title)}</div>`;
        for (const entry of entries) {
          const {name, node, roleValue, healthScope} = entry;
          const isObserver = roleValue === "observer" || healthScope === "advisory";
        const backend = backendInfoForNode(name, backendState) || {};
        const fanRole = backend.fan_in_role || (backend.selected ? "selected" : "");
        const roleHtml = `<span class="node-role">${escapeHtml(roleValue)}</span>`
          + (node?.planned_sync_pause ? `<span class="node-role">paused</span>` : "")
          + (fanRole ? `<span class="node-role">${escapeHtml(fanRole)}</span>` : "");
        const wsText = hasValue(backend.ws_connected) ? ` ws=${metricEnabled(backend.ws_connected) ? "on" : "off"}` : "";
        const templateAge = hasValue(backend.template_age_seconds) ? ` template_age=${fmt(backend.template_age_seconds)}s` : "";
        const syncNode = syncNodes[name] || {};
        const syncHtml = isObserver && !hasValue(syncNode.status)
          ? `<div class="subtle">Advisory observer; not included in production sync health.</div>`
          : nodeSyncProgressHtml(name, syncNode, data, node);
        const blockHeight = nodeBlockHeight(name, node, syncNode, data);
        const blockHeightText = hasValue(blockHeight) ? fmt(blockHeight) : "chain RPC unavailable";
        const div = document.createElement("div");
        div.className = `node-card${isObserver ? " observer" : ""}`;
        div.innerHTML = `
          <div class="node-card-head">
            <div class="kpi-label node-card-title">${escapeHtml(name)} Sync</div>
            <div class="node-badges">${roleHtml}</div>
          </div>
          <div class="kpi-value">${escapeHtml(blockHeightText)}</div>
          <div class="sync-progress sync-progress-node">${syncHtml}</div>
          <div class="subtle">${escapeHtml(healthScope)} scope | ${escapeHtml(nodeSummaryText(node))}${escapeHtml(templateAge + wsText)}</div>`;
          group.appendChild(div);
        }
        container.appendChild(group);
      }
      appendGroup("Managed production routing nodes", managed);
      appendGroup("Observer nodes - advisory only", observers);
    }
    function renderNodeLogs(nodeNames, nodes, data) {
      const container = document.getElementById("nodeLogsGrid");
      container.innerHTML = "";
      if (!nodeNames.length) {
        container.innerHTML = `<div class="subtle">No node logs available.</div>`;
        return;
      }
      for (const name of nodeNames) {
        const node = nodes[name] || {};
        const roleValue = nodeRole(name, node, data || {});
        const div = document.createElement("div");
        div.className = "node-log-block";
        div.innerHTML = `
          <div class="kpi-label">${escapeHtml(name)}<span class="node-role">${escapeHtml(roleValue)}</span></div>
          <div class="subtle">${escapeHtml(nodeSummaryText(node))}</div>
          <pre>${escapeHtml((node.tail || []).join("\\n"))}</pre>`;
        container.appendChild(div);
      }
    }
    function hydrateMinerDefaults(data) {
      if (minerDefaultsLoaded) return;
      const endpoint = data.pool_endpoint || `127.0.0.1:${data.pool_port || "3334"}`;
      const firstIp = (data.local_ips || [])[0] || "192.168.1.1";
      const parts = firstIp.split(".");
      if (!document.getElementById("minerScanTarget").value && parts.length === 4) {
        document.getElementById("minerScanTarget").value = `${parts[0]}.${parts[1]}.${parts[2]}.0/24`;
      }
      if (!document.getElementById("minerPoolUrl").value) document.getElementById("minerPoolUrl").value = `stratum+tcp://${endpoint}`;
      if (!document.getElementById("minerWorkerUser").value && data.mining_address) document.getElementById("minerWorkerUser").value = data.mining_address;
      minerDefaultsLoaded = true;
    }
    function renderMiners() {
      const tbody = document.getElementById("minersTable");
      tbody.innerHTML = "";
      if (!miners.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="7" class="subtle">No miners discovered yet.</td>`;
        tbody.appendChild(tr);
        return;
      }
      for (const miner of miners) {
        const result = minerResults[miner.ip];
        const pool = miner.current_pool || {};
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="checkbox-cell"><input type="checkbox" class="miner-select" value="${escapeHtml(miner.ip)}" checked></td>
          <td>${escapeHtml(minerShortIp(miner) || "")}</td>
          <td>${escapeHtml(miner.model || miner.hardware || "unknown")}</td>
          <td>${escapeHtml(miner.firmware || miner.mcbversion || "")}</td>
          <td>${escapeHtml(pool.url || "")}<br><span class="subtle">${escapeShortEth(pool.user || "")}</span></td>
          <td>${miner.active ? "yes" : "no"}</td>
          <td>${result ? escapeHtml(result.status + (result.error ? ": " + result.error : "")) : ""}</td>
        `;
        tbody.appendChild(tr);
      }
    }
    function selectedMinerIps() {
      return Array.from(document.querySelectorAll(".miner-select:checked")).map(input => input.value);
    }
    function selectAllMiners(checked) {
      for (const input of document.querySelectorAll(".miner-select")) input.checked = checked;
    }
    function renderManagedMiners(health) {
      const tbody = document.getElementById("managedMinersTable");
      if (!tbody) return;
      tbody.innerHTML = "";
      text("minerHealthSummary", `tracked=${fmt(health.tracked_count || 0)} connected=${fmt(health.connected_count || 0)} managed=${fmt(health.managed_count || 0)} ok=${fmt(health.ok_count || 0)} stratum=${fmt(health.stratum_count || 0)}`);
      const rows = health.miners || [];
      if (!rows.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="12" class="subtle">No tracked miners have been seen yet.</td>`;
        tbody.appendChild(tr);
        return;
      }
      for (const miner of rows) {
        const cls = miner.status === "ok" || miner.status === "connected" ? "ok" : miner.status === "degraded" ? "warn" : miner.status === "inactive" ? "syncing" : "down";
        const workers = (miner.workers || []).join(", ") || miner.expected_worker_user || "";
        const issue = miner.issue || miner.api_error || "";
        const identity = minerIdentity(miner);
        const color = minerColor(identity);
        const name = minerDisplayLabel(miner);
        const tr = document.createElement("tr");
        tr.className = "miner-row";
        tr.style.setProperty("--miner-row-color", transparentColor(color, 0.08));
        tr.style.setProperty("--miner-color", color);
        tr.innerHTML = `
          <td class="nowrap miner-name"><span class="miner-dot"></span>${escapeHtml(name)}</td>
          <td class="nowrap">${escapeHtml(miner.device_type || "unknown")}</td>
          <td class="${cls}">${escapeHtml(miner.status)}</td>
          <td>${miner.configured ? "yes" : "no"}</td>
          <td>${miner.connected || miner.pool_active ? "yes" : "no"}</td>
          <td class="nowrap">${escapeShortEth(workers)}</td>
          <td class="right">${fmt(miner.shares || 0)}</td>
          <td class="right">${escapeHtml(miner.work_percent || "0.00")}</td>
          <td class="right">${fmt(miner.share_work || 0)}</td>
          <td class="right">${fmt(miner.blocks_found || 0)}</td>
          <td>${escapeHtml(miner.last_share_at || "")}</td>
          <td>${escapeHtml(issue)}</td>
        `;
        tbody.appendChild(tr);
      }
    }
    async function scanMinerLan() {
      if (busy) return;
      busy = true;
      for (const btn of document.querySelectorAll("button")) btn.disabled = true;
      text("minersOutput", "Scanning LAN...");
      try {
        const response = await fetch("/api/miners/scan", {
          method: "POST",
          headers: {"content-type": "application/json"},
          body: JSON.stringify({target: document.getElementById("minerScanTarget").value, token: document.getElementById("token").value})
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "scan failed");
        miners = payload.miners || [];
        minerResults = {};
        renderMiners();
        text("minersOutput", JSON.stringify(payload, null, 2));
      } catch (error) {
        text("minersOutput", String(error));
        alert(String(error));
      } finally {
        busy = false;
        for (const btn of document.querySelectorAll("button")) btn.disabled = false;
      }
    }
    async function configureSelectedMiners() {
      const ips = selectedMinerIps();
      if (!ips.length) return alert("Select at least one miner.");
      const adminPassword = document.getElementById("minerAdminPassword").value;
      if (!adminPassword) return alert("Enter the miner admin password.");
      const poolUrl = document.getElementById("minerPoolUrl").value.trim();
      const workerUser = document.getElementById("minerWorkerUser").value.trim();
      const poolPassword = document.getElementById("minerPoolPassword").value;
      if (!poolUrl || !workerUser) return alert("Pool URL and worker/wallet are required.");
      if (!confirm(`Configure ${ips.length} miner(s) to ${poolUrl}?`)) return;

      busy = true;
      for (const btn of document.querySelectorAll("button")) btn.disabled = true;
      text("minersOutput", "Configuring selected miners...");
      try {
        const response = await fetch("/api/miners/configure", {
          method: "POST",
          headers: {"content-type": "application/json"},
          body: JSON.stringify({
            ips,
            admin_password: adminPassword,
            pool_url: poolUrl,
            worker_user: workerUser,
            pool_password: poolPassword,
            token: document.getElementById("token").value
          })
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "configuration failed");
        minerResults = {};
        for (const item of payload.results || []) minerResults[item.ip] = item;
        renderMiners();
        text("minersOutput", JSON.stringify(payload, null, 2));
      } catch (error) {
        text("minersOutput", String(error));
        alert(String(error));
      } finally {
        busy = false;
        for (const btn of document.querySelectorAll("button")) btn.disabled = false;
      }
    }
    async function saveMinerAuth() {
      const adminPassword = document.getElementById("minerAdminPassword").value;
      if (!adminPassword) return alert("Enter the miner admin password first.");
      if (!confirm("Save this password locally so the watchdog can repair miners without asking again?")) return;
      try {
        const response = await fetch("/api/miners/save-auth", {
          method: "POST",
          headers: {"content-type": "application/json"},
          body: JSON.stringify({admin_password: adminPassword, token: document.getElementById("token").value})
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "save failed");
        alert("Saved for watchdog repairs.");
      } catch (error) {
        alert(String(error));
      }
    }
    function currency(value, prefix, places = 2) {
      if (value === null || value === undefined || value === "") return "n/a";
      return `${prefix}${Number(value).toLocaleString(undefined, {maximumFractionDigits: places})}`;
    }
    function priceQuote(value, prefix) {
      return currency(value, prefix, 6);
    }
    const minerColors = ["#2563eb", "#16a34a", "#dc2626", "#d97706", "#7c3aed", "#0891b2", "#be185d", "#4b5563", "#0f766e", "#9333ea", "#b45309", "#0284c7"];
    function hashString(value) {
      let hash = 0;
      const text = String(value || "");
      for (let i = 0; i < text.length; i += 1) {
        hash = ((hash << 5) - hash + text.charCodeAt(i)) | 0;
      }
      return Math.abs(hash);
    }
    function minerIdentity(row) {
      return String(row.device_id || (row.mac ? `mac:${row.mac}` : row.ip) || "").trim();
    }
    function minerDisplayName(row) {
      return String(row.display_name || row.name || minerIdentity(row) || "Miner").trim();
    }
    function minerShortIp(row) {
      const ip = String(row.ip || "").trim();
      const parts = ip.split(".");
      const last = parts.length === 4 ? parts[3] : "";
      return /^\d{1,3}$/.test(last) ? `.${last}` : "";
    }
    function minerDisplayLabel(row) {
      const suffix = minerShortIp(row);
      return `${minerDisplayName(row)}${suffix ? " " + suffix : ""}`;
    }
    function minerColor(identity) {
      if (!identity) return "#4b5563";
      return minerColors[hashString(identity) % minerColors.length];
    }
    function globalPoolIdentity(row) {
      if (typeof row === "string") return row.trim().toLowerCase();
      return String(row?.address || row?.address_short || row?.pool_label || row?.pool_name || "").trim().toLowerCase();
    }
    function globalPoolColor(identity) {
      const key = globalPoolIdentity(identity);
      if (!key) return "#4b5563";
      return minerColors[hashString(`pool:${key}`) % minerColors.length];
    }
    function transparentColor(hex, alpha) {
      const match = String(hex || "").match(/^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i);
      if (!match) return `rgba(75,85,99,${alpha})`;
      const r = parseInt(match[1], 16);
      const g = parseInt(match[2], 16);
      const b = parseInt(match[3], 16);
      return `rgba(${r},${g},${b},${alpha})`;
    }
    function numberValue(value) {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    }
    function formatDisplayTime(value) {
      const parsed = Date.parse(value);
      if (!Number.isFinite(parsed)) return value || "n/a";
      return new Date(parsed).toLocaleString(undefined, {month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit"});
    }
    let earningsChartRangeHours = 1;
    let earningsChartUnit = "usd";
    function updateEarningsRangeButtons() {
      for (const button of document.querySelectorAll(".earnings-range-button")) {
        button.classList.toggle("active", Number(button.dataset.range || 0) === earningsChartRangeHours);
      }
    }
    function updateEarningsUnitButtons() {
      for (const button of document.querySelectorAll(".earnings-unit-button")) {
        button.classList.toggle("active", String(button.dataset.unit || "") === earningsChartUnit);
      }
      const summary = document.getElementById("earningsChartUnitSummary");
      if (summary) summary.textContent = `${earningsChartUnit.toUpperCase()} per miner per hour`;
    }
    function setEarningsChartRange(hours) {
      earningsChartRangeHours = hours;
      updateEarningsRangeButtons();
      if (lastEarningsData) drawEarningsChart(lastEarningsData);
    }
    function setEarningsChartUnit(unit) {
      if (!["bdag", "usd", "zar"].includes(unit)) return;
      earningsChartUnit = unit;
      updateEarningsUnitButtons();
      if (lastEarningsData) drawEarningsChart(lastEarningsData);
    }
    let minerWorkChartRangeHours = 1;
    let minerWorkChartMetric = "work";
    const minerWorkMetricConfigs = {
      work: {
        label: "Accepted work percentage by miner",
        axis: "%",
        detail: "Work %",
        empty: "No miner work-share history available yet.",
        floor: 0,
        ceiling: 100,
        minYMax: 20,
      },
      blocks: {
        label: "Actual found blocks by miner",
        axis: "blocks",
        detail: "Blocks",
        empty: "No per-miner block history available yet.",
        floor: 0,
        ceiling: null,
        minYMax: 1,
      },
      hashrate: {
        label: "Hashrate by miner; reconstructed history uses accepted-work estimates",
        axis: "GH/s",
        detail: "GH/s",
        empty: "No per-miner hashrate history available yet.",
        floor: 0,
        ceiling: null,
        minYMax: 1,
      },
    };
    function updateMinerWorkRangeButtons() {
      for (const button of document.querySelectorAll(".miner-work-range-button")) {
        button.classList.toggle("active", Number(button.dataset.range || 0) === minerWorkChartRangeHours);
      }
    }
    function updateMinerWorkMetricButtons() {
      for (const button of document.querySelectorAll(".miner-work-metric-button")) {
        button.classList.toggle("active", String(button.dataset.metric || "") === minerWorkChartMetric);
      }
      const summary = document.getElementById("minerWorkChartMetricSummary");
      if (summary) summary.textContent = (minerWorkMetricConfigs[minerWorkChartMetric] || minerWorkMetricConfigs.work).label;
    }
    function setMinerWorkChartRange(hours) {
      minerWorkChartRangeHours = hours;
      updateMinerWorkRangeButtons();
      if (lastEarningsData) drawMinerWorkChart(lastEarningsData);
    }
    function setMinerWorkChartMetric(metric) {
      if (!minerWorkMetricConfigs[metric]) return;
      minerWorkChartMetric = metric;
      updateMinerWorkMetricButtons();
      if (lastEarningsData) drawMinerWorkChart(lastEarningsData);
    }
    function parseDashboardTime(value) {
      if (!value) return null;
      const text = String(value).trim().replace(/([+-]\d{2})(\d{2})$/, "$1:$2");
      const parsed = Date.parse(text);
      return Number.isFinite(parsed) ? parsed : null;
    }
    function chartRangeLabel(hours) {
      if (hours === 72) return "3d";
      if (hours === 168) return "week";
      if (hours === 720) return "month";
      return `${hours}h`;
    }
    function chartHistoryFreshness(data) {
      if (!data?.history_stale) return "";
      const age = Number(data.history_latest_age_seconds || 0);
      const ageText = age >= 3600 ? `${(age / 3600).toFixed(1)}h` : `${Math.round(age / 60)}m`;
      const latest = data.history_latest_at ? ` since ${formatDisplayTime(data.history_latest_at)}` : "";
      return ` | sampler stopped ${ageText}${latest}`;
    }
    function samplerAlertMessage(data) {
      if (!data) return "";
      if (data.history_stale) {
        const age = Number(data.history_latest_age_seconds || 0);
        const ageText = age >= 3600 ? `${(age / 3600).toFixed(1)} hours` : `${Math.round(age / 60)} minutes`;
        const latest = data.history_latest_at ? ` Last good plot sample: ${formatDisplayTime(data.history_latest_at)}.` : "";
        const reason = data.history_stale_reason ? ` ${data.history_stale_reason}` : "";
        return `Sampler stopped: earnings and miner plots are not receiving fresh history. No valid sample for ${ageText}.${latest}${reason}`;
      }
      if (data.history_sampler_status === "missing") {
        return "No earnings/miner plot sampler history exists yet. The watchdog should create the first sample shortly.";
      }
      return "";
    }
    function renderSamplerAlert(id, data) {
      const el = document.getElementById(id);
      if (!el) return;
      const message = samplerAlertMessage(data);
      if (!message) {
        el.classList.add("hidden");
        el.textContent = "";
        return;
      }
      el.textContent = message;
      el.classList.remove("hidden");
    }
    function formatChartTime(ms, hours = earningsChartRangeHours) {
      const options = hours > 168
        ? {month: "short", day: "numeric"}
        : hours > 24
          ? {month: "short", day: "numeric", hour: "2-digit"}
          : hours >= 12
            ? {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}
            : {hour: "2-digit", minute: "2-digit"};
      return new Date(ms).toLocaleString(undefined, options);
    }
    function chartRangeProfile(hours) {
      const rangeHours = Number(hours) || 1;
      if (rangeHours <= 1) return {bucketMs: 30 * 1000, smoothMs: 60 * 1000, gapMs: 8 * 60 * 1000, detail: "30s"};
      if (rangeHours <= 4) return {bucketMs: 60 * 1000, smoothMs: 2 * 60 * 1000, gapMs: 15 * 60 * 1000, detail: "1m"};
      if (rangeHours <= 12) return {bucketMs: 3 * 60 * 1000, smoothMs: 6 * 60 * 1000, gapMs: 30 * 60 * 1000, detail: "3m"};
      if (rangeHours <= 24) return {bucketMs: 5 * 60 * 1000, smoothMs: 12 * 60 * 1000, gapMs: 60 * 60 * 1000, detail: "5m"};
      if (rangeHours <= 72) return {bucketMs: 15 * 60 * 1000, smoothMs: 45 * 60 * 1000, gapMs: 3 * 60 * 60 * 1000, detail: "15m"};
      if (rangeHours <= 168) return {bucketMs: 30 * 60 * 1000, smoothMs: 2 * 60 * 60 * 1000, gapMs: 8 * 60 * 60 * 1000, detail: "30m"};
      return {bucketMs: 2 * 60 * 60 * 1000, smoothMs: 6 * 60 * 60 * 1000, gapMs: 36 * 60 * 60 * 1000, detail: "2h"};
    }
    function chartTickCount(chartW, hours) {
      const maxTicks = Number(hours) <= 4 ? 8 : Number(hours) <= 24 ? 7 : 6;
      return Math.min(maxTicks, Math.max(2, Math.floor(chartW / 125)));
    }
    function clampChartValue(value, floor = 0, ceiling = null) {
      let result = value;
      if (floor !== null) result = Math.max(floor, result);
      if (ceiling !== null) result = Math.min(ceiling, result);
      return result;
    }
    function filterChartPointsForRange(points, latestTime, rangeHours) {
      const sorted = [...points]
        .filter(point => Number.isFinite(point.t) && Number.isFinite(point.v))
        .sort((a, b) => a.t - b.t);
      if (!sorted.length || latestTime === null) return sorted;
      const cutoff = latestTime - (rangeHours * 60 * 60 * 1000);
      const filtered = sorted.filter(point => point.t >= cutoff && point.t <= latestTime);
      if (filtered.length) {
        const anchor = [...sorted].reverse().find(point => point.t < cutoff);
        if (anchor) filtered.unshift({...anchor, t: cutoff, clipped: true});
      }
      return filtered;
    }
    function bucketChartPoints(points, rangeHours, floor = 0, ceiling = null) {
      const sorted = [...points].sort((a, b) => a.t - b.t);
      if (sorted.length < 3) {
        return sorted.map(point => ({...point, v: clampChartValue(point.v, floor, ceiling)}));
      }
      const profile = chartRangeProfile(rangeHours);
      const buckets = new Map();
      for (const point of sorted) {
        const bucketKey = Math.floor(point.t / profile.bucketMs) * profile.bucketMs;
        const bucket = buckets.get(bucketKey) || {tSum: 0, vSum: 0, count: 0};
        bucket.tSum += point.t;
        bucket.vSum += point.v;
        bucket.count += 1;
        buckets.set(bucketKey, bucket);
      }
      return Array.from(buckets.values())
        .map(bucket => ({
          t: Math.round(bucket.tSum / bucket.count),
          v: clampChartValue(bucket.vSum / bucket.count, floor, ceiling),
          samples: bucket.count,
        }))
        .sort((a, b) => a.t - b.t);
    }
    function smoothChartPoints(points, rangeHours, floor = 0, ceiling = null) {
      const sorted = bucketChartPoints(points, rangeHours, floor, ceiling);
      if (sorted.length < 4) return sorted;
      const windowMs = chartRangeProfile(rangeHours).smoothMs;
      return sorted.map(point => {
        let weightedValue = 0;
        let weightTotal = 0;
        for (const peer of sorted) {
          const distance = Math.abs(peer.t - point.t);
          if (distance > windowMs) continue;
          const weight = 1 - (distance / (windowMs + 1));
          weightedValue += peer.v * weight;
          weightTotal += weight;
        }
        let value = weightTotal ? weightedValue / weightTotal : point.v;
        value = clampChartValue(value, floor, ceiling);
        return {...point, rawV: point.v, v: value};
      });
    }
    function drawSmoothChartSegment(ctx, coords) {
      if (!coords.length) return;
      ctx.beginPath();
      ctx.moveTo(coords[0].x, coords[0].y);
      if (coords.length === 1) {
        ctx.lineTo(coords[0].x + 0.01, coords[0].y);
      } else if (coords.length === 2) {
        ctx.lineTo(coords[1].x, coords[1].y);
      } else {
        for (let i = 1; i < coords.length - 2; i += 1) {
          const midX = (coords[i].x + coords[i + 1].x) / 2;
          const midY = (coords[i].y + coords[i + 1].y) / 2;
          ctx.quadraticCurveTo(coords[i].x, coords[i].y, midX, midY);
        }
        const penultimate = coords[coords.length - 2];
        const last = coords[coords.length - 1];
        ctx.quadraticCurveTo(penultimate.x, penultimate.y, last.x, last.y);
      }
      ctx.stroke();
    }
    function drawSmoothChartLine(ctx, points, xFor, yFor, rangeHours) {
      if (!points.length) return;
      const gapMs = chartRangeProfile(rangeHours).gapMs;
      let segment = [];
      for (let i = 0; i < points.length; i += 1) {
        const point = points[i];
        if (i > 0 && point.t - points[i - 1].t > gapMs) {
          drawSmoothChartSegment(ctx, segment);
          segment = [];
        }
        segment.push({x: xFor(point), y: yFor(point)});
      }
      drawSmoothChartSegment(ctx, segment);
    }
    function firstNumeric(...values) {
      for (const value of values) {
        const parsed = numberValue(value);
        if (parsed !== null) return parsed;
      }
      return null;
    }
    function minerEarningsPerHour(row, unit, price) {
      const bdag = firstNumeric(
        row.estimated_wallet_bdag_recent_hour,
        row.estimated_wallet_bdag_avg_hour,
        row.estimated_wallet_bdag_1h,
        row.estimated_bdag_avg_hour,
        row.estimated_bdag_1h
      );
      const usd = firstNumeric(
        row.estimated_wallet_usd_recent_hour,
        row.estimated_wallet_usd_avg_hour,
        row.estimated_wallet_usd_1h,
        row.estimated_usd_avg_hour,
        row.estimated_usd_1h
      );
      const zar = firstNumeric(
        row.estimated_wallet_zar_recent_hour,
        row.estimated_wallet_zar_avg_hour,
        row.estimated_wallet_zar_1h,
        row.estimated_zar_avg_hour,
        row.estimated_zar_1h
      );
      const usdPrice = numberValue(price?.usd);
      const zarPrice = numberValue(price?.zar);
      if (unit === "bdag") return bdag ?? (usd !== null && usdPrice ? usd / usdPrice : null) ?? (zar !== null && zarPrice ? zar / zarPrice : null);
      if (unit === "zar") return zar ?? (bdag !== null && zarPrice !== null ? bdag * zarPrice : null) ?? (usd !== null && usdPrice && zarPrice !== null ? usd * (zarPrice / usdPrice) : null);
      return usd ?? (bdag !== null && usdPrice !== null ? bdag * usdPrice : null) ?? (zar !== null && zarPrice && usdPrice !== null ? zar * (usdPrice / zarPrice) : null);
    }
    function formatEarningsChartValue(value, unit) {
      if (unit === "bdag") return currency(value, "", 0);
      if (unit === "zar") return currency(value, "R");
      return currency(value, "$");
    }
    function earningsDbToWalletScale(data) {
      const onchain24 = firstNumeric(data.earnings_24h?.bdag, data.hourly_averages?.wallet_24h_bdag);
      const db24 = firstNumeric(data.earnings_24h?.db_credit_fallback_bdag, data.credits?.recent_24h?.wallet_total_bdag, data.credits?.recent_24h?.total_bdag);
      const onchain1 = firstNumeric(data.onchain_earnings?.last_1h?.earned_bdag, data.hourly_averages?.recent_bdag_hour);
      const db1 = firstNumeric(data.credits?.recent_1h?.total_bdag);
      const candidates = [];
      if (onchain24 !== null && db24 !== null && db24 > 0) candidates.push(onchain24 / db24);
      if (onchain1 !== null && db1 !== null && db1 > 0) candidates.push(onchain1 / db1);
      const factor = candidates.find(value => Number.isFinite(value) && value > 1.5 && value < 100) || 1;
      return {factor, normalized: factor !== 1};
    }
    function isLegacyDbScaleEarningsRow(row) {
      const hasActualWalletBdag = firstNumeric(row.estimated_wallet_bdag_recent_hour, row.estimated_wallet_bdag_avg_hour, row.estimated_wallet_bdag_1h) !== null;
      const hasZarFields = firstNumeric(row.estimated_zar_avg_hour, row.estimated_zar_1h, row.estimated_wallet_zar_recent_hour, row.estimated_wallet_zar_avg_hour, row.estimated_wallet_zar_1h) !== null;
      const hasUsdOnly = firstNumeric(row.estimated_wallet_usd_recent_hour, row.estimated_usd_avg_hour, row.estimated_usd_1h) !== null;
      return !hasActualWalletBdag && !hasZarFields && hasUsdOnly;
    }
    function applyLegacyEarningsScale(value, row, scale) {
      return value !== null && isLegacyDbScaleEarningsRow(row) ? value * scale.factor : value;
    }
    function drawEarningsChart(data) {
      const canvas = document.getElementById("earningsChart");
      const legend = document.getElementById("earningsChartLegend");
      const rangeLabel = document.getElementById("earningsChartRangeLabel");
      if (!canvas) return;
      updateEarningsRangeButtons();
      updateEarningsUnitButtons();
      const rect = canvas.parentElement.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      const width = Math.max(360, Math.floor(rect.width));
      const height = Math.max(240, Math.floor(rect.height));
      canvas.width = Math.floor(width * ratio);
      canvas.height = Math.floor(height * ratio);
      const ctx = canvas.getContext("2d");
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#fbfcfd";
      ctx.fillRect(0, 0, width, height);

      const snapshots = [...(data.history || []), {
        generated_at: data.generated_at,
        miner_estimates: data.miner_estimates || [],
      }];
      const timestamped = snapshots
        .map(snapshot => ({
          at: parseDashboardTime(snapshot.generated_at),
          miners: snapshot.miner_estimates || [],
        }))
        .filter(snapshot => snapshot.at !== null)
        .sort((a, b) => a.at - b.at);

      const latestTime = timestamped.length ? timestamped[timestamped.length - 1].at : null;
      const cutoff = latestTime === null ? null : latestTime - (earningsChartRangeHours * 60 * 60 * 1000);
      const scale = earningsDbToWalletScale(data);
      const seriesMap = new Map();
      for (const snapshot of timestamped) {
        for (const row of snapshot.miners) {
          const value = applyLegacyEarningsScale(minerEarningsPerHour(row, earningsChartUnit, data.price || {}), row, scale);
          if (value === null) continue;
          const key = minerIdentity(row);
          if (!key) continue;
          if (!seriesMap.has(key)) {
            seriesMap.set(key, {key, label: minerDisplayLabel(row), points: []});
          }
          seriesMap.get(key).label = minerDisplayLabel(row);
          seriesMap.get(key).points.push({t: snapshot.at, v: value});
        }
      }

      const series = Array.from(seriesMap.values())
        .map(item => {
          return {...item, points: filterChartPointsForRange(item.points, latestTime, earningsChartRangeHours)};
        })
        .filter(item => item.points.length)
        .sort((a, b) => (b.points[b.points.length - 1]?.v || 0) - (a.points[a.points.length - 1]?.v || 0));

      const visibleSeries = series.map(item => ({...item, points: smoothChartPoints(item.points, earningsChartRangeHours)}));

      if (rangeLabel) {
        const period = latestTime !== null && cutoff !== null ? `${formatChartTime(cutoff, earningsChartRangeHours)} to ${formatChartTime(latestTime, earningsChartRangeHours)}` : "no earnings history yet";
        const normalized = scale.normalized ? ` | legacy history normalized x${scale.factor.toFixed(2)}` : "";
        rangeLabel.textContent = `${chartRangeLabel(earningsChartRangeHours)} window | detail ${chartRangeProfile(earningsChartRangeHours).detail} | ${earningsChartUnit.toUpperCase()}/h${normalized}${chartHistoryFreshness(data)} | ${period}`;
      }

      if (!visibleSeries.length) {
        if (legend) legend.innerHTML = "";
        ctx.fillStyle = "#617181";
        ctx.font = "13px system-ui, sans-serif";
        ctx.fillText("No miner earnings history available yet.", 16, 34);
        return;
      }

      const legendLimit = 8;
      if (legend) {
        legend.innerHTML = "";
        for (let i = 0; i < Math.min(visibleSeries.length, legendLimit); i += 1) {
          const item = visibleSeries[i];
          const span = document.createElement("span");
          span.className = "legend-key";
          span.style.setProperty("--key-color", minerColor(item.key));
          span.textContent = item.label;
          legend.appendChild(span);
        }
        if (visibleSeries.length > legendLimit) {
          const more = document.createElement("span");
          more.className = "subtle";
          more.textContent = `+${visibleSeries.length - legendLimit} more`;
          legend.appendChild(more);
        }
      }

      const allPoints = visibleSeries.flatMap(item => item.points);
      const minTime = Math.min(...allPoints.map(point => point.t));
      const maxTime = Math.max(...allPoints.map(point => point.t));
      const maxValue = Math.max(...allPoints.map(point => point.v), 1);
      const margin = {top: 24, right: 18, bottom: 54, left: earningsChartUnit === "bdag" || earningsChartUnit === "zar" ? 78 : 58};
      const chartW = width - margin.left - margin.right;
      const chartH = height - margin.top - margin.bottom;
      const xSpan = Math.max(maxTime - minTime, 1);
      const yMax = maxValue * 1.1;
      const yTicks = 5;
      const xTicks = chartTickCount(chartW, earningsChartRangeHours);

      ctx.strokeStyle = "#d7dbe0";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(margin.left, margin.top);
      ctx.lineTo(margin.left, margin.top + chartH);
      ctx.lineTo(width - margin.right, margin.top + chartH);
      ctx.stroke();

      ctx.fillStyle = "#617181";
      ctx.font = "12px system-ui, sans-serif";
      ctx.textAlign = "right";
      for (let i = 0; i <= yTicks; i += 1) {
        const ratioY = i / yTicks;
        const value = yMax * (1 - ratioY);
        const y = margin.top + chartH * ratioY;
        ctx.strokeStyle = "#edf0f3";
        ctx.beginPath();
        ctx.moveTo(margin.left, y);
        ctx.lineTo(width - margin.right, y);
        ctx.stroke();
        ctx.fillText(formatEarningsChartValue(value, earningsChartUnit), margin.left - 8, y + 4);
      }

      ctx.textAlign = "center";
      for (let i = 0; i <= xTicks; i += 1) {
        const ratioX = i / xTicks;
        const t = minTime + xSpan * ratioX;
        const x = margin.left + chartW * ratioX;
        ctx.strokeStyle = "#edf0f3";
        ctx.beginPath();
        ctx.moveTo(x, margin.top);
        ctx.lineTo(x, margin.top + chartH);
        ctx.stroke();
        ctx.fillText(formatChartTime(t, earningsChartRangeHours), x, height - 18);
      }

      for (let i = 0; i < visibleSeries.length; i += 1) {
        const item = visibleSeries[i];
        const color = minerColor(item.key);
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 2;
        drawSmoothChartLine(
          ctx,
          item.points,
          point => margin.left + ((point.t - minTime) / xSpan) * chartW,
          point => margin.top + chartH - ((point.v / yMax) * chartH),
          earningsChartRangeHours
        );
        const last = item.points[item.points.length - 1];
        const lastX = margin.left + ((last.t - minTime) / xSpan) * chartW;
        const lastY = margin.top + chartH - ((last.v / yMax) * chartH);
        ctx.beginPath();
        ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    function minerWorkPercent(row) {
      const parsed = numberValue(row.work_percent);
      return parsed === null ? null : parsed;
    }
    function minerBlocksFound(row) {
      return firstNumeric(row.blocks_found, row.found_blocks);
    }
    function minerHashrateGhs(row) {
      return firstNumeric(
        row.av_hashrate_ghs,
        row.hashrate_ghs,
        row.observed_hashrate_ghs,
        row.av_hashrate,
        row.hashrate
      );
    }
    function minerChartMetricValue(row) {
      if (minerWorkChartMetric === "blocks") return minerBlocksFound(row);
      if (minerWorkChartMetric === "hashrate") return minerHashrateGhs(row);
      return minerWorkPercent(row);
    }
    function formatMinerMetricValue(value, metric = minerWorkChartMetric) {
      if (metric === "work") return `${value.toFixed(0)}%`;
      if (metric === "hashrate") return `${value.toLocaleString(undefined, {maximumFractionDigits: 1})}`;
      if (value < 10 && value % 1 !== 0) return value.toFixed(1);
      return value.toLocaleString(undefined, {maximumFractionDigits: 0});
    }
    function drawMinerWorkChart(data) {
      const canvas = document.getElementById("minerWorkChart");
      const legend = document.getElementById("minerWorkChartLegend");
      const rangeLabel = document.getElementById("minerWorkChartRangeLabel");
      if (!canvas) return;
      updateMinerWorkRangeButtons();
      updateMinerWorkMetricButtons();
      const metricConfig = minerWorkMetricConfigs[minerWorkChartMetric] || minerWorkMetricConfigs.work;
      const rect = canvas.parentElement.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      const width = Math.max(360, Math.floor(rect.width));
      const height = Math.max(240, Math.floor(rect.height));
      canvas.width = Math.floor(width * ratio);
      canvas.height = Math.floor(height * ratio);
      const ctx = canvas.getContext("2d");
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#fbfcfd";
      ctx.fillRect(0, 0, width, height);

      const snapshots = [...(data.history || []), {
        generated_at: data.generated_at,
        miner_estimates: data.miner_estimates || [],
      }];
      const timestamped = snapshots
        .map(snapshot => ({
          at: parseDashboardTime(snapshot.generated_at),
          miners: snapshot.miner_estimates || [],
        }))
        .filter(snapshot => snapshot.at !== null)
        .sort((a, b) => a.at - b.at);

      const latestTime = timestamped.length ? timestamped[timestamped.length - 1].at : null;
      const cutoff = latestTime === null ? null : latestTime - (minerWorkChartRangeHours * 60 * 60 * 1000);
      const seriesMap = new Map();
      for (const snapshot of timestamped) {
        for (const row of snapshot.miners) {
          const value = minerChartMetricValue(row);
          if (value === null) continue;
          const key = minerIdentity(row);
          if (!key) continue;
          if (!seriesMap.has(key)) {
            seriesMap.set(key, {key, label: minerDisplayLabel(row), points: []});
          }
          seriesMap.get(key).label = minerDisplayLabel(row);
          seriesMap.get(key).points.push({t: snapshot.at, v: value});
        }
      }

      const visibleSeries = Array.from(seriesMap.values())
        .map(item => {
          return {...item, points: filterChartPointsForRange(item.points, latestTime, minerWorkChartRangeHours)};
        })
        .filter(item => item.points.length)
        .sort((a, b) => (b.points[b.points.length - 1]?.v || 0) - (a.points[a.points.length - 1]?.v || 0))
        .map(item => ({...item, points: smoothChartPoints(item.points, minerWorkChartRangeHours, metricConfig.floor, metricConfig.ceiling)}));

      if (rangeLabel) {
        const period = latestTime !== null && cutoff !== null ? `${formatChartTime(cutoff, minerWorkChartRangeHours)} to ${formatChartTime(latestTime, minerWorkChartRangeHours)}` : "no miner history yet";
        rangeLabel.textContent = `${chartRangeLabel(minerWorkChartRangeHours)} window | detail ${chartRangeProfile(minerWorkChartRangeHours).detail} | ${metricConfig.detail}${chartHistoryFreshness(data)} | ${period}`;
      }

      if (!visibleSeries.length) {
        if (legend) legend.innerHTML = "";
        ctx.fillStyle = "#617181";
        ctx.font = "13px system-ui, sans-serif";
        ctx.fillText(metricConfig.empty, 16, 34);
        return;
      }

      const legendLimit = 8;
      if (legend) {
        legend.innerHTML = "";
        for (let i = 0; i < Math.min(visibleSeries.length, legendLimit); i += 1) {
          const item = visibleSeries[i];
          const span = document.createElement("span");
          span.className = "legend-key";
          span.style.setProperty("--key-color", minerColor(item.key));
          span.textContent = item.label;
          legend.appendChild(span);
        }
        if (visibleSeries.length > legendLimit) {
          const more = document.createElement("span");
          more.className = "subtle";
          more.textContent = `+${visibleSeries.length - legendLimit} more`;
          legend.appendChild(more);
        }
      }

      const allPoints = visibleSeries.flatMap(item => item.points);
      const minTime = Math.min(...allPoints.map(point => point.t));
      const maxTime = Math.max(...allPoints.map(point => point.t));
      const maxValue = Math.max(...allPoints.map(point => point.v), 1);
      const margin = {top: 24, right: 18, bottom: 54, left: minerWorkChartMetric === "hashrate" ? 68 : 58};
      const chartW = width - margin.left - margin.right;
      const chartH = height - margin.top - margin.bottom;
      const xSpan = Math.max(maxTime - minTime, 1);
      const yMaxRaw = Math.max(metricConfig.minYMax || 1, maxValue * 1.15);
      const yMax = metricConfig.ceiling === null ? yMaxRaw : Math.min(metricConfig.ceiling, yMaxRaw);
      const yTicks = 5;
      const xTicks = chartTickCount(chartW, minerWorkChartRangeHours);

      ctx.strokeStyle = "#d7dbe0";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(margin.left, margin.top);
      ctx.lineTo(margin.left, margin.top + chartH);
      ctx.lineTo(width - margin.right, margin.top + chartH);
      ctx.stroke();

      ctx.fillStyle = "#617181";
      ctx.font = "12px system-ui, sans-serif";
      ctx.textAlign = "right";
      for (let i = 0; i <= yTicks; i += 1) {
        const ratioY = i / yTicks;
        const value = yMax * (1 - ratioY);
        const y = margin.top + chartH * ratioY;
        ctx.strokeStyle = "#edf0f3";
        ctx.beginPath();
        ctx.moveTo(margin.left, y);
        ctx.lineTo(width - margin.right, y);
        ctx.stroke();
        ctx.fillText(formatMinerMetricValue(value), margin.left - 8, y + 4);
      }

      ctx.textAlign = "center";
      for (let i = 0; i <= xTicks; i += 1) {
        const ratioX = i / xTicks;
        const t = minTime + xSpan * ratioX;
        const x = margin.left + chartW * ratioX;
        ctx.strokeStyle = "#edf0f3";
        ctx.beginPath();
        ctx.moveTo(x, margin.top);
        ctx.lineTo(x, margin.top + chartH);
        ctx.stroke();
        ctx.fillText(formatChartTime(t, minerWorkChartRangeHours), x, height - 18);
      }

      for (const item of visibleSeries) {
        const color = minerColor(item.key);
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 2;
        drawSmoothChartLine(
          ctx,
          item.points,
          point => margin.left + ((point.t - minTime) / xSpan) * chartW,
          point => margin.top + chartH - ((point.v / yMax) * chartH),
          minerWorkChartRangeHours
        );
        const last = item.points[item.points.length - 1];
        const lastX = margin.left + ((last.t - minTime) / xSpan) * chartW;
        const lastY = margin.top + chartH - ((last.v / yMax) * chartH);
        ctx.beginPath();
        ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    let globalChartRangeHours = 1;
    let globalChartMetric = "usd";
    function updateGlobalRangeButtons() {
      for (const button of document.querySelectorAll(".global-range-button")) {
        button.classList.toggle("active", Number(button.dataset.range || 0) === globalChartRangeHours);
      }
    }
    function updateGlobalMetricButtons() {
      for (const button of document.querySelectorAll(".global-metric-button")) {
        button.classList.toggle("active", String(button.dataset.metric || "") === globalChartMetric);
      }
      const summary = document.getElementById("globalChartMetricSummary");
      if (summary) {
        summary.textContent = globalChartMetric === "blocks"
          ? "Blocks produced per pool per hour"
          : "USD per pool per hour";
      }
    }
    function setGlobalChartRange(hours) {
      globalChartRangeHours = hours;
      updateGlobalRangeButtons();
      if (lastGlobalData) drawGlobalChart(lastGlobalData);
    }
    function setGlobalChartMetric(metric) {
      if (!["usd", "blocks"].includes(metric)) return;
      globalChartMetric = metric;
      updateGlobalMetricButtons();
      if (lastGlobalData) drawGlobalChart(lastGlobalData);
    }
    function poolUsdPerHour(row) {
      return numberValue(
        row.estimated_usd_avg_hour ??
        row.estimated_usd_recent_hour ??
        row.estimated_wallet_usd_avg_hour ??
        row.estimated_wallet_usd_recent_hour ??
        row.estimated_usd_1h ??
        row.estimated_wallet_usd_1h
      );
    }
    function poolBlocksPerHour(row, snapshot) {
      const direct = firstNumeric(
        row.blocks_per_hour,
        row.blocks_avg_hour,
        row.blocks_recent_hour,
        row.blocks_1h
      );
      if (direct !== null) return direct;
      const blocks = numberValue(row.blocks);
      const windowHours = firstNumeric(row.scan_window_hours, snapshot?.scan_window_hours);
      return blocks !== null && windowHours !== null && windowHours > 0 ? blocks / windowHours : null;
    }
    function poolGlobalChartValue(row, snapshot) {
      if (globalChartMetric === "blocks") return poolBlocksPerHour(row, snapshot);
      return poolUsdPerHour(row);
    }
    function formatGlobalChartValue(value) {
      if (globalChartMetric === "blocks") {
        if (value >= 100) return `${value.toLocaleString(undefined, {maximumFractionDigits: 0})}/h`;
        return `${value.toLocaleString(undefined, {maximumFractionDigits: 2})}/h`;
      }
      return currency(value, "$");
    }
    function drawGlobalChart(data) {
      const canvas = document.getElementById("globalChart");
      const legend = document.getElementById("globalChartLegend");
      const rangeLabel = document.getElementById("globalChartRangeLabel");
      if (!canvas) return;
      updateGlobalRangeButtons();
      updateGlobalMetricButtons();
      const rect = canvas.parentElement.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      const width = Math.max(360, Math.floor(rect.width));
      const height = Math.max(240, Math.floor(rect.height));
      canvas.width = Math.floor(width * ratio);
      canvas.height = Math.floor(height * ratio);
      const ctx = canvas.getContext("2d");
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#fbfcfd";
      ctx.fillRect(0, 0, width, height);

      const snapshots = [...(data.history || []), {
        generated_at: data.updated_at,
        scan_window_hours: data.scan_window_hours,
        clusters: data.clusters || [],
      }];
      const timestamped = snapshots
        .map(snapshot => ({
          at: parseDashboardTime(snapshot.generated_at),
          scan_window_hours: snapshot.scan_window_hours,
          pools: snapshot.clusters || [],
        }))
        .filter(snapshot => snapshot.at !== null)
        .sort((a, b) => a.at - b.at);

      const latestTime = timestamped.length ? timestamped[timestamped.length - 1].at : null;
      const cutoff = latestTime === null ? null : latestTime - (globalChartRangeHours * 60 * 60 * 1000);
      const seriesMap = new Map();
      for (const snapshot of timestamped) {
        for (const row of snapshot.pools) {
          const value = poolGlobalChartValue(row, snapshot);
          if (value === null) continue;
          const key = globalPoolIdentity(row);
          if (!key) continue;
          if (!seriesMap.has(key)) {
            seriesMap.set(key, {key, label: globalPoolLabel(row), points: []});
          }
          seriesMap.get(key).points.push({t: snapshot.at, v: value});
        }
      }

      const series = Array.from(seriesMap.values())
        .map(item => {
          return {...item, points: filterChartPointsForRange(item.points, latestTime, globalChartRangeHours)};
        })
        .filter(item => item.points.length)
        .sort((a, b) => (b.points[b.points.length - 1]?.v || 0) - (a.points[a.points.length - 1]?.v || 0));

      const visibleSeries = series.map(item => ({...item, points: smoothChartPoints(item.points, globalChartRangeHours)}));

      if (rangeLabel) {
        const period = latestTime !== null && cutoff !== null ? `${formatChartTime(cutoff, globalChartRangeHours)} to ${formatChartTime(latestTime, globalChartRangeHours)}` : "no pool history yet";
        rangeLabel.textContent = `${chartRangeLabel(globalChartRangeHours)} window | detail ${chartRangeProfile(globalChartRangeHours).detail} | ${period}`;
      }

      if (!visibleSeries.length) {
        if (legend) legend.innerHTML = "";
        ctx.fillStyle = "#617181";
        ctx.font = "13px system-ui, sans-serif";
        ctx.fillText(`No pool ${globalChartMetric === "blocks" ? "block-production" : "earnings"} history available yet.`, 16, 34);
        return;
      }

      const legendLimit = visibleSeries.length;
      if (legend) {
        legend.innerHTML = "";
        for (let i = 0; i < Math.min(visibleSeries.length, legendLimit); i += 1) {
          const item = visibleSeries[i];
          const span = document.createElement("span");
          span.className = "legend-key";
          span.style.setProperty("--key-color", globalPoolColor(item.key));
          span.textContent = item.label;
          legend.appendChild(span);
        }
        if (visibleSeries.length > legendLimit) {
          const more = document.createElement("span");
          more.className = "subtle";
          more.textContent = `+${visibleSeries.length - legendLimit} more`;
          legend.appendChild(more);
        }
      }

      const allPoints = visibleSeries.flatMap(item => item.points);
      const minTime = Math.min(...allPoints.map(point => point.t));
      const maxTime = Math.max(...allPoints.map(point => point.t));
      const maxValue = Math.max(...allPoints.map(point => point.v), 1);
      const margin = {top: 24, right: 18, bottom: 54, left: 58};
      const chartW = width - margin.left - margin.right;
      const chartH = height - margin.top - margin.bottom;
      const xSpan = Math.max(maxTime - minTime, 1);
      const yMax = maxValue * 1.1;
      const yTicks = 5;
      const xTicks = chartTickCount(chartW, globalChartRangeHours);

      ctx.strokeStyle = "#d7dbe0";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(margin.left, margin.top);
      ctx.lineTo(margin.left, margin.top + chartH);
      ctx.lineTo(width - margin.right, margin.top + chartH);
      ctx.stroke();

      ctx.fillStyle = "#617181";
      ctx.font = "12px system-ui, sans-serif";
      ctx.textAlign = "right";
      for (let i = 0; i <= yTicks; i += 1) {
        const ratioY = i / yTicks;
        const value = yMax * (1 - ratioY);
        const y = margin.top + chartH * ratioY;
        ctx.strokeStyle = "#edf0f3";
        ctx.beginPath();
        ctx.moveTo(margin.left, y);
        ctx.lineTo(width - margin.right, y);
        ctx.stroke();
        ctx.fillText(formatGlobalChartValue(value), margin.left - 8, y + 4);
      }

      ctx.textAlign = "center";
      for (let i = 0; i <= xTicks; i += 1) {
        const ratioX = i / xTicks;
        const t = minTime + xSpan * ratioX;
        const x = margin.left + chartW * ratioX;
        ctx.strokeStyle = "#edf0f3";
        ctx.beginPath();
        ctx.moveTo(x, margin.top);
        ctx.lineTo(x, margin.top + chartH);
        ctx.stroke();
        ctx.fillText(formatChartTime(t, globalChartRangeHours), x, height - 18);
      }

      for (let i = 0; i < visibleSeries.length; i += 1) {
        const item = visibleSeries[i];
        const color = globalPoolColor(item.key);
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 2;
        drawSmoothChartLine(
          ctx,
          item.points,
          point => margin.left + ((point.t - minTime) / xSpan) * chartW,
          point => margin.top + chartH - ((point.v / yMax) * chartH),
          globalChartRangeHours
        );
        const last = item.points[item.points.length - 1];
        const lastX = margin.left + ((last.t - minTime) / xSpan) * chartW;
        const lastY = margin.top + chartH - ((last.v / yMax) * chartH);
        ctx.beginPath();
        ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    async function refreshEarnings() {
      text("priceFeedOutput", "Loading earnings...");
      try {
        const response = await fetch("/api/earnings", {cache: "no-store"});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "earnings request failed");
        renderEarnings(data);
        earningsLoaded = true;
      } catch (error) {
        text("priceFeedOutput", String(error));
      }
    }
    async function refreshGlobal() {
      try {
        const response = await fetch("/api/global", {cache: "no-store"});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "global request failed");
        renderGlobal(data);
      } catch (error) {
        text("globalLatestBlock", "error");
        text("globalScannedBlocks", "error");
        text("globalUniqueMiners", "error");
        text("globalScanWindow", "error");
        text("globalAvgBlockSec", "error");
        text("globalTopShare", "error");
      const table = document.getElementById("globalPoolsTable");
      table.innerHTML = "";
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="13">${escapeHtml(String(error))}</td>`;
      table.appendChild(tr);
      } finally {
        globalLoaded = true;
      }
    }
    function renderEarnings(data) {
      lastEarningsData = data;
      const totals = data.credits?.totals || {};
      const hourly = data.hourly_averages || {};
      const paymentWallet = data.payment_wallet_balance || data.wallet_balance || {};
      const creditWallet = data.credit_wallet_balance || data.wallet?.aggregate || null;
      const walletBdag = hasValue(paymentWallet.total_bdag) ? paymentWallet.total_bdag : (data.credit_balance_check?.actual_wallet_bdag || "n/a");
      const avgIncomeHour = hourly.wallet_24h_avg_bdag_hour || hourly.recent_bdag_hour || hourly.tracked_avg_bdag_hour || hourly.wallet_tracked_avg_bdag_hour || hourly.wallet_avg_bdag_hour_since_pool_start || "n/a";
      const walletAvgHour = hourly.wallet_24h_avg_bdag_hour || "n/a";
      const priceOk = data.price?.status === "ok" && data.price?.source === "exchange-average";
      const usdPrice = priceOk ? numberValue(data.price?.usd) : null;
      const zarPrice = priceOk ? numberValue(data.price?.zar) : null;
      const avgIncomeUsdHour = numberValue(avgIncomeHour) !== null && usdPrice !== null ? currency(numberValue(avgIncomeHour) * usdPrice, "$") : "n/a";
      const walletRecentHour = hourly.wallet_recent_bdag_hour || data.credits?.recent_1h?.total_bdag || "n/a";
      const wallet24hBdag = data.earnings_24h?.bdag || hourly.wallet_24h_bdag || "n/a";
      const wallet24hUsd = currency(data.earnings_24h?.usd || data.wallet_24h_usd, "$");
      const wallet24hZar = currency(data.earnings_24h?.zar || data.wallet_24h_zar, "R");
      const currentPriceUsd = priceQuote(usdPrice, "$");
      const currentPriceZar = priceQuote(zarPrice, "R");
      text("earnWalletBdag", walletBdag);
      text("earnAvgIncomeBdagHour", avgIncomeHour);
      text("earnWalletAvgBdagHour", walletAvgHour);
      text("earnWalletRecentBdagHour", walletRecentHour);
      text("earnWallet24hZar", wallet24hZar);
      text("earnWallet24hBdag", wallet24hBdag);
      text("earnWallet24hUsd", wallet24hUsd);
      text("earnAvgIncomeUsdHour", avgIncomeUsdHour);
      text("earnCurrentPriceUsd", currentPriceUsd);
      text("earnCurrentPriceZar", currentPriceZar);
      text("earnTotalUsd", currency(data.wallet_total_usd || data.total_usd, "$"));
      text("earnTotalZar", currency(data.wallet_total_zar || data.total_zar, "R"));

      const addressBody = document.getElementById("addressCreditsTable");
      addressBody.innerHTML = "";
      for (const row of data.credits?.by_address || []) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td class="nowrap" title="${escapeHtml(row.miner_address)}">${escapeShortEth(row.miner_address)}</td><td class="right">${fmt(row.credit_count)}</td><td class="right">${escapeHtml(row.total_bdag)}</td><td class="right">${escapeHtml(row.pending_bdag)}</td><td>${escapeHtml(row.last_credit_at || "")}</td>`;
        addressBody.appendChild(tr);
      }

      const walletBody = document.getElementById("walletSourcesTable");
      walletBody.innerHTML = "";
      if (paymentWallet) {
        const aggregate = paymentWallet;
        const cls = aggregate.status === "ok" ? "ok" : aggregate.status === "partial" ? "warn" : "down";
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>Payment wallet</td><td class="${cls}">${escapeHtml(aggregate.status || "")}</td><td class="right">${escapeHtml(aggregate.total_bdag || "")}</td><td>${escapeHtml(`${aggregate.ok_address_count || 0}/${aggregate.address_count || 0} wallet addresses, ${aggregate.source_truth || "on-chain"}`)}</td>`;
        walletBody.appendChild(tr);
        for (const balance of aggregate.addresses || []) {
          const rowCls = balance.status === "ok" ? "ok" : "warn";
          const detail = balance.status === "ok" ? `${balance.source || ""} ${balance.type || ""}` : (balance.error || "");
          const row = document.createElement("tr");
          row.innerHTML = `<td title="${escapeHtml(balance.address)}">${escapeHtml(balance.address_short || shortEth(balance.address))}</td><td class="${rowCls}">${escapeHtml(balance.status || "")}</td><td class="right">${escapeHtml(balance.bdag || "")}</td><td>${escapeHtml(detail)}</td>`;
          walletBody.appendChild(row);
        }
      }
      if (creditWallet && Number(creditWallet.address_count || 0) > Number(paymentWallet.address_count || 0)) {
        const cls = creditWallet.status === "ok" ? "ok" : creditWallet.status === "partial" ? "warn" : "down";
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>Credit addresses total</td><td class="${cls}">${escapeHtml(creditWallet.status || "")}</td><td class="right">${escapeHtml(creditWallet.total_bdag || "")}</td><td>${escapeHtml(`${creditWallet.ok_address_count || 0}/${creditWallet.address_count || 0} historical credit addresses`)}</td>`;
        walletBody.appendChild(tr);
      }
      for (const source of data.wallet?.sources || []) {
        const cls = source.status === "ok" ? "ok" : "warn";
        const detail = source.error || (source.block_number_balance_updated_at ? `balance block ${source.block_number_balance_updated_at}` : "");
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>${escapeHtml(`Primary ${source.source}`)}</td><td class="${cls}">${escapeHtml(source.status)}</td><td class="right">${escapeHtml(source.bdag || "")}</td><td>${escapeHtml(detail)}</td>`;
        walletBody.appendChild(tr);
      }

      const minerBody = document.getElementById("minerEarningsTable");
      minerBody.innerHTML = "";
      for (const row of data.miner_estimates || []) {
        const tr = document.createElement("tr");
        const workers = (row.workers || []).join(", ");
        const creditWorkers = (row.credit_workers || []).join(", ");
        const workerNote = creditWorkers ? `credited: ${shortEth(creditWorkers)}` : "";
        const identity = minerIdentity(row);
        const color = minerColor(identity);
        const name = minerDisplayLabel(row);
        tr.className = "miner-row";
        tr.style.setProperty("--miner-row-color", transparentColor(color, 0.08));
        tr.style.setProperty("--miner-color", color);
        tr.innerHTML = `<td class="nowrap miner-name"><span class="miner-dot"></span>${escapeHtml(name)} <span class="subtle">${escapeHtml(row.device_type || "")}</span></td><td class="nowrap" title="${escapeHtml(workers)}">${escapeShortEth(workers)}${workerNote ? ` <span class="subtle">${escapeHtml(workerNote)}</span>` : ""}</td><td class="right">${fmt(row.shares)}</td><td class="right">${escapeHtml(row.work_percent)}</td><td class="right">${fmt(row.credited_blocks || 0)}</td><td class="right">${escapeHtml(row.credited_bdag_total || "0")}</td><td class="right">${fmt(row.blocks_found)}</td><td class="right">${escapeHtml(row.estimated_wallet_bdag_total || row.estimated_bdag_total || "")}</td><td class="right">${escapeHtml(row.estimated_wallet_bdag_recent_hour || row.estimated_bdag_avg_hour || row.estimated_bdag_1h || "")}</td><td class="right">${escapeHtml(row.estimated_wallet_bdag_avg_hour || row.tracked_avg_bdag_hour || "")}</td><td class="right">${currency(row.estimated_wallet_usd_total || row.estimated_usd_total, "$")}</td><td class="right">${currency(row.estimated_wallet_zar_total || row.estimated_zar_total, "R")}</td><td>${escapeHtml(row.last_share_at || "")}</td>`;
        minerBody.appendChild(tr);
      }

      drawEarningsChart(data);
      drawMinerWorkChart(data);
      renderSamplerAlert("earningsSamplerAlert", data);
      renderSamplerAlert("minerWorkSamplerAlert", data);
      text("priceFeedOutput", JSON.stringify({price: data.price, earnings_24h: data.earnings_24h, onchain_earnings: data.onchain_earnings, payment_wallet_balance: data.payment_wallet_balance, credit_wallet_balance: data.credit_wallet_balance, wallet_balance: data.wallet_balance, hourly_averages: data.hourly_averages, credit_balance_check: data.credit_balance_check}, null, 2));
      text("earningsHistoryOutput", JSON.stringify({snapshot_log: data.snapshot_log, recent: (data.history || []).slice(-24)}, null, 2));
    }
    function renderGlobal(data) {
      lastGlobalData = data;
      text("globalLatestBlock", fmt(data.latest_block));
      text("globalScannedBlocks", fmt(data.fetched_blocks || data.requested_blocks));
      text("globalUniqueMiners", fmt(data.unique_miners));
      text("globalScanWindow", data.scan_window_hours ? `${data.scan_window_hours}h` : "n/a");
      text("globalAvgBlockSec", data.avg_block_seconds ? `${data.avg_block_seconds}s` : "n/a");
      text("globalTopShare", data.clusters?.[0]?.share_percent ? `${data.clusters[0].share_percent}%` : "n/a");

      const peerBody = document.getElementById("globalPeerIpsTable");
      peerBody.innerHTML = "";
      for (const item of data.peer_location?.observations || []) {
        const tr = document.createElement("tr");
        const seenBy = (item.seen_by || []).join(", ");
        tr.innerHTML = `<td class="nowrap">${escapeHtml(item.ip || "")}</td><td>${escapeHtml(item.location || "")}</td><td>${escapeHtml(item.country_code || item.country || "")}</td><td>${escapeHtml(item.region_code || item.region || "")}</td><td>${escapeHtml(item.city || "")}</td><td>${escapeHtml(item.asn ? String(item.asn) : "")}</td><td>${escapeHtml(item.org || "")}</td><td class="right nowrap">${escapeHtml(seenBy || "1")}</td>`;
        peerBody.appendChild(tr);
      }

      const body = document.getElementById("globalPoolsTable");
      body.innerHTML = "";
      for (const row of data.clusters || []) {
        const tr = document.createElement("tr");
        const nodes = globalNodesLabel(row);
        const share = row.share_percent ? `${escapeHtml(row.share_percent)}%` : "n/a";
        const poolName = row.pool_name || globalPoolName(row.address);
        const poolAddress = row.address || row.address_short || "";
        const poolIdentity = globalPoolIdentity(row);
        const poolColor = globalPoolColor(poolIdentity);
        const poolCell = poolName
          ? `<span class="pool-dot"></span>${escapeHtml(poolName)} <span class="subtle">${escapeShortEth(poolAddress)}</span>`
          : `<span class="pool-dot"></span>${escapeShortEth(poolAddress)}`;
        tr.className = "pool-row";
        tr.style.setProperty("--pool-row-color", transparentColor(poolColor, 0.08));
        tr.style.setProperty("--pool-color", poolColor);
        tr.innerHTML = `<td class="nowrap pool-name" title="${escapeHtml(poolAddress)}">${poolCell}</td><td class="nowrap">${escapeHtml(nodes || "")}</td><td class="right">${fmt(row.blocks)}</td><td class="right">${share}</td><td class="right">${fmt(row.blocks)}</td><td class="right">${escapeHtml(row.estimated_bdag || "")}</td><td class="right">${fmt(row.blocks)}</td><td class="right">${escapeHtml(row.estimated_bdag || "")}</td><td class="right">${currency(row.estimated_usd_avg_hour || row.estimated_usd_recent_hour, "$")}</td><td class="right">${currency(row.estimated_bdag_avg_hour || row.estimated_bdag_recent_hour, "")}</td><td class="right">${currency(row.estimated_usd, "$")}</td><td class="right">${currency(row.estimated_zar, "R")}</td><td class="nowrap">${escapeHtml(formatDisplayTime(row.last_seen_at))}</td>`;
        body.appendChild(tr);
      }
      drawGlobalChart(data);
    }
    async function action(name) {
      if (busy) return;
      if (name === "clean_restore" && !confirm("This stops the stack, backs up node data, restores the latest snapshot, and starts again. Continue?")) return;
      busy = true;
      for (const btn of document.querySelectorAll("button")) btn.disabled = true;
      try {
        const response = await fetch("/api/action", {
          method: "POST",
          headers: {"content-type": "application/json"},
          body: JSON.stringify({action: name, token: document.getElementById("token").value})
        });
        const payload = await response.json();
        if (!response.ok) alert(payload.error || "Action failed");
        await refresh();
      } catch (error) {
        alert(String(error));
      } finally {
        busy = false;
        for (const btn of document.querySelectorAll("button")) btn.disabled = false;
      }
    }
    setTheme(currentTheme());
    refresh();
    setInterval(refresh, 90000);
    setInterval(() => {
      if (earningsLoaded && (
        !document.getElementById("tab-earnings").classList.contains("hidden")
        || !document.getElementById("tab-miners").classList.contains("hidden")
      )) refreshEarnings();
    }, 90000);
    setInterval(() => { if (globalLoaded && !document.getElementById("tab-global").classList.contains("hidden")) refreshGlobal(); }, 300000);
    window.addEventListener("resize", () => {
      if (lastEarningsData && !document.getElementById("tab-earnings").classList.contains("hidden")) drawEarningsChart(lastEarningsData);
      if (lastEarningsData && !document.getElementById("tab-miners").classList.contains("hidden")) drawMinerWorkChart(lastEarningsData);
      if (lastGlobalData && !document.getElementById("tab-global").classList.contains("hidden")) drawGlobalChart(lastGlobalData);
    });
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "BDAGDashboard/1.0"
    client_disconnect_errors = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)

    def log_client_disconnect(self, exc: BaseException) -> None:
        client = self.client_address[0] if self.client_address else "unknown"
        with (RUNTIME_DIR / "dashboard-access.log").open("a", encoding="utf-8") as log:
            log.write(f"[{now_iso()}] {client} client disconnected during response: {exc.__class__.__name__}\n")

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature.
        with (RUNTIME_DIR / "dashboard-access.log").open("a", encoding="utf-8") as log:
            log.write(f"[{now_iso()}] {self.address_string()} {fmt % args}\n")

    def send_body(self, body: bytes, content_type: str, status: int = 200) -> None:
        try:
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except self.client_disconnect_errors as exc:
            self.log_client_disconnect(exc)

    def send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_body(body, "application/json; charset=utf-8", status)

    def serve_report(self, path: str) -> None:
        rel = unquote(path.removeprefix("/reports/"))
        if not rel or "/" in rel or "\\" in rel:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        report_path = (REPORTS_DIR / rel).resolve()
        reports_root = REPORTS_DIR.resolve()
        if reports_root not in report_path.parents or report_path.suffix.lower() != ".html" or not report_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_body(report_path.read_bytes(), "text/html; charset=utf-8", HTTPStatus.OK)

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name.
        path = urlparse(self.path).path
        if path == "/":
            body = HTML.encode("utf-8")
            self.send_body(body, "text/html; charset=utf-8", HTTPStatus.OK)
            return
        if path.startswith("/reports/"):
            self.serve_report(path)
            return
        if path == "/api/status":
            self.send_json(cached_payload("status", STATUS_CACHE_SECONDS, dashboard_status_payload))
            return
        if path == "/api/token-required":
            self.send_json({"required": token_required(), "token_file": str(RUNTIME_DIR / "dashboard-token.txt")})
            return
        if path == "/api/miners/defaults":
            self.send_json(default_miner_pool_settings())
            return
        if path == "/api/miners/registry":
            self.send_json(read_miner_registry())
            return
        if path == "/api/global":
            try:
                self.send_json(cached_payload("global", GLOBAL_CACHE_SECONDS, collect_global_blockchain))
            except Exception as exc:  # noqa: BLE001
                self.send_json({"error": str(exc)}, status=500)
            return
        if path == "/api/earnings":
            self.send_json(cached_payload("earnings", EARNINGS_CACHE_SECONDS, lambda: collect_earnings(include_history=True)))
            return
        if path == "/api/sampler":
            self.send_json(cached_payload("sampler", SAMPLER_CACHE_SECONDS, collect_sampler_status))
            return
        if path == "/api/incidents":
            self.send_json({"generated_at": now_iso(), "incidents": read_recent_incidents(100)})
            return
        if path == "/api/router":
            router_path = RUNTIME_DIR / "rpc-router-state.json"
            if router_path.exists():
                try:
                    self.send_json(json.loads(router_path.read_text(encoding="utf-8")))
                except json.JSONDecodeError as exc:
                    self.send_json({"generated_at": now_iso(), "error": str(exc)}, status=500)
            else:
                self.send_json({"generated_at": now_iso(), "error": "router state not available"}, status=404)
            return
        if path == "/api/p2p":
            if P2P_GUARD_STATE.exists():
                try:
                    self.send_json(json.loads(P2P_GUARD_STATE.read_text(encoding="utf-8")))
                except json.JSONDecodeError as exc:
                    self.send_json({"generated_at": now_iso(), "error": str(exc)}, status=500)
            else:
                self.send_json({"generated_at": now_iso(), "error": "p2p guard state not available"}, status=404)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name.
        path = urlparse(self.path).path
        if path not in {"/api/action", "/api/miners/scan", "/api/miners/configure", "/api/miners/save-auth"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        length = int(self.headers.get("content-length", "0") or "0")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.send_json({"error": "invalid JSON"}, status=400)
            return

        if token_required() and payload.get("token") != get_action_token():
            self.send_json({"error": "invalid action token"}, status=403)
            return
        with API_CACHE_LOCK:
            API_CACHE.clear()

        if path == "/api/miners/scan":
            try:
                result = scan_miners(payload.get("target"))
                defaults = default_miner_pool_settings()
                upsert_miner_registry(result.get("miners", []), defaults["pool_url"], defaults["worker_user"])
                self.send_json(result)
            except Exception as exc:  # noqa: BLE001 - return scanner validation errors to the browser.
                self.send_json({"error": str(exc)}, status=400)
            return

        if path == "/api/miners/save-auth":
            try:
                result = save_miner_admin_password(str(payload.get("admin_password") or ""))
                write_action_state({"name": "save-miner-auth", "status": "ok", "finished_at": now_iso()})
                self.send_json(result)
            except Exception as exc:  # noqa: BLE001
                self.send_json({"error": str(exc)}, status=400)
            return

        if path == "/api/miners/configure":
            ips = payload.get("ips") or []
            if not isinstance(ips, list) or not all(isinstance(item, str) for item in ips):
                self.send_json({"error": "ips must be a list of miner IP addresses"}, status=400)
                return
            if not ips:
                self.send_json({"error": "no miners selected"}, status=400)
                return
            admin_password = str(payload.get("admin_password") or "")
            pool_url = str(payload.get("pool_url") or "")
            worker_user = str(payload.get("worker_user") or "")
            pool_password = str(payload.get("pool_password") or "1234")
            if not admin_password or not pool_url or not worker_user:
                self.send_json({"error": "admin_password, pool_url, and worker_user are required"}, status=400)
                return
            result = configure_miners(
                ips=ips,
                admin_password=admin_password,
                pool_url=pool_url,
                worker_user=worker_user,
                pool_password=pool_password,
                replace_existing=True,
            )
            mark_configured_miners(result.get("results", []), pool_url, worker_user)
            write_action_state(
                {
                    "name": "configure-miners",
                    "status": result["status"],
                    "finished_at": now_iso(),
                    "miner_count": len(ips),
                    "pool_url": pool_url,
                    "worker_user": worker_user,
                    "results": [
                        {
                            "ip": item.get("ip"),
                            "status": item.get("status"),
                            "active": item.get("active"),
                            "backup_path": item.get("backup_path"),
                            "error": item.get("error"),
                            "delete_errors": item.get("delete_errors"),
                        }
                        for item in result.get("results", [])
                    ],
                }
            )
            self.send_json(result)
            return

        action = payload.get("action")
        if action == "start":
            result = start_background_action("start", [sys.executable, str(WATCHDOG), "--repair", "start", "--reason", "dashboard start"], "dashboard start")
        elif action == "restart":
            result = start_background_action("restart", [sys.executable, str(WATCHDOG), "--repair", "restart", "--reason", "dashboard restart"], "dashboard restart")
        elif action == "clean_restore":
            result = start_background_action("clean-restore", [sys.executable, str(WATCHDOG), "--repair", "clean", "--reason", "dashboard clean restore"], "dashboard clean restore")
        elif action == "handoff":
            path = make_handoff()
            result = {"status": "ok", "path": str(path)}
            write_action_state({"name": "codex-handoff", "status": "ok", "finished_at": now_iso(), "path": str(path)})
        else:
            self.send_json({"error": f"unknown action: {action}"}, status=400)
            return
        self.send_json(result)


def main() -> int:
    ensure_runtime()
    if token_required():
        token = get_action_token()
        print(f"Action token file: {RUNTIME_DIR / 'dashboard-token.txt'}")
        print(f"Action token: {token}")
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"BlockDAG dashboard listening on http://{HOST}:{PORT}")
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
