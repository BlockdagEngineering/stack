#!/usr/bin/env python3
"""Resolve BlockDAG appliance capabilities into safe runtime recommendations.

The stack runs on small flash-backed appliances, USB SSD hosts, and larger
NVMe/desktop systems. Architecture alone is not enough to choose cache, sync,
and maintenance settings; the active chain storage and network topology matter
more for accepted block production.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


MIB = 1024**2
GIB = 1024**3


def run(command: list[str], timeout: float = 3.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def split_env_list(value: str | None) -> list[str]:
    return [item.strip() for item in re.split(r"[,;]", value or "") if item.strip()]


def normalize_os_name(raw: str | None = None) -> str:
    value = (raw or platform.system() or "unknown").strip().lower()
    if value in {"darwin", "mac", "macos"}:
        return "darwin"
    if value.startswith("win"):
        return "windows"
    if value in {"linux", "windows", "darwin"}:
        return value
    return value or "unknown"


def normalize_arch_name(raw: str | None = None) -> str:
    value = (raw or platform.machine() or "unknown").strip().lower()
    if value in {"x86_64", "amd64"}:
        return "amd64"
    if value in {"aarch64", "arm64"}:
        return "arm64"
    if value.startswith("armv7"):
        return "armv7"
    return value or "unknown"


def memory_total_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def hardware_model() -> str:
    for candidate in (
        Path("/proc/device-tree/model"),
        Path("/sys/firmware/devicetree/base/model"),
        Path("/sys/devices/virtual/dmi/id/product_name"),
    ):
        try:
            text = candidate.read_text(encoding="utf-8").replace("\x00", "").strip()
            if text:
                return text
        except OSError:
            continue
    return ""


def host_class(os_name: str, cpu_count: int, memory_bytes: int, model: str) -> str:
    model_lower = model.lower()
    if os_name == "linux" and "raspberry pi 5" in model_lower:
        return "pi5"
    if cpu_count <= 4 or (memory_bytes and memory_bytes <= 6 * GIB):
        return "constrained"
    if cpu_count <= 8 or (memory_bytes and memory_bytes <= 16 * GIB):
        return "standard"
    return "large"


def existing_path_for_usage(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def mount_info(path: Path) -> dict[str, str]:
    anchor = existing_path_for_usage(path)
    try:
        proc = run(["findmnt", "-J", "-T", str(anchor), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"], timeout=2)
    except (OSError, subprocess.SubprocessError):
        return {"path": str(path), "anchor": str(anchor)}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"path": str(path), "anchor": str(anchor), "stderr": proc.stderr.strip()}
    try:
        filesystems = json.loads(proc.stdout).get("filesystems", [])
    except json.JSONDecodeError:
        return {"path": str(path), "anchor": str(anchor), "stdout": proc.stdout.strip()}
    if not filesystems:
        return {"path": str(path), "anchor": str(anchor)}
    item = filesystems[0]
    return {
        "path": str(path),
        "anchor": str(anchor),
        "target": str(item.get("target") or ""),
        "source": str(item.get("source") or ""),
        "fstype": str(item.get("fstype") or ""),
        "options": str(item.get("options") or ""),
    }


def block_device_for_source(source: str) -> str:
    if not source.startswith("/dev/"):
        return ""
    try:
        proc = run(["lsblk", "-no", "PKNAME", source], timeout=2)
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc and proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().splitlines()[0]
    name = Path(source).name
    if name.startswith("nvme") or name.startswith("mmcblk"):
        return re.sub(r"p[0-9]+$", "", name)
    return re.sub(r"[0-9]+$", "", name)


def read_sysfs_value(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def lsblk_device_details(device: str) -> dict[str, str]:
    if not device:
        return {}
    try:
        proc = run(["lsblk", "-dn", "-o", "NAME,MODEL,TRAN,RM,ROTA,SIZE", f"/dev/{device}"], timeout=2)
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    parts = proc.stdout.strip().split()
    if not parts:
        return {}
    # MODEL may contain spaces, so read stable sysfs values separately below.
    return {"raw": proc.stdout.strip()}


def device_facts(device: str) -> dict[str, Any]:
    if not device:
        return {}
    sys_block = Path("/sys/block") / device
    facts: dict[str, Any] = {"device": device, **lsblk_device_details(device)}
    facts["rotational"] = read_sysfs_value(sys_block / "queue" / "rotational")
    facts["removable"] = read_sysfs_value(sys_block / "removable")
    facts["model"] = read_sysfs_value(sys_block / "device" / "model")
    facts["vendor"] = read_sysfs_value(sys_block / "device" / "vendor")
    facts["discard_max_bytes"] = read_sysfs_value(sys_block / "queue" / "discard_max_bytes")
    facts["nr_requests"] = read_sysfs_value(sys_block / "queue" / "nr_requests")
    facts["read_ahead_kb"] = read_sysfs_value(sys_block / "queue" / "read_ahead_kb")
    device_path = ""
    try:
        device_path = str((sys_block / "device").resolve())
    except OSError:
        pass
    facts["transport"] = "usb" if "/usb" in device_path else ""
    if device.startswith("nvme"):
        facts["transport"] = "nvme"
    elif device.startswith("mmcblk"):
        facts["transport"] = "mmc"
    return facts


def classify_storage(device: str, fstype: str, facts: dict[str, Any]) -> str:
    transport = str(facts.get("transport") or "").lower()
    removable = str(facts.get("removable") or "")
    rotational = str(facts.get("rotational") or "")
    if device.startswith("mmcblk") or transport == "mmc":
        return "sd-card"
    if device.startswith("nvme") or transport == "nvme":
        return "nvme-ssd"
    if transport == "usb":
        if rotational == "1":
            return "usb-hdd"
        if removable == "1":
            return "usb-removable-flash"
        return "usb-ssd"
    if rotational == "1":
        return "hdd"
    if fstype.lower() in {"f2fs"}:
        return "flash"
    if device:
        return "ssd"
    return "unknown"


@dataclass
class StoragePath:
    path: str
    mount: dict[str, str]
    device: str
    facts: dict[str, Any]
    storage_class: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "mount": self.mount,
            "device": self.device,
            "facts": self.facts,
            "storage_class": self.storage_class,
        }


@dataclass
class HostFacts:
    os_name: str
    arch: str
    cpu_count: int
    memory_bytes: int
    host_profile: str
    hardware_model: str
    topology: str
    node_mode: str
    chain_paths: list[StoragePath] = field(default_factory=list)
    project_path: str = ""
    docker_root: str = ""

    @property
    def memory_gib(self) -> float:
        return round(self.memory_bytes / GIB, 2) if self.memory_bytes else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "os": self.os_name,
            "arch": self.arch,
            "cpu_count": self.cpu_count,
            "memory_bytes": self.memory_bytes,
            "memory_gib": self.memory_gib,
            "host_profile": self.host_profile,
            "hardware_model": self.hardware_model,
            "topology": self.topology,
            "node_mode": self.node_mode,
            "project_path": self.project_path,
            "docker_root": self.docker_root,
            "chain_paths": [item.as_dict() for item in self.chain_paths],
        }


def env_data_dir(root: Path, env: dict[str, str]) -> Path:
    raw = env.get("BDAG_DATA_DIR") or env.get("DATA_DIR") or "data"
    path = Path(raw)
    return path if path.is_absolute() else root / path


def default_chain_paths(root: Path, env: dict[str, str]) -> list[Path]:
    explicit = split_env_list(env.get("BDAG_CHAIN_DATA_PATHS"))
    if explicit:
        return [(Path(item) if Path(item).is_absolute() else root / item) for item in explicit]
    data_dir = env_data_dir(root, env)
    return [data_dir / "node1", data_dir / "node2", data_dir / "postgres"]


def docker_root_dir() -> str:
    try:
        proc = run(["docker", "info", "--format", "{{.DockerRootDir}}"], timeout=3)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def detect_network_topology(env: dict[str, str]) -> str:
    configured = (env.get("BDAG_DETECTED_NETWORK_TOPOLOGY") or env.get("BDAG_NETWORK_TOPOLOGY") or "").strip().lower()
    if configured and configured != "auto":
        return configured
    lan_iface = (env.get("BDAG_ASIC_LAN_INTERFACE") or "eth0").strip() or "eth0"
    lan_cidrs = split_env_list(env.get("BDAG_ASIC_LAN_CIDRS") or "192.168.50.0/24")
    try:
        route = run(["ip", "-o", "-4", "route", "get", "1.1.1.1"], timeout=2).stdout
        addr = run(["ip", "-br", "-4", "addr", "show", "dev", lan_iface], timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return "auto"
    route_dev = ""
    route_parts = route.split()
    for idx, part in enumerate(route_parts[:-1]):
        if part == "dev":
            route_dev = route_parts[idx + 1]
            break
    has_lan_addr = any(cidr.split("/", 1)[0].rsplit(".", 1)[0] in addr for cidr in lan_cidrs)
    if lan_iface and has_lan_addr and route_dev and route_dev != lan_iface:
        return "single-node-asic-router"
    return "standard"


def detect_host_facts(root: Path, env: dict[str, str]) -> HostFacts:
    os_name = normalize_os_name()
    arch = normalize_arch_name()
    cpu_count = max(1, os.cpu_count() or 1)
    memory_bytes = memory_total_bytes()
    model = hardware_model()
    host_profile_override = (env.get("BDAG_HOST_PROFILE") or "auto").strip().lower()
    profile = host_profile_override if host_profile_override not in {"", "auto"} else host_class(os_name, cpu_count, memory_bytes, model)
    chain_paths: list[StoragePath] = []
    for path in default_chain_paths(root, env):
        mount = mount_info(path)
        device = block_device_for_source(mount.get("source", ""))
        facts = device_facts(device)
        storage_class = classify_storage(device, mount.get("fstype", ""), facts)
        chain_paths.append(StoragePath(str(path), mount, device, facts, storage_class))
    return HostFacts(
        os_name=os_name,
        arch=arch,
        cpu_count=cpu_count,
        memory_bytes=memory_bytes,
        host_profile=profile,
        hardware_model=model,
        topology=detect_network_topology(env),
        node_mode=(env.get("BDAG_NODE_MODE") or "single").strip().lower() or "single",
        chain_paths=chain_paths,
        project_path=str(root),
        docker_root=docker_root_dir(),
    )


def round_down_mb(value: float, quantum: int = 256) -> int:
    return max(quantum, int(value) // quantum * quantum)


def round_nearest_mb(value: float, quantum: int = 256) -> int:
    return max(quantum, int((value + quantum / 2) // quantum) * quantum)


def clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def memory_mb(facts: HostFacts) -> int:
    return int(facts.memory_bytes / MIB) if facts.memory_bytes else 0


def capability_profile_name(facts: HostFacts, env: dict[str, str] | None = None) -> str:
    override = ((env or {}).get("BDAG_CAPABILITY_PROFILE") or "auto").strip().lower()
    if override not in {"", "auto"}:
        return override
    classes = {item.storage_class for item in facts.chain_paths}
    fragile = bool(classes & {"sd-card", "usb-removable-flash", "flash"})
    usb = any(item.storage_class.startswith("usb-") for item in facts.chain_paths)
    if facts.topology == "single-node-asic-router" and (usb or fragile):
        return "pi5-usb-asic-router" if facts.host_profile == "pi5" else "usb-asic-router"
    if fragile:
        return "fragile-flash"
    if "usb-ssd" in classes:
        return "usb-ssd"
    if "nvme-ssd" in classes:
        return "nvme-dual-node" if facts.node_mode in {"double", "dual", "2"} else "nvme-single-node"
    if facts.host_profile == "large":
        return "large-ssd"
    if facts.host_profile == "constrained":
        return "constrained"
    return "standard"


def recommended_cache_settings(profile: str, facts: HostFacts) -> dict[str, str]:
    mem = memory_mb(facts)
    dual = facts.node_mode in {"double", "dual", "2"}
    if mem <= 0:
        mem = 8192 if facts.host_profile in {"pi5", "standard"} else 4096

    if profile in {"pi5-usb-asic-router", "usb-asic-router"}:
        per_node_cache = round_nearest_mb(mem * (0.375 if not dual else 0.22), 512)
        per_node_cache = clamp_int(per_node_cache, 1536, 6144)
        evm_cache = per_node_cache
        db_percent = 50
        evm_db_percent = 85
    elif profile in {"fragile-flash", "constrained"}:
        per_node_cache = round_nearest_mb(mem * (0.25 if not dual else 0.16), 256)
        per_node_cache = clamp_int(per_node_cache, 1024, 4096)
        evm_cache = per_node_cache
        db_percent = 60
        evm_db_percent = 80
    elif profile == "usb-ssd":
        per_node_cache = round_nearest_mb(mem * (0.32 if not dual else 0.20), 512)
        per_node_cache = clamp_int(per_node_cache, 2048, 6144)
        evm_cache = per_node_cache
        db_percent = 60
        evm_db_percent = 85
    elif profile.startswith("nvme") or profile == "large-ssd":
        per_node_cache = round_nearest_mb(mem * (0.34 if not dual else 0.22), 512)
        per_node_cache = clamp_int(per_node_cache, 4096, 12288)
        evm_cache = per_node_cache
        db_percent = 65
        evm_db_percent = 85
    else:
        per_node_cache = round_nearest_mb(mem * (0.28 if not dual else 0.18), 512)
        per_node_cache = clamp_int(per_node_cache, 2048, 6144)
        evm_cache = per_node_cache
        db_percent = 60
        evm_db_percent = 80

    node_count = 2 if dual else 1
    reserve_mb = max(2048, int(mem * 0.18))
    gomem_mb = max(2048, (mem - reserve_mb) // node_count)
    gomem_mb = min(gomem_mb, int(mem * (0.86 if not dual else 0.43)))
    gomem_mb = round_down_mb(gomem_mb, 256)
    pool_gomem_mb = clamp_int(round_down_mb(mem * 0.05, 128), 512, 2048)

    bd_dag_cache = 16384 if mem >= 24 * 1024 else 8192
    return {
        "BDAG_NODE_CACHE_MB": str(per_node_cache),
        "BDAG_NODE_CACHE_DATABASE_PERCENT": str(db_percent),
        "BDAG_NODE_CACHE_SNAPSHOT_PERCENT": "35",
        "BDAG_NODE_BD_CACHE_SIZE": str(bd_dag_cache),
        "BDAG_NODE_DAG_CACHE_SIZE": str(bd_dag_cache),
        "BDAG_EVM_CACHE_MB": str(evm_cache),
        "BDAG_EVM_CACHE_DATABASE_PERCENT": str(evm_db_percent),
        "BDAG_EVM_CACHE_SNAPSHOT_PERCENT": "1",
        "BDAG_NODE_GOMEMLIMIT": f"{gomem_mb}MiB",
        "BDAG_NODE_GOGC": "100",
        "BDAG_POOL_GOMEMLIMIT": f"{pool_gomem_mb}MiB",
    }


def recommended_io_settings(profile: str, facts: HostFacts) -> dict[str, str]:
    if profile in {"pi5-usb-asic-router", "usb-asic-router", "fragile-flash", "constrained"}:
        dirty_background = 64 * MIB
        dirty = 256 * MIB
        read_ahead = 256
        nr_requests = 128
        fstrim = "1"
    elif profile == "usb-ssd":
        dirty_background = 128 * MIB
        dirty = 512 * MIB
        read_ahead = 512
        nr_requests = 256
        fstrim = "1"
    else:
        dirty_background = 256 * MIB
        dirty = 1024 * MIB
        read_ahead = 1024
        nr_requests = 512
        fstrim = "1"
    chain_paths = ",".join(item.path for item in facts.chain_paths)
    return {
        "BDAG_CHAIN_DATA_PATHS": chain_paths,
        "BDAG_BLOCK_READ_AHEAD_KB": str(read_ahead),
        "BDAG_BLOCK_NR_REQUESTS": str(nr_requests),
        "BDAG_VM_TUNING_ENABLED": "1",
        "BDAG_VM_SWAPPINESS": "10",
        "BDAG_VM_VFS_CACHE_PRESSURE": "50",
        "BDAG_VM_DIRTY_BACKGROUND_BYTES": str(dirty_background),
        "BDAG_VM_DIRTY_BYTES": str(dirty),
        "BDAG_FSTRIM_ENABLED": fstrim,
    }


def recommended_sync_settings(profile: str, facts: HostFacts) -> dict[str, str]:
    if profile in {"pi5-usb-asic-router", "usb-asic-router"}:
        return {
            "NODE_MAX_PEERS": "96",
            "BDAG_FASTSYNC_PREPROCESS_WORKERS": "1",
            "BDAG_FASTSNAP_PARALLELISM": "4",
            "BDAG_NO_FASTSYNC_SERVE": "1",
            "BDAG_FASTARTIFACTSYNC_ENABLED": "1",
            "BDAG_BACKGROUND_MAINTENANCE_BACKOFF_ENABLED": "1",
            "BDAG_STATUS_SAMPLER_ENABLED": "1",
            "BDAG_ADAPTIVE_CONCURRENCY_ENABLED": "1",
        }
    if profile in {"fragile-flash", "constrained"}:
        return {
            "NODE_MAX_PEERS": "96",
            "BDAG_FASTSYNC_PREPROCESS_WORKERS": "1",
            "BDAG_FASTSNAP_PARALLELISM": "2",
            "BDAG_NO_FASTSYNC_SERVE": "auto",
            "BDAG_FASTARTIFACTSYNC_ENABLED": "1",
            "BDAG_BACKGROUND_MAINTENANCE_BACKOFF_ENABLED": "1",
            "BDAG_STATUS_SAMPLER_ENABLED": "1",
            "BDAG_ADAPTIVE_CONCURRENCY_ENABLED": "1",
        }
    if profile == "usb-ssd":
        return {
            "NODE_MAX_PEERS": "160",
            "BDAG_FASTSYNC_PREPROCESS_WORKERS": "2",
            "BDAG_FASTSNAP_PARALLELISM": "4",
            "BDAG_NO_FASTSYNC_SERVE": "auto",
            "BDAG_FASTARTIFACTSYNC_ENABLED": "1",
            "BDAG_BACKGROUND_MAINTENANCE_BACKOFF_ENABLED": "1",
            "BDAG_STATUS_SAMPLER_ENABLED": "1",
            "BDAG_ADAPTIVE_CONCURRENCY_ENABLED": "1",
        }
    return {
        "NODE_MAX_PEERS": "256",
        "BDAG_FASTSYNC_PREPROCESS_WORKERS": "4",
        "BDAG_FASTSNAP_PARALLELISM": "8",
        "BDAG_NO_FASTSYNC_SERVE": "auto",
        "BDAG_FASTARTIFACTSYNC_ENABLED": "1",
        "BDAG_BACKGROUND_MAINTENANCE_BACKOFF_ENABLED": "1",
        "BDAG_STATUS_SAMPLER_ENABLED": "1",
        "BDAG_ADAPTIVE_CONCURRENCY_ENABLED": "1",
    }


def recommended_postgres_settings(profile: str, facts: HostFacts) -> dict[str, str]:
    mem = memory_mb(facts)
    if profile in {"pi5-usb-asic-router", "usb-asic-router", "usb-ssd"} and mem >= 12 * 1024:
        return {
            "POSTGRES_SHARED_BUFFERS": "512MB",
            "POSTGRES_EFFECTIVE_CACHE_SIZE": "4GB",
            "POSTGRES_MAX_WAL_SIZE": "1GB",
            "POSTGRES_CHECKPOINT_TIMEOUT": "15min",
        }
    if profile in {"fragile-flash", "constrained"} or mem <= 8 * 1024:
        return {
            "POSTGRES_SHARED_BUFFERS": "256MB",
            "POSTGRES_EFFECTIVE_CACHE_SIZE": "1GB",
            "POSTGRES_MAX_WAL_SIZE": "512MB",
            "POSTGRES_CHECKPOINT_TIMEOUT": "15min",
        }
    return {
        "POSTGRES_SHARED_BUFFERS": "1GB",
        "POSTGRES_EFFECTIVE_CACHE_SIZE": "8GB",
        "POSTGRES_MAX_WAL_SIZE": "2GB",
        "POSTGRES_CHECKPOINT_TIMEOUT": "15min",
    }


def recommended_pool_optimizer_settings(profile: str, facts: HostFacts) -> dict[str, str]:
    if profile in {"pi5-usb-asic-router", "usb-asic-router"}:
        return {
            "BDAG_POOL_OPTIMIZER_WINDOW_SECONDS": "900",
            "BDAG_POOL_OPTIMIZER_SAMPLE_INTERVAL_SECONDS": "30",
            "BDAG_POOL_OPTIMIZER_CHANGE_COOLDOWN_SECONDS": "1800",
            "BDAG_POOL_OPTIMIZER_SAFE_TEMPLATE_TTL_MS": "750",
            "BDAG_POOL_OPTIMIZER_SAFE_BLOCK_CANDIDATE_JOB_AGE_MS": "1000",
            "BDAG_POOL_OPTIMIZER_SAFE_VARDIFF_TARGET_SHARE_SECONDS": "4.0",
            "BDAG_POOL_OPTIMIZER_SAFE_VARDIFF_WINDOW_SECONDS": "120",
            "BDAG_POOL_OPTIMIZER_TIMER_ON_BOOT_SEC": "12m",
            "BDAG_POOL_OPTIMIZER_TIMER_ON_UNIT_ACTIVE_SEC": "30m",
            "BDAG_POOL_OPTIMIZER_TIMER_RANDOMIZED_DELAY_SEC": "3m",
            "BDAG_POOL_OPTIMIZER_RUNNER_INTERVAL_SECONDS": "1800",
        }
    if profile in {"fragile-flash", "constrained"}:
        return {
            "BDAG_POOL_OPTIMIZER_WINDOW_SECONDS": "1200",
            "BDAG_POOL_OPTIMIZER_SAMPLE_INTERVAL_SECONDS": "40",
            "BDAG_POOL_OPTIMIZER_CHANGE_COOLDOWN_SECONDS": "2400",
            "BDAG_POOL_OPTIMIZER_SAFE_TEMPLATE_TTL_MS": "750",
            "BDAG_POOL_OPTIMIZER_SAFE_BLOCK_CANDIDATE_JOB_AGE_MS": "1000",
            "BDAG_POOL_OPTIMIZER_SAFE_VARDIFF_TARGET_SHARE_SECONDS": "4.0",
            "BDAG_POOL_OPTIMIZER_SAFE_VARDIFF_WINDOW_SECONDS": "150",
            "BDAG_POOL_OPTIMIZER_TIMER_ON_BOOT_SEC": "15m",
            "BDAG_POOL_OPTIMIZER_TIMER_ON_UNIT_ACTIVE_SEC": "45m",
            "BDAG_POOL_OPTIMIZER_TIMER_RANDOMIZED_DELAY_SEC": "5m",
            "BDAG_POOL_OPTIMIZER_RUNNER_INTERVAL_SECONDS": "2700",
        }
    if profile == "usb-ssd":
        return {
            "BDAG_POOL_OPTIMIZER_WINDOW_SECONDS": "900",
            "BDAG_POOL_OPTIMIZER_SAMPLE_INTERVAL_SECONDS": "30",
            "BDAG_POOL_OPTIMIZER_CHANGE_COOLDOWN_SECONDS": "1800",
            "BDAG_POOL_OPTIMIZER_SAFE_TEMPLATE_TTL_MS": "500",
            "BDAG_POOL_OPTIMIZER_SAFE_BLOCK_CANDIDATE_JOB_AGE_MS": "900",
            "BDAG_POOL_OPTIMIZER_SAFE_VARDIFF_TARGET_SHARE_SECONDS": "3.0",
            "BDAG_POOL_OPTIMIZER_SAFE_VARDIFF_WINDOW_SECONDS": "120",
            "BDAG_POOL_OPTIMIZER_TIMER_ON_BOOT_SEC": "10m",
            "BDAG_POOL_OPTIMIZER_TIMER_ON_UNIT_ACTIVE_SEC": "30m",
            "BDAG_POOL_OPTIMIZER_TIMER_RANDOMIZED_DELAY_SEC": "2m",
            "BDAG_POOL_OPTIMIZER_RUNNER_INTERVAL_SECONDS": "1800",
        }
    if profile.startswith("nvme") or profile == "large-ssd":
        return {
            "BDAG_POOL_OPTIMIZER_WINDOW_SECONDS": "600",
            "BDAG_POOL_OPTIMIZER_SAMPLE_INTERVAL_SECONDS": "20",
            "BDAG_POOL_OPTIMIZER_CHANGE_COOLDOWN_SECONDS": "1200",
            "BDAG_POOL_OPTIMIZER_SAFE_TEMPLATE_TTL_MS": "500",
            "BDAG_POOL_OPTIMIZER_SAFE_BLOCK_CANDIDATE_JOB_AGE_MS": "800",
            "BDAG_POOL_OPTIMIZER_SAFE_VARDIFF_TARGET_SHARE_SECONDS": "2.5",
            "BDAG_POOL_OPTIMIZER_SAFE_VARDIFF_WINDOW_SECONDS": "90",
            "BDAG_POOL_OPTIMIZER_TIMER_ON_BOOT_SEC": "5m",
            "BDAG_POOL_OPTIMIZER_TIMER_ON_UNIT_ACTIVE_SEC": "20m",
            "BDAG_POOL_OPTIMIZER_TIMER_RANDOMIZED_DELAY_SEC": "90s",
            "BDAG_POOL_OPTIMIZER_RUNNER_INTERVAL_SECONDS": "1200",
        }
    return {
        "BDAG_POOL_OPTIMIZER_WINDOW_SECONDS": "900",
        "BDAG_POOL_OPTIMIZER_SAMPLE_INTERVAL_SECONDS": "30",
        "BDAG_POOL_OPTIMIZER_CHANGE_COOLDOWN_SECONDS": "1800",
        "BDAG_POOL_OPTIMIZER_SAFE_TEMPLATE_TTL_MS": "500",
        "BDAG_POOL_OPTIMIZER_SAFE_BLOCK_CANDIDATE_JOB_AGE_MS": "800",
        "BDAG_POOL_OPTIMIZER_SAFE_VARDIFF_TARGET_SHARE_SECONDS": "3.0",
        "BDAG_POOL_OPTIMIZER_SAFE_VARDIFF_WINDOW_SECONDS": "120",
        "BDAG_POOL_OPTIMIZER_TIMER_ON_BOOT_SEC": "10m",
        "BDAG_POOL_OPTIMIZER_TIMER_ON_UNIT_ACTIVE_SEC": "30m",
        "BDAG_POOL_OPTIMIZER_TIMER_RANDOMIZED_DELAY_SEC": "2m",
        "BDAG_POOL_OPTIMIZER_RUNNER_INTERVAL_SECONDS": "1800",
    }


def recommendations(profile: str, facts: HostFacts) -> dict[str, str]:
    result: dict[str, str] = {
        "BDAG_CAPABILITY_PROFILE": profile,
        "BDAG_HOST_PROFILE": facts.host_profile,
        "BDAG_POOL_OPTIMIZER_ITERATIONS": "1",
    }
    result.update(recommended_cache_settings(profile, facts))
    result.update(recommended_io_settings(profile, facts))
    result.update(recommended_sync_settings(profile, facts))
    result.update(recommended_postgres_settings(profile, facts))
    result.update(recommended_pool_optimizer_settings(profile, facts))
    return result


def resolve(root: Path, env: dict[str, str]) -> dict[str, Any]:
    facts = detect_host_facts(root, env)
    profile = capability_profile_name(facts, env)
    recs = recommendations(profile, facts)
    return {
        "capability_profile": profile,
        "host_facts": facts.as_dict(),
        "recommendations": recs,
        "notes": [
            "Cache recommendations reserve RAM for the Linux page cache; they do not try to pin the whole chain in process heap.",
            "USB-backed ASIC-router miners consume FastSync but do not serve bulk sync from the mining chain device.",
            "Pool adaptive optimizer timing is platform-adaptive; constrained flash hosts sample slowly while NVMe hosts can use shorter windows.",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve BlockDAG stack capability profile.")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--json", action="store_true", help="Print JSON payload. This is the default unless --env is used.")
    parser.add_argument("--env", action="store_true", help="Print recommended KEY=VALUE lines.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    env_file = Path(args.env_file) if args.env_file else root / ".env"
    env = os.environ.copy()
    env.update(load_env_file(env_file))
    payload = resolve(root, env)
    if args.env:
        for key, value in sorted(payload["recommendations"].items()):
            print(f"{key}={value}")
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
