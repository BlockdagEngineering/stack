#!/usr/bin/env python3
"""Application-aware RPC/template routing helper for the BlockDAG pool."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from incident_journal import append_incident
from pool_ops import NODES, PROJECT_ROOT, RUNTIME_DIR, collect_status_cached, ensure_runtime, now_iso


RPC_ROUTER_STATE_FILE = Path(os.environ.get("BDAG_RPC_ROUTER_STATE_FILE", RUNTIME_DIR / "rpc-router-state.json"))
SNAPSHOT_STOP_STATE_FILE = Path(
    os.environ.get("BDAG_SNAPSHOT_STOP_STATE_FILE", RUNTIME_DIR / "snapshot-node-stop-state.json")
)
HAPROXY_CFG = PROJECT_ROOT / "haproxy.cfg"
NODE_TO_HAPROXY_SERVER = {
    "bdag-miner-node-1": "node1",
    "bdag-miner-node-2": "node2",
}
HAPROXY_SERVER_TO_NODE = {server: node for node, server in NODE_TO_HAPROXY_SERVER.items()}

MIN_SWITCH_SCORE = float(os.environ.get("BDAG_RPC_ROUTER_MIN_SWITCH_SCORE", "45"))
MIN_SCORE_DELTA = float(os.environ.get("BDAG_RPC_ROUTER_MIN_SCORE_DELTA", "15"))
IMPORT_STALE_SECONDS = int(os.environ.get("BDAG_RPC_ROUTER_IMPORT_STALE_SECONDS", "90"))
HARD_IMPORT_STALE_SECONDS = int(os.environ.get("BDAG_RPC_ROUTER_HARD_IMPORT_STALE_SECONDS", "180"))
SNAPSHOT_RECOVERY_SECONDS = int(os.environ.get("BDAG_RPC_ROUTER_SNAPSHOT_RECOVERY_SECONDS", "180"))
P2P_SWITCH_ERROR_COUNT = int(os.environ.get("BDAG_RPC_ROUTER_P2P_SWITCH_ERROR_COUNT", "10"))
P2P_MIN_SCORE_DELTA = float(os.environ.get("BDAG_RPC_ROUTER_P2P_MIN_SCORE_DELTA", "8"))
POOL_PRESSURE_MIN_SCORE_DELTA = float(os.environ.get("BDAG_RPC_ROUTER_POOL_PRESSURE_MIN_SCORE_DELTA", "5"))
POOL_QUALITY_MIN_BLOCK_SAMPLES = int(os.environ.get("BDAG_RPC_ROUTER_POOL_QUALITY_MIN_BLOCK_SAMPLES", "8"))
POOL_QUALITY_MIN_SHARE_SAMPLES = int(os.environ.get("BDAG_RPC_ROUTER_POOL_QUALITY_MIN_SHARE_SAMPLES", "80"))
POOL_BLOCK_ERROR_RATIO_WARN = float(os.environ.get("BDAG_RPC_ROUTER_BLOCK_ERROR_RATIO_WARN", "0.12"))
POOL_STALE_JOB_RATIO_WARN = float(os.environ.get("BDAG_RPC_ROUTER_STALE_JOB_RATIO_WARN", "0.05"))
POOL_TIP_OVERDUE_RATIO_WARN = float(os.environ.get("BDAG_RPC_ROUTER_TIP_OVERDUE_RATIO_WARN", "0.06"))
POOL_VALID_SHARE_RATIO_WARN = float(os.environ.get("BDAG_RPC_ROUTER_VALID_SHARE_RATIO_WARN", "0.55"))
POOL_ZERO_SUCCESS_FAILURE_WARN = int(os.environ.get("BDAG_RPC_ROUTER_ZERO_SUCCESS_FAILURE_WARN", "25"))


def current_rpc_primary() -> str | None:
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
            return match.group(2)
    return None


def pool_selected_backend(status: dict[str, Any]) -> tuple[str, str]:
    for key in ("pool_metrics", "pool_health", "pool"):
        source = status.get(key) if isinstance(status.get(key), dict) else {}
        selected = str(source.get("selected_backend") or "")
        if not selected:
            continue
        node = HAPROXY_SERVER_TO_NODE.get(selected, selected if selected in NODES else "")
        if node:
            return selected, node
    return "", ""


def recently_stopped_snapshot_nodes(now: int | None = None) -> dict[str, int]:
    now = now or int(time.time())
    try:
        payload = json.loads(SNAPSHOT_STOP_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    node = str(payload.get("node") or "")
    if node not in NODES:
        return {}
    try:
        written_epoch = int(payload.get("written_epoch") or 0)
    except (TypeError, ValueError):
        return {}
    if written_epoch <= 0:
        return {}
    try:
        recovery_seconds = int(payload.get("recovery_seconds") or SNAPSHOT_RECOVERY_SECONDS)
    except (TypeError, ValueError):
        recovery_seconds = SNAPSHOT_RECOVERY_SECONDS
    remaining = recovery_seconds - (now - written_epoch)
    if remaining <= 0:
        return {}
    return {node: remaining}


def node_scores(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    nodes = status.get("nodes") if isinstance(status.get("nodes"), dict) else {}
    snapshot_recovery = recently_stopped_snapshot_nodes()
    latest_values = [
        int(info.get("latest_block") or 0)
        for info in nodes.values()
        if isinstance(info, dict) and int(info.get("latest_block") or 0) > 0
    ]
    max_latest = max(latest_values) if latest_values else 0
    scores: dict[str, dict[str, Any]] = {}
    for node in NODES:
        info = nodes.get(node, {}) if isinstance(nodes.get(node), dict) else {}
        score = 100.0
        reasons: list[str] = []
        hard_fail = False

        if not info.get("child_running"):
            score -= 100
            hard_fail = True
            reasons.append("child-not-running")
        if info.get("critical"):
            score -= 90
            hard_fail = True
            reasons.append("critical-log")

        latest = int(info.get("latest_block") or 0)
        if latest <= 0:
            score -= 30
            reasons.append("no-latest-block")
        elif max_latest and max_latest - latest > 0:
            lag = max_latest - latest
            score -= min(60, lag * 4)
            reasons.append(f"height-lag-{lag}")

        peer_ahead = int(info.get("peer_ahead_blocks") or 0)
        if peer_ahead > 0:
            score -= min(50, peer_ahead * 3)
            reasons.append(f"peer-ahead-{peer_ahead}")

        import_age = info.get("last_import_age_seconds")
        if import_age is None:
            score -= 10
            reasons.append("import-age-unknown")
        else:
            import_age = int(import_age)
            if import_age > HARD_IMPORT_STALE_SECONDS:
                score -= 60
                reasons.append(f"import-hard-stale-{import_age}s")
            elif import_age > IMPORT_STALE_SECONDS:
                score -= 25
                reasons.append(f"import-stale-{import_age}s")

        template_errors = int(
            info.get("mining_template_hard_error_count")
            if info.get("mining_template_hard_error_count") is not None
            else info.get("mining_template_error_count") or 0
        )
        transient_template_errors = int(info.get("mining_template_transient_tx_error_count") or 0)
        if template_errors:
            score -= min(80, template_errors * 8)
            reasons.append(f"template-errors-{template_errors}")
        if info.get("mining_template_failing"):
            score -= 45
            reasons.append("template-failing")
        probe_samples = int(info.get("template_probe_sample_count") or 0)
        probe_errors = int(info.get("template_probe_error_count") or 0)
        probe_benign_tx_throttle = bool(info.get("template_probe_benign_tx_throttle"))
        probe_benign_tx_template = bool(
            info.get("template_probe_benign_tx_template_error")
            or probe_benign_tx_throttle
        )
        if probe_samples and probe_errors:
            if not probe_benign_tx_template:
                probe_ratio = probe_errors / max(1, probe_samples)
                score -= min(80, probe_ratio * 80)
                reasons.append(f"template-probe-errors-{probe_errors}-{probe_samples}")
        if info.get("template_probe_failing"):
            score -= 45
            reasons.append("template-probe-failing")

        if node in snapshot_recovery:
            score -= 60
            reasons.append(f"recent-snapshot-stop-{snapshot_recovery[node]}s")

        p2p_errors = int(info.get("p2p_stream_errors") or 0)
        if p2p_errors:
            score -= min(20, p2p_errors)
            reasons.append(f"p2p-errors-{p2p_errors}")
        invalid_peer_errors = int(info.get("invalid_peer_errors") or 0)

        if score < 0:
            score = 0.0
        state = "down" if hard_fail or score <= 0 else "degraded" if score < 75 or reasons else "ok"
        scores[node] = {
            "node": node,
            "score": round(score, 3),
            "state": state,
            "reasons": reasons,
            "latest_block": latest or None,
            "last_import_age_seconds": info.get("last_import_age_seconds"),
            "mining_template_error_count": template_errors,
            "mining_template_transient_tx_error_count": transient_template_errors,
            "mining_template_failing": bool(info.get("mining_template_failing")),
            "template_probe_sample_count": probe_samples,
            "template_probe_error_count": probe_errors,
            "template_probe_error_ratio": float(info.get("template_probe_error_ratio") or 0.0),
            "template_probe_benign_tx_throttle": probe_benign_tx_throttle,
            "template_probe_benign_tx_template_error": probe_benign_tx_template,
            "template_probe_failing": bool(info.get("template_probe_failing")),
            "template_probe_last_error": info.get("template_probe_last_error") or "",
            "p2p_stream_errors": p2p_errors,
            "invalid_peer_errors": invalid_peer_errors,
            "critical": bool(info.get("critical")),
            "child_running": bool(info.get("child_running")),
        }
    return scores


def _ratio(numerator: int | float, denominator: int | float) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator), 4)


def _pool_pressure(status: dict[str, Any]) -> dict[str, Any]:
    pool = status.get("pool_health") or status.get("pool") or {}
    submit_count = int(pool.get("submit_count") or 0)
    valid_share_count = int(pool.get("valid_share_count") or 0)
    block_submit_success_count = int(pool.get("block_submit_success_count") or 0)
    block_submit_error_count = int(pool.get("block_submit_error_count") or 0)
    stale_job_candidate_count = int(pool.get("stale_job_candidate_count") or 0)
    duplicate_block_count = int(pool.get("duplicate_block_count") or 0)
    stale_submit_count = int(pool.get("stale_submit_count") or 0)
    tip_overdue_count = int(pool.get("tip_overdue_count") or 0)
    accepted_job_expired_storm = bool(pool.get("accepted_job_expired_storm"))
    block_submit_failure_count = int(
        pool.get("block_submit_failure_count")
        if pool.get("block_submit_failure_count") is not None
        else block_submit_error_count + duplicate_block_count + stale_job_candidate_count
    )
    block_submit_zero_success_storm = bool(
        pool.get("block_submit_zero_success_storm")
        or (
            block_submit_success_count == 0
            and block_submit_failure_count >= POOL_ZERO_SUCCESS_FAILURE_WARN
        )
    )

    block_samples = block_submit_success_count + block_submit_error_count
    block_error_ratio = _ratio(block_submit_error_count, max(1, block_submit_success_count))
    stale_job_candidate_ratio = _ratio(stale_job_candidate_count, max(1, block_submit_success_count))
    tip_overdue_ratio = _ratio(tip_overdue_count, max(1, block_submit_success_count))
    duplicate_block_ratio = _ratio(duplicate_block_count, max(1, block_submit_success_count))
    valid_share_ratio = _ratio(valid_share_count, max(1, submit_count))

    quality_reasons: list[str] = []
    if block_samples >= POOL_QUALITY_MIN_BLOCK_SAMPLES and block_error_ratio >= POOL_BLOCK_ERROR_RATIO_WARN:
        quality_reasons.append(f"block-error-ratio-{block_error_ratio:.3f}")
    if block_samples >= POOL_QUALITY_MIN_BLOCK_SAMPLES and stale_job_candidate_ratio >= POOL_STALE_JOB_RATIO_WARN:
        quality_reasons.append(f"stale-job-ratio-{stale_job_candidate_ratio:.3f}")
    if block_samples >= POOL_QUALITY_MIN_BLOCK_SAMPLES and tip_overdue_ratio >= POOL_TIP_OVERDUE_RATIO_WARN:
        quality_reasons.append(f"tip-overdue-ratio-{tip_overdue_ratio:.3f}")
    if submit_count >= POOL_QUALITY_MIN_SHARE_SAMPLES and valid_share_ratio <= POOL_VALID_SHARE_RATIO_WARN:
        quality_reasons.append(f"valid-share-ratio-{valid_share_ratio:.3f}")
    if block_submit_zero_success_storm:
        quality_reasons.append(f"zero-success-submit-failures-{block_submit_failure_count}")
    if accepted_job_expired_storm:
        quality_reasons.append(f"accepted-job-expired-storm-{stale_submit_count}")

    hard_pressure = bool(
        pool.get("rpc_refused")
        or pool.get("share_stall")
        or pool.get("job_stall")
        or pool.get("rpc_template_failing")
        or pool.get("node_template_probe_failing")
        or pool.get("pool_template_frozen")
        or pool.get("duplicate_block_storm")
        or pool.get("stale_job_candidate_storm")
        or pool.get("block_submit_error_storm")
        or accepted_job_expired_storm
        or block_submit_zero_success_storm
    )
    return {
        "initial_download": bool(pool.get("initial_download")),
        "rpc_refused": bool(pool.get("rpc_refused")),
        "share_stall": bool(pool.get("share_stall")),
        "job_stall": bool(pool.get("job_stall")),
        "rpc_template_failing": bool(pool.get("rpc_template_failing")),
        "node_template_probe_failing": bool(pool.get("node_template_probe_failing")),
        "pool_template_frozen": bool(pool.get("pool_template_frozen")),
        "duplicate_block_storm": bool(pool.get("duplicate_block_storm")),
        "stale_job_candidate_storm": bool(pool.get("stale_job_candidate_storm")),
        "block_submit_error_storm": bool(pool.get("block_submit_error_storm")),
        "block_submit_success_count": block_submit_success_count,
        "block_submit_error_count": block_submit_error_count,
        "block_submit_failure_count": block_submit_failure_count,
        "block_submit_zero_success_storm": block_submit_zero_success_storm,
        "accepted_job_expired_storm": accepted_job_expired_storm,
        "stale_job_candidate_count": stale_job_candidate_count,
        "duplicate_block_count": duplicate_block_count,
        "stale_submit_count": stale_submit_count,
        "tip_overdue_count": tip_overdue_count,
        "submit_count": submit_count,
        "valid_share_count": valid_share_count,
        "block_samples": block_samples,
        "block_error_ratio": block_error_ratio,
        "stale_job_candidate_ratio": stale_job_candidate_ratio,
        "tip_overdue_ratio": tip_overdue_ratio,
        "duplicate_block_ratio": duplicate_block_ratio,
        "valid_share_ratio": valid_share_ratio,
        "hard_pool_pressure": hard_pressure,
        "pool_quality_pressure": bool(quality_reasons),
        "pool_quality_reasons": quality_reasons,
    }


def recommend_rpc_primary(
    status: dict[str, Any],
    current_primary: str | None = None,
    failing_nodes: list[str] | None = None,
    min_delta: float = MIN_SCORE_DELTA,
) -> dict[str, Any]:
    current_primary = current_primary or current_rpc_primary()
    failing_nodes = failing_nodes or []
    scores = node_scores(status)
    pressure = _pool_pressure(status)
    pool_selected_label, pool_selected_node = pool_selected_backend(status)

    ranked = sorted(scores.values(), key=lambda item: item["score"], reverse=True)
    recommended = ranked[0]["node"] if ranked else current_primary
    current_score = float(scores.get(current_primary or "", {}).get("score", 0))
    recommended_score = float(scores.get(recommended or "", {}).get("score", 0))
    current_reasons = scores.get(current_primary or "", {}).get("reasons", [])
    current_score_row = scores.get(current_primary or "", {})
    recommended_score_row = scores.get(recommended or "", {})

    hard_current_problem = bool(
        current_primary in failing_nodes
        or "template-failing" in current_reasons
        or "child-not-running" in current_reasons
        or "critical-log" in current_reasons
    )
    hard_pool_pressure = any(
        bool(pressure.get(key))
        for key in (
            "rpc_refused",
            "share_stall",
            "job_stall",
            "rpc_template_failing",
            "node_template_probe_failing",
            "pool_template_frozen",
            "duplicate_block_storm",
            "stale_job_candidate_storm",
            "block_submit_error_storm",
            "block_submit_zero_success_storm",
        )
    )
    pool_quality_pressure = bool(pressure.get("pool_quality_pressure"))
    pool_pressure = hard_pool_pressure or pool_quality_pressure
    score_delta = recommended_score - current_score
    enough_delta = recommended_score >= MIN_SWITCH_SCORE and recommended_score - current_score >= min_delta
    current_p2p_errors = int(current_score_row.get("p2p_stream_errors") or 0)
    recommended_p2p_errors = int(recommended_score_row.get("p2p_stream_errors") or 0)
    p2p_degraded_current = bool(
        current_p2p_errors >= P2P_SWITCH_ERROR_COUNT
        and recommended_p2p_errors < current_p2p_errors
        and recommended_score >= MIN_SWITCH_SCORE
        and score_delta >= P2P_MIN_SCORE_DELTA
    )
    pool_pressure_has_node_case = bool(
        pool_pressure
        and recommended_score >= MIN_SWITCH_SCORE
        and score_delta >= POOL_PRESSURE_MIN_SCORE_DELTA
    )
    should_switch = bool(
        current_primary
        and recommended
        and recommended != current_primary
        and recommended_score >= MIN_SWITCH_SCORE
        and (hard_current_problem or p2p_degraded_current or enough_delta or pool_pressure_has_node_case)
    )
    reasons: list[str] = []
    if hard_current_problem:
        reasons.append("current-primary-hard-problem")
    if p2p_degraded_current:
        reasons.append("current-primary-p2p-degraded")
    if hard_pool_pressure:
        reasons.append("pool-pressure")
    if pool_quality_pressure:
        reasons.append("pool-quality-pressure")
    if enough_delta:
        reasons.append(f"score-delta-{score_delta:.1f}")
    elif pool_pressure and not pool_pressure_has_node_case:
        reasons.append("pool-pressure-no-node-advantage")
    if not should_switch:
        reasons.append("no-switch")

    return {
        "generated_at": now_iso(),
        "current_primary": current_primary,
        "current_haproxy_primary": current_primary,
        "pool_selected_backend": pool_selected_label,
        "pool_selected_backend_node": pool_selected_node,
        "routing_alignment": (
            "unknown"
            if not current_primary or not pool_selected_node
            else "aligned"
            if current_primary == pool_selected_node
            else "diverged"
        ),
        "recommended_primary": recommended,
        "should_switch": should_switch,
        "reason": ", ".join(reasons),
        "score_delta": round(score_delta, 3),
        "min_switch_score": MIN_SWITCH_SCORE,
        "min_score_delta": min_delta,
        "p2p_switch_error_count": P2P_SWITCH_ERROR_COUNT,
        "p2p_min_score_delta": P2P_MIN_SCORE_DELTA,
        "pool_pressure_min_score_delta": POOL_PRESSURE_MIN_SCORE_DELTA,
        "current_primary_suboptimal": bool(
            current_score <= 90
            or hard_current_problem
            or p2p_degraded_current
            or pool_pressure_has_node_case
        ),
        "scores": scores,
        "pool_pressure": pressure,
    }


def write_rpc_router_state(status: dict[str, Any], decision: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_runtime()
    decision = decision or recommend_rpc_primary(status)
    payload = {
        **decision,
        "status_overall": status.get("overall"),
        "status_reason": status.get("status_reason"),
        "status_generated_at": status.get("generated_at"),
    }
    RPC_ROUTER_STATE_FILE.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return payload


def apply_recommendation(status: dict[str, Any], decision: dict[str, Any], reason: str) -> bool:
    if not decision.get("should_switch"):
        return False
    target = str(decision.get("recommended_primary") or "")
    if target not in NODES:
        return False
    from watchdog import run_rpc_failover_switch  # Import lazily to avoid module cycles.

    ok = run_rpc_failover_switch(target, reason)
    append_incident(
        "rpc_router_switch",
        "warning" if ok else "critical",
        "rpc-router",
        f"rpc router switch to {target} {'succeeded' if ok else 'failed'}",
        {"decision": decision, "target": target, "ok": ok},
        status=status,
    )
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print JSON decision")
    parser.add_argument("--apply", action="store_true", help="apply the recommended RPC primary switch")
    parser.add_argument("--reason", default="manual rpc-router evaluation", help="reason to log if applying")
    parser.add_argument("--min-delta", type=float, default=MIN_SCORE_DELTA, help="minimum score delta before switching")
    args = parser.parse_args()

    status = collect_status_cached(include_logs=True)
    decision = recommend_rpc_primary(status, min_delta=args.min_delta)
    write_rpc_router_state(status, decision)
    applied = False
    if args.apply:
        applied = apply_recommendation(status, decision, args.reason)
        decision["applied"] = applied
    if args.json:
        print(json.dumps(decision, indent=2, sort_keys=True, default=str))
    else:
        print(f"current={decision.get('current_primary')} recommended={decision.get('recommended_primary')}")
        print(f"should_switch={decision.get('should_switch')} applied={applied}")
        print(f"reason={decision.get('reason')}")
        for node, item in (decision.get("scores") or {}).items():
            print(f"{node}: score={item.get('score')} state={item.get('state')} reasons={','.join(item.get('reasons') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
