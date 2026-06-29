from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = ROOT / "ops"
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402
import pool_start_gate  # noqa: E402

MIGRATE = ROOT / "scripts" / "migrate-node-data-volume-to-host.sh"


def write_env(root: Path, text: str) -> Path:
    env_file = root / ".env"
    env_file.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return env_file


def make_chain(root: Path, payload_bytes: int = 0, snapshot: bool = False) -> None:
    network = root / "mainnet"
    network.mkdir(parents=True, exist_ok=True)
    if snapshot:
        snapshot_path = network / "snapshot.bdsnap"
        with snapshot_path.open("wb") as handle:
            handle.truncate(max(payload_bytes, 1))
        return
    for relative in ["BdagChain", "bdageth", "peerstore"]:
        (network / relative).mkdir(parents=True, exist_ok=True)
    (network / "network.key").write_text("test-key\n", encoding="utf-8")
    if payload_bytes:
        payload = network / "BdagChain" / "payload.dat"
        with payload.open("wb") as handle:
            handle.truncate(payload_bytes)


def fake_docker(root: Path, volume_path: Path | None = None) -> Path:
    bin_dir = root / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    if volume_path is None:
        docker.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    else:
        docker.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1 $2 $3\" == \"volume inspect stack_node-data\" ]]; then\n"
            f"  printf '%s\\n' '{volume_path}'\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
    docker.chmod(0o755)
    return bin_dir


