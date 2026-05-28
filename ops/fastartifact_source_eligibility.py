#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("BDAG_PROJECT_ROOT", Path(__file__).resolve().parents[1])).resolve()
ENV_FILE = Path(os.environ.get("BDAG_ENV_FILE", ROOT / ".env"))
STATUS_FILE = Path(
    os.environ.get(
        "BDAG_RAWDATADIR_SOURCE_STATUS",
        ROOT / "ops" / "runtime" / "rawdatadir-source-status.json",
    )
)
UNSAFE_FSTYPES = {
    "vfat",
    "exfat",
    "ntfs",
    "ntfs3",
    "fuseblk",
    "fuse",
    "nfs",
    "nfs4",
    "cifs",
    "smb3",
    "tmpfs",
    "ramfs",
}


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            env.setdefault(key, value)
    return env


def split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]


def as_int(value: str | None, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def as_float(value: str | None, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def resolve_path(raw: str | Path) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def nearest_existing(path: Path) -> Path:
    cur = path
    while not cur.exists() and cur != cur.parent:
        cur = cur.parent
    return cur


def run_json(args: list[str]) -> Any | None:
    try:
        result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=3)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def mount_info(path: Path) -> dict[str, Any]:
    probe = nearest_existing(path)
    payload = run_json(["findmnt", "-J", "-T", str(probe), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"])
    filesystems = payload.get("filesystems") if isinstance(payload, dict) else None
    if isinstance(filesystems, list) and filesystems:
        return dict(filesystems[0])
    return {"target": "", "source": "", "fstype": "", "options": ""}


def disk_name_for_source(source: str) -> str:
    if not source.startswith("/dev/"):
        return ""
    try:
        result = subprocess.run(
            ["lsblk", "-no", "PKNAME", source],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        parent = result.stdout.strip().splitlines()
        if parent and parent[0].strip():
            return parent[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(source).name


def block_device_facts(source: str) -> dict[str, Any]:
    disk = disk_name_for_source(source)
    facts: dict[str, Any] = {"disk": disk, "transport": "", "removable": None, "hotplug": None, "rotational": None}
    if not disk:
        return facts
    try:
        result = subprocess.run(
            ["lsblk", "-dnJ", "-o", "NAME,TRAN,RM,HOTPLUG,ROTA", f"/dev/{disk}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        payload = json.loads(result.stdout or "{}")
        devices = payload.get("blockdevices") or []
        if devices:
            dev = devices[0]
            facts["transport"] = str(dev.get("tran") or "")
            facts["removable"] = bool(dev.get("rm")) if dev.get("rm") is not None else None
            facts["hotplug"] = bool(dev.get("hotplug")) if dev.get("hotplug") is not None else None
            facts["rotational"] = bool(dev.get("rota")) if dev.get("rota") is not None else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    removable_path = Path("/sys/class/block") / disk / "removable"
    if removable_path.exists():
        try:
            facts["removable"] = removable_path.read_text(encoding="utf-8").strip() == "1"
        except OSError:
            pass
    try:
        device_realpath = str((Path("/sys/class/block") / disk / "device").resolve())
        facts["sysfs_device"] = device_realpath
        if "usb" in device_realpath.lower():
            facts["transport"] = facts["transport"] or "usb"
    except OSError:
        pass
    return facts


def classify_path(name: str, path: Path) -> dict[str, Any]:
    mount = mount_info(path)
    source = str(mount.get("source") or "")
    fstype = str(mount.get("fstype") or "")
    facts = block_device_facts(source)
    path_text = str(path)
    transport = str(facts.get("transport") or "").lower()
    reasons: list[str] = []
    if transport == "usb" or facts.get("removable") or facts.get("hotplug"):
        reasons.append("usb_or_removable")
    if fstype.lower() in UNSAFE_FSTYPES or fstype.lower().startswith("fuse."):
        reasons.append(f"unsafe_fstype:{fstype}")
    if path_text.startswith(("/media/", "/run/media/")):
        reasons.append("removable_mount_path")
    if source and not source.startswith("/dev/"):
        reasons.append(f"non_block_source:{source}")
    return {
        "name": name,
        "path": str(path),
        "mount": mount,
        "device": facts,
        "unsafe": bool(reasons),
        "unsafe_reasons": reasons,
    }


def dir_size_bytes(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        result = subprocess.run(
            ["du", "-sb", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return int(result.stdout.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def total_memory_bytes() -> int | None:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return None
    for line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2:
                return int(parts[1]) * 1024
    return None


def active_node_service(env: dict[str, str]) -> str:
    services = split_csv(env.get("BDAG_NODE_SERVICES", "bdag-miner-node-1"))
    return services[0] if services else "bdag-miner-node-1"


def node_data_dir(env: dict[str, str], service: str) -> Path:
    if service.endswith("node-1") or service == "node1":
        return resolve_path(env.get("BDAG_NODE1_DATA_DIR", "./data/node1"))
    if service.endswith("node-2") or service == "node2":
        return resolve_path(env.get("BDAG_NODE2_DATA_DIR", "./data/node2"))
    return resolve_path(env.get("BDAG_NODE_DATA_DIR", env.get("BDAG_DATA_DIR", "./data/node")))


def build_payload(full: bool) -> dict[str, Any]:
    env = load_env()
    network = env.get("BDAG_RAWDATADIR_NETWORK") or env.get("BDAG_FASTSNAP_NETWORK") or "mainnet"
    service = active_node_service(env)
    data_dir = node_data_dir(env, service)
    source_dir = resolve_path(env.get("BDAG_RAWDATADIR_SIDECAR_SOURCE", data_dir / network))
    sidecar_dir = resolve_path(
        env.get("BDAG_RAWDATADIR_SIDECAR_DIR", ROOT / "data-restore" / "rawdatadir-sidecar" / network)
    )
    artifact_base = resolve_path(env.get("BDAG_RAWDATADIR_ARTIFACT_BASE", ROOT / "data-restore" / "rawdatadir"))
    tmp_dir = resolve_path(env.get("BDAG_RAWDATADIR_TMPDIR", artifact_base / "tmp"))
    mode = (env.get("BDAG_RAWDATADIR_SOURCE_MODE") or env.get("BDAG_FASTARTIFACT_SOURCE_MODE") or "auto").lower()
    node_mode = (env.get("BDAG_NODE_MODE") or "single").lower()

    paths = [
        classify_path("active_node_datadir", data_dir),
        classify_path("source_datadir", source_dir),
        classify_path("sidecar_dir", sidecar_dir),
        classify_path("artifact_base", artifact_base),
        classify_path("tmp_dir", tmp_dir),
        classify_path("docker_root", Path("/var/lib/docker")),
    ]
    reasons: list[str] = []
    if mode in {"0", "false", "no", "off", "disabled"}:
        reasons.append("source_mode_disabled")
    if os.name != "posix":
        reasons.append("unsupported_os")
    for item in paths:
        if item["unsafe"]:
            reasons.append(f"{item['name']}:{','.join(item['unsafe_reasons'])}")

    min_ram_gib = as_float(env.get("BDAG_RAWDATADIR_MIN_RAM_GIB"), 8.0)
    memory = total_memory_bytes()
    if memory is not None and memory < min_ram_gib * 1024**3:
        reasons.append(f"insufficient_ram:{memory / 1024**3:.1f}GiB<{min_ram_gib:.1f}GiB")
    min_cpu = as_int(env.get("BDAG_RAWDATADIR_MIN_CPU_COUNT"), 4)
    cpu_count = os.cpu_count() or 1
    if cpu_count < min_cpu:
        reasons.append(f"insufficient_cpu:{cpu_count}<{min_cpu}")

    usage = shutil.disk_usage(nearest_existing(artifact_base))
    source_size = dir_size_bytes(source_dir) if full else None
    min_free_gib = as_float(env.get("BDAG_RAWDATADIR_MIN_FREE_GIB"), 100.0)
    multiplier = as_float(env.get("BDAG_RAWDATADIR_FREE_SPACE_MULTIPLIER"), 2.5)
    required_free = int(min_free_gib * 1024**3)
    if source_size is not None:
        required_free = max(required_free, int(source_size * multiplier))
    if usage.free < required_free:
        reasons.append(
            f"insufficient_disk:{usage.free / 1024**3:.1f}GiB<{required_free / 1024**3:.1f}GiB"
        )

    publish_mode = (env.get("BDAG_RAWDATADIR_PUBLISH_MODE") or "finalized-sidecar").lower()
    finalization = (env.get("BDAG_RAWDATADIR_SINGLE_NODE_FINALIZE") or "0").lower() in {"1", "true", "yes", "on"}
    publish_requires_finalization = node_mode == "single" and publish_mode == "finalized-sidecar" and not finalization

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "project_root": str(ROOT),
        "mode": mode,
        "node_mode": node_mode,
        "active_node_service": service,
        "network": network,
        "eligible": not reasons,
        "reasons": reasons,
        "publish_allowed": not reasons and not publish_requires_finalization,
        "publish_requires_finalization": publish_requires_finalization,
        "paths": paths,
        "source_size_bytes": source_size,
        "artifact_free_bytes": usage.free,
        "artifact_required_free_bytes": required_free,
        "cpu_count": cpu_count,
        "memory_bytes": memory,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--full", action="store_true", help="include a du -sb source-size check")
    parser.add_argument("--status-file", default=str(STATUS_FILE))
    args = parser.parse_args()

    payload = build_payload(full=args.full)
    status_file = Path(args.status_file)
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["eligible"] else 2


if __name__ == "__main__":
    sys.exit(main())
