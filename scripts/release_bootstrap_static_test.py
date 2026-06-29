#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "scripts" / "render-release-bootstrap.py"
SPEC = importlib.util.spec_from_file_location("render_release_bootstrap", RENDERER)
assert SPEC and SPEC.loader
renderer = importlib.util.module_from_spec(SPEC)
sys.modules["render_release_bootstrap"] = renderer
SPEC.loader.exec_module(renderer)


class BootstrapSelectionTests(unittest.TestCase):
    def test_selects_runtime_payload_for_supported_hosts(self) -> None:
        cases = [
            ("Linux", "x86_64", "linux-amd64"),
            ("Linux", "amd64", "linux-amd64"),
            ("Linux", "arm64", "linux-arm64"),
            ("Linux", "aarch64", "linux-arm64"),
        ]
        for os_name, arch, expected in cases:
            with self.subTest(os_name=os_name, arch=arch):
                self.assertEqual(renderer.select_payload_target(os_name, arch), expected)

    def test_rejects_unsupported_bootstrap_selection(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported CPU architecture"):
            renderer.select_payload_target("Linux", "riscv64")
        with self.assertRaisesRegex(ValueError, "unsupported operating system"):
            renderer.select_payload_target("Darwin", "arm64")
        with self.assertRaisesRegex(ValueError, "unsupported operating system"):
            renderer.select_payload_target("Windows", "amd64")

    def test_generated_bootstraps_are_pinned_to_one_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            result = subprocess.run(
                [
                    sys.executable,
                    str(RENDERER),
                    "--version",
                    "pool-v1.2.3",
                    "--repository",
                    "BlockdagEngineering/stack",
                    "--out-dir",
                    str(out_dir),
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            shell = (out_dir / "install.sh").read_text(encoding="utf-8")
            self.assertFalse((out_dir / "install.ps1").exists())
        self.assertIn("pool-v1.2.3", shell)
        self.assertIn("releases/download/", shell)
        self.assertNotIn("latest/download", shell)
        self.assertIn('ASSET="$PACKAGE_NAME-$VERSION-$PAYLOAD_TARGET.zip"', shell)
        self.assertIn('exec bash "$ROOT/install.sh" "$@"', shell)
        self.assertNotIn("installers/", shell)


class PayloadInstallerTests(unittest.TestCase):
    def payload_installer(self) -> str:
        return (ROOT / "scripts" / "release" / "install.sh").read_text(encoding="utf-8")

    def copy_minimal_payload(self, package_root: Path) -> None:
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / ".env.example").write_text(
            (ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (package_root / "install.sh").write_text(
            self.payload_installer(),
            encoding="utf-8",
        )
        (package_root / "release-payload.env").write_text(
            "\n".join(
                [
                    "BDAG_RELEASE_PAYLOAD_TARGET=linux-amd64",
                    "BDAG_RELEASE_PAYLOAD_ARCH=amd64",
                    "DOCKER_PLATFORM=linux/amd64",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def test_release_has_one_payload_installer(self) -> None:
        release_dir = ROOT / "scripts" / "release"

        self.assertTrue((release_dir / "install.sh").is_file())
        self.assertFalse((release_dir / "install-node.sh").exists())
        self.assertFalse((release_dir / "installers").exists())

    def test_installer_does_not_warn_arm_hosts_to_use_amd64_emulation(self) -> None:
        installer = self.payload_installer()

        self.assertIn("release-payload.env", installer)
        self.assertNotIn("amd64 emulation", installer)

    def test_installer_clears_stale_snapshot_url_without_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp)
            self.copy_minimal_payload(package_root)
            env_path = package_root / ".env"
            env_path.write_text(
                (package_root / ".env.example").read_text(encoding="utf-8").replace(
                    "BDAG_SNAPSHOT_URL=",
                    "BDAG_SNAPSHOT_URL=https://stale.invalid/latest.bdsnap",
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["BDAG_INSTALL_TEST_WRITE_ENV_ONLY"] = "1"
            env["BDAG_NO_PAUSE"] = "1"
            result = subprocess.run(
                ["bash", "install.sh"],
                cwd=package_root,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            env_text = env_path.read_text(encoding="utf-8")
            self.assertIn("BDAG_SNAPSHOT_URL=", env_text)
            self.assertNotIn("https://stale.invalid/latest.bdsnap", env_text)
            self.assertTrue((package_root / "node-data").is_dir())

    def test_installer_leaves_chain_download_to_node_startup(self) -> None:
        installer = self.payload_installer()

        self.assertIn("--snapshot-url", installer)
        self.assertIn("BDAG_SNAPSHOT_URL", installer)
        self.assertIn("existing NODE_DATA_DIR first", installer)
        self.assertNotIn("BDAG_RELEASE_INSTALL_CHAIN_DB_ONLY", installer)
        self.assertNotIn("--chain-db-only", installer)
        self.assertNotIn("bootstrap_chain_db_archive", installer)

    def test_node_entrypoint_logs_http_snapshot_download_progress(self) -> None:
        entrypoint = (ROOT / "docker" / "entrypoint-nodeworker.sh").read_text(
            encoding="utf-8"
        )
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("BDAG_SNAPSHOT_PROGRESS_INTERVAL_SECONDS", entrypoint)
        self.assertIn("snapshot download ${event}:", entrypoint)
        self.assertIn("log_snapshot_download_measurement progress", entrypoint)
        self.assertIn("downloaded_bytes=", entrypoint)
        self.assertIn("downloaded_mib=", entrypoint)
        self.assertIn("total_bytes=", entrypoint)
        self.assertIn("remaining_bytes=", entrypoint)
        self.assertIn("rate_bytes_per_second=", entrypoint)
        self.assertIn("eta_seconds=", entrypoint)
        self.assertIn("eta_text=", entrypoint)
        self.assertIn("snapshot_download_total_bytes", entrypoint)
        self.assertIn("download_snapshot_with_progress", entrypoint)
        self.assertIn(
            "BDAG_SNAPSHOT_PROGRESS_INTERVAL_SECONDS: ${BDAG_SNAPSHOT_PROGRESS_INTERVAL_SECONDS:-30}",
            compose,
        )
        self.assertIn("BDAG_SNAPSHOT_TOTAL_BYTES: ${BDAG_SNAPSHOT_TOTAL_BYTES:-}", compose)

    def test_node_entrypoint_handles_downloaded_datadir_archives(self) -> None:
        entrypoint = (ROOT / "docker" / "entrypoint-nodeworker.sh").read_text(
            encoding="utf-8"
        )
        dockerfile = (ROOT / "dockerfile").read_text(encoding="utf-8")

        self.assertIn("prepare_downloaded_chain_datadir_archive", entrypoint)
        self.assertIn("find_chain_datadir_payload_root", entrypoint)
        self.assertIn("BdagChain", entrypoint)
        self.assertIn("bdageth", entrypoint)
        self.assertIn("metaData", entrypoint)
        self.assertIn("downloaded snapshot is a chain datadir archive", entrypoint)
        self.assertIn("zstd", dockerfile)

    def test_node_entrypoint_refuses_partial_chain_datadirs_by_default(self) -> None:
        entrypoint = (ROOT / "docker" / "entrypoint-nodeworker.sh").read_text(
            encoding="utf-8"
        )
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn("validate_chain_datadir_state", entrypoint)
        self.assertIn("refusing partial chain datadir", entrypoint)
        self.assertIn("BdagChain, bdageth, and metaData together", entrypoint)
        self.assertIn("BDAG_ALLOW_PARTIAL_CHAIN_DATADIR_BOOTSTRAP=0", env_example)
        self.assertIn(
            "BDAG_ALLOW_PARTIAL_CHAIN_DATADIR_BOOTSTRAP: ${BDAG_ALLOW_PARTIAL_CHAIN_DATADIR_BOOTSTRAP:-0}",
            compose,
        )

    def test_archive_installs_force_evm_archive_gcmode(self) -> None:
        installer = self.payload_installer()
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        entrypoint = (ROOT / "docker" / "entrypoint-nodeworker.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("BDAG_EVM_GCMODE=", env_example)
        self.assertIn("BDAG_EVM_GCMODE: ${BDAG_EVM_GCMODE:-}", compose)
        self.assertIn("apply_evm_runtime_args", entrypoint)
        self.assertIn("BDAG_EVM_GCMODE", entrypoint)
        self.assertIn("--gcmode=archive", entrypoint)
        self.assertIn("BDAG_EVM_GCMODE=archive", installer)
        self.assertIn('set_env_value .env BDAG_EVM_GCMODE "$BDAG_EVM_GCMODE"', installer)

    def test_installer_writes_url_safe_pool_database_url(self) -> None:
        installer = self.payload_installer()

        self.assertIn("openssl rand -hex 32", installer)
        self.assertIn("urlencode_component", installer)
        self.assertIn("set_env_value .env PG_URL", installer)

    def test_installer_starts_node_before_pool_services(self) -> None:
        installer = self.payload_installer()

        self.assertIn("docker compose up -d --no-build --pull never node", installer)
        self.assertIn("wait_for_node_sync", installer)
        self.assertIn("--no-wait-for-node-sync", installer)
        self.assertIn("BDAG_WAIT_FOR_NODE_SYNC_BEFORE_STACK=1|0", installer)
        self.assertIn("BDAG_HAS_LOCAL_ASIC_MINER=1|0", installer)
        self.assertIn("Wait for the node to complete sync before starting the rest of the stack?", installer)
        self.assertIn('prompt_yes_no_default "Wait for the node to complete sync before starting the rest of the stack?" "yes"', installer)
        self.assertIn('elif [[ "$WAIT_FOR_NODE_SYNC_BEFORE_STACK" == "yes" ]]; then', installer)
        self.assertIn("Skipping node sync wait before starting remaining services.", installer)
        self.assertIn("Is there a local ASIC miner connected to this host LAN to configure now?", installer)
        self.assertIn('prompt_yes_no_default "Is there a local ASIC miner connected to this host LAN to configure now?" "no"', installer)
        self.assertIn('if [[ "$HAS_LOCAL_ASIC_MINER" == "yes" ]]; then', installer)
        self.assertIn("No local ASIC miner selected; leaving ASIC LAN discovery scope empty.", installer)
        self.assertLess(
            installer.index("Is there a local ASIC miner connected to this host LAN to configure now?"),
            installer.index("Pool LAN IP miners should connect to"),
        )
        self.assertIn("docker compose config --services", installer)
        self.assertIn('[[ "$service" == "node" ]] && continue', installer)
        self.assertIn("write_pool_start_lease", installer)
        self.assertIn('if service_in_list pool "${REMAINING_SERVICES[@]}"; then', installer)
        self.assertIn('docker compose pull --ignore-buildable --policy missing "$@"', installer)
        self.assertIn('docker compose up -d --no-build --pull never "${REMAINING_SERVICES[@]}"', installer)
        self.assertNotIn("docker compose up -d --no-build --pull never pool-db pool dashboard", installer)
        self.assertNotIn("docker compose up -d --no-build --pull never pool-db pool collector dashboard", installer)


class BootstrapPeerDefaultTests(unittest.TestCase):
    QUARANTINED_BOOTSTRAP_PEERS = (
        (
            "/ip4/13.140.165.186/tcp/8150/p2p/"
            "16Uiu2HAm4hHD7Ht5LJrLgaKXr7YP2RzHHjrrCLNt8zv8FQ9s3gBU"
        ),
        (
            "/ip4/102.182.77.21/tcp/8151/p2p/"
            "16Uiu2HAm2uPLqM1dfd3ddbzg3FDAmvuvyo3vbBSXpVHurqAsUxWQ"
        ),
        (
            "/ip4/102.182.77.16/tcp/8152/p2p/"
            "16Uiu2HAm99a8KYuUkL5LGEQbpRNhz5nzScrwYpEh47BfSaVMCg5G"
        ),
    )
    STABLE_BOOTSTRAP_PEERS = (
        (
            "/ip4/3.126.64.13/tcp/8152/p2p/"
            "16Uiu2HAmEFxRaBbbf3sRi43CCvMk5Y6zPkuGY9s4uRK2FKJVJkqo"
        ),
        (
            "/ip4/63.182.36.180/tcp/8150/p2p/"
            "16Uiu2HAmP8HsTF9ks8JjFamzT9JBZb3ymSiCJ8rkzXBZqYj4yKtP"
        ),
        (
            "/ip4/16.28.133.168/tcp/8150/p2p/"
            "16Uiu2HAm9UcTayJDSajjJYsWwVaN2qqGeczcs9kXse3dMdvGDRjz"
        ),
        "/ip4/13.57.132.47/tcp/8150/p2p/"
        "16Uiu2HAmDynYpWjWmgVGf9qVWvDdLnJ3ybVgDmFexizR4zMereus",
        (
            "/ip4/102.182.77.21/tcp/8150/p2p/"
            "16Uiu2HAm2uPLqM1dfd3ddbzg3FDAmvuvyo3vbBSXpVHurqAsUxWQ"
        ),
        (
            "/ip4/102.182.77.16/tcp/8150/p2p/"
            "16Uiu2HAm99a8KYuUkL5LGEQbpRNhz5nzScrwYpEh47BfSaVMCg5G"
        ),
        (
            "/ip4/129.121.92.232/tcp/8152/p2p/"
            "16Uiu2HAmQSJzJjXUxtyX5rc2bQBAsTvhcjp4GQBPmwqEYu9D8zA5"
        ),
        (
            "/ip4/3.120.205.55/tcp/8150/p2p/"
            "16Uiu2HAm8tJ2Loxi1hc7Apg4v5i8mqpNxWyRknn1tZUcx8AvbNYj"
        ),
    )

    def test_release_defaults_keep_bootstrap_peers_in_node_config(self) -> None:
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        node_conf = (ROOT / "node.conf.example").read_text(encoding="utf-8")

        self.assertNotIn("BOOTSTRAP_PEER_ADDRESSES=", env_example)
        self.assertNotIn("BOOTSTRAP_PEER_ADDRESSES:", compose)
        for peer in self.STABLE_BOOTSTRAP_PEERS:
            self.assertIn(f"addpeer={peer}", node_conf)
        for peer in self.QUARANTINED_BOOTSTRAP_PEERS:
            self.assertNotIn(peer, env_example)
            self.assertNotIn(f"addpeer={peer}", node_conf)

    def test_release_defaults_do_not_ship_dead_or_site_local_seed_peers(self) -> None:
        node_conf = (ROOT / "node.conf.example").read_text(encoding="utf-8")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

        for text in (node_conf, env_example):
            self.assertNotIn("/ip4/52.8.80.249/tcp/8150/p2p/", text)
            self.assertNotIn("/ip4/192.168.", text)
            self.assertNotIn("/ip4/199.229.220.118/tcp/8151/p2p/", text)
            self.assertNotIn("/ip4/16.28.133.168/tcp/8151/p2p/16Uiu2HAkx4", text)
            self.assertNotIn("/tcp/52604/p2p/", text)
            self.assertNotIn("/tcp/34040/p2p/", text)


class MiningTemplateDefaultTests(unittest.TestCase):
    def test_release_defaults_use_no_pending_mining_templates(self) -> None:
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        defaults = (ROOT / "ops" / "config" / "stack-defaults.env").read_text(
            encoding="utf-8"
        )
        entrypoint = (ROOT / "docker" / "entrypoint-nodeworker.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("BDAG_NODE_MINING_NO_PENDING_TX=1", env_example)
        self.assertIn(
            "BDAG_NODE_MINING_NO_PENDING_TX: ${BDAG_NODE_MINING_NO_PENDING_TX:-1}",
            compose,
        )
        self.assertIn("BDAG_NODE_MINING_NO_PENDING_TX=1", defaults)
        self.assertIn("--miningnopendingtx", entrypoint)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
