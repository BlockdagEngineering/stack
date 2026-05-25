#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, flock
from pathlib import Path


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] fastartifact-sidecar: {message}", flush=True)


def env_value(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
    return values


def int_env(name: str, default: int) -> int:
    raw = env_value(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


@contextmanager
def lock_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        try:
            flock(handle.fileno(), LOCK_EX | LOCK_NB)
        except BlockingIOError:
            log("another sidecar run is active")
            raise SystemExit(0)
        yield


def metric_order(url: str, timeout: float) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    fallback: int | None = None
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        name = fields[0]
        try:
            value = int(float(fields[1]))
        except ValueError:
            continue
        if name == "Blockdag_mainorder":
            return value
        if name == "chain_head_block":
            fallback = value
    return fallback


def newest_live_order(urls: str, timeout: float) -> int | None:
    orders: list[int] = []
    for entry in urls.split(","):
        entry = entry.strip()
        if not entry:
            continue
        _, _, url = entry.partition("=")
        url = url or entry
        order = metric_order(url, timeout)
        if order is not None:
            orders.append(order)
    return max(orders) if orders else None


def manifest_tip(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for key in ("tip_order", "tipOrder", "TipOrder", "block_count", "blockCount"):
        value = data.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def file_age_seconds(path: Path) -> int | None:
    try:
        return int(time.time() - path.stat().st_mtime)
    except OSError:
        return None


def should_export(seed_dir: Path, live_order: int | None) -> tuple[bool, str]:
    force = env_value("BDAG_FASTARTIFACT_SIDECAR_FORCE", "0") == "1"
    if force:
        return True, "forced by BDAG_FASTARTIFACT_SIDECAR_FORCE=1"

    archive = seed_dir / "snapshot.bdsnap"
    manifest = seed_dir / "snapshot.bdsnap.manifest.json"
    if not archive.exists() or archive.stat().st_size == 0:
        return True, "published snapshot archive is missing"
    if not manifest.exists() or manifest.stat().st_size == 0:
        return True, "published snapshot manifest is missing"

    max_age = int_env("BDAG_FASTARTIFACT_SIDECAR_MAX_ARCHIVE_AGE_SECONDS", 7200)
    age = file_age_seconds(archive)
    if age is not None and max_age > 0 and age > max_age:
        return True, f"published snapshot is stale by age age={age}s max={max_age}s"

    max_lag = int_env("BDAG_FASTARTIFACT_SIDECAR_MAX_SEED_LAG", 10000)
    tip = manifest_tip(manifest)
    if live_order is not None and tip is not None:
        lag = max(0, live_order - tip)
        if lag > max_lag:
            return True, f"published snapshot is behind live order lag={lag} max={max_lag}"
        return False, f"published snapshot is fresh lag={lag} age={age if age is not None else 'unknown'}s"

    if tip is None:
        return True, "published snapshot tip could not be read"
    return False, "published snapshot exists; live order unavailable, leaving current seed in place"


def main() -> int:
    project_root = Path(env_value("BDAG_PROJECT_ROOT", str(Path(__file__).resolve().parents[1]))).resolve()
    dotenv = read_dotenv(project_root / ".env")
    node_mode = env_value("BDAG_NODE_MODE", dotenv.get("BDAG_NODE_MODE", "single"))
    require_dual = env_value("BDAG_FASTARTIFACT_SIDECAR_REQUIRE_DUAL_NODE", "1") == "1"
    if require_dual and node_mode != "double":
        log(f"skipping because BDAG_NODE_MODE={node_mode}; dual-node export is required")
        return 0

    seed_dir = Path(env_value("BDAG_FASTARTIFACT_SIDECAR_SEED_DIR", env_value("BDAG_FASTSNAP_SEED_DIR", str(project_root / "data-restore" / "fastsnap")))).resolve()
    lock_path = Path(env_value("BDAG_FASTARTIFACT_SIDECAR_LOCK", str(project_root / "ops" / "runtime" / "fastartifact-sidecar.lock")))
    metrics_urls = env_value(
        "BDAG_FASTSNAP_NODE_METRICS_URLS",
        "node1=http://127.0.0.1:6061/debug/metrics/prometheus,node2=http://127.0.0.1:6062/debug/metrics/prometheus",
    )
    metrics_timeout = float(env_value("BDAG_FASTARTIFACT_SIDECAR_METRICS_TIMEOUT", "3"))

    with lock_file(lock_path):
        live_order = newest_live_order(metrics_urls, metrics_timeout)
        export, reason = should_export(seed_dir, live_order)
        if not export:
            log(f"skip export: {reason}")
            return 0
        log(f"export needed: {reason}")

        script = project_root / "ops" / "build-fastsnap-seed.sh"
        if not script.exists():
            log(f"missing seed builder: {script}")
            return 1
        env = os.environ.copy()
        env.setdefault("BDAG_PROJECT_ROOT", str(project_root))
        env.setdefault("BDAG_ENV_FILE", str(project_root / ".env"))
        env.setdefault("BDAG_COMPOSE_FILE", str(project_root / "docker-compose.yml"))
        env.setdefault("BDAG_FASTSNAP_SEED_DIR", str(seed_dir))
        env.setdefault("BDAG_FASTSNAP_REQUIRE_BOTH_BACKENDS_FOR_VERIFY", "0")
        result = subprocess.run([str(script)], cwd=project_root, env=env, check=False)
        if result.returncode == 0:
            log("published Fast Artifact Sync V2 seed is ready")
        else:
            log(f"seed builder failed rc={result.returncode}; current published seed was left in place if present")
        return result.returncode


if __name__ == "__main__":
    sys.exit(main())
