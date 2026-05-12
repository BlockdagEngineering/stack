#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


SCRIPT = Path(__file__).with_name("release-readiness-check.py")
SPEC = importlib.util.spec_from_file_location("release_readiness_check", SCRIPT)
assert SPEC and SPEC.loader
readiness = importlib.util.module_from_spec(SPEC)
sys.modules["release_readiness_check"] = readiness
SPEC.loader.exec_module(readiness)


class MockRPCHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        method = body["method"]
        result = {
            "getNodeInfo": {"ID": "self-node", "network": "mainnet", "connections": 4},
            "getTemplateHealth": {
                "mineable_now": True,
                "submit_ready": True,
                "reason_code": "ok",
            },
            "getPeerInfo": [
                {
                    "id": "self-node",
                    "address": "/ip4/10.0.0.1/tcp/8150/p2p/self-node",
                    "active": True,
                    "state": True,
                },
                {
                    "id": "loopback",
                    "address": "/ip4/127.0.0.1/tcp/8150/p2p/loopback",
                    "active": True,
                    "state": True,
                },
                {
                    "id": "inactive",
                    "address": "/ip4/10.0.0.9/tcp/8150/p2p/inactive",
                    "active": False,
                    "state": True,
                },
                {
                    "id": "good",
                    "address": "/ip4/52.8.80.249/tcp/8150/p2p/good",
                    "active": True,
                    "state": True,
                },
            ],
            "getBlockTemplate": {
                "height": 42,
                "previousblockhash": "abcd",
                "txroot": "tx",
                "stateroot": "state",
                "coinbase_address": "0x0000000000000000000000000000000000000000",
                "pow_diff_reference": {"nbits": "1d00ffff"},
            },
        }[method]
        encoded = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": result})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded.encode("utf-8"))

    def log_message(self, fmt: str, *args: object) -> None:
        return


class ReadinessCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), MockRPCHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.rpc_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join(timeout=2)

    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            env_file="/nonexistent",
            rpc_url=self.rpc_url,
            rpc_user="test",
            rpc_pass="test",
            timeout=2.0,
            min_peers=1,
            pow_type=10,
            mining_address="",
            skip_postgres=True,
            postgres_service="postgres",
            pg_url=None,
            schema_file=None,
            json=False,
        )

    def test_readiness_passes_with_sane_peer_and_template(self) -> None:
        results = readiness.run_checks(self.args())
        self.assertTrue(all(result.ok for result in results), results)
        peer_result = next(result for result in results if result.name == "peer_sanity")
        self.assertIn("1 sane peers", peer_result.detail)

    def test_peer_gate_fails_when_minimum_exceeds_filtered_peers(self) -> None:
        args = self.args()
        args.min_peers = 2
        results = readiness.run_checks(args)
        peer_result = next(result for result in results if result.name == "peer_sanity")
        self.assertFalse(peer_result.ok)
        self.assertIn("need 2", peer_result.detail)

    def test_empty_peer_address_is_not_treated_as_loopback(self) -> None:
        self.assertFalse(readiness.is_loopback_or_unspecified(""))
        self.assertTrue(readiness.is_loopback_or_unspecified("127.0.0.1"))

    def test_rpc_timeout_returns_clean_check_error(self) -> None:
        with mock.patch.object(readiness.urllib.request, "urlopen", side_effect=TimeoutError()):
            with self.assertRaises(readiness.CheckError) as ctx:
                readiness.rpc_call(self.rpc_url, "test", "test", "getNodeInfo", timeout=0.1)
        self.assertIn("getNodeInfo timed out after 0.1s", str(ctx.exception))

    def test_run_checks_continues_when_get_node_info_is_unavailable(self) -> None:
        args = self.args()
        args.min_peers = 0

        def fake_rpc_call(url, user, password, method, params=None, timeout=5.0):
            if method == "getNodeInfo":
                raise readiness.CheckError("getNodeInfo timed out after 5.0s")
            if method == "getTemplateHealth":
                return {"mineable_now": True, "submit_ready": True}
            if method == "getPeerInfo":
                return []
            if method == "getBlockTemplate":
                return {
                    "height": 42,
                    "previousblockhash": "abcd",
                    "txroot": "tx",
                    "stateroot": "state",
                    "coinbase_address": "0x0000000000000000000000000000000000000000",
                    "pow_diff_reference": {"nbits": "1d00ffff"},
                }
            raise AssertionError(method)

        with mock.patch.object(readiness, "rpc_call", side_effect=fake_rpc_call):
            results = readiness.run_checks(args)
        node_result = next(result for result in results if result.name == "node_rpc")
        self.assertTrue(node_result.ok)
        self.assertTrue(node_result.skipped)
        self.assertTrue(all(result.ok for result in results), results)


if __name__ == "__main__":
    unittest.main()
