#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class DeploymentPortabilityTests(unittest.TestCase):
    def test_node_child_detection_accepts_packaged_binary_name(self) -> None:
        top = """UID PID PPID C STIME TTY TIME CMD
root 1 0 0 07:45 ? 00:00:00 runuser -u bdagStack -g bdagStack -- /usr/local/bin/nodeworker
dnsmasq 64 55 0 07:45 ? 00:00:00 /usr/local/bin/blockdag-node --configfile /etc/bdagStack/node.conf
"""

        self.assertTrue(pool_ops.bdag_child_running_from_top(top))

    def test_node_child_detection_keeps_legacy_bdag_binary_name(self) -> None:
        top = """UID PID PPID C STIME TTY TIME CMD
bdag 64 55 0 07:45 ? 00:00:00 /usr/local/bin/bdag --configfile /etc/bdagStack/node.conf
"""

        self.assertTrue(pool_ops.bdag_child_running_from_top(top))

    def test_node_child_detection_does_not_count_wrapper_only(self) -> None:
        top = """UID PID PPID C STIME TTY TIME CMD
root 1 0 0 07:45 ? 00:00:00 runuser -u bdagStack -g bdagStack -- /usr/local/bin/nodeworker
dnsmasq 55 1 0 07:45 ? 00:00:00 /usr/local/bin/nodeworker --node-binary=/usr/local/bin/blockdag-node
"""

        self.assertFalse(pool_ops.bdag_child_running_from_top(top))

    def test_fetch_text_url_uses_python_http_client_not_host_curl(self) -> None:
        captured: dict[str, object] = {}

        class FakeHeaders:
            def get_content_charset(self) -> str:
                return "utf-8"

        class FakeResponse:
            headers = FakeHeaders()

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return b"pool_active_connections 0\n"

        def fake_urlopen(request: object, timeout: float) -> FakeResponse:
            captured["url"] = getattr(request, "full_url", "")
            captured["timeout"] = timeout
            return FakeResponse()

        def forbidden_subprocess_run(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("fetch_text_url must not require host curl")

        old_urlopen = pool_ops.urllib.request.urlopen
        old_run = pool_ops.subprocess.run
        try:
            pool_ops.urllib.request.urlopen = fake_urlopen
            pool_ops.subprocess.run = forbidden_subprocess_run
            text = pool_ops.fetch_text_url("http://127.0.0.1:9090/metrics", {"accept": "text/plain"}, timeout=2.5)
        finally:
            pool_ops.urllib.request.urlopen = old_urlopen
            pool_ops.subprocess.run = old_run

        self.assertEqual(text, "pool_active_connections 0\n")
        self.assertEqual(captured["url"], "http://127.0.0.1:9090/metrics")
        self.assertEqual(captured["timeout"], 2.5)


if __name__ == "__main__":
    unittest.main()
