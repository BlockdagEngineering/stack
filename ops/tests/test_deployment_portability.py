#!/usr/bin/env python3

import pathlib
import re
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

    def test_live_deploy_copy_contract_covers_live_validator_files(self) -> None:
        deploy = (ROOT_DIR / "ops" / "deploy-live-runtime-update.sh").read_text(encoding="utf-8")
        validator = (ROOT_DIR / "scripts" / "validate-pi5-restart-hardening.sh").read_text(encoding="utf-8")
        files_match = re.search(r"FILES=\((.*?)\n\)", deploy, re.DOTALL)
        self.assertIsNotNone(files_match)
        deploy_files = set(re.findall(r'"([^"]+)"', files_match.group(1)))
        ignored = {
            ".github/workflows/rc-hardening.yml",
            "docker-compose.yml",
            "scripts/check-doc-consistency.py",
        }
        required = {
            rel
            for rel in re.findall(r'need_file "([^"]+)"', validator)
            if rel not in ignored and not rel.startswith(".github/")
        }

        self.assertEqual([], sorted(required - deploy_files))

    def test_live_runtime_validator_allows_legacy_runtime_surfaces(self) -> None:
        validator = (ROOT_DIR / "scripts" / "validate-pi5-restart-hardening.sh").read_text(encoding="utf-8")

        self.assertIn('if [[ "$mode" == "source" && -e "$root/ops/observability" ]]; then', validator)
        self.assertIn('need_grep \'POOL_SUBMIT_RPC_URLS: .*POOL_SUBMIT_RPC_URLS\' "docker-compose.yml"', validator)
        self.assertRegex(
            validator,
            r'if \[\[ "\$mode" == "live-runtime" \]\]; then\n'
            r'.*NODE_RPC_URLS: \.\*http://\(node\|rpc-failover\):38131.*\n'
            r'else\n'
            r'.*NODE_RPC_URLS: \.\*http://node:38131.*\n'
            r'.*POOL_SUBMIT_RPC_URLS: \.\*POOL_SUBMIT_RPC_URLS.*\n'
            r'fi',
        )

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
