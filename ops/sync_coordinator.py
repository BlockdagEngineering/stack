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
    collect_status_cached,
    ensure_runtime,
    now_iso,
    read_json_file,
    run,
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
SEED_NEAR_TIP_BLOCKS = int(os.environ.get("BDAG_SYNC_COORDINATOR_SEED_NEAR_TIP_BLOCKS", "5"))
LEADER_IMPORT_STALE_SECONDS = int(os.environ.get("BDAG_SYNC_COORDINATOR_IMPORT_STALE_SECONDS", "180"))
FINAL_RSYNC_TIMEOUT_SECONDS = int(os.environ.get("BDAG_SYNC_COORDINATOR_FINAL_RSYNC_TIMEOUT_SECONDS", "900"))
WARM_RSYNC_BWLIMIT_KB = os.environ.get("BDAG_SYNC_COORDINATOR_RSYNC_BWLIMIT_KB", "0")
MIN_TRUSTED_HEIGHT = int(os.environ.get("BDAG_SYNC_COORDINATOR_MIN_TRUSTED_HEIGHT", "0"))
LEADER_CATCHUP_CPU_SHARES = int(os.environ.get("BDAG_SYNC_COORDINATOR_LEADER_CPU_SHARES", "8192"))
LEADER_CATCHUP_BLKIO_WEIGHT = int(os.environ.get("BDAG_SYNC_COORDINATOR_LEADER_BLKIO_WEIGHT", "1000"))
FAST_CATCHUP_RESTART_COOLDOWN_SECONDS = int(os.environ.get("BDAG_SYNC_COORDINATOR_FAST_RESTART_COOLDOWN_SECONDS", "900"))
FAST_CATCHUP_NODE_RESTART_TIMEOUT_SECONDS = int(os.environ.get("BDAG_SYNC_COORDINATOR_NODE_RESTART_TIMEOUT_SECONDS", "240"))
FAST_CATCHUP_REQUIRED_NODE_FLAG = "--fastartifactsync"


