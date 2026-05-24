#!/usr/bin/env python3
"""Guarded helper for BlockDAG observer-node peer-diversity experiments."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(os.environ.get("BDAG_PROJECT_ROOT", Path(__file__).resolve().parents[1]))
RUNTIME_DIR = PROJECT_ROOT / "ops" / "runtime"
REPORT_ROOT = RUNTIME_DIR / "observer-node-experiments"
ENV_FILE = PROJECT_ROOT / "asic-pool" / ".env"
COMPOSE_FILES = ["docker-compose.yml", "docker-compose.observers.yml"]
LIVE_SERVICES = ["asic-pool", "bdag-miner-node-1", "bdag-miner-node-2", "rpc-failover", "pool-db"]
LIVE_NODE_SERVICES = ["bdag-miner-node-1", "bdag-miner-node-2"]
MAINTENANCE_UNITS = [
    "bdag-hourly-snapshot.service",
    "bdag-chain-presync.service",
    "bdag-sync-coordinator.service",
]
REQUIRED_GUARDS = [
    "bdag-watchdog.service",
    "bdag-p2p-guard.service",
    "bdag-dashboard.service",
]
STATUS_URL = "http://127.0.0.1:8088/api/status"
LATEST_SNAPSHOT = PROJECT_ROOT / "data-restore" / "latest-hourly"
OBSERVER_SEED_BWLIMIT_KB = int(os.environ.get("BDAG_OBSERVER_SEED_BWLIMIT_KB", "4000"))


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def stamp() -> str:
    return dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def run(cmd: list[str], *, timeout: int = 60, cwd: Path = PROJECT_ROOT, env: dict[str, str] | None = None) -> dict[str, Any]:
    started = time.time()
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "ok": proc.returncode == 0,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "elapsed_seconds": round(time.time() - started, 3),
        "at": now_iso(),
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def fetch_status(timeout: float = 15.0) -> tuple[dict[str, Any], str]:
    try:
        request = urllib.request.Request(STATUS_URL, headers={"accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")), ""
    except Exception as exc:  # noqa: BLE001 - report status failure as a gate failure.
        return {}, str(exc)


def systemctl_user_is_active(unit: str) -> str:
    result = run(["systemctl", "--user", "is-active", unit], timeout=10, cwd=Path.home())
    return (result["stdout"] or result["stderr"] or "unknown").strip() or "unknown"


def disk_summary() -> dict[str, Any]:
    usage = shutil.disk_usage(PROJECT_ROOT)
    total_gb = usage.total / (1024**3)
    used_gb = (usage.total - usage.free) / (1024**3)
    free_gb = usage.free / (1024**3)
    return {
        "path": str(PROJECT_ROOT),
        "total_gb": round(total_gb, 2),
        "used_gb": round(used_gb, 2),
        "free_gb": round(free_gb, 2),
        "used_percent": round((used_gb / total_gb) * 100, 2) if total_gb else None,
    }


def observer_container_name(node_index: int) -> str:
    return f"bdag-observer-node-{node_index}"


def observer_data_dir(node_index: int) -> Path:
    return PROJECT_ROOT / "data" / f"node{node_index}"


def docker_container_exists(name: str) -> bool:
    result = run(["docker", "inspect", name], timeout=10)
    return bool(result["ok"])


def observer_running(node_index: int) -> bool:
    name = observer_container_name(node_index)
    result = run(["docker", "inspect", name, "--format", "{{.State.Running}}"], timeout=10)
    return result["ok"] and result["stdout"].strip() == "true"


def low_priority_command(command: list[str]) -> list[str]:
    wrapped = list(command)
    if shutil.which("ionice"):
        wrapped = ["ionice", "-c3", *wrapped]
    if shutil.which("nice"):
        wrapped = ["nice", "-n", "19", *wrapped]
    return wrapped


def seed_rsync_command(source: Path, destination: Path) -> list[str]:
    command = [
        "rsync",
        "-a",
        "--delete",
        "--no-owner",
        "--no-group",
        "--chmod=Du+rwx,Dgo+rx,Fu+rw,Fgo+r",
        f"--bwlimit={OBSERVER_SEED_BWLIMIT_KB}",
        "--exclude=/mainnet/network.key",
        "--exclude=/mainnet/peerstore/",
        "--exclude=/mainnet/bdageth/nodekey",
        "--exclude=/mainnet/keystore/",
        f"{source}/",
        f"{destination}/",
    ]
    return low_priority_command(command)


def parse_peer_value(output: str, key: str) -> str:
    for line in output.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def generated_peer_value(node_count: int, node_index: int, *, include_known_local: bool = False) -> tuple[str, dict[str, Any]]:
    key = f"NODE{node_index}_PEER_ADDRESSES"
    command = [
        sys.executable,
        "ops/multinode_peer_sets.py",
        "--nodes",
        str(node_count),
        "--only-node",
        str(node_index),
        "--print-values",
    ]
    if include_known_local:
        command.append("--include-known-local")
    result = run(command, timeout=20)
    return parse_peer_value(result["stdout"], key), result


def evaluate_gates(args: argparse.Namespace) -> dict[str, Any]:
    status, status_error = fetch_status()
    disk = disk_summary()
    maintenance = {unit: systemctl_user_is_active(unit) for unit in MAINTENANCE_UNITS}
    guards = {unit: systemctl_user_is_active(unit) for unit in REQUIRED_GUARDS}
    observer_name = observer_container_name(args.node_index)
    data_dir = observer_data_dir(args.node_index)
    gate_failures: list[str] = []

    if status_error:
        gate_failures.append(f"dashboard status unavailable: {status_error}")
    if status.get("overall") != "ok":
        gate_failures.append(f"stack overall is {status.get('overall')!r}: {status.get('status_reason') or ''}")
    connected = int(((status.get("miner_health") or {}).get("connected_count") or 0) if status else 0)
    if connected < args.min_miners:
        gate_failures.append(f"connected miners {connected} < required {args.min_miners}")
    if ((status.get("sync_progress") or {}).get("status") if status else None) != "synced":
        gate_failures.append(f"sync status is {((status.get('sync_progress') or {}).get('status'))!r}")

    pool_health = status.get("pool_health") or {}
    for key in ("block_submit_zero_success_storm", "accepted_job_expired_storm", "share_stall", "job_stall"):
        if pool_health.get(key):
            gate_failures.append(f"pool health gate failed: {key}=true")

    active_maintenance = [unit for unit, state in maintenance.items() if state == "active" or state == "activating"]
    if active_maintenance and not args.allow_maintenance:
        gate_failures.append(f"maintenance active: {', '.join(active_maintenance)}")

    inactive_guards = [unit for unit, state in guards.items() if state != "active"]
    if inactive_guards:
        gate_failures.append(f"required guard inactive: {', '.join(inactive_guards)}")

    if disk["free_gb"] < args.min_free_gb:
        gate_failures.append(f"free disk {disk['free_gb']}GB < required {args.min_free_gb}GB")
    if disk["used_percent"] is not None and disk["used_percent"] > args.max_used_percent:
        gate_failures.append(f"disk used {disk['used_percent']}% > limit {args.max_used_percent}%")

    if args.require_empty_data and data_dir.exists() and any(data_dir.iterdir()):
        gate_failures.append(f"{data_dir} already exists and is not empty")
    if docker_container_exists(observer_name) and not args.allow_existing_container:
        gate_failures.append(f"{observer_name} already exists")

    return {
        "generated_at": now_iso(),
        "node_index": args.node_index,
        "observer_container": observer_name,
        "observer_data_dir": str(data_dir),
        "gates_ok": not gate_failures,
        "gate_failures": gate_failures,
        "status_error": status_error,
        "status": {
            "overall": status.get("overall"),
            "status_reason": status.get("status_reason"),
            "connected_miners": connected,
            "sync_status": (status.get("sync_progress") or {}).get("status"),
            "last_valid_share_age_seconds": pool_health.get("last_valid_share_age_seconds"),
            "last_block_submit_age_seconds": pool_health.get("last_block_submit_age_seconds"),
            "block_submit_error_count": pool_health.get("block_submit_error_count"),
        },
        "disk": disk,
        "maintenance_units": maintenance,
        "required_guards": guards,
    }


def command_status(args: argparse.Namespace) -> int:
    payload = evaluate_gates(args)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["gates_ok"] else 1


def capture_command(path: Path, cmd: list[str], *, timeout: int = 60, env: dict[str, str] | None = None) -> dict[str, Any]:
    result = run(cmd, timeout=timeout, env=env)
    path.write_text(
        f"$ {' '.join(cmd)}\n\nreturncode={result['returncode']}\n\nSTDOUT:\n{result['stdout']}\n\nSTDERR:\n{result['stderr']}\n",
        encoding="utf-8",
    )
    return result


def create_savepoint(args: argparse.Namespace, *, gates: dict[str, Any], peer_value: str, peer_result: dict[str, Any]) -> Path:
    run_dir = REPORT_ROOT / f"node{args.node_index}-observer-{stamp()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "gates.json", gates)
    write_json(run_dir / "peer-generation.json", peer_result)
    (run_dir / f"NODE{args.node_index}_PEER_ADDRESSES.txt").write_text(peer_value + "\n", encoding="utf-8")
    for rel in ("docker-compose.yml", "docker-compose.observers.yml", "haproxy.cfg", ".env", "asic-pool/.env"):
        src = PROJECT_ROOT / rel
        if src.exists():
            dst = run_dir / rel.replace("/", "__")
            shutil.copy2(src, dst)

    status, status_error = fetch_status()
    write_json(run_dir / "status.before.json", {"status_error": status_error, "status": status})
    capture_command(run_dir / "docker-compose-ps.before.txt", ["docker", "compose", "ps"], timeout=30)
    capture_command(run_dir / "live-containers.before.txt", ["docker", "inspect", *LIVE_SERVICES], timeout=30)
    capture_command(run_dir / "disk.before.txt", ["df", "-h", str(PROJECT_ROOT)], timeout=20)
    capture_command(
        run_dir / "du.before.txt",
        ["du", "-sh", "data/node1", "data/node2", f"data/node{args.node_index}", "data-restore/hourly"],
        timeout=120,
    )
    rollback = f"""#!/usr/bin/env bash
