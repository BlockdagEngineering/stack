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

    def test_compose_dashboard_targets_stack_container_names(self) -> None:
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("BDAG_NODE_SERVICES: node", compose)
        self.assertIn("BDAG_NETWORK: ${BDAG_NETWORK:-${NETWORK:-mainnet}}", compose)
        self.assertIn("BDAG_STACK_SERVICES: postgres,node,pool", compose)
        self.assertIn("BDAG_POOL_CONTAINER: pool", compose)
        self.assertIn("BDAG_POOL_DB_CONTAINER: postgres", compose)
        self.assertIn("BDAG_NODE_RPC_URLS: node=http://node:38131", compose)
        self.assertIn("DASHBOARD_EVM_RPC_URL: http://node:18545", compose)
        self.assertNotIn("BDAG_RPC_URL: http://bdag-miner-node-1:38131", compose)

    def test_host_dashboard_env_uses_host_reachable_chain_rpc(self) -> None:
        installer = (ROOT_DIR / "ops" / "install-dashboard.sh").read_text(encoding="utf-8")
        portable_env = (ROOT_DIR / "ops" / "portable.env.example").read_text(encoding="utf-8")

        self.assertIn("BDAG_NODE_RPC_URLS=node=http://127.0.0.1:38131", installer)
        self.assertIn("BDAG_GLOBAL_CHAIN_RPC_URLS=node=http://127.0.0.1:38131", installer)
        self.assertIn(
            'migrate_legacy_env_value BDAG_NODE_RPC_URLS "node=http://node:38131" "node=http://127.0.0.1:38131"',
            installer,
        )
        self.assertIn("BDAG_NODE_RPC_URLS=node=http://127.0.0.1:38131", portable_env)
        self.assertIn("BDAG_GLOBAL_CHAIN_RPC_URLS=node=http://127.0.0.1:38131", portable_env)
        self.assertNotIn("NODE_RPC_URLS=http://node:38131", portable_env)

    def test_compose_protects_temp_paths_from_overlay_io(self) -> None:
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertGreaterEqual(compose.count("/var/tmp:size=${BDAG_CONTAINER_TMPFS_SIZE:-128m},mode=1777"), 4)
        self.assertGreaterEqual(compose.count("TMPDIR: /tmp"), 5)
        self.assertGreaterEqual(compose.count("TMP: /tmp"), 5)
        self.assertGreaterEqual(compose.count("TEMP: /tmp"), 5)

    def test_live_deploy_copy_contract_covers_live_validator_files(self) -> None:
        deploy = (ROOT_DIR / "ops" / "deploy-live-runtime-update.sh").read_text(encoding="utf-8")
        validator = (ROOT_DIR / "scripts" / "validate-pi5-restart-hardening.sh").read_text(encoding="utf-8")
        files_match = re.search(r"FILES=\((.*?)\n\)", deploy, re.DOTALL)
        self.assertIsNotNone(files_match)
        deploy_files = set(re.findall(r'"([^"]+)"', files_match.group(1)))
        ignored = {
            ".env.cpu.example",
            ".github/workflows/build-cpu.yml",
            ".github/workflows/build.yml",
            ".github/workflows/rc-hardening.yml",
            "ops/monitor-fastsync-peers.sh",
            "docker-compose.yml",
            "scripts/check-doc-consistency.py",
            "scripts/release/installers/install-unix-common.sh",
            "scripts/release/installers/install-windows.ps1",
        }
        required = {
            rel
            for rel in re.findall(r'need_file "([^"]+)"', validator)
            if rel not in ignored and not rel.startswith(".github/")
        }

        self.assertEqual([], sorted(required - deploy_files))

    def test_live_runtime_validator_requires_current_runtime_surfaces(self) -> None:
        validator = (ROOT_DIR / "scripts" / "validate-pi5-restart-hardening.sh").read_text(encoding="utf-8")

        self.assertIn('if [[ "$mode" == "source" && -e "$root/ops/observability" ]]; then', validator)
        self.assertIn('need_grep \'POOL_SUBMIT_RPC_URLS: .*POOL_SUBMIT_RPC_URLS\' "docker-compose.yml"', validator)
        self.assertIn('need_grep \'NODE_RPC_URLS: .*http://node:38131\' "docker-compose.yml"', validator)
        self.assertIn('need_grep \'BDAG_STACK_SERVICES=postgres,node,pool\' ".env.example"', validator)
        self.assertIn('reject_grep \'container_name:\' "docker-compose.yml"', validator)

    def test_live_runtime_validator_keeps_release_packaging_source_only(self) -> None:
        validator = (ROOT_DIR / "scripts" / "validate-pi5-restart-hardening.sh").read_text(encoding="utf-8")

        self.assertRegex(
            validator,
            r'if \[\[ "\$mode" == "source" \]\]; then\n'
            r'(?:  need_grep .*\n)+'
            r'  need_grep .if ! command -v jq. "ops/monitor-fastsync-peers.sh"\n'
            r'fi',
        )
        self.assertIn('need_grep \'BDAG_FASTSYNC_PEER_ORDERING=p2p-latency\' ".env.cpu.example"', validator)
        self.assertIn('reject_grep \'BDAG_P2P_LAN_PEERS=\' ".env.cpu.example"', validator)

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
