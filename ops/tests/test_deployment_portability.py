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

    def test_node_child_detection_accepts_rosetta_wrapped_packaged_binary(self) -> None:
        top = """UID PID PPID C STIME TTY TIME CMD
root 41658 41563 0 16:41 ? 00:00:00 /run/rosetta/rosetta /usr/sbin/runuser runuser -u bdagStack -g bdagStack -- /usr/local/bin/nodeworker --node-binary=/usr/local/bin/blockdag-node
999 41917 41658 0 16:41 ? 00:00:02 /run/rosetta/rosetta /usr/local/bin/nodeworker /usr/local/bin/nodeworker --node-binary=/usr/local/bin/blockdag-node
999 41954 41917 99 16:41 ? 00:04:52 /run/rosetta/rosetta /usr/local/bin/blockdag-node /usr/local/bin/blockdag-node --configfile /etc/bdagStack/node.conf
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

    def test_node_child_detection_does_not_count_rosetta_wrapped_wrapper_only(self) -> None:
        top = """UID PID PPID C STIME TTY TIME CMD
root 41658 41563 0 16:41 ? 00:00:00 /run/rosetta/rosetta /usr/sbin/runuser runuser -u bdagStack -g bdagStack -- /usr/local/bin/nodeworker --node-binary=/usr/local/bin/blockdag-node
999 41917 41658 0 16:41 ? 00:00:02 /run/rosetta/rosetta /usr/local/bin/nodeworker /usr/local/bin/nodeworker --node-binary=/usr/local/bin/blockdag-node
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
        self.assertIn("BDAG_STACK_SERVICES: postgres,node,pool", compose)
        self.assertIn("BDAG_POOL_CONTAINER: pool", compose)
        self.assertIn("BDAG_POOL_DB_CONTAINER: postgres", compose)
        self.assertIn("BDAG_NODE_RPC_URLS: node=http://node:38131", compose)
        self.assertIn("BDAG_GLOBAL_CHAIN_RPC_URLS: node=http://node:38131", compose)
        self.assertIn("BDAG_RPC_URL: http://node:38131", compose)
        self.assertIn("BDAG_COLLECTOR_API: ${BDAG_COLLECTOR_API:-http://collector:9280}", compose)
        self.assertIn("ADDR: ${DASHBOARD_LISTEN:-0.0.0.0:8088}", compose)
        self.assertIn('${DASHBOARD_HOST_PORT:-8088}:8088"', compose)

    def test_release_collector_image_uses_packaged_collector_source(self) -> None:
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        dockerfile = (ROOT_DIR / "dockerfile").read_text(encoding="utf-8")
        dockerfile_dev_path = ROOT_DIR / "dockerfile-dev"
        dockerfile_dev = dockerfile_dev_path.read_text(encoding="utf-8") if dockerfile_dev_path.exists() else ""
        entrypoint = (ROOT_DIR / "docker" / "entrypoint-collector.sh").read_text(encoding="utf-8")
        workflow = (ROOT_DIR / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")

        self.assertIn("Checkout collector repo", workflow)
        self.assertIn("find src/collector -type f -name collector.py", workflow)
        self.assertIn('src/collector/ "${ROOT}/collector/"', workflow)
        self.assertIn('cp "${collector_entry}" "${ROOT}/collector/collector.py"', workflow)
        self.assertIn("Release zip is missing collector.py", workflow)
        self.assertIn("collector_src: ${COLLECTOR_SRC_CONTEXT:-./collector}", compose)
        self.assertRegex(compose, r"watchdog:\n(?:.*\n){0,12}\s+target: watchdog")
        self.assertRegex(compose, r"sentinel:\n(?:.*\n){0,12}\s+target: sentinel")

        def service_block(name: str) -> str:
            tail = compose.split(f"  {name}:", 1)[1]
            block_lines = []
            for line in tail.splitlines()[1:]:
                if line.startswith("  ") and not line.startswith("    ") and line.rstrip().endswith(":"):
                    break
                block_lines.append(line)
            return "\n".join(block_lines)

        watchdog_block = service_block("watchdog")
        sentinel_block = service_block("sentinel")
        self.assertNotIn("collector_src", watchdog_block)
        self.assertNotIn("collector_src", sentinel_block)
        self.assertIn("FROM docker:27-cli AS ops-runtime", dockerfile)
        self.assertIn("FROM ops-runtime AS collector", dockerfile)
        self.assertIn("FROM ops-runtime AS watchdog", dockerfile)
        self.assertIn("FROM ops-runtime AS sentinel", dockerfile)
        self.assertIn("COPY --from=collector_src . /src/collector", dockerfile)
        self.assertIn("COPY --from=collector-source /src/collector /opt/collector", dockerfile)
        self.assertNotIn("git clone --depth 1", dockerfile)
        self.assertNotIn("COPY --from=collector_src . /opt/collector", dockerfile)
        if dockerfile_dev:
            self.assertIn("FROM docker:27-cli AS ops-runtime", dockerfile_dev)
            self.assertIn("FROM ops-runtime AS collector", dockerfile_dev)
            self.assertIn("FROM ops-runtime AS watchdog", dockerfile_dev)
            self.assertIn("FROM ops-runtime AS sentinel", dockerfile_dev)
            self.assertIn("COPY --from=collector_src . /src/collector", dockerfile_dev)
        self.assertLess(
            entrypoint.index("add_pythonpath_dir /opt/collector/ops"),
            entrypoint.index('add_pythonpath_dir "$BDAG_PROJECT_ROOT/ops"'),
        )
        self.assertIn("pool_ops.bdag_child_running_from_top = bdag_child_running_from_top", entrypoint)
        self.assertIn('executable_name == "rosetta"', entrypoint)
        self.assertIn('runpy.run_path(sys.argv[1], run_name="__main__")', entrypoint)

    def test_dashboard_image_uses_checked_out_dashboard_context(self) -> None:
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        dockerfile_dev_path = ROOT_DIR / "dockerfile-dev"
        dockerfile_dev = dockerfile_dev_path.read_text(encoding="utf-8") if dockerfile_dev_path.exists() else ""

        self.assertIn("dashboard_src: ${DASHBOARD_SRC_CONTEXT:-.}", compose)
        if dockerfile_dev:
            self.assertIn("WORKDIR /src/dashboard", dockerfile_dev)
            self.assertIn("COPY --from=dashboard_src . .", dockerfile_dev)


    def test_host_dashboard_env_uses_host_reachable_chain_rpc(self) -> None:
        installer = (ROOT_DIR / "ops" / "install-dashboard.sh").read_text(encoding="utf-8")
        portable_env = (ROOT_DIR / "ops" / "portable.env.example").read_text(encoding="utf-8")

        self.assertIn("BDAG_NODE_RPC_URLS=node=http://127.0.0.1:38131", installer)
        self.assertIn("BDAG_GLOBAL_CHAIN_RPC_URLS=node=http://127.0.0.1:38131", installer)
        self.assertIn("BDAG_NODE_RPC_URLS=node=http://127.0.0.1:38131", portable_env)
        self.assertIn("BDAG_GLOBAL_CHAIN_RPC_URLS=node=http://127.0.0.1:38131", portable_env)
        self.assertNotIn("NODE_RPC_URLS=http://node:38131", portable_env)

    def test_compose_protects_temp_paths_from_overlay_io(self) -> None:
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertGreaterEqual(compose.count("/var/tmp:size=${BDAG_CONTAINER_TMPFS_SIZE:-128m},mode=1777"), 4)
        self.assertIn("cpu_shares: 4096", compose)
        self.assertIn("cpu_shares: 3072", compose)
        self.assertIn("cpu_shares: 256", compose)
        self.assertGreaterEqual(compose.count("TMPDIR: /tmp"), 5)
        self.assertGreaterEqual(compose.count("TMP: /tmp"), 5)
        self.assertGreaterEqual(compose.count("TEMP: /tmp"), 5)

    def test_compose_mounts_configured_persistent_data_paths(self) -> None:
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("postgres-data:/var/lib/postgresql/data", compose)
        self.assertIn("${NODE_DATA_DIR:-node-data}:/var/lib/bdagStack/node", compose)
        self.assertIn("nodeworker-data:/var/lib/bdagStack/nodeworker", compose)
        self.assertIn("  postgres-data:", compose)
        self.assertIn("  node-data:", compose)
        self.assertIn("  nodeworker-data:", compose)

    def test_pool_node_health_defaults_live_in_stack_defaults(self) -> None:
        stack_defaults = (ROOT_DIR / "ops" / "config" / "stack-defaults.env").read_text(encoding="utf-8")

        self.assertEqual(1, stack_defaults.count("POOL_RPC_ROUTER_NODE_HEALTH_ENABLED=true"))

    def test_pool_node_health_gate_is_enabled_by_default(self) -> None:
        stack_defaults = (ROOT_DIR / "ops" / "config" / "stack-defaults.env").read_text(encoding="utf-8")
        env_example = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")
        installer = (ROOT_DIR / "ops" / "release-install.sh").read_text(encoding="utf-8")

        self.assertIn("POOL_RPC_ROUTER_NODE_HEALTH_ENABLED=true", stack_defaults)
        self.assertIn("POOL_RPC_ROUTER_NODE_HEALTH_ENABLED=true", env_example)
        self.assertIn("set_stack_default_env_value .env POOL_RPC_ROUTER_NODE_HEALTH_ENABLED", installer)

    def test_live_deploy_copy_contract_covers_runtime_update_files(self) -> None:
        deploy = (ROOT_DIR / "ops" / "deploy-live-runtime-update.sh").read_text(encoding="utf-8")
        files_match = re.search(r"FILES=\((.*?)\n\)", deploy, re.DOTALL)
        self.assertIsNotNone(files_match)
        deploy_files = set(re.findall(r'"([^"]+)"', files_match.group(1)))

        self.assertIn("ops/release-install.sh", deploy_files)
        self.assertIn("docker/entrypoint-nodeworker.sh", deploy_files)
        self.assertIn("ops/install-p2p-services.sh", deploy_files)
        self.assertFalse(any(rel.endswith("_chain_candidate.py") for rel in deploy_files))

    def test_release_validator_requires_current_runtime_surfaces(self) -> None:
        validator = (ROOT_DIR / "scripts" / "validate-release-build.sh").read_text(encoding="utf-8")

        self.assertIn('need_grep \'^BDAG_CHAIN_DB_ARCHIVE_URL=\' ".env.example"', validator)
        self.assertIn("normal-node-exports/.*/blockdag-normal-node-bootstrap-mainnet-20260623T080429Z-verified", validator)
        self.assertIn('need_grep \'^BDAG_CHAIN_DB_ARCHIVE_MIN_BYTES=1048576$\' ".env.example"', validator)
        self.assertIn('need_grep \'^BDAG_CHAIN_DB_IMPORT_BATCH_BYTES=16777216$\' ".env.example"', validator)
        self.assertIn('need_grep \'snap import --datadir\' "ops/release-install.sh"', validator)
        self.assertIn('need_grep \'tar --zstd -xf\' "ops/release-install.sh"', validator)
        self.assertIn('need_grep \'chain_db_archive_local_path\' "ops/release-install.sh"', validator)
        self.assertIn('need_grep \'BDAG_CHAIN_DB_ARCHIVE_URL\' "ops/release-install.sh"', validator)
        self.assertIn('need_grep \'Existing node chain data was found in \\$node_dir/mainnet/BdagChain; skipping chain DB download.\' "ops/release-install.sh"', validator)
        self.assertIn('need_file "ops/wait_for_node_sync.py"', validator)
        self.assertIn('need_grep \'python3 ops/wait_for_node_sync.py\' "ops/release-install.sh"', validator)
        self.assertIn('need_grep \'python3 ops/wait_for_node_sync.py\' "scripts/release/installers/install-unix-common.sh"', validator)
        self.assertIn('need_grep \'Syncing graph state\' "ops/wait_for_node_sync.py"', validator)
        self.assertIn('need_grep \'"docker", "logs", "-f", "--tail"\' "ops/wait_for_node_sync.py"', validator)
        self.assertIn('need_grep \'docker_logs\\(LOG_CONTAINER, lines=LOG_LINES\\)\' "ops/wait_for_node_sync.py"', validator)
        self.assertIn('need_grep \'Reusing POSTGRES_PASSWORD from existing [.][e]nv\' "ops/release-install.sh"', validator)
        self.assertIn('need_grep \'Reusing POSTGRES_PASSWORD from existing [.][e]nv\' "scripts/release/installers/install-unix-common.sh"', validator)
        self.assertIn('need_grep \'Reusing POSTGRES_PASSWORD from existing [.][e]nv\' "scripts/release/installers/install-windows.ps1"', validator)
        self.assertIn('need_grep \'Preserving existing [.][e]nv\' "scripts/sim-release.sh"', validator)
        self.assertIn('reject_grep \'^[[:space:]]*ip -o -4 route get 1[.]1[.]1[.]1 .*awk\' "ops/release-install.sh"', validator)
        self.assertIn('reject_grep \'^[[:space:]]*ip -o -4 route get 1[.]1[.]1[.]1 .*awk\' "scripts/release/installers/install-unix-common.sh"', validator)
        self.assertIn('reject_grep \'BDAG_CHAIN_DB_ARCHIVE_SHA256\' ".env.example"', validator)
        self.assertIn('reject_grep \'BDAG_CHAIN_DB_ARCHIVE_SHA256\' "ops/release-install.sh"', validator)
        self.assertIn('need_grep \'NODE_RPC_URLS: .*http://node:38131\' "docker-compose.yml"', validator)
        self.assertIn('need_grep \'^BDAG_STACK_SERVICES=postgres,node,pool$\' ".env.example"', validator)

    def test_release_validator_keeps_release_packaging_source_only(self) -> None:
        validator = (ROOT_DIR / "scripts" / "validate-release-build.sh").read_text(encoding="utf-8")

        self.assertIn('need_grep \'scripts/check-release-archive.py\' ".github/workflows/build.yml"', validator)
        self.assertIn('reject_grep \'SNAPSHOT_PATH\' ".env.example"', validator)
        self.assertIn('reject_grep \'BDAG_SNAPSHOT_URL\' "docker-compose.yml"', validator)

    def test_live_deploy_rollback_validates_manifest_not_new_rc_contract(self) -> None:
        deploy = (ROOT_DIR / "ops" / "deploy-live-runtime-update.sh").read_text(encoding="utf-8")
        rollback_body = deploy.split("rollback_from_backup()", 1)[1].split("if [[ -n \"$ROLLBACK_DIR\" ]]", 1)[0]

        self.assertIn("validate_rollback_restored", deploy)
        self.assertIn("validate_rollback_restored || die", rollback_body)
        self.assertNotIn("run_target_validation", rollback_body)

    def test_release_installer_defaults_to_zero_miner_sources(self) -> None:
        installer = (ROOT_DIR / "ops" / "release-install.sh").read_text(encoding="utf-8")

        self.assertIn('configure discovered miner sources now?" "n"', installer)

    def test_linux_installers_start_sync_services_before_pool(self) -> None:
        local_installer = (ROOT_DIR / "ops" / "release-install.sh").read_text(encoding="utf-8")
        payload_installer = (
            ROOT_DIR / "scripts" / "release" / "installers" / "install-unix-common.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("automation_control.py ensure-normal", local_installer)
        self.assertIn("compose_cmd up -d --no-build --pull never node", local_installer)
        self.assertIn("wait_for_node_sync", local_installer)
        self.assertIn("automation_control.py ensure-normal", payload_installer)
        self.assertIn("docker compose up -d --no-build --pull never node", payload_installer)
        self.assertIn("BDAG_RELEASE_INSTALL_CHAIN_DB_ONLY=1 bash ops/release-install.sh --chain-db-only", payload_installer)
        self.assertIn("ensure_node_datadir_bind_mount", payload_installer)
        self.assertIn("python3 ops/wait_for_node_sync.py", payload_installer)
        self.assertIn("docker compose up -d --no-build --pull never pool-db pool collector dashboard", payload_installer)

    def test_installers_preserve_existing_postgres_password(self) -> None:
        local_installer = (ROOT_DIR / "ops" / "release-install.sh").read_text(encoding="utf-8")
        payload_installer = (
            ROOT_DIR / "scripts" / "release" / "installers" / "install-unix-common.sh"
        ).read_text(encoding="utf-8")
        windows_installer = (
            ROOT_DIR / "scripts" / "release" / "installers" / "install-windows.ps1"
        ).read_text(encoding="utf-8")
        sim_release = (ROOT_DIR / "scripts" / "sim-release.sh").read_text(encoding="utf-8")

        self.assertIn('postgres_password="$(env_value_unquoted POSTGRES_PASSWORD "")"', local_installer)
        self.assertIn('postgres_password="$POSTGRES_PASSWORD"', local_installer)
        self.assertIn('say "Reusing POSTGRES_PASSWORD from existing .env"', local_installer)
        self.assertIn('postgres_user="${postgres_user:-bdag_pool}"', local_installer)
        self.assertIn('postgres_db="${postgres_db:-bdagpool}"', local_installer)

        self.assertIn("[[ -f .env ]] || cp .env.example .env", payload_installer)
        self.assertNotIn("\ncp .env.example .env\nset_env_value .env POSTGRES_PASSWORD", payload_installer)
        self.assertIn('existing_postgres_password="$(env_file_value .env POSTGRES_PASSWORD)"', payload_installer)
        self.assertIn('POSTGRES_PASSWORD="$existing_postgres_password"', payload_installer)
        self.assertIn('echo "Reusing POSTGRES_PASSWORD from existing .env."', payload_installer)

        self.assertIn("if (-not (Test-Path '.env'))", windows_installer)
        self.assertIn("$existingPgPassword = Get-EnvFileValue '.env' 'POSTGRES_PASSWORD'", windows_installer)
        self.assertIn("Write-Host \"Reusing POSTGRES_PASSWORD from existing .env.\"", windows_installer)
        self.assertNotIn("Copy-Item .env.example .env -Force", windows_installer)

        self.assertIn("Preserving existing .env", sim_release)
        self.assertIn("! -name node-data ! -name .env", sim_release)

    def test_release_installer_extracts_preserved_chain_peer_evidence(self) -> None:
        installer = (ROOT_DIR / "ops" / "release-install.sh").read_text(encoding="utf-8")

        self.assertIn("discover_preserved_chain_peers", installer)
        self.assertIn('python3 ops/update-local-peers.py --env-file "$ROOT/.env" --force-apply', installer)
        self.assertIn("peer-discovery-current.json", installer)

    def test_installers_pin_pool_host_and_asic_lan_scope(self) -> None:
        env_example = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        local_installer = (ROOT_DIR / "ops" / "release-install.sh").read_text(encoding="utf-8")
        entrypoint = (ROOT_DIR / "docker" / "entrypoint-nodeworker.sh").read_text(encoding="utf-8")
        payload_installer = (
            ROOT_DIR / "scripts" / "release" / "installers" / "install-unix-common.sh"
        ).read_text(encoding="utf-8")
        local_deploy = (ROOT_DIR / "scripts" / "local-deploy-test-release.sh").read_text(encoding="utf-8")
        windows_installer = (
            ROOT_DIR / "scripts" / "release" / "installers" / "install-windows.ps1"
        ).read_text(encoding="utf-8")
        validator = (ROOT_DIR / "scripts" / "validate-release-build.sh").read_text(encoding="utf-8")

        self.assertIn("BDAG_DOCKER_BRIDGE_CIDRS=172.16.0.0/12", env_example)
        self.assertIn("BDAG_ALLOW_DOCKER_BRIDGE_ASIC_IPS=0", env_example)
        self.assertIn("BDAG_ASIC_LAN_INTERFACE=", env_example)
        self.assertIn("BDAG_ASIC_LAN_INTERFACE: ${BDAG_ASIC_LAN_INTERFACE:-}", compose)
        self.assertIn("BDAG_ASIC_LAN_CIDRS: ${BDAG_ASIC_LAN_CIDRS:-}", compose)
        self.assertIn("tr ',' ' '", entrypoint)
        self.assertIn('append_node_arg_once "--modules=${word}"', entrypoint)
        self.assertIn('set_env_value .env BDAG_ASIC_LAN_INTERFACE "$asic_lan_interface"', local_installer)
        self.assertIn('set_env_value .env BDAG_ASIC_LAN_CIDRS "$scan_target"', local_installer)
        self.assertIn("validate_pool_lan_config", local_installer)
        self.assertIn('ensure_asic_lan_address "$lan_ip" "$asic_lan_interface"', local_installer)
        self.assertIn("BDAG_ASIC_LAN_INTERFACE so the host can own", local_installer)
        self.assertIn('set_env_value .env BDAG_ASIC_LAN_INTERFACE "$ASIC_LAN_INTERFACE"', payload_installer)
        self.assertIn('set_env_value .env BDAG_ASIC_LAN_CIDRS "$MINER_SCAN_TARGET"', payload_installer)
        self.assertIn("validate_pool_lan_config", payload_installer)
        self.assertIn('ensure_asic_lan_address "$POOL_LAN_IP" "$ASIC_LAN_INTERFACE"', payload_installer)
        self.assertIn("refusing Docker bridge pool endpoint", payload_installer)
        self.assertIn("configured_env_value BDAG_ASIC_LAN_INTERFACE", local_deploy)
        self.assertIn("configured_env_value BDAG_MINER_SCAN_TARGET", local_deploy)
        self.assertIn("BDAG_POOL_HOST=$current_pool_host is link-local", local_deploy)
        self.assertIn("Set-EnvValue .env BDAG_ASIC_LAN_CIDRS $minerScanTarget", windows_installer)
        self.assertIn("Assert-PoolLanConfig", windows_installer)
        self.assertIn("Refusing Docker bridge pool endpoint", windows_installer)
        self.assertIn("BDAG_DOCKER_BRIDGE_CIDRS=172.16.0.0/12", validator)
        self.assertIn("BDAG_ALLOW_DOCKER_BRIDGE_ASIC_IPS=0", validator)

    def test_installers_do_not_offer_docker_bridge_lan_defaults(self) -> None:
        local_installer = (ROOT_DIR / "ops" / "release-install.sh").read_text(encoding="utf-8")
        payload_installer = (
            ROOT_DIR / "scripts" / "release" / "installers" / "install-unix-common.sh"
        ).read_text(encoding="utf-8")

        self.assertEqual(1, local_installer.count("route get 1.1.1.1"))
        self.assertEqual(1, payload_installer.count("route get 1.1.1.1"))
        self.assertIn("172\\.(1[6-9]|2[0-9]|3[0-1])", local_installer)
        self.assertIn("172\\.(1[6-9]|2[0-9]|3[0-1])", payload_installer)

    def test_release_docs_keep_zero_miner_default_invariant(self) -> None:
        agents = (ROOT_DIR / "AGENTS.md").read_text(encoding="utf-8")
        readme = (ROOT_DIR / "README.md").read_text(encoding="utf-8")

        self.assertIn("Fresh installs assume zero miner sources", agents)
        self.assertIn("Fresh installs assume zero miner sources", readme)
        self.assertIn("0..N ASIC or Stratum miners", agents)

    def test_p2p_firewall_uses_single_compose_port(self) -> None:
        env_example = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")
        firewall = (ROOT_DIR / "ops" / "allow-p2p-iptables.sh").read_text(encoding="utf-8")
        installer = (ROOT_DIR / "ops" / "install-p2p-services.sh").read_text(encoding="utf-8")
        unit = (ROOT_DIR / "ops" / "systemd" / "bdag-p2p-firewall.service").read_text(encoding="utf-8")

        combined = "\n".join([env_example, firewall, installer, unit])
        self.assertIn("P2P_PORT=8150", env_example)
        self.assertIn('PORT="${P2P_PORT:-8150}"', firewall)
        self.assertIn("Environment=P2P_PORT=8150", unit)
        self.assertNotIn("BDAG_P2P_PORTS", combined)
        self.assertNotIn("--dports", firewall)


if __name__ == "__main__":
    unittest.main()