def env_enabled(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


FAST_CATCHUP_RESTART_ON_MISSING_FASTARTIFACT = env_enabled(
    "BDAG_SYNC_COORDINATOR_RESTART_ON_MISSING_FASTARTIFACT",
    True,
)
FAST_CATCHUP_RESTART_ON_STALE_IMPORT = env_enabled(
    "BDAG_SYNC_COORDINATOR_RESTART_ON_STALE_IMPORT",
    True,
)


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
    for key in ("latest_block", "current_block", "block_height"):
        value = safe_int(info.get(key))
        if value > 0:
            return value
    progress = progress_info(status, node)
    for key in ("current_block", "highest_block"):
        value = safe_int(progress.get(key))
        if value > 0:
            return value
    return 0


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


def remembered_highest_block(state: dict[str, Any]) -> int:
    values = [MIN_TRUSTED_HEIGHT, safe_int(state.get("observed_highest_block"))]
    last_decision = state.get("last_decision")
    if isinstance(last_decision, dict):
        values.append(safe_int(last_decision.get("network_highest")))
        nodes = last_decision.get("nodes")
        if isinstance(nodes, dict):
            values.extend(safe_int(item.get("height")) for item in nodes.values() if isinstance(item, dict))
    return max(values or [0])


def build_decision(status: dict[str, Any], previous_state: dict[str, Any]) -> dict[str, Any]:
    heights = {node: node_height(status, node) for node in NODES}
    remaining = {node: node_remaining(status, node) for node in NODES}
    running = {node: container_running(status, node) for node in NODES}
    importing = {node: node_importing(status, node) for node in NODES}
    leader = choose_leader(status)
    highest_height = max([value for value in heights.values() if value > 0] or [0])
    lowest_height = min([value for value in heights.values() if value > 0] or [0])
    block_lag = highest_height - lowest_height if highest_height and lowest_height else 0
    sync = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    current_network_highest = safe_int(sync.get("highest_block"))
    remembered_highest = remembered_highest_block(previous_state)
    network_highest = max(current_network_highest, remembered_highest)
    if network_highest:
        for node, height in heights.items():
            if height > 0:
                remaining[node] = max(remaining.get(node, 0), max(0, network_highest - height))
    max_remaining = max(remaining.values() or [0])

    followers = [node for node in NODES if node != leader]
    follower = ""
    if followers:
        running_followers = [node for node in followers if running.get(node, False)]
        candidate_followers = running_followers or followers
        follower = sorted(candidate_followers, key=lambda item: (heights.get(item, 0), item))[0]

    leader_remaining = remaining.get(leader or "", 0)
    leader_near_tip = bool(leader and network_highest > 0 and leader_remaining <= LEADER_NEAR_TIP_BLOCKS)
    far_behind = bool(max_remaining >= FAR_BEHIND_BLOCKS)
    follower_lag = max(0, heights.get(leader or "", 0) - heights.get(follower, 0)) if leader and follower else 0
    follower_materially_lagging = bool(follower and leader and follower_lag >= FOLLOWER_LAG_BLOCKS)
    paused_follower = str(previous_state.get("paused_follower") or "")
    paused_still_down = bool(stopped_by_coordinator(previous_state, paused_follower) and not running.get(paused_follower, False))
    paused_follower_remaining = remaining.get(paused_follower, 0)
    previous_decision = previous_state.get("last_decision") if isinstance(previous_state.get("last_decision"), dict) else {}
    previous_nodes = previous_decision.get("nodes") if isinstance(previous_decision.get("nodes"), dict) else {}
    if paused_follower and paused_follower_remaining <= 0 and isinstance(previous_nodes.get(paused_follower), dict):
        paused_follower_remaining = safe_int(previous_nodes.get(paused_follower, {}).get("remaining_blocks"))

    action = "monitor"
    reason = "dual-node sync is acceptable"
    target = ""

    if not leader:
        action = "none"
        reason = "no running node has a usable height"
    elif paused_still_down and leader_near_tip:
        action = "seed_or_resume_follower"
        target = paused_follower
        reason = (
            f"{leader} is near tip with {leader_remaining} remaining block(s); "
            f"{paused_follower} can be seeded from the leader or resumed once its remembered lag is within policy "
            f"(target remaining {paused_follower_remaining} block(s))"
        )
    elif paused_still_down:
        action = "keep_follower_paused"
        target = paused_follower
        reason = (
            f"{leader} is still catching up with {leader_remaining} remaining block(s); "
            f"keeping {paused_follower} paused saves bandwidth and disk IO"
        )
    elif previous_state.get("mode") == "leader_catchup" and paused_follower and running.get(paused_follower) and not far_behind:
        action = "clear_pause_state"
        target = paused_follower
        reason = (
            f"{paused_follower} is running and catch-up is within policy "
            f"(max remaining {max_remaining} block(s), threshold {FAR_BEHIND_BLOCKS})"
        )
    elif previous_state.get("mode") == "fast_sync_catchup" and not far_behind:
        action = "clear_pause_state"
        reason = (
            f"fast sync catch-up is within policy "
            f"(max remaining {max_remaining} block(s), threshold {FAR_BEHIND_BLOCKS})"
        )
    elif far_behind and follower and running.get(follower) and importing.get(leader):
        action = "pause_follower"
        target = follower
        if follower_materially_lagging:
            reason = (
                f"large catch-up detected; {leader} is ahead at {heights.get(leader)} "
                f"and {follower} trails by {follower_lag} block(s), so {leader} will sync alone"
            )
        else:
            reason = (
                f"large catch-up detected; {leader} will sync alone to conserve bandwidth and disk IO "
                f"while {follower} is paused (node-to-node lag {follower_lag} block(s))"
            )
    elif far_behind:
        action = "accelerate_leader_catchup"
        reason = (
            f"large catch-up detected; applying fastest sync defaults to {leader} "
            f"(remaining {leader_remaining} block(s), threshold {FAR_BEHIND_BLOCKS})"
        )

    return {
        "generated_at": now_iso(),
        "action": action,
        "reason": reason,
        "leader": leader,
        "target": target,
        "network_highest": network_highest,
        "current_network_highest": current_network_highest,
        "remembered_highest_block": remembered_highest,
        "block_lag": block_lag,
        "max_remaining_blocks": max_remaining,
        "leader_remaining_blocks": leader_remaining,
        "leader_near_tip": leader_near_tip,
        "far_behind": far_behind,
        "target_remaining_blocks": paused_follower_remaining if target == paused_follower else remaining.get(target, 0),
        "thresholds": {
            "far_behind_blocks": FAR_BEHIND_BLOCKS,
            "follower_lag_blocks": FOLLOWER_LAG_BLOCKS,
            "leader_near_tip_blocks": LEADER_NEAR_TIP_BLOCKS,
            "seed_near_tip_blocks": SEED_NEAR_TIP_BLOCKS,
            "import_stale_seconds": LEADER_IMPORT_STALE_SECONDS,
            "fast_restart_cooldown_seconds": FAST_CATCHUP_RESTART_COOLDOWN_SECONDS,
        },
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
        "--exclude=/mainnet/bdageth/nodekey",
        "--exclude=/mainnet/keystore/",
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


def compose_service_container_ids(service: str) -> list[str]:
    proc = run(compose_command("ps", "-q", service), timeout=20)
    ids = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return ids or [service]


def apply_leader_catchup_resources(
    decision: dict[str, Any],
    state: dict[str, Any],
    log_path: Path,
    *,
    record_incident: bool,
) -> bool:
    leader = str(decision.get("leader") or state.get("leader") or "")
    if leader not in NODES:
        return False
    targets = compose_service_container_ids(leader)
    command = [
        "docker",
        "update",
        "--cpu-shares",
        str(LEADER_CATCHUP_CPU_SHARES),
        "--blkio-weight",
        str(LEADER_CATCHUP_BLKIO_WEIGHT),
        *targets,
    ]
    ok = run_logged(command, log_path, timeout=60).ok
    state["leader_catchup_resources"] = {
        "leader": leader,
        "targets": targets,
        "cpu_shares": LEADER_CATCHUP_CPU_SHARES,
        "blkio_weight": LEADER_CATCHUP_BLKIO_WEIGHT,
        "applied_at": now_iso(),
        "ok": ok,
    }
    if ok and record_incident:
        append_incident(
            "sync_coordinator_boost_leader_resources",
            "warning",
            "sync-coordinator",
            f"boosted {leader} resources for one-node catch-up",
            {"decision": decision, "cpu_shares": LEADER_CATCHUP_CPU_SHARES, "blkio_weight": LEADER_CATCHUP_BLKIO_WEIGHT},
        )
    return ok


def node_command_has_fast_artifact_sync(command_line: str) -> bool:
    for word in command_line.split():
        if word == FAST_CATCHUP_REQUIRED_NODE_FLAG:
            return True
        if word.startswith(f"{FAST_CATCHUP_REQUIRED_NODE_FLAG}="):
            return word.split("=", 1)[1].strip().lower() not in {"0", "false", "no", "off"}
    return False


def node_command_line(node: str) -> Any:
    return run(
        compose_command(
            "exec",
            "-T",
            node,
            "sh",
            "-lc",
            "ps -eo args | awk '/[b]lockdag-node/{print; exit}'",
        ),
        timeout=20,
    )


def fast_sync_restart_cooldown_remaining(state: dict[str, Any]) -> int:
    last_epoch = safe_int(state.get("last_fast_sync_restart_epoch"), 0)
    if last_epoch <= 0:
        return 0
    return max(0, FAST_CATCHUP_RESTART_COOLDOWN_SECONDS - int(time.time() - last_epoch))


def fast_sync_restart_reason(
    decision: dict[str, Any],
    state: dict[str, Any],
    command_line: str,
    command_ok: bool,
) -> str:
    if fast_sync_restart_cooldown_remaining(state) > 0:
        return ""
    if (
        FAST_CATCHUP_RESTART_ON_MISSING_FASTARTIFACT
        and command_ok
        and command_line.strip()
        and not node_command_has_fast_artifact_sync(command_line)
    ):
        return f"node process is missing {FAST_CATCHUP_REQUIRED_NODE_FLAG}"

    leader = str(decision.get("leader") or state.get("leader") or "")
    nodes = decision.get("nodes") if isinstance(decision.get("nodes"), dict) else {}
    leader_row = nodes.get(leader) if isinstance(nodes.get(leader), dict) else {}
    if FAST_CATCHUP_RESTART_ON_STALE_IMPORT and leader_row and not bool(leader_row.get("importing")):
        return "node is far behind and import progress is stale"
    return ""


def maybe_restart_leader_for_fast_sync(decision: dict[str, Any], state: dict[str, Any], log_path: Path) -> bool:
    leader = str(decision.get("leader") or state.get("leader") or "")
    if leader not in NODES:
        return True

    command = node_command_line(leader)
    reason = fast_sync_restart_reason(decision, state, command.stdout, command.ok)
    with log_path.open("a", encoding="utf-8") as handle:
        if command.ok and command.stdout.strip():
            handle.write(f"[{now_iso()}] {leader} command line: {command.stdout.strip()}\n")
        elif not command.ok:
            handle.write(f"[{now_iso()}] could not inspect {leader} command line: {command.stderr.strip()}\n")
        cooldown = fast_sync_restart_cooldown_remaining(state)
        if cooldown > 0:
            handle.write(f"[{now_iso()}] fast sync restart cooldown active for {cooldown}s\n")

    if not reason:
        return True

    restart_ok = run_logged(
        compose_command("restart", leader),
        log_path,
        timeout=FAST_CATCHUP_NODE_RESTART_TIMEOUT_SECONDS,
    ).ok
    state["last_fast_sync_restart_epoch"] = int(time.time())
    state["last_fast_sync_restart_at"] = now_iso()
    state["last_fast_sync_restart_reason"] = reason
    state["last_fast_sync_restart_node"] = leader
    state["last_fast_sync_restart_ok"] = restart_ok
    append_incident(
        "sync_coordinator_fast_sync_restart",
        "warning" if restart_ok else "critical",
        "sync-coordinator",
        f"{'restarted' if restart_ok else 'failed to restart'} {leader} for fastest catch-up: {reason}",
        {"decision": decision, "restart_ok": restart_ok, "reason": reason},
    )
    return restart_ok


def accelerate_leader_fast_sync(decision: dict[str, Any], state: dict[str, Any], log_path: Path) -> bool:
    leader = str(decision.get("leader") or state.get("leader") or "")
    if leader not in NODES:
        return False
    state.update(
        {
            "mode": "fast_sync_catchup",
            "leader": leader,
            "fast_sync_accelerated_at": state.get("fast_sync_accelerated_at") or now_iso(),
            "fast_sync_accelerated_reason": decision.get("reason"),
        }
    )
    resource_ok = apply_leader_catchup_resources(decision, state, log_path, record_incident=False)
    restart_ok = maybe_restart_leader_for_fast_sync(decision, state, log_path)
    return resource_ok and restart_ok


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
        apply_leader_catchup_resources(decision, state, log_path, record_incident=True)
        maybe_restart_leader_for_fast_sync(decision, state, log_path)
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
    if safe_int(decision.get("network_highest")) <= 0 or safe_int(decision.get("leader_remaining_blocks"), 999999999) > SEED_NEAR_TIP_BLOCKS:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"[{now_iso()}] refusing follower seed because leader is not proven near tip; "
                f"network_highest={decision.get('network_highest')} "
                f"leader_remaining={decision.get('leader_remaining_blocks')} "
                f"threshold={SEED_NEAR_TIP_BLOCKS}\n"
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


def apply_decision(
    decision: dict[str, Any],
    state: dict[str, Any],
    *,
    allow_pause: bool,
    allow_resume: bool,
    allow_seed: bool,
    allow_leader_stop: bool,
    allow_accelerate_fastsync: bool,
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
        seed_allowed_now = safe_int(decision.get("leader_remaining_blocks"), 999999999) <= SEED_NEAR_TIP_BLOCKS
        resume_allowed_now = safe_int(decision.get("target_remaining_blocks"), 999999999) <= FAR_BEHIND_BLOCKS
        if allow_seed and seed_allowed_now:
            ok = seed_follower_from_leader(decision, state, log_path, allow_leader_stop)
            applied = "seed_follower" if ok else "seed_follower_failed"
        elif allow_resume and resume_allowed_now:
            ok = resume_follower(decision, state, log_path)
            applied = "resume_follower" if ok else "resume_follower_failed"
        elif allow_pause:
            ok = apply_leader_catchup_resources(decision, state, log_path, record_incident=False)
            applied = "keep_follower_paused" if ok else "keep_follower_paused_resource_boost_failed"
        else:
            applied = "seed_or_resume_suppressed"
    elif action == "keep_follower_paused":
        if allow_pause:
            target = str(decision.get("target") or state.get("paused_follower") or "")
            leader = str(decision.get("leader") or state.get("leader") or "")
            if target in NODES and leader in NODES:
                state.update(
                    {
                        "mode": "leader_catchup",
                        "paused_follower": target,
                        "leader": leader,
                        "paused_reason": decision.get("reason"),
                    }
                )
            ok = apply_leader_catchup_resources(decision, state, log_path, record_incident=False)
            restart_ok = maybe_restart_leader_for_fast_sync(decision, state, log_path)
            ok = ok and restart_ok
            applied = "keep_follower_paused" if ok else "keep_follower_paused_resource_boost_failed"
        else:
            applied = "keep_follower_paused_suppressed"
    elif action == "accelerate_leader_catchup":
        if allow_accelerate_fastsync:
            ok = accelerate_leader_fast_sync(decision, state, log_path)
            applied = "accelerate_leader_catchup" if ok else "accelerate_leader_catchup_failed"
        else:
            applied = "accelerate_leader_catchup_suppressed"
    elif action == "clear_pause_state":
        state.update(
            {
                "mode": "normal",
                "paused_follower": "",
                "leader": "",
                "cleared_at": now_iso(),
                "cleared_reason": decision.get("reason"),
            }
        )
        applied = "clear_pause_state"
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
    status = collect_status_cached(include_logs=True)
    decision = build_decision(status, previous_state)
    state = dict(previous_state)
    observed_highest = max(
        remembered_highest_block(previous_state),
        safe_int(decision.get("network_highest")),
    )
    state.update(
        {
            "updated_at": now_iso(),
            "last_decision": decision,
            "observed_highest_block": observed_highest,
        }
    )

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
                    allow_accelerate_fastsync=args.accelerate_fastsync,
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
        f"resume={args.resume_follower} seed={args.seed_follower} "
        f"accelerate_fastsync={args.accelerate_fastsync}"
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
        "--accelerate-fastsync",
        dest="accelerate_fastsync",
        action="store_true",
        default=env_enabled("BDAG_SYNC_COORDINATOR_ACCELERATE_FASTSYNC", True),
        help="allow fastest-sync acceleration when a running node is more than the far-behind threshold behind",
    )
    parser.add_argument(
        "--no-accelerate-fastsync",
        dest="accelerate_fastsync",
        action="store_false",
        help="disable fastest-sync acceleration even when the node is far behind",
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
