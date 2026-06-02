#!/usr/bin/env python3

import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
ROOT_DIR = OPS_DIR.parent
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

    def test_compose_dashboard_targets_stack_container_names(self) -> None:
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("BDAG_NODE_SERVICES: ${BDAG_DASHBOARD_NODE_SERVICES:-node}", compose)
        self.assertIn("BDAG_STACK_SERVICES: ${BDAG_DASHBOARD_STACK_SERVICES:-pool-db,node,pool}", compose)
        self.assertIn("BDAG_POOL_CONTAINER: ${BDAG_DASHBOARD_POOL_CONTAINER:-pool}", compose)
        self.assertIn("BDAG_POOL_DB_CONTAINER: ${BDAG_DASHBOARD_POOL_DB_CONTAINER:-pool-db}", compose)
        self.assertIn("BDAG_NODE_RPC_URLS: ${BDAG_DASHBOARD_NODE_RPC_URLS:-node=http://node:38131}", compose)
        self.assertIn("DASHBOARD_EVM_RPC_URL: ${BDAG_DASHBOARD_EVM_RPC_URL:-http://node:18545}", compose)
        self.assertNotIn("BDAG_RPC_URL: http://bdag-miner-node-1:38131", compose)

    def test_compose_protects_temp_paths_from_overlay_io(self) -> None:
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertGreaterEqual(compose.count("/var/tmp:size=${BDAG_CONTAINER_TMPFS_SIZE:-128m},mode=1777"), 4)
        self.assertGreaterEqual(compose.count("TMPDIR: /tmp"), 5)
        self.assertGreaterEqual(compose.count("TMP: /tmp"), 5)
        self.assertGreaterEqual(compose.count("TEMP: /tmp"), 5)

    def test_live_deploy_rollback_validates_manifest_not_new_rc_contract(self) -> None:
        deploy = (ROOT_DIR / "ops" / "deploy-live-runtime-update.sh").read_text(encoding="utf-8")
        rollback_body = deploy.split("rollback_from_backup()", 1)[1].split("if [[ -n \"$ROLLBACK_DIR\" ]]", 1)[0]

        self.assertIn("validate_rollback_restored", deploy)
        self.assertIn("validate_rollback_restored || die", rollback_body)
        self.assertNotIn("run_target_validation", rollback_body)

    def test_release_installer_defaults_to_zero_miner_sources(self) -> None:
        installer = (ROOT_DIR / "ops" / "release-install.sh").read_text(encoding="utf-8")

        self.assertIn('configure discovered miner sources now?" "n"', installer)

    def test_release_docs_keep_zero_miner_default_invariant(self) -> None:
        agents = (ROOT_DIR / "AGENTS.md").read_text(encoding="utf-8")
        readme = (ROOT_DIR / "README.md").read_text(encoding="utf-8")

        self.assertIn("Fresh installs assume zero miner sources", agents)
        self.assertIn("Fresh installs assume zero miner sources", readme)
        self.assertIn("0..N ASIC or Stratum miners", agents)


if __name__ == "__main__":
    unittest.main()
