#!/usr/bin/env python3

import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class ComposeStartCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_stack_services = list(pool_ops.STACK_SERVICES)
        self.original_project_root = pool_ops.PROJECT_ROOT
        self.original_pool_env = pool_ops.POOL_ENV_FILE
        self.original_env = dict(os.environ)
        os.environ.pop("BDAG_START_SERVICES", None)
        self.addCleanup(self.restore)

    def restore(self) -> None:
        pool_ops.STACK_SERVICES = self.original_stack_services
        pool_ops.PROJECT_ROOT = self.original_project_root
        pool_ops.POOL_ENV_FILE = self.original_pool_env
        os.environ.clear()
        os.environ.update(self.original_env)

    def fake_inspect(self, labels: dict[str, str]):
        def run(command, **_kwargs):
            name = command[-1]
            service = labels.get(name)
            if service is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="not found")
            return SimpleNamespace(returncode=0, stdout=f"{service}\n", stderr="")

        return run

    def test_repair_start_command_uses_configured_core_services(self) -> None:
        pool_ops.STACK_SERVICES = [
            "pool-stack-docker-postgres-1",
            "pool-stack-docker-node-1",
            "pool-stack-docker-pool-1",
            "pool-stack-docker-dashboard-1",
        ]
        labels = {
            "pool-stack-docker-postgres-1": "postgres",
            "pool-stack-docker-node-1": "node",
            "pool-stack-docker-pool-1": "pool",
            "pool-stack-docker-dashboard-1": "dashboard",
        }
        with mock.patch.object(pool_ops.subprocess, "run", side_effect=self.fake_inspect(labels)):
            command = pool_ops.docker_compose_start_command()

        self.assertEqual(command[-6:], ["up", "-d", "postgres", "node", "pool", "dashboard"])
        self.assertNotIn("hotsnap", command)
        self.assertNotIn("snapshot-node", command)

    def test_repair_start_command_infers_service_from_compose_container_name(self) -> None:
        pool_ops.STACK_SERVICES = ["pool-stack-docker-node-1"]
        with mock.patch.object(
            pool_ops.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="not found"),
        ):
            command = pool_ops.docker_compose_start_command()

        self.assertEqual(command[-3:], ["up", "-d", "node"])

    def test_inspect_timeout_falls_back_to_compose_container_name(self) -> None:
        pool_ops.STACK_SERVICES = ["pool-stack-docker-node-1"]
        with mock.patch.object(pool_ops.subprocess, "run", side_effect=pool_ops.subprocess.TimeoutExpired("docker", 10)):
            command = pool_ops.docker_compose_start_command()

        self.assertEqual(command[-3:], ["up", "-d", "node"])

    def test_compose_run_refreshes_env_file_values_over_stale_parent_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            env_file = root / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "BOOTSTRAP_PEER_ADDRESSES=/ip4/203.0.113.10/tcp/8150/p2p/newPeer",
                        "BDAG_NODE_PEER_ADDRESSES=/ip4/203.0.113.11/tcp/8150/p2p/directPeer",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            pool_ops.PROJECT_ROOT = root
            pool_ops.POOL_ENV_FILE = env_file
            os.environ["BOOTSTRAP_PEER_ADDRESSES"] = "/ip4/192.0.2.10/tcp/8150/p2p/stalePeer"
            os.environ["BDAG_NODE_PEER_ADDRESSES"] = ""
            captured: dict[str, object] = {}

            def fake_run(command, **kwargs):
                captured["command"] = command
                captured["env"] = kwargs.get("env")
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            with mock.patch.object(pool_ops.subprocess, "run", side_effect=fake_run):
                result = pool_ops.run(pool_ops.docker_compose_command("up", "-d", "--no-build", "node"))

            self.assertEqual(result.returncode, 0)
            env = captured["env"]
            self.assertIsInstance(env, dict)
            self.assertEqual(
                env["BOOTSTRAP_PEER_ADDRESSES"],
                "/ip4/203.0.113.10/tcp/8150/p2p/newPeer",
            )
            self.assertEqual(
                env["BDAG_NODE_PEER_ADDRESSES"],
                "/ip4/203.0.113.11/tcp/8150/p2p/directPeer",
            )


if __name__ == "__main__":
    unittest.main()
