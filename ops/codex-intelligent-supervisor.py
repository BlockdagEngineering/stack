#!/usr/bin/env python3
"""Run Codex as an intelligent mining-stack supervisor.

This is intentionally separate from deterministic watchdogs.  Watchdogs repair
known failure modes cheaply; this supervisor periodically gives Codex a compact
health packet so it can diagnose unfamiliar degradations, create backlog items,
patch source/config when appropriate, and leave durable evidence.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


HOME = Path(os.environ.get("HOME", "/home/jeremy"))
POOL_ROOT = Path(os.environ.get("BDAG_POOL_ROOT", HOME / "blockdag-asic-pool"))
RUNTIME_DIR = Path(os.environ.get("BDAG_RUNTIME_DIR", POOL_ROOT / "ops/runtime"))
SUPERVISOR_DIR = Path(os.environ.get("BDAG_CODEX_SUPERVISOR_DIR", RUNTIME_DIR / "codex-intelligent-supervisor"))
DASHBOARD_STATUS_URL = os.environ.get("BDAG_CODEX_STATUS_URL", "http://127.0.0.1:8088/api/status")
POOL_METRICS_URL = os.environ.get("BDAG_CODEX_POOL_METRICS_URL", "http://127.0.0.1:9092/metrics")
CODEX_BIN = os.environ.get("BDAG_CODEX_BIN", "/usr/bin/codex")
CODEX_MODEL = os.environ.get("BDAG_CODEX_MODEL", "gpt-5.5")
CODEX_TIMEOUT_SECONDS = int(os.environ.get("BDAG_CODEX_TIMEOUT_SECONDS", "2700"))
LOG_FILE = SUPERVISOR_DIR / "runs.jsonl"
LOCK_FILE = SUPERVISOR_DIR / "supervisor.lock"
PROMPT_FILE = SUPERVISOR_DIR / "latest-prompt.txt"
OUTPUT_FILE = SUPERVISOR_DIR / "latest-output.txt"
STATUS_FILE = SUPERVISOR_DIR / "latest-status.json"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def fetch_json(url: str, timeout: float = 20) -> tuple[dict[str, Any] | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - local operator endpoint.
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        return payload if isinstance(payload, dict) else None, ""
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return None, str(exc)


def fetch_text(url: str, timeout: float = 5) -> tuple[str, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - local operator endpoint.
            return response.read().decode("utf-8", errors="replace"), ""
    except (OSError, urllib.error.URLError) as exc:
        return "", str(exc)


def label_value(metric_name: str, label: str) -> str:
    marker = f'{label}="'
    start = metric_name.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = metric_name.find('"', start)
    return metric_name[start:end] if end >= start else ""


def parse_pool_metrics(text: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "active_connections": None,
        "job_health_ok": None,
        "ready_miners": None,
        "selected_backends": [],
        "backend_scores": {},
        "submit_outcomes": {},
    }
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value_text = line.partition(" ")
        try:
            value = float(value_text)
        except ValueError:
            continue
        if name.startswith("pool_active_connections"):
            metrics["active_connections"] = value
        elif name.startswith("pool_job_health_ok"):
            metrics["job_health_ok"] = value
        elif name.startswith("pool_job_health_ready_miners"):
            metrics["ready_miners"] = value
        elif name.startswith("pool_rpc_backend_selected") and value == 1:
            backend = label_value(name, "backend")
            if backend:
                metrics["selected_backends"].append(backend)
        elif name.startswith("pool_rpc_backend_score"):
            backend = label_value(name, "backend")
            if backend:
                metrics["backend_scores"][backend] = value
        elif name.startswith("pool_block_submit_outcomes_total"):
            outcome = label_value(name, "outcome") or "unknown"
            reason = label_value(name, "reason") or "unknown"
            metrics["submit_outcomes"][f"{outcome}:{reason}"] = value
    return metrics


def systemd_user_states() -> dict[str, str]:
    units = [
        "bdag-watchdog.service",
        "bdag-dashboard.service",
        "bdag-p2p-guard.service",
        "bdag-miner-15min-supervisor.timer",
        "bdag-stack-sentinel.timer",
        "bdag-chain-restore-guard.timer",
        "bdag-codex-intelligent-supervisor.timer",
    ]
    states: dict[str, str] = {}
    for unit in units:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            text=True,
            capture_output=True,
            timeout=4,
            check=False,
        )
        states[unit] = (proc.stdout or proc.stderr).strip() or f"exit-{proc.returncode}"
    return states


def compact_dashboard_status(status: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(status, dict):
        return None
    pool_metrics = status.get("pool_metrics") if isinstance(status.get("pool_metrics"), dict) else {}
    miner_health = status.get("miner_health") if isinstance(status.get("miner_health"), dict) else {}
    sync = status.get("sync_progress") if isinstance(status.get("sync_progress"), dict) else {}
    return {
        "generated_at": status.get("generated_at"),
        "overall": status.get("overall") or status.get("status"),
        "status_reason": status.get("status_reason") or "",
        "failures": status.get("failures") or [],
        "warnings": status.get("warnings") or [],
        "miner_failures": status.get("miner_failures") or [],
        "stack_failures": status.get("stack_failures") or [],
        "connected_miners": miner_health.get("connected_count") or pool_metrics.get("active_connections"),
        "sync_status": sync.get("status"),
        "sync_percent": sync.get("percent"),
        "pool_status": pool_metrics.get("status"),
        "selected_backend": pool_metrics.get("selected_backend"),
        "template_backend_state": pool_metrics.get("template_backend_state") or {},
        "block_submit_outcomes": pool_metrics.get("block_submit_outcomes") or {},
    }


def recent_incidents(limit: int = 30) -> list[dict[str, Any]]:
    path = RUNTIME_DIR / "logs/incidents.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def health_snapshot() -> dict[str, Any]:
    status, status_error = fetch_json(DASHBOARD_STATUS_URL, timeout=25)
    metrics_text, metrics_error = fetch_text(POOL_METRICS_URL, timeout=6)
    return {
        "generated_at": now_iso(),
        "dashboard_status": compact_dashboard_status(status),
        "dashboard_status_error": status_error,
        "pool_metrics": parse_pool_metrics(metrics_text) if metrics_text else {},
        "pool_metrics_error": metrics_error,
        "systemd_user": systemd_user_states(),
        "recent_incidents": recent_incidents(20),
    }


def build_prompt(snapshot: dict[str, Any]) -> str:
    return f"""You are Codex running as the 15-minute intelligent BlockDAG mining supervisor.

