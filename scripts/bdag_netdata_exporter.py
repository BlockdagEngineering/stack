#!/usr/bin/env python3
"""Expose BlockDAG node (JSON-RPC) + mining pool (HTTP) metrics for Netdata go.d Prometheus scraper."""
import argparse
import base64
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib import error, request
from urllib.parse import urlparse, urlunparse

# Bumped when exporter exposition changes — curl /metrics and search for bdag_exporter_info.
EXPORTER_VERSION = "3"

def _default_evm_rpc_url(bdag_url):
    """If BDAG RPC is on 38131, default EVM JSON-RPC to same host:18545 (see pool-stack-node.http)."""
    try:
        p = urlparse(bdag_url)
        if not p.scheme or not p.hostname:
            return ""
        if p.port == 38131:
            return urlunparse((p.scheme, f"{p.hostname}:18545", p.path or "", "", "", ""))
    except Exception:
        pass
    return ""


def _flatten(prefix, value, out):
    if isinstance(value, dict):
        for key, child in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            _flatten(next_prefix, child, out)
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            _flatten(f"{prefix}.{idx}", child, out)
    else:
        out[prefix.lower()] = value


def _to_flat_map(payload):
    out = {}
    _flatten("", payload, out)
    return out


def _first_number(flat_map, candidates):
    for key in candidates:
        value = flat_map.get(key.lower())
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                pass
    return None


def _eth_quantity_to_float(result):
    """Parse eth_blockNumber-style hex string or int to float for Prometheus."""
    if result is None:
        return None
    if isinstance(result, bool):
        return None
    if isinstance(result, (int, float)):
        return float(result)
    if isinstance(result, str):
        s = result.strip()
        if s.startswith(("0x", "0X")):
            try:
                return float(int(s, 16))
            except ValueError:
                return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _first_bool(flat_map, candidates):
    for key in candidates:
        value = flat_map.get(key.lower())
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if isinstance(value, (int, float)):
            return 1.0 if float(value) != 0 else 0.0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("true", "yes", "1", "synced"):
                return 1.0
            if normalized in ("false", "no", "0", "syncing"):
                return 0.0
    return None


