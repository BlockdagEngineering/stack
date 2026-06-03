#!/usr/bin/env python3
"""Write a shared atomic BlockDAG stack status sample for local agents."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import time
from typing import Any

from incident_journal import append_incident
from pool_ops import (
    EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS,
    LOG_DIR,
    POOL_ACTIVITY_BOOTSTRAP_LOG_LINES,
    POOL_CONTAINER,
    POOL_ENV_FILE,
    PROJECT_ROOT,
    STATUS_SAMPLER_FILE,
    collect_pool_activity,
    collect_status_cached,
    docker_compose_command,
    ensure_runtime,
    env_bool,
    now_iso,
    read_env_file_value,
    read_env_value,
    read_miner_registry,
    read_neighbor_macs,
    read_latest_earnings_snapshot_info,
    record_earnings_snapshot,
    run,
    save_miner_registry,
    split_env_list,
    upsert_pool_activity_miners,
    write_json_file,
    write_status_sampler_payload,
)


def env_float(name: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


DEFAULT_INTERVAL_SECONDS = env_float("BDAG_STATUS_SAMPLER_INTERVAL_SECONDS", 10.0, minimum=1.0)
DEFAULT_EARNINGS_SNAPSHOT_INTERVAL_SECONDS = env_float(
    "BDAG_STATUS_SAMPLER_EARNINGS_SNAPSHOT_INTERVAL_SECONDS",
    float(EARNINGS_SNAPSHOT_EXPECTED_INTERVAL_SECONDS),
    minimum=0.0,
)
MINING_IMPERATIVE_REPAIR_ENABLED = env_bool("BDAG_MINING_IMPERATIVE_REPAIR_ENABLED", True)
MINING_IMPERATIVE_REPAIR_INTERVAL_SECONDS = env_float(
    "BDAG_MINING_IMPERATIVE_REPAIR_INTERVAL_SECONDS",
    30.0,
    minimum=5.0,
)
MINING_IMPERATIVE_GUARD_UNITS = split_env_list(
    "BDAG_MINING_IMPERATIVE_GUARD_UNITS",
    "bdag-stack-sentinel.timer,bdag-watchdog.service",
)
MINING_IMPERATIVE_START_POOL_ENABLED = env_bool("BDAG_MINING_IMPERATIVE_START_POOL_ENABLED", True)
MINING_IMPERATIVE_START_IDLE_SYNCED_POOL = env_bool("BDAG_MINING_IMPERATIVE_START_IDLE_SYNCED_POOL", True)
MINING_IMPERATIVE_MINER_TRACKING_REPAIR_ENABLED = env_bool(
    "BDAG_MINING_IMPERATIVE_MINER_TRACKING_REPAIR_ENABLED",
    True,
)
MINING_IMPERATIVE_CONSTRAINED_FASTARTIFACT_REPAIR_ENABLED = env_bool(
    "BDAG_MINING_IMPERATIVE_CONSTRAINED_FASTARTIFACT_REPAIR_ENABLED",
    True,
)
MINING_IMPERATIVE_NODE_MINING_REPAIR_ENABLED = env_bool(
    "BDAG_MINING_IMPERATIVE_NODE_MINING_REPAIR_ENABLED",
    True,
)
MINING_IMPERATIVE_FASTSYNC_PEER_QUARANTINE_ENABLED = env_bool(
    "BDAG_MINING_IMPERATIVE_FASTSYNC_PEER_QUARANTINE_ENABLED",
    True,
)
CONSTRAINED_FASTARTIFACT_TOPOLOGIES = {
    item.lower()
    for item in split_env_list(
        "BDAG_CONSTRAINED_FASTARTIFACT_TOPOLOGIES",
        "asic-router",
    )
}
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
CONSTRAINED_FASTARTIFACT_STORAGE_PROFILES = {
    item.lower()
    for item in split_env_list(
        "BDAG_CONSTRAINED_FASTARTIFACT_STORAGE_PROFILES",
        "usb-chain-internal-runtime",
    )
}
FASTSYNC_PEER_QUARANTINE_ENV_KEYS = split_env_list(
    "BDAG_MINING_IMPERATIVE_FASTSYNC_PEER_QUARANTINE_ENV_KEYS",
    "NODE1_PEER_ADDRESSES,BDAG_FASTSYNC_PEERS,BOOTSTRAP_PEER_ADDRESSES",
)
NODE_MINING_REQUIRED_BOOL_FLAGS = (
    "--allowminingwhennearlysynced",
    "--allowsubmitwhennotsynced",
    "--miner",
)
NODE_MINING_CONSTRAINED_ASSIGNMENTS = {
    "--maxinbound": "1",
}
LOG_FILE = LOG_DIR / "status-sampler.log"


def log(message: str) -> None:
    ensure_runtime()
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message}\n")


def record_incident(
    event_type: str,
    severity: str,
    message: str,
    details: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    try:
        append_incident(
            event_type,
            severity,
            "status-sampler",
            message,
            details,
            status=payload,
            action=details,
        )
    except Exception as exc:  # noqa: BLE001 - repair must not fail because incident logging failed.
        log(f"mining imperative incident logging failed event={event_type} error={exc}")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def config_value(name: str, default: str = "") -> str:
    for path in (POOL_ENV_FILE, PROJECT_ROOT / ".env"):
        try:
            file_value = read_env_file_value(path, name)
        except OSError:
            file_value = None
        if file_value is not None:
            return file_value
    value = os.environ.get(name)
    if value is not None:
        return value
    return read_env_value(name) or default


def set_env_file_value(path: Any, key: str, value: str) -> bool:
    env_path = path if hasattr(path, "read_text") else PROJECT_ROOT / str(path)
    if not env_path.exists():
        return False
    lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    changed = False
    found = False
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        prefix = "export " if stripped.startswith("export ") else ""
        assignment = stripped[7:].strip() if prefix else stripped
        if assignment.startswith(f"{key}="):
            found = True
            replacement = f"{prefix}{key}={value}" if prefix else f"{key}={value}"
            output.append(replacement)
            changed = changed or line != replacement
        else:
            output.append(line)
    if not found:
        output.append(f"{key}={value}")
        changed = True
    if not changed:
        return False
    tmp = env_path.with_suffix(env_path.suffix + ".tmp")
    tmp.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.replace(tmp, env_path)
    return True


def set_runtime_env_value(key: str, value: str) -> list[str]:
    changed_paths: list[str] = []
    seen: set[Any] = set()
    for path in (PROJECT_ROOT / ".env", POOL_ENV_FILE):
        if path in seen:
            continue
        seen.add(path)
        if set_env_file_value(path, key, value):
            changed_paths.append(str(path))
    os.environ[key] = value
    return changed_paths


def env_enabled_value(value: str | None, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def configured_mining_address() -> str:
    for key in ("POOL_COINBASE_ADDRESS", "MINING_POOL_ADDRESS", "MINING_ADDRESS"):
        value = config_value(key).strip()
        if value:
            return value
    return ""


def valid_mining_address(address: str) -> bool:
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", address or "")) and address.lower() != ZERO_ADDRESS


def node_args_have_mining_address(args: str, address: str) -> bool:
    address_lower = address.lower()
    for word in args.replace("'", " ").replace('"', " ").split():
        if word.startswith("--miningaddr=") and word.split("=", 1)[1].lower() == address_lower:
            return True
    return False


def node_args_words(args: str) -> list[str]:
    return [word for word in args.replace("'", " ").replace('"', " ").split() if word]


def node_args_have_bool_flag(args: str, flag: str) -> bool:
    for word in node_args_words(args):
        if word == flag:
            return True
        if word.startswith(f"{flag}="):
            return word.split("=", 1)[1].strip().lower() not in {"0", "false", "no", "off"}
    return False


def node_args_assignment_value(args: str, flag: str) -> str | None:
    for word in node_args_words(args):
        if word.startswith(f"{flag}="):
            return word.split("=", 1)[1].strip()
    return None


def node_mining_runtime_args(address: str) -> str:
    parts = [
        *NODE_MINING_REQUIRED_BOOL_FLAGS,
        f"--miningaddr={address}",
    ]
    if constrained_fastartifact_profile():
        # A USB-backed ASIC router should mine and relay blocks, not serve as a
        # catch-up source for other peers while it is trying to convert shares
        # into accepted blocks. Keep one inbound slot because this node build
        # treats a zero inbound budget as an unusable P2P server.
        parts.extend(f"{key}={value}" for key, value in NODE_MINING_CONSTRAINED_ASSIGNMENTS.items())
    return " ".join(parts)


def node_mining_args_have_required_submit_guards(args: str, address: str) -> bool:
    if not node_args_have_mining_address(args, address):
        return False
    for flag in NODE_MINING_REQUIRED_BOOL_FLAGS:
        if not node_args_have_bool_flag(args, flag):
            return False
    if constrained_fastartifact_profile():
        for flag, wanted in NODE_MINING_CONSTRAINED_ASSIGNMENTS.items():
            if node_args_assignment_value(args, flag) != wanted:
                return False
    return True


def mining_imperative_enabled() -> bool:
    return MINING_IMPERATIVE_REPAIR_ENABLED and env_bool("BDAG_MINING_IMPERATIVE_REPAIR_ENABLED", True)


def systemctl_user(*args: str):
    return run(["systemctl", "--user", *args], timeout=30)


def ensure_user_unit(unit: str, payload: dict[str, Any]) -> bool:
    if not unit:
        return False
    enabled = systemctl_user("is-enabled", unit)
    active = systemctl_user("is-active", unit)
    enabled_text = enabled.stdout.strip()
    active_text = active.stdout.strip()
    if enabled.ok and enabled_text in {"enabled", "static", "generated", "linked"} and active.ok and active_text == "active":
        return False

    action = ["enable", "--now", unit] if not enabled.ok or enabled_text in {"", "disabled", "indirect"} else ["start", unit]
    result = systemctl_user(*action)
    details = {
        "unit": unit,
        "action": " ".join(action),
        "enabled_before": enabled_text,
        "active_before": active_text,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
    if result.ok:
        log(
            "mining imperative repaired user unit "
            f"unit={unit} action={' '.join(action)} enabled_before={enabled_text} active_before={active_text}"
        )
        record_incident(
            "mining_imperative_user_unit_repaired",
            "warning",
            f"Mining imperative guard repaired {unit}",
            details,
            payload,
        )
        return True
    log(f"mining imperative could not repair user unit unit={unit} rc={result.returncode} stderr={result.stderr.strip()}")
    record_incident(
        "mining_imperative_user_unit_repair_failed",
        "critical",
        f"Mining imperative guard could not repair {unit}",
        details,
        payload,
    )
    return False


def chain_ready_for_mining(payload: dict[str, Any]) -> bool:
    sync = dict_value(payload.get("sync_progress"))
    if str(sync.get("status") or "").lower() == "synced":
        return True
    remaining = sync.get("remaining_blocks")
    if remaining is not None and safe_int(remaining, 1) <= 0 and sync.get("chain_block_count") is not None:
        return True
    return payload.get("overall") == "ok" and not payload.get("sync_warnings")


def status_payload_has_miner_demand(payload: dict[str, Any]) -> bool:
    miner_health = dict_value(payload.get("miner_health"))
    if safe_int(miner_health.get("connected_count")) > 0 or safe_int(miner_health.get("managed_count")) > 0:
        return True

    pool = dict_value(payload.get("pool"))
    pool_metrics = dict_value(payload.get("pool_metrics")) or dict_value(pool.get("metrics"))
    if safe_float(pool_metrics.get("active_connections")) > 0:
        return True

    source_job_health = dict_value(pool.get("source_job_health")) or dict_value(pool_metrics.get("source_job_health"))
    return (
        safe_int(source_job_health.get("authorized_miners")) > 0
        or safe_int(source_job_health.get("ready_miners")) > 0
    )


def status_payload_has_tracking_gap(payload: dict[str, Any]) -> bool:
    if not MINING_IMPERATIVE_MINER_TRACKING_REPAIR_ENABLED:
        return False
    miner_health = dict_value(payload.get("miner_health"))
    if safe_int(miner_health.get("tracked_count")) > 0:
        return False
    return status_payload_has_miner_demand(payload) or asic_lan_neighbor_present()


def asic_lan_neighbor_present() -> bool:
    cidrs = split_env_list("BDAG_ASIC_LAN_CIDRS", "")
    if not cidrs:
        target = os.environ.get("BDAG_MINER_SCAN_TARGET", "")
        cidrs = [target] if "/" in target else []
    networks = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            log(f"mining imperative ignored invalid ASIC LAN CIDR {cidr!r}")
    if not networks:
        return False

    for ip_text, mac in read_neighbor_macs().items():
        if not mac:
            continue
        try:
            address = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if any(address in network for network in networks):
            return True
    return False


def constrained_fastartifact_profile() -> bool:
    topology = (config_value("BDAG_DETECTED_NETWORK_TOPOLOGY") or config_value("BDAG_NETWORK_TOPOLOGY")).strip().lower()
    storage_profile = config_value("BDAG_STORAGE_PROFILE").strip().lower()
    return bool(
        topology in CONSTRAINED_FASTARTIFACT_TOPOLOGIES
        or storage_profile in CONSTRAINED_FASTARTIFACT_STORAGE_PROFILES
    )


def node_services_for_recreate() -> list[str]:
    configured = config_value("BDAG_NODE_SERVICES", "node")
    services = [item for item in configured.replace(" ", ",").split(",") if item]
    return services or ["node"]


def node_command_line(node_service: str) -> str | None:
    result = run(
        docker_compose_command(
            "exec",
            "-T",
            node_service,
            "sh",
            "-lc",
            "ps -eo args | awk '/[b]dag/{print; exit}'",
        ),
        timeout=20,
    )
    if not result.ok:
        return None
    command_line = result.stdout.strip()
    return command_line or None


def node_command_has_fastartifact(node_service: str) -> bool:
    command_line = node_command_line(node_service)
    if not command_line:
        return False
    for word in command_line.split():
        if word == "--fastartifactsync":
            return True
        if word.startswith("--fastartifactsync="):
            return word.split("=", 1)[1].strip().lower() not in {"0", "false", "no", "off"}
    return False


def node_mining_template_support_should_repair(payload: dict[str, Any]) -> bool:
    if not MINING_IMPERATIVE_NODE_MINING_REPAIR_ENABLED:
        return False
    if not (status_payload_has_miner_demand(payload) or asic_lan_neighbor_present()):
        return False
    address = configured_mining_address()
    if not valid_mining_address(address):
        return False
    modules = {item.strip().lower() for item in config_value("BDAG_NODE_MODULES", "Blockdag").split(",")}
    args = config_value("BDAG_NODE_MINING_ARGS")
    if not env_enabled_value(config_value("BDAG_ENABLE_NODE_MINING"), False):
        return True
    if "miner" not in modules:
        return True
    if not node_mining_args_have_required_submit_guards(args, address):
        return True
    for service in node_services_for_recreate():
        command_line = node_command_line(service)
        if command_line and not node_mining_args_have_required_submit_guards(command_line, address):
            return True
    return False


def payload_node_tail_lines(payload: dict[str, Any]) -> list[str]:
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
    lines: list[str] = []
    for row in nodes.values():
        if not isinstance(row, dict):
            continue
        tail = row.get("tail") if isinstance(row.get("tail"), list) else []
        lines.extend(str(line) for line in tail)
    return lines


def fastsync_orphan_peer_ids(payload: dict[str, Any]) -> list[str]:
    peer_ids: list[str] = []
    pattern = re.compile(r"Fast-sync range returned only orphan blocks.*\bpeer=([A-Za-z0-9]+)")
    for line in payload_node_tail_lines(payload):
        match = pattern.search(line)
        if match and match.group(1) not in peer_ids:
            peer_ids.append(match.group(1))
    return peer_ids


def fastsync_peer_quarantine_should_repair(payload: dict[str, Any]) -> bool:
    if not MINING_IMPERATIVE_FASTSYNC_PEER_QUARANTINE_ENABLED:
        return False
    if not constrained_fastartifact_profile():
        return False
    if not chain_ready_for_mining(payload):
        return False
    if not (status_payload_has_miner_demand(payload) or asic_lan_neighbor_present()):
        return False
    return bool(fastsync_orphan_peer_ids(payload))


def constrained_fastartifact_should_repair(payload: dict[str, Any]) -> bool:
    if not MINING_IMPERATIVE_CONSTRAINED_FASTARTIFACT_REPAIR_ENABLED:
        return False
    if not constrained_fastartifact_profile():
        return False
    if not (status_payload_has_miner_demand(payload) or asic_lan_neighbor_present()):
        return False
    if env_enabled_value(config_value("BDAG_FASTARTIFACTSYNC_ENABLED"), True):
        return True
    return any(node_command_has_fastartifact(service) for service in node_services_for_recreate())


def recreate_node_services() -> tuple[bool, list[dict[str, Any]]]:
    node_results = []
    ok = True
    for service in node_services_for_recreate():
        result = run(
            docker_compose_command("up", "-d", "--no-deps", "--force-recreate", service),
            timeout=240,
        )
        node_results.append({"service": service, "returncode": result.returncode, "ok": result.ok})
        ok = ok and result.ok
    return ok, node_results


def remove_peer_ids_from_csv(value: str, peer_ids: list[str]) -> str:
    peers = [item.strip() for item in value.split(",") if item.strip()]
    if not peers or not peer_ids:
        return value
    kept = [peer for peer in peers if not any(peer_id in peer for peer_id in peer_ids)]
    return ",".join(kept)


def repair_missing_tracked_miners(payload: dict[str, Any]) -> bool:
    activity = collect_pool_activity(lines=POOL_ACTIVITY_BOOTSTRAP_LOG_LINES)
    registry = upsert_pool_activity_miners(activity)
    if not registry.get("miners"):
        hinted = read_miner_registry()
        if hinted.get("miners"):
            registry = save_miner_registry(hinted.get("miners", []))
    count = len(registry.get("miners") or [])
    action = {
        "tracked_count_after": count,
        "activity_miners": len(activity.get("miners") or []),
        "unattributed_valid_shares": activity.get("unattributed_valid_shares"),
        "unattributed_blocks": activity.get("unattributed_blocks"),
    }
    if count > 0:
        log(f"mining imperative repaired tracked-miner registry count={count}")
        record_incident(
            "mining_imperative_tracked_miners_repaired",
            "critical",
            "Mining imperative repaired missing tracked miners from LAN/pool evidence",
            action,
            payload,
        )
        return True
    log("mining imperative could not repair missing tracked miners")
    record_incident(
        "mining_imperative_tracked_miners_repair_failed",
        "critical",
        "Mining imperative could not repair missing tracked miners despite miner demand",
        action,
        payload,
    )
    return False


def repair_fastsync_orphan_peers(payload: dict[str, Any]) -> bool:
    peer_ids = fastsync_orphan_peer_ids(payload)
    changed_paths = []
    changed_keys = []
    for key in FASTSYNC_PEER_QUARANTINE_ENV_KEYS:
        current = config_value(key)
        updated = remove_peer_ids_from_csv(current, peer_ids)
        if updated != current:
            changed_paths.extend(set_runtime_env_value(key, updated))
            changed_keys.append(key)
    action = {
        "peer_ids": peer_ids,
        "changed_keys": changed_keys,
        "changed_env_paths": sorted(set(changed_paths)),
    }
    if not changed_keys:
        log(f"mining imperative found orphan FastSync peer(s) but no configured peer list matched: {','.join(peer_ids)}")
        record_incident(
            "mining_imperative_fastsync_peer_quarantine_no_match",
            "warning",
            "FastSync orphan peer observed but no configured peer list matched it",
            action,
            payload,
        )
        return False

    ok, node_results = recreate_node_services()
    action["node_recreate_results"] = node_results
    if ok:
        log(f"mining imperative quarantined orphan FastSync peer(s): {','.join(peer_ids)}")
        record_incident(
            "mining_imperative_fastsync_peer_quarantined",
            "critical",
            "Quarantined FastSync peer returning only orphan blocks on constrained mining host",
            action,
            payload,
        )
        return True
    log("mining imperative failed to recreate node after quarantining orphan FastSync peer(s)")
    record_incident(
        "mining_imperative_fastsync_peer_quarantine_failed",
        "critical",
        "Could not recreate node after quarantining FastSync orphan peer",
        action,
        payload,
    )
    return False


def repair_constrained_fastartifact(payload: dict[str, Any]) -> bool:
    changed_paths = set_runtime_env_value("BDAG_FASTARTIFACTSYNC_ENABLED", "0")
    changed_paths.extend(set_runtime_env_value("SYNC_SOURCE_NODE", "0"))
    changed_paths.extend(set_runtime_env_value("BDAG_NO_FASTSYNC_SERVE", "1"))
    changed_paths.extend(set_runtime_env_value("NODE_ARGS_APPEND", ""))
    ok, node_results = recreate_node_services()
    action = {
        "changed_env_paths": changed_paths,
        "node_recreate_results": node_results,
        "topology": config_value("BDAG_DETECTED_NETWORK_TOPOLOGY") or config_value("BDAG_NETWORK_TOPOLOGY"),
        "storage_profile": config_value("BDAG_STORAGE_PROFILE"),
    }
    if ok:
        log("mining imperative disabled FastArtifact during constrained synced mining profile")
        record_incident(
            "mining_imperative_constrained_fastartifact_disabled",
            "critical",
            "Disabled continuous FastArtifact mode for constrained synced ASIC-router mining",
            action,
            payload,
        )
        return True
    log("mining imperative failed to recreate node after disabling constrained FastArtifact mode")
    record_incident(
        "mining_imperative_constrained_fastartifact_repair_failed",
        "critical",
        "Could not recreate node after disabling constrained FastArtifact mode",
        action,
        payload,
    )
    return False


def repair_node_mining_template_support(payload: dict[str, Any]) -> bool:
    address = configured_mining_address()
    if not valid_mining_address(address):
        action = {"address_present": bool(address), "address_zero": address.lower() == ZERO_ADDRESS}
        log("mining imperative cannot enable node mining template support without a valid payout address")
        record_incident(
            "mining_imperative_node_mining_address_missing",
            "critical",
            "Cannot enable node mining template support without a valid non-zero payout address",
            action,
            payload,
        )
        return False

    changed_paths = []
    changed_paths.extend(set_runtime_env_value("BDAG_ENABLE_NODE_MINING", "1"))
    changed_paths.extend(set_runtime_env_value("BDAG_NODE_MODULES", "Blockdag,miner"))
    changed_paths.extend(
        set_runtime_env_value(
            "BDAG_NODE_MINING_ARGS",
            node_mining_runtime_args(address),
        )
    )
    ok, node_results = recreate_node_services()
    action = {
        "changed_env_paths": sorted(set(changed_paths)),
        "node_recreate_results": node_results,
        "mining_address_configured": True,
    }
    if ok:
        log("mining imperative enabled node miner/template support for attached ASIC demand")
        record_incident(
            "mining_imperative_node_mining_enabled",
            "critical",
            "Enabled node miner/template support because miner demand is present",
            action,
            payload,
        )
        return True
    log("mining imperative failed to recreate node after enabling miner/template support")
    record_incident(
        "mining_imperative_node_mining_enable_failed",
        "critical",
        "Could not recreate node after enabling miner/template support",
        action,
        payload,
    )
    return False


def pool_container_running(payload: dict[str, Any]) -> bool:
    containers = dict_value(payload.get("containers"))
    container = dict_value(containers.get(POOL_CONTAINER))
    return bool(container.get("running"))


def start_pool_container(payload: dict[str, Any], reason: str) -> bool:
    start = run(["docker", "start", POOL_CONTAINER], timeout=60)
    action = {
        "reason": reason,
        "container": POOL_CONTAINER,
        "method": "docker start",
        **start.as_dict(),
    }
    if start.ok:
        log(f"mining imperative started {POOL_CONTAINER}: {reason}")
        record_incident(
            "mining_imperative_started_pool",
            "critical",
            f"Mining imperative started {POOL_CONTAINER}: {reason}",
            action,
            payload,
        )
        return True

    compose = run(docker_compose_command("up", "-d", "--no-deps", POOL_CONTAINER), timeout=180)
    action = {
        "reason": reason,
        "container": POOL_CONTAINER,
        "method": "docker compose up --no-deps",
        "docker_start": start.as_dict(),
        "compose": compose.as_dict(),
    }
    if compose.ok:
        log(f"mining imperative recreated {POOL_CONTAINER} without dependencies: {reason}")
        record_incident(
            "mining_imperative_recreated_pool",
            "critical",
            f"Mining imperative recreated {POOL_CONTAINER}: {reason}",
            action,
            payload,
        )
        return True

    log(
        f"mining imperative could not start {POOL_CONTAINER}: {reason}; "
        f"docker_start_rc={start.returncode} compose_rc={compose.returncode}"
    )
    record_incident(
        "mining_imperative_pool_start_failed",
        "critical",
        f"Mining imperative could not start {POOL_CONTAINER}: {reason}",
        action,
        payload,
    )
    return False


def mining_imperative_repair(payload: dict[str, Any]) -> dict[str, Any]:
    if not mining_imperative_enabled():
        return {"enabled": False, "actions": []}

    actions: list[str] = []
    for unit in MINING_IMPERATIVE_GUARD_UNITS:
        if ensure_user_unit(unit, payload):
            actions.append(f"repaired_unit:{unit}")

    if status_payload_has_tracking_gap(payload):
        if repair_missing_tracked_miners(payload):
            actions.append("repaired_tracked_miners")

    if constrained_fastartifact_should_repair(payload):
        if repair_constrained_fastartifact(payload):
            actions.append("disabled_constrained_fastartifact")

    if node_mining_template_support_should_repair(payload):
        if repair_node_mining_template_support(payload):
            actions.append("enabled_node_mining_template_support")

    if fastsync_peer_quarantine_should_repair(payload):
        if repair_fastsync_orphan_peers(payload):
            actions.append("quarantined_fastsync_orphan_peer")

    if MINING_IMPERATIVE_START_POOL_ENABLED and not pool_container_running(payload):
        miner_demand = status_payload_has_miner_demand(payload)
        lan_candidate = asic_lan_neighbor_present()
        chain_ready = chain_ready_for_mining(payload)
        should_start = miner_demand or lan_candidate or (MINING_IMPERATIVE_START_IDLE_SYNCED_POOL and chain_ready)
        if should_start:
            reasons = []
            if miner_demand:
                reasons.append("miner demand is visible in status metrics")
            if lan_candidate:
                reasons.append("ASIC LAN neighbor is present")
            if chain_ready:
                reasons.append("chain is ready")
            if start_pool_container(payload, "; ".join(reasons) or "mining service is required"):
                actions.append(f"started_container:{POOL_CONTAINER}")
        else:
            log(
                f"mining imperative left {POOL_CONTAINER} stopped: "
                "no miner demand, no ASIC LAN neighbor, and chain is not ready"
            )

    return {"enabled": True, "actions": actions}


def write_error_state(error: Exception) -> None:
    write_json_file(
        STATUS_SAMPLER_FILE,
        {
            "schema_version": 1,
            "updated_at": now_iso(),
            "epoch": time.time(),
            "status": "failed",
            "error": str(error),
        },
        mode=0o600,
    )


def sample_once(include_logs: bool) -> dict[str, Any]:
    # max_age_seconds=0 is the explicit hard-bypass path: do not read either
    # the shared sampler file or the short shared cache while producing a sample.
    payload = collect_status_cached(include_logs=include_logs, max_age_seconds=0)
    write_status_sampler_payload(payload, include_logs=include_logs)
    log(
        "sampled "
        f"overall={payload.get('overall')} mode={payload.get('mode')} "
        f"fresh={payload.get('fresh')} include_logs={include_logs}"
    )
    return payload


def maybe_record_earnings_snapshot(
    now_epoch: float,
    last_attempt_epoch: float,
    interval_seconds: float,
    enabled: bool,
) -> float:
    if not enabled or interval_seconds <= 0:
        return last_attempt_epoch
    if last_attempt_epoch and now_epoch - last_attempt_epoch < interval_seconds:
        return last_attempt_epoch

    info = read_latest_earnings_snapshot_info()
    latest_epoch = info.get("latest_epoch")
    try:
        latest_age = now_epoch - float(latest_epoch) if latest_epoch is not None else None
    except (TypeError, ValueError):
        latest_age = None
    if latest_age is not None and latest_age < interval_seconds:
        return last_attempt_epoch

    try:
        snapshot = record_earnings_snapshot()
    except Exception as exc:  # noqa: BLE001 - status sampling must not die on plot history failures.
        log(f"earnings snapshot failed: {exc}")
        return now_epoch
    miners = snapshot.get("miner_estimates")
    miner_count = len(miners) if isinstance(miners, list) else 0
    log(f"earnings snapshot recorded generated_at={snapshot.get('generated_at')} miners={miner_count}")
    return now_epoch


def run_loop(interval_seconds: float, include_logs: bool, earnings_snapshot_interval_seconds: float, record_earnings: bool) -> int:
    ensure_runtime()
    last_earnings_attempt_epoch = 0.0
    last_mining_repair_epoch = 0.0
    while True:
        started = time.time()
        try:
            payload = sample_once(include_logs=include_logs)
            now_epoch = time.time()
            if now_epoch - last_mining_repair_epoch >= MINING_IMPERATIVE_REPAIR_INTERVAL_SECONDS:
                repair = mining_imperative_repair(payload)
                if repair.get("actions"):
                    log(f"mining imperative repair actions={','.join(repair['actions'])}")
                last_mining_repair_epoch = now_epoch
            last_earnings_attempt_epoch = maybe_record_earnings_snapshot(
                time.time(),
                last_earnings_attempt_epoch,
                earnings_snapshot_interval_seconds,
                record_earnings,
            )
        except Exception as exc:  # noqa: BLE001 - sampler must keep trying.
            log(f"sample failed: {exc}")
            try:
                write_error_state(exc)
            except Exception as write_exc:  # noqa: BLE001
                log(f"failed to write error state: {write_exc}")
        elapsed = time.time() - started
        time.sleep(max(1.0, interval_seconds - elapsed))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="keep sampling until the service is stopped")
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument(
        "--earnings-snapshot-interval-seconds",
        type=float,
        default=DEFAULT_EARNINGS_SNAPSHOT_INTERVAL_SECONDS,
        help="append miner/earnings plot snapshots when the valid history is older than this interval; 0 disables",
    )
    parser.add_argument(
        "--no-earnings-snapshots",
        action="store_true",
        help="do not append miner/earnings plot snapshots from the status sampler",
    )
    parser.add_argument("--no-logs", action="store_true", help="omit container log tails from each sample")
    parser.add_argument("--json", action="store_true", help="print the sampled payload")
    args = parser.parse_args()

    include_logs = not args.no_logs
    if args.loop:
        return run_loop(
            max(1.0, args.interval_seconds),
            include_logs,
            max(0.0, args.earnings_snapshot_interval_seconds),
            not args.no_earnings_snapshots,
        )
    try:
        payload = sample_once(include_logs=include_logs)
    except Exception as exc:  # noqa: BLE001
        log(f"sample failed: {exc}")
        write_error_state(exc)
        raise
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
