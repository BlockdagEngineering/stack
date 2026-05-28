#!/usr/bin/env python3
"""Coordinate large BlockDAG node catch-up without wasting duplicate bandwidth."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def bootstrap_stack_env() -> None:
    project_root = Path(os.environ.get("BDAG_PROJECT_ROOT") or Path(__file__).resolve().parents[1])
    pool_env = Path(os.environ["BDAG_POOL_ENV_FILE"]) if os.environ.get("BDAG_POOL_ENV_FILE") else None
    if pool_env is not None and not pool_env.is_absolute():
        pool_env = project_root / pool_env
    candidates = [
        pool_env,
        project_root / ".env",
        project_root / "asic-pool" / ".env",
    ]
    for path in candidates:
        if path is None or not path.exists():
            continue
        for line in path.read_text(errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped[7:].strip()
            if "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)


bootstrap_stack_env()

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
FAST_CATCHUP_ARTIFACT_MODE = os.environ.get("BDAG_FAST_CATCHUP_ARTIFACT_MODE", "auto").strip().lower()
FAST_CATCHUP_ARTIFACT_RETRY_SECONDS = int(os.environ.get("BDAG_FAST_CATCHUP_ARTIFACT_RETRY_SECONDS", "300"))
FAST_CATCHUP_ARTIFACT_MIN_BEHIND_BLOCKS = int(
    os.environ.get("BDAG_FAST_CATCHUP_ARTIFACT_MIN_BEHIND_BLOCKS", str(FAR_BEHIND_BLOCKS))
)
FAST_CATCHUP_ARTIFACT_MIN_GAIN_BLOCKS = int(
    os.environ.get("BDAG_FAST_CATCHUP_ARTIFACT_MIN_GAIN_BLOCKS", str(FAR_BEHIND_BLOCKS))
)
FAST_CATCHUP_ARTIFACT_MAX_PROBE_PEERS = int(os.environ.get("BDAG_FAST_CATCHUP_ARTIFACT_MAX_PROBE_PEERS", "8"))
FAST_CATCHUP_ARTIFACT_PROBE_TIMEOUT_SECONDS = int(os.environ.get("BDAG_FAST_CATCHUP_ARTIFACT_PROBE_TIMEOUT_SECONDS", "20"))
FAST_CATCHUP_ARTIFACT_IMPORT_TIMEOUT_SECONDS = int(os.environ.get("BDAG_FAST_CATCHUP_ARTIFACT_IMPORT_TIMEOUT_SECONDS", "21600"))
FAST_CATCHUP_ARTIFACT_DOWNLOAD_TIMEOUT = os.environ.get("BDAG_FAST_CATCHUP_ARTIFACT_TIMEOUT", "21600s")
FAST_CATCHUP_ARTIFACT_FETCH_SCRIPT = Path(
    os.environ.get("BDAG_FAST_CATCHUP_ARTIFACT_FETCH_SCRIPT", str(PROJECT_ROOT / "ops" / "fetch-rawdatadir-artifact.sh"))
)


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
FAST_CATCHUP_ARTIFACT_TRUST_ON_FIRST_SIGNED = env_enabled(
    "BDAG_FAST_CATCHUP_ARTIFACT_TRUST_ON_FIRST_SIGNED",
    True,
)
FAST_CATCHUP_ALLOW_UNSIGNED_ARTIFACTS = env_enabled(
    "BDAG_FAST_CATCHUP_ALLOW_UNSIGNED_ARTIFACTS",
    False,
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


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def env_value(env_values: dict[str, str], name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    if raw is not None and raw != "":
        return raw
    return env_values.get(name, default)


def split_list_value(value: str) -> list[str]:
    return [item for item in re.split(r"[\s,;]+", value.strip()) if item]


def append_unique(items: list[str], value: str) -> None:
    value = value.strip()
    if value and value not in items:
        items.append(value)


def addpeer_values(raw_args: str) -> list[str]:
    if not raw_args.strip():
        return []
    try:
        words = shlex.split(raw_args)
    except ValueError:
        words = raw_args.split()
    peers: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        value = ""
        if word == "--addpeer" and index + 1 < len(words):
            index += 1
            value = words[index]
        elif word.startswith("--addpeer="):
            value = word.split("=", 1)[1]
        for peer in split_list_value(value):
            append_unique(peers, peer)
        index += 1
    return peers


def fastest_artifact_peer_candidates(env_values: dict[str, str]) -> list[str]:
    """Return raw-datadir artifact candidates in fastest-first order."""
    peer_env_order = [
        "BDAG_RAWDATADIR_PEERS",
        "BDAG_FASTSNAP_PEERS",
        "BDAG_FASTSYNC_LAN_PEERS",
        "BDAG_FASTSYNC_LOCAL_PEERS",
        "BDAG_P2P_LAN_PEERS",
        "LAN_PEER_ADDRESSES",
        "BDAG_FASTSYNC_VPN_PEERS",
        "BDAG_FASTSYNC_PRIVATE_PEERS",
        "BDAG_P2P_VPN_PEERS",
        "VPN_PEER_ADDRESSES",
        "ZEROTIER_PEER_ADDRESSES",
        "BDAG_FASTSYNC_PUBLIC_PEERS",
        "BDAG_P2P_PUBLIC_PEERS",
        "BDAG_FASTSYNC_PEERS",
        "BOOTSTRAP_PEER_ADDRESSES",
        "NODE1_PEER_ADDRESSES",
        "NODE2_PEER_ADDRESSES",
    ]
    peers: list[str] = []
    for name in peer_env_order:
        for peer in split_list_value(env_value(env_values, name)):
            append_unique(peers, peer)
    for name in ("NODE_ARGS_APPEND", "NODE_ARGS", "BDAG_NODE_ARGS", "NODE1_ARGS", "NODE2_ARGS"):
        for peer in addpeer_values(env_value(env_values, name)):
            append_unique(peers, peer)
    return peers


def peer_probe_batch(peers: list[str], state: dict[str, Any], pinned_count: int = 0) -> list[str]:
    if not peers:
        return []
    max_peers = max(1, FAST_CATCHUP_ARTIFACT_MAX_PROBE_PEERS)
    pinned = peers[: min(max(0, pinned_count), len(peers), max_peers)]
    rotating = peers[len(pinned) :]
    if not rotating:
        return pinned
    cursor = safe_int(state.get("fast_artifact_probe_cursor"), 0) % len(rotating)
    ordered = rotating[cursor:] + rotating[:cursor]
    batch = [*pinned, *ordered[: max_peers - len(pinned)]]
    state["fast_artifact_probe_cursor"] = (cursor + max(0, len(batch) - len(pinned))) % len(rotating)
    return batch


def fastsnap_binary(env_values: dict[str, str]) -> str:
    configured = env_value(env_values, "BDAG_RAWDATADIR_FASTSNAP_BINARY") or env_value(env_values, "BDAG_FASTSNAP_BINARY")
    candidates = [
        configured,
        str(PROJECT_ROOT / "artifacts" / "binaries" / "linux-arm64" / "fastsnap"),
        str(PROJECT_ROOT / "artifacts" / "binaries" / "linux-amd64" / "fastsnap"),
        "/usr/local/bin/fastsnap",
        shutil.which("fastsnap") or "",
        "fastsnap",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if "/" not in candidate:
            return candidate
        path = Path(candidate)
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return "fastsnap"


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


def parse_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def recursive_numeric_value(payload: Any, names: set[str]) -> int:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key) in names:
                numeric = safe_int(value)
                if numeric > 0:
                    return numeric
        for value in payload.values():
            numeric = recursive_numeric_value(value, names)
            if numeric > 0:
                return numeric
    elif isinstance(payload, list):
        for value in payload:
            numeric = recursive_numeric_value(value, names)
            if numeric > 0:
                return numeric
    return 0


def rawdatadir_manifest_progress(manifest: dict[str, Any]) -> dict[str, int]:
    tip_order = recursive_numeric_value(manifest, {"tip_order", "tipOrder", "tip_height", "height"})
    block_total = recursive_numeric_value(manifest, {"block_total", "blockTotal", "blocks", "block_count", "blockCount"})
    return {
        "tip_order": tip_order,
        "block_total": block_total,
        "best_height": max(tip_order, block_total),
    }


def signature_dict_has_material(payload: dict[str, Any], under_signature_key: bool) -> bool:
    if under_signature_key:
        return True
    for key in payload:
        lowered = str(key).lower()
        if lowered in {"sig", "signature", "signature_hex", "signaturehex", "value"}:
            return True
        if "signature" in lowered:
            return True
    return False


def collect_signature_specs(payload: Any, *, under_signature_key: bool = False) -> list[str]:
    specs: list[str] = []
    if isinstance(payload, dict):
        key_id = str(payload.get("key_id") or payload.get("keyId") or payload.get("id") or "").strip()
        public_key = str(
            payload.get("public_key")
            or payload.get("publicKey")
            or payload.get("signing_public_key")
            or payload.get("signingPublicKey")
            or ""
        ).strip()
        if key_id and public_key and signature_dict_has_material(payload, under_signature_key):
            append_unique(specs, f"{key_id}:{public_key}")
        for key, value in payload.items():
            child_specs = collect_signature_specs(
                value,
                under_signature_key=under_signature_key or "signature" in str(key).lower(),
            )
            for spec in child_specs:
                append_unique(specs, spec)
    elif isinstance(payload, list):
        for value in payload:
            for spec in collect_signature_specs(value, under_signature_key=under_signature_key):
                append_unique(specs, spec)
    return specs


def configured_trusted_signers(env_values: dict[str, str]) -> list[str]:
    signers: list[str] = []
    for name in ("BDAG_RAWDATADIR_TRUSTED_SIGNERS", "BDAG_FASTSNAP_TRUSTED_SIGNERS"):
        for signer in split_list_value(env_value(env_values, name)):
            append_unique(signers, signer)
    return signers


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
    import_stale = safe_int(info.get("last_import_age_seconds"), 0) > max(LEADER_IMPORT_STALE_SECONDS * 3, 600)
    behind_tip = node_remaining(status, node) > LEADER_NEAR_TIP_BLOCKS
    return bool(
        info.get("critical")
        or info.get("mining_template_failing")
        or (behind_tip and import_stale)
    )


def choose_leader(status: dict[str, Any]) -> str | None:
    candidates: list[tuple[int, int, int, int, str]] = []
    for node in NODES:
        if not container_running(status, node):
            continue
        height = node_height(status, node)
        if height <= 0:
            continue
        healthy = 0 if node_hard_bad(status, node) else 1
        importing = 1 if node_importing(status, node) else 0
        # A caught-up node can be idle because there is nothing left to import.
        # Height must outrank importing state so catch-up never pauses the node
        # closest to tip in favor of a lower node that is still importing.
        candidates.append((healthy, height, importing, -node_remaining(status, node), node))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][4]


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
    leader_height_unknown = False
    if leader is None:
        running_nodes = [node for node in NODES if running.get(node, False)]
        if running_nodes:
            leader = running_nodes[0]
            leader_height_unknown = True
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
    if leader_height_unknown:
        leader_remaining = max(leader_remaining, FAR_BEHIND_BLOCKS)
        remaining[leader or ""] = leader_remaining
        max_remaining = max(max_remaining, leader_remaining)
    leader_near_tip = bool(leader and network_highest > 0 and leader_remaining <= LEADER_NEAR_TIP_BLOCKS)
    far_behind = bool(max_remaining >= FAR_BEHIND_BLOCKS)
    follower_lag = max(0, heights.get(leader or "", 0) - heights.get(follower, 0)) if leader and follower else 0
    follower_materially_lagging = bool(follower and leader and follower_lag >= FOLLOWER_LAG_BLOCKS)
    paused_follower = str(previous_state.get("paused_follower") or "")
    if paused_follower not in NODES:
        paused_follower = ""
    paused_still_down = bool(stopped_by_coordinator(previous_state, paused_follower) and not running.get(paused_follower, False))
    paused_follower_remaining = remaining.get(paused_follower, 0)
    previous_decision = previous_state.get("last_decision") if isinstance(previous_state.get("last_decision"), dict) else {}
    previous_nodes = previous_decision.get("nodes") if isinstance(previous_decision.get("nodes"), dict) else {}
    if paused_follower and paused_follower_remaining <= 0 and isinstance(previous_nodes.get(paused_follower), dict):
        paused_follower_remaining = safe_int(previous_nodes.get(paused_follower, {}).get("remaining_blocks"))
    if paused_follower and paused_follower_remaining <= 0:
        paused_follower_remaining = safe_int(previous_state.get("paused_follower_remaining_blocks"))
    paused_previous_height = safe_int(previous_state.get("paused_follower_height"))
    if isinstance(previous_nodes.get(paused_follower), dict):
        paused_previous_height = max(paused_previous_height, safe_int(previous_nodes.get(paused_follower, {}).get("height")))
    paused_follower_was_ahead = bool(
        paused_still_down
        and leader
        and paused_previous_height > heights.get(leader, 0) + FOLLOWER_LAG_BLOCKS
    )
    paused_follower_was_near_tip = bool(
        paused_still_down
        and leader
        and paused_previous_height > 0
        and paused_follower_remaining <= LEADER_NEAR_TIP_BLOCKS
        and leader_remaining >= FAR_BEHIND_BLOCKS
    )

    action = "monitor"
    reason = "single-node sync is within policy" if len(NODES) == 1 else "dual-node sync is acceptable"
    target = ""

    if not leader:
        action = "none"
        reason = "no running node has a usable height"
    elif leader_height_unknown:
        action = "accelerate_leader_catchup"
        reason = (
            f"{leader} is running but has no usable chain height; probing fastest verified sync sources "
            "instead of waiting for slow or damaged local state"
        )
    elif paused_follower_was_ahead or paused_follower_was_near_tip:
        action = "seed_or_resume_follower"
        target = paused_follower
        reason = (
            f"{paused_follower} is paused but its remembered height/lag is better than the running leader "
            f"({paused_follower} height {paused_previous_height}, remaining {paused_follower_remaining}; "
            f"{leader} height {heights.get(leader, 0)}, remaining {leader_remaining}); resuming it prevents mining on a lagging node"
        )
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
    elif far_behind and follower and running.get(follower) and leader and not node_hard_bad(status, leader):
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
        "leader_height_unknown": leader_height_unknown,
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


def pause_follower_safety(decision: dict[str, Any]) -> tuple[bool, str]:
    leader = str(decision.get("leader") or "")
    target = str(decision.get("target") or "")
    nodes = decision.get("nodes") if isinstance(decision.get("nodes"), dict) else {}
    leader_row = nodes.get(leader) if isinstance(nodes.get(leader), dict) else {}
    target_row = nodes.get(target) if isinstance(nodes.get(target), dict) else {}
    leader_height = safe_int(leader_row.get("height"))
    target_height = safe_int(target_row.get("height"))
    leader_remaining = safe_int(leader_row.get("remaining_blocks"))
    target_remaining = safe_int(target_row.get("remaining_blocks"))
    if not leader or not target or leader == target:
        return False, f"invalid leader/target leader={leader!r} target={target!r}"
    if leader_height > 0 and target_height > leader_height:
        return (
            False,
            f"refusing to pause {target}: target height {target_height} is ahead of leader {leader} height {leader_height}",
        )
    if leader_remaining >= FAR_BEHIND_BLOCKS and target_remaining + FOLLOWER_LAG_BLOCKS < leader_remaining:
        return (
            False,
            f"refusing to pause {target}: target remaining {target_remaining} is materially better than leader {leader} remaining {leader_remaining}",
        )
    return True, ""


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


def docker_container_is_running(name: str) -> bool:
    proc = run(["docker", "inspect", "-f", "{{.State.Running}}", name], timeout=20)
    return bool(proc.ok and proc.stdout.strip().lower() == "true")


def stop_node(node: str, log_path: Path) -> bool:
    compose_ok = run_logged(compose_command("stop", node), log_path, timeout=180).ok
    if docker_container_is_running(node):
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] compose stop left {node} running; using direct docker stop\n")
        run_logged(["docker", "stop", node], log_path, timeout=180)
    return compose_ok and not docker_container_is_running(node)


def start_node(node: str, log_path: Path) -> bool:
    start_ok = run_logged(compose_command("start", node), log_path, timeout=180).ok
    if not start_ok:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] compose start failed for {node}; using compose up -d fallback\n")
        start_ok = run_logged(compose_command("up", "-d", node), log_path, timeout=240).ok
    return start_ok and docker_container_is_running(node)


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
    if safe_int(decision.get("leader_remaining_blocks")) <= LEADER_NEAR_TIP_BLOCKS:
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


def fast_artifact_retry_cooldown_remaining(state: dict[str, Any]) -> int:
    last_epoch = safe_int(state.get("last_fast_artifact_attempt_epoch"), 0)
    if last_epoch <= 0:
        return 0
    return max(0, FAST_CATCHUP_ARTIFACT_RETRY_SECONDS - int(time.time() - last_epoch))


def rawdatadir_import_target(node: str, env_values: dict[str, str]) -> Path:
    network = env_value(env_values, "BDAG_RAWDATADIR_NETWORK") or env_value(env_values, "BDAG_FASTSNAP_NETWORK", "mainnet")
    return node_data_dir(node) / network


def leader_local_height(decision: dict[str, Any], leader: str) -> int:
    nodes = decision.get("nodes") if isinstance(decision.get("nodes"), dict) else {}
    row = nodes.get(leader) if isinstance(nodes.get(leader), dict) else {}
    return safe_int(row.get("height"))


def probe_rawdatadir_manifest(peer: str, env_values: dict[str, str], log_path: Path) -> dict[str, Any] | None:
    network = env_value(env_values, "BDAG_RAWDATADIR_NETWORK") or env_value(env_values, "BDAG_FASTSNAP_NETWORK", "mainnet")
    timeout = f"{FAST_CATCHUP_ARTIFACT_PROBE_TIMEOUT_SECONDS}s"
    command = [
        fastsnap_binary(env_values),
        "--manifest-only",
        "--artifact-v2=true",
        "--artifact-type",
        "raw_datadir_checkpoint",
        "--legacy-fallback=false",
        "--network",
        network,
        "--timeout",
        timeout,
        "--peer",
        peer,
    ]
    for signer in configured_trusted_signers(env_values):
        command.extend(["--trusted-signer", signer])
    if FAST_CATCHUP_ARTIFACT_TRUST_ON_FIRST_SIGNED or FAST_CATCHUP_ALLOW_UNSIGNED_ARTIFACTS:
        command.append("--allow-unsigned")

    proc = run(command, timeout=FAST_CATCHUP_ARTIFACT_PROBE_TIMEOUT_SECONDS + 5)
    with log_path.open("a", encoding="utf-8") as handle:
        if proc.ok:
            handle.write(f"[{now_iso()}] raw datadir manifest probe ok peer={peer}\n")
        else:
            detail = (proc.stderr or proc.stdout).strip().replace("\n", " ")[:500]
            handle.write(f"[{now_iso()}] raw datadir manifest probe failed peer={peer}: {detail}\n")
    if not proc.ok:
        return None
    return parse_json_object(f"{proc.stdout}\n{proc.stderr}")


def select_rawdatadir_artifact_candidate(
    decision: dict[str, Any],
    state: dict[str, Any],
    env_values: dict[str, str],
    log_path: Path,
) -> dict[str, Any] | None:
    leader = str(decision.get("leader") or state.get("leader") or "")
    local_height = leader_local_height(decision, leader)
    peers = fastest_artifact_peer_candidates(env_values)
    pinned_count = len(split_list_value(env_value(env_values, "BDAG_RAWDATADIR_PEERS")))
    batch = peer_probe_batch(peers, state, pinned_count=pinned_count)
    if not peers:
        state["last_fast_artifact_result"] = "no_peers"
        state["last_fast_artifact_reason"] = (
            "no raw-datadir/FastSnap/FastSync peer candidates configured; coordinator will retry"
        )
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] no raw datadir artifact peers configured; retry remains enabled\n")
        return None
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"[{now_iso()}] probing {len(batch)}/{len(peers)} raw datadir artifact peer(s); "
            f"cursor={state.get('fast_artifact_probe_cursor')}\n"
        )

    trusted_signers = configured_trusted_signers(env_values)
    best: dict[str, Any] | None = None
    for peer in batch:
        manifest = probe_rawdatadir_manifest(peer, env_values, log_path)
        if not manifest:
            continue
        progress = rawdatadir_manifest_progress(manifest)
        best_height = progress["best_height"]
        gain = best_height - local_height if local_height > 0 else best_height
        manifest_signers = collect_signature_specs(manifest)
        install_signers = trusted_signers or (manifest_signers if FAST_CATCHUP_ARTIFACT_TRUST_ON_FIRST_SIGNED else [])
        if not install_signers and not FAST_CATCHUP_ALLOW_UNSIGNED_ARTIFACTS:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"[{now_iso()}] raw datadir artifact peer={peer} skipped: signed trusted signer not available\n"
                )
            continue
        if gain < FAST_CATCHUP_ARTIFACT_MIN_GAIN_BLOCKS:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"[{now_iso()}] raw datadir artifact peer={peer} skipped: gain={gain} "
                    f"min_gain={FAST_CATCHUP_ARTIFACT_MIN_GAIN_BLOCKS} progress={progress}\n"
                )
            continue
        candidate = {
            "peer": peer,
            "manifest_progress": progress,
            "gain_blocks": gain,
            "trusted_signers": install_signers,
            "manifest_signers": manifest_signers,
        }
        if best is None or safe_int(candidate.get("gain_blocks")) > safe_int(best.get("gain_blocks")):
            best = candidate

    if best is None:
        state["last_fast_artifact_result"] = "no_usable_candidate"
        state["last_fast_artifact_reason"] = "no probed peer offered a verified raw datadir artifact ahead enough"
    return best


def install_rawdatadir_artifact(candidate: dict[str, Any], leader: str, env_values: dict[str, str], log_path: Path) -> bool:
    peer = str(candidate.get("peer") or "")
    trusted_signers = [str(item) for item in candidate.get("trusted_signers", []) if str(item).strip()]
    target = rawdatadir_import_target(leader, env_values)
    fetch_log = LOG_DIR / f"rawdatadir-fetch-{time.strftime('%Y%m%d')}.log"
    env_pairs = {
        "BDAG_PROJECT_ROOT": str(PROJECT_ROOT),
        "BDAG_RAWDATADIR_PEERS": peer,
        "BDAG_RAWDATADIR_TRUSTED_SIGNERS": ",".join(trusted_signers),
        "BDAG_RAWDATADIR_IMPORT_TARGET": str(target),
        "BDAG_RAWDATADIR_IMPORT_REPLACE": "1",
        "BDAG_RAWDATADIR_FASTSNAP_BINARY": fastsnap_binary(env_values),
        "BDAG_RAWDATADIR_TIMEOUT": env_value(env_values, "BDAG_RAWDATADIR_TIMEOUT", FAST_CATCHUP_ARTIFACT_DOWNLOAD_TIMEOUT),
        "BDAG_RAWDATADIR_NETWORK": env_value(env_values, "BDAG_RAWDATADIR_NETWORK")
        or env_value(env_values, "BDAG_FASTSNAP_NETWORK", "mainnet"),
        "BDAG_RAWDATADIR_PARALLELISM": env_value(env_values, "BDAG_RAWDATADIR_PARALLELISM", "4"),
        "BDAG_RAWDATADIR_DOWNLOAD_BASE": env_value(
            env_values,
            "BDAG_RAWDATADIR_DOWNLOAD_BASE",
            str(DATA_DIR / "rawdatadir-downloads"),
        ),
        "BDAG_RAWDATADIR_FETCH_LOG": str(fetch_log),
        "BDAG_RAWDATADIR_ALLOW_UNSIGNED": "1" if FAST_CATCHUP_ALLOW_UNSIGNED_ARTIFACTS else "0",
    }
    command = ["env", *[f"{key}={value}" for key, value in env_pairs.items()], str(FAST_CATCHUP_ARTIFACT_FETCH_SCRIPT)]

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"[{now_iso()}] installing verified raw datadir artifact for {leader}: peer={peer} "
            f"target={target} progress={candidate.get('manifest_progress')} gain={candidate.get('gain_blocks')}\n"
        )
    if not stop_node(leader, log_path):
        return False
    fetch_ok = False
    start_ok = False
    try:
        fetch_ok = run_logged(command, log_path, timeout=FAST_CATCHUP_ARTIFACT_IMPORT_TIMEOUT_SECONDS).ok
    finally:
        start_ok = start_node(leader, log_path)
        if not start_ok:
            append_incident(
                "sync_coordinator_fast_artifact_restart_failed",
                "critical",
                "sync-coordinator",
                f"failed to restart {leader} after raw datadir artifact attempt",
                {"leader": leader, "candidate": candidate, "fetch_ok": fetch_ok},
            )
    return fetch_ok and start_ok


def maybe_apply_fast_artifact_catchup(decision: dict[str, Any], state: dict[str, Any], log_path: Path) -> bool:
    if FAST_CATCHUP_ARTIFACT_MODE in {"0", "false", "no", "off", "disabled"}:
        return True
    leader = str(decision.get("leader") or state.get("leader") or "")
    if leader not in NODES:
        return True
    behind_blocks = max(safe_int(decision.get("leader_remaining_blocks")), safe_int(decision.get("max_remaining_blocks")))
    if behind_blocks < FAST_CATCHUP_ARTIFACT_MIN_BEHIND_BLOCKS:
        return True
    cooldown = fast_artifact_retry_cooldown_remaining(state)
    if cooldown > 0:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] raw datadir artifact retry cooldown active for {cooldown}s\n")
        return True

    state["last_fast_artifact_attempt_epoch"] = int(time.time())
    state["last_fast_artifact_attempt_at"] = now_iso()
    state["last_fast_artifact_attempt_leader"] = leader
    state["last_fast_artifact_attempt_behind_blocks"] = behind_blocks
    env_values = read_env_file(POOL_ENV_FILE)
    candidate = select_rawdatadir_artifact_candidate(decision, state, env_values, log_path)
    if not candidate:
        return True

    state["last_fast_artifact_candidate"] = candidate
    append_incident(
        "sync_coordinator_fast_artifact_candidate",
        "warning",
        "sync-coordinator",
        f"raw datadir artifact candidate found for {leader}; fastest verified sync will override normal catch-up",
        {"leader": leader, "candidate": candidate, "decision": decision},
    )
    install_ok = install_rawdatadir_artifact(candidate, leader, env_values, log_path)
    state["last_fast_artifact_result"] = "installed" if install_ok else "install_failed"
    state["last_fast_artifact_finished_at"] = now_iso()
    state["last_fast_artifact_install_ok"] = install_ok
    if install_ok:
        state["fast_sync_accelerated_at"] = now_iso()
        state["fast_sync_accelerated_reason"] = "verified raw datadir artifact import"
        append_incident(
            "sync_coordinator_fast_artifact_installed",
            "warning",
            "sync-coordinator",
            f"installed verified raw datadir artifact for {leader}",
            {"leader": leader, "candidate": candidate},
        )
    else:
        append_incident(
            "sync_coordinator_fast_artifact_failed",
            "critical",
            "sync-coordinator",
            f"failed to install verified raw datadir artifact for {leader}; normal sync remains active and retry will continue",
            {"leader": leader, "candidate": candidate},
        )
    return install_ok


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
    artifact_ok = maybe_apply_fast_artifact_catchup(decision, state, log_path)
    resource_ok = apply_leader_catchup_resources(decision, state, log_path, record_incident=False)
    restart_ok = maybe_restart_leader_for_fast_sync(decision, state, log_path)
    return artifact_ok and resource_ok and restart_ok


def pause_follower(decision: dict[str, Any], state: dict[str, Any], log_path: Path) -> bool:
    target = str(decision.get("target") or "")
    leader = str(decision.get("leader") or "")
    if target not in NODES or leader not in NODES:
        return False
    safe_to_pause, unsafe_reason = pause_follower_safety(decision)
    if not safe_to_pause:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] {unsafe_reason}\n")
        state["last_pause_refused_at"] = now_iso()
        state["last_pause_refused_reason"] = unsafe_reason
        state["last_pause_refused_decision"] = decision
        append_incident(
            "sync_coordinator_pause_refused",
            "critical",
            "sync-coordinator",
            unsafe_reason,
            {"decision": decision},
        )
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
                "paused_follower_height": safe_int(((decision.get("nodes") or {}).get(target) or {}).get("height")),
                "paused_follower_remaining_blocks": safe_int(((decision.get("nodes") or {}).get(target) or {}).get("remaining_blocks")),
                "pause_leader_height": safe_int(((decision.get("nodes") or {}).get(leader) or {}).get("height")),
                "pause_leader_remaining_blocks": safe_int(((decision.get("nodes") or {}).get(leader) or {}).get("remaining_blocks")),
            }
        )
        apply_leader_catchup_resources(decision, state, log_path, record_incident=True)
        maybe_apply_fast_artifact_catchup(decision, state, log_path)
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
            resource_ok = apply_leader_catchup_resources(decision, state, log_path, record_incident=False)
            artifact_ok = maybe_apply_fast_artifact_catchup(decision, state, log_path)
            ok = resource_ok and artifact_ok
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
            resource_ok = apply_leader_catchup_resources(decision, state, log_path, record_incident=False)
            artifact_ok = maybe_apply_fast_artifact_catchup(decision, state, log_path)
            restart_ok = maybe_restart_leader_for_fast_sync(decision, state, log_path)
            ok = resource_ok and artifact_ok and restart_ok
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


def build_arg_parser() -> argparse.ArgumentParser:
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
    parser.add_argument(
        "--restart-lagging-follower",
        dest="accelerate_fastsync",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--interval", type=int, default=int(os.environ.get("BDAG_SYNC_COORDINATOR_INTERVAL", "120")))
    return parser


def parse_args(argv: list[str]) -> argparse.Namespace:
    return build_arg_parser().parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

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
