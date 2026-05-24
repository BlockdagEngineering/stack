#!/usr/bin/env python3
"""Read-only Prometheus exporter for the local BlockDAG operations dashboard."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


BASE_URL = os.environ.get("BDAG_DASHBOARD_BASE_URL", "http://127.0.0.1:8088").rstrip("/")
BIND = os.environ.get("BDAG_EXPORTER_BIND", "127.0.0.1")
PORT = int(os.environ.get("BDAG_EXPORTER_PORT", "9108"))
TIMEOUT = float(os.environ.get("BDAG_EXPORTER_TIMEOUT", "8"))
STATUS_CACHE_SECONDS = float(os.environ.get("BDAG_STATUS_CACHE_SECONDS", "30"))
EARNINGS_CACHE_SECONDS = float(os.environ.get("BDAG_EARNINGS_CACHE_SECONDS", "300"))
GLOBAL_CACHE_SECONDS = float(os.environ.get("BDAG_GLOBAL_CACHE_SECONDS", "300"))
SAMPLER_CACHE_SECONDS = float(os.environ.get("BDAG_SAMPLER_CACHE_SECONDS", "30"))
ROUTER_CACHE_SECONDS = float(os.environ.get("BDAG_ROUTER_CACHE_SECONDS", "30"))
INCIDENT_CACHE_SECONDS = float(os.environ.get("BDAG_INCIDENT_CACHE_SECONDS", "60"))
P2P_CACHE_SECONDS = float(os.environ.get("BDAG_P2P_CACHE_SECONDS", "60"))
CACHE: dict[str, tuple[float, dict[str, Any] | None, float, str]] = {}


def fetch_json(path: str) -> tuple[dict[str, Any] | None, float, str]:
    started = time.time()
    try:
        with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=TIMEOUT) as response:
            raw = response.read()
        elapsed = time.time() - started
        return json.loads(raw.decode("utf-8")), elapsed, ""
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return None, time.time() - started, str(exc)


def fetch_json_cached(path: str, ttl: float) -> tuple[dict[str, Any] | None, float, str]:
    now = time.time()
    cached = CACHE.get(path)
    if cached and now - cached[0] < ttl:
        _, data, elapsed, error = cached
        return data, elapsed, error
    data, elapsed, error = fetch_json(path)
    CACHE[path] = (now, data, elapsed, error)
    return data, elapsed, error


def number(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def bool_number(value: Any) -> float:
    return 1.0 if bool(value) else 0.0


def status_number(value: Any) -> float:
    text = str(value or "").lower()
    if text in {"ok", "connected", "synced", "running"}:
        return 1.0
    if text in {"syncing", "degraded", "warning"}:
        return 0.5
    return 0.0


def esc(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def node_role(name: str, row: dict[str, Any] | None = None) -> str:
    if row and row.get("role"):
        return str(row.get("role"))
    return "observer" if str(name).startswith("bdag-observer-node-") else "managed"


def node_health_scope(name: str, row: dict[str, Any] | None = None) -> str:
    if row and row.get("health_scope"):
        return str(row.get("health_scope"))
    return "advisory" if node_role(name, row) == "observer" else "production"


def node_labels(name: str, row: dict[str, Any] | None = None) -> dict[str, Any]:
    role = node_role(name, row)
    return {"node": name, "role": role, "health_scope": node_health_scope(name, row)}


class Metrics:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.seen: set[str] = set()

    def help(self, name: str, text: str, metric_type: str = "gauge") -> None:
        if name in self.seen:
            return
        self.lines.append(f"# HELP {name} {text}")
        self.lines.append(f"# TYPE {name} {metric_type}")
        self.seen.add(name)

    def gauge(self, name: str, value: Any, help_text: str, labels: dict[str, Any] | None = None) -> None:
        self.help(name, help_text, "gauge")
        label_text = ""
        if labels:
            label_text = "{" + ",".join(f'{key}="{esc(value)}"' for key, value in sorted(labels.items())) + "}"
        self.lines.append(f"{name}{label_text} {number(value)}")

    def counter(self, name: str, value: Any, help_text: str, labels: dict[str, Any] | None = None) -> None:
        self.help(name, help_text, "counter")
        label_text = ""
        if labels:
            label_text = "{" + ",".join(f'{key}="{esc(value)}"' for key, value in sorted(labels.items())) + "}"
        self.lines.append(f"{name}{label_text} {max(0.0, number(value))}")

    def render(self) -> bytes:
        return ("\n".join(self.lines) + "\n").encode("utf-8")


def add_status_metrics(metrics: Metrics, data: dict[str, Any] | None, scrape_ok: bool, elapsed: float) -> None:
    metrics.gauge("bdag_dashboard_api_up", bool_number(scrape_ok), "Old dashboard status API availability.", {"api": "status"})
    metrics.gauge("bdag_dashboard_api_scrape_seconds", elapsed, "Old dashboard API scrape duration.", {"api": "status"})
    if not data:
        return

    metrics.gauge("bdag_stack_status", status_number(data.get("overall")), "Overall stack status as numeric state.", {"state": data.get("overall", "unknown")})
    metrics.gauge("bdag_status_contract_version", number(data.get("status_version")), "Dashboard status contract version.")
    metrics.gauge("bdag_status_fresh", bool_number(data.get("fresh", True)), "Dashboard status payload freshness flag.")
    metrics.gauge("bdag_status_age_seconds", number(data.get("age_seconds")), "Dashboard status payload age in seconds.")
    metrics.gauge("bdag_status_can_mine", bool_number(data.get("can_mine")), "Stack can send mining work and submit blocks.")
    metrics.gauge("bdag_status_can_accept_shares", bool_number(data.get("can_accept_shares")), "Stack can accept miner shares.")
    metrics.gauge("bdag_status_can_submit_blocks", bool_number(data.get("can_submit_blocks")), "Stack can submit solved blocks.")
    metrics.gauge("bdag_stack_mode", 1.0, "Current BlockDAG desired-state mode.", {"mode": data.get("mode", "unknown")})

    progress = data.get("sync_progress") or {}
    metrics.gauge("bdag_sync_progress_percent", number(progress.get("percent")), "Overall BlockDAG sync progress percentage.")
    metrics.gauge("bdag_sync_remaining_blocks", number(progress.get("remaining_blocks")), "Overall remaining sync blocks.")

    sync_health = data.get("sync_health") or {}
    metrics.gauge("bdag_node_block_lag", number(sync_health.get("block_lag")), "Block height lag between configured nodes.")
    metrics.gauge("bdag_node_main_order_lag", number(sync_health.get("main_order_lag")), "Main-order lag between configured nodes.")
    metrics.gauge("bdag_node_recent_importers", number(sync_health.get("nodes_with_recent_imports")), "Nodes with recent imports.")

    for name, node in (data.get("nodes") or {}).items():
        labels = node_labels(name, node if isinstance(node, dict) else None)
        affects_production = node.get("affects_production_health") if isinstance(node, dict) else labels["role"] == "managed"
        if affects_production is None:
            affects_production = labels["role"] == "managed"
        metrics.gauge(
            "bdag_node_affects_production_health",
            bool_number(affects_production),
            "Node participates in production stack health decisions.",
            labels,
        )
        metrics.gauge("bdag_node_child_running", bool_number(node.get("child_running")), "Node child process is running.", labels)
        metrics.gauge("bdag_node_latest_block", number(node.get("latest_block")), "Latest imported EVM block by node.", labels)
        metrics.gauge("bdag_node_last_import_age_seconds", number(node.get("last_import_age_seconds")), "Seconds since last node import.", labels)
        metrics.gauge("bdag_node_import_count_recent", number(node.get("import_count")), "Recent import lines observed in node logs.", labels)
        metrics.gauge("bdag_node_p2p_stream_errors_recent", number(node.get("p2p_stream_errors")), "Recent P2P stream-reset errors.", labels)
        metrics.gauge("bdag_node_template_errors_recent", number(node.get("mining_template_error_count")), "Recent mining template errors.", labels)
        node_progress = (progress.get("nodes") or {}).get(name) or {}
        metrics.gauge("bdag_node_sync_progress_percent", number(node_progress.get("percent")), "Per-node sync progress percentage.", labels)
        metrics.gauge("bdag_node_sync_remaining_blocks", number(node_progress.get("remaining_blocks")), "Per-node remaining sync blocks.", labels)

    pool = data.get("pool_health") or data.get("pool") or {}
    metrics.gauge("bdag_pool_connected_miners", number(pool.get("connected_miners")), "Connected miner count from pool health.")
    metrics.gauge("bdag_pool_managed_miners", number(pool.get("managed_miners")), "Managed miner count from pool health.")
    metrics.gauge("bdag_pool_valid_shares_recent", pool.get("valid_share_count"), "Valid shares in the recent pool log window.")
    metrics.gauge("bdag_pool_submits_recent", pool.get("submit_count"), "Submit events in the recent pool log window.")
    metrics.gauge("bdag_pool_stale_submits_recent", pool.get("stale_submit_count"), "Stale submit events in the recent pool log window.")
    metrics.gauge("bdag_pool_accepted_job_expired_storm", bool_number(pool.get("accepted_job_expired_storm")), "Pool acceptedJobs expired-submit storm state.")
    metrics.gauge("bdag_pool_block_submit_success_recent", pool.get("block_submit_success_count"), "Successful block submissions in the recent pool log window.")
    metrics.gauge("bdag_pool_block_submit_error_recent", pool.get("block_submit_error_count"), "Failed block submissions in the recent pool log window.")
    metrics.gauge("bdag_pool_job_notify_recent", pool.get("job_notify_count"), "Job notify or difficulty pushes in the recent pool log window.")
    metrics.gauge("bdag_pool_head_changes_recent", pool.get("head_change_count"), "Template head changes in the recent pool log window.")
    metrics.gauge("bdag_pool_duplicate_blocks_recent", pool.get("duplicate_block_count"), "Duplicate block submissions in the recent pool log window.")
    metrics.gauge("bdag_pool_tip_overdue_recent", pool.get("tip_overdue_count"), "Tip-overdue block submit responses in the recent pool log window.")
    metrics.gauge("bdag_pool_template_fetch_errors_recent", pool.get("gbt_errors"), "Template fetch errors in the recent pool log window.")
    metrics.gauge("bdag_pool_last_valid_share_age_seconds", pool.get("last_valid_share_age_seconds"), "Seconds since last valid pool share.")
    metrics.gauge("bdag_pool_last_job_notify_age_seconds", pool.get("last_job_notify_age_seconds"), "Seconds since last stratum job notify.")
    metrics.gauge("bdag_pool_last_head_change_age_seconds", pool.get("last_head_change_age_seconds"), "Seconds since last template head change.")
    metrics.gauge("bdag_pool_last_block_submit_age_seconds", pool.get("last_block_submit_age_seconds"), "Seconds since last block submit.")
    metrics.gauge("bdag_pool_share_stall", bool_number(pool.get("share_stall")), "Pool share stall state.")
    metrics.gauge("bdag_pool_job_stall", bool_number(pool.get("job_stall")), "Pool job notify stall state.")
    metrics.gauge("bdag_pool_template_frozen", bool_number(pool.get("pool_template_frozen")), "Pool template frozen state.")
    metrics.gauge("bdag_pool_duplicate_block_storm", bool_number(pool.get("duplicate_block_storm")), "Pool duplicate block storm state.")
    metrics.gauge("bdag_pool_stale_job_candidates_recent", pool.get("stale_job_candidate_count"), "Stale-job block candidates in the recent pool log window.")
    metrics.gauge("bdag_pool_stale_job_candidate_storm", bool_number(pool.get("stale_job_candidate_storm")), "Pool stale-job candidate storm state.")
    metrics.gauge("bdag_pool_block_submit_error_storm", bool_number(pool.get("block_submit_error_storm")), "Pool block submit error storm state.")

    miner_health = data.get("miner_health") or {}
    metrics.gauge("bdag_miner_managed_count", number(miner_health.get("managed_count")), "Tracked managed miners.")
    metrics.gauge("bdag_miner_ok_count", number(miner_health.get("ok_count")), "Tracked miners in ok state.")
    metrics.gauge("bdag_miner_connected_count", number(miner_health.get("connected_count")), "Tracked connected miners.")
    for miner in miner_health.get("miners") or []:
        name = miner.get("display_name") or miner.get("ip") or "unknown"
        labels = {
            "miner": name,
            "ip": miner.get("ip", ""),
            "mac": miner.get("mac", ""),
            "worker": (miner.get("expected_worker_user") or "")[:10],
        }
        metrics.gauge("bdag_miner_up", 1.0 if miner.get("status") == "ok" else 0.0, "Miner status is ok.", labels)
        metrics.gauge("bdag_miner_connected", bool_number(miner.get("connected") or miner.get("pool_active")), "Miner is connected or pool-active.", labels)
        metrics.gauge("bdag_miner_configured", bool_number(miner.get("configured")), "Miner is configured for the expected pool.", labels)
        metrics.gauge("bdag_miner_work_percent", number(miner.get("work_percent")), "Miner accepted work share percentage.", labels)
        metrics.gauge("bdag_miner_last_share_age_seconds", miner.get("last_share_age_seconds"), "Seconds since miner last share.", labels)
        metrics.gauge("bdag_miner_last_submit_age_seconds", miner.get("last_submit_age_seconds"), "Seconds since miner last submit.", labels)
        metrics.gauge("bdag_miner_shares_recent", miner.get("shares"), "Miner shares in recent pool log window.", labels)
        metrics.gauge("bdag_miner_submits_recent", miner.get("submits"), "Miner submits in recent pool log window.", labels)
        metrics.gauge("bdag_miner_blocks_found_recent", miner.get("blocks_found"), "Miner blocks found in recent pool log window.", labels)
        debug = miner.get("debug") or {}
        metrics.gauge("bdag_miner_hashrate_ghs", number(debug.get("hashrate")), "ASIC reported current hashrate in GH/s where available.", labels)
        metrics.gauge("bdag_miner_avg_hashrate_ghs", number(debug.get("av_hashrate")), "ASIC reported average hashrate in GH/s where available.", labels)
        metrics.gauge("bdag_miner_hw_error_ratio", number(debug.get("hwerr_ratio")), "ASIC hardware error ratio where available.", labels)


def add_earnings_metrics(metrics: Metrics, data: dict[str, Any] | None, scrape_ok: bool, elapsed: float) -> None:
    metrics.gauge("bdag_dashboard_api_up", bool_number(scrape_ok), "Old dashboard status API availability.", {"api": "earnings"})
    metrics.gauge("bdag_dashboard_api_scrape_seconds", elapsed, "Old dashboard API scrape duration.", {"api": "earnings"})
    if not data:
        return
    hourly = data.get("hourly_averages") or {}
    credit = data.get("credit_balance_check") or {}
    price = data.get("price") or {}
    metrics.gauge("bdag_wallet_balance_bdag", credit.get("wallet_bdag"), "Wallet balance in BDAG from old dashboard.")
    metrics.gauge("bdag_wallet_recent_bdag_per_hour", hourly.get("wallet_recent_bdag_hour"), "Recent estimated wallet BDAG per hour.")
    metrics.gauge("bdag_wallet_avg_bdag_per_hour", hourly.get("wallet_tracked_avg_bdag_hour") or hourly.get("wallet_avg_bdag_hour_since_pool_start"), "Tracked average wallet BDAG per hour.")
    metrics.gauge("bdag_wallet_24h_bdag", hourly.get("wallet_24h_bdag"), "Estimated wallet BDAG for last 24 hours.")
    metrics.gauge("bdag_price_usd", price.get("usd"), "BDAG price in USD from old dashboard price feed.")
    metrics.gauge("bdag_price_zar", price.get("zar"), "BDAG price in ZAR from old dashboard price feed.")
    metrics.gauge("bdag_wallet_total_usd", data.get("wallet_total_usd"), "Wallet balance converted to USD.")
    metrics.gauge("bdag_wallet_total_zar", data.get("wallet_total_zar"), "Wallet balance converted to ZAR.")
    metrics.gauge("bdag_wallet_24h_usd", data.get("wallet_24h_usd"), "Estimated wallet USD for last 24 hours.")
    metrics.gauge("bdag_wallet_24h_zar", data.get("wallet_24h_zar"), "Estimated wallet ZAR for last 24 hours.")
    metrics.gauge("bdag_earnings_history_stale", bool_number(data.get("history_stale")), "Whether the earnings/miner plot sampler history is stale.")
    metrics.gauge("bdag_earnings_history_latest_age_seconds", data.get("history_latest_age_seconds"), "Age of the latest valid earnings/miner plot snapshot.")
    metrics.gauge("bdag_earnings_history_expected_interval_seconds", data.get("history_expected_interval_seconds"), "Expected earnings/miner plot snapshot interval.")
    metrics.gauge("bdag_earnings_history_stale_threshold_seconds", data.get("history_stale_threshold_seconds"), "Age threshold after which earnings/miner plot history is considered stale.")
    metrics.gauge(
        "bdag_wallet_recent_usd_per_hour",
        number(hourly.get("wallet_recent_bdag_hour")) * number(price.get("usd")),
        "Recent estimated wallet USD per hour.",
    )
    metrics.gauge(
        "bdag_wallet_recent_zar_per_hour",
        number(hourly.get("wallet_recent_bdag_hour")) * number(price.get("zar")),
        "Recent estimated wallet ZAR per hour.",
    )
    for row in data.get("miner_estimates") or []:
        labels = {
            "miner": row.get("display_name") or row.get("ip") or "unknown",
            "ip": row.get("ip", ""),
            "mac": row.get("mac", ""),
        }
        metrics.gauge("bdag_miner_estimated_usd_per_hour", row.get("estimated_wallet_usd_recent_hour") or row.get("estimated_usd_avg_hour"), "Estimated USD per hour by miner.", labels)
        metrics.gauge("bdag_miner_estimated_bdag_per_hour", row.get("estimated_wallet_bdag_recent_hour") or row.get("estimated_bdag_avg_hour"), "Estimated BDAG per hour by miner.", labels)
        metrics.gauge("bdag_miner_estimated_zar_per_hour", row.get("estimated_wallet_zar_recent_hour") or row.get("estimated_zar_avg_hour"), "Estimated ZAR per hour by miner.", labels)
        metrics.gauge("bdag_miner_estimated_usd_total", row.get("estimated_wallet_usd_total") or row.get("estimated_usd_total"), "Estimated total USD by miner.", labels)
        metrics.gauge("bdag_miner_estimated_bdag_total", row.get("estimated_wallet_bdag_total") or row.get("estimated_bdag_total"), "Estimated total BDAG by miner.", labels)
        metrics.gauge("bdag_miner_credited_blocks", row.get("credited_blocks"), "Credited blocks by miner or worker scope.", labels)


def add_global_metrics(metrics: Metrics, data: dict[str, Any] | None, scrape_ok: bool, elapsed: float) -> None:
    metrics.gauge("bdag_dashboard_api_up", bool_number(scrape_ok), "Old dashboard status API availability.", {"api": "global"})
    metrics.gauge("bdag_dashboard_api_scrape_seconds", elapsed, "Old dashboard API scrape duration.", {"api": "global"})
    if not data:
        return
    metrics.gauge("bdag_global_latest_block", data.get("latest_block"), "Latest on-chain block observed by global scanner.")
    metrics.gauge("bdag_global_unique_miners", data.get("unique_miners"), "Unique miners observed in global scanner window.")
    metrics.gauge("bdag_global_scan_window_hours", data.get("scan_window_hours"), "Global scanner window hours.")
    metrics.gauge("bdag_global_avg_block_seconds", data.get("avg_block_seconds"), "Average block interval in global scanner window.")
    peer_location = data.get("peer_location") or {}
    metrics.gauge("bdag_peer_ip_count", peer_location.get("peer_ip_count"), "Public P2P peer IP count observed from node sockets.")
    metrics.gauge("bdag_peer_geo_ip_count", peer_location.get("geo_ip_count"), "Public P2P peer IPs with successful geolocation.")
    best_guess = peer_location.get("best_guess") or {}
    metrics.gauge(
        "bdag_peer_location_best_guess",
        1,
        "Best-guess peer location label with confidence.",
        {
            "location": best_guess.get("location") or peer_location.get("location") or "unknown",
            "level": best_guess.get("level") or "unknown",
            "confidence": best_guess.get("confidence") or peer_location.get("location_confidence") or "",
        },
    )
    rankings = peer_location.get("rankings") or {}
    for level in ("country", "region", "city", "asn"):
        for row in rankings.get(level) or []:
            labels = {
                "level": level,
                "label": row.get("label") or "unknown",
                "representative_ip": ((row.get("representative") or {}).get("ip") or ""),
            }
            metrics.gauge("bdag_peer_location_share_percent", row.get("share_percent"), "Observed peer location weighted share percent.", labels)
            metrics.gauge("bdag_peer_location_weight", row.get("count"), "Observed peer location weighted count.", labels)
    for row in peer_location.get("observations") or []:
        labels = {
            "peer_ip": row.get("ip") or "",
            "seen_by": ",".join(row.get("seen_by") or []),
            "country": row.get("country_code") or row.get("country") or "",
            "region": row.get("region_code") or row.get("region") or "",
            "city": row.get("city") or "",
            "asn": row.get("asn") or "",
            "org": row.get("org") or "",
        }
        metrics.gauge("bdag_peer_observed", 1, "Observed public P2P peer IP with location labels.", labels)
        metrics.gauge("bdag_peer_seen_count", row.get("seen_count"), "How many local nodes observed this peer IP.", labels)
    for row in data.get("clusters") or []:
        labels = {
            "pool": row.get("pool_name") or row.get("pool_label") or row.get("address_short") or "unknown",
            "address": row.get("address_short") or row.get("address") or "",
        }
        metrics.gauge("bdag_global_pool_work_percent", row.get("share_percent"), "Observed global pool block share percentage.", labels)
        metrics.gauge("bdag_global_pool_blocks", row.get("blocks"), "Observed global pool blocks in scan window.", labels)
        metrics.gauge("bdag_global_pool_estimated_usd_per_hour", row.get("estimated_usd_avg_hour") or row.get("estimated_usd_recent_hour"), "Estimated global pool USD per hour.", labels)


def add_sampler_metrics(metrics: Metrics, data: dict[str, Any] | None, scrape_ok: bool, elapsed: float) -> None:
    metrics.gauge("bdag_dashboard_api_up", bool_number(scrape_ok), "Old dashboard status API availability.", {"api": "sampler"})
    metrics.gauge("bdag_dashboard_api_scrape_seconds", elapsed, "Old dashboard API scrape duration.", {"api": "sampler"})
    if not data:
        return
    metrics.gauge("bdag_earnings_sampler_stale", bool_number(data.get("stale")), "Whether the lightweight earnings/miner plot sampler check is stale.")
    metrics.gauge("bdag_earnings_sampler_latest_age_seconds", data.get("latest_age_seconds"), "Age of the latest valid earnings/miner plot snapshot from the lightweight sampler check.")
    metrics.gauge("bdag_earnings_sampler_expected_interval_seconds", data.get("expected_interval_seconds"), "Expected earnings/miner plot sampler interval from the lightweight sampler check.")
    metrics.gauge("bdag_earnings_sampler_stale_threshold_seconds", data.get("stale_threshold_seconds"), "Stale threshold for earnings/miner plot sampler history.")
    metrics.gauge("bdag_earnings_sampler_status", status_number(data.get("status")), "Earnings/miner plot sampler status as numeric state.", {"state": data.get("status") or "unknown"})


def add_router_metrics(metrics: Metrics, data: dict[str, Any] | None, scrape_ok: bool, elapsed: float) -> None:
    metrics.gauge("bdag_dashboard_api_up", bool_number(scrape_ok), "Old dashboard status API availability.", {"api": "router"})
    metrics.gauge("bdag_dashboard_api_scrape_seconds", elapsed, "Old dashboard API scrape duration.", {"api": "router"})
    if not data:
        return
    current = data.get("current_primary") or ""
    recommended = data.get("recommended_primary") or ""
    pressure = data.get("pool_pressure") or {}
    metrics.gauge("bdag_rpc_router_should_switch", bool_number(data.get("should_switch")), "Router recommends switching RPC primary.")
    metrics.gauge("bdag_rpc_router_score_delta", data.get("score_delta"), "Recommended node score minus current primary score.")
    metrics.gauge("bdag_rpc_router_current_suboptimal", bool_number(data.get("current_primary_suboptimal")), "Router sees node-specific evidence that the current RPC primary is suboptimal.")
    metrics.gauge("bdag_rpc_router_hard_pool_pressure", bool_number(pressure.get("hard_pool_pressure")), "Router sees hard pool pressure that can require repair.")
    metrics.gauge("bdag_rpc_router_pool_quality_pressure", bool_number(pressure.get("pool_quality_pressure")), "Router sees degraded pool quality before a hard stall.")
    metrics.gauge("bdag_rpc_router_block_error_ratio", pressure.get("block_error_ratio"), "Recent block submit errors per successful block submit.")
    metrics.gauge("bdag_rpc_router_stale_job_ratio", pressure.get("stale_job_candidate_ratio"), "Recent stale-job candidates per successful block submit.")
    metrics.gauge("bdag_rpc_router_tip_overdue_ratio", pressure.get("tip_overdue_ratio"), "Recent tip-overdue submits per successful block submit.")
    metrics.gauge("bdag_rpc_router_valid_share_ratio", pressure.get("valid_share_ratio"), "Recent accepted shares per submit in the router quality window.")
    for node, row in (data.get("scores") or {}).items():
        labels = {"node": node}
        metrics.gauge("bdag_rpc_node_score", row.get("score"), "Application-aware RPC node health score.", labels)
        metrics.gauge("bdag_rpc_node_primary", 1 if node == current else 0, "Node is current HAProxy RPC primary.", labels)
        metrics.gauge("bdag_rpc_node_recommended", 1 if node == recommended else 0, "Node is recommended RPC primary.", labels)
        metrics.gauge("bdag_rpc_node_template_failing", bool_number(row.get("mining_template_failing")), "Router sees node template failure.", labels)
        metrics.gauge("bdag_rpc_node_p2p_stream_errors", row.get("p2p_stream_errors"), "Router recent P2P stream-reset errors by node.", labels)


def add_incident_metrics(metrics: Metrics, data: dict[str, Any] | None, scrape_ok: bool, elapsed: float) -> None:
    metrics.gauge("bdag_dashboard_api_up", bool_number(scrape_ok), "Old dashboard status API availability.", {"api": "incidents"})
    metrics.gauge("bdag_dashboard_api_scrape_seconds", elapsed, "Old dashboard API scrape duration.", {"api": "incidents"})
    if not data:
        return
    incidents = data.get("incidents") or []
    counts: dict[tuple[str, str], int] = {}
    latest_ts = 0.0
    for row in incidents:
        severity = str(row.get("severity") or "unknown")
        event_type = str(row.get("event_type") or "unknown")
        counts[(severity, event_type)] = counts.get((severity, event_type), 0) + 1
        generated = str(row.get("generated_at") or "")
        try:
            parsed = time.strptime(generated[:19], "%Y-%m-%dT%H:%M:%S")
            latest_ts = max(latest_ts, time.mktime(parsed))
        except ValueError:
            pass
    metrics.gauge("bdag_incidents_recent_total", len(incidents), "Recent structured incidents exposed by old dashboard.")
    metrics.gauge("bdag_incident_latest_timestamp_seconds", latest_ts, "Latest structured incident timestamp.")
    for (severity, event_type), count in counts.items():
        metrics.gauge(
            "bdag_incidents_recent_by_type",
            count,
            "Recent structured incidents by severity and type.",
            {"severity": severity, "event_type": event_type},
        )


def add_p2p_metrics(metrics: Metrics, data: dict[str, Any] | None, scrape_ok: bool, elapsed: float) -> None:
    metrics.gauge("bdag_dashboard_api_up", bool_number(scrape_ok), "Old dashboard status API availability.", {"api": "p2p"})
    metrics.gauge("bdag_dashboard_api_scrape_seconds", elapsed, "Old dashboard API scrape duration.", {"api": "p2p"})
    metrics.gauge("bdag_p2p_guard_up", bool_number(scrape_ok and data is not None), "P2P guard state availability.")
    if not data:
        return

    metrics.gauge("bdag_p2p_guard_state", status_number(data.get("guard_state")), "P2P guard state as numeric status.", {"state": data.get("guard_state", "unknown")})
    metrics.gauge("bdag_p2p_overall_score", data.get("overall_score"), "Lowest node P2P health score.")
    metrics.gauge("bdag_p2p_active_primary_score", data.get("active_primary_score"), "P2P score for the active RPC primary.")
    metrics.gauge("bdag_p2p_best_alternate_score", data.get("best_alternate_score"), "P2P score for the best alternate RPC backend.")

    active = str(data.get("active_primary") or "")
    best_alternate = str(data.get("best_alternate") or "")
    for node, row in (data.get("nodes") or {}).items():
        labels = node_labels(node, row if isinstance(row, dict) else None)
        metrics.gauge("bdag_p2p_node_score", row.get("score"), "P2P health score by node.", labels)
        metrics.gauge("bdag_p2p_node_active_primary", 1 if node == active else 0, "Node is active RPC primary.", labels)
        metrics.gauge("bdag_p2p_node_best_alternate", 1 if node == best_alternate else 0, "Node is the best alternate backend.", labels)
        metrics.gauge("bdag_p2p_node_public_peer_count", row.get("public_peer_count"), "Public peer IP count observed from node sockets.", labels)
        metrics.gauge("bdag_p2p_node_native_peer_count", row.get("native_peers"), "Native p2p peer count from node metrics.", labels)
        metrics.gauge("bdag_p2p_node_dial_errors_delta", row.get("native_dial_errors_delta"), "Native dial error delta since previous guard sample.", labels)
        metrics.gauge("bdag_p2p_node_ingress_bytes_delta", row.get("p2p_ingress_delta"), "Native P2P ingress byte delta since previous guard sample.", labels)
        metrics.gauge("bdag_p2p_node_egress_bytes_delta", row.get("p2p_egress_delta"), "Native P2P egress byte delta since previous guard sample.", labels)

    quality = data.get("pool_quality") or {}
    metrics.gauge("bdag_p2p_pool_valid_share_ratio", quality.get("valid_share_ratio"), "Accepted shares per submit in the guard sample.")
    metrics.gauge("bdag_p2p_pool_block_error_ratio", quality.get("block_error_ratio"), "Block submit errors per successful block submit in the guard sample.")
    metrics.gauge("bdag_p2p_pool_stale_job_ratio", quality.get("stale_job_ratio"), "Stale-job candidates per successful block submit in the guard sample.")
    metrics.gauge("bdag_p2p_pool_tip_overdue_ratio", quality.get("tip_overdue_ratio"), "Tip-overdue submits per successful block submit in the guard sample.")

    network = data.get("network") or {}
    route = network.get("default_route") or {}
    gateway = network.get("gateway_ping") or {}
    metrics.gauge("bdag_network_default_route_ok", bool_number(route.get("mining_interface_ok")), "Default route uses the wired mining interface.", {"interface": route.get("interface") or ""})
    metrics.gauge("bdag_network_default_route_wifi", bool_number(route.get("uses_wifi")), "Default route uses Wi-Fi.", {"interface": route.get("interface") or ""})
    metrics.gauge("bdag_network_default_route_zerotier", bool_number(route.get("uses_zerotier")), "Default route uses ZeroTier.", {"interface": route.get("interface") or ""})
    metrics.gauge("bdag_network_gateway_ping_up", bool_number(gateway.get("up")), "Default gateway ping succeeded.", {"gateway": gateway.get("ip") or route.get("gateway") or ""})
    metrics.gauge("bdag_network_gateway_rtt_ms", gateway.get("rtt_ms"), "Default gateway ICMP RTT in milliseconds.", {"gateway": gateway.get("ip") or route.get("gateway") or ""})
    peer_summary = network.get("public_peer_ping_summary") or {}
    miner_summary = network.get("miner_ping_summary") or {}
    metrics.gauge("bdag_p2p_public_peer_ping_up_count", peer_summary.get("up_count"), "Public P2P peers that answered the guard ping sample.")
    metrics.gauge("bdag_p2p_public_peer_ping_avg_rtt_ms", peer_summary.get("avg_rtt_ms"), "Average RTT for public peers that answered pings.")
    metrics.gauge("bdag_lan_miner_ping_up_count", miner_summary.get("up_count"), "LAN miners that answered the guard ping sample.")
    metrics.gauge("bdag_lan_miner_ping_avg_rtt_ms", miner_summary.get("avg_rtt_ms"), "Average RTT for LAN miners that answered pings.")
    for item in network.get("miner_pings") or []:
        labels = {"miner": item.get("miner") or item.get("ip") or "", "ip": item.get("ip") or ""}
        metrics.gauge("bdag_lan_miner_ping_up", bool_number(item.get("up")), "LAN miner ping state.", labels)
        metrics.gauge("bdag_lan_miner_ping_rtt_ms", item.get("rtt_ms"), "LAN miner ping RTT in milliseconds.", labels)


def collect_metrics() -> bytes:
    metrics = Metrics()
    status, status_elapsed, _ = fetch_json_cached("/api/status", STATUS_CACHE_SECONDS)
    earnings, earnings_elapsed, _ = fetch_json_cached("/api/earnings", EARNINGS_CACHE_SECONDS)
    global_data, global_elapsed, _ = fetch_json_cached("/api/global", GLOBAL_CACHE_SECONDS)
    sampler, sampler_elapsed, _ = fetch_json_cached("/api/sampler", SAMPLER_CACHE_SECONDS)
    router, router_elapsed, _ = fetch_json_cached("/api/router", ROUTER_CACHE_SECONDS)
    incidents, incidents_elapsed, _ = fetch_json_cached("/api/incidents", INCIDENT_CACHE_SECONDS)
    p2p, p2p_elapsed, _ = fetch_json_cached("/api/p2p", P2P_CACHE_SECONDS)
    add_status_metrics(metrics, status, status is not None, status_elapsed)
    add_earnings_metrics(metrics, earnings, earnings is not None, earnings_elapsed)
    add_global_metrics(metrics, global_data, global_data is not None, global_elapsed)
    add_sampler_metrics(metrics, sampler, sampler is not None, sampler_elapsed)
    add_router_metrics(metrics, router, router is not None, router_elapsed)
    add_incident_metrics(metrics, incidents, incidents is not None, incidents_elapsed)
    add_p2p_metrics(metrics, p2p, p2p is not None, p2p_elapsed)
    metrics.gauge("bdag_exporter_build_info", 1, "BDAG exporter build info.", {"version": "1"})
    return metrics.render()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            body = b"ok\n"
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split("?", 1)[0] != "/metrics":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            body = collect_metrics()
            status = HTTPStatus.OK
        except Exception as exc:  # noqa: BLE001
            body = f"# exporter_error {esc(exc)}\nbdag_exporter_error 1\n".encode("utf-8")
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        self.send_response(status)
        self.send_header("content-type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"bdag_exporter listening on http://{BIND}:{PORT}/metrics base_url={BASE_URL}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
