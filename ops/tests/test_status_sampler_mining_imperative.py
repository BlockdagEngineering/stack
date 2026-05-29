#!/usr/bin/env python3

import os
import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import pool_ops  # noqa: E402
import status_sampler  # noqa: E402


class StatusSamplerMiningImperativeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            name: getattr(status_sampler, name)
            for name in (
                "MINING_IMPERATIVE_REPAIR_ENABLED",
                "MINING_IMPERATIVE_GUARD_UNITS",
                "MINING_IMPERATIVE_START_POOL_ENABLED",
                "MINING_IMPERATIVE_START_IDLE_SYNCED_POOL",
                "append_incident",
                "log",
                "read_neighbor_macs",
                "run",
            )
        }
        self.original_env = dict(os.environ)
        self.addCleanup(self.restore)
        status_sampler.append_incident = lambda *args, **kwargs: {}
        status_sampler.log = lambda _message: None
        status_sampler.MINING_IMPERATIVE_REPAIR_ENABLED = True
        status_sampler.MINING_IMPERATIVE_START_POOL_ENABLED = True
        status_sampler.MINING_IMPERATIVE_START_IDLE_SYNCED_POOL = True

    def restore(self) -> None:
        for name, value in self.originals.items():
            setattr(status_sampler, name, value)
        os.environ.clear()
        os.environ.update(self.original_env)

    def command_result(self, command: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""):
        return pool_ops.CommandResult(command, returncode, stdout, stderr, 0.0)

    def stopped_pool_payload(self, sync_status: str = "syncing", remaining_blocks: int = 5) -> dict:
        return {
            "overall": "syncing" if sync_status != "synced" else "ok",
            "sync_warnings": [] if sync_status == "synced" else ["behind"],
            "containers": {"asic-pool": {"running": False}},
            "sync_progress": {
                "status": sync_status,
                "remaining_blocks": remaining_blocks,
                "chain_block_count": 1000,
            },
            "miner_health": {"connected_count": 0, "managed_count": 0},
            "pool": {"metrics": {"active_connections": 0}, "source_job_health": {}},
            "pool_metrics": {"active_connections": 0, "source_job_health": {}},
        }

    def test_starts_stopped_pool_when_asic_lan_neighbor_is_present(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["BDAG_ASIC_LAN_CIDRS"] = "192.168.50.0/24"
        status_sampler.read_neighbor_macs = lambda: {"192.168.50.177": "28:e2:97:1e:c0:b5"}

        def fake_run(command: list[str], timeout: int = 20):
            commands.append(command)
            return self.command_result(command)

        status_sampler.run = fake_run

        repair = status_sampler.mining_imperative_repair(self.stopped_pool_payload())

        self.assertIn(["docker", "start", "asic-pool"], commands)
        self.assertIn("started_container:asic-pool", repair["actions"])

    def test_starts_stopped_idle_pool_when_chain_is_synced(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["BDAG_ASIC_LAN_CIDRS"] = "192.168.50.0/24"
        status_sampler.read_neighbor_macs = lambda: {}
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(self.stopped_pool_payload(sync_status="synced", remaining_blocks=0))

        self.assertIn(["docker", "start", "asic-pool"], commands)
        self.assertIn("started_container:asic-pool", repair["actions"])

    def test_does_not_start_pool_without_miner_demand_or_ready_chain(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["BDAG_ASIC_LAN_CIDRS"] = "192.168.50.0/24"
        status_sampler.read_neighbor_macs = lambda: {}
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(self.stopped_pool_payload(sync_status="syncing", remaining_blocks=12))

        self.assertNotIn(["docker", "start", "asic-pool"], commands)
        self.assertEqual(repair["actions"], [])

    def test_reenables_guard_timer_when_it_drifts_disabled(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = ["bdag-stack-sentinel.timer"]

        def fake_run(command: list[str], timeout: int = 20):
            commands.append(command)
            if command[:3] == ["systemctl", "--user", "is-enabled"]:
                return self.command_result(command, 1, "disabled\n", "")
            if command[:3] == ["systemctl", "--user", "is-active"]:
                return self.command_result(command, 3, "inactive\n", "")
            return self.command_result(command)

        status_sampler.run = fake_run
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"]["asic-pool"]["running"] = True

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn(["systemctl", "--user", "enable", "--now", "bdag-stack-sentinel.timer"], commands)
        self.assertIn("repaired_unit:bdag-stack-sentinel.timer", repair["actions"])


if __name__ == "__main__":
    unittest.main()