class Exporter:
    def __init__(self, pool_api_url, node_rpc_url, rpc_user, rpc_pass, timeout, evm_rpc_url=""):
        self.pool_api_url = pool_api_url
        self.node_rpc_url = node_rpc_url
        self.rpc_user = rpc_user
        self.rpc_pass = rpc_pass
        self.timeout = timeout
        ev = (evm_rpc_url or "").strip()
        self.evm_rpc_url = ev or _default_evm_rpc_url(node_rpc_url)

    def _get_json(self, url):
        req = request.Request(url, headers={"Accept": "application/json"})
        with request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _rpc(self, method, params=None, url=None, omit_auth=False):
        body = {
            "jsonrpc": "2.0",
            "id": int(time.time()),
            "method": method,
            "params": params if params is not None else [],
        }
        data = json.dumps(body).encode("utf-8")
        target = url or self.node_rpc_url
        req = request.Request(
            target,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if self.rpc_user and not omit_auth:
            token = f"{self.rpc_user}:{self.rpc_pass or ''}".encode("utf-8")
            auth = base64.b64encode(token).decode("ascii")
            req.add_header("Authorization", f"Basic {auth}")
        with request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if "error" in payload and payload["error"] is not None:
            raise RuntimeError(str(payload["error"]))
        return payload.get("result", {})

    def collect_stable(self):
        """Always return the same metric names (Netdata-friendly stable chart set)."""
        now = time.time()
        out = {
            "bdag_pool_up": 0.0,
            "bdag_pool_hashrate": 0.0,
            "bdag_pool_workers": 0.0,
            "bdag_pool_shares_accepted": 0.0,
            "bdag_pool_shares_rejected": 0.0,
            "bdag_pool_shares_stale": 0.0,
            "bdag_pool_shares_invalid": 0.0,
            "bdag_pool_seconds_since_last_block": 0.0,
            "bdag_node_rpc_up": 0.0,
            "bdag_node_block_height": 0.0,
            "bdag_node_is_synced": 0.0,
            "bdag_node_peer_count": 0.0,
        }
        labels = {
            "network": os.getenv("BDAG_NETWORK", "unknown"),
            "version": os.getenv("BDAG_NODE_VERSION", "unknown"),
        }

        pool_flat = {}
        try:
            pool_payload = self._get_json(self.pool_api_url)
            pool_flat = _to_flat_map(pool_payload)
            out["bdag_pool_up"] = 1.0
        except (error.URLError, error.HTTPError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
            pass

        h = _first_number(
            pool_flat,
            ["hashrate", "pool.hashrate", "stats.hashrate", "poolstats.hashrate"],
        )
        if h is not None:
            out["bdag_pool_hashrate"] = h
        w = _first_number(
            pool_flat,
            ["workers", "pool.workers", "stats.workers", "miners", "activeworkers"],
        )
        if w is not None:
            out["bdag_pool_workers"] = w
        for key, field in [
            ("bdag_pool_shares_accepted", ["shares.accepted", "acceptedshares", "stats.accepted", "accepted"]),
            ("bdag_pool_shares_rejected", ["shares.rejected", "rejectedshares", "stats.rejected", "rejected"]),
            ("bdag_pool_shares_stale", ["shares.stale", "staleshares", "stats.stale", "stale"]),
            ("bdag_pool_shares_invalid", ["shares.invalid", "invalidshares", "stats.invalid", "invalid"]),
        ]:
            v = _first_number(pool_flat, field)
            if v is not None:
                out[key] = v
        block_ts = _first_number(
            pool_flat,
            ["lastblocktime", "last_block_time", "stats.lastblocktime", "lastblock.timestamp"],
        )
        if block_ts is not None:
            out["bdag_pool_seconds_since_last_block"] = max(0.0, now - block_ts)

        rpc_ok = False
        node_flat = {}
        network_info = {}
        for method in ("getBlockDagInfo", "getInfo", "getNetworkInfo"):
            try:
                payload = self._rpc(method)
                if method == "getNetworkInfo" and isinstance(payload, dict):
                    network_info = payload
                if isinstance(payload, dict):
                    node_flat.update(_to_flat_map(payload))
                rpc_ok = True
            except Exception:
                continue
        try:
            height = self._rpc("getBlockCount")
            if isinstance(height, (int, float)):
                node_flat["blockcount"] = height
            elif isinstance(height, str):
                try:
                    node_flat["blockcount"] = float(height)
                except ValueError:
                    pass
            rpc_ok = True
        except Exception:
            pass

        out["bdag_node_rpc_up"] = 1.0 if rpc_ok else 0.0
        nh = _first_number(
            node_flat,
            ["blockcount", "blocks", "tipheight", "bestblockheight", "virtualdaa.score"],
        )
        if nh is not None:
            out["bdag_node_block_height"] = nh

        evm_bh = None
        for rpc_try in (self.evm_rpc_url, self.node_rpc_url):
            if not rpc_try:
                continue
            for omit_auth in (False, True):
                if omit_auth and not self.rpc_user:
                    break
                try:
                    bn = self._rpc("eth_blockNumber", url=rpc_try, omit_auth=omit_auth)
                    evm_bh = _eth_quantity_to_float(bn)
                    if evm_bh is not None:
                        break
                except Exception:
                    continue
            if evm_bh is not None:
                break

        # Synced: prefer eth_syncing on EVM HTTP (:18545). BDAG RPC (:38131) is a different
        # listener; isCurrent there often fails or is absent, which left the default 0 (wrong).
        synced_val = None
        for rpc_try in (self.evm_rpc_url, self.node_rpc_url):
            if synced_val is not None:
                break
            if not rpc_try:
                continue
            for omit_auth in (False, True):
                if omit_auth and not self.rpc_user:
                    break
                try:
                    es = self._rpc("eth_syncing", url=rpc_try, omit_auth=omit_auth)
                    if es is False:
                        synced_val = 1.0
                    elif isinstance(es, dict) and es:
                        synced_val = 0.0
                    elif es is True:
                        synced_val = 0.0
                except Exception:
                    continue
                if synced_val is not None:
                    break
            if synced_val is not None:
                break
        if synced_val is None:
            try:
                is_cur = self._rpc("isCurrent")
                if isinstance(is_cur, bool):
                    synced_val = 1.0 if is_cur else 0.0
                elif isinstance(is_cur, str):
                    synced_val = (
                        1.0 if is_cur.strip().lower() in ("true", "1", "yes") else 0.0
                    )
                elif isinstance(is_cur, (int, float)):
                    synced_val = 1.0 if float(is_cur) != 0 else 0.0
            except Exception:
                pass
        if synced_val is None:
            ibd = node_flat.get("isinitialblockdownload")
            ibd_true = False
            if isinstance(ibd, bool):
                ibd_true = ibd
            elif isinstance(ibd, (int, float)):
                ibd_true = float(ibd) != 0.0
            elif isinstance(ibd, str):
                ibd_true = ibd.strip().lower() in ("true", "1", "yes")
            if ibd_true:
                synced_val = 0.0
            else:
                ns = _first_bool(node_flat, ["issynced", "synced", "sync.is_synced"])
                if ns is not None:
                    synced_val = ns
                else:
                    # Only treat explicit "syncing" as not synced; absence of keys is unknown.
                    if _first_bool(node_flat, ["is_syncing"]) == 1.0:
                        synced_val = 0.0
        if synced_val is not None:
            out["bdag_node_is_synced"] = synced_val

        np = _first_number(
            node_flat,
            [
                "totalconnected",
                "totalpeers",
                "connections",
                "peers",
                "network.peers",
                "peercount",
            ],
        )
        if np is not None:
            out["bdag_node_peer_count"] = np

        if network_info:
            labels["network"] = str(network_info.get("chain") or labels["network"])
            labels["version"] = str(
                network_info.get("subversion")
                or network_info.get("version")
                or labels["version"]
            )

        rows = []
        for k in sorted(out.keys()):
            rows.append((k, out[k]))
            if k == "bdag_node_block_height" and evm_bh is not None:
                rows.append(("bdag_node_block_height", evm_bh, {"layer": "evm"}))
        rows.append(("bdag_exporter_info", 1.0, {"version": EXPORTER_VERSION}))
        rows.append(("bdag_node_info", 1.0, labels))
        return rows


def _fmt_labels(labels):
    if not labels:
        return ""
    parts = [f'{k}="{str(v).replace(chr(34), "")}"' for k, v in labels.items()]
    return "{" + ",".join(parts) + "}"


def render_prometheus(rows):
    meta = {
        "bdag_pool_up": ("gauge", "Pool API reachable (1=yes)."),
        "bdag_pool_hashrate": ("gauge", "Pool hashrate from API."),
        "bdag_pool_workers": ("gauge", "Active workers/miners."),
        "bdag_pool_shares_accepted": (
            "gauge",
            "Cumulative accepted shares from pool /stats (counter; resets if pool restarts).",
        ),
        "bdag_pool_shares_rejected": (
            "gauge",
            "Cumulative rejected shares from pool /stats (counter; resets if pool restarts).",
        ),
        "bdag_pool_shares_stale": (
            "gauge",
            "Cumulative stale share events from pool /stats (counter; resets if pool restarts).",
        ),
        "bdag_pool_shares_invalid": ("gauge", "Invalid shares (same semantics as other share metrics)."),
        "bdag_pool_seconds_since_last_block": (
            "gauge",
            "Seconds since last successful pool block submit (from pool /stats last_block_time).",
        ),
        "bdag_node_rpc_up": ("gauge", "Node JSON-RPC reachable (1=yes)."),
        "bdag_node_block_height": (
            "gauge",
            "DAG height (unlabeled); EVM chain head is bdag_node_block_height{layer=\"evm\"} via eth_blockNumber.",
        ),
        "bdag_exporter_info": (
            "gauge",
            "Exporter build — check version label matches repo EXPORTER_VERSION after deploy/rebuild.",
        ),
        "bdag_node_is_synced": (
            "gauge",
            "1 if eth_syncing is false (EVM :18545) or isCurrent; else IBD / issynced / is_syncing heuristics.",
        ),
        "bdag_node_peer_count": ("gauge", "Peers: getNetworkInfo totalconnected / totalpeers when present."),
        "bdag_node_info": ("gauge", "Labeled network and version."),
    }
    lines = []
    seen = set()
    for item in rows:
        if len(item) == 2:
            name, value = item
            labels = {}
        else:
            name, value, labels = item
        if name in meta and name not in seen:
            t, h = meta[name]
            lines.append(f"# HELP {name} {h}")
            lines.append(f"# TYPE {name} {t}")
            seen.add(name)
        lines.append(f"{name}{_fmt_labels(labels)} {value}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="BlockDAG Netdata / Prometheus exporter")
    parser.add_argument("--listen-address", default=os.getenv("BDAG_EXPORTER_LISTEN", "127.0.0.1"))
    parser.add_argument("--listen-port", type=int, default=int(os.getenv("BDAG_EXPORTER_PORT", "9198")))
    parser.add_argument("--pool-api-url", default=os.getenv("POOL_API_URL", "http://127.0.0.1:8080/stats"))
    parser.add_argument("--node-rpc-url", default=os.getenv("BDAG_RPC_URL", "http://127.0.0.1:38131"))
    parser.add_argument(
        "--evm-rpc-url",
        default=os.getenv("BDAG_EVM_RPC_URL", ""),
        help="EVM JSON-RPC for eth_syncing (default: same host as BDAG RPC but port 18545).",
    )
    parser.add_argument("--rpc-user", default=os.getenv("BDAG_RPC_USER", ""))
    parser.add_argument("--rpc-password", default=os.getenv("BDAG_RPC_PASSWORD", ""))
    parser.add_argument("--timeout-seconds", type=float, default=2.5)
    args = parser.parse_args()

    exporter = Exporter(
        pool_api_url=args.pool_api_url,
        node_rpc_url=args.node_rpc_url,
        rpc_user=args.rpc_user,
        rpc_pass=args.rpc_password,
        timeout=args.timeout_seconds,
        evm_rpc_url=args.evm_rpc_url,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/metrics", "/metrics/"):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"not found\n")
                return
            body = render_prometheus(exporter.collect_stable()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = HTTPServer((args.listen_address, args.listen_port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
