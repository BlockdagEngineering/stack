#!/usr/bin/env python3
"""Read-only preflight checks for constrained BlockDAG mining appliances."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


GIB = 1024**3
ZERO_ETH_ADDRESS = "0x0000000000000000000000000000000000000000"
FLASH_UNFRIENDLY_FS = {"exfat", "vfat", "ntfs", "fuseblk"}
RAM_BACKED_FS = {"tmpfs", "ramfs"}
CHAIN_DB_MARKERS = ("BdagChain", "Blockdag", "chaindata", "mainnet")


@dataclass
class Check:
    name: str
    status: str
    detail: str
    mitigation: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "mitigation": self.mitigation,
            "evidence": self.evidence,
        }


@dataclass
class HostProfile:
    os_name: str
    arch: str
    cpu_count: int
    memory_bytes: int
    profile: str
    kernel: str
    model: str = ""

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
            "profile": self.profile,
            "kernel": self.kernel,
            "model": self.model,
        }


def run(command: list[str], timeout: float = 5.0, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
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


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def bool_enabled(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def node_args_enable_fastartifact(value: str | None) -> bool:
    for word in (value or "").split():
        if word == "--fastartifactsync":
            return True
        if word.startswith("--fastartifactsync="):
            return word.split("=", 1)[1].strip().lower() not in {"0", "false", "no", "off"}
    return False


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


def detect_host_profile() -> HostProfile:
    os_name = platform.system().lower() or "unknown"
    arch = platform.machine().lower() or "unknown"
    cpu_count = max(1, os.cpu_count() or 1)
    memory_bytes = memory_total_bytes()
    model = hardware_model()
    model_lower = model.lower()
    if os_name == "linux" and "raspberry pi 5" in model_lower:
        profile = "pi5"
    elif cpu_count <= 4 or (memory_bytes and memory_bytes <= 6 * GIB):
        profile = "constrained"
    elif cpu_count <= 8 or (memory_bytes and memory_bytes <= 16 * GIB):
        profile = "standard"
    else:
        profile = "large"
    return HostProfile(os_name, arch, cpu_count, memory_bytes, profile, platform.release(), model)


def existing_path_for_usage(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def disk_usage(path: Path) -> dict[str, Any]:
    anchor = existing_path_for_usage(path)
    usage = shutil.disk_usage(anchor)
    return {
        "path": str(path),
        "anchor": str(anchor),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "free_gib": round(usage.free / GIB, 2),
        "used_percent": round(usage.used * 100.0 / usage.total, 1) if usage.total else None,
    }


def mount_info(path: Path) -> dict[str, Any]:
    anchor = existing_path_for_usage(path)
    proc = run(["findmnt", "-J", "-T", str(anchor), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"], timeout=4)
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            filesystems = json.loads(proc.stdout).get("filesystems", [])
            if filesystems:
                item = filesystems[0]
                return {
                    "target": item.get("target", ""),
                    "source": item.get("source", ""),
                    "fstype": item.get("fstype", ""),
                    "options": item.get("options", ""),
                }
        except json.JSONDecodeError:
            pass
    return {"target": "", "source": "", "fstype": "", "options": ""}


def same_filesystem(left: Path, right: Path) -> bool:
    try:
        return existing_path_for_usage(left).stat().st_dev == existing_path_for_usage(right).stat().st_dev
    except OSError:
        return False


def clean_mount_source(source: str) -> str:
    return source.split("[", 1)[0]


def block_name_from_source(source: str) -> str:
    source = clean_mount_source(source)
    if not source.startswith("/dev/"):
        return ""
    name = Path(source).name
    if name.startswith(("nvme", "mmcblk")):
        return re.sub(r"p\d+$", "", name)
    return re.sub(r"\d+$", "", name)


def is_usb_source(source: str) -> bool:
    source = clean_mount_source(source)
    block = block_name_from_source(source)
    if not block:
        return False
    try:
        device_path = Path(f"/sys/block/{block}/device").resolve()
    except OSError:
        return False
    return "usb" in str(device_path).lower()


def parse_swaps() -> list[dict[str, Any]]:
    swaps: list[dict[str, Any]] = []
    try:
        lines = Path("/proc/swaps").read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return swaps
    for line in lines:
        parts = line.split()
        if len(parts) < 5:
            continue
        swaps.append(
            {
                "filename": parts[0],
                "type": parts[1],
                "size_bytes": int(parts[2]) * 1024,
                "used_bytes": int(parts[3]) * 1024,
                "priority": parts[4],
            }
        )
    return swaps


def parse_compose_bind(line: str) -> tuple[str, str] | None:
    stripped = line.strip().strip('"').strip("'")
    if not stripped.startswith("- "):
        return None
    spec = stripped[2:].strip().strip('"').strip("'")
    if ":" not in spec:
        return None
    host, remainder = spec.split(":", 1)
    container = remainder.split(":", 1)[0]
    if not host or host.startswith("${"):
        return None
    if not (host.startswith("/") or host.startswith(".") or host.startswith("~")):
        return None
    return host, container


def discover_compose_data_dir(root: Path) -> Path | None:
    candidates: list[tuple[int, Path]] = []
    for filename in ("docker-compose.override.yml", "docker-compose.yml"):
        path = root / filename
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            parsed = parse_compose_bind(line)
            if not parsed:
                continue
            host, container = parsed
            if container not in {"/var/lib/bdagStack/node", "/data"}:
                continue
            host_path = Path(host)
            if not host_path.is_absolute():
                host_path = root / host_path
            # The node datadir bind is more exact than a broad /data bind.
            priority = 0 if container == "/var/lib/bdagStack/node" else 1
            candidates.append((priority, host_path))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0])[0][1]


def env_data_dir(root: Path, env: dict[str, str]) -> Path:
    compose_dir = discover_compose_data_dir(root)
    if compose_dir is not None:
        return compose_dir
    raw = next(
        (
            value
            for value in (env.get("BDAG_CHAIN_DATA_DIR"), env.get("BDAG_DATA_DIR"), env.get("DATA_DIR"))
            if value and value.strip().lower() != "auto"
        ),
        "data",
    )
    path = Path(raw)
    return path if path.is_absolute() else root / path


def env_path(root: Path, env: dict[str, str], key: str, default: str | Path) -> Path:
    raw = env.get(key) or str(default)
    if raw.strip().lower() == "auto":
        raw = str(default)
    path = Path(raw).expanduser()
    return path if path.is_absolute() else root / path


def env_node_data_dir(root: Path, env: dict[str, str], node_name: str) -> Path:
    key = "BDAG_NODE1_DATA_DIR"
    return env_path(root, env, key, env_data_dir(root, env) / node_name)


def env_postgres_dir(root: Path, env: dict[str, str]) -> Path:
    return env_path(root, env, "BDAG_POSTGRES_DATA_DIR", root / "data" / "postgres")


def env_runtime_dir(root: Path, env: dict[str, str]) -> Path:
    return env_path(root, env, "BDAG_RUNTIME_DIR", root / "ops" / "runtime")


def env_ephemeral_dir(root: Path, env: dict[str, str]) -> Path:
    return env_path(root, env, "BDAG_EPHEMERAL_DIR", Path("/run/bdag-pool"))


def storage_device(path: Path) -> dict[str, Any]:
    mount = mount_info(path)
    source = clean_mount_source(str(mount.get("source") or ""))
    return {
        "path": str(path),
        "mount": mount,
        "source": source,
        "block": block_name_from_source(source),
        "fstype": str(mount.get("fstype") or "").lower(),
        "is_usb": is_usb_source(source),
    }


def same_storage_device(left: Path, left_device: dict[str, Any], right: Path, right_device: dict[str, Any]) -> bool:
    left_block = str(left_device.get("block") or "")
    right_block = str(right_device.get("block") or "")
    if left_block and right_block:
        return left_block == right_block
    left_source = str(left_device.get("source") or "")
    right_source = str(right_device.get("source") or "")
    if left_source and right_source:
        return left_source == right_source
    return same_filesystem(left, right)


def add(
    checks: list[Check],
    status: str,
    name: str,
    detail: str,
    mitigation: str = "",
    evidence: dict[str, Any] | None = None,
) -> None:
    checks.append(Check(name, status, detail, mitigation, evidence or {}))


def check_host(checks: list[Check], profile: HostProfile) -> None:
    add(
        checks,
        "pass",
        "host_profile",
        (
            f"{profile.os_name}/{profile.arch} profile={profile.profile} "
            f"cpu={profile.cpu_count} memory={profile.memory_gib:.2f}GiB kernel={profile.kernel}"
        ),
        "Use single-node mode and adaptive concurrency on constrained hosts.",
        profile.as_dict(),
    )
    if profile.os_name != "linux":
        add(checks, "warn", "host_os", f"{profile.os_name} lacks Linux pressure and block-device tuning APIs.")
    if profile.profile == "constrained" and profile.memory_bytes and profile.memory_bytes < 4 * GIB:
        add(
            checks,
            "warn",
            "memory_budget",
            f"available RAM is only {profile.memory_gib:.2f}GiB.",
            "Keep one node, cache near 1024MB, status sampler enabled, and maintenance deferred under pressure.",
        )
    if profile.profile == "constrained" and profile.cpu_count <= 2:
        add(
            checks,
            "warn",
            "cpu_budget",
            f"host has {profile.cpu_count} CPU cores.",
            "Keep catch-up and expensive dashboard/miner scans capped with adaptive workers.",
        )


def check_storage(checks: list[Check], root: Path, env: dict[str, str], profile: HostProfile) -> None:
    project_usage = disk_usage(root)
    data_dir = env_data_dir(root, env)
    data_usage = disk_usage(data_dir)
    project_mount = mount_info(root)
    data_mount = mount_info(data_dir)
    data_same_as_root = same_filesystem(root, data_dir)
    data_fstype = str(data_mount.get("fstype") or "").lower()
    data_options = str(data_mount.get("options") or "")
    evidence = {
        "project": project_usage,
        "project_mount": project_mount,
        "data_dir": str(data_dir),
        "data": data_usage,
        "data_mount": data_mount,
        "data_same_filesystem_as_project": data_same_as_root,
    }

    if project_usage["free_bytes"] < 2 * GIB:
        add(checks, "fail", "project_filesystem_free_space", f"project filesystem has only {project_usage['free_gib']}GiB free.", "Free space or move the release root before starting Docker and Postgres.", evidence)
    elif project_usage["free_bytes"] < 6 * GIB:
        add(checks, "warn", "project_filesystem_free_space", f"project filesystem has {project_usage['free_gib']}GiB free.", "Keep chain data, Docker root, archives, and old snapshots off the boot filesystem.", evidence)
    else:
        add(checks, "pass", "project_filesystem_free_space", f"{project_usage['free_gib']}GiB free", evidence=evidence)

    if data_usage["free_bytes"] < 10 * GIB:
        add(checks, "fail", "chain_data_free_space", f"chain data filesystem has only {data_usage['free_gib']}GiB free.", "Move chain data to a larger disk before initial sync or snapshot import.", evidence)
    elif data_usage["free_bytes"] < 50 * GIB:
        add(checks, "warn", "chain_data_free_space", f"chain data filesystem has {data_usage['free_gib']}GiB free.", "Allow headroom for chain growth, FastSnap artifacts, Postgres, and rollback backups.", evidence)
    else:
        add(checks, "pass", "chain_data_free_space", f"{data_usage['free_gib']}GiB free", evidence=evidence)

    if data_fstype in FLASH_UNFRIENDLY_FS:
        add(checks, "fail", "chain_data_filesystem", f"chain data is on {data_fstype}, which is not suitable for node databases.", "Use F2FS or ext4 on Linux for chain data.", evidence)
    elif data_fstype and data_fstype not in {"f2fs", "ext4", "xfs", "btrfs", "zfs"}:
        add(checks, "warn", "chain_data_filesystem", f"chain data filesystem is {data_fstype}.", "Prefer F2FS for USB flash or ext4/xfs on SSD/NVMe.", evidence)
    elif data_fstype == "f2fs":
        add(checks, "pass", "chain_data_filesystem", "chain data is on F2FS", evidence=evidence)
    elif data_fstype:
        add(checks, "pass", "chain_data_filesystem", f"chain data is on {data_fstype}", evidence=evidence)
    else:
        add(checks, "warn", "chain_data_filesystem", "could not identify chain data filesystem", evidence=evidence)

    if data_fstype == "f2fs" and "noatime" not in data_options and "relatime" not in data_options:
        add(checks, "warn", "chain_data_mount_options", "F2FS chain data mount is missing noatime/relatime.", "Mount with noatime or relatime; lazytime is also recommended on flash-backed appliances.", evidence)
    elif data_fstype == "f2fs" and "lazytime" not in data_options:
        add(checks, "warn", "chain_data_mount_options", "F2FS chain data mount is missing lazytime.", "Use noatime,lazytime to reduce metadata write pressure where supported.", evidence)
    elif data_options:
        add(checks, "pass", "chain_data_mount_options", "mount options include low-write protections where available", evidence=evidence)

    if profile.profile == "constrained" and data_same_as_root:
        add(checks, "warn", "chain_data_placement", "chain data is on the same filesystem as the release/root path.", "On thin clients and small eMMC hosts, put chain data and Docker writes on a dedicated SSD/USB filesystem.", evidence)
    elif not data_same_as_root:
        add(checks, "pass", "chain_data_placement", "chain data is separated from the project/root filesystem", evidence=evidence)

    usb_chain_data = is_usb_source(str(data_mount.get("source") or ""))
    if usb_chain_data and data_fstype not in {"f2fs", "ext4"}:
        add(checks, "warn", "usb_chain_filesystem", f"USB chain device uses {data_fstype or 'unknown'} filesystem.", "Use F2FS for USB flash or ext4 for USB SSD.", evidence)

    mining_address = (env.get("MINING_ADDRESS") or env.get("MINING_POOL_ADDRESS") or "").strip()
    topology = (env.get("BDAG_DETECTED_NETWORK_TOPOLOGY") or env.get("BDAG_NETWORK_TOPOLOGY") or "").strip().lower()
    mining_appliance = (
        mining_address and mining_address.lower() != ZERO_ETH_ADDRESS
    ) or topology == "single-node-asic-router"
    no_fastsync_serve = bool_enabled(env.get("BDAG_NO_FASTSYNC_SERVE", "auto"), True)
    if usb_chain_data and mining_appliance and not no_fastsync_serve:
        add(
            checks,
            "fail",
            "usb_mining_fastsync_serving",
            "USB-backed mining node is configured to serve bulk FastSync data.",
            "Set BDAG_NO_FASTSYNC_SERVE=auto or 1 so the node can consume sync and relay blocks without serving range, snapshot, or artifact traffic from USB while mining.",
            evidence,
        )
    elif usb_chain_data and mining_appliance:
        add(checks, "pass", "usb_mining_fastsync_serving", "USB-backed mining node will not serve bulk FastSync data", evidence=evidence)


def check_storage_profile(checks: list[Check], root: Path, env: dict[str, str], profile: HostProfile) -> None:
    selected = (env.get("BDAG_STORAGE_PROFILE") or "auto").strip().lower() or "auto"
    known_profiles = {"auto", "single-device", "single-usb-constrained", "usb-chain-internal-runtime", "split-ssd", "dev"}
    chain_base = env_data_dir(root, env)
    active_node_dir = env_node_data_dir(root, env, "node1")
    postgres_dir = env_postgres_dir(root, env)
    runtime_dir = env_runtime_dir(root, env)

    chain_device = storage_device(active_node_dir)
    postgres_device = storage_device(postgres_dir)
    runtime_device = storage_device(runtime_dir)
    project_device = storage_device(root)

    postgres_same_as_chain = same_storage_device(active_node_dir, chain_device, postgres_dir, postgres_device)
    runtime_same_as_chain = same_storage_device(active_node_dir, chain_device, runtime_dir, runtime_device)
    project_same_as_chain = same_storage_device(active_node_dir, chain_device, root, project_device)

    docker_root = docker_root_dir()
    docker_same_as_chain = None
    docker_device: dict[str, Any] | None = None
    if docker_root:
        docker_path = Path(docker_root)
        docker_device = storage_device(docker_path)
        docker_same_as_chain = same_storage_device(active_node_dir, chain_device, docker_path, docker_device)

    if selected not in known_profiles:
        add(
            checks,
            "warn",
            "storage_profile",
            f"unknown BDAG_STORAGE_PROFILE={selected}",
            "Use auto, usb-chain-internal-runtime, single-usb-constrained, split-ssd, single-device, or dev.",
            {"BDAG_STORAGE_PROFILE": selected},
        )
        return

    chain_is_usb = bool(chain_device.get("is_usb"))
    if selected == "auto":
        if chain_is_usb and not postgres_same_as_chain and not runtime_same_as_chain:
            resolved = "usb-chain-internal-runtime"
        elif chain_is_usb:
            resolved = "single-usb-constrained"
        elif not project_same_as_chain:
            resolved = "split-ssd"
        else:
            resolved = "single-device"
    else:
        resolved = selected

    evidence = {
        "selected_profile": selected,
        "resolved_profile": resolved,
        "chain_base": str(chain_base),
        "active_node_data_dir": str(active_node_dir),
        "postgres_dir": str(postgres_dir),
        "runtime_dir": str(runtime_dir),
        "chain_device": chain_device,
        "postgres_device": postgres_device,
        "runtime_device": runtime_device,
        "project_device": project_device,
        "postgres_same_as_chain": postgres_same_as_chain,
        "runtime_same_as_chain": runtime_same_as_chain,
        "project_same_as_chain": project_same_as_chain,
        "docker_root": docker_root,
        "docker_device": docker_device,
        "docker_same_as_chain": docker_same_as_chain,
    }

    add(checks, "pass", "storage_profile", f"{selected} resolved to {resolved}", evidence=evidence)

    if chain_is_usb:
        if not postgres_same_as_chain and not runtime_same_as_chain:
            add(
                checks,
                "pass",
                "storage_io_split",
                "USB chain data is separated from Postgres and dashboard/runtime writes",
                evidence=evidence,
            )
        else:
            add(
                checks,
                "warn",
                "storage_io_split",
                "USB chain data shares a device with frequent small runtime writes.",
                "USB-backed chain installs should keep growing node data on USB capacity storage, but place BDAG_POSTGRES_DATA_DIR and BDAG_RUNTIME_DIR on internal or other non-USB storage when it has at least 4GiB free.",
                evidence,
            )
    elif profile.profile == "constrained" and project_same_as_chain:
        add(
            checks,
            "warn",
            "storage_io_split",
            "constrained host is using one device for project, chain, and runtime writes.",
            "Use a large USB/SSD for BDAG_CHAIN_DATA_DIR and keep BDAG_POSTGRES_DATA_DIR/BDAG_RUNTIME_DIR on internal storage if capacity allows.",
            evidence,
        )
    else:
        add(checks, "pass", "storage_io_split", "chain and runtime storage placement is acceptable for this host profile", evidence=evidence)

    if selected in {"usb-chain-internal-runtime", "split-ssd"} and (postgres_same_as_chain or runtime_same_as_chain):
        add(
            checks,
            "warn",
            "explicit_storage_profile_mismatch",
            f"BDAG_STORAGE_PROFILE={selected} expects Postgres and runtime writes away from the active chain device.",
            "Correct BDAG_POSTGRES_DATA_DIR and BDAG_RUNTIME_DIR or set BDAG_STORAGE_PROFILE=auto if this is an intentional single-device install.",
            evidence,
        )

    if docker_same_as_chain is True and (profile.profile == "constrained" or chain_is_usb):
        add(
            checks,
            "warn",
            "docker_chain_shared_device",
            "Docker root shares the active chain device.",
            "Keep Docker root on internal storage when image/build-cache budget fits; otherwise prune builder cache and keep local Docker log caps enabled.",
            evidence,
        )
    elif docker_same_as_chain is False:
        add(checks, "pass", "docker_chain_shared_device", "Docker root is separated from the active chain device", evidence=evidence)


def check_ephemeral_storage(checks: list[Check], root: Path, env: dict[str, str]) -> None:
    enabled = bool_enabled(env.get("BDAG_EPHEMERAL_TMPFS_ENABLED"), True)
    ephemeral_dir = env_ephemeral_dir(root, env)
    tmpdir = env_path(root, env, "TMPDIR", ephemeral_dir / "tmp")
    ephemeral_device = storage_device(ephemeral_dir)
    tmpdir_device = storage_device(tmpdir)
    staging_raw = (env.get("BDAG_FASTSNAP_DIRECTORY_STAGING") or "").strip()
    staging_path = env_path(root, env, "BDAG_FASTSNAP_DIRECTORY_STAGING", staging_raw) if staging_raw else None
    staging_device = storage_device(staging_path) if staging_path else None
    evidence = {
        "BDAG_EPHEMERAL_TMPFS_ENABLED": env.get("BDAG_EPHEMERAL_TMPFS_ENABLED"),
        "BDAG_EPHEMERAL_DIR": str(ephemeral_dir),
        "TMPDIR": str(tmpdir),
        "BDAG_CONTAINER_TMPFS_SIZE": env.get("BDAG_CONTAINER_TMPFS_SIZE"),
        "BDAG_NODE_TMPFS_SIZE": env.get("BDAG_NODE_TMPFS_SIZE"),
        "BDAG_NODE_SHM_SIZE": env.get("BDAG_NODE_SHM_SIZE"),
        "ephemeral_device": ephemeral_device,
        "tmpdir_device": tmpdir_device,
        "BDAG_FASTSNAP_DIRECTORY_STAGING": str(staging_path) if staging_path else "",
        "fastsnap_staging_device": staging_device,
    }
    if not enabled:
        add(
            checks,
            "warn",
            "ephemeral_tmpfs",
            "ephemeral tmpfs placement is disabled.",
            "Use RAM-backed storage for small temporary files and caches that are safe to lose; keep large snapshot/chain staging on capacity storage.",
            evidence,
        )
        return

    if str(ephemeral_device.get("fstype") or "") in RAM_BACKED_FS and str(tmpdir_device.get("fstype") or "") in RAM_BACKED_FS:
        add(
            checks,
            "pass",
            "ephemeral_tmpfs",
            "ephemeral scratch paths resolve to RAM-backed storage",
            evidence=evidence,
        )
    else:
        add(
            checks,
            "warn",
            "ephemeral_tmpfs",
            "ephemeral scratch paths are disk-backed.",
            "Prefer /run/bdag-pool or container tmpfs mounts for small temporary files, transient caches, and scratch state that can be lost on reboot.",
            evidence,
        )

    if staging_device and str(staging_device.get("fstype") or "") in RAM_BACKED_FS:
        add(
            checks,
            "warn",
            "fastsnap_staging_tmpfs",
            "FastSnap directory staging is on RAM-backed storage.",
            "Only small ephemeral scratch belongs on tmpfs. Keep large FastSnap artifact staging on chain/capacity storage unless the host has deliberately provisioned enough RAM.",
            evidence,
        )


def chain_marker_exists(path: Path) -> bool:
    if not path.exists():
        return False
    return any((path / marker).exists() for marker in CHAIN_DB_MARKERS)


def check_node_data_layout(checks: list[Check], root: Path, env: dict[str, str]) -> None:
    data_dir = env_data_dir(root, env)
    node_mode = (env.get("BDAG_NODE_MODE") or "single").strip().lower()
    node1 = data_dir / "node1"
    node1_has = chain_marker_exists(node1 / "mainnet") or chain_marker_exists(node1)
    evidence = {"data_dir": str(data_dir), "node_mode": node_mode, "active_node_has_chain_markers": node1_has}
    add(checks, "pass", "single_node_data_layout", "active node data layout is inspectable", evidence=evidence)

    backup_like = []
    if data_dir.exists():
        for item in data_dir.iterdir():
            name = item.name
            if ".pre-v2-" in name or ".backup." in name or name.startswith("node-data.pre-"):
                backup_like.append(name)
    if backup_like:
        add(checks, "warn", "old_chain_backups_present", f"found parked chain backup directories: {', '.join(backup_like[:5])}", "Retain until stable mining, then remove old backups deliberately to reclaim space.", {"data_dir": str(data_dir), "backups": backup_like})


def check_env_defaults(checks: list[Check], env: dict[str, str], profile: HostProfile) -> None:
    evidence = {
        "BDAG_NODE_MODE": env.get("BDAG_NODE_MODE"),
        "BDAG_NODE_CACHE_MB": env.get("BDAG_NODE_CACHE_MB"),
        "NODE_MAX_PEERS": env.get("NODE_MAX_PEERS"),
        "BDAG_FASTSYNC_PREPROCESS_WORKERS": env.get("BDAG_FASTSYNC_PREPROCESS_WORKERS"),
        "BDAG_NO_FASTSYNC_SERVE": env.get("BDAG_NO_FASTSYNC_SERVE"),
        "BDAG_FASTARTIFACTSYNC_ENABLED": env.get("BDAG_FASTARTIFACTSYNC_ENABLED"),
        "BDAG_STORAGE_PROFILE": env.get("BDAG_STORAGE_PROFILE"),
        "BDAG_DETECTED_NETWORK_TOPOLOGY": env.get("BDAG_DETECTED_NETWORK_TOPOLOGY"),
        "BDAG_SYNC_COORDINATOR_ACCELERATE_FASTSYNC": env.get("BDAG_SYNC_COORDINATOR_ACCELERATE_FASTSYNC"),
        "BDAG_SYNC_COORDINATOR_FAST_RESTART_COOLDOWN_SECONDS": env.get("BDAG_SYNC_COORDINATOR_FAST_RESTART_COOLDOWN_SECONDS"),
        "BDAG_STATUS_SAMPLER_ENABLED": env.get("BDAG_STATUS_SAMPLER_ENABLED"),
        "BDAG_ADAPTIVE_CONCURRENCY_ENABLED": env.get("BDAG_ADAPTIVE_CONCURRENCY_ENABLED"),
        "BDAG_ENTRYPOINT_CHOWN_MODE": env.get("BDAG_ENTRYPOINT_CHOWN_MODE"),
        "BDAG_ENABLE_NODE_MINING": env.get("BDAG_ENABLE_NODE_MINING"),
        "BDAG_NODE_MODULES": env.get("BDAG_NODE_MODULES"),
        "BDAG_NODE_MINING_ARGS": env.get("BDAG_NODE_MINING_ARGS"),
    }
    node_mode = (env.get("BDAG_NODE_MODE") or "single").strip().lower()
    if profile.profile == "constrained" and node_mode not in {"single", "single-node", "one", "1"}:
        add(checks, "warn", "constrained_node_mode", f"constrained host is configured for BDAG_NODE_MODE={node_mode}.", "Use single-node mode unless the host has enough RAM, disk bandwidth, and power headroom for two nodes.", evidence)
    else:
        add(checks, "pass", "constrained_node_mode", f"BDAG_NODE_MODE={node_mode or 'single'}", evidence=evidence)

    cache_mb = safe_int(env.get("BDAG_NODE_CACHE_MB"), 1024)
    if profile.profile == "constrained" and cache_mb and cache_mb > 1536:
        add(checks, "warn", "node_cache_budget", f"BDAG_NODE_CACHE_MB={cache_mb} is high for this host.", "Use 1024MB to reduce swap and write stalls on 3-4GiB mining appliances.", evidence)
    else:
        add(checks, "pass", "node_cache_budget", f"BDAG_NODE_CACHE_MB={cache_mb}", evidence=evidence)

    max_peers = safe_int(env.get("NODE_MAX_PEERS"), 160)
    if profile.profile == "constrained" and max_peers and max_peers > 200:
        add(checks, "warn", "peer_budget", f"NODE_MAX_PEERS={max_peers} is high for this host.", "Use 160 or lower on constrained single-ASIC appliances.", evidence)
    else:
        add(checks, "pass", "peer_budget", f"NODE_MAX_PEERS={max_peers}", evidence=evidence)

    preprocess = safe_int(env.get("BDAG_FASTSYNC_PREPROCESS_WORKERS"), 1)
    if profile.profile == "constrained" and preprocess and preprocess > 1:
        add(checks, "warn", "fastsync_preprocess_workers", f"BDAG_FASTSYNC_PREPROCESS_WORKERS={preprocess} can contend with mining.", "Use one preprocess worker on slow disks and two-core hosts.", evidence)
    else:
        add(checks, "pass", "fastsync_preprocess_workers", f"BDAG_FASTSYNC_PREPROCESS_WORKERS={preprocess}", evidence=evidence)

    storage_profile = (env.get("BDAG_STORAGE_PROFILE") or "").strip().lower()
    topology = (env.get("BDAG_DETECTED_NETWORK_TOPOLOGY") or env.get("BDAG_NETWORK_TOPOLOGY") or "").strip().lower()
    constrained_mining_profile = (
        storage_profile in {"usb-chain-internal-runtime", "single-usb-constrained"}
        or topology == "single-node-asic-router"
    )
    node_args_append_enables_fastartifact = node_args_enable_fastartifact(env.get("NODE_ARGS_APPEND"))
    evidence["NODE_ARGS_APPEND"] = env.get("NODE_ARGS_APPEND")
    append_args_enable_fastartifact = node_args_append_enables_fastartifact
    no_fastsync_serve = bool_enabled(env.get("BDAG_NO_FASTSYNC_SERVE", "auto"), True)
    if constrained_mining_profile and no_fastsync_serve and append_args_enable_fastartifact:
        add(checks, "fail", "fastartifactsync", "BDAG_NO_FASTSYNC_SERVE suppresses serving, but node args still add --fastartifactsync.", "Clear NODE_ARGS_APPEND so constrained USB/router profiles do not serve FastArtifact while mining.", evidence)
    elif constrained_mining_profile and not bool_enabled(env.get("BDAG_FASTARTIFACTSYNC_ENABLED"), True) and append_args_enable_fastartifact:
        add(checks, "fail", "fastartifactsync", "BDAG_FASTARTIFACTSYNC_ENABLED is disabled, but node args still add --fastartifactsync.", "Clear NODE_ARGS_APPEND so constrained USB/router profiles do not serve FastArtifact while mining.", evidence)
    elif constrained_mining_profile and no_fastsync_serve:
        add(checks, "pass", "fastartifactsync", "Bulk FastSync/FastArtifact serving is suppressed for constrained mining profile", evidence=evidence)
    elif not bool_enabled(env.get("BDAG_FASTARTIFACTSYNC_ENABLED"), True) and constrained_mining_profile:
        add(checks, "pass", "fastartifactsync", "Fast Artifact node startup flag is disabled for constrained mining profile", evidence=evidence)
    elif not bool_enabled(env.get("BDAG_FASTARTIFACTSYNC_ENABLED"), True):
        add(checks, "warn", "fastartifactsync", "BDAG_FASTARTIFACTSYNC_ENABLED is disabled.", "Enable Fast Artifact Sync V2 so nodes can advertise and use the fastest sync path.", evidence)
    elif constrained_mining_profile:
        add(checks, "warn", "fastartifactsync", "Fast Artifact node startup flag is enabled on a constrained ASIC-router/storage profile.", "Disable continuous node FastArtifact mode while synced and mining; keep raw-datadir catch-up acceleration available through the sync coordinator.", evidence)
    else:
        add(checks, "pass", "fastartifactsync", "Fast Artifact Sync V2 startup flag is enabled", evidence=evidence)

    node_mining_enabled = bool_enabled(env.get("BDAG_ENABLE_NODE_MINING"), False)
    node_modules = {item.strip().lower() for item in (env.get("BDAG_NODE_MODULES") or "").split(",") if item.strip()}
    node_mining_args = env.get("BDAG_NODE_MINING_ARGS") or ""
    missing_mining_args = [
        flag
        for flag in ("--allowminingwhennearlysynced", "--allowsubmitwhennotsynced", "--miner", "--miningaddr=")
        if flag not in node_mining_args
    ]
    if node_mining_enabled and "miner" not in node_modules:
        add(checks, "fail", "node_mining_runtime", "node mining is enabled but the miner module is not exposed.", "Set BDAG_NODE_MODULES=Blockdag,miner so the pool can request fresh templates.", evidence)
    elif node_mining_enabled and missing_mining_args:
        add(checks, "fail", "node_mining_runtime", "node mining is enabled but required mining guard args are missing: " + ", ".join(missing_mining_args), "Set BDAG_NODE_MINING_ARGS with near-sync mining, submit override, miner mode, and the payout mining address.", evidence)
    elif node_mining_enabled and constrained_mining_profile and "--maxinbound=1" not in node_mining_args:
        add(checks, "warn", "node_mining_runtime", "constrained ASIC-router mining is enabled without --maxinbound=1.", "Add --maxinbound=1 so inbound catch-up peers cannot contend with paid block submission on USB/router hosts while P2P remains usable.", evidence)
    elif node_mining_enabled:
        add(checks, "pass", "node_mining_runtime", "node miner/template runtime guard args are configured", evidence=evidence)
    else:
        add(checks, "pass", "node_mining_runtime", "node mining stays disabled until miners are present", evidence=evidence)

    if not bool_enabled(env.get("BDAG_SYNC_COORDINATOR_ACCELERATE_FASTSYNC"), True):
        add(checks, "warn", "fastsync_acceleration", "BDAG_SYNC_COORDINATOR_ACCELERATE_FASTSYNC is disabled.", "Enable coordinator acceleration so nodes more than 1000 blocks behind use fastest catch-up defaults.", evidence)
    else:
        add(checks, "pass", "fastsync_acceleration", "sync coordinator fastest catch-up is enabled", evidence=evidence)

    fast_restart_cooldown = safe_int(env.get("BDAG_SYNC_COORDINATOR_FAST_RESTART_COOLDOWN_SECONDS"), 900)
    if fast_restart_cooldown and fast_restart_cooldown > 1800:
        add(checks, "warn", "fastsync_restart_cooldown", f"fast restart cooldown is {fast_restart_cooldown}s.", "Use 900s so a stale or unaccelerated importer does not remain down-level for too long.", evidence)
    else:
        add(checks, "pass", "fastsync_restart_cooldown", f"fast restart cooldown={fast_restart_cooldown}s", evidence=evidence)

    if not bool_enabled(env.get("BDAG_STATUS_SAMPLER_ENABLED"), True):
        add(checks, "warn", "status_sampler", "BDAG_STATUS_SAMPLER_ENABLED is disabled.", "Enable the sampler so dashboard, watchdog, and guards share one low-overhead status collection.", evidence)
    else:
        add(checks, "pass", "status_sampler", "shared status sampler enabled", evidence=evidence)

    if not bool_enabled(env.get("BDAG_ADAPTIVE_CONCURRENCY_ENABLED"), True):
        add(checks, "warn", "adaptive_concurrency", "BDAG_ADAPTIVE_CONCURRENCY_ENABLED is disabled.", "Enable adaptive workers so monitoring backs off during CPU, RAM, disk, or RPC pressure.", evidence)
    else:
        add(checks, "pass", "adaptive_concurrency", "adaptive concurrency enabled", evidence=evidence)

    chown_mode = (env.get("BDAG_ENTRYPOINT_CHOWN_MODE") or "needed").strip().lower()
    if chown_mode not in {"needed", "never"}:
        add(checks, "warn", "entrypoint_chown_mode", f"BDAG_ENTRYPOINT_CHOWN_MODE={chown_mode} may rescan large volumes on boot.", "Use needed or never to avoid repeated ownership walks on chain data.", evidence)
    else:
        add(checks, "pass", "entrypoint_chown_mode", f"BDAG_ENTRYPOINT_CHOWN_MODE={chown_mode}", evidence=evidence)


def check_swap(checks: list[Check], profile: HostProfile) -> None:
    swaps = parse_swaps()
    total = sum(item["size_bytes"] for item in swaps)
    used = sum(item["used_bytes"] for item in swaps)
    non_zram_total = sum(item["size_bytes"] for item in swaps if "zram" not in item["filename"])
    evidence = {"swaps": swaps, "total_bytes": total, "used_bytes": used}
    if profile.profile == "constrained" and non_zram_total > 2 * GIB:
        add(checks, "warn", "swap_budget", f"non-zram swap is {round(non_zram_total / GIB, 2)}GiB.", "Keep disk-backed swap small on flash appliances; large swap can hide memory pressure as disk write latency.", evidence)
    elif profile.profile == "constrained" and total == 0:
        add(checks, "warn", "swap_budget", "no swap is configured on a constrained host.", "A small emergency swap file or zram device is safer than OOM kills during snapshot import.", evidence)
    else:
        add(checks, "pass", "swap_budget", f"swap total={round(total / GIB, 2)}GiB used={round(used / GIB, 2)}GiB", evidence=evidence)


def check_network(checks: list[Check]) -> None:
    proc = run(["ip", "-o", "-4", "route", "get", "1.1.1.1"], timeout=3)
    if proc.returncode != 0 or not proc.stdout.strip():
        add(checks, "warn", "default_route", "no IPv4 default route was detected.", "Configure networking before FastSnap peer discovery or ASIC setup.", {"stderr": proc.stderr.strip()})
        return
    line = proc.stdout.strip().splitlines()[0]
    parts = line.split()
    src = next((parts[i + 1] for i, part in enumerate(parts[:-1]) if part == "src"), "")
    dev = next((parts[i + 1] for i, part in enumerate(parts[:-1]) if part == "dev"), "")
    evidence = {"route": line, "src": src, "dev": dev, "hostname": socket.gethostname()}
    if dev.startswith("wl"):
        add(checks, "warn", "default_route", f"default route uses Wi-Fi interface {dev} with source {src}.", "Keep ASIC and trusted FastSnap peers on the same low-latency LAN; prefer wired Ethernet if shares or submits stall.", evidence)
    else:
        add(checks, "pass", "default_route", f"default route uses {dev or 'unknown'} source {src or 'unknown'}", evidence=evidence)


def docker_root_dir() -> str:
    proc = run(["docker", "info", "--format", "{{.DockerRootDir}}"], timeout=5)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def check_docker_storage(checks: list[Check], root: Path, env: dict[str, str], profile: HostProfile) -> None:
    docker_root = docker_root_dir()
    if not docker_root:
        add(checks, "warn", "docker_storage", "Docker root directory could not be queried.", "Install Docker and make sure the installer user can run docker before starting the stack.")
        return
    docker_path = Path(docker_root)
    usage = disk_usage(docker_path)
    same_as_root = same_filesystem(root, docker_path)
    same_as_data = same_filesystem(env_data_dir(root, env), docker_path)
    evidence = {"docker_root": docker_root, "usage": usage, "same_as_project": same_as_root, "same_as_data": same_as_data}
    if profile.profile == "constrained" and same_as_root and usage["free_bytes"] < 8 * GIB:
        add(checks, "warn", "docker_storage", f"Docker root is on the constrained project/root filesystem with {usage['free_gib']}GiB free.", "Move Docker data root or the release root to the appliance data disk so image layers and logs do not fill eMMC.", evidence)
    else:
        add(checks, "pass", "docker_storage", f"Docker root {docker_root} has {usage['free_gib']}GiB free", evidence=evidence)


def check_live_node_child(checks: list[Check], root: Path) -> None:
    ps_proc = run(["docker", "compose", "ps", "-q", "node"], timeout=5, cwd=root)
    if ps_proc.returncode != 0 or not ps_proc.stdout.strip():
        add(
            checks,
            "pass",
            "live_node_child",
            "no running compose node service was detected during preflight",
            "When checking an installed live runtime, this check fails if the wrapper is up but blockdag-node is gone.",
        )
        return

    exec_proc = run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "node",
            "sh",
            "-lc",
            "pgrep -af '(^|/)blockdag-node|/usr/local/bin/bdag' | grep -v pgrep",
        ],
        timeout=8,
        cwd=root,
    )
    evidence = {
        "compose_node_ids": [line for line in ps_proc.stdout.splitlines() if line.strip()],
        "stdout": exec_proc.stdout.strip(),
        "stderr": exec_proc.stderr.strip(),
        "returncode": exec_proc.returncode,
    }
    if exec_proc.returncode != 0 or not exec_proc.stdout.strip():
        add(
            checks,
            "fail",
            "live_node_child",
            "compose node service is running but blockdag-node child is not visible.",
            "Restart the node container and ensure watchdog/node-child-guard services are installed and active.",
            evidence,
        )
    else:
        add(checks, "pass", "live_node_child", "blockdag-node child process is running inside the node service", evidence=evidence)


def check_schema_file(checks: list[Check], root: Path) -> None:
    schema = root / "sql" / "pool-schema.sql"
    if not schema.exists():
        add(checks, "fail", "pool_schema_file", "sql/pool-schema.sql is missing.", "The release must include the pool schema so block submissions and earnings can be persisted.")
        return
    text = schema.read_text(encoding="utf-8")
    required = ["block_submissions", "credits_block_miner_unique", "block_submissions_created_at_idx"]
    missing = [item for item in required if item not in text]
    if missing:
        add(checks, "fail", "pool_schema_file", "pool schema is missing " + ", ".join(missing), "Apply the release schema gate before packaging.", {"schema": str(schema), "missing": missing})
    else:
        add(checks, "pass", "pool_schema_file", "pool schema includes block submission and credit idempotency gates", evidence={"schema": str(schema)})


def check_wallet(checks: list[Check], env: dict[str, str]) -> None:
    address = (env.get("MINING_ADDRESS") or env.get("MINING_POOL_ADDRESS") or "").strip()
    node_mining_enabled = bool_enabled(env.get("BDAG_ENABLE_NODE_MINING"), False)
    if node_mining_enabled and (not address or address.lower() == ZERO_ETH_ADDRESS):
        add(checks, "fail", "mining_address", "node mining is enabled but the reward wallet is unset or zero.", "Set MINING_ADDRESS/MINING_POOL_ADDRESS before attaching ASICs.", {"address": address, "BDAG_ENABLE_NODE_MINING": env.get("BDAG_ENABLE_NODE_MINING")})
    elif not address or address.lower() == ZERO_ETH_ADDRESS:
        add(checks, "warn", "mining_address", "reward wallet is unset or zero.", "Set the wallet before enabling miner sources or node mining.", {"address": address})
    else:
        add(checks, "pass", "mining_address", f"reward wallet configured: {address[:10]}...{address[-6:]}", evidence={"address": address})


def run_preflight(root: Path, env_file: Path) -> dict[str, Any]:
    root = root.resolve()
    env = os.environ.copy()
    env.update(load_env_file(env_file))
    profile = detect_host_profile()
    checks: list[Check] = []
    check_host(checks, profile)
    check_storage(checks, root, env, profile)
    check_storage_profile(checks, root, env, profile)
    check_ephemeral_storage(checks, root, env)
    check_node_data_layout(checks, root, env)
    check_env_defaults(checks, env, profile)
    check_swap(checks, profile)
    check_network(checks)
    check_docker_storage(checks, root, env, profile)
    check_live_node_child(checks, root)
    check_schema_file(checks, root)
    check_wallet(checks, env)
    failures = [check for check in checks if check.status == "fail"]
    warnings = [check for check in checks if check.status == "warn"]
    return {
        "ok": not failures,
        "root": str(root),
        "env_file": str(env_file),
        "host_profile": profile.as_dict(),
        "failure_count": len(failures),
        "warning_count": len(warnings),
        "checks": [check.as_dict() for check in checks],
    }


def print_human(payload: dict[str, Any]) -> None:
    for check in payload["checks"]:
        print(f"{check['status'].upper()} {check['name']}: {check['detail']}")
        if check.get("mitigation"):
            print(f"  mitigation: {check['mitigation']}")
    print(
        "SUMMARY "
        f"ok={payload['ok']} failures={payload['failure_count']} warnings={payload['warning_count']} "
        f"profile={payload['host_profile'].get('profile')}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run BlockDAG mining appliance preflight checks.")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--warn-only", action="store_true", help="Always exit 0 after reporting failures.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root)
    env_file = Path(args.env_file) if args.env_file else root / ".env"
    payload = run_preflight(root, env_file)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_human(payload)
    if args.warn_only:
        return 0
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
