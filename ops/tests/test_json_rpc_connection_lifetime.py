#!/usr/bin/env python3

import json
import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class FakeResponse:
    status = 200
    reason = "OK"

    def __init__(self) -> None:
        self.closed = False

    def read(self, _limit: int) -> bytes:
        return json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0x2a"}).encode()

    def close(self) -> None:
        self.closed = True


class FakeHTTPConnection:
    instances: list["FakeHTTPConnection"] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.closed = False
        self.response = FakeResponse()
        self.request_headers: dict[str, str] = {}
        FakeHTTPConnection.instances.append(self)

    def request(self, _method: str, _path: str, body: bytes, headers: dict[str, str]) -> None:
        self.body = body
        self.request_headers = headers

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class JsonRpcConnectionLifetimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_http_connection = pool_ops.http.client.HTTPConnection
        FakeHTTPConnection.instances = []
        pool_ops.http.client.HTTPConnection = FakeHTTPConnection
        self.addCleanup(self.restore)

    def restore(self) -> None:
        pool_ops.http.client.HTTPConnection = self.original_http_connection

    def test_json_rpc_call_closes_response_and_connection(self) -> None:
        result = pool_ops.json_rpc_call("http://node:18545", "eth_blockNumber", [], timeout=2.0)

        self.assertEqual(result, "0x2a")
        connection = FakeHTTPConnection.instances[-1]
        self.assertTrue(connection.response.closed)
        self.assertTrue(connection.closed)
        self.assertEqual(connection.request_headers["connection"], "close")


if __name__ == "__main__":
    unittest.main()
