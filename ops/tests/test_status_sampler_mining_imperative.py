#!/usr/bin/env python3

import os
import pathlib
import sys
import tempfile
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
                "MINING_IMPERATIVE_MINER_TRACKING_REPAIR_ENABLED",
                "MINING_IMPERATIVE_CONSTRAINED_FASTARTIFACT_REPAIR_ENABLED",
                "MINING_IMPERATIVE_NODE_MINING_REPAIR_ENABLED",
                "MINING_IMPERATIVE_NODE_BACKEND_REPAIR_ENABLED",
                "MINING_IMPERATIVE_NODE_BACKEND_REPAIR_COOLDOWN_SECONDS",
                "MINING_IMPERATIVE_FASTSYNC_PEER_QUARANTINE_ENABLED",
                "CATCHUP_PAUSE_ENABLED",
                "CATCHUP_PAUSE_THRESHOLD_BLOCKS",
                "CATCHUP_NODE_RECREATE_ENABLED",
                "CATCHUP_NODE_CACHE_MB",
                "CATCHUP_NODE_CACHE_MIN_MB",
                "CATCHUP_NODE_CACHE_MEMORY_PERCENT",
                "CATCHUP_IO_PRESSURE_PAUSE_ENABLED",
                "CATCHUP_IO_PRESSURE_MIN_LAG_BLOCKS",
                "CATCHUP_IOWAIT_WARN_PERCENT",
                "CATCHUP_IO_SOME_AVG10_WARN",
                "CATCHUP_IO_FULL_AVG10_WARN",
                "append_incident",
                "collect_pool_activity",
                "detect_total_memory_bytes",
                "log",
                "POOL_ENV_FILE",
                "PROJECT_ROOT",
                "REPAIR_STATE_FILE",
                "read_neighbor_macs",
                "read_miner_registry",
                "run",
                "save_miner_registry",
                "set_runtime_env_value",
                "upsert_pool_activity_miners",
            )
        }
        self.tmpdir = tempfile.TemporaryDirectory()
        self.original_env = dict(os.environ)
        self.addCleanup(self.restore)
        status_sampler.append_incident = lambda *args, **kwargs: {}
        status_sampler.log = lambda _message: None
        status_sampler.MINING_IMPERATIVE_REPAIR_ENABLED = True
        status_sampler.MINING_IMPERATIVE_START_POOL_ENABLED = True
        status_sampler.MINING_IMPERATIVE_START_IDLE_SYNCED_POOL = True
        status_sampler.MINING_IMPERATIVE_MINER_TRACKING_REPAIR_ENABLED = True
        status_sampler.MINING_IMPERATIVE_CONSTRAINED_FASTARTIFACT_REPAIR_ENABLED = True
        status_sampler.MINING_IMPERATIVE_NODE_MINING_REPAIR_ENABLED = True
        status_sampler.MINING_IMPERATIVE_NODE_BACKEND_REPAIR_ENABLED = True
        status_sampler.MINING_IMPERATIVE_NODE_BACKEND_REPAIR_COOLDOWN_SECONDS = 120.0
        status_sampler.MINING_IMPERATIVE_FASTSYNC_PEER_QUARANTINE_ENABLED = True
        status_sampler.POOL_ENV_FILE = pathlib.Path("/nonexistent/status-sampler-test.env")
        status_sampler.PROJECT_ROOT = pathlib.Path("/nonexistent/status-sampler-test-root")
        status_sampler.REPAIR_STATE_FILE = pathlib.Path(self.tmpdir.name) / "repair-state.json"
        status_sampler.CATCHUP_PAUSE_ENABLED = True
        status_sampler.CATCHUP_PAUSE_THRESHOLD_BLOCKS = 300
        status_sampler.CATCHUP_NODE_RECREATE_ENABLED = True
        status_sampler.CATCHUP_NODE_CACHE_MB = 6144
        status_sampler.CATCHUP_NODE_CACHE_MIN_MB = 1024
        status_sampler.CATCHUP_NODE_CACHE_MEMORY_PERCENT = 40.0
        status_sampler.CATCHUP_IO_PRESSURE_PAUSE_ENABLED = True
        status_sampler.CATCHUP_IO_PRESSURE_MIN_LAG_BLOCKS = 25
        status_sampler.CATCHUP_IOWAIT_WARN_PERCENT = 15.0
        status_sampler.CATCHUP_IO_SOME_AVG10_WARN = 20.0
        status_sampler.CATCHUP_IO_FULL_AVG10_WARN = 10.0
        os.environ["BDAG_ALLOW_UNSYNCED_NODE_MINING"] = "0"
        os.environ["BDAG_ENABLE_NODE_MINING"] = "0"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag"
        os.environ["BDAG_NODE_MINING_ARGS"] = ""
        os.environ["MINING_ADDRESS"] = ""
        os.environ["MINING_POOL_ADDRESS"] = ""
        os.environ["NODE_ARGS_APPEND"] = ""
        os.environ["POOL_COINBASE_ADDRESS"] = ""
        for key in (
            "BDAG_COMPOSE_PROJECT_NAME",
            "COMPOSE_PROJECT_NAME",
        ):
            os.environ.pop(key, None)

    def restore(self) -> None:
        for name, value in self.originals.items():
            setattr(status_sampler, name, value)
        os.environ.clear()
        os.environ.update(self.original_env)
        self.tmpdir.cleanup()

    def command_result(self, command: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""):
        return pool_ops.CommandResult(command, returncode, stdout, stderr, 0.0)

    def stopped_pool_payload(self, sync_status: str = "syncing", remaining_blocks: int = 5) -> dict:
        return {
            "overall": "syncing" if sync_status != "synced" else "ok",
            "sync_warnings": [] if sync_status == "synced" else ["behind"],
            "containers": {status_sampler.POOL_CONTAINER: {"running": False}},
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
        os.environ["BDAG_ASIC_LAN_CIDRS"] = "192.168.1.0/24"
        status_sampler.read_neighbor_macs = lambda: {"192.168.1.107": "28:e2:97:1e:c0:b5"}

        def fake_run(command: list[str], timeout: int = 20):
            commands.append(command)
            return self.command_result(command)

        status_sampler.run = fake_run

        repair = status_sampler.mining_imperative_repair(self.stopped_pool_payload())

        self.assertIn(["docker", "start", status_sampler.POOL_CONTAINER], commands)
        self.assertIn(f"started_container:{status_sampler.POOL_CONTAINER}", repair["actions"])

    def test_compose_command_uses_stable_project_name_for_symlinked_runtime(self) -> None:
        os.environ.pop("BDAG_COMPOSE_PROJECT_NAME", None)
        os.environ.pop("COMPOSE_PROJECT_NAME", None)
        os.environ["BDAG_PROJECT_ROOT"] = "/home/jeremy/blockdag-asic-pool"

        command = pool_ops.docker_compose_command("ps")

        self.assertIn("-p", command)
        self.assertEqual(command[command.index("-p") + 1], "blockdag-asic-pool")

    def test_starts_stopped_idle_pool_when_chain_is_synced(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["BDAG_ASIC_LAN_CIDRS"] = "192.168.1.0/24"
        status_sampler.read_neighbor_macs = lambda: {}
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(self.stopped_pool_payload(sync_status="synced", remaining_blocks=0))

        self.assertIn(["docker", "start", status_sampler.POOL_CONTAINER], commands)
        self.assertIn(f"started_container:{status_sampler.POOL_CONTAINER}", repair["actions"])

    def test_does_not_start_pool_without_miner_demand_or_ready_chain(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["BDAG_ASIC_LAN_CIDRS"] = "192.168.1.0/24"
        status_sampler.read_neighbor_macs = lambda: {}
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(self.stopped_pool_payload(sync_status="syncing", remaining_blocks=12))

        self.assertNotIn(["docker", "start", status_sampler.POOL_CONTAINER], commands)
        self.assertEqual(repair["actions"], [])

    def test_catchup_pause_stops_pool_and_removes_node_mining_churn(self) -> None:
        commands = []
        env_updates = {}
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["BDAG_ENABLE_NODE_MINING"] = "1"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag,miner"
        os.environ["BDAG_NODE_MINING_ARGS"] = "--miner --miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        os.environ["NODE_ARGS_APPEND"] = os.environ["BDAG_NODE_MINING_ARGS"]
        os.environ["BDAG_NODE_SERVICES"] = "node"
        payload = self.stopped_pool_payload(sync_status="syncing", remaining_blocks=450)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}
        payload["catchup_policy"] = {
            "active": True,
            "lag_blocks": 450,
            "threshold_blocks": 300,
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            status_sampler.PROJECT_ROOT = root
            (root / "node.conf").write_text(
                "\n".join(
                    [
                        "cache=2048",
                        "cache.database=70",
                        "miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc",
                        "modules=Blockdag",
                        "modules=miner",
                        "miner=true",
                        'evmenv="--metrics --cache 2048"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            def fake_set_runtime_env(key: str, value: str):
                env_updates[key] = value
                os.environ[key] = value
                return [f"/runtime/{key}"]

            def fake_run(command: list[str], timeout: int = 20):
                commands.append(command)
                return self.command_result(command)

            status_sampler.set_runtime_env_value = fake_set_runtime_env
            status_sampler.detect_total_memory_bytes = lambda: 16 * 1024 * 1024 * 1024
            status_sampler.run = fake_run

            repair = status_sampler.mining_imperative_repair(payload)
            node_conf = (root / "node.conf").read_text(encoding="utf-8")

        self.assertIn(f"stopped_container:{status_sampler.POOL_CONTAINER}:catchup_pause", repair["actions"])
        self.assertIn("applied_catchup_node_runtime", repair["actions"])
        self.assertEqual(env_updates["BDAG_ENABLE_NODE_MINING"], "0")
        self.assertEqual(env_updates["BDAG_NODE_MODULES"], "Blockdag")
        self.assertEqual(env_updates["BDAG_NODE_MINING_ARGS"], "")
        self.assertEqual(env_updates["NODE_ARGS_APPEND"], "")
        self.assertIn("cache=6144", node_conf)
        self.assertIn("--cache 6144", node_conf)
        self.assertIn("miningaddr=", node_conf)
        self.assertIn("# modules=miner disabled during catch-up pause", node_conf)
        self.assertIn("# miner=true disabled during catch-up pause", node_conf)
        self.assertTrue(any(command[-2:] == ["stop", status_sampler.POOL_CONTAINER] for command in commands))
        self.assertTrue(any("--force-recreate" in command for command in commands))

    def test_catchup_pause_does_not_restart_stopped_pool_for_visible_miners(self) -> None:
        commands = []
        env_updates = {}
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["BDAG_ENABLE_NODE_MINING"] = "0"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag"
        os.environ["BDAG_NODE_MINING_ARGS"] = ""
        os.environ["NODE_ARGS_APPEND"] = ""
        payload = self.stopped_pool_payload(sync_status="syncing", remaining_blocks=450)
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}
        payload["catchup_policy"] = {
            "active": True,
            "lag_blocks": 450,
            "threshold_blocks": 300,
        }

        def fake_set_runtime_env(key: str, value: str):
            env_updates[key] = value
            os.environ[key] = value
            return [f"/runtime/{key}"]

        status_sampler.set_runtime_env_value = fake_set_runtime_env
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        with tempfile.TemporaryDirectory() as tmp:
            status_sampler.PROJECT_ROOT = pathlib.Path(tmp)
            repair = status_sampler.mining_imperative_repair(payload)

        self.assertNotIn(f"started_container:{status_sampler.POOL_CONTAINER}", repair["actions"])
        self.assertNotIn(f"stopped_container:{status_sampler.POOL_CONTAINER}:catchup_pause", repair["actions"])
        self.assertFalse(any(command[:2] == ["docker", "start"] for command in commands))

    def test_backend_recovery_recreates_dead_node_and_blocks_pool_start(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["BDAG_NODE_SERVICES"] = "node"
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"]["node"] = {"running": True}
        payload["nodes"] = {
            "node": {
                "affects_production_health": True,
                "child_running": False,
                "tail": ['SetupConfig: invalid BDAG_NETWORK "mainnet" (use devnet, testnet, privnet, mixnet)'],
            }
        }

        def fake_run(command: list[str], timeout: int = 20):
            commands.append(command)
            return self.command_result(command)

        status_sampler.run = fake_run

        repair = status_sampler.mining_imperative_repair(payload)
        state = status_sampler.read_repair_state()

        self.assertIn("recreated_node_backend", repair["actions"])
        self.assertTrue(any("--force-recreate" in command and "node" in command for command in commands))
        self.assertFalse(any(command[:2] == ["docker", "start"] for command in commands))
        self.assertEqual(
            state["last_node_backend_repair"]["fatal_config_errors"][0]["code"],
            "invalid_bdag_network",
        )

    def test_backend_recovery_pauses_running_pool_before_recreate(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["BDAG_NODE_SERVICES"] = "node"
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["containers"]["node"] = {"running": True}
        payload["nodes"] = {
            "node": {
                "affects_production_health": True,
                "child_running": False,
                "tail": [],
            }
        }

        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(payload)
        pause = status_sampler.pool_pause_state()

        self.assertIn(f"stopped_container:{status_sampler.POOL_CONTAINER}:backend_recovery", repair["actions"])
        self.assertIn("recreated_node_backend", repair["actions"])
        self.assertEqual(pause["type"], "backend_recovery")
        self.assertTrue(any(command[-2:] == ["stop", status_sampler.POOL_CONTAINER] for command in commands))

    def test_starts_pool_after_backend_recovery_when_chain_is_ready(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        status_sampler.write_repair_state(
            {
                "pool_pause": {
                    "active": True,
                    "type": "backend_recovery",
                    "reason": "node backend was not healthy",
                    "started_at_epoch": 1,
                }
            }
        )
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"]["node"] = {"running": True}
        payload["nodes"] = {"node": {"affects_production_health": True, "child_running": True, "tail": []}}

        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn(["docker", "start", status_sampler.POOL_CONTAINER], commands)
        self.assertIn(f"started_container:{status_sampler.POOL_CONTAINER}", repair["actions"])
        self.assertEqual(status_sampler.pool_pause_state(), {})

    def test_io_pressure_without_backend_evidence_does_not_create_zero_lag_pause(self) -> None:
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["can_mine"] = False
        payload["host_pressure"] = {
            "iowait_percent": 22.0,
            "io_some_avg10": 28.0,
            "io_full_avg10": 23.0,
        }

        policy = status_sampler.catchup_policy_from_payload(payload)

        self.assertFalse(policy["active"])
        self.assertFalse(policy["io_pressure_active"])
        self.assertFalse(policy["backend_unready_under_pressure"])

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
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn(["systemctl", "--user", "enable", "--now", "bdag-stack-sentinel.timer"], commands)
        self.assertIn("repaired_unit:bdag-stack-sentinel.timer", repair["actions"])

    def test_repairs_missing_tracked_miners_from_pool_activity(self) -> None:
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 0, "connected_count": 1, "managed_count": 0}
        activity = {"miners": [{"ip": "172.18.0.1"}], "unattributed_valid_shares": 8, "unattributed_blocks": 1}
        status_sampler.collect_pool_activity = lambda lines=0: activity
        status_sampler.upsert_pool_activity_miners = lambda _activity: {
            "miners": [{"ip": "192.168.1.107", "mac": "28:e2:97:1e:c0:b5"}]
        }
        status_sampler.read_miner_registry = lambda: {"miners": []}

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn("repaired_tracked_miners", repair["actions"])

    def test_disables_fastartifact_on_constrained_synced_mining_profile(self) -> None:
        commands = []
        env_updates = {}
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["BDAG_DETECTED_NETWORK_TOPOLOGY"] = "asic-router"
        os.environ["BDAG_STORAGE_PROFILE"] = "usb-chain-internal-runtime"
        os.environ["BDAG_FASTARTIFACTSYNC_ENABLED"] = "1"
        os.environ["NODE_ARGS_APPEND"] = "--fastartifactsync"
        os.environ["BDAG_NODE_SERVICES"] = "bdag-miner-node-1"
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}

        def fake_set_runtime_env(key: str, value: str):
            env_updates[key] = value
            os.environ[key] = value
            return [f"/runtime/{key}={value}"]

        status_sampler.set_runtime_env_value = fake_set_runtime_env

        def fake_run(command: list[str], timeout: int = 20):
            commands.append(command)
            return self.command_result(command)

        status_sampler.run = fake_run

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn("disabled_constrained_fastartifact", repair["actions"])
        self.assertEqual(env_updates["BDAG_FASTARTIFACTSYNC_ENABLED"], "0")
        self.assertEqual(env_updates["SYNC_SOURCE_NODE"], "0")
        self.assertEqual(env_updates["BDAG_NO_FASTSYNC_SERVE"], "1")
        self.assertEqual(env_updates["NODE_ARGS_APPEND"], "")
        self.assertTrue(any("--force-recreate" in command for command in commands))

    def test_enables_node_mining_template_support_when_miner_is_present(self) -> None:
        commands = []
        env_updates = {}
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["MINING_ADDRESS"] = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        os.environ["BDAG_ENABLE_NODE_MINING"] = "0"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag"
        os.environ["BDAG_NODE_MINING_ARGS"] = ""
        os.environ["BDAG_NODE_SERVICES"] = "bdag-miner-node-1"
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}

        def fake_set_runtime_env(key: str, value: str):
            env_updates[key] = value
            os.environ[key] = value
            return [f"/runtime/{key}"]

        def fake_run(command: list[str], timeout: int = 20):
            commands.append(command)
            return self.command_result(command)

        status_sampler.set_runtime_env_value = fake_set_runtime_env
        status_sampler.run = fake_run

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn("enabled_node_mining_template_support", repair["actions"])
        self.assertEqual(env_updates["BDAG_ENABLE_NODE_MINING"], "1")
        self.assertEqual(env_updates["BDAG_NODE_MODULES"], "Blockdag")
        self.assertIn("--miner", env_updates["NODE_ARGS_APPEND"])
        self.assertIn("--miner", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertIn("--miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertNotIn("--allowminingwhennearlysynced", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertNotIn("--allowsubmitwhennotsynced", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertEqual(env_updates["NODE_ARGS_APPEND"], env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertTrue(any("--force-recreate" in command for command in commands))

    def test_node_args_parser_accepts_nodeworker_embedded_node_args(self) -> None:
        address = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        command_line = f"nodeworker --node-args=--miner --miningaddr={address}"

        self.assertTrue(status_sampler.node_mining_args_are_safe_and_complete(command_line, address))

    def test_repairs_node_mining_args_when_unsafe_sync_bypass_is_present(self) -> None:
        commands = []
        env_updates = {}
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["MINING_ADDRESS"] = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        os.environ["BDAG_ENABLE_NODE_MINING"] = "1"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag"
        os.environ["BDAG_NODE_MINING_ARGS"] = (
            "--allowminingwhennearlysynced --allowsubmitwhennotsynced --miner "
            "--miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        )
        os.environ["BDAG_NODE_SERVICES"] = "bdag-miner-node-1"
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}

        def fake_set_runtime_env(key: str, value: str):
            env_updates[key] = value
            os.environ[key] = value
            return [f"/runtime/{key}"]

        status_sampler.set_runtime_env_value = fake_set_runtime_env
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn("enabled_node_mining_template_support", repair["actions"])
        self.assertIn("--miner", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertNotIn("--allowminingwhennearlysynced", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertNotIn("--allowsubmitwhennotsynced", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertEqual(env_updates["NODE_ARGS_APPEND"], env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertTrue(any("--force-recreate" in command for command in commands))

    def test_repairs_unsupported_miner_rpc_module(self) -> None:
        commands = []
        env_updates = {}
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["MINING_ADDRESS"] = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        os.environ["BDAG_ENABLE_NODE_MINING"] = "1"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag,miner"
        os.environ["BDAG_NODE_MINING_ARGS"] = (
            "--miner --miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        )
        os.environ["BDAG_NODE_SERVICES"] = "bdag-miner-node-1"
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}

        def fake_set_runtime_env(key: str, value: str):
            env_updates[key] = value
            os.environ[key] = value
            return [f"/runtime/{key}"]

        status_sampler.set_runtime_env_value = fake_set_runtime_env
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn("enabled_node_mining_template_support", repair["actions"])
        self.assertEqual(env_updates["BDAG_NODE_MODULES"], "Blockdag")
        self.assertNotIn("--allowsubmitwhennotsynced", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertTrue(any("--force-recreate" in command for command in commands))

    def test_recreates_node_when_live_process_has_unsafe_sync_bypass(self) -> None:
        commands = []
        env_updates = {}
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["MINING_ADDRESS"] = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        os.environ["BDAG_ENABLE_NODE_MINING"] = "1"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag"
        os.environ["BDAG_NODE_MINING_ARGS"] = (
            "--miner --miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        )
        os.environ["BDAG_NODE_SERVICES"] = "bdag-miner-node-1"
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}

        def fake_set_runtime_env(key: str, value: str):
            env_updates[key] = value
            os.environ[key] = value
            return [f"/runtime/{key}"]

        def fake_run(command: list[str], timeout: int = 20):
            commands.append(command)
            if "exec" in command and "-T" in command and any("ps -eo args" in part for part in command):
                stdout = (
                    "nodeworker --node-args=--allowminingwhennearlysynced --allowsubmitwhennotsynced --miner "
                    "--miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc\n"
                )
                return self.command_result(command, stdout=stdout)
            return self.command_result(command)

        status_sampler.set_runtime_env_value = fake_set_runtime_env
        status_sampler.run = fake_run

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn("enabled_node_mining_template_support", repair["actions"])
        self.assertIn("--miner", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertNotIn("--allowminingwhennearlysynced", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertNotIn("--allowsubmitwhennotsynced", env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertEqual(env_updates["NODE_ARGS_APPEND"], env_updates["BDAG_NODE_MINING_ARGS"])
        self.assertTrue(any("--force-recreate" in command for command in commands))

    def test_does_not_enable_node_mining_without_valid_address(self) -> None:
        commands = []
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["MINING_ADDRESS"] = "0x0000000000000000000000000000000000000000"
        os.environ["BDAG_ENABLE_NODE_MINING"] = "0"
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}
        status_sampler.run = lambda command, timeout=20: commands.append(command) or self.command_result(command)

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertNotIn("enabled_node_mining_template_support", repair["actions"])
        self.assertFalse(any("--force-recreate" in command for command in commands))

    def test_quarantines_fastsync_peer_returning_only_orphan_blocks(self) -> None:
        commands = []
        env_updates = {}
        peer_id = "16Uiu2HAkvvmkRJXJAZAWq3bFDzBAFQwQJ88PQqMedULsrv4t3XCD"
        status_sampler.MINING_IMPERATIVE_GUARD_UNITS = []
        os.environ["BDAG_DETECTED_NETWORK_TOPOLOGY"] = "asic-router"
        os.environ["BDAG_STORAGE_PROFILE"] = "usb-chain-internal-runtime"
        os.environ["BDAG_FASTARTIFACTSYNC_ENABLED"] = "0"
        os.environ["BDAG_ENABLE_NODE_MINING"] = "1"
        os.environ["BDAG_NODE_MODULES"] = "Blockdag"
        os.environ["MINING_ADDRESS"] = "0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc"
        os.environ["BDAG_NODE_MINING_ARGS"] = (
            "--miner --miningaddr=0xA1Ee1005c4Ff181e93e717D2C624554b66AB7DFc --maxinbound=1"
        )
        os.environ["NODE1_PEER_ADDRESSES"] = f"/ip4/10.0.0.2/tcp/8151/p2p/{peer_id},/ip4/3.3.3.3/tcp/8150/p2p/good"
        os.environ["BDAG_FASTSYNC_PEERS"] = f"/ip4/10.0.0.2/tcp/8151/p2p/{peer_id}"
        os.environ["BOOTSTRAP_PEER_ADDRESSES"] = f"/ip4/10.0.0.2/tcp/8151/p2p/{peer_id},/ip4/4.4.4.4/tcp/8150/p2p/good"
        os.environ["BDAG_NODE_SERVICES"] = "bdag-miner-node-1"
        payload = self.stopped_pool_payload(sync_status="synced", remaining_blocks=0)
        payload["containers"][status_sampler.POOL_CONTAINER]["running"] = True
        payload["miner_health"] = {"tracked_count": 1, "connected_count": 1, "managed_count": 1}
        payload["nodes"] = {
            "bdag-miner-node-1": {
                "tail": [
                    "Fast-sync range returned only orphan blocks; falling back to legacy sync DAG "
                    f"module=SYNC peer={peer_id} processID=54"
                ]
            }
        }

        def fake_set_runtime_env(key: str, value: str):
            env_updates[key] = value
            os.environ[key] = value
            return [f"/runtime/{key}"]

        def fake_run(command: list[str], timeout: int = 20):
            commands.append(command)
            return self.command_result(command)

        status_sampler.set_runtime_env_value = fake_set_runtime_env
        status_sampler.run = fake_run

        repair = status_sampler.mining_imperative_repair(payload)

        self.assertIn("quarantined_fastsync_orphan_peer", repair["actions"])
        self.assertNotIn(peer_id, env_updates["NODE1_PEER_ADDRESSES"])
        self.assertNotIn(peer_id, env_updates["BDAG_FASTSYNC_PEERS"])
        self.assertNotIn(peer_id, env_updates["BOOTSTRAP_PEER_ADDRESSES"])
        self.assertTrue(any("--force-recreate" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
