#!/usr/bin/env python3
"""Generate provisioned Grafana dashboards for the BlockDAG observability stack."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "grafana" / "dashboards"
PROM = {"type": "prometheus", "uid": "bdag-prometheus"}
LOKI = {"type": "loki", "uid": "bdag-loki"}


def grid(index: int, w: int = 8, h: int = 8) -> dict[str, int]:
    return {"h": h, "w": w, "x": (index % 3) * 8, "y": (index // 3) * h}


def pos(x: int, y: int, w: int, h: int) -> dict[str, int]:
    return {"x": x, "y": y, "w": w, "h": h}


def targets(items: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [
        {"expr": expr, "legendFormat": legend, "refId": chr(65 + index)}
        for index, (expr, legend) in enumerate(items)
    ]


def stat(panel_id: int, title: str, expr: str, idx: int, unit: str = "short", decimals: int | None = None) -> dict:
    field_config = {"defaults": {"unit": unit}, "overrides": []}
    if decimals is not None:
        field_config["defaults"]["decimals"] = decimals
    return {
        "id": panel_id,
        "type": "stat",
        "title": title,
        "datasource": PROM,
        "gridPos": grid(idx, 6, 5),
        "targets": targets([(expr, "")]),
        "fieldConfig": field_config,
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "orientation": "auto"},
    }


def stat_at(
    panel_id: int,
    title: str,
    expr: str,
    grid_pos: dict[str, int],
    unit: str = "short",
    decimals: int | None = None,
    thresholds: list[tuple[float | None, str]] | None = None,
    color_mode: str = "value",
) -> dict:
    field_config = {"defaults": {"unit": unit, "custom": {}, "color": {"mode": "thresholds"}}, "overrides": []}
    if decimals is not None:
        field_config["defaults"]["decimals"] = decimals
    if thresholds:
        field_config["defaults"]["thresholds"] = {
            "mode": "absolute",
            "steps": [{"value": value, "color": color} for value, color in thresholds],
        }
    return {
        "id": panel_id,
        "type": "stat",
        "title": title,
        "datasource": PROM,
        "gridPos": grid_pos,
        "targets": targets([(expr, "")]),
        "fieldConfig": field_config,
        "options": {
            "colorMode": color_mode,
            "graphMode": "none",
            "justifyMode": "center",
            "orientation": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "auto",
        },
    }


def compact_stat(
    panel_id: int,
    title: str,
    expr: str,
    grid_pos: dict[str, int],
    unit: str = "short",
    decimals: int | None = None,
    thresholds: list[tuple[float | None, str]] | None = None,
) -> dict:
    panel = stat_at(panel_id, title, expr, grid_pos, unit, decimals, thresholds, color_mode="background")
    panel["options"].update(
        {
            "graphMode": "none",
            "justifyMode": "center",
            "orientation": "auto",
            "textMode": "value_and_name",
            "wideLayout": True,
        }
    )
    panel["fieldConfig"]["defaults"].setdefault("custom", {})
    panel["fieldConfig"]["defaults"]["noValue"] = "-"
    return panel


def timeseries(panel_id: int, title: str, targets: list[dict[str, str]], idx: int, unit: str = "short", h: int = 9) -> dict:
    for target in targets:
        target["interval"] = "$__interval"
    return {
        "id": panel_id,
        "type": "timeseries",
        "title": title,
        "datasource": PROM,
        "gridPos": grid(idx, 12, h),
        "maxDataPoints": 1200,
        "targets": targets,
        "fieldConfig": {"defaults": {"unit": unit, "custom": {"drawStyle": "line", "lineInterpolation": "smooth", "showPoints": "never"}}, "overrides": []},
        "options": {"legend": {"displayMode": "table", "placement": "bottom"}, "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def timeseries_at(
    panel_id: int,
    title: str,
    target_items: list[tuple[str, str]],
    grid_pos: dict[str, int],
    unit: str = "short",
    decimals: int | None = None,
    draw_style: str = "line",
) -> dict:
    field_config = {
        "defaults": {
            "unit": unit,
            "custom": {
                "drawStyle": draw_style,
                "lineInterpolation": "smooth",
                "lineWidth": 2,
                "fillOpacity": 12 if draw_style == "line" else 70,
                "showPoints": "never",
                "spanNulls": True,
            },
        },
        "overrides": [],
    }
    if decimals is not None:
        field_config["defaults"]["decimals"] = decimals
    panel_targets = targets(target_items)
    for target in panel_targets:
        target["interval"] = "$__interval"
    return {
        "id": panel_id,
        "type": "timeseries",
        "title": title,
        "datasource": PROM,
        "gridPos": grid_pos,
        "maxDataPoints": 1200,
        "targets": panel_targets,
        "fieldConfig": field_config,
        "options": {
            "legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull", "mean"]},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def bargauge_at(
    panel_id: int,
    title: str,
    expr: str,
    legend: str,
    grid_pos: dict[str, int],
    unit: str = "short",
    decimals: int | None = None,
    thresholds: list[tuple[float | None, str]] | None = None,
) -> dict:
    field_config = {"defaults": {"unit": unit, "color": {"mode": "palette-classic"}}, "overrides": []}
    if decimals is not None:
        field_config["defaults"]["decimals"] = decimals
    if thresholds:
        field_config["defaults"]["color"] = {"mode": "thresholds"}
        field_config["defaults"]["thresholds"] = {
            "mode": "absolute",
            "steps": [{"value": value, "color": color} for value, color in thresholds],
        }
    return {
        "id": panel_id,
        "type": "bargauge",
        "title": title,
        "datasource": PROM,
        "gridPos": grid_pos,
        "targets": targets([(expr, legend)]),
        "fieldConfig": field_config,
        "options": {
            "displayMode": "gradient",
            "minVizHeight": 18,
            "minVizWidth": 0,
            "namePlacement": "left",
            "orientation": "horizontal",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showUnfilled": True,
            "sizing": "auto",
            "valueMode": "color",
        },
    }


def table_at(panel_id: int, title: str, target_items: list[tuple[str, str]], grid_pos: dict[str, int], unit: str = "short") -> dict:
    table_targets = [
        {"expr": expr, "legendFormat": legend, "refId": chr(65 + index), "instant": True, "format": "table"}
        for index, (expr, legend) in enumerate(target_items)
    ]
    return {
        "id": panel_id,
        "type": "table",
        "title": title,
        "datasource": PROM,
        "gridPos": grid_pos,
        "targets": table_targets,
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"cellHeight": "sm", "footer": {"show": False}, "showHeader": True},
        "transformations": [
            {
                "id": "organize",
                "options": {
                    "excludeByName": {
                        "Time": True,
                        "__name__": True,
                        "instance": True,
                        "job": True,
                        "service": True,
                        "ip": True,
                        "mac": True,
                    },
                    "renameByName": {"Value": "Value", "miner": "Miner", "node": "Node", "pool": "Pool"},
                },
            }
        ],
    }


def node_block_card(panel_id: int, title: str, node: str, grid_pos: dict[str, int]) -> dict:
    return {
        "id": panel_id,
        "type": "stat",
        "title": title,
        "datasource": PROM,
        "gridPos": grid_pos,
        "targets": targets([(f'bdag_node_latest_block{{node="{node}"}}', "")]),
        "fieldConfig": {
            "defaults": {
                "unit": "locale",
                "decimals": 0,
                "color": {"mode": "thresholds"},
                "custom": {},
                "noValue": "-",
            },
            "overrides": [],
        },
        "options": {
            "colorMode": "none",
            "graphMode": "none",
            "justifyMode": "center",
            "orientation": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "value",
            "wideLayout": False,
            "text": {"valueSize": 42, "titleSize": 12},
        },
    }


def node_sync_card(panel_id: int, title: str, node: str, grid_pos: dict[str, int]) -> dict:
    return {
        "id": panel_id,
        "type": "stat",
        "title": title,
        "datasource": PROM,
        "gridPos": grid_pos,
        "targets": targets(
            [
                (f'bdag_node_sync_progress_percent{{node="{node}"}}', "synced"),
                (f'bdag_node_sync_remaining_blocks{{node="{node}"}}', "gap blocks"),
            ]
        ),
        "fieldConfig": {
            "defaults": {
                "unit": "short",
                "decimals": 0,
                "color": {"mode": "thresholds"},
                "custom": {},
                "thresholds": {
                    "mode": "absolute",
                    "steps": [{"value": None, "color": "green"}],
                },
                "noValue": "-",
            },
            "overrides": [
                {"matcher": {"id": "byName", "options": "synced"}, "properties": [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 2}]},
                {"matcher": {"id": "byName", "options": "gap blocks"}, "properties": [{"id": "unit", "value": "locale"}, {"id": "decimals", "value": 0}]},
            ],
        },
        "options": {
            "colorMode": "none",
            "graphMode": "none",
            "justifyMode": "center",
            "orientation": "horizontal",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "value_and_name",
            "wideLayout": True,
            "text": {"valueSize": 22, "titleSize": 11},
        },
    }


def logs(panel_id: int, title: str, query: str, idx: int, h: int = 12) -> dict:
    return {
        "id": panel_id,
        "type": "logs",
        "title": title,
        "datasource": LOKI,
        "gridPos": grid(idx, 24, h),
        "targets": [{"expr": query, "refId": "A"}],
        "options": {"showTime": True, "showLabels": False, "wrapLogMessage": True, "sortOrder": "Descending"},
    }


def logs_at(panel_id: int, title: str, query: str, grid_pos: dict[str, int], h: int = 12) -> dict:
    panel = logs(panel_id, title, query, 0, h)
    panel["gridPos"] = grid_pos
    return panel


def dashboard(uid: str, title: str, panels: list[dict], extra_links: list[dict] | None = None) -> dict:
    links = [
        {"title": "Operator Cockpit", "url": "/d/bdag-operator-cockpit/bdag-operator-cockpit", "targetBlank": False, "type": "link"},
        {"title": "Accepted Work Share", "url": "/d/bdag-work-share/bdag-accepted-work-share", "targetBlank": False, "type": "link"},
        {"title": "Miner Freshness", "url": "/d/bdag-miner-freshness/bdag-miner-freshness", "targetBlank": False, "type": "link"},
        {"title": "Observed Peer IPs", "url": "/d/bdag-observed-peers/bdag-observed-peer-ips", "targetBlank": False, "type": "link"},
        {"title": "Template Fan-In", "url": "/d/bdag-template-fan-in/bdag-template-fan-in", "targetBlank": False, "type": "link"},
        {"title": "Old Repair Dashboard", "url": "http://127.0.0.1:8088", "targetBlank": True, "type": "link"},
    ]
    if extra_links:
        links = extra_links + links
    return {
        "uid": uid,
        "title": title,
        "tags": ["blockdag", "mining", "provisioned"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "timepicker": {
            "refresh_intervals": ["10s", "30s", "1m", "5m", "15m"],
            "time_options": ["1h", "6h", "12h", "24h", "3d", "7d", "30d"],
        },
        "editable": False,
        "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"}, "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)", "name": "Annotations & Alerts", "type": "dashboard"}]},
        "links": links,
        "panels": panels,
    }


def write(name: str, payload: dict) -> None:
    DASH_DIR.mkdir(parents=True, exist_ok=True)
    (DASH_DIR / name).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    write(
        "operator-cockpit.json",
        dashboard(
            "bdag-operator-cockpit",
            "BDAG Operator Cockpit",
            [
                compact_stat(1, "Mining State", "max(bdag_stack_status)", pos(0, 0, 3, 3), thresholds=[(None, "red"), (0.5, "orange"), (1, "green")]),
                compact_stat(2, "USD / Hour", "bdag_wallet_recent_usd_per_hour", pos(3, 0, 3, 3), "currencyUSD", 2, thresholds=[(None, "red"), (0.01, "green")]),
                compact_stat(3, "24h USD", "bdag_wallet_24h_usd", pos(6, 0, 3, 3), "currencyUSD", 2),
                compact_stat(4, "Connected ASICs", "bdag_miner_connected_count", pos(9, 0, 3, 3), thresholds=[(None, "red"), (6, "orange"), (7, "green")]),
                compact_stat(5, "Node Lag", "bdag_node_block_lag", pos(12, 0, 3, 3), thresholds=[(None, "green"), (1, "orange"), (5, "red")]),
                compact_stat(6, "Share Age", "bdag_pool_last_valid_share_age_seconds", pos(15, 0, 3, 3), "s", 0, thresholds=[(None, "green"), (60, "orange"), (180, "red")]),
                compact_stat(7, "Submit OK", "bdag_pool_block_submit_success_recent", pos(18, 0, 3, 3), thresholds=[(None, "orange"), (1, "green")]),
                compact_stat(8, "Submit Errors", "bdag_pool_block_submit_error_recent", pos(21, 0, 3, 3), thresholds=[(None, "green"), (1, "orange"), (5, "red")]),
                compact_stat(21, "Node Blocks", "bdag_node_latest_block", pos(0, 3, 8, 4), "locale", 0),
                compact_stat(22, "Node Sync %", "bdag_node_sync_progress_percent", pos(8, 3, 8, 4), "percent", 2),
                compact_stat(23, "Node Sync Gap", "bdag_node_sync_remaining_blocks", pos(16, 3, 8, 4), "locale", 0, thresholds=[(None, "green"), (1, "orange"), (5, "red")]),
                bargauge_at(9, "Income By Miner - USD/h", "sort_desc(bdag_miner_estimated_usd_per_hour)", "{{miner}}", pos(0, 7, 8, 9), "currencyUSD", 2),
                bargauge_at(10, "Accepted Work Share", "sort_desc(bdag_miner_work_percent)", "{{miner}}", pos(8, 7, 8, 9), "percent", 2),
                bargauge_at(
                    11,
                    "Miner Freshness - Last Share Age",
                    "sort_desc(bdag_miner_last_share_age_seconds)",
                    "{{miner}}",
                    pos(16, 7, 8, 9),
                    "s",
                    0,
                    thresholds=[(None, "green"), (60, "orange"), (180, "red")],
                ),
                timeseries_at(
                    12,
                    "Revenue Velocity",
                    [
                        ("bdag_wallet_recent_usd_per_hour", "wallet USD/h"),
                        ("bdag_wallet_recent_bdag_per_hour", "wallet BDAG/h"),
                    ],
                    pos(0, 16, 12, 8),
                    "short",
                    2,
                ),
                timeseries_at(
                    13,
                    "Per-Miner Income Trend",
                    [("bdag_miner_estimated_usd_per_hour", "{{miner}}")],
                    pos(12, 16, 12, 8),
                    "currencyUSD",
                    2,
                ),
                timeseries_at(
                    14,
                    "Node Height And Import Health",
                    [
                        ("bdag_node_latest_block", "{{node}} height"),
                        ("bdag_node_last_import_age_seconds", "{{node}} import age"),
                    ],
                    pos(0, 24, 12, 8),
                ),
                timeseries_at(
                    15,
                    "Pool Template Cadence",
                    [
                        ("bdag_pool_head_changes_recent", "head changes"),
                        ("bdag_pool_job_notify_recent", "job/diff pushes"),
                        ("bdag_pool_template_fetch_errors_recent", "template errors"),
                    ],
                    pos(12, 24, 12, 8),
                ),
                timeseries_at(
                    16,
                    "Block Submit Quality",
                    [
                        ("bdag_pool_block_submit_success_recent", "success"),
                        ("bdag_pool_block_submit_error_recent", "errors"),
                        ("bdag_pool_tip_overdue_recent", "tip overdue"),
                        ("bdag_pool_duplicate_blocks_recent", "duplicates"),
                    ],
                    pos(0, 32, 12, 8),
                ),
                timeseries_at(
                    17,
                    "Global Pool Position",
                    [
                        ("bdag_global_pool_work_percent", "{{pool}}"),
                    ],
                    pos(12, 32, 12, 8),
                    "percent",
                    2,
                ),
                table_at(
                    18,
                    "Miner Operator Table",
                    [
                        ("sort_desc(bdag_miner_estimated_usd_per_hour)", "USD/h"),
                        ("sort_desc(bdag_miner_work_percent)", "work %"),
                        ("sort_desc(bdag_miner_blocks_found_recent)", "blocks found"),
                        ("sort_desc(bdag_miner_shares_recent)", "shares"),
                    ],
                    pos(0, 40, 12, 8),
                ),
                table_at(
                    19,
                    "Node And Pool Risk Table",
                    [
                        ("bdag_node_last_import_age_seconds", "node import age"),
                        ("bdag_node_p2p_stream_errors_recent", "p2p stream errors"),
                        ("bdag_node_template_errors_recent", "template errors"),
                        ("bdag_pool_last_valid_share_age_seconds", "share age"),
                        ("bdag_pool_last_head_change_age_seconds", "head age"),
                    ],
                    pos(12, 40, 12, 8),
                ),
                logs_at(
                    20,
                    "Recent Mining Incidents",
                    '{job=~"docker|bdag-runtime"} |~ "(ERROR|WARN|failed|degraded|repair|Block submission too late|template fetch error|miner)"',
                    pos(0, 48, 24, 10),
                ),
            ],
        ),
    )
    write(
        "work-share.json",
        dashboard(
            "bdag-work-share",
            "BDAG Accepted Work Share",
            [
                stat_at(1, "Connected ASICs", "bdag_miner_connected_count", pos(0, 0, 4, 4), thresholds=[(None, "red"), (6, "orange"), (7, "green")]),
                stat_at(2, "Valid Share Age", "bdag_pool_last_valid_share_age_seconds", pos(4, 0, 4, 4), "s", 0, thresholds=[(None, "green"), (60, "orange"), (180, "red")]),
                stat_at(3, "Recent Shares", "bdag_pool_valid_shares_recent", pos(8, 0, 4, 4)),
                stat_at(4, "Recent Submits", "bdag_pool_submits_recent", pos(12, 0, 4, 4)),
                stat_at(5, "Submit Errors", "bdag_pool_block_submit_error_recent", pos(16, 0, 4, 4), thresholds=[(None, "green"), (1, "orange"), (5, "red")]),
                stat_at(6, "USD / Hour", "bdag_wallet_recent_usd_per_hour", pos(20, 0, 4, 4), "currencyUSD", 2),
                bargauge_at(7, "Accepted Work Share Now", "sort_desc(bdag_miner_work_percent)", "{{miner}}", pos(0, 4, 12, 11), "percent", 2),
                bargauge_at(8, "Income By Work Contributor", "sort_desc(bdag_miner_estimated_usd_per_hour)", "{{miner}}", pos(12, 4, 12, 11), "currencyUSD", 2),
                timeseries_at(
                    9,
                    "Accepted Work Share Over Time",
                    [("bdag_miner_work_percent", "{{miner}}")],
                    pos(0, 15, 24, 10),
                    "percent",
                    2,
                ),
                timeseries_at(
                    10,
                    "Shares, Submits, And Blocks Found",
                    [
                        ("bdag_miner_shares_recent", "{{miner}} shares"),
                        ("bdag_miner_submits_recent", "{{miner}} submits"),
                        ("bdag_miner_blocks_found_recent", "{{miner}} blocks"),
                    ],
                    pos(0, 25, 12, 9),
                ),
                timeseries_at(
                    11,
                    "Pool Submit Quality",
                    [
                        ("bdag_pool_block_submit_success_recent", "submit success"),
                        ("bdag_pool_block_submit_error_recent", "submit errors"),
                        ("bdag_pool_stale_submits_recent", "stale submits"),
                        ("bdag_pool_tip_overdue_recent", "tip overdue"),
                    ],
                    pos(12, 25, 12, 9),
                ),
                table_at(
                    12,
                    "Work Share Detail",
                    [
                        ("sort_desc(bdag_miner_work_percent)", "work %"),
                        ("sort_desc(bdag_miner_estimated_usd_per_hour)", "USD/h"),
                        ("sort_desc(bdag_miner_shares_recent)", "shares"),
                        ("sort_desc(bdag_miner_blocks_found_recent)", "blocks found"),
                    ],
                    pos(0, 34, 24, 9),
                ),
            ],
        ),
    )
    write(
        "miner-freshness.json",
        dashboard(
            "bdag-miner-freshness",
            "BDAG Miner Freshness",
            [
                stat_at(1, "Connected ASICs", "bdag_miner_connected_count", pos(0, 0, 4, 4), thresholds=[(None, "red"), (6, "orange"), (7, "green")]),
                stat_at(2, "Pool Share Age", "bdag_pool_last_valid_share_age_seconds", pos(4, 0, 4, 4), "s", 0, thresholds=[(None, "green"), (60, "orange"), (180, "red")]),
                stat_at(3, "Job Notify Age", "bdag_pool_last_job_notify_age_seconds", pos(8, 0, 4, 4), "s", 0, thresholds=[(None, "green"), (60, "orange"), (180, "red")]),
                stat_at(4, "Head Change Age", "bdag_pool_last_head_change_age_seconds", pos(12, 0, 4, 4), "s", 0, thresholds=[(None, "green"), (60, "orange"), (180, "red")]),
                stat_at(5, "Share Stall", "bdag_pool_share_stall", pos(16, 0, 4, 4), thresholds=[(None, "green"), (1, "red")]),
                stat_at(6, "Job Stall", "bdag_pool_job_stall", pos(20, 0, 4, 4), thresholds=[(None, "green"), (1, "red")]),
                bargauge_at(
                    7,
                    "Miner Freshness - Last Share Age",
                    "sort_desc(bdag_miner_last_share_age_seconds)",
                    "{{miner}}",
                    pos(0, 4, 12, 11),
                    "s",
                    0,
                    thresholds=[(None, "green"), (60, "orange"), (180, "red")],
                ),
                bargauge_at(
                    8,
                    "Miner Submit Freshness",
                    "sort_desc(bdag_miner_last_submit_age_seconds)",
                    "{{miner}}",
                    pos(12, 4, 12, 11),
                    "s",
                    0,
                    thresholds=[(None, "green"), (60, "orange"), (180, "red")],
                ),
                timeseries_at(
                    9,
                    "Last Share Age Over Time",
                    [("bdag_miner_last_share_age_seconds", "{{miner}}")],
                    pos(0, 15, 24, 10),
                    "s",
                    0,
                ),
                timeseries_at(
                    10,
                    "Pool Freshness",
                    [
                        ("bdag_pool_last_valid_share_age_seconds", "valid share age"),
                        ("bdag_pool_last_job_notify_age_seconds", "job notify age"),
                        ("bdag_pool_last_head_change_age_seconds", "head change age"),
                        ("bdag_pool_last_block_submit_age_seconds", "block submit age"),
                    ],
                    pos(0, 25, 12, 9),
                    "s",
                    0,
                ),
                timeseries_at(
                    11,
                    "Dual Node Import Freshness",
                    [
                        ("bdag_node_last_import_age_seconds", "{{node}} import age"),
                        ("bdag_node_p2p_stream_errors_recent", "{{node}} p2p resets"),
                        ("bdag_node_template_errors_recent", "{{node}} template errors"),
                    ],
                    pos(12, 25, 12, 9),
                ),
                table_at(
                    12,
                    "Freshness Detail",
                    [
                        ("sort_desc(bdag_miner_last_share_age_seconds)", "last share age"),
                        ("sort_desc(bdag_miner_last_submit_age_seconds)", "last submit age"),
                        ("sort_desc(bdag_miner_connected)", "connected"),
                        ("sort_desc(bdag_miner_up)", "ok"),
                    ],
                    pos(0, 34, 24, 9),
                ),
                logs_at(
                    13,
                    "Freshness And Repair Events",
                    '{job=~"docker|bdag-runtime"} |~ "(share_stall|job_stall|miner_down|degraded|repair|disconnect|read error|connection reset)"',
                    pos(0, 43, 24, 10),
                ),
            ],
        ),
    )
    write(
        "observed-peers.json",
        dashboard(
            "bdag-observed-peers",
            "BDAG Observed Peer IPs",
            [
                stat_at(1, "Observed Peer IPs", "bdag_peer_ip_count", pos(0, 0, 4, 4), thresholds=[(None, "orange"), (1, "green")]),
                stat_at(2, "Geolocated Peers", "bdag_peer_geo_ip_count", pos(4, 0, 4, 4), thresholds=[(None, "orange"), (1, "green")]),
                stat_at(3, "Node Lag", "bdag_node_block_lag", pos(8, 0, 4, 4), thresholds=[(None, "green"), (1, "orange"), (5, "red")]),
                stat_at(4, "Recent Importers", "bdag_node_recent_importers", pos(12, 0, 4, 4), thresholds=[(None, "red"), (1, "orange"), (2, "green")]),
                stat_at(5, "P2P Resets", "sum(bdag_node_p2p_stream_errors_recent)", pos(16, 0, 4, 4), thresholds=[(None, "green"), (10, "orange"), (30, "red")]),
                stat_at(6, "Template Errors", "sum(bdag_node_template_errors_recent)", pos(20, 0, 4, 4), thresholds=[(None, "green"), (1, "orange"), (10, "red")]),
                table_at(
                    7,
                    "Best Guess Location",
                    [
                        ("bdag_peer_location_best_guess", "best guess"),
                    ],
                    pos(0, 4, 24, 5),
                ),
                bargauge_at(
                    8,
                    "Peer Countries",
                    "sort_desc(bdag_peer_location_share_percent{level=\"country\"})",
                    "{{label}}",
                    pos(0, 9, 12, 10),
                    "percent",
                    1,
                ),
                bargauge_at(
                    9,
                    "Peer ASNs / Providers",
                    "sort_desc(bdag_peer_location_share_percent{level=\"asn\"})",
                    "{{label}}",
                    pos(12, 9, 12, 10),
                    "percent",
                    1,
                ),
                bargauge_at(
                    10,
                    "Peer Cities",
                    "sort_desc(bdag_peer_location_share_percent{level=\"city\"})",
                    "{{label}}",
                    pos(0, 19, 12, 10),
                    "percent",
                    1,
                ),
                timeseries_at(
                    11,
                    "Peer Count And Node Health",
                    [
                        ("bdag_peer_ip_count", "peer IPs"),
                        ("bdag_peer_geo_ip_count", "geolocated peers"),
                        ("bdag_node_recent_importers", "recent importing nodes"),
                        ("bdag_node_block_lag", "node block lag"),
                    ],
                    pos(12, 19, 12, 10),
                ),
                table_at(
                    12,
                    "Observed Peer IP Detail",
                    [
                        ("sort_desc(bdag_peer_seen_count)", "seen count"),
                    ],
                    pos(0, 29, 24, 12),
                ),
                logs_at(
                    13,
                    "P2P And Peer-Related Logs",
                    '{job="docker",container=~"bdag-miner-node-[0-9]+"} |~ "(peer|P2P|stream|sync|import)"',
                    pos(0, 41, 24, 10),
                ),
            ],
        ),
    )
    write(
        "overview.json",
        dashboard(
            "bdag-overview",
            "BDAG Overview",
            [
                stat(1, "Stack Status", "bdag_stack_status", 0),
                stat(2, "Connected Miners", "bdag_miner_connected_count", 1),
                stat(3, "Valid Share Age", "bdag_pool_last_valid_share_age_seconds", 2, "s"),
                stat(4, "Sync Progress", "bdag_sync_progress_percent", 3, "percent", 2),
                timeseries(5, "Pool Activity", targets([("bdag_pool_valid_shares_recent", "valid shares"), ("bdag_pool_submits_recent", "submits"), ("bdag_pool_stale_submits_recent", "stale")]), 3),
                timeseries(6, "Miner Work Share", targets([("bdag_miner_work_percent", "{{miner}}")]), 4, "percent"),
                timeseries(7, "Node Heights", targets([("bdag_node_latest_block", "{{node}}")]), 5),
            ],
        ),
    )
    write(
        "miners.json",
        dashboard(
            "bdag-miners",
            "BDAG Miners",
            [
                stat(1, "Managed", "bdag_miner_managed_count", 0),
                stat(2, "OK", "bdag_miner_ok_count", 1),
                stat(3, "Connected", "bdag_miner_connected_count", 2),
                timeseries(4, "Work % By Miner", targets([("bdag_miner_work_percent", "{{miner}}")]), 3, "percent"),
                timeseries(5, "Last Share Age", targets([("bdag_miner_last_share_age_seconds", "{{miner}}")]), 4, "s"),
                timeseries(6, "ASIC Hashrate", targets([("bdag_miner_hashrate_ghs", "{{miner}}")]), 5, "none"),
                timeseries(7, "Hardware Error Ratio", targets([("bdag_miner_hw_error_ratio", "{{miner}}")]), 6, "percentunit"),
            ],
        ),
    )
    write(
        "pool.json",
        dashboard(
            "bdag-pool",
            "BDAG Pool",
            [
                stat(1, "Share Stall", "bdag_pool_share_stall", 0),
                stat(2, "Job Stall", "bdag_pool_job_stall", 1),
                stat(3, "Template Frozen", "bdag_pool_template_frozen", 2),
                timeseries(4, "Share And Submit Recent Window", targets([("bdag_pool_valid_shares_recent", "valid"), ("bdag_pool_submits_recent", "submits"), ("bdag_pool_stale_submits_recent", "stale")]), 3),
                timeseries(5, "Pool Ages", targets([("bdag_pool_last_valid_share_age_seconds", "valid share"), ("bdag_pool_last_job_notify_age_seconds", "job notify"), ("bdag_pool_last_block_submit_age_seconds", "block submit")]), 4, "s"),
                timeseries(6, "Block Submit Results", targets([("bdag_pool_block_submit_success_recent", "success"), ("bdag_pool_block_submit_error_recent", "error")]), 5),
            ],
        ),
    )
    write(
        "nodes.json",
        dashboard(
            "bdag-nodes",
            "BDAG Nodes",
            [
                stat(1, "Block Lag", "bdag_node_block_lag", 0),
                stat(2, "Recent Importers", "bdag_node_recent_importers", 1),
                stat(3, "Remaining Blocks", "bdag_sync_remaining_blocks", 2),
                timeseries(4, "Latest Block", targets([("bdag_node_latest_block", "{{node}}")]), 3),
                timeseries(5, "Import Age", targets([("bdag_node_last_import_age_seconds", "{{node}}")]), 4, "s"),
                timeseries(6, "Native Node Height", targets([("Blockdag_mainheight{job=\"bdag-native-node\"}", "{{node}}")]), 5),
                timeseries(7, "Native Tips And Unsequenced", targets([("Blockdag_tips_total{job=\"bdag-native-node\"}", "tips {{node}}"), ("Blockdag_unsequenced{job=\"bdag-native-node\"}", "unsequenced {{node}}")]), 6),
                timeseries(8, "Template Errors", targets([("bdag_node_template_errors_recent", "{{node}}")]), 7),
                timeseries(9, "P2P Stream Errors", targets([("bdag_node_p2p_stream_errors_recent", "{{node}}")]), 8),
            ],
        ),
    )
    write(
        "template-fan-in.json",
        dashboard(
            "bdag-template-fan-in",
            "BDAG Template Fan-In",
            [
                compact_stat(1, "Fan-In Enabled", "max(pool_template_fanin_enabled)", pos(0, 0, 4, 3), thresholds=[(None, "gray"), (0.5, "green")]),
                compact_stat(2, "Fan-In Backends", "max(pool_template_fanin_backends)", pos(4, 0, 4, 3)),
                compact_stat(3, "Healthy Backend Ratio", "sum(pool_rpc_backend_healthy) / clamp_min(count(pool_rpc_backend_healthy), 1)", pos(8, 0, 4, 3), "percentunit", 2, thresholds=[(None, "red"), (0.5, "orange"), (1, "green")]),
                compact_stat(4, "WS Stream Ratio", "sum(pool_rpc_backend_ws_connected) / clamp_min(count(pool_rpc_backend_ws_connected), 1)", pos(12, 0, 4, 3), "percentunit", 2, thresholds=[(None, "red"), (0.5, "orange"), (1, "green")]),
                compact_stat(5, "Template Age p95", "quantile(0.95, pool_rpc_backend_template_age_seconds)", pos(16, 0, 4, 3), "s", 2, thresholds=[(None, "green"), (2, "orange"), (5, "red")]),
                compact_stat(6, "Local Rejects / min", 'sum(rate(pool_block_submit_outcomes_total{outcome="rejected-local",reason=~"stale-parent|old-template-sequence|duplicate-block"}[5m])) * 60', pos(20, 0, 4, 3), "ops", 2, thresholds=[(None, "green"), (5, "orange"), (20, "red")]),
                timeseries_at(
                    7,
                    "Backend Template Age",
                    [("pool_rpc_backend_template_age_seconds", "{{backend}}")],
                    pos(0, 3, 12, 8),
                    "s",
                    2,
                ),
                timeseries_at(
                    8,
                    "Backend WS And Health",
                    [
                        ("pool_rpc_backend_ws_connected", "ws {{backend}}"),
                        ("pool_rpc_backend_healthy", "healthy {{backend}}"),
                        ("pool_rpc_backend_selected", "selected {{backend}}"),
                    ],
                    pos(12, 3, 12, 8),
                ),
                timeseries_at(
                    9,
                    "Template Sequence Changes",
                    [("changes(pool_rpc_backend_template_sequence[$__rate_interval])", "{{backend}} seq changes")],
                    pos(0, 11, 12, 8),
                ),
                timeseries_at(
                    10,
                    "Fan-In Accepted / Rejected",
                    [('sum by (backend,result,reason) (rate(pool_template_fanin_updates_total[$__rate_interval]))', "{{backend}} {{result}} {{reason}}")],
                    pos(12, 11, 12, 8),
                ),
                timeseries_at(
                    11,
                    "Submit Outcomes",
                    [('sum by (outcome,reason) (rate(pool_block_submit_outcomes_total[$__rate_interval]))', "{{outcome}} {{reason}}")],
                    pos(0, 19, 12, 8),
                ),
                timeseries_at(
                    12,
                    "Backend Submit Latency p95",
                    [('histogram_quantile(0.95, sum by (backend,le) (rate(pool_rpc_backend_submit_duration_seconds_bucket[$__rate_interval])))', "{{backend}}")],
                    pos(12, 19, 12, 8),
                    "s",
                    3,
                ),
                table_at(
                    13,
                    "Backend State",
                    [
                        ("pool_rpc_backend_score", "{{backend}} score"),
                        ("pool_rpc_backend_template_age_seconds", "{{backend}} age"),
                        ("pool_rpc_backend_template_sequence", "{{backend}} seq"),
                        ("pool_template_fanin_backend_participant", "{{backend}} participant"),
                        ("pool_template_fanin_backend_role", "{{backend}} {{role}} role"),
                        ("pool_template_fanin_observed_height", "{{backend}} observed height"),
                        ("pool_template_fanin_best_height", "{{backend}} accepted height"),
                        ("pool_template_fanin_winner", "{{backend}} winner"),
                    ],
                    pos(0, 27, 24, 8),
                ),
                timeseries_at(
                    15,
                    "Observed vs Accepted Template Height",
                    [
                        ("pool_template_fanin_observed_height", "{{backend}} observed"),
                        ("pool_template_fanin_best_height", "{{backend}} accepted"),
                        ("pool_template_fanin_winner", "{{backend}} winner"),
                    ],
                    pos(0, 35, 12, 8),
                ),
                timeseries_at(
                    16,
                    "Stale Race vs Expired Job Recovery",
                    [
                        ('sum by (action,reason) (rate(pool_stale_job_race_recoveries_total[$__rate_interval]))', "stale {{action}} {{reason}}"),
                        ('sum by (action) (rate(pool_expired_job_recoveries_total[$__rate_interval]))', "expired {{action}}"),
                    ],
                    pos(12, 35, 12, 8),
                ),
                logs_at(
                    14,
                    "Fan-In And Router Logs",
                    '{job="docker",container="asic-pool"} |~ "(FANIN|ROUTER|SUBMIT-STALL|WS)"',
                    pos(0, 43, 24, 10),
                ),
            ],
        ),
    )
    write(
        "earnings.json",
        dashboard(
            "bdag-earnings",
            "BDAG Earnings",
            [
                stat(1, "Wallet BDAG", "bdag_wallet_balance_bdag", 0),
                stat(2, "Recent BDAG/h", "bdag_wallet_recent_bdag_per_hour", 1),
                stat(3, "BDAG Price USD", "bdag_price_usd", 2, "currencyUSD", 6),
                timeseries(4, "Miner USD/h", targets([("bdag_miner_estimated_usd_per_hour", "{{miner}}")]), 3, "currencyUSD"),
                timeseries(5, "Miner BDAG/h", targets([("bdag_miner_estimated_bdag_per_hour", "{{miner}}")]), 4),
                timeseries(6, "Global Pool Share", targets([("bdag_global_pool_work_percent", "{{pool}}")]), 5, "percent"),
            ],
        ),
    )
    write(
        "system.json",
        dashboard(
            "bdag-system",
            "BDAG Host And Containers",
            [
                stat(1, "CPU Load 1m", "node_load1", 0),
                stat(2, "Disk Free %", "100 * node_filesystem_avail_bytes{mountpoint=\"/\",fstype!~\"tmpfs|overlay\"} / node_filesystem_size_bytes{mountpoint=\"/\",fstype!~\"tmpfs|overlay\"}", 1, "percent"),
                stat(3, "Postgres Exporter Up", "up{job=\"postgres-exporter\"}", 2),
                timeseries(4, "CPU Usage", targets([("100 - (avg by (instance) (rate(node_cpu_seconds_total{mode=\"idle\"}[$__rate_interval])) * 100)", "cpu used")]), 3, "percent"),
                timeseries(5, "Memory Available", targets([("node_memory_MemAvailable_bytes", "available")]), 4, "bytes"),
                timeseries(6, "Container CPU", targets([("rate(container_cpu_usage_seconds_total{name=~\"asic-pool|bdag-miner-node-[0-9]+|pool-db|rpc-failover\"}[$__rate_interval])", "{{name}}")]), 5),
            ],
        ),
    )
    write(
        "thermals.json",
        dashboard(
            "bdag-thermals",
            "BDAG Thermals",
            [
                stat(1, "Max Thermal Zone", "max(node_thermal_zone_temp)", 0, "celsius"),
                timeseries(2, "Thermal Zones", targets([("node_thermal_zone_temp", "{{zone}}")]), 1, "celsius", 10),
                timeseries(3, "CPU Frequency", targets([("node_cpu_scaling_frequency_hertz", "{{cpu}}")]), 2, "hertz", 10),
            ],
        ),
    )
    write(
        "incidents.json",
        dashboard(
            "bdag-incidents",
            "BDAG Logs And Incidents",
            [
                logs(1, "Pool And Node Errors", '{job="docker"} |= "ERROR"', 0),
                logs(2, "Watchdog And Repair Events", '{job="bdag-runtime"} |~ "(repair|miner_down|share_stall|failed|critical)"', 1),
                timeseries(3, "Dashboard API Scrape Time", targets([("bdag_dashboard_api_scrape_seconds", "{{api}}")]), 2, "s"),
            ],
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