def run_script(script: Path, root: Path, args: list[str] | None = None, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    bin_dir = root / "bin"
    if not (bin_dir / "docker").exists():
        fake_docker(root)
    env = os.environ.copy()
    for key in (
        "NODE_DATA_DIR",
        "BDAG_NODE_DATA_DIR",
        "BDAG_ALLOW_NODE_DATA_DIR_OVERRIDE",
    ):
        env.pop(key, None)
    env.update(
        {
            "BDAG_PROJECT_ROOT": str(root),
            "BDAG_ENV_FILE": str(root / ".env"),
            "HOME": str(root),
            "PATH": f"{bin_dir}:{env['PATH']}",
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(script), *(args or [])],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class ChainDataProvenanceTests(unittest.TestCase):
    def test_migration_quarantines_target_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "legacy"
            target = root / "node-data"
            write_env(root, "NODE_DATA_DIR=./node-data\n")
            make_chain(source, payload_bytes=4096)
            make_chain(target, payload_bytes=1024)

            result = run_script(
                MIGRATE,
                root,
                ["--source", str(source), "--force", "--no-stop"],
                {
                    "BDAG_MIGRATION_USE_SUDO": "0",
                    "BDAG_MIGRATION_STAMP": "teststamp",
                },
            )

            manifest = root / "ops" / "runtime" / "chain-data-migration-teststamp.json"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((target / "mainnet" / "BdagChain").is_dir())
            payload = json.loads(manifest.read_text(encoding="utf-8"))

        self.assertIn("node.pre-migration-teststamp", payload["quarantined_previous_target"])
        self.assertTrue(payload["rollback_source_preserved"])

    def test_runtime_provenance_flags_low_height_with_better_legacy_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = root / "node-data"
            make_chain(selected, payload_bytes=1024)
            originals = {
                "PROJECT_ROOT": pool_ops.PROJECT_ROOT,
                "POOL_ENV_FILE": pool_ops.POOL_ENV_FILE,
                "CANONICAL_NODE_DATA_DIR": pool_ops.CANONICAL_NODE_DATA_DIR,
                "NODE_DATA_MISMATCH_PEER_GAP_BLOCKS": pool_ops.NODE_DATA_MISMATCH_PEER_GAP_BLOCKS,
                "NODE_DATA_MISMATCH_LOW_LOCAL_HEIGHT_BLOCKS": pool_ops.NODE_DATA_MISMATCH_LOW_LOCAL_HEIGHT_BLOCKS,
                "NODE_DATA_MISMATCH_MATERIAL_BYTES": pool_ops.NODE_DATA_MISMATCH_MATERIAL_BYTES,
                "NODE_DATA_MISMATCH_SIZE_RATIO_NUMERATOR": pool_ops.NODE_DATA_MISMATCH_SIZE_RATIO_NUMERATOR,
                "docker_volume_chain_candidate": pool_ops.docker_volume_chain_candidate,
            }

            def restore() -> None:
                for name, value in originals.items():
                    setattr(pool_ops, name, value)

            self.addCleanup(restore)
            pool_ops.PROJECT_ROOT = root
            pool_ops.POOL_ENV_FILE = root / ".env"
            pool_ops.CANONICAL_NODE_DATA_DIR = selected.resolve()
            pool_ops.NODE_DATA_MISMATCH_PEER_GAP_BLOCKS = 100
            pool_ops.NODE_DATA_MISMATCH_LOW_LOCAL_HEIGHT_BLOCKS = 1_000
            pool_ops.NODE_DATA_MISMATCH_MATERIAL_BYTES = 1
            pool_ops.NODE_DATA_MISMATCH_SIZE_RATIO_NUMERATOR = 2
            pool_ops.docker_volume_chain_candidate = lambda _volume="stack_node-data": {
                "volume": "stack_node-data",
                "exists": True,
                "valid": True,
                "path": "/var/lib/docker/volumes/stack_node-data/_data",
                "size_bytes": 8192,
            }

            with unittest.mock.patch.dict(
                pool_ops.os.environ,
                {"NODE_DATA_DIR": "./node-data", "BDAG_NODE_DATA_DIR": ""},
                clear=False,
            ):
                health = pool_ops.node_data_provenance_health(
                    {"status": "syncing", "chain_block_count": 200, "remaining_blocks": 12_000},
                    {"node": {"latest_block": 200, "peer_ahead_blocks": 12_000}},
                    {},
                )

        self.assertTrue(health["restore_required"])
        self.assertTrue(health["node_data_mount_mismatch_suspected"])
        self.assertIn("Node data mount mismatch suspected", " ".join(health["reasons"]))

    def test_runtime_provenance_degrades_on_unreadable_selected_datadir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = root / "node-data"
            network = selected / "mainnet"
            network.mkdir(parents=True)
            write_env(root, "NODE_DATA_DIR=./node-data\n")
            network.chmod(0)
            originals = {
                "PROJECT_ROOT": pool_ops.PROJECT_ROOT,
                "POOL_ENV_FILE": pool_ops.POOL_ENV_FILE,
                "CANONICAL_NODE_DATA_DIR": pool_ops.CANONICAL_NODE_DATA_DIR,
                "NODE_DATA_MISMATCH_PEER_GAP_BLOCKS": pool_ops.NODE_DATA_MISMATCH_PEER_GAP_BLOCKS,
                "NODE_DATA_MISMATCH_LOW_LOCAL_HEIGHT_BLOCKS": pool_ops.NODE_DATA_MISMATCH_LOW_LOCAL_HEIGHT_BLOCKS,
                "docker_volume_chain_candidate": pool_ops.docker_volume_chain_candidate,
                "chain_data_marker_probe": pool_ops.chain_data_marker_probe,
            }

            def restore() -> None:
                for name, value in originals.items():
                    setattr(pool_ops, name, value)

            try:
                pool_ops.PROJECT_ROOT = root
                pool_ops.POOL_ENV_FILE = root / ".env"
                pool_ops.CANONICAL_NODE_DATA_DIR = selected.resolve()
                pool_ops.NODE_DATA_MISMATCH_PEER_GAP_BLOCKS = 100
                pool_ops.NODE_DATA_MISMATCH_LOW_LOCAL_HEIGHT_BLOCKS = 1_000
                pool_ops.docker_volume_chain_candidate = lambda _volume="stack_node-data": {
                    "volume": "stack_node-data",
                    "exists": False,
                    "valid": False,
                    "path": "",
                    "size_bytes": 0,
                }
                pool_ops.chain_data_marker_probe = lambda _path: (
                    False,
                    "PermissionError: [Errno 13] Permission denied: 'snapshot.bdsnap'",
                )

                with unittest.mock.patch.dict(
                    pool_ops.os.environ,
                    {"NODE_DATA_DIR": "./node-data", "BDAG_NODE_DATA_DIR": ""},
                    clear=False,
                ):
                    health = pool_ops.node_data_provenance_health(
                        {"status": "synced", "chain_block_count": 2_000_000, "remaining_blocks": 0},
                        {"node": {"latest_block": 2_000_000, "peer_ahead_blocks": 0}},
                        {},
                    )
            finally:
                network.chmod(0o755)
                restore()

        self.assertFalse(health["restore_required"])
        self.assertEqual(health["status"], "degraded")
        self.assertIn("PermissionError", health["selected_marker_error"])
        self.assertIn("marker inspection unavailable", " ".join(health["warnings"]))

    def test_node_log_evm_rebuild_interrupted_requires_chain_state_restore(self) -> None:
        log = (
            '2026-06-25|20:18:07.616 [ERROR] prepare startup evm environment failed '
            'module=CHAIN mainTipOrder=12648088 mainTipHash=0xabc '
            'err="bdag chain env error:targetEVM.number=12285979, '
            'targetEVM.hash=0x25e622, targetState.order=12648088, '
            'cur.number=0, cur.hash=0x3fb19e, native EVM rebuild interrupted"'
        )

        parsed = pool_ops.parse_node_log(log)
        reasons = pool_ops.chain_data_restore_hard_reasons("node", parsed)

        self.assertTrue(parsed["evm_rebuild_interrupted"])
        self.assertEqual([log], parsed["evm_rebuild_interrupted_lines"])
        self.assertIn("EVM rebuild from native state was interrupted", " ".join(reasons))

    def test_pool_start_gate_blocks_chain_data_restore_required(self) -> None:
        decision = pool_start_gate.pool_start_decision(
            {
                "fresh": True,
                "mode": "mining",
                "overall": "ok",
                "peer_count": 3,
                "canonical_mining_safety": {"safe": True},
                "sync_progress": {"status": "synced", "remaining_blocks": 0},
                "sync_health": {
                    "needs_chain_data_restore": True,
                    "chain_data_restore_required": True,
                    "node_data_mount_mismatch_suspected": True,
                    "node_data_provenance": {
                        "reasons": [
                            "Node data mount mismatch suspected: current node data path appears reset while preserved chain data exists."
                        ]
                    },
                },
            }
        )

        self.assertFalse(decision.allowed)
        self.assertIn("node chain data restore or migration is required", decision.reason)
