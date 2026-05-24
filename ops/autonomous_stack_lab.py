#!/usr/bin/env python3
"""Guarded autonomous stack experiment runner for the local BlockDAG pool.

The runner alternates known stack variants, records savepoints, measures each
phase, and rolls back immediately when live health checks show mining damage.
It is intentionally conservative about source-built images: only the WebSocket
pool source is treated as deployable here because its base revision matches the
known WebSocket release image.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import fcntl
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from factorial_stack_test import (
    NODES,
    append_jsonl,
    current_rpc_primary,
    db_block_counts,
    iso,
    log_counts,
    read_jsonl,
    scan_window,
    snapshot_context,
    utc_now,
    write_json,
)
from pool_log_summary import summarize_logs
from pool_ops import RUNTIME_DIR, collect_status, now_iso
from stack_ab_test import NEW_NODE_IMAGE, NEW_POOL_IMAGE, OLD_NODE_IMAGE, OLD_POOL_IMAGE, set_env_value
from watchdog import run_rpc_failover_switch


ROOT = Path(__file__).resolve().parents[1]
POOL_ENV = ROOT / "asic-pool" / ".env"
ROOT_ENV = ROOT / ".env"
HAPROXY_CFG = ROOT / "haproxy.cfg"
POOL_SOURCE = Path("/home/jeremy/blockdag-source/pool")
ASIC_POOL_SOURCE = Path("/home/jeremy/blockdag-source/asic-pool")
LOCK_FILE = RUNTIME_DIR / "autonomous-stack-lab.lock"
LATEST_FILE = RUNTIME_DIR / "latest-autonomous-stack-lab-dir.txt"

DEFAULT_CANDIDATE_IMAGE = "bdag-local/asic-pool:template-sequence-guard-20260509-031501"
WS_BASE_REVISION = "ae55a65c34e39a1047469b40daace7b508d31c8b"
CORE_CONTAINERS = ("asic-pool", "bdag-miner-node-1", "bdag-miner-node-2", "pool-db", "rpc-failover")


def run(
    cmd: list[str],
    *,
    cwd: Path = ROOT,
    check: bool = True,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )


def log(run_dir: Path, message: str) -> None:
    line = f"{now_iso()} {message}"
    print(line, flush=True)
    with (run_dir / "runner.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def current_images() -> dict[str, str]:
    images: dict[str, str] = {}
    for name in CORE_CONTAINERS:
        proc = run(["docker", "inspect", "-f", "{{.Config.Image}}", name], check=False, timeout=15)
        images[name] = proc.stdout.strip() if proc.returncode == 0 else ""
    return images


def current_variant(variants: dict[str, dict[str, str]]) -> str:
    images = current_images()
    pool_image = images.get("asic-pool")
    node1_image = images.get("bdag-miner-node-1")
    node2_image = images.get("bdag-miner-node-2")
    for name, variant in variants.items():
        if pool_image == variant["pool_image"] and node1_image == variant["node_image"] and node2_image == variant["node_image"]:
            return name
    return "unknown"


def other_primary(primary: str) -> str:
    return "bdag-miner-node-2" if primary == "bdag-miner-node-1" else "bdag-miner-node-1"


def normalize_primary(primary: str | None) -> str:
    return primary if primary in NODES else "bdag-miner-node-1"


def inspect_binary_revision_from_image(image: str) -> str:
    with tempfile.TemporaryDirectory(prefix="bdag-pool-bin-") as tmp:
        tmp_path = Path(tmp)
        cid = run(["docker", "create", image], timeout=60).stdout.strip()
        try:
            target = tmp_path / "pool"
            proc = run(["docker", "cp", f"{cid}:/usr/local/bin/pool", str(target)], check=False, timeout=60)
            if proc.returncode != 0:
                run(["docker", "cp", f"{cid}:/usr/local/bin/mining-pool", str(target)], timeout=60)
            version = run(["go", "version", "-m", str(target)], timeout=30).stdout
        finally:
            run(["docker", "rm", cid], check=False, timeout=30)
    for line in version.splitlines():
        line = line.strip()
        if line.startswith("build\tvcs.revision="):
            return line.split("=", 1)[1].strip()
    return ""


def git_head(repo: Path) -> str:
    return run(["git", "rev-parse", "HEAD"], cwd=repo, timeout=30).stdout.strip()


def git_status(repo: Path) -> str:
    return run(["git", "status", "--short", "--branch"], cwd=repo, timeout=30).stdout.strip()


def is_ancestor(repo: Path, ancestor: str, descendant: str = "HEAD") -> bool:
    proc = run(["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=repo, check=False, timeout=30)
    return proc.returncode == 0


def image_exists(image: str) -> bool:
    return run(["docker", "image", "inspect", image], check=False, timeout=30).returncode == 0


def build_candidate_image(run_dir: Path, image: str) -> None:
    log(run_dir, f"building candidate pool image {image} from {POOL_SOURCE}")
    env = {"GOMAXPROCS": "2"}
    test_proc = run(
        ["nice", "-n", "5", "ionice", "-c2", "-n5", "go", "test", "./..."],
        cwd=POOL_SOURCE,
        timeout=900,
        env=env,
    )
    (run_dir / "candidate-go-test.log").write_text(test_proc.stdout, encoding="utf-8")
    build_proc = run(
        ["nice", "-n", "5", "ionice", "-c2", "-n5", "go", "build", "-o", "bin/pool", "./cmd/pool"],
        cwd=POOL_SOURCE,
        timeout=900,
        env=env,
    )
    (run_dir / "candidate-go-build.log").write_text(build_proc.stdout, encoding="utf-8")

    build_dir = run_dir / "candidate-image-build"
    build_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(POOL_SOURCE / "bin" / "pool", build_dir / "pool")
    (build_dir / "Dockerfile").write_text(
        "\n".join(
            [
                f"FROM {NEW_POOL_IMAGE}",
                "COPY pool /usr/local/bin/pool",
                "COPY pool /usr/local/bin/mining-pool",
                'ENTRYPOINT ["/usr/local/bin/pool"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    docker_proc = run(
        ["docker", "build", "-t", image, "."],
        cwd=build_dir,
        timeout=900,
    )
    (run_dir / "candidate-docker-build.log").write_text(docker_proc.stdout, encoding="utf-8")


def preflight_candidate(run_dir: Path, image: str, build_candidate: bool) -> tuple[bool, dict[str, Any]]:
    details: dict[str, Any] = {
        "image": image,
        "source": str(POOL_SOURCE),
        "source_status": "",
        "head": "",
        "base_revision": WS_BASE_REVISION,
        "base_is_ancestor": False,
        "image_revision": "",
        "enabled": False,
        "reason": "",
    }
    try:
        if not POOL_SOURCE.exists():
            details["reason"] = "pool source repo missing"
            return False, details
        details["source_status"] = git_status(POOL_SOURCE)
        details["head"] = git_head(POOL_SOURCE)
        details["base_is_ancestor"] = is_ancestor(POOL_SOURCE, WS_BASE_REVISION)
        if not details["base_is_ancestor"]:
            details["reason"] = "websocket release base is not an ancestor of the source branch"
            return False, details

        if build_candidate:
            build_candidate_image(run_dir, image)
        elif not image_exists(image):
            details["reason"] = "candidate image does not exist and build was disabled"
            return False, details

        details["image_revision"] = inspect_binary_revision_from_image(image)
        if details["image_revision"] != details["head"]:
            details["reason"] = "candidate image revision does not match source HEAD"
            return False, details

        if ASIC_POOL_SOURCE.exists():
            details["non_websocket_source_status"] = git_status(ASIC_POOL_SOURCE)
            details["non_websocket_source_note"] = (
                "not deployed by this runner: local non-websocket source is not source-equivalent "
                "to the live rollback image"
            )
        details["enabled"] = True
        details["reason"] = "source base and image revision verified"
        return True, details
    except Exception as exc:  # noqa: BLE001 - failed preflight disables only the candidate.
        details["reason"] = f"preflight failed: {exc}"
        return False, details


def savepoint(run_dir: Path, variants: dict[str, dict[str, str]]) -> None:
    save_dir = run_dir / "savepoint"
    save_dir.mkdir(parents=True, exist_ok=True)
    for source, name in ((POOL_ENV, "asic-pool.env"), (ROOT_ENV, "root.env"), (HAPROXY_CFG, "haproxy.cfg")):
        if source.exists():
            shutil.copy2(source, save_dir / name)
    run(["docker", "compose", "config"], timeout=60).stdout and (save_dir / "docker-compose.config.yml").write_text(
        run(["docker", "compose", "config"], timeout=60).stdout,
        encoding="utf-8",
    )
    inspect = run(["docker", "inspect", *CORE_CONTAINERS], check=False, timeout=60)
    (save_dir / "core-containers.inspect.json").write_text(inspect.stdout, encoding="utf-8")
    image_names = sorted({variant["pool_image"] for variant in variants.values()} | {variant["node_image"] for variant in variants.values()})
    image_inspect = run(["docker", "image", "inspect", *image_names], check=False, timeout=60)
    (save_dir / "images.inspect.json").write_text(image_inspect.stdout, encoding="utf-8")
    try:
        write_json(save_dir / "initial-status.json", collect_status(include_logs=True))
    except Exception as exc:  # noqa: BLE001
        write_json(save_dir / "initial-status-error.json", {"error": str(exc), "generated_at": now_iso()})
    write_json(save_dir / "initial-log-summary-15m.json", summarize_logs("15m"))
    write_json(save_dir / "initial-images.json", current_images())


def compose_up(service: str, run_dir: Path) -> dict[str, Any]:
    started = utc_now()
    proc = run(
        ["docker", "compose", "--env-file", "asic-pool/.env", "up", "-d", "--no-deps", service],
        check=False,
        timeout=180,
    )
    payload = {
        "service": service,
        "started_utc": iso(started),
        "finished_utc": iso(utc_now()),
        "returncode": proc.returncode,
        "output": (proc.stdout or "")[-4000:],
    }
    append_jsonl(run_dir / "compose-actions.jsonl", payload)
    if proc.returncode != 0:
        raise RuntimeError(f"docker compose up failed for {service}: {(proc.stdout or '')[-1000:]}")
    return payload


def apply_variant(run_dir: Path, variant_name: str, variants: dict[str, dict[str, str]]) -> dict[str, Any]:
    variant = variants[variant_name]
    before = current_images()
    started = utc_now()
    for env_file in (POOL_ENV, ROOT_ENV):
        set_env_value(env_file, "POOL_IMAGE", variant["pool_image"])
        set_env_value(env_file, "BLOCKDAG_NODE_IMAGE", variant["node_image"])

    steps: list[dict[str, Any]] = []
    node_change = (
        before.get("bdag-miner-node-1") != variant["node_image"]
        or before.get("bdag-miner-node-2") != variant["node_image"]
    )
    pool_change = before.get("asic-pool") != variant["pool_image"]

    if node_change:
        for service, wait_seconds in (("bdag-miner-node-2", 20), ("bdag-miner-node-1", 20)):
            log(run_dir, f"switching {service} to {variant['node_image']}")
            steps.append(compose_up(service, run_dir))
            time.sleep(wait_seconds)
    if node_change or pool_change:
        log(run_dir, f"switching asic-pool to {variant['pool_image']}")
        steps.append(compose_up("asic-pool", run_dir))
        time.sleep(20)
    else:
        log(run_dir, f"variant {variant_name} already active")

    after = current_images()
    payload = {
        "variant": variant_name,
        "started_utc": iso(started),
        "finished_utc": iso(utc_now()),
        "before": before,
        "after": after,
        "steps": steps,
    }
    append_jsonl(run_dir / "switches.jsonl", payload)
    return payload


def apply_primary(run_dir: Path, target_primary: str, phase_number: int) -> dict[str, Any]:
    before = current_rpc_primary()
    payload: dict[str, Any] = {"target_primary": target_primary, "primary_before": before, "skipped": before == target_primary}
    if before != target_primary:
        log(run_dir, f"phase {phase_number}: switching RPC primary to {target_primary}")
        ok = run_rpc_failover_switch(target_primary, f"autonomous stack lab phase {phase_number}")
        payload.update({"skipped": False, "ok": ok, "primary_after": current_rpc_primary()})
    return payload


def container_health() -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for name in ("asic-pool", "bdag-miner-node-1", "bdag-miner-node-2", "pool-db", "rpc-failover"):
        proc = run(
            ["docker", "inspect", "-f", "{{.State.Status}} {{.State.Restarting}} {{.RestartCount}}", name],
            check=False,
            timeout=15,
        )
        parts = proc.stdout.strip().split()
        payload[name] = {
            "ok": proc.returncode == 0 and len(parts) >= 3 and parts[0] == "running" and parts[1] == "false",
            "raw": proc.stdout.strip(),
        }
    return payload


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value)


def capture_pool_logs(
    run_dir: Path,
    label: str,
    since_iso: str,
    until_iso: str | None = None,
    directory: str = "failure-logs",
) -> Path:
    command = ["docker", "logs", "--since", since_iso]
    if until_iso:
        command.extend(["--until", until_iso])
    command.append("asic-pool")
    proc = run(command, check=False, timeout=90)
    path = run_dir / directory / f"{safe_name(label)}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(proc.stdout or "", encoding="utf-8", errors="replace")
    return path


def health_check(grace_seconds: int, phase_started: dt.datetime) -> tuple[bool, dict[str, Any]]:
    age = (utc_now() - phase_started).total_seconds()
    reasons: list[str] = []
    details: dict[str, Any] = {
        "generated_at": now_iso(),
        "phase_age_seconds": round(age, 1),
        "in_grace": age < grace_seconds,
        "container_health": container_health(),
    }
    for name, item in details["container_health"].items():
        if not item.get("ok"):
            reasons.append(f"{name} is not running cleanly: {item.get('raw')}")

    try:
        status = collect_status(include_logs=True)
    except Exception as exc:  # noqa: BLE001
        status = {"overall": "unknown", "error": str(exc)}
        reasons.append(f"collect_status failed: {exc}")
    details["status"] = {
        "overall": status.get("overall"),
        "status_reason": status.get("status_reason"),
        "miner_health": status.get("miner_health"),
        "pool": status.get("pool"),
        "sync_health": status.get("sync_health"),
    }

    try:
        summary = summarize_logs("2m")
    except Exception as exc:  # noqa: BLE001
        summary = {"error": str(exc)}
        reasons.append(f"log summary failed: {exc}")
    details["log_summary_2m"] = summary

    miner_health = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}
    pool = status.get("pool") if isinstance(status.get("pool"), dict) else {}
    connected = int(miner_health.get("connected_count") or 0)
    if age >= grace_seconds:
        if connected < 6:
            reasons.append(f"connected miner count is {connected}, expected at least 6")
        last_share_age = pool.get("last_valid_share_age_seconds")
        last_job_age = pool.get("last_job_notify_age_seconds")
        if isinstance(last_share_age, (int, float)) and last_share_age > 120:
            reasons.append(f"last valid share age is {last_share_age}s")
        if isinstance(last_job_age, (int, float)) and last_job_age > 120:
            reasons.append(f"last job notify age is {last_job_age}s")
        if pool.get("share_stall"):
            reasons.append("pool reports share stall")
        if pool.get("job_stall"):
            reasons.append("pool reports job stall")
        if pool.get("block_submit_error_storm"):
            reasons.append("pool reports block submit error storm")
        counts = (summary.get("counts") or {}) if isinstance(summary, dict) else {}
        ratios = (summary.get("ratios") or {}) if isinstance(summary, dict) else {}
        if int(counts.get("template_error") or 0) >= 5:
            reasons.append(f"template errors in last 2m: {counts.get('template_error')}")
        if int(counts.get("rpc_refused") or 0) >= 3:
            reasons.append(f"RPC refused errors in last 2m: {counts.get('rpc_refused')}")
        if int(counts.get("block_error") or 0) >= 10 and float(ratios.get("block_errors_per_ok") or 0) > 1.0:
            reasons.append(f"block error ratio high in last 2m: {ratios.get('block_errors_per_ok')}")
        if int(counts.get("too_late") or 0) >= 10 and float(ratios.get("too_late_per_ok") or 0) > 0.8:
            reasons.append(f"too-late ratio high in last 2m: {ratios.get('too_late_per_ok')}")

    details["reasons"] = reasons
    return not reasons, details


def build_schedule(
    phase_count: int,
    variants: list[str],
    initial_variant: str,
    initial_primary: str,
) -> list[dict[str, Any]]:
    variant_order = [initial_variant, *[name for name in variants if name != initial_variant]]
    primary_order = [initial_primary, other_primary(initial_primary)]
    combos = [{"variant": variant, "target_primary": primary} for primary in primary_order for variant in variant_order]
    return [{**combos[index % len(combos)], "phase": index + 1} for index in range(phase_count)]


def phase_metric_rows(run_dir: Path) -> list[dict[str, Any]]:
    return [row for row in read_jsonl(run_dir / "phase-results.jsonl") if row.get("status") == "measured"]


def avg(values: list[float | int | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return round(sum(clean) / len(clean), 6) if clean else None


def aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    by_variant: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        grouped[(str(row.get("variant") or row.get("stack")), str(row.get("target_primary")))].append(row)
        by_variant[str(row.get("variant") or row.get("stack"))].append(row)

    def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "phase_count": len(items),
            "avg_local_chain_share_pct": avg([item.get("local_chain_share_pct") for item in items]),
            "avg_local_blocks_per_hour": avg([item.get("local_blocks_per_hour") for item in items]),
            "avg_db_blocks_per_hour": avg([item.get("db_blocks_per_hour") for item in items]),
            "avg_chain_blocks_per_hour": avg([item.get("chain_blocks_per_hour") for item in items]),
            "avg_submit_errors_per_ok": avg([(item.get("derived") or {}).get("submit_errors_per_ok") for item in items]),
            "avg_too_late_per_ok": avg([(item.get("derived") or {}).get("too_late_per_ok") for item in items]),
            "avg_stale_jobs_per_ok": avg([(item.get("derived") or {}).get("stale_jobs_per_ok") for item in items]),
            "avg_template_errors_per_ok": avg([(item.get("derived") or {}).get("template_errors_per_ok") for item in items]),
            "total_local_blocks": sum(int(item.get("local_blocks") or 0) for item in items),
            "total_db_blocks": sum(int(item.get("db_blocks") or 0) for item in items),
        }

    combo_rows = []
    for (variant, primary), items in sorted(grouped.items()):
        combo_rows.append({"variant": variant, "target_primary": primary, **summarize(items)})
    variant_rows = []
    for variant, items in sorted(by_variant.items()):
        variant_rows.append({"variant": variant, **summarize(items)})
    best = sorted(
        variant_rows,
        key=lambda row: (
            row.get("avg_local_chain_share_pct") is not None,
            row.get("avg_local_chain_share_pct") or -1,
            row.get("avg_db_blocks_per_hour") or -1,
            -(row.get("avg_submit_errors_per_ok") or 0),
            -(row.get("avg_too_late_per_ok") or 0),
        ),
        reverse=True,
    )
    return {
        "generated_at": now_iso(),
        "measured_phase_count": len(rows),
        "by_variant": variant_rows,
        "by_combo": combo_rows,
        "best_variant_so_far": best[0] if best else None,
    }


def render_report(run_dir: Path, summary: dict[str, Any], complete: bool) -> str:
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    lines = [
        "# Autonomous Stack Lab Report",
        "",
        f"Generated: {now_iso()}",
        f"Complete: `{str(complete).lower()}`",
        f"Run directory: `{run_dir}`",
        "",
        "## Design",
        "",
        f"- Target duration: `{config['hours']}` hours.",
        f"- Phase length: `{config['phase_minutes']}` minutes.",
        f"- Warmup excluded from each phase: `{config['warmup_seconds']}` seconds.",
        "- Variants: old rollback image, websocket release image, and source-built websocket sequence guard when preflight passes.",
        "- Rollback rule: any variant that causes miner loss, share/job stalls, template/RPC error storms, or severe submit-error ratios is reverted to the last stable variant.",
        "",
        "## Variant Summary",
        "",
        "| Variant | Phases | Avg Share | Local b/h | DB b/h | Chain b/h | Submit Err/OK | Late/OK | Template/OK | Local Blocks | DB Blocks |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary.get("by_variant") or []:
        lines.append(
            f"| {row.get('variant')} | {row.get('phase_count')} | {row.get('avg_local_chain_share_pct')} | "
            f"{row.get('avg_local_blocks_per_hour')} | {row.get('avg_db_blocks_per_hour')} | "
            f"{row.get('avg_chain_blocks_per_hour')} | {row.get('avg_submit_errors_per_ok')} | "
            f"{row.get('avg_too_late_per_ok')} | {row.get('avg_template_errors_per_ok')} | "
            f"{row.get('total_local_blocks')} | {row.get('total_db_blocks')} |"
        )
    lines.extend(["", "## Combo Summary", ""])
    lines.append("| Variant | Primary | Phases | Avg Share | Local b/h | DB b/h | Submit Err/OK | Late/OK |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in summary.get("by_combo") or []:
        lines.append(
            f"| {row.get('variant')} | {row.get('target_primary')} | {row.get('phase_count')} | "
            f"{row.get('avg_local_chain_share_pct')} | {row.get('avg_local_blocks_per_hour')} | "
            f"{row.get('avg_db_blocks_per_hour')} | {row.get('avg_submit_errors_per_ok')} | {row.get('avg_too_late_per_ok')} |"
        )
    best = summary.get("best_variant_so_far") or {}
    if best:
        lines.extend(["", "## Best So Far", ""])
        lines.append(f"- Variant: `{best.get('variant')}`")
        lines.append(f"- Avg local chain share: `{best.get('avg_local_chain_share_pct')}`")
        lines.append(f"- Avg DB blocks/hour: `{best.get('avg_db_blocks_per_hour')}`")
    lines.extend(
        [
            "",
            "## Files",
            "",
            f"- Config: `{run_dir / 'config.json'}`",
            f"- Savepoint: `{run_dir / 'savepoint'}`",
            f"- Samples: `{run_dir / 'samples.jsonl'}`",
            f"- Phase results: `{run_dir / 'phase-results.jsonl'}`",
            f"- Guard events: `{run_dir / 'guard-events.jsonl'}`",
            f"- Runner log: `{run_dir / 'runner.log'}`",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(run_dir: Path, complete: bool) -> dict[str, Any]:
    summary = aggregate_rows(phase_metric_rows(run_dir))
    write_json(run_dir / "summary.json", summary)
    (run_dir / "report.md").write_text(render_report(run_dir, summary, complete), encoding="utf-8")
    return summary


def choose_best_variant(summary: dict[str, Any], fallback: str, enabled_variants: set[str]) -> str:
    best = summary.get("best_variant_so_far")
    if isinstance(best, dict):
        candidate = str(best.get("variant") or "")
        if candidate in enabled_variants:
            return candidate
    return fallback


def restore_variant_and_primary(
    run_dir: Path,
    variant_name: str,
    primary: str,
    variants: dict[str, dict[str, str]],
    reason: str,
) -> None:
    log(run_dir, f"restoring variant={variant_name} primary={primary}: {reason}")
    apply_variant(run_dir, variant_name, variants)
    if current_rpc_primary() != primary:
        run_rpc_failover_switch(primary, reason)


def monitor_phase(
    run_dir: Path,
    phase_record: dict[str, Any],
    phase_end: dt.datetime,
    sample_interval_seconds: int,
    guard_grace_seconds: int,
) -> tuple[bool, dict[str, Any] | None]:
    next_sample = utc_now()
    consecutive_bad = 0
    while utc_now() < phase_end:
        if (run_dir / "STOP").exists():
            log(run_dir, "STOP file detected; ending after current partial phase")
            return False, {"reason": "STOP", "generated_at": now_iso()}
        now = utc_now()
        if now >= next_sample:
            sample = {**phase_record, **snapshot_context(), "images": current_images()}
            append_jsonl(run_dir / "samples.jsonl", sample)
            ok, health = health_check(guard_grace_seconds, dt.datetime.fromisoformat(phase_record["phase_started_utc"]))
            append_jsonl(run_dir / "health-checks.jsonl", {**phase_record, **health, "ok": ok})
            if ok:
                consecutive_bad = 0
            else:
                consecutive_bad += 1
                append_jsonl(run_dir / "guard-events.jsonl", {**phase_record, **health, "consecutive_bad": consecutive_bad})
                log(run_dir, f"guard warning phase {phase_record['phase']}: {'; '.join(health.get('reasons') or [])}")
                if consecutive_bad >= 2:
                    return False, health
            next_sample = now + dt.timedelta(seconds=sample_interval_seconds)
        time.sleep(min(15, max(1, (phase_end - utc_now()).total_seconds())))
    return True, None


def current_db_context() -> dict[str, Any]:
    end_ts = int(time.time())
    start_ts = end_ts - 300
    try:
        return db_block_counts(start_ts, end_ts)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=10.0)
    parser.add_argument("--phase-minutes", type=int, default=40)
    parser.add_argument("--warmup-seconds", type=int, default=180)
    parser.add_argument("--sample-interval-seconds", type=int, default=60)
    parser.add_argument("--guard-grace-seconds", type=int, default=150)
    parser.add_argument("--candidate-image", default=DEFAULT_CANDIDATE_IMAGE)
    parser.add_argument("--build-candidate", action="store_true")
    parser.add_argument("--baseline-name", default="old")
    parser.add_argument("--baseline-pool-image", default=OLD_POOL_IMAGE)
    parser.add_argument("--baseline-node-image", default=OLD_NODE_IMAGE)
    parser.add_argument(
        "--candidate-only",
        action="store_true",
        help="test only the stable old stack and the source candidate, skipping the stock websocket release",
    )
    parser.add_argument("--final-policy", choices=["best", "initial", "leave"], default="best")
    parser.add_argument("--run-dir", type=Path, default=None)
    args = parser.parse_args()

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_FILE.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"another autonomous stack lab is already running; lock={LOCK_FILE}", file=sys.stderr)
        return 2

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = args.run_dir or RUNTIME_DIR / f"autonomous-stack-lab-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    LATEST_FILE.write_text(str(run_dir) + "\n", encoding="utf-8")

    variants: dict[str, dict[str, str]] = {
        args.baseline_name: {"pool_image": args.baseline_pool_image, "node_image": args.baseline_node_image},
    }
    if not args.candidate_only:
        variants["websocket"] = {"pool_image": NEW_POOL_IMAGE, "node_image": NEW_NODE_IMAGE}
    candidate_enabled, candidate_details = preflight_candidate(run_dir, args.candidate_image, args.build_candidate)
    if candidate_enabled:
        variants["websocket-seqguard"] = {"pool_image": args.candidate_image, "node_image": NEW_NODE_IMAGE}

    initial_variant = current_variant(variants)
    if initial_variant == "unknown":
        initial_variant = "old"
    initial_primary = normalize_primary(current_rpc_primary())
    enabled_variants = set(variants)
    phase_count = max(1, math.floor(args.hours * 60 / args.phase_minutes))
    schedule = build_schedule(phase_count, list(variants), initial_variant, initial_primary)

    config = {
        "created_at": now_iso(),
        "hours": args.hours,
        "phase_minutes": args.phase_minutes,
        "phase_count": phase_count,
        "warmup_seconds": args.warmup_seconds,
        "sample_interval_seconds": args.sample_interval_seconds,
        "guard_grace_seconds": args.guard_grace_seconds,
        "final_policy": args.final_policy,
        "initial_variant": initial_variant,
        "initial_primary": initial_primary,
        "variants": variants,
        "candidate_preflight": candidate_details,
        "schedule": schedule,
        "initial_db_context_5m": current_db_context(),
    }
    write_json(run_dir / "config.json", config)
    savepoint(run_dir, variants)
    log(run_dir, f"starting autonomous stack lab: run_dir={run_dir}")
    log(run_dir, f"enabled variants: {', '.join(variants)}")
    if not candidate_enabled:
        log(run_dir, f"candidate disabled: {candidate_details.get('reason')}")

    complete = False
    fatal_error = ""
    last_stable_variant = initial_variant
    last_stable_primary = initial_primary
    disabled_variants: set[str] = set()
    try:
        for phase in schedule:
            variant_name = str(phase["variant"])
            if variant_name in disabled_variants:
                log(run_dir, f"phase {phase['phase']}: skipping disabled variant {variant_name}")
                continue
            phase_started = utc_now()
            phase_record = {
                **phase,
                "stack": variant_name,
                "phase_started_utc": iso(phase_started),
                "status": "configuring",
            }
            log(
                run_dir,
                f"phase {phase['phase']}/{phase_count} starting: variant={variant_name} primary={phase['target_primary']}",
            )
            try:
                phase_record["switch"] = apply_variant(run_dir, variant_name, variants)
                phase_record["primary_switch"] = apply_primary(run_dir, str(phase["target_primary"]), int(phase["phase"]))
                configured_at = utc_now()
                phase_end = configured_at + dt.timedelta(minutes=args.phase_minutes)
                measured_start = int(configured_at.timestamp()) + args.warmup_seconds
                measured_end = int(phase_end.timestamp())
                phase_record.update(
                    {
                        "status": "scheduled",
                        "phase_configured_utc": iso(configured_at),
                        "phase_end_utc": iso(phase_end),
                        "measured_start_utc": iso(measured_start),
                        "measured_end_utc": iso(measured_end),
                        "actual_variant_start": current_variant(variants),
                        "actual_primary_start": current_rpc_primary(),
                        "images_start": current_images(),
                    }
                )
                append_jsonl(run_dir / "phase-schedule.jsonl", phase_record)
                phase_ok, guard_failure = monitor_phase(
                    run_dir,
                    phase_record,
                    phase_end,
                    args.sample_interval_seconds,
                    args.guard_grace_seconds,
                )
                actual_end = int(time.time())
                if guard_failure and guard_failure.get("reason") == "STOP":
                    break
                if not phase_ok:
                    disabled = variant_name == "websocket-seqguard"
                    if disabled:
                        disabled_variants.add(variant_name)
                    failure_log = capture_pool_logs(
                        run_dir,
                        f"phase-{phase['phase']}-{variant_name}-guard-rollback",
                        phase_record["phase_started_utc"],
                        iso(time.time()),
                    )
                    error_row = {
                        **phase_record,
                        "status": "guard-rollback",
                        "guard_failure": guard_failure,
                        "failure_log": str(failure_log),
                        "measured_end_utc": iso(actual_end),
                        "actual_variant_end": current_variant(variants),
                        "actual_primary_end": current_rpc_primary(),
                        "disabled_variant": disabled,
                    }
                    append_jsonl(run_dir / "phase-results.jsonl", error_row)
                    restore_variant_and_primary(
                        run_dir,
                        last_stable_variant,
                        last_stable_primary,
                        variants,
                        f"guard rollback from phase {phase['phase']}",
                    )
                    write_outputs(run_dir, complete=False)
                    continue

                result = scan_window(phase_record, measured_start, measured_end)
                phase_log = capture_pool_logs(
                    run_dir,
                    f"phase-{phase['phase']}-{variant_name}-measured",
                    iso(measured_start),
                    iso(measured_end),
                    directory="phase-logs",
                )
                result["status"] = "measured"
                result["variant"] = variant_name
                result["phase_log"] = str(phase_log)
                result["actual_variant_end"] = current_variant(variants)
                result["actual_primary_end"] = current_rpc_primary()
                result["images_end"] = current_images()
                append_jsonl(run_dir / "phase-results.jsonl", result)
                last_stable_variant = variant_name
                last_stable_primary = str(result["actual_primary_end"] or phase["target_primary"])
                summary = write_outputs(run_dir, complete=False)
                log(
                    run_dir,
                    "phase "
                    f"{phase['phase']} measured: share={result.get('local_chain_share_pct')} "
                    f"db_bph={result.get('db_blocks_per_hour')} best={summary.get('best_variant_so_far')}",
                )
            except Exception as exc:  # noqa: BLE001
                error_row = {
                    **phase_record,
                    "status": "failed",
                    "error": str(exc),
                    "failed_at": now_iso(),
                    "actual_variant_end": current_variant(variants),
                    "actual_primary_end": current_rpc_primary(),
                    "images_end": current_images(),
                }
                append_jsonl(run_dir / "phase-results.jsonl", error_row)
                write_outputs(run_dir, complete=False)
                restore_variant_and_primary(
                    run_dir,
                    last_stable_variant,
                    last_stable_primary,
                    variants,
                    f"exception rollback from phase {phase['phase']}",
                )
        else:
            complete = True
    except Exception as exc:  # noqa: BLE001
        fatal_error = str(exc)
        log(run_dir, f"fatal experiment error: {fatal_error}")
    finally:
        summary = write_outputs(run_dir, complete=complete)
        try:
            if args.final_policy == "initial" or fatal_error:
                target_variant = initial_variant
                target_primary = initial_primary
            elif args.final_policy == "best":
                target_variant = choose_best_variant(summary, initial_variant, enabled_variants - disabled_variants)
                target_primary = initial_primary
                combo_rows = summary.get("by_combo") or []
                best_combos = [row for row in combo_rows if row.get("variant") == target_variant]
                if best_combos:
                    best_combo = sorted(
                        best_combos,
                        key=lambda row: (
                            row.get("avg_local_chain_share_pct") is not None,
                            row.get("avg_local_chain_share_pct") or -1,
                            row.get("avg_db_blocks_per_hour") or -1,
                        ),
                        reverse=True,
                    )[0]
                    target_primary = str(best_combo.get("target_primary") or initial_primary)
            else:
                target_variant = current_variant(variants)
                target_primary = current_rpc_primary()
            if args.final_policy != "leave" or fatal_error:
                restore_variant_and_primary(run_dir, target_variant, target_primary, variants, f"final policy {args.final_policy}")
        except Exception as exc:  # noqa: BLE001
            log(run_dir, f"final restore failed: {exc}")
        final_summary = write_outputs(run_dir, complete=complete)
        write_json(
            run_dir / "final-state.json",
            {
                "generated_at": now_iso(),
                "complete": complete,
                "fatal_error": fatal_error,
                "current_variant": current_variant(variants),
                "current_primary": current_rpc_primary(),
                "current_images": current_images(),
                "summary": final_summary,
            },
        )
        if fatal_error:
            (run_dir / "fatal-error.txt").write_text(fatal_error + "\n", encoding="utf-8")
        log(run_dir, f"experiment finished complete={complete} fatal={bool(fatal_error)}")

    print(run_dir)
    return 1 if fatal_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
