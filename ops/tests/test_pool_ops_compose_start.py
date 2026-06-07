#!/usr/bin/env python3

import os
import pathlib
import sys
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
        self.addCleanup(self.restore)

    def restore(self) -> None:
        pool_ops.clear_docker_compose_service_container_cache()
        pool_ops.STACK_SERVICES = self.original_stack_services
        pool_ops.PROJECT_ROOT = self.original_project_root
        pool_ops.POOL_ENV_FILE = self.original_pool_env
        os.environ.clear()
        os.environ.update(self.original_env)
        pool_ops.clear_docker_compose_service_container_cache()

    def command_result(self, command: list[str], stdout: str = "", returncode: int = 0, stderr: str = ""):
        return pool_ops.CommandResult(command=command, returncode=returncode, stdout=stdout, stderr=stderr, elapsed=0.01)

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

    def test_docker_inspect_resolves_compose_service_aliases(self) -> None:
        def fake_run(command: list[str], **_kwargs):
            if command[:2] == ["docker", "ps"]:
                return self.command_result(
                    command,
                    "stack-postgres-1\tpostgres\nstack-node-1\tnode\nstack-pool-1\tpool\n",
                )
            if command[:2] == ["docker", "inspect"]:
                payload = []
                for name in command[2:]:
                    service = {
                        "stack-postgres-1": "postgres",
                        "stack-node-1": "node",
                        "stack-pool-1": "pool",
                    }[name]
                    payload.append(
                        {
                            "Name": f"/{name}",
                            "State": {"Running": True, "Status": "running", "StartedAt": "", "FinishedAt": ""},
                            "Config": {"Image": "image:latest", "Labels": {"com.docker.compose.service": service}},
                            "NetworkSettings": {"Networks": {"stack_default": {"IPAddress": "172.18.0.2", "Gateway": "172.18.0.1"}}},
                            "RestartCount": 0,
                        }
                    )
                return self.command_result(command, pool_ops.json.dumps(payload))
            return self.command_result(command, returncode=1, stderr="unexpected command")

        with mock.patch.object(pool_ops, "run", side_effect=fake_run):
            inspected = pool_ops.docker_inspect(["postgres", "node", "pool"])

        self.assertEqual("stack-postgres-1", inspected["postgres"]["container_name"])
        self.assertEqual("postgres", inspected["postgres"]["compose_service"])
        self.assertTrue(inspected["node"]["running"])
        self.assertEqual(["172.18.0.2"], inspected["pool"]["network_ips"])

    def test_docker_command_wrappers_resolve_service_aliases(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs):
            calls.append(command)
            if command[:2] == ["docker", "ps"]:
                return self.command_result(
                    command,
                    "stack-postgres-1\tpostgres\nstack-node-1\tnode\nstack-pool-1\tpool\n",
                )
            if command == ["docker", "top", "stack-node-1"]:
                return self.command_result(command, "UID PID PPID C STIME TTY TIME CMD\nroot 1 0 0 now ? 0:00 bdag\n")
            if command == ["docker", "logs", "-n", "12", "stack-pool-1"]:
                return self.command_result(command, "pool ready\n")
            if command == [
                "docker",
                "exec",
                "stack-postgres-1",
                "psql",
                "-U",
                pool_ops.POOL_DB_USER,
                "-d",
                pool_ops.POOL_DB_NAME,
                "-t",
                "-A",
                "-c",
                "SELECT '[]'::json",
            ]:
                return self.command_result(command, "[]\n")
            if command == [
                "docker",
                "inspect",
                "stack-node-1",
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            ]:
                return self.command_result(command, "172.18.0.4\n")
            if command == ["docker", "exec", "stack-node-1", "cat", "/proc/net/tcp"]:
                return self.command_result(command, "")
            if command == ["docker", "exec", "stack-node-1", "cat", "/proc/net/tcp6"]:
                return self.command_result(command, "")
            return self.command_result(command, returncode=1, stderr="unexpected command")

        with mock.patch.object(pool_ops, "run", side_effect=fake_run):
            self.assertIn("bdag", pool_ops.docker_top("node"))
            self.assertIn("pool ready", pool_ops.docker_logs("pool", lines=12))
            self.assertEqual([], pool_ops.pool_db_json("SELECT '[]'::json"))
            self.assertEqual("172.18.0.4", pool_ops.docker_container_ip("node"))
            self.assertEqual([], pool_ops.container_peer_ips("node"))

        self.assertIn(["docker", "top", "stack-node-1"], calls)
        self.assertIn(["docker", "logs", "-n", "12", "stack-pool-1"], calls)
        self.assertIn(
            [
                "docker",
                "inspect",
                "stack-node-1",
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            ],
            calls,
        )
        self.assertIn(["docker", "exec", "stack-node-1", "cat", "/proc/net/tcp"], calls)


if __name__ == "__main__":
    unittest.main()
