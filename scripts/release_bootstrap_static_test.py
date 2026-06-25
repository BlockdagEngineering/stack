#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
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
            powershell = (out_dir / "install.ps1").read_text(encoding="utf-8")
        for text in (shell, powershell):
            self.assertIn("pool-v1.2.3", text)
            self.assertIn("releases/download/", text)
            self.assertNotIn("latest/download", text)
        self.assertIn('ASSET="$PACKAGE_NAME-$VERSION-$PAYLOAD_TARGET.zip"', shell)
        self.assertIn("$PackageName-$Version-$PayloadTarget.zip", powershell)


class PayloadInstallerTests(unittest.TestCase):
    def test_installers_do_not_warn_arm_hosts_to_use_amd64_emulation(self) -> None:
        unix = (ROOT / "scripts" / "release" / "installers" / "install-unix-common.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("release-payload.env", unix)
        self.assertNotIn("amd64 emulation", unix)

    def test_unix_installer_supports_local_raw_chain_data_archive(self) -> None:
        unix = (ROOT / "scripts" / "release" / "installers" / "install-unix-common.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("BDAG_CHAIN_DATA_ARCHIVE", unix)
        self.assertIn("tar --zstd -tf", unix)
        self.assertIn("chain data archive does not contain recognizable BlockDAG chain markers", unix)

    def test_unix_installer_pulls_external_pool_db_image_before_pull_never_start(self) -> None:
        unix = (ROOT / "scripts" / "release" / "installers" / "install-unix-common.sh").read_text(
            encoding="utf-8"
        )
        windows = (ROOT / "scripts" / "release" / "installers" / "install-windows.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("docker compose pull pool-db", unix)
        self.assertIn("docker compose up -d --no-build --pull never pool-db node dashboard", unix)
        self.assertIn("docker compose pull pool-db", windows)
        self.assertIn("docker compose up -d --no-build --pull never pool-db node dashboard", windows)


class BootstrapPeerDefaultTests(unittest.TestCase):
    QUARANTINED_BOOTSTRAP_PEERS = (
        (
            "/ip4/13.140.165.186/tcp/8150/p2p/"
            "16Uiu2HAm4hHD7Ht5LJrLgaKXr7YP2RzHHjrrCLNt8zv8FQ9s3gBU"
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
    )

    def test_release_defaults_pass_bootstrap_peers_to_node(self) -> None:
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        node_conf = (ROOT / "node.conf.example").read_text(encoding="utf-8")

        bootstrap_line = next(
            line
            for line in env_example.splitlines()
            if line.startswith("BOOTSTRAP_PEER_ADDRESSES=")
        )
        self.assertIn("BOOTSTRAP_PEER_ADDRESSES: ${BOOTSTRAP_PEER_ADDRESSES:-}", compose)
        for peer in self.STABLE_BOOTSTRAP_PEERS:
            self.assertIn(peer, bootstrap_line)
            self.assertIn(f"addpeer={peer}", node_conf)
        for peer in self.QUARANTINED_BOOTSTRAP_PEERS:
            self.assertNotIn(peer, bootstrap_line)
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
