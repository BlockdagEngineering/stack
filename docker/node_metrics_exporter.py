#!/usr/bin/env python3
"""Expose release-stable BlockDAG node metrics from local BDAG RPC."""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


CONFIG_FILE = Path(os.environ.get("BDAG_NODE_CONFIG_FILE", "/etc/bdagStack/node.conf"))
DEFAULT_RPC_URL = os.environ.get("BDAG_NODE_METRICS_EXPORTER_RPC_URL", "http://127.0.0.1:38131")
DEFAULT_BIND_ADDR = os.environ.get("BDAG_NODE_METRICS_EXPORTER_ADDR", "0.0.0.0")
DEFAULT_BIND_PORT = int(os.environ.get("BDAG_NODE_METRICS_EXPORTER_PORT", "6060"))
RPC_TIMEOUT = float(os.environ.get("BDAG_NODE_METRICS_EXPORTER_RPC_TIMEOUT", "2.0"))

CONFIG_LINE_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=\s*(.*?)\s*$")


def read_config_values(path: Path = CONFIG_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = CONFIG_LINE_RE.match(line)
        if not match:
            continue
        key, value = match.groups()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        values[key] = value
    return values


def rpc_credentials(config: dict[str, str]) -> tuple[str, str]:
    user = os.environ.get("NODE_RPC_USER") or config.get("rpcuser") or "test"
    password = os.environ.get("NODE_RPC_PASS") or config.get("rpcpass") or "test"
    return user, password


def rpc_call(method: str, params: list[Any] | None = None) -> Any:
    config = read_config_values()
    user, password = rpc_credentials(config)
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": "node-metrics-exporter", "method": method, "params": params or []}
    ).encode("utf-8")
    request = urllib.request.Request(
        DEFAULT_RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if user or password:
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        request.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(request, timeout=RPC_TIMEOUT) as response:
        body = response.read().decode("utf-8", errors="replace")
    decoded = json.loads(body)
    if decoded.get("error"):
        raise RuntimeError(f"{method}: {decoded['error']}")
    return decoded.get("result")


def number(value: Any, default: float = 0) -> float:
    if isinstance(value, bool):
        return 1 if value else 0
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def boolean(value: Any) -> float:
    return 1 if bool(value) else 0


def build_metrics(
    *,
    health: dict[str, Any],
    active_peer_count: int,
    block_count: int | float,
    scrape_error: str,
) -> dict[str, float]:
    main_order = number(health.get("main_order"), number(block_count))
    metrics = {
        "node_metrics_exporter_scrape_success": 0 if scrape_error else 1,
        "node_metrics_exporter_last_scrape_unix_seconds": float(int(time.time())),
        "p2p_peers": float(active_peer_count),
        "p2p_peers_": float(active_peer_count),
        "p2p_miningFreshness_consensusPeers": number(health.get("p2p_consensus_peer_count")),
        "p2p_miningFreshness_freshConsensusPeers": number(
            health.get("p2p_fresh_consensus_peer_count")
        ),
        "p2p_miningFreshness_staleConsensusPeers": number(
            health.get("p2p_stale_consensus_peer_count")
        ),
        "p2p_miningFreshness_bestPeerMainOrder": number(
            health.get("p2p_best_peer_main_order")
        ),
        "p2p_miningFreshness_bestPeerLeadBlocks": number(
            health.get("p2p_best_peer_lead_blocks")
        ),
        "p2p_miningFreshness_bestGraphStateAgeMs": number(
            health.get("p2p_best_peer_graph_state_age_ms")
        ),
        "p2p_miningFreshness_avgGraphStateAgeMs": number(
            health.get("p2p_avg_graph_state_age_ms")
        ),
        "p2p_miningFreshness_maxGraphStateAgeMs": number(
            health.get("p2p_max_graph_state_age_ms")
        ),
        "p2p_miningFreshness_syncPeerPresent": boolean(
            health.get("p2p_sync_peer_present")
        ),
        "p2p_miningFreshness_syncPeerFresh": boolean(health.get("p2p_sync_peer_fresh")),
        "p2p_miningFreshness_syncPeerGraphStateAgeMs": number(
            health.get("p2p_sync_peer_graph_state_age_ms")
        ),
        "p2p_miningFreshness_miningFresh": boolean(health.get("p2p_mining_fresh")),
        "p2p_freshness_connectedConsensus": number(health.get("p2p_consensus_peer_count")),
        "p2p_freshness_staleConsensus": number(health.get("p2p_stale_consensus_peer_count")),
        "Blockdag_mainorder": main_order,
        "chain_head_block": number(block_count, main_order),
    }
    return metrics


def format_value(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def render_prometheus(metrics: dict[str, float]) -> str:
    lines = [
        "# HELP node_metrics_exporter_scrape_success Whether the latest BDAG RPC scrape succeeded.",
        "# TYPE node_metrics_exporter_scrape_success gauge",
    ]
    for name in sorted(metrics):
        if name != "node_metrics_exporter_scrape_success":
            lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {format_value(metrics[name])}")
    lines.append("")
    return "\n".join(lines)


def scrape_metrics() -> dict[str, float]:
    errors: list[str] = []
    health: dict[str, Any] = {}
    active_peer_count = 0
    block_count: int | float = 0

    try:
        result = rpc_call("getTemplateHealth")
        if isinstance(result, dict):
            health = result
    except Exception as exc:  # noqa: BLE001 - exporter must report scrape health.
        errors.append(f"getTemplateHealth: {exc}")

    try:
        result = rpc_call("getPeerInfo")
        if isinstance(result, list):
            active_peer_count = len(result)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"getPeerInfo: {exc}")

    try:
        result = rpc_call("getBlockCount")
        block_count = number(result)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"getBlockCount: {exc}")

    return build_metrics(
        health=health,
        active_peer_count=active_peer_count,
        block_count=block_count,
        scrape_error="; ".join(errors),
    )


class MetricsHandler(BaseHTTPRequestHandler):
    server_version = "BlockDAGNodeMetricsExporter/1.0"

    def do_GET(self) -> None:  # noqa: N802 - http.server API.
        if self.path in {"/", "/debug/metrics", "/debug/metrics/prometheus", "/metrics"}:
            body = render_prometheus(scrape_metrics()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("node-metrics-exporter: " + (fmt % args) + "\n")


def serve() -> None:
    try:
        server = ThreadingHTTPServer((DEFAULT_BIND_ADDR, DEFAULT_BIND_PORT), MetricsHandler)
    except OSError as exc:
        print(
            "node-metrics-exporter: not starting; "
            f"{DEFAULT_BIND_ADDR}:{DEFAULT_BIND_PORT} is unavailable: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return
    print(
        f"node-metrics-exporter: listening on {DEFAULT_BIND_ADDR}:{DEFAULT_BIND_PORT}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    serve()
