#!/bin/sh
set -eu

log() {
  printf '[%s] collector-entrypoint: %s\n' "$(date -Is)" "$*" >&2
}

export BDAG_PROJECT_ROOT="${BDAG_PROJECT_ROOT:-/workspace}"
export BDAG_RUNTIME_DIR="${BDAG_RUNTIME_DIR:-/var/lib/bdag-collector/runtime}"
export BDAG_POOL_ENV_FILE="${BDAG_POOL_ENV_FILE:-$BDAG_PROJECT_ROOT/.env}"
export BDAG_COLLECTOR_BIND="${BDAG_COLLECTOR_BIND:-0.0.0.0}"
export BDAG_COLLECTOR_PORT="${BDAG_COLLECTOR_PORT:-9280}"

collector_pythonpath=""
add_pythonpath_dir() {
  if [ -d "$1" ]; then
    collector_pythonpath="${collector_pythonpath}${collector_pythonpath:+:}$1"
  fi
}

add_pythonpath_dir /opt/collector/ops
add_pythonpath_dir "$BDAG_PROJECT_ROOT/ops"
if [ -n "$collector_pythonpath" ]; then
  export PYTHONPATH="$collector_pythonpath${PYTHONPATH:+:$PYTHONPATH}"
fi

mkdir -p "$BDAG_RUNTIME_DIR"

app_dir=/opt/collector
app=/opt/collector/collector.py
if [ ! -f "$app" ] && [ -f /opt/collector/ops/collector.py ]; then
  app_dir=/opt/collector/ops
  app=/opt/collector/ops/collector.py
fi
if [ ! -f "$app" ]; then
  log "collector.py not found in collector checkout"
  find /opt/collector -maxdepth 2 -type f | sort >&2 || true
  exit 1
fi

if [ ! -S /var/run/docker.sock ]; then
  log "warning: /var/run/docker.sock is not mounted; container status and logs will be limited"
fi

log "starting collector from $app"
cd "$app_dir"
exec python3 - "$app" <<'PY'
from pathlib import Path
import runpy
import sys

BDAG_CHILD_EXECUTABLES = {"bdag", "blockdag-node"}


def command_is_bdag_child(command: str) -> bool:
    parts = command.split()
    if not parts:
        return False
    executable_name = Path(parts[0]).name
    if executable_name in BDAG_CHILD_EXECUTABLES:
        return True
    if executable_name == "rosetta" or executable_name.startswith("qemu-"):
        wrapped_executable_name = Path(parts[1]).name if len(parts) > 1 else ""
        return wrapped_executable_name in BDAG_CHILD_EXECUTABLES
    return False


def bdag_child_running_from_top(top: str) -> bool:
    for line in top.splitlines()[1:]:
        parts = line.split(None, 7)
        command = parts[7] if len(parts) >= 8 else line
        if command_is_bdag_child(command):
            return True
    return False


try:
    import pool_ops

    pool_ops.BDAG_CHILD_EXECUTABLES = BDAG_CHILD_EXECUTABLES
    pool_ops.command_is_bdag_child = command_is_bdag_child
    pool_ops.bdag_child_running_from_top = bdag_child_running_from_top

    def enrich_status_peer_counts(payload):
        if not isinstance(payload, dict) or not hasattr(pool_ops, "container_peer_ips"):
            return payload
        node_names = payload.get("node_services") or payload.get("managed_node_services") or list((payload.get("nodes") or {}).keys())
        peer_sources = {}
        peer_errors = {}
        for node in node_names:
            node_name = str(node or "").strip()
            if not node_name:
                continue
            try:
                ips = list(pool_ops.container_peer_ips(node_name))
                error = ""
            except Exception as exc:  # noqa: BLE001 - collector status should degrade, not fail.
                ips = []
                error = str(exc)
            peer_sources[node_name] = ips
            if error:
                peer_errors[node_name] = error
            nodes = payload.setdefault("nodes", {})
            if isinstance(nodes, dict):
                info = nodes.setdefault(node_name, {})
                if isinstance(info, dict):
                    info["peer_count"] = len(ips)
                    info["p2p_connections"] = len(ips)
                    info["peer_ips_sample"] = ips[:12]
                    info["p2p_connections_error"] = error

        peer_ips = []
        for ips in peer_sources.values():
            for ip in ips:
                if ip not in peer_ips:
                    peer_ips.append(ip)
        peer_count = len(peer_ips)
        error_text = "; ".join(f"{node}: {error}" for node, error in peer_errors.items())
        payload["peer_count"] = peer_count
        payload["p2p_connections"] = peer_count
        payload["peer_sources"] = peer_sources
        payload["peer_ips_sample"] = peer_ips[:12]
        payload["p2p_connections_error"] = error_text
        sync_progress = payload.get("sync_progress")
        if isinstance(sync_progress, dict):
            sync_progress["peer_count"] = peer_count
            sync_progress["p2p_connections"] = peer_count
            sync_progress["p2p_connections_error"] = error_text
            progress_nodes = sync_progress.get("nodes")
            if isinstance(progress_nodes, dict):
                for node, ips in peer_sources.items():
                    progress = progress_nodes.get(node)
                    if isinstance(progress, dict):
                        progress["peer_count"] = len(ips)
                        progress["p2p_connections"] = len(ips)
                        progress["p2p_connections_error"] = peer_errors.get(node, "")
        return payload

    original_collect_status = pool_ops.collect_status
    original_collect_status_cached = pool_ops.collect_status_cached

    def collect_status_with_peer_counts(*args, **kwargs):
        return enrich_status_peer_counts(original_collect_status(*args, **kwargs))

    def collect_status_cached_with_peer_counts(*args, **kwargs):
        return enrich_status_peer_counts(original_collect_status_cached(*args, **kwargs))

    pool_ops.collect_status = collect_status_with_peer_counts
    pool_ops.collect_status_cached = collect_status_cached_with_peer_counts
except Exception as exc:
    print(f"collector-entrypoint: warning: could not patch node child detector: {exc}", file=sys.stderr)

runpy.run_path(sys.argv[1], run_name="__main__")
PY
