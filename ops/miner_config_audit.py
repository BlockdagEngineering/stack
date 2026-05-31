#!/usr/bin/env python3
"""Read-only audit of ASIC identity, pool configuration, and stale dashboard entries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pool_ops import (
    POOL_CONNECTED_STALE_SECONDS,
    RUNTIME_DIR,
    collect_miner_health,
    default_miner_pool_settings,
    is_lan_ipv4,
    now_iso,
    read_miner_registry,
)


def worker_short(value: str | None) -> str:
    text = str(value or "")
    if len(text) <= 14:
        return text
    return f"{text[:6]}...{text[-4:]}"


def recent_activity_age(value: Any) -> bool:
    try:
        return int(value) <= POOL_CONNECTED_STALE_SECONDS
    except (TypeError, ValueError):
        return False


def audit_miners() -> dict[str, Any]:
    registry = read_miner_registry()
    health = collect_miner_health()
    defaults = default_miner_pool_settings()
    miners = health.get("miners") if isinstance(health.get("miners"), list) else []
    by_mac: dict[str, list[dict[str, Any]]] = {}
    stale: list[dict[str, Any]] = []
    wrong_config: list[dict[str, Any]] = []
    down: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    for miner in miners:
        ip = str(miner.get("ip") or "")
        if not is_lan_ipv4(ip):
            continue
        mac = str(miner.get("mac") or "")
        if not mac and not ip.startswith("192.168."):
            continue
        if mac:
            by_mac.setdefault(mac, []).append(miner)
        if miner.get("status") == "inactive":
            stale.append(miner)
        if miner.get("status") == "down":
            down.append(miner)
        expected_worker = str(miner.get("expected_worker_user") or "").lower()
        seen_workers = [str(worker).lower() for worker in miner.get("workers") or []]
        pool_log_activity_is_recent = bool(
            miner.get("connected")
            or miner.get("pool_active")
            or recent_activity_age(miner.get("last_share_age_seconds"))
            or recent_activity_age(miner.get("last_submit_age_seconds"))
        )
        pool_log_matches_expected_worker = bool(
            expected_worker and expected_worker in seen_workers and pool_log_activity_is_recent
        )
        pool_activity_verified = bool(
            miner.get("connected")
            and miner.get("status") in {"ok", "connected", "degraded"}
            and (int(miner.get("shares") or 0) > 0 or int(miner.get("submits") or 0) > 0)
        )
        config_verified = bool(miner.get("configured") or pool_log_matches_expected_worker or pool_activity_verified)
        if not config_verified and miner.get("status") != "inactive":
            wrong_config.append(miner)
        rows.append(
            {
                "name": miner.get("display_name") or miner.get("ip"),
                "ip": ip,
                "mac": mac,
                "status": miner.get("status"),
                "connected": miner.get("connected") or miner.get("pool_active"),
                "configured": config_verified,
                "config_verified_by_api": bool(miner.get("configured")),
                "config_verified_by_pool_log": pool_log_matches_expected_worker,
                "config_verified_by_pool_activity": pool_activity_verified,
                "expected_pool_url": miner.get("expected_pool_url"),
                "expected_worker_user": miner.get("expected_worker_user"),
                "worker_short": worker_short(miner.get("expected_worker_user")),
                "last_share_age_seconds": miner.get("last_share_age_seconds"),
                "last_submit_age_seconds": miner.get("last_submit_age_seconds"),
                "shares": miner.get("shares"),
                "submits": miner.get("submits"),
                "work_percent": miner.get("work_percent"),
            }
        )

    duplicates = {
        mac: [
            {
                "name": item.get("display_name") or item.get("ip"),
                "ip": item.get("ip"),
                "status": item.get("status"),
                "last_share_age_seconds": item.get("last_share_age_seconds"),
            }
            for item in values
        ]
        for mac, values in sorted(by_mac.items())
        if len(values) > 1
    }
    expected_count = len([item for item in rows if item.get("expected_worker_user")])
    ok_count = len([item for item in rows if item.get("status") == "ok"])
    connected_count = len([item for item in rows if item.get("connected")])
    return {
        "generated_at": now_iso(),
        "expected_pool_url": defaults["pool_url"],
        "expected_worker_user": defaults["worker_user"],
        "registry_updated_at": registry.get("updated_at"),
        "tracked_count": len(rows),
        "expected_worker_count": expected_count,
        "ok_count": ok_count,
        "connected_count": connected_count,
        "wrong_config_count": len(wrong_config),
        "down_count": len(down),
        "stale_inactive_count": len(stale),
        "duplicate_mac_count": len(duplicates),
        "duplicates_by_mac": duplicates,
        "wrong_config": [
            {
                "name": item.get("display_name") or item.get("ip"),
                "ip": item.get("ip"),
                "mac": item.get("mac"),
                "status": item.get("status"),
                "expected_pool_url": item.get("expected_pool_url"),
                "expected_worker_user": item.get("expected_worker_user"),
            }
            for item in wrong_config
        ],
        "down": [
            {
                "name": item.get("display_name") or item.get("ip"),
                "ip": item.get("ip"),
                "mac": item.get("mac"),
                "last_share_age_seconds": item.get("last_share_age_seconds"),
                "last_submit_age_seconds": item.get("last_submit_age_seconds"),
            }
            for item in down
        ],
        "miners": rows,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Miner Configuration Audit",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        f"- Expected pool: `{payload['expected_pool_url']}`",
        f"- Connected miners: `{payload['connected_count']}`",
        f"- OK miners: `{payload['ok_count']}`",
        f"- Wrong config: `{payload['wrong_config_count']}`",
        f"- Down: `{payload['down_count']}`",
        f"- Duplicate MAC groups: `{payload['duplicate_mac_count']}`",
        "",
        "| Miner | IP | MAC | Status | Configured | Worker | Share Age | Work % |",
        "| --- | --- | --- | --- | --- | --- | ---: | ---: |",
    ]
    for row in payload["miners"]:
        lines.append(
            "| "
            f"{row.get('name') or ''} | "
            f"{row.get('ip') or ''} | "
            f"{row.get('mac') or ''} | "
            f"{row.get('status') or ''} | "
            f"{row.get('configured')} | "
            f"{row.get('worker_short') or ''} | "
            f"{row.get('last_share_age_seconds') if row.get('last_share_age_seconds') is not None else ''} | "
            f"{row.get('work_percent') if row.get('work_percent') is not None else ''} |"
        )
    if payload["wrong_config"]:
        lines.extend(["", "## Wrong Configuration", ""])
        for item in payload["wrong_config"]:
            lines.append(f"- {item.get('name')} {item.get('ip')} expected `{item.get('expected_pool_url')}`")
    if payload["duplicates_by_mac"]:
        lines.extend(["", "## Duplicate MAC Groups", ""])
        for mac, items in payload["duplicates_by_mac"].items():
            labels = ", ".join(f"{item.get('name')} {item.get('ip')} {item.get('status')}" for item in items)
            lines.append(f"- `{mac}`: {labels}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--write-report", action="store_true", help="write markdown report under ops/runtime")
    args = parser.parse_args()

    payload = audit_miners()
    if args.write_report:
        path = RUNTIME_DIR / f"miner-config-audit-{payload['generated_at'].replace(':', '').replace('+', '-')}.md"
        path.write_text(render_markdown(payload), encoding="utf-8")
        print(path)
    elif args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(render_markdown(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
