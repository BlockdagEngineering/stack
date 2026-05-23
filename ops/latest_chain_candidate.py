#!/usr/bin/env python3
"""Select the newest restore-safe BlockDAG chain-data candidate.

This helper is intentionally read-only. It records whether a newer local chain
copy is safe enough to consider during recovery, but it never moves live node
data or starts/stops services by itself.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(os.environ.get("BDAG_PROJECT_ROOT", Path.cwd())).resolve()
RUNTIME_DIR = Path(os.environ.get("BDAG_RUNTIME_DIR", PROJECT_ROOT / "ops" / "runtime")).resolve()
STATE_FILE = RUNTIME_DIR / "latest-chain-candidate-state.json"

DEFAULT_CANDIDATE_DIRS = [
    PROJECT_ROOT / "data",
    PROJECT_ROOT / "data-restore",
    PROJECT_ROOT / "data-restore" / "hourly",
    PROJECT_ROOT / "snapshots",
]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def manifest_restore_safe(manifest: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return whether the manifest is restore-safe and why.

    Policy phrase for release validators: prefer the newest chain data only
    after the manifest is restore-safe. Unsafe warm copies are recorded so the
    recovery path can reject unsafe warm copies instead of repeatedly retrying
    them against live nodes.
    """

    reasons: list[str] = []
    explicit = manifest.get("restore_safe")
    if explicit is False:
        reasons.append("manifest restore_safe=false")
    if manifest.get("degraded") is True:
        reasons.append("manifest degraded=true")
    if manifest.get("unsafe") is True:
        reasons.append("manifest unsafe=true")
    if manifest.get("partial") is True:
        reasons.append("manifest partial=true")

    height = manifest_height(manifest)
    if height <= 0:
        reasons.append("missing positive height/main_order")

    root = str(manifest.get("genesis_hash") or manifest.get("network") or manifest.get("chain") or "").strip()
    if not root:
        reasons.append("missing network/genesis identity")

    if explicit is True and not reasons:
        return True, []
    if explicit is None and not reasons:
        return True, []
    return False, reasons or ["manifest did not prove restore safety"]


def manifest_height(manifest: dict[str, Any]) -> int:
    for key in (
        "main_order",
        "mainOrder",
        "height",
        "block_height",
        "blockHeight",
        "latest_block",
        "latestBlock",
    ):
        value = manifest.get(key)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def candidate_from_manifest(path: Path) -> dict[str, Any]:
    manifest = load_json(path) or {}
    safe, reasons = manifest_restore_safe(manifest)
    base_name = path.name.removesuffix(".manifest.json")
    base = path.with_name(base_name)
    if not base.exists() and base_name.endswith(".tar.gz"):
        base = path.with_name(base_name.removesuffix(".tar.gz"))
    return {
        "manifest": str(path),
        "path": str(base),
        "path_exists": base.exists(),
        "height": manifest_height(manifest),
        "restore_safe": safe and base.exists(),
        "unsafe_reasons": ([] if safe else reasons) + ([] if base.exists() else ["candidate payload missing"]),
        "generated_at": manifest.get("generated_at") or manifest.get("created_at") or "",
        "kind": "manifest",
    }


def discover_manifests(roots: list[Path]) -> list[Path]:
    manifests: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        if root.is_file() and root.name.endswith(".manifest.json"):
            paths = [root]
        else:
            paths = list(root.rglob("*.manifest.json"))
        for path in paths:
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                manifests.append(resolved)
    return manifests


def build_state(roots: list[Path]) -> dict[str, Any]:
    candidates = [candidate_from_manifest(path) for path in discover_manifests(roots)]
    candidates.sort(key=lambda item: (int(item.get("height") or 0), item.get("generated_at") or "", item.get("path") or ""), reverse=True)
    safe_candidates = [item for item in candidates if item.get("restore_safe")]
    unsafe_candidates = [item for item in candidates if not item.get("restore_safe")]
    selected = safe_candidates[0] if safe_candidates else None
    return {
        "generated_at": now_iso(),
        "project_root": str(PROJECT_ROOT),
        "state_file": str(STATE_FILE),
        "policy": "prefer the newest chain data only after the manifest is restore-safe; reject unsafe warm copies",
        "candidate_roots": [str(path) for path in roots],
        "selected": selected,
        "decision": "newest_safe_candidate_available" if selected else "no_restore_safe_candidate",
        "safe_candidate_count": len(safe_candidates),
        "unsafe_candidate_count": len(unsafe_candidates),
        "candidates": candidates[:50],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", action="append", default=[], help="Directory or manifest path to scan. May be repeated.")
    parser.add_argument("--write-state", action="store_true", help=f"Write {STATE_FILE}.")
    args = parser.parse_args()

    roots = [Path(item).expanduser().resolve() for item in args.candidate_root]
    if not roots:
        roots = DEFAULT_CANDIDATE_DIRS

    state = build_state(roots)
    if args.write_state:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(json.dumps(state, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