Read local runbooks before acting:
- /home/jeremy/AGENTS.md
- /home/jeremy/codex-memory/AGENTS.md
- /home/jeremy/codex-memory/context/current-context.html
- /home/jeremy/blockdag-asic-pool/ops/runtime/current-stack-memory.md
- /home/jeremy/blockdag-asic-pool/MINING_OPTIMIZATION_HANDOFF.md
- /home/jeremy/blockdag-asic-pool/AGENTS.md

Mission:
1. Keep the mining pool producing paid accepted blocks.
2. If health is OK, do not restart or reconfigure anything; write a concise heartbeat/analysis and exit.
3. If degraded, diagnose from dashboard/API/logs/metrics before action.
4. Prefer permanent source/config fixes over watchdog-only workarounds.
5. Do not restart mining services, nodes, Docker, or ASICs unless evidence says it is necessary.
6. If a source bug or missing mitigation cannot be fixed safely now, add a prioritized backlog item.
7. If changing code/config/scripts, test it, commit it, and push it under established Gitflow rules.
8. If an incident or repair happens, create/update an HTML report under /home/jeremy/blockdag-asic-pool/ops/runtime/reports.
9. Keep output concise and include exact absolute paths for artifacts.

Health packet:

```json
{json.dumps(snapshot, indent=2, sort_keys=True, default=str)}
```
"""


def append_log(row: dict[str, Any]) -> None:
    SUPERVISOR_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def run_codex(prompt: str) -> dict[str, Any]:
    PROMPT_FILE.write_text(prompt, encoding="utf-8")
    OUTPUT_FILE.write_text("", encoding="utf-8")
    cmd = [
        CODEX_BIN,
        "exec",
        "--cd",
        str(HOME),
        "--skip-git-repo-check",
        "--sandbox",
        "danger-full-access",
        "-m",
        CODEX_MODEL,
        "-c",
        'approval_policy="never"',
        "-c",
        'model_reasoning_effort="xhigh"',
        "-c",
        'model_verbosity="low"',
        "-o",
        str(OUTPUT_FILE),
        "-",
    ]
    started = time.time()
    proc = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        cwd=str(HOME),
        capture_output=True,
        timeout=CODEX_TIMEOUT_SECONDS,
        check=False,
    )
    return {
        "exit_code": proc.returncode,
        "duration_seconds": round(time.time() - started, 3),
        "stdout_tail": proc.stdout[-8000:],
        "stderr_tail": proc.stderr[-8000:],
        "output_file": str(OUTPUT_FILE),
    }


def main() -> int:
    SUPERVISOR_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            row = {"generated_at": now_iso(), "status": "skipped", "reason": "another supervisor run is active"}
            append_log(row)
            STATUS_FILE.write_text(json.dumps(row, indent=2, sort_keys=True), encoding="utf-8")
            print(json.dumps(row, indent=2, sort_keys=True))
            return 0

        snapshot = health_snapshot()
        started = {"generated_at": now_iso(), "status": "started", "snapshot": snapshot}
        append_log(started)
        try:
            result = run_codex(build_prompt(snapshot))
            row = {"generated_at": started["generated_at"], "completed_at": now_iso(), "status": "completed", "codex": result, "snapshot": snapshot}
        except subprocess.TimeoutExpired as exc:
            row = {
                "generated_at": started["generated_at"],
                "completed_at": now_iso(),
                "status": "timeout",
                "timeout_seconds": CODEX_TIMEOUT_SECONDS,
                "stdout_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
                "stderr_tail": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
                "snapshot": snapshot,
            }
        STATUS_FILE.write_text(json.dumps(row, indent=2, sort_keys=True, default=str), encoding="utf-8")
        append_log(row)
        print(json.dumps(row, indent=2, sort_keys=True, default=str))
        return 0 if row.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
