#!/usr/bin/env python3
"""Coordinate large BlockDAG node catch-up without wasting duplicate bandwidth."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from incident_journal import append_incident
from pool_ops import (
    DATA_DIR,
    LOG_DIR,
    NODE_DATA_DIRS,
    NODES,
    POOL_ENV_FILE,
    PROJECT_ROOT,
    RUNTIME_DIR,
    action_log_path,
    collect_status,
    ensure_runtime,
    now_iso,
    read_json_file,
    run_logged,
    write_action_state,
    write_json_file,
)


STATE_FILE = RUNTIME_DIR / "sync-coordinator-state.json"
LOCK_FILE = RUNTIME_DIR / "sync-coordinator.lock"
LOG_FILE = LOG_DIR / "sync-coordinator.log"

FAR_BEHIND_BLOCKS = int(os.environ.get("BDAG_SYNC_COORDINATOR_FAR_BEHIND_BLOCKS", "1000"))
FOLLOWER_LAG_BLOCKS = int(os.environ.get("BDAG_SYNC_COORDINATOR_FOLLOWER_LAG_BLOCKS", "1000"))
LEADER_NEAR_TIP_BLOCKS = int(os.environ.get("BDAG_SYNC_COORDINATOR_LEADER_NEAR_TIP_BLOCKS", "1000"))
MIN_TRUSTED_HEIGHT = int(os.environ.get("BDAG_SYNC_COORDINATOR_MIN_TRUSTED_HEIGHT", "1000"))
LEADER_IMPORT_STALE_SECONDS = int(os.environ.get("BDAG_SYNC_COORDINATOR_IMPORT_STALE_SECONDS", "180"))
FINAL_RSYNC_TIMEOUT_SECONDS = int(os.environ.get("BDAG_SYNC_COORDINATOR_FINAL_RSYNC_TIMEOUT_SECONDS", "120"))
WARM_RSYNC_BWLIMIT_KB = os.environ.get("BDAG_SYNC_COORDINATOR_RSYNC_BWLIMIT_KB", "0")
LAGGING_FOLLOWER_RESTART_COOLDOWN_SECONDS = int(
    os.environ.get("BDAG_SYNC_COORDINATOR_LAGGING_RESTART_COOLDOWN_SECONDS", "1800")
)
MAJOR_LAG_MAX_SECONDS = int(os.environ.get("BDAG_SYNC_COORDINATOR_MAJOR_LAG_MAX_SECONDS", "600"))


def log(message: str) -> None:
    ensure_runtime()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message}\n")


def acquire_lock(blocking: bool = False):
    ensure_runtime()
    handle = LOCK_FILE.open("w")
    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), flags)
        return handle
    except BlockingIOError:
        handle.close()
        return None


def node_data_dir(node: str) -> Path:
    try:
        index = NODES.index(node)
    except ValueError as exc:
        raise ValueError(f"unknown node service: {node}") from exc
    dirname = NODE_DATA_DIRS[index] if index < len(NODE_DATA_DIRS) else f"node{index + 1}"
    return DATA_DIR / dirname


def compose_command(*args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(POOL_ENV_FILE),
        "-f",
        str(PROJECT_ROOT / "docker-compose.yml"),
        *args,
    ]


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def container_running(status: dict[str, Any], node: str) -> bool:
    containers = status.get("containers") if isinstance(status.get("containers"), dict) else {}
    row = containers.get(node) if isinstance(containers, dict) else None
    return bool(isinstance(row, dict) and row.get("running"))


def node_info(status: dict[str, Any], node: str) -> dict[str, Any]:
    nodes = status.get("nodes") if isinstance(status.get("nodes"), dict) else {}
    item = nodes.get(node) if isinstance(nodes, dict) else None
    return item if isinstance(item, dict) else {}


def progress_info(status: dict[str, Any], node: str) -> dict[str, Any]:
    progress = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    nodes = progress.get("nodes") if isinstance(progress.get("nodes"), dict) else {}
    item = nodes.get(node) if isinstance(nodes, dict) else None
    return item if isinstance(item, dict) else {}


def node_height(status: dict[str, Any], node: str) -> int:
    info = node_info(status, node)
    return safe_int(info.get("best_main_order") or info.get("latest_block"))


def node_remaining(status: dict[str, Any], node: str) -> int:
    progress = progress_info(status, node)
    remaining = progress.get("remaining_blocks")
    if remaining is not None:
        return safe_int(remaining)
    peer_ahead = node_info(status, node).get("peer_ahead_blocks")
    if peer_ahead is not None:
        return safe_int(peer_ahead)
    sync = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    highest = safe_int(sync.get("highest_block"))
    height = node_height(status, node)
    return max(0, highest - height) if highest and height else 0


def pool_has_fresh_mining_work(status: dict[str, Any]) -> bool:
    pool_health = status.get("pool_health") if isinstance(status.get("pool_health"), dict) else {}
    miner_health = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}
    connected = safe_int(miner_health.get("connected_count"))
    if connected <= 0:
        return False

    last_valid_share_age = safe_int(pool_health.get("last_valid_share_age_seconds"), 10**9)
    last_block_submit_age = safe_int(pool_health.get("last_block_submit_age_seconds"), 10**9)
    recent_valid_shares = safe_int(pool_health.get("valid_share_count"))
    recent_submit_success = safe_int(pool_health.get("block_submit_success_count"))
    return bool(
        last_valid_share_age <= 60
        or last_block_submit_age <= 60
        or recent_valid_shares > 0
        or recent_submit_success > 0
    )


def node_importing(status: dict[str, Any], node: str) -> bool:
    info = node_info(status, node)
    if info.get("importing"):
        return True
    return safe_int(info.get("last_import_age_seconds"), 999999) <= LEADER_IMPORT_STALE_SECONDS


def node_hard_bad(status: dict[str, Any], node: str) -> bool:
    info = node_info(status, node)
    return bool(
        info.get("critical")
        or info.get("mining_template_failing")
        or safe_int(info.get("last_import_age_seconds"), 0) > max(LEADER_IMPORT_STALE_SECONDS * 3, 600)
    )


def choose_leader(status: dict[str, Any]) -> str | None:
    candidates: list[tuple[int, int, int, str]] = []
    for node in NODES:
        if not container_running(status, node):
            continue
        height = node_height(status, node)
        if height <= 0:
            continue
        healthy = 0 if node_hard_bad(status, node) else 1
        importing = 1 if node_importing(status, node) else 0
        candidates.append((healthy, importing, height, node))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][3]


def stopped_by_coordinator(state: dict[str, Any], node: str) -> bool:
    return state.get("paused_follower") == node and state.get("mode") == "leader_catchup"


def remembered_highest_block(previous_state: dict[str, Any]) -> int:
    remembered = safe_int(previous_state.get("observed_highest_block"))
    last_decision = previous_state.get("last_decision") if isinstance(previous_state.get("last_decision"), dict) else {}
    remembered = max(remembered, safe_int(last_decision.get("observed_highest_block")))
    remembered = max(remembered, safe_int(last_decision.get("network_highest")))
    return remembered if remembered >= MIN_TRUSTED_HEIGHT else 0


def build_decision(status: dict[str, Any], previous_state: dict[str, Any]) -> dict[str, Any]:
    heights = {node: node_height(status, node) for node in NODES}
    remaining = {node: node_remaining(status, node) for node in NODES}
    running = {node: container_running(status, node) for node in NODES}
    importing = {node: node_importing(status, node) for node in NODES}
    leader = choose_leader(status)
    highest_height = max([value for value in heights.values() if value > 0] or [0])
    lowest_height = min([value for value in heights.values() if value > 0] or [0])
    block_lag = highest_height - lowest_height if highest_height and lowest_height else 0
    max_remaining = max(remaining.values() or [0])
    sync = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    current_network_highest = safe_int(sync.get("highest_block"))
    remembered_highest = remembered_highest_block(previous_state)
    network_highest = max(current_network_highest, remembered_highest)
    observed_highest = max(network_highest, highest_height)

    followers = [node for node in NODES if node != leader]
    follower = ""
    if followers:
        follower = sorted(followers, key=lambda item: (running.get(item, False), heights.get(item, 0)))[0]

    leader_remaining = remaining.get(leader or "", 0)
    leader_near_tip = bool(leader and leader_remaining <= LEADER_NEAR_TIP_BLOCKS)
    leader_gap_to_network = (
        max(0, network_highest - heights.get(leader, 0))
        if leader and network_highest >= MIN_TRUSTED_HEIGHT
        else 0
    )
    leader_proven_near_tip = bool(
        leader
        and network_highest >= MIN_TRUSTED_HEIGHT
        and leader_gap_to_network <= LEADER_NEAR_TIP_BLOCKS
    )
    active_mining = pool_has_fresh_mining_work(status)
    far_behind = bool(max_remaining >= FAR_BEHIND_BLOCKS)
    follower_lag_blocks = heights.get(leader, 0) - heights.get(follower, 0) if follower and leader else 0
    follower_materially_lagging = bool(follower and leader and follower_lag_blocks >= FOLLOWER_LAG_BLOCKS)
    paused_follower = str(previous_state.get("paused_follower") or "")
    paused_still_down = bool(paused_follower and not running.get(paused_follower, False))
    previous_decision = previous_state.get("last_decision") if isinstance(previous_state.get("last_decision"), dict) else {}
    previous_nodes = previous_decision.get("nodes") if isinstance(previous_decision.get("nodes"), dict) else {}
    previous_follower_height = 0
    if follower and isinstance(previous_nodes.get(follower), dict):
        previous_follower_height = safe_int(previous_nodes.get(follower, {}).get("height"))
    follower_progress = heights.get(follower, 0) - previous_follower_height if follower and previous_follower_height else 0
    now_epoch = int(time.time())
    last_lagging_restart_epoch = safe_int(previous_state.get("last_lagging_follower_restart_epoch"))
    lagging_restart_cooling_down = bool(
        last_lagging_restart_epoch
        and now_epoch - last_lagging_restart_epoch < LAGGING_FOLLOWER_RESTART_COOLDOWN_SECONDS
    )
    previous_major_lag_node = str(previous_state.get("major_lag_node") or "")
    previous_major_lag_started_epoch = safe_int(previous_state.get("major_lag_started_epoch"))
    if follower_materially_lagging:
        if previous_major_lag_node == follower and previous_major_lag_started_epoch:
            major_lag_started_epoch = previous_major_lag_started_epoch
        else:
            major_lag_started_epoch = now_epoch
        major_lag_duration_seconds = max(0, now_epoch - major_lag_started_epoch)
    else:
        major_lag_started_epoch = 0
        major_lag_duration_seconds = 0
    major_lag_over_max_duration = bool(
        follower_materially_lagging and major_lag_duration_seconds >= MAJOR_LAG_MAX_SECONDS
    )

    action = "monitor"
    reason = "dual-node sync is acceptable"
    target = ""

    if not leader:
        action = "none"
        reason = "no running node has a usable height"
    elif paused_still_down and leader_proven_near_tip:
        action = "seed_or_resume_follower"
        target = paused_follower
        reason = (
            f"{leader} is proven near tip with gap={leader_gap_to_network} block(s); "
            f"{paused_follower} can be seeded from the leader or resumed"
        )
    elif paused_still_down:
        action = "keep_follower_paused"
        target = paused_follower
        reason = (
            f"refusing follower seed because leader is not proven near tip; "
            f"{leader} remaining={leader_remaining} gap_to_network={leader_gap_to_network}; "
            f"keeping {paused_follower} paused saves bandwidth and disk IO"
        )
    elif (
        follower_materially_lagging
        and follower
        and running.get(follower)
        and active_mining
        and leader_near_tip
        and (follower_progress <= 0 or major_lag_over_max_duration)
    ):
        if lagging_restart_cooling_down:
            action = "monitor_lagging_follower"
            target = follower
            cooldown_remaining = LAGGING_FOLLOWER_RESTART_COOLDOWN_SECONDS - (now_epoch - last_lagging_restart_epoch)
            reason = (
                f"{follower} lags {follower_lag_blocks} DAG main-order/block(s) behind {leader}; "
                f"major lag duration={major_lag_duration_seconds}s max={MAJOR_LAG_MAX_SECONDS}s; "
                f"restart cooldown has {cooldown_remaining}s remaining"
            )
        else:
            action = "restart_lagging_follower"
            target = follower
            progress_reason = (
                f"major lag persisted for {major_lag_duration_seconds}s"
                if major_lag_over_max_duration
                else "it has not advanced since the last check"
            )
            reason = (
                f"{follower} lags {follower_lag_blocks} DAG main-order/block(s) "
                f"behind {leader} and {progress_reason}; restarting only the lagging "
                "standby clears stuck sync loops while mining continues on the near-tip leader"
            )
    elif follower_materially_lagging and follower and running.get(follower) and active_mining and leader_near_tip:
        action = "monitor_lagging_follower"
        target = follower
        reason = (
            f"{follower} lags {follower_lag_blocks} DAG main-order/block(s) "
            f"behind {leader}, but it advanced by {follower_progress} since the last check; "
            f"major lag duration={major_lag_duration_seconds}s max={MAJOR_LAG_MAX_SECONDS}s"
        )
    elif far_behind and active_mining and leader_near_tip:
        action = "monitor"
        reason = (
            "large catch-up detected, but pool has fresh mining work on a near-tip leader; "
            "not pausing a follower during productive mining"
        )
    elif far_behind and follower and running.get(follower) and follower_materially_lagging and importing.get(leader):
        action = "pause_follower"
        target = follower
        reason = (
            f"both nodes are far behind; {leader} is ahead at {heights.get(leader)} "
            f"and {follower} lags by {heights.get(leader, 0) - heights.get(follower, 0)} block(s)"
        )
    elif far_behind:
        action = "monitor_leader_catchup"
        reason = f"large catch-up detected, but no safe follower pause target was found; leader={leader}"

    return {
        "generated_at": now_iso(),
        "action": action,
        "reason": reason,
        "leader": leader,
        "target": target,
        "network_highest": network_highest,
        "current_network_highest": current_network_highest,
        "remembered_highest": remembered_highest,
        "observed_highest_block": observed_highest,
        "block_lag": block_lag,
        "max_remaining_blocks": max_remaining,
        "leader_remaining_blocks": leader_remaining,
        "leader_near_tip": leader_near_tip,
        "leader_gap_to_network": leader_gap_to_network,
        "leader_proven_near_tip": leader_proven_near_tip,
        "active_mining": active_mining,
        "far_behind": far_behind,
        "thresholds": {
            "far_behind_blocks": FAR_BEHIND_BLOCKS,
            "follower_lag_blocks": FOLLOWER_LAG_BLOCKS,
            "leader_near_tip_blocks": LEADER_NEAR_TIP_BLOCKS,
            "min_trusted_height": MIN_TRUSTED_HEIGHT,
            "import_stale_seconds": LEADER_IMPORT_STALE_SECONDS,
            "lagging_restart_cooldown_seconds": LAGGING_FOLLOWER_RESTART_COOLDOWN_SECONDS,
            "major_lag_max_seconds": MAJOR_LAG_MAX_SECONDS,
        },
        "major_lag": follower_materially_lagging,
        "major_lag_node": follower if follower_materially_lagging else "",
        "major_lag_blocks": follower_lag_blocks if follower_materially_lagging else 0,
        "major_lag_started_epoch": major_lag_started_epoch,
        "major_lag_duration_seconds": major_lag_duration_seconds,
        "nodes": {
            node: {
                "height": heights.get(node, 0),
                "remaining_blocks": remaining.get(node, 0),
                "running": running.get(node, False),
                "importing": importing.get(node, False),
                "hard_bad": node_hard_bad(status, node),
            }
            for node in NODES
        },
    }


def preserve_node_identity(source_dir: Path, preserve_dir: Path) -> dict[str, str]:
    preserve_dir.mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {}
    relative_paths = [
        Path("mainnet/network.key"),
        Path("mainnet/bdageth/nodekey"),
        Path("mainnet/keystore"),
    ]
    for rel in relative_paths:
        src = source_dir / rel
        if not src.exists():
            continue
        dst = preserve_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)
        copied[str(rel)] = str(dst)
    return copied


def apply_node_identity(target_dir: Path, preserve_dir: Path) -> None:
    for rel in [Path("mainnet/network.key"), Path("mainnet/bdageth/nodekey"), Path("mainnet/keystore")]:
        dst = target_dir / rel
        src = preserve_dir / rel
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists():
            dst.unlink()
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)
    peerstore = target_dir / "mainnet/peerstore"
    if peerstore.exists():
        shutil.rmtree(peerstore)


def rsync_node(source: Path, target: Path, log_path: Path, timeout: int) -> bool:
    target.mkdir(parents=True, exist_ok=True)
    command = [
        "rsync",
        "-a",
        "--delete-during",
        "--no-owner",
        "--no-group",
        "--chmod=Du+rwx,Dgo+rx,Fu+rw,Fgo+r",
        "--exclude=/mainnet/network.key",
        "--exclude=/mainnet/peerstore/",
    ]
    if WARM_RSYNC_BWLIMIT_KB != "0":
        command.append(f"--bwlimit={WARM_RSYNC_BWLIMIT_KB}")
    command.extend([f"{source}/", f"{target}/"])
    if shutil.which("ionice"):
        command = ["ionice", "-c3", *command]
    if shutil.which("nice"):
        command = ["nice", "-n", "19", *command]
    return run_logged(command, log_path, timeout=timeout).ok


def stop_node(node: str, log_path: Path) -> bool:
    return run_logged(compose_command("stop", node), log_path, timeout=180).ok


def start_node(node: str, log_path: Path) -> bool:
    return run_logged(compose_command("start", node), log_path, timeout=180).ok


def pause_follower(decision: dict[str, Any], state: dict[str, Any], log_path: Path) -> bool:
    target = str(decision.get("target") or "")
    leader = str(decision.get("leader") or "")
    if target not in NODES or leader not in NODES:
        return False
    ok = stop_node(target, log_path)
    if ok:
        state.update(
            {
                "mode": "leader_catchup",
                "paused_follower": target,
                "leader": leader,
                "paused_at": now_iso(),
                "paused_reason": decision.get("reason"),
            }
        )
        append_incident(
            "sync_coordinator_pause_follower",
            "warning",
            "sync-coordinator",
            f"paused {target} while {leader} catches up",
            {"decision": decision},
        )
    return ok


def seed_follower_from_leader(decision: dict[str, Any], state: dict[str, Any], log_path: Path, allow_leader_stop: bool) -> bool:
    leader = str(decision.get("leader") or state.get("leader") or "")
    follower = str(decision.get("target") or state.get("paused_follower") or "")
    if leader not in NODES or follower not in NODES or leader == follower:
        log_path.write_text(f"[{now_iso()}] invalid seed request leader={leader} follower={follower}\n", encoding="utf-8")
        return False
    if not bool(decision.get("leader_proven_near_tip")):
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"[{now_iso()}] refusing follower seed because leader is not proven near tip; "
                f"leader={leader} follower={follower} "
                f"gap_to_network={decision.get('leader_gap_to_network')} "
                f"network_highest={decision.get('network_highest')}\n"
            )
        return False
    if not allow_leader_stop:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"[{now_iso()}] refusing safe follower seed without --allow-leader-stop; "
                "copying a live LevelDB without a stopped final sync is unsafe\n"
            )
        return False

    stamp = time.strftime("%Y%m%d-%H%M%S")
    leader_dir = node_data_dir(leader)
    follower_dir = node_data_dir(follower)
    stage_dir = DATA_DIR / f".sync-coordinator-{follower}-{stamp}.tmp"
    preserve_dir = RUNTIME_DIR / f"sync-coordinator-identity-{follower}-{stamp}"
    backup_dir = DATA_DIR / f"{follower_dir.name}.before-sync-coordinator-{stamp}"
    preserved = preserve_node_identity(follower_dir, preserve_dir)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] preserved follower identity: {json.dumps(preserved, sort_keys=True)}\n")
        handle.write(f"[{now_iso()}] warm rsync {leader_dir} -> {stage_dir}\n")
    if not rsync_node(leader_dir, stage_dir, log_path, timeout=7200):
        return False

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] stopping follower {follower}\n")
    if not stop_node(follower, log_path):
        return False

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] stopping leader {leader} for final consistent rsync\n")
    if not stop_node(leader, log_path):
        start_node(follower, log_path)
        return False
    try:
        if not rsync_node(leader_dir, stage_dir, log_path, timeout=FINAL_RSYNC_TIMEOUT_SECONDS):
            return False
    finally:
        start_node(leader, log_path)

    if follower_dir.exists():
        follower_dir.rename(backup_dir)
    stage_dir.rename(follower_dir)
    apply_node_identity(follower_dir, preserve_dir)
    ok = start_node(follower, log_path)
    if ok:
        state.update(
            {
                "mode": "normal",
                "paused_follower": "",
                "leader": "",
                "seeded_follower": follower,
                "seeded_from": leader,
                "seeded_at": now_iso(),
                "follower_backup_dir": str(backup_dir),
            }
        )
        append_incident(
            "sync_coordinator_seed_follower",
            "warning",
            "sync-coordinator",
            f"seeded {follower} from {leader}",
            {"backup_dir": str(backup_dir), "decision": decision},
        )
    return ok


def resume_follower(decision: dict[str, Any], state: dict[str, Any], log_path: Path) -> bool:
    target = str(decision.get("target") or state.get("paused_follower") or "")
    if target not in NODES:
        return False
    ok = start_node(target, log_path)
    if ok:
        state.update(
            {
                "mode": "normal",
                "paused_follower": "",
                "leader": "",
                "resumed_follower": target,
                "resumed_at": now_iso(),
            }
        )
        append_incident(
            "sync_coordinator_resume_follower",
            "warning",
            "sync-coordinator",
            f"resumed {target}",
            {"decision": decision},
        )
    return ok


def restart_lagging_follower(decision: dict[str, Any], state: dict[str, Any], log_path: Path) -> bool:
    target = str(decision.get("target") or "")
    leader = str(decision.get("leader") or "")
    if target not in NODES or target == leader:
        return False
    ok = run_logged(compose_command("restart", target), log_path, timeout=180).ok
    if ok:
        state.update(
            {
                "mode": "normal",
                "last_lagging_follower_restart": target,
                "last_lagging_follower_restart_at": now_iso(),
                "last_lagging_follower_restart_epoch": int(time.time()),
                "last_lagging_follower_restart_reason": decision.get("reason"),
            }
        )
        append_incident(
            "sync_coordinator_restart_lagging_follower",
            "critical",
            "sync-coordinator",
            f"restarted lagging standby {target}",
            {"decision": decision},
        )
    return ok


def apply_decision(
    decision: dict[str, Any],
    state: dict[str, Any],
    *,
    allow_pause: bool,
    allow_resume: bool,
    allow_seed: bool,
    allow_leader_stop: bool,
    allow_restart_lagging_follower: bool,
) -> dict[str, Any]:
    action = str(decision.get("action") or "")
    action_name = f"sync-coordinator-{action}"
    log_path = action_log_path(action_name)
    payload = {
        "name": action_name,
        "mode": "sync-coordinator",
        "decision": decision,
        "status": "running",
        "started_at": now_iso(),
        "finished_at": None,
        "log_path": str(log_path),
    }
    write_action_state(payload)

    ok = True
    applied = "none"
    if action == "pause_follower":
        if allow_pause:
            ok = pause_follower(decision, state, log_path)
            applied = "pause_follower" if ok else "pause_follower_failed"
        else:
            applied = "pause_follower_suppressed"
    elif action == "seed_or_resume_follower":
        if allow_seed:
            ok = seed_follower_from_leader(decision, state, log_path, allow_leader_stop)
            applied = "seed_follower" if ok else "seed_follower_failed"
        elif allow_resume:
            ok = resume_follower(decision, state, log_path)
            applied = "resume_follower" if ok else "resume_follower_failed"
        else:
            applied = "seed_or_resume_suppressed"
    elif action == "restart_lagging_follower":
        if allow_restart_lagging_follower:
            ok = restart_lagging_follower(decision, state, log_path)
            applied = "restart_lagging_follower" if ok else "restart_lagging_follower_failed"
        else:
            applied = "restart_lagging_follower_suppressed"
    else:
        applied = "monitor"

    payload.update(
        {
            "status": "ok" if ok else "failed",
            "finished_at": now_iso(),
            "applied": applied,
        }
    )
    write_action_state(payload)
    decision["applied"] = applied
    decision["repair_ok"] = ok
    decision["log_path"] = str(log_path)
    return decision


def check_once(args: argparse.Namespace) -> dict[str, Any]:
    ensure_runtime()
    previous_state = read_json_file(STATE_FILE, {})
    status = collect_status(include_logs=True)
    decision = build_decision(status, previous_state)
    state = dict(previous_state)
    state.update(
        {
            "updated_at": now_iso(),
            "last_decision": decision,
            "observed_highest_block": max(
                remembered_highest_block(previous_state),
                safe_int(decision.get("observed_highest_block")),
            ),
        }
    )
    if decision.get("major_lag"):
        state["major_lag_node"] = decision.get("major_lag_node")
        state["major_lag_blocks"] = decision.get("major_lag_blocks")
        state["major_lag_started_epoch"] = decision.get("major_lag_started_epoch")
        state["major_lag_duration_seconds"] = decision.get("major_lag_duration_seconds")
    else:
        for key in (
            "major_lag_node",
            "major_lag_blocks",
            "major_lag_started_epoch",
            "major_lag_duration_seconds",
        ):
            state.pop(key, None)

    if args.repair:
        lock = acquire_lock(blocking=False)
        if lock is None:
            decision["applied"] = "suppressed"
            decision["repair_ok"] = False
            decision["reason"] = f"{decision.get('reason')}; another sync coordinator action is running"
        else:
            try:
                decision = apply_decision(
                    decision,
                    state,
                    allow_pause=args.pause_follower,
                    allow_resume=args.resume_follower,
                    allow_seed=args.seed_follower,
                    allow_leader_stop=args.allow_leader_stop,
                    allow_restart_lagging_follower=args.restart_lagging_follower,
                )
            finally:
                lock.close()

    state["last_decision"] = decision
    write_json_file(STATE_FILE, state)
    log(f"decision action={decision.get('action')} applied={decision.get('applied', 'dry-run')} reason={decision.get('reason')}")
    return {"decision": decision, "state": state}


def loop(args: argparse.Namespace) -> None:
    log(
        "sync coordinator started "
        f"interval={args.interval}s repair={args.repair} pause={args.pause_follower} "
        f"resume={args.resume_follower} seed={args.seed_follower}"
    )
    while True:
        try:
            check_once(args)
        except Exception as exc:  # noqa: BLE001 - coordinator should keep sampling.
            log(f"sync coordinator check crashed: {exc}")
            append_incident(
                "sync_coordinator_crashed",
                "critical",
                "sync-coordinator",
                str(exc),
                {},
            )
        time.sleep(args.interval)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Coordinate large BlockDAG dual-node catch-up")
    parser.add_argument("--once", action="store_true", help="run one check and print JSON")
    parser.add_argument("--loop", action="store_true", help="run continuously")
    parser.add_argument("--repair", action="store_true", help="apply permitted coordinator actions")
    parser.add_argument("--pause-follower", action="store_true", help="allow stopping the lagging follower during large catch-up")
    parser.add_argument("--resume-follower", action="store_true", help="allow starting a paused follower when catch-up is near complete")
    parser.add_argument("--seed-follower", action="store_true", help="allow replacing the paused follower data from the leader")
    parser.add_argument("--allow-leader-stop", action="store_true", help="allow a brief leader stop for final consistent follower seeding")
    parser.add_argument(
        "--restart-lagging-follower",
        action="store_true",
        help="allow restarting a running standby node that is materially lagging and not advancing",
    )
    parser.add_argument("--interval", type=int, default=int(os.environ.get("BDAG_SYNC_COORDINATOR_INTERVAL", "120")))
    args = parser.parse_args(argv)

    if args.loop:
        loop(args)
        return 0
    result = check_once(args)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    decision = result.get("decision", {})
    if decision.get("repair_ok") is False:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
