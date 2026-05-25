#!/usr/bin/env python3

import pathlib
import sys
import unittest
from datetime import datetime, timezone

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402


class NoMinerCollectStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            name: getattr(pool_ops, name)
            for name in (
                "NODES",
                "OBSERVER_NODES",
                "SERVICES",
                "STACK_SERVICES",
                "POOL_CONTAINER",
                "POOL_CONTAINERS",
                "ensure_runtime",
                "docker_access_error",
                "local_ipv4_addresses",
                "default_miner_pool_settings",
                "run",
                "read_latest_action",
                "discover_observer_node_services",
                "docker_inspect",
                "docker_top",
                "docker_logs",
                "docker_logs_many",
                "collect_template_probe_health",
                "collect_pool_prometheus_metrics",
                "collect_miner_health",
                "collect_sync_progress",
                "observe_sync_progress_health",
                "read_sync_coordinator_state",
                "collect_host_pressure",
            )
        }
        self.old_time = pool_ops.time.time
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        for name, value in self.originals.items():
            setattr(pool_ops, name, value)
        pool_ops.time.time = self.old_time

    def test_no_miner_status_suppresses_template_and_rpc_noise(self) -> None:
        now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        pool_ops.time.time = lambda: now
        pool_ops.NODES = ["bdag-miner-node-2"]
        pool_ops.OBSERVER_NODES = []
        pool_ops.STACK_SERVICES = ["pool-db", "bdag-miner-node-2", "rpc-failover", "asic-pool"]
        pool_ops.SERVICES = list(pool_ops.STACK_SERVICES)
        pool_ops.POOL_CONTAINER = "asic-pool"
        pool_ops.POOL_CONTAINERS = ["asic-pool"]
        pool_ops.ensure_runtime = lambda: None
        pool_ops.docker_access_error = lambda: None
        pool_ops.local_ipv4_addresses = lambda: ["192.168.68.55"]
        pool_ops.default_miner_pool_settings = lambda: {
            "pool_url": "stratum+tcp://192.168.68.55:3334",
            "worker_user": "0x0000000000000000000000000000000000000000",
            "pool_password": "1234",
        }
        pool_ops.run = lambda command, timeout=20: pool_ops.CommandResult(command, 0, "", "", 0.0)
        pool_ops.read_latest_action = lambda: None
        pool_ops.discover_observer_node_services = lambda: []
        pool_ops.docker_top = lambda _name: (
            "UID PID PPID C STIME TTY TIME CMD\n"
            "root 1 0 0 12:00 ? 00:00:01 /usr/local/bin/bdag\n"
        )
        pool_ops.docker_logs = lambda _name, lines=160: ""
        pool_ops.docker_logs_many = lambda _names, lines=160: (
            "2026/05/25 11:59:30 GBT ERROR: connect: connection refused\n"
        )
        pool_ops.collect_host_pressure = lambda: {
            "iowait_percent": 10.0,
            "iowait_warning_active": False,
            "samples": [],
        }

        def fake_inspect(names):
            return {
                name: {
                    "name": name,
                    "image": "test",
                    "running": True,
                    "status": "running",
                    "restart_count": 0,
                    "exit_code": 0,
                    "error": "",
                    "ports": {},
                }
                for name in names
            }

        pool_ops.docker_inspect = fake_inspect
        pool_ops.collect_template_probe_health = lambda: {
            "generated_at": "2026-05-25T12:00:00+0000",
            "cached": False,
            "nodes": {
                "bdag-miner-node-2": {
                    "sample_count": 1,
                    "ok_count": 0,
                    "error_count": 1,
                    "last_error": "connection refused",
                    "failing": True,
                }
            },
            "rpc_failover": {
                "sample_count": 1,
                "ok_count": 0,
                "error_count": 1,
                "last_error": "connection refused",
                "failing": True,
            },
            "failing_nodes": ["bdag-miner-node-2"],
            "all_nodes_failing": True,
        }
        pool_ops.collect_pool_prometheus_metrics = lambda _containers: {
            "generated_at": "2026-05-25T12:00:00+0000",
            "status": "ok",
            "active_connections": 0,
            "selected_backend": "node2",
            "source_job_health": {},
            "source_backend_health": {},
            "selected_backend_source_health": {},
            "template_conversion_stall": {},
            "loss_ledger": {},
        }
        pool_ops.collect_miner_health = lambda: {
            "managed_count": 0,
            "connected_count": 0,
            "failures": [],
            "warnings": [],
            "miners": [],
        }
        pool_ops.collect_sync_progress = lambda: {
            "status": "synced",
            "percent": 100.0,
            "current_block": 8_658_598,
            "highest_block": 8_658_598,
            "remaining_blocks": 0,
            "source": "nodes",
            "error": "",
            "nodes": {
                "bdag-miner-node-2": {
                    "status": "synced",
                    "percent": 100.0,
                    "current_block": 8_658_598,
                    "highest_block": 8_658_598,
                    "remaining_blocks": 0,
                    "source": "bdag-miner-node-2",
                    "error": "",
                    "chain_block_count": 8_658_598,
                    "chain_main_height": 7_001_831,
                    "chain_rpc_source": "getBlockCount",
                    "chain_rpc_latency_ms": 3.3,
                    "chain_rpc_attempts": 1,
                    "chain_rpc_retry_limit": 2,
                    "chain_rpc_error": "",
                }
            },
        }
        pool_ops.observe_sync_progress_health = lambda _sync_progress: {
            "active_nodes": [],
            "active_node_count": 0,
            "node_rates_blocks_per_second": {},
            "lookback_seconds": 2700,
        }
        pool_ops.read_sync_coordinator_state = lambda: {}

        status = pool_ops.collect_status(include_logs=True)

        self.assertEqual(status["overall"], "ok")
        self.assertEqual(status["mode"], "ready_no_miners")
        self.assertFalse(status["pool_health"]["needs_fast_repair"])
        self.assertFalse(status["pool_health"]["rpc_refused"])
        self.assertTrue(status["pool_health"]["rpc_refused_raw"])
        self.assertEqual(status["sync_warnings"], [])
        joined_warnings = "\n".join(status["warnings"])
        self.assertNotIn("live mining template probes", joined_warnings)
        self.assertNotIn("pool recently saw RPC connection refused", joined_warnings)


if __name__ == "__main__":
    unittest.main()