set -Eeuo pipefail
cd {PROJECT_ROOT}
docker compose --env-file asic-pool/.env -f docker-compose.yml -f docker-compose.observers.yml --profile observer-nodes stop {observer_container_name(args.node_index)} || true
docker compose --env-file asic-pool/.env -f docker-compose.yml -f docker-compose.observers.yml --profile observer-nodes rm -f {observer_container_name(args.node_index)} || true
echo "Observer container removed. Data directory left intact for inspection: {observer_data_dir(args.node_index)}"
"""
    rollback_path = run_dir / "rollback-observer-container.sh"
    rollback_path.write_text(rollback, encoding="utf-8")
    rollback_path.chmod(0o755)
    return run_dir


def current_managed_node_image() -> str:
    for service in LIVE_NODE_SERVICES:
        result = run(["docker", "inspect", service, "--format", "{{.Config.Image}}"], timeout=20)
        image = result["stdout"].strip()
        if result["ok"] and image:
            return image
    return ""


def command_savepoint(args: argparse.Namespace) -> int:
    gates = evaluate_gates(args)
    peer_value, peer_result = generated_peer_value(args.nodes, args.node_index, include_known_local=args.include_known_local)
    run_dir = create_savepoint(args, gates=gates, peer_value=peer_value, peer_result=peer_result)
    print(json.dumps({"run_dir": str(run_dir), "gates_ok": gates["gates_ok"], "gate_failures": gates["gate_failures"]}, indent=2))
    return 0 if gates["gates_ok"] else 1


def command_plan_start(args: argparse.Namespace) -> int:
    gates = evaluate_gates(args)
    peer_value, peer_result = generated_peer_value(args.nodes, args.node_index, include_known_local=args.include_known_local)
    run_dir = create_savepoint(args, gates=gates, peer_value=peer_value, peer_result=peer_result)
    image_key = f"BLOCKDAG_NODE{args.node_index}_IMAGE"
    image_value = os.environ.get(image_key) or os.environ.get("BLOCKDAG_NODE_IMAGE") or current_managed_node_image()
    env_pairs = [f"NODE{args.node_index}_PEER_ADDRESSES={peer_value}"]
    if image_value:
        env_pairs.append(f"{image_key}={image_value}")
    docker_command = [
        "docker",
        "compose",
        "--env-file",
        "asic-pool/.env",
        "-f",
        "docker-compose.yml",
        "-f",
        "docker-compose.observers.yml",
        "--profile",
        "observer-nodes",
        "up",
        "-d",
        "--no-deps",
        observer_container_name(args.node_index),
    ]
    command = [
        "env",
        *env_pairs,
        *docker_command,
    ]
    payload = {
        "run_dir": str(run_dir),
        "gates_ok": gates["gates_ok"],
        "gate_failures": gates["gate_failures"],
        "observer_image": image_value,
        "start_command": command,
        "rollback_script": str(run_dir / "rollback-observer-container.sh"),
        "executed": False,
    }
    if args.execute:
        if not gates["gates_ok"] and not args.force:
            payload["error"] = "refusing to execute because gates failed; use --force only after a deliberate safety decision"
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 2
        env = dict(os.environ)
        env[f"NODE{args.node_index}_PEER_ADDRESSES"] = peer_value
        if image_value:
            env[image_key] = image_value
        result = run(docker_command, timeout=120, env=env)
        write_json(run_dir / "start-result.json", result)
        payload["executed"] = True
        payload["start_result"] = {"returncode": result["returncode"], "ok": result["ok"]}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if gates["gates_ok"] else 1


def remove_node_identity(data_dir: Path) -> list[str]:
    removed: list[str] = []
    for rel in (
        "mainnet/network.key",
        "mainnet/bdageth/nodekey",
        "mainnet/keystore",
        "mainnet/peerstore",
    ):
        path = data_dir / rel
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(str(path))
        elif path.exists():
            path.unlink()
            removed.append(str(path))
    return removed


def command_seed(args: argparse.Namespace) -> int:
    data_dir = observer_data_dir(args.node_index)
    gates = evaluate_gates(args)
    latest_exists = LATEST_SNAPSHOT.exists()
    if not latest_exists:
        gates["gates_ok"] = False
        gates["gate_failures"].append(f"latest restore snapshot is unavailable or broken: {LATEST_SNAPSHOT}")
    if args.require_empty_data and data_dir.exists() and any(data_dir.iterdir()):
        gates["gates_ok"] = False
        gates["gate_failures"].append(f"{data_dir} already exists and is not empty")

    run_dir = REPORT_ROOT / f"node{args.node_index}-seed-{stamp()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "gates.json", gates)
    payload: dict[str, Any] = {
        "run_dir": str(run_dir),
        "latest_snapshot": str(LATEST_SNAPSHOT.resolve()) if latest_exists else str(LATEST_SNAPSHOT),
        "data_dir": str(data_dir),
        "gates_ok": gates["gates_ok"],
        "gate_failures": gates["gate_failures"],
        "executed": False,
    }
    if not args.execute:
        payload["seed_command"] = seed_rsync_command(LATEST_SNAPSHOT, data_dir)
        payload["seed_bwlimit_kb"] = OBSERVER_SEED_BWLIMIT_KB
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if gates["gates_ok"] else 1

    if not gates["gates_ok"] and not args.force:
        payload["error"] = "refusing to seed because gates failed; use --force only after a deliberate safety decision"
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2

    data_dir.mkdir(parents=True, exist_ok=True)
    command = seed_rsync_command(LATEST_SNAPSHOT, data_dir)
    result = run(command, timeout=args.timeout_seconds)
    removed = remove_node_identity(data_dir) if result["ok"] else []
    payload["executed"] = True
    payload["seed_bwlimit_kb"] = OBSERVER_SEED_BWLIMIT_KB
    payload["seed_result"] = {"returncode": result["returncode"], "ok": result["ok"], "elapsed_seconds": result["elapsed_seconds"]}
    payload["removed_identity_paths"] = removed
    write_json(run_dir / "seed-result.json", {**payload, "command_result": result})
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


def command_stop(args: argparse.Namespace) -> int:
    name = observer_container_name(args.node_index)
    stop = run(
        [
            "docker",
            "compose",
            "--env-file",
            "asic-pool/.env",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.observers.yml",
            "--profile",
            "observer-nodes",
            "stop",
            name,
        ],
        timeout=60,
    )
    remove = run(
        [
            "docker",
            "compose",
            "--env-file",
            "asic-pool/.env",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.observers.yml",
            "--profile",
            "observer-nodes",
            "rm",
            "-f",
            name,
        ],
        timeout=60,
    )
    print(json.dumps({"stop": stop, "remove": remove, "data_dir_left": str(observer_data_dir(args.node_index))}, indent=2, sort_keys=True))
    return 0 if stop["ok"] and remove["ok"] else 1


def add_gate_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--node-index", type=int, choices=(3, 4), default=3)
    parser.add_argument("--nodes", type=int, choices=(3, 4), default=3)
    parser.add_argument("--min-miners", type=int, default=6)
    parser.add_argument("--min-free-gb", type=float, default=100.0)
    parser.add_argument("--max-used-percent", type=float, default=80.0)
    parser.add_argument("--allow-maintenance", action="store_true")
    parser.add_argument("--allow-existing-container", action="store_true")
    parser.add_argument("--require-empty-data", action="store_true", default=True)
    parser.add_argument(
        "--allow-existing-data",
        action="store_false",
        dest="require_empty_data",
        help="allow rsync to resume an existing observer data directory",
    )
    parser.add_argument(
        "--include-known-local",
        action="store_true",
        help="include known local node peers as observer bootstrap peers",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="evaluate safety gates")
    add_gate_args(status)
    status.set_defaults(func=command_status)

    savepoint = sub.add_parser("savepoint", help="create a rollback/evidence savepoint without starting an observer")
    add_gate_args(savepoint)
    savepoint.set_defaults(func=command_savepoint)

    plan_start = sub.add_parser("plan-start", help="create savepoint and print guarded start command; use --execute to run")
    add_gate_args(plan_start)
    plan_start.add_argument("--execute", action="store_true")
    plan_start.add_argument("--force", action="store_true")
    plan_start.set_defaults(func=command_plan_start)

    seed = sub.add_parser("seed", help="seed an observer datadir from the latest restore point")
    add_gate_args(seed)
    seed.add_argument("--execute", action="store_true")
    seed.add_argument("--force", action="store_true")
    seed.add_argument("--timeout-seconds", type=int, default=3600)
    seed.set_defaults(func=command_seed)

    stop = sub.add_parser("stop", help="stop and remove an observer container, leaving data for inspection")
    stop.add_argument("--node-index", type=int, choices=(3, 4), default=3)
    stop.set_defaults(func=command_stop)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
