#!/usr/bin/env python3

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import watchdog  # noqa: E402
import pool_ops  # noqa: E402


def node_status(*, importing: bool, last_import_age_seconds: int, latest_block: int = 1000) -> dict[str, object]:
    return {
        "nodes": {
            "node": {
                "child_running": True,
                "importing": importing,
                "latest_block": latest_block,
                "last_import_age_seconds": last_import_age_seconds,
            }
        },
        "sync_progress": {
            "nodes": {
                "node": {
                    "current_block": latest_block,
                    "remaining_blocks": 100,
                    "status": "syncing",
                }
            }
        },
        "pool_health": {"initial_download": True},
        "sync_health": {"needs_fast_sync_repair": True},
    }


def advisory_evm_sync_with_ready_native_mining_status() -> dict[str, object]:
    return {
        "failures": [],
        "stack_failures": [],
        "miner_failures": [],
        "warnings": [],
        "overall": "ok",
        "mode": "mining",
        "can_submit_blocks": True,
        "containers": {
            "node": {"running": True, "started_at": "2026-01-14T12:00:00.000000000Z"},
            watchdog.POOL_CONTAINER: {"running": True, "started_at": "2026-01-14T12:00:00.000000000Z"},
        },
        "nodes": {
            "node": {
                "child_running": True,
                "importing": False,
                "latest_block": 12552152,
                "last_import_age_seconds": 10,
            }
        },
        "sync_progress": {
            "status": "syncing",
            "current_block": 12159946,
            "highest_block": 12192806,
            "remaining_blocks": 32860,
            "sync_current_block": 12159946,
            "sync_highest_block": 12192806,
            "chain_block_count": 12552152,
            "p2p_network_height": 12552145,
            "p2p_network_gap": 0,
            "native_is_current": True,
            "mining_advisory_sync": True,
            "evm_chain_syncing": True,
            "evm_sync_advisory": "eth_syncing active while native P2P mining state is current",
        },
        "sync_health": {},
        "pool_health": {
            "source_selected_backend_submit_ready": True,
            "source_selected_backend_mineable": True,
            "source_selected_backend_p2p_fresh": True,
        },
        "pool_job_state": {
            "status": "ok",
            "reason_code": "ok",
            "active_connections": 4,
            "authorized_connections": 4,
            "ready_connections": 4,
            "connections_without_current_job": 0,
        },
        "miner_health": {"connected_count": 4, "connected_count_effective": 4, "miners": []},
        "mining_address": "0x1111111111111111111111111111111111111111",
    }


def fresh_paid_backend_readiness_flicker_status() -> dict[str, object]:
    return {
        "failures": [],
        "stack_failures": [],
        "miner_failures": [],
        "warnings": ["template health unsafe: mineable=false; submit_ready=false"],
        "overall": "syncing",
        "mode": "mining",
        "can_submit_blocks": False,
        "containers": {
            "node": {"running": True, "started_at": "2026-01-14T12:00:00.000000000Z"},
            watchdog.POOL_CONTAINER: {"running": True, "started_at": "2026-01-14T12:00:00.000000000Z"},
        },
        "sync_progress": {
            "status": "syncing",
            "source": "template-health",
            "error": "template health unsafe: mineable=false; submit_ready=false",
            "remaining_blocks": 0,
            "nodes": {"node": {"status": "synced", "current_block": 12552152, "remaining_blocks": 0}},
        },
        "sync_health": {
            "needs_fast_sync_repair": True,
            "pool_has_recent_paid_work": True,
        },
        "pool_health": {
            "selected_backend": "node",
            "source_selected_backend_submit_ready": False,
            "source_selected_backend_mineable": False,
            "source_selected_backend_p2p_fresh": True,
            "block_submit_success_count": 10,
            "last_block_submit_age_seconds": 20,
            "selected_backend_source_health": {
                "healthy": True,
                "node_mineable": False,
                "node_submit_ready": False,
                "node_p2p_mining_fresh": True,
                "node_p2p_best_peer_lead_blocks": 0,
                "node_p2p_peer_lead_tolerance_blocks": 10,
                "node_template_coinbase_valid": True,
                "node_template_age_seconds": 1,
                "ws_connected": True,
            },
            "template_health": {
                "safe_for_mining": False,
                "blocking_reasons": ["mineable=false", "submit_ready=false"],
                "mineable_now": False,
                "submit_ready": False,
                "p2p_mining_fresh": True,
                "p2p_best_peer_lead_blocks": 0,
                "p2p_peer_lead_tolerance_blocks": 10,
                "template_coinbase_valid": True,
                "template_age_seconds": 1,
            },
        },
        "pool_job_state": {
            "status": "ok",
            "reason_code": "ok",
            "active_connections": 4,
            "authorized_connections": 4,
            "ready_connections": 4,
            "connections_without_current_job": 0,
        },
        "miner_health": {"connected_count": 4, "connected_count_effective": 4, "managed_count": 4, "miners": []},
        "mining_address": "0x1111111111111111111111111111111111111111",
    }


def peer_lead_stall_status(*, recent_paid_work: bool = False) -> dict[str, object]:
    last_block_age = 20 if recent_paid_work else None
    block_success_count = 10 if recent_paid_work else 0
    return {
        "failures": [],
        "stack_failures": [],
        "miner_failures": [],
        "warnings": [
            "selected pool backend is still catching up by 40 blocks according to pool backend health"
        ],
        "mode": "sync_blocked",
        "overall": "syncing",
        "can_submit_blocks": False,
        "containers": {
            "node": {"running": True, "started_at": "2026-01-14T12:00:00.000000000Z"},
            watchdog.POOL_CONTAINER: {"running": True, "started_at": "2026-01-14T12:00:00.000000000Z"},
        },
        "nodes": {
            "node": {
                "child_running": True,
                "importing": False,
                "latest_block": 1000,
                "last_import_age_seconds": 900,
            }
        },
        "sync_progress": {
            "status": "syncing",
            "current_block": 1000,
            "highest_block": 1040,
            "remaining_blocks": 40,
            "peer_ahead_blocks": 40,
            "nodes": {
                "node": {
                    "current_block": 1000,
                    "highest_block": 1040,
                    "remaining_blocks": 40,
                    "status": "syncing",
                }
            },
        },
        "sync_health": {
            "needs_fast_sync_repair": True,
            "pool_has_recent_paid_work": recent_paid_work,
        },
        "pool_health": {
            "initial_download": True,
            "selected_backend": "node",
            "selected_backend_source_health": {
                "node_mineable": False,
                "node_submit_ready": False,
                "node_p2p_mining_fresh": False,
                "node_p2p_best_peer_lead_blocks": 40,
                "node_p2p_peer_lead_tolerance_blocks": 10,
                "node_template_age_seconds": 90,
                "node_reason_code": "node_syncing",
                "node_p2p_mining_fresh_reason_code": "peer_lead_exceeds_tolerance",
            },
            "source_selected_backend_submit_ready": False,
            "source_selected_backend_mineable": False,
            "source_selected_backend_p2p_fresh": False,
            "source_job_health": {
                "ok": False,
                "authorized_miners": 4,
                "ready_miners": 0,
                "reason_code": "miners_without_current_job",
            },
            "block_submit_success_count": block_success_count,
            "last_block_submit_age_seconds": last_block_age,
        },
        "pool_job_state": {
            "active_connections": 4,
            "authorized_connections": 4,
            "ready_connections": 0,
            "connections_without_current_job": 4,
            "current_template_seq": 0,
            "reason_code": "miners_without_current_job",
        },
        "miner_health": {"connected_count": 4, "connected_count_effective": 4, "managed_count": 4, "miners": []},
        "mining_address": "0x1111111111111111111111111111111111111111",
    }


def peer_starvation_status(*, recent_paid_work: bool = False, ready_miners: int = 0) -> dict[str, object]:
    status = peer_lead_stall_status(recent_paid_work=recent_paid_work)
    pool_health = status["pool_health"]
    assert isinstance(pool_health, dict)
    selected = pool_health["selected_backend_source_health"]
    assert isinstance(selected, dict)
    selected["node_p2p_best_peer_lead_blocks"] = 0
    selected["node_p2p_fresh_consensus_peer_count"] = 0
    selected["node_p2p_consensus_peer_count"] = 0
    selected["node_template_age_seconds"] = 14
    selected["node_p2p_mining_fresh_reason_code"] = "insufficient_fresh_peers"
    selected["node_reason_code"] = "node_syncing"
    source_job = pool_health["source_job_health"]
    assert isinstance(source_job, dict)
    source_job["ready_miners"] = ready_miners
    source_job["max_current_job_age_seconds"] = 52
    source_job["reason_code"] = "invalidated_current_job" if ready_miners == 0 else "ok"
    job_state = status["pool_job_state"]
    assert isinstance(job_state, dict)
    job_state["ready_connections"] = ready_miners
    job_state["connections_without_current_job"] = 0
    job_state["last_broadcast_age_ms"] = 52_000 if ready_miners == 0 else 1_000
    job_state["reason_code"] = "invalidated_current_job" if ready_miners == 0 else "ok"
    return status


def native_current_template_sync_wedge_status(*, recent_paid_work: bool = False) -> dict[str, object]:
    last_block_age = 20 if recent_paid_work else None
    block_success_count = 10 if recent_paid_work else 0
    return {
        "failures": [],
        "stack_failures": [],
        "miner_failures": [],
        "warnings": ["template health unsafe: node pending-template backend is syncing"],
        "mode": "sync_blocked",
        "overall": "syncing",
        "can_submit_blocks": False,
        "containers": {
            "node": {"running": True, "started_at": "2026-01-14T12:00:00.000000000Z"},
            watchdog.POOL_CONTAINER: {"running": True, "started_at": "2026-01-14T12:00:00.000000000Z"},
        },
        "nodes": {
            "node": {
                "child_running": True,
                "importing": False,
                "latest_block": 12552152,
                "last_import_age_seconds": 900,
            }
        },
        "sync_progress": {
            "status": "syncing",
            "source": "template-health",
            "error": "node pending-template backend is syncing",
            "current_block": 12552152,
            "highest_block": 12552152,
            "remaining_blocks": 0,
            "peer_ahead_blocks": 0,
            "nodes": {
                "node": {
                    "current_block": 12552152,
                    "highest_block": 12552152,
                    "remaining_blocks": 0,
                    "peer_ahead_blocks": 0,
                    "status": "synced",
                }
            },
        },
        "sync_health": {
            "needs_fast_sync_repair": True,
            "pool_has_recent_paid_work": recent_paid_work,
        },
        "pool_health": {
            "selected_backend": "node",
            "source_selected_backend_submit_ready": False,
            "source_selected_backend_mineable": False,
            "source_selected_backend_p2p_fresh": True,
            "block_submit_success_count": block_success_count,
            "last_block_submit_age_seconds": last_block_age,
            "selected_backend_source_health": {
                "node_mineable": False,
                "node_submit_ready": False,
                "node_get_block_template_ready": False,
                "node_p2p_mining_fresh": True,
                "node_p2p_fresh_consensus_peer_count": 8,
                "node_p2p_best_peer_lead_blocks": 0,
                "node_p2p_peer_lead_tolerance_blocks": 10,
                "node_template_coinbase_valid": True,
                "node_template_age_seconds": 184,
                "node_reason_code": "node_syncing",
                "node_p2p_mining_fresh_reason_code": "ok",
            },
            "template_health": {
                "safe_for_mining": False,
                "blocking_reasons": ["mineable=false", "submit_ready=false"],
                "mineable_now": False,
                "submit_ready": False,
                "get_block_template_ready": False,
                "p2p_mining_fresh": True,
                "p2p_fresh_consensus_peer_count": 8,
                "p2p_best_peer_lead_blocks": 0,
                "p2p_peer_lead_tolerance_blocks": 10,
                "template_coinbase_valid": True,
                "template_age_seconds": 184,
                "reason_code": "node_syncing",
            },
            "source_job_health": {
                "ok": False,
                "authorized_miners": 4,
                "ready_miners": 0,
                "reason_code": "miners_without_current_job",
            },
        },
        "pool_job_state": {
            "active_connections": 4,
            "authorized_connections": 4,
            "ready_connections": 0,
            "connections_without_current_job": 4,
            "current_template_seq": 0,
            "reason_code": "miners_without_current_job",
        },
        "miner_health": {"connected_count": 4, "connected_count_effective": 4, "managed_count": 4, "miners": []},
        "mining_address": "0x1111111111111111111111111111111111111111",
    }


class WatchdogSyncRestartTests(unittest.TestCase):
    def test_watchdog_default_interval_limits_hard_stall_detection_latency(self) -> None:
        self.assertEqual(watchdog.DEFAULT_INTERVAL_SECONDS, 5)
        self.assertEqual(watchdog.DEFAULT_NODE_PEER_LEAD_HARD_STALL_CONFIRM_SECONDS, 10)
        self.assertEqual(watchdog.DEFAULT_NODE_PEER_LEAD_HARD_STALL_JOB_AGE_SECONDS, 12)
        self.assertEqual(watchdog.DEFAULT_NODE_PEER_LEAD_HARD_STALL_REPAIR_COOLDOWN, 300)

    def test_active_import_requires_fresh_import_age_when_importing_flag_is_stuck(self) -> None:
        now = 1_779_200_000
        status = node_status(importing=True, last_import_age_seconds=700)
        state = {
            "last_sync_height_changed_at_by_node": {"node": now - 700},
        }

        with mock.patch.object(watchdog, "NODES", ["node"]):
            active = watchdog.active_sync_import_nodes(status, state=state, now=now, grace_seconds=300)

        self.assertEqual([], active)

    def test_active_import_allows_fresh_importing_node(self) -> None:
        now = 1_779_200_000
        status = node_status(importing=True, last_import_age_seconds=40)
        state = {
            "last_sync_height_changed_at_by_node": {"node": now - 700},
        }

        with mock.patch.object(watchdog, "NODES", ["node"]):
            active = watchdog.active_sync_import_nodes(status, state=state, now=now, grace_seconds=300)

        self.assertEqual(["node"], active)

    def test_sync_restart_not_suppressed_for_stale_importing_node(self) -> None:
        now = 1_779_200_000
        status = node_status(importing=True, last_import_age_seconds=700)
        state = {
            "last_sync_height_changed_at_by_node": {"node": now - 700},
        }

        with mock.patch.object(watchdog, "NODES", ["node"]):
            suppressed = watchdog.suppress_sync_restart_for_active_import(
                status,
                state,
                "node has not imported a block for 700s; waiting for node sync",
                "node",
            )

        self.assertFalse(suppressed)

    def test_active_import_suppression_does_not_consume_repair_cooldown(self) -> None:
        now = 1_779_200_000
        status = node_status(importing=True, last_import_age_seconds=40)
        state = {
            "last_sync_height_changed_at_by_node": {"node": now - 700},
        }

        with mock.patch.object(watchdog, "NODES", ["node"]), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None):
            suppressed = watchdog.suppress_sync_restart_for_active_import(
                status,
                state,
                "waiting for node sync",
                "node",
            )

        self.assertTrue(suppressed)
        self.assertNotIn("last_sync_repair_at", state)
        self.assertIn("last_sync_repair_suppressed_epoch", state)

    def test_check_once_active_import_suppression_does_not_consume_repair_cooldown(self) -> None:
        now = 1_779_200_000
        status = {
            **node_status(importing=True, last_import_age_seconds=40),
            "failures": [],
            "stack_failures": [],
            "miner_failures": [],
            "warnings": ["pool is waiting for node sync to finish"],
            "overall": "syncing",
            "mining_address": "0x1111111111111111111111111111111111111111",
            "pool_health": {"initial_download": True},
            "miner_health": {"connected_count": 0, "connected_count_effective": 0, "miners": []},
        }
        state = {
            "consecutive_syncing": 4,
            "last_sync_height_by_node": {"node": 1000},
            "last_sync_height_changed_at_by_node": {"node": now - 700},
        }
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("suppressed import should not restart")
        ), mock.patch.object(
            watchdog, "run_repair", side_effect=AssertionError("suppressed import should not restart the stack")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("syncing", result["watchdog_state"]["last_status"])
        self.assertNotIn("last_sync_repair_at", result["watchdog_state"])
        self.assertIn("last_sync_repair_suppressed_epoch", result["watchdog_state"])
        self.assertTrue(written)

    def test_check_once_does_not_start_pool_when_catchup_pause_stopped_it(self) -> None:
        now = 1_779_200_000
        pool_failure = f"{watchdog.POOL_CONTAINER} is not running"
        status = {
            **node_status(importing=True, last_import_age_seconds=20),
            "failures": [pool_failure],
            "stack_failures": [pool_failure],
            "miner_failures": [],
            "warnings": [],
            "overall": "syncing",
            "status_reason": "catch-up pause active: chain node is 90000 blocks behind peers",
            "catchup_policy": {"active": True},
            "sync_health": {"catchup_pause_active": True},
            "mining_address": "0x1111111111111111111111111111111111111111",
            "pool_health": {},
            "miner_health": {"connected_count": 4, "connected_count_effective": 4, "miners": []},
        }
        state: dict[str, object] = {}
        written: list[dict[str, object]] = []
        events: list[tuple[str, str, str]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", side_effect=lambda event_type, severity, *_args, **_kwargs: events.append((event_type, severity, ""))
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_repair", side_effect=AssertionError("catch-up containment must not start the pool")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("pool_start_blocked", result["watchdog_state"]["last_status"])
        self.assertEqual([], result["watchdog_state"]["last_failures"])
        self.assertIn("chain catch-up pause is active", result["watchdog_state"]["last_sync_warnings"][0])
        self.assertTrue(any(item[0] == "pool_start_blocked" for item in events))
        self.assertTrue(written)

    def test_check_once_leaves_running_pool_up_when_sync_progress_is_syncing(self) -> None:
        now = 1_779_200_000
        status = {
            **node_status(importing=True, last_import_age_seconds=20),
            "failures": [],
            "stack_failures": [],
            "miner_failures": [],
            "warnings": [],
            "mode": "mining",
            "overall": "ok",
            "containers": {
                watchdog.POOL_CONTAINER: {
                    "running": True,
                    "started_at": "2026-06-14T12:00:00.000000000Z",
                }
            },
            "pool_health": {},
            "miner_health": {"connected_count": 4, "connected_count_effective": 4, "miners": []},
        }
        status["sync_progress"]["status"] = "syncing"
        status["sync_progress"]["remaining_blocks"] = 90_000
        state: dict[str, object] = {}
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_repair", side_effect=AssertionError("sync containment must not restart the stack")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("pool_sync_template_pause", result["watchdog_state"]["last_status"])
        self.assertEqual(
            ["sync progress is syncing with 90000 block(s) remaining"],
            result["watchdog_state"]["last_sync_warnings"],
        )
        self.assertEqual("sync progress is syncing with 90000 block(s) remaining", written[-1]["last_pool_sync_pause_reason"])

    def test_advisory_evm_sync_does_not_mask_ready_native_mining_as_pool_pause(self) -> None:
        status = advisory_evm_sync_with_ready_native_mining_status()

        with mock.patch.object(watchdog, "NODES", ["node"]):
            self.assertEqual("", watchdog.sync_progress_pool_pause_reason(status))
            self.assertEqual(12552152, watchdog.node_sync_height(status, "node"))

        progress = watchdog.sync_progress_for_node(status, "node")
        self.assertEqual("synced", progress["status"])
        self.assertEqual(0, progress["remaining_blocks"])
        self.assertEqual(12552152, progress["current_block"])

    def test_fresh_paid_backend_readiness_flicker_does_not_become_pool_sync_pause(self) -> None:
        status = fresh_paid_backend_readiness_flicker_status()

        self.assertTrue(watchdog.pool_mining_pipeline_ready(status))
        self.assertTrue(watchdog.fresh_paid_work_bridges_status_backend_readiness_flicker(status))
        self.assertEqual("", watchdog.sync_progress_pool_pause_reason(status))

    def test_check_once_ignores_fresh_paid_backend_readiness_flicker(self) -> None:
        now = 1_779_200_000
        status = fresh_paid_backend_readiness_flicker_status()
        state = {
            "consecutive_syncing": 4,
            "last_sync_repair_at": 0,
            "last_pool_sync_pause_active": True,
        }
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(watchdog, "collect_stack_status", return_value=status), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(watchdog, "record_earnings_snapshot", return_value={}), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("fresh production flicker must not restart node")
        ), mock.patch.object(
            watchdog, "run_repair", side_effect=AssertionError("fresh production flicker must not restart stack")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertNotIn(
            result["watchdog_state"]["last_status"],
            {"pool_sync_template_pause", "pool_sync_template_pause_stalled"},
        )
        self.assertFalse(result["watchdog_state"]["last_pool_sync_pause_active"])
        self.assertEqual(0, result["watchdog_state"]["consecutive_syncing"])
        self.assertTrue(written)

    def test_check_once_ignores_advisory_evm_sync_when_native_pipeline_is_ready(self) -> None:
        now = 1_779_200_000
        status = advisory_evm_sync_with_ready_native_mining_status()
        state = {
            "consecutive_syncing": 4,
            "last_sync_height_by_node": {"node": 12552140},
            "last_sync_height_changed_at_by_node": {"node": now - 700},
            "last_sync_repair_at": 0,
            "last_pool_sync_pause_active": True,
        }
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(watchdog, "collect_stack_status", return_value=status), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(watchdog, "record_earnings_snapshot", return_value={}), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("advisory EVM sync must not restart node")
        ), mock.patch.object(
            watchdog, "run_repair", side_effect=AssertionError("advisory EVM sync must not restart stack")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("ok", result["watchdog_state"]["last_status"])
        self.assertEqual(0, result["watchdog_state"]["consecutive_syncing"])
        self.assertEqual([], result["watchdog_state"]["last_sync_warnings"])
        self.assertEqual({"node": 12552152}, result["watchdog_state"]["last_sync_height_by_node"])
        self.assertFalse(result["watchdog_state"]["last_pool_sync_pause_active"])
        self.assertTrue(written)

    def test_observe_sync_progress_tracks_top_level_primary_node_height_without_false_active_import(self) -> None:
        now = 1_779_200_000
        status = {
            "sync_progress": {
                "status": "syncing",
                "current_block": 12156762,
                "highest_block": 12191150,
                "remaining_blocks": 34388,
            },
            "containers": {
                "node": {"running": True},
            },
        }
        state: dict[str, object] = {}

        with mock.patch.object(watchdog, "NODES", ["node"]):
            watchdog.observe_sync_progress(status, state, now)

        self.assertEqual({"node": 12156762}, state["last_sync_height_by_node"])
        self.assertEqual({}, state["last_sync_height_changed_at_by_node"])

    def test_check_once_restarts_node_when_sync_pause_height_is_flat(self) -> None:
        now = 1_779_200_000
        status = {
            "failures": [],
            "stack_failures": [],
            "miner_failures": [],
            "warnings": [],
            "overall": "syncing",
            "mode": "catchup_pause",
            "containers": {
                "node": {"running": True, "started_at": "2026-01-14T12:00:00.000000000Z"},
                watchdog.POOL_CONTAINER: {"running": True, "started_at": "2026-01-14T12:00:00.000000000Z"},
            },
            "sync_progress": {
                "status": "syncing",
                "current_block": 12156762,
                "highest_block": 12191150,
                "remaining_blocks": 34388,
            },
            "nodes": {
                "node": {
                    "child_running": True,
                    "importing": False,
                    "latest_block": None,
                    "last_import_age_seconds": None,
                }
            },
            "sync_health": {},
            "pool_health": {},
            "miner_health": {"connected_count": 4, "connected_count_effective": 4, "miners": []},
            "mining_address": "0x1111111111111111111111111111111111111111",
        }
        state = {
            "consecutive_syncing": 4,
            "last_sync_height_by_node": {"node": 12156762},
            "last_sync_height_changed_at_by_node": {"node": now - 700},
            "last_sync_repair_at": 0,
        }
        restarts: list[tuple[str, str]] = []
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(watchdog, "collect_stack_status", return_value=status), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(watchdog, "record_earnings_snapshot", return_value={}), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=lambda node, reason: restarts.append((node, reason)) or True
        ), mock.patch.object(
            watchdog, "run_repair", side_effect=AssertionError("sync-pause stall should restart only the node")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("pool_sync_template_pause_stalled", result["watchdog_state"]["last_status"])
        self.assertTrue(result["watchdog_state"]["last_pool_sync_pause_active"])
        self.assertEqual(1, len(restarts))
        self.assertEqual("node", restarts[0][0])
        self.assertIn("stalled catch-up during pool sync pause", restarts[0][1])
        self.assertEqual(0, result["watchdog_state"]["consecutive_syncing"])
        self.assertTrue(written)

    def test_check_once_does_not_restart_node_when_sync_pause_height_recently_advanced(self) -> None:
        now = 1_779_200_000
        status = {
            "failures": [],
            "stack_failures": [],
            "miner_failures": [],
            "warnings": [],
            "overall": "syncing",
            "mode": "catchup_pause",
            "containers": {
                "node": {"running": True, "started_at": "2026-01-14T12:00:00.000000000Z"},
                watchdog.POOL_CONTAINER: {"running": True, "started_at": "2026-01-14T12:00:00.000000000Z"},
            },
            "sync_progress": {
                "status": "syncing",
                "current_block": 12156762,
                "highest_block": 12191150,
                "remaining_blocks": 34388,
            },
            "nodes": {
                "node": {
                    "child_running": True,
                    "importing": False,
                    "latest_block": None,
                    "last_import_age_seconds": None,
                }
            },
            "sync_health": {},
            "pool_health": {},
            "miner_health": {"connected_count": 4, "connected_count_effective": 4, "miners": []},
            "mining_address": "0x1111111111111111111111111111111111111111",
        }
        state = {
            "consecutive_syncing": 4,
            "last_sync_height_by_node": {"node": 12156761},
            "last_sync_height_changed_at_by_node": {"node": now - 700},
            "last_sync_repair_at": 0,
        }
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(watchdog, "collect_stack_status", return_value=status), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(watchdog, "record_earnings_snapshot", return_value={}), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("recent height advance must suppress restart")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("pool_sync_template_pause", result["watchdog_state"]["last_status"])
        self.assertTrue(result["watchdog_state"]["last_pool_sync_pause_active"])
        self.assertEqual(0, result["watchdog_state"]["consecutive_syncing"])
        self.assertEqual(["node"], result["watchdog_state"]["last_pool_sync_pause_active_import_nodes"])
        self.assertTrue(written)

    def test_check_once_does_not_restart_pool_when_nested_node_syncing(self) -> None:
        now = 1_779_200_000
        status = {
            **node_status(importing=True, last_import_age_seconds=20),
            "failures": [],
            "stack_failures": [],
            "miner_failures": [],
            "warnings": ["pool is waiting for node sync to finish"],
            "mode": "mining",
            "overall": "ok",
            "containers": {
                watchdog.POOL_CONTAINER: {
                    "running": True,
                    "started_at": "2026-01-14T12:00:00.000000000Z",
                }
            },
            "pool_health": {
                "initial_download": True,
                "pool_template_frozen": True,
                "template_freeze_age_seconds": 600,
            },
            "miner_health": {"connected_count": 4, "connected_count_effective": 4, "miners": []},
        }
        state: dict[str, object] = {
            "consecutive_share_stalls": watchdog.DEFAULT_SHARE_STALL_THRESHOLD - 1,
        }
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_pool_restart", side_effect=AssertionError("sync mode must not restart the pool")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("pool_sync_template_pause", result["watchdog_state"]["last_status"])
        self.assertIn("node sync progress is syncing", result["watchdog_state"]["last_sync_warnings"][0])
        self.assertTrue(written)

    def test_check_once_restarts_node_for_confirmed_rpc_refused_before_sync_pause(self) -> None:
        now = 1_779_200_000
        status = {
            **node_status(importing=True, last_import_age_seconds=20),
            "failures": [],
            "stack_failures": [],
            "miner_failures": [],
            "warnings": ["pool recently saw RPC connection refused"],
            "mode": "sync_blocked",
            "overall": "syncing",
            "containers": {
                "node": {"running": True, "started_at": "2026-01-14T12:00:00.000000000Z"},
                watchdog.POOL_CONTAINER: {"running": True, "started_at": "2026-01-14T12:00:00.000000000Z"},
            },
            "pool_health": {
                "initial_download": True,
                "rpc_refused_recent": True,
                "last_rpc_refused_age_seconds": 20,
                "rpc_refused_warn_seconds": 120,
                "source_job_health": {"ok": False, "reason_code": "node-health-transport"},
            },
            "pool_job_state": {
                "active_connections": 1,
                "authorized_connections": 1,
                "ready_connections": 0,
                "connections_without_current_job": 1,
                "current_template_seq": 0,
                "reason_code": "miners_without_current_job",
            },
            "miner_health": {"connected_count": 1, "connected_count_effective": 1, "miners": []},
        }
        state = {"node_rpc_refused_since": now - watchdog.DEFAULT_NODE_RPC_REFUSED_CONFIRM_SECONDS}
        restarts: list[tuple[str, str]] = []
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=lambda node, reason: restarts.append((node, reason)) or True
        ), mock.patch.object(
            watchdog, "run_pool_restart", side_effect=AssertionError("node RPC refusal must restart node first")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("node_rpc_refused", result["watchdog_state"]["last_status"])
        self.assertEqual(1, len(restarts))
        self.assertEqual("node", restarts[0][0])
        self.assertIn("node RPC refused", restarts[0][1])
        self.assertIn("node_rpc_refused_pool_restart_pending", result["watchdog_state"])
        self.assertTrue(written)

    def test_peer_lead_stall_evidence_uses_selected_backend_health(self) -> None:
        evidence = watchdog.selected_backend_peer_lead_stall_evidence(peer_lead_stall_status())

        self.assertTrue(evidence["active"])
        self.assertEqual(40, evidence["lead"])
        self.assertEqual(10, evidence["tolerance"])
        self.assertFalse(evidence["p2p_mining_fresh"])
        self.assertEqual(4, evidence["active_miners"])
        self.assertEqual(0, evidence["ready_miners"])

    def test_peer_lead_stall_evidence_accepts_numeric_metrics_source(self) -> None:
        status = peer_lead_stall_status()
        pool_health = status["pool_health"]
        assert isinstance(pool_health, dict)
        selected = pool_health.pop("selected_backend_source_health")
        pool_health.pop("source_selected_backend_p2p_fresh")
        pool_health.pop("source_selected_backend_submit_ready")
        pool_health.pop("source_selected_backend_mineable")
        status["pool_metrics"] = {
            "selected_backend_source_health": {
                **selected,
                "node_p2p_mining_fresh": 0.0,
                "node_submit_ready": 0.0,
                "node_mineable": 0.0,
                "node_p2p_best_peer_lead_blocks": 40.0,
                "node_p2p_peer_lead_tolerance_blocks": 10.0,
            },
        }

        evidence = watchdog.selected_backend_peer_lead_stall_evidence(status)

        self.assertTrue(evidence["active"])
        self.assertEqual(40, evidence["lead"])
        self.assertEqual(10, evidence["tolerance"])
        self.assertFalse(evidence["p2p_mining_fresh"])
        self.assertFalse(evidence["submit_ready"])
        self.assertFalse(evidence["mineable"])

    def test_peer_lead_stall_evidence_merges_sparse_dashboard_with_pool_metrics(self) -> None:
        status = peer_starvation_status()
        pool_health = status["pool_health"]
        assert isinstance(pool_health, dict)
        selected = pool_health["selected_backend_source_health"]
        assert isinstance(selected, dict)
        selected.pop("node_p2p_fresh_consensus_peer_count")
        selected.pop("node_p2p_consensus_peer_count")
        status["pool_metrics"] = {
            "selected_backend_source_health": {
                "node_p2p_fresh_consensus_peer_count": 0,
                "node_p2p_consensus_peer_count": 1,
            },
        }

        evidence = watchdog.selected_backend_peer_lead_stall_evidence(status)

        self.assertTrue(evidence["active"])
        self.assertTrue(evidence["peer_starvation"])
        self.assertEqual(0, evidence["fresh_consensus_peer_count"])
        self.assertEqual(1, evidence["consensus_peer_count"])

    def test_peer_lead_stall_evidence_uses_enriched_node_label_metrics(self) -> None:
        status = peer_lead_stall_status()
        pool_health = status["pool_health"]
        assert isinstance(pool_health, dict)
        selected = pool_health.pop("selected_backend_source_health")
        pool_health.pop("source_selected_backend_p2p_fresh")
        pool_health.pop("source_selected_backend_submit_ready")
        pool_health.pop("source_selected_backend_mineable")
        status["pool_metrics"] = {
            "selected_backend": "node",
            "source_backend_health": {"node": dict(selected)},
            "selected_backend_source_health": dict(selected),
            "source_job_health": {
                "authorized_miners": 4,
                "ready_miners": 0,
                "reason_code": "miners_without_current_job",
            },
        }

        evidence = watchdog.selected_backend_peer_lead_stall_evidence(status)

        self.assertTrue(evidence["active"])
        self.assertEqual(40, evidence["lead"])
        self.assertFalse(evidence["p2p_mining_fresh"])
        self.assertFalse(evidence["submit_ready"])
        self.assertFalse(evidence["mineable"])

    def test_peer_lead_stall_evidence_accepts_explicit_reason_without_numeric_lead(self) -> None:
        status = peer_lead_stall_status()
        pool_health = status["pool_health"]
        assert isinstance(pool_health, dict)
        selected = pool_health["selected_backend_source_health"]
        assert isinstance(selected, dict)
        selected.pop("node_p2p_best_peer_lead_blocks")
        selected.pop("node_p2p_peer_lead_tolerance_blocks")

        evidence = watchdog.selected_backend_peer_lead_stall_evidence(status)

        self.assertTrue(evidence["active"])
        self.assertIsNone(evidence["lead"])
        self.assertFalse(evidence["p2p_mining_fresh"])

    def test_template_sync_wedge_evidence_covers_native_current_zero_ready_jobs(self) -> None:
        status = native_current_template_sync_wedge_status()

        peer_lead = watchdog.selected_backend_peer_lead_stall_evidence(status)
        wedge = watchdog.selected_backend_template_sync_wedge_evidence(status)

        self.assertFalse(peer_lead["active"])
        self.assertTrue(wedge["active"])
        self.assertTrue(wedge["p2p_mining_fresh"])
        self.assertEqual(0, wedge["lead"])
        self.assertEqual(0, wedge["ready_miners"])
        self.assertTrue(wedge["syncing_reason"])
        self.assertTrue(watchdog.template_sync_wedge_hard_mining_outage(status, wedge))

    def test_template_sync_wedge_evidence_accepts_parent_stale_reason(self) -> None:
        status = native_current_template_sync_wedge_status()
        status["warnings"] = []
        sync = status["sync_progress"]
        assert isinstance(sync, dict)
        sync["error"] = ""
        pool_health = status["pool_health"]
        assert isinstance(pool_health, dict)
        selected = pool_health["selected_backend_source_health"]
        assert isinstance(selected, dict)
        selected["node_reason_code"] = "template_parent_stale"
        template = pool_health["template_health"]
        assert isinstance(template, dict)
        template["reason_code"] = "template-parent-stale"

        wedge = watchdog.selected_backend_template_sync_wedge_evidence(status)

        self.assertTrue(wedge["active"])
        self.assertTrue(wedge["syncing_reason"])
        self.assertEqual(0, wedge["ready_miners"])

    def test_check_once_waits_for_template_sync_wedge_confirmation(self) -> None:
        now = 1_779_200_000
        status = native_current_template_sync_wedge_status()
        state: dict[str, object] = {}
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("first template-sync wedge sample must not restart")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("node_template_sync_wedge", result["watchdog_state"]["last_status"])
        self.assertEqual(now, result["watchdog_state"]["node_template_sync_wedge_since"])
        self.assertEqual(1, result["watchdog_state"]["consecutive_syncing"])
        self.assertTrue(written)

    def test_check_once_restarts_node_for_confirmed_template_sync_wedge(self) -> None:
        now = 1_779_200_000
        status = native_current_template_sync_wedge_status()
        state = {"node_template_sync_wedge_since": now - watchdog.DEFAULT_NODE_TEMPLATE_SYNC_WEDGE_CONFIRM_SECONDS}
        restarts: list[tuple[str, str]] = []
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=lambda node, reason: restarts.append((node, reason)) or True
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual(1, len(restarts))
        self.assertEqual("node", restarts[0][0])
        self.assertIn("template-sync mining wedge", restarts[0][1])
        self.assertIn("last_node_template_sync_wedge_restart", result["watchdog_state"])
        self.assertNotIn("node_template_sync_wedge_since", result["watchdog_state"])
        self.assertTrue(written)

    def test_template_sync_wedge_repair_suppressed_by_recent_paid_work(self) -> None:
        now = 1_779_200_000
        status = native_current_template_sync_wedge_status(recent_paid_work=True)
        state = {"node_template_sync_wedge_since": now - watchdog.DEFAULT_NODE_TEMPLATE_SYNC_WEDGE_CONFIRM_SECONDS}
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("fresh paid work suppresses template-sync restart")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("node_template_sync_wedge_observing", result["watchdog_state"]["last_status"])
        self.assertEqual(0, result["watchdog_state"]["consecutive_syncing"])
        self.assertTrue(written)

    def test_template_sync_wedge_repair_suppressed_by_active_import(self) -> None:
        now = 1_779_200_000
        status = native_current_template_sync_wedge_status()
        pool_health = status["pool_health"]
        assert isinstance(pool_health, dict)
        selected = pool_health["selected_backend_source_health"]
        assert isinstance(selected, dict)
        selected["node_template_age_seconds"] = 5
        template = pool_health["template_health"]
        assert isinstance(template, dict)
        template["template_age_seconds"] = 5
        nodes = status["nodes"]
        assert isinstance(nodes, dict)
        node = nodes["node"]
        assert isinstance(node, dict)
        node["importing"] = True
        node["last_import_age_seconds"] = 10
        state = {"node_template_sync_wedge_since": now - watchdog.DEFAULT_NODE_TEMPLATE_SYNC_WEDGE_CONFIRM_SECONDS}
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("active import suppresses template-sync restart")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("node_template_sync_wedge_observing", result["watchdog_state"]["last_status"])
        self.assertFalse(result["watchdog_state"]["last_node_template_sync_wedge_hard_mining_outage"])
        self.assertEqual(0, result["watchdog_state"]["consecutive_syncing"])
        self.assertTrue(written)

    def test_hard_template_sync_wedge_restarts_despite_active_import(self) -> None:
        now = 1_779_200_000
        status = native_current_template_sync_wedge_status()
        nodes = status["nodes"]
        assert isinstance(nodes, dict)
        node = nodes["node"]
        assert isinstance(node, dict)
        node["importing"] = True
        node["last_import_age_seconds"] = 10
        state = {"node_template_sync_wedge_since": now - watchdog.DEFAULT_NODE_TEMPLATE_SYNC_WEDGE_CONFIRM_SECONDS}
        restarts: list[tuple[str, str]] = []
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=lambda node_name, reason: restarts.append((node_name, reason)) or True
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual(1, len(restarts))
        self.assertEqual("node", restarts[0][0])
        self.assertIn("template-sync mining wedge", restarts[0][1])
        self.assertTrue(result["watchdog_state"]["last_node_template_sync_wedge_hard_mining_outage"])
        self.assertIn("last_node_template_sync_wedge_restart", result["watchdog_state"])
        self.assertTrue(written)

    def test_check_once_waits_for_peer_lead_stall_confirmation(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status()
        state: dict[str, object] = {}
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("first peer-lead sample must not restart")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("node_peer_lead_stall", result["watchdog_state"]["last_status"])
        self.assertEqual(now, result["watchdog_state"]["node_peer_lead_stall_since"])
        self.assertEqual(1, result["watchdog_state"]["consecutive_syncing"])
        self.assertTrue(written)

    def test_check_once_restarts_node_for_confirmed_peer_lead_stall(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status()
        state = {"node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_STALL_CONFIRM_SECONDS}
        restarts: list[tuple[str, str]] = []
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=lambda node, reason: restarts.append((node, reason)) or True
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual(1, len(restarts))
        self.assertEqual("node", restarts[0][0])
        self.assertIn("peer-lead", restarts[0][1])
        self.assertIn("last_node_peer_lead_stall_restart", result["watchdog_state"])
        self.assertNotIn("node_peer_lead_stall_since", result["watchdog_state"])
        self.assertTrue(written)

    def test_confirmed_peer_lead_stall_does_not_use_pool_or_stack_restart(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status()
        state = {"node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_STALL_CONFIRM_SECONDS}
        restarts: list[tuple[str, str]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", lambda _payload: None
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=lambda node, reason: restarts.append((node, reason)) or True
        ), mock.patch.object(
            watchdog, "run_pool_restart", side_effect=AssertionError("peer-lead stall must not restart pool directly")
        ), mock.patch.object(
            watchdog, "run_repair", side_effect=AssertionError("peer-lead stall must not restart full stack")
        ):
            watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual(1, len(restarts))

    def test_peer_lead_stall_repair_suppressed_by_recent_paid_work(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status(recent_paid_work=True)
        state = {"node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_STALL_CONFIRM_SECONDS}
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("fresh paid work suppresses peer-lead restart")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("node_peer_lead_stall_observing", result["watchdog_state"]["last_status"])
        self.assertEqual(0, result["watchdog_state"]["consecutive_syncing"])
        self.assertTrue(written)

    def test_recent_paid_work_beats_expired_active_import_peer_lead_grace(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status(recent_paid_work=True)
        nodes = status["nodes"]
        assert isinstance(nodes, dict)
        node = nodes["node"]
        assert isinstance(node, dict)
        node["importing"] = True
        node["last_import_age_seconds"] = 10
        state = {
            "node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_STALL_CONFIRM_SECONDS,
            "node_peer_lead_active_import_by_node": {
                "node": {
                    "since": now - watchdog.DEFAULT_NODE_PEER_LEAD_ACTIVE_IMPORT_SUPPRESS_SECONDS - 1,
                    "first_lead": 40,
                    "best_lead": 40,
                    "worst_lead": 40,
                }
            },
        }
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("fresh paid work suppresses restart")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("node_peer_lead_stall_observing", result["watchdog_state"]["last_status"])
        self.assertEqual(0, result["watchdog_state"]["consecutive_syncing"])
        self.assertTrue(written)

    def test_peer_lead_stall_repair_suppressed_by_active_import(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status()
        nodes = status["nodes"]
        assert isinstance(nodes, dict)
        node = nodes["node"]
        assert isinstance(node, dict)
        node["importing"] = True
        node["last_import_age_seconds"] = 10
        pool_health = status["pool_health"]
        assert isinstance(pool_health, dict)
        source_job = pool_health["source_job_health"]
        assert isinstance(source_job, dict)
        source_job["ready_miners"] = 4
        source_job["reason_code"] = "ok"
        job_state = status["pool_job_state"]
        assert isinstance(job_state, dict)
        job_state["ready_connections"] = 4
        job_state["connections_without_current_job"] = 0
        job_state["reason_code"] = "ok"
        state = {"node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_STALL_CONFIRM_SECONDS}
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("active import suppresses peer-lead restart")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("node_peer_lead_stall_observing", result["watchdog_state"]["last_status"])
        self.assertEqual(0, result["watchdog_state"]["consecutive_syncing"])
        self.assertTrue(written)

    def test_hard_peer_lead_mining_outage_restarts_despite_active_import(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status()
        nodes = status["nodes"]
        assert isinstance(nodes, dict)
        node = nodes["node"]
        assert isinstance(node, dict)
        node["importing"] = True
        node["last_import_age_seconds"] = 10
        pool_health = status["pool_health"]
        assert isinstance(pool_health, dict)
        selected = pool_health["selected_backend_source_health"]
        assert isinstance(selected, dict)
        selected["node_p2p_best_peer_lead_blocks"] = 18
        state = {"node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_HARD_STALL_CONFIRM_SECONDS}
        restarts: list[tuple[str, str]] = []
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=lambda node_name, reason: restarts.append((node_name, reason)) or True
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual(1, len(restarts))
        self.assertEqual("node", restarts[0][0])
        self.assertIn("hard peer-lead mining outage", restarts[0][1])
        self.assertTrue(result["watchdog_state"]["last_node_peer_lead_hard_mining_outage"])
        self.assertIn("last_node_peer_lead_stall_restart", result["watchdog_state"])
        self.assertTrue(written)

    def test_peer_lead_runaway_replay_restarts_before_long_stall(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status()
        pool_health = status["pool_health"]
        assert isinstance(pool_health, dict)
        selected = pool_health["selected_backend_source_health"]
        assert isinstance(selected, dict)
        selected["node_p2p_best_peer_lead_blocks"] = 51
        selected["node_template_age_seconds"] = 37
        selected["node_reason_code"] = "node_syncing"
        pool_health["last_block_submit_age_seconds"] = 37
        pool_health["block_submit_success_count"] = 1397
        source_job = pool_health["source_job_health"]
        assert isinstance(source_job, dict)
        source_job["ready_miners"] = 0
        job_state = status["pool_job_state"]
        assert isinstance(job_state, dict)
        job_state["ready_connections"] = 0
        job_state["connections_without_current_job"] = 1
        job_state["reason_code"] = "miners_without_current_job"
        state = {"node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_HARD_STALL_CONFIRM_SECONDS}
        restarts: list[tuple[str, str]] = []
        written: list[dict[str, object]] = []

        evidence = watchdog.selected_backend_peer_lead_stall_evidence(status)
        self.assertTrue(watchdog.peer_lead_hard_mining_outage(status, evidence))

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=lambda node_name, reason: restarts.append((node_name, reason)) or True
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual(1, len(restarts))
        self.assertEqual("node", restarts[0][0])
        self.assertIn("hard peer-lead mining outage", restarts[0][1])
        self.assertTrue(result["watchdog_state"]["last_node_peer_lead_hard_mining_outage"])
        self.assertIn("last_node_peer_lead_stall_restart", result["watchdog_state"])
        self.assertTrue(written)

    def test_observed_active_import_peer_lead_outage_restarts_at_hard_confirm(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status()
        nodes = status["nodes"]
        assert isinstance(nodes, dict)
        node = nodes["node"]
        assert isinstance(node, dict)
        node["importing"] = True
        node["last_import_age_seconds"] = 5
        pool_health = status["pool_health"]
        assert isinstance(pool_health, dict)
        selected = pool_health["selected_backend_source_health"]
        assert isinstance(selected, dict)
        selected["node_p2p_best_peer_lead_blocks"] = 20
        selected["node_template_age_seconds"] = 49
        selected["node_reason_code"] = "node_syncing"
        source_job = pool_health["source_job_health"]
        assert isinstance(source_job, dict)
        source_job["ready_miners"] = 0
        source_job["reason_code"] = "invalidated_current_job"
        job_state = status["pool_job_state"]
        assert isinstance(job_state, dict)
        job_state["ready_connections"] = 0
        job_state["connections_without_current_job"] = 0
        job_state["reason_code"] = "invalidated_current_job"
        state = {"node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_HARD_STALL_CONFIRM_SECONDS}
        restarts: list[tuple[str, str]] = []
        written: list[dict[str, object]] = []

        evidence = watchdog.selected_backend_peer_lead_stall_evidence(status)
        self.assertTrue(watchdog.peer_lead_hard_mining_outage(status, evidence))

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=lambda node_name, reason: restarts.append((node_name, reason)) or True
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual(1, len(restarts))
        self.assertEqual("node", restarts[0][0])
        self.assertIn("hard peer-lead mining outage", restarts[0][1])
        self.assertTrue(result["watchdog_state"]["last_node_peer_lead_hard_mining_outage"])
        self.assertIn("last_node_peer_lead_stall_restart", result["watchdog_state"])
        self.assertTrue(written)

    def test_peer_lead_hard_outage_uses_pool_job_age_when_templates_refresh(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status()
        nodes = status["nodes"]
        assert isinstance(nodes, dict)
        node = nodes["node"]
        assert isinstance(node, dict)
        node["importing"] = True
        node["last_import_age_seconds"] = 5
        pool_health = status["pool_health"]
        assert isinstance(pool_health, dict)
        selected = pool_health["selected_backend_source_health"]
        assert isinstance(selected, dict)
        selected["node_p2p_best_peer_lead_blocks"] = 38
        selected["node_template_age_seconds"] = 4
        selected["node_reason_code"] = "node_syncing"
        source_job = pool_health["source_job_health"]
        assert isinstance(source_job, dict)
        source_job["ready_miners"] = 0
        source_job["max_current_job_age_seconds"] = 23
        source_job["reason_code"] = "invalidated_current_job"
        job_state = status["pool_job_state"]
        assert isinstance(job_state, dict)
        job_state["ready_connections"] = 0
        job_state["connections_without_current_job"] = 0
        job_state["last_broadcast_age_ms"] = 27_246
        job_state["reason_code"] = "invalidated_current_job"
        state = {"node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_HARD_STALL_CONFIRM_SECONDS}
        restarts: list[tuple[str, str]] = []
        written: list[dict[str, object]] = []

        evidence = watchdog.selected_backend_peer_lead_stall_evidence(status)
        self.assertEqual(27.246, evidence["pool_job_age_seconds"])
        self.assertTrue(watchdog.peer_lead_hard_mining_outage(status, evidence))

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=lambda node_name, reason: restarts.append((node_name, reason)) or True
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual(1, len(restarts))
        self.assertEqual("node", restarts[0][0])
        self.assertIn("hard peer-lead mining outage", restarts[0][1])
        self.assertTrue(result["watchdog_state"]["last_node_peer_lead_hard_mining_outage"])
        self.assertIn("last_node_peer_lead_stall_restart", result["watchdog_state"])
        self.assertTrue(written)

    def test_peer_lead_hard_outage_restarts_after_short_job_age_gate(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status()
        nodes = status["nodes"]
        assert isinstance(nodes, dict)
        node = nodes["node"]
        assert isinstance(node, dict)
        node["importing"] = True
        node["last_import_age_seconds"] = 5
        pool_health = status["pool_health"]
        assert isinstance(pool_health, dict)
        selected = pool_health["selected_backend_source_health"]
        assert isinstance(selected, dict)
        selected["node_p2p_best_peer_lead_blocks"] = 24
        selected["node_template_age_seconds"] = 14
        selected["node_reason_code"] = "node_syncing"
        source_job = pool_health["source_job_health"]
        assert isinstance(source_job, dict)
        source_job["ready_miners"] = 0
        source_job["reason_code"] = "invalidated_current_job"
        job_state = status["pool_job_state"]
        assert isinstance(job_state, dict)
        job_state["ready_connections"] = 0
        job_state["connections_without_current_job"] = 0
        job_state["last_broadcast_age_ms"] = 12_500
        job_state["reason_code"] = "invalidated_current_job"
        state = {"node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_HARD_STALL_CONFIRM_SECONDS}
        restarts: list[tuple[str, str]] = []
        written: list[dict[str, object]] = []

        evidence = watchdog.selected_backend_peer_lead_stall_evidence(status)
        self.assertEqual(12.5, evidence["pool_job_age_seconds"])
        self.assertTrue(watchdog.peer_lead_hard_mining_outage(status, evidence))

        job_state["last_broadcast_age_ms"] = 11_500
        below_gate = watchdog.selected_backend_peer_lead_stall_evidence(status)
        self.assertFalse(watchdog.peer_lead_hard_mining_outage(status, below_gate))
        job_state["last_broadcast_age_ms"] = 12_500

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=lambda node_name, reason: restarts.append((node_name, reason)) or True
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual(1, len(restarts))
        self.assertEqual("node", restarts[0][0])
        self.assertIn("hard peer-lead mining outage", restarts[0][1])
        self.assertTrue(result["watchdog_state"]["last_node_peer_lead_hard_mining_outage"])
        self.assertTrue(written)

    def test_peer_lead_pool_job_age_does_not_override_recent_paid_work(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status(recent_paid_work=True)
        pool_health = status["pool_health"]
        assert isinstance(pool_health, dict)
        source_job = pool_health["source_job_health"]
        assert isinstance(source_job, dict)
        source_job["max_current_job_age_seconds"] = 45
        job_state = status["pool_job_state"]
        assert isinstance(job_state, dict)
        job_state["last_broadcast_age_ms"] = 45_000
        evidence = watchdog.selected_backend_peer_lead_stall_evidence(status)
        self.assertFalse(watchdog.peer_lead_hard_mining_outage(status, evidence))

    def test_peer_starvation_job_age_restarts_before_peer_lead_is_measurable(self) -> None:
        now = 1_779_200_000
        status = peer_starvation_status()
        state = {"node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_HARD_STALL_CONFIRM_SECONDS}
        restarts: list[tuple[str, str]] = []
        written: list[dict[str, object]] = []

        evidence = watchdog.selected_backend_peer_lead_stall_evidence(status)
        self.assertTrue(evidence["active"])
        self.assertTrue(evidence["peer_starvation"])
        self.assertEqual(0, evidence["lead"])
        self.assertEqual(0, evidence["fresh_consensus_peer_count"])
        self.assertEqual(52.0, evidence["pool_job_age_seconds"])
        self.assertTrue(watchdog.peer_lead_hard_mining_outage(status, evidence))

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=lambda node_name, reason: restarts.append((node_name, reason)) or True
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual(1, len(restarts))
        self.assertEqual("node", restarts[0][0])
        self.assertIn("hard peer-starvation mining outage", restarts[0][1])
        self.assertTrue(result["watchdog_state"]["last_node_peer_lead_hard_mining_outage"])
        self.assertTrue(written)

    def test_peer_starvation_job_age_does_not_override_recent_paid_work(self) -> None:
        status = peer_starvation_status(recent_paid_work=True)

        evidence = watchdog.selected_backend_peer_lead_stall_evidence(status)

        self.assertTrue(evidence["peer_starvation"])
        self.assertFalse(watchdog.peer_lead_hard_mining_outage(status, evidence))

    def test_peer_starvation_with_ready_jobs_is_not_a_hard_outage(self) -> None:
        status = peer_starvation_status(ready_miners=4)

        evidence = watchdog.selected_backend_peer_lead_stall_evidence(status)

        self.assertTrue(evidence["active"])
        self.assertTrue(evidence["peer_starvation"])
        self.assertFalse(watchdog.peer_lead_hard_mining_outage(status, evidence))

    def test_explicit_block_age_wins_over_broad_recent_paid_work_flag(self) -> None:
        status = peer_lead_stall_status(recent_paid_work=True)
        sync_health = status["sync_health"]
        assert isinstance(sync_health, dict)
        sync_health["pool_has_recent_paid_work"] = True
        pool_health = status["pool_health"]
        assert isinstance(pool_health, dict)
        pool_health["last_block_submit_age_seconds"] = 37
        pool_health["block_submit_success_count"] = 1397
        source_job = pool_health["source_job_health"]
        assert isinstance(source_job, dict)
        source_job["max_current_job_age_seconds"] = 37
        job_state = status["pool_job_state"]
        assert isinstance(job_state, dict)
        job_state["last_broadcast_age_ms"] = 37_000

        evidence = watchdog.selected_backend_peer_lead_stall_evidence(status)

        self.assertFalse(watchdog.pool_has_recent_mining_work(status, 20))
        self.assertTrue(watchdog.pool_has_recent_mining_work(status, 60))
        self.assertTrue(watchdog.peer_lead_hard_mining_outage(status, evidence))

    def test_hard_peer_lead_mining_outage_keeps_normal_startup_grace(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status()
        containers = status["containers"]
        assert isinstance(containers, dict)
        node_container = containers["node"]
        assert isinstance(node_container, dict)
        started_at = datetime.fromtimestamp(now - 50, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
        node_container["started_at"] = started_at
        state = {"node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_HARD_STALL_CONFIRM_SECONDS}
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("normal startup grace suppresses hard restart")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("node_peer_lead_stall", result["watchdog_state"]["last_status"])
        self.assertTrue(result["watchdog_state"]["last_node_peer_lead_hard_mining_outage"])
        self.assertTrue(written)

    def test_peer_lead_stall_active_import_grace_expires(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status()
        nodes = status["nodes"]
        assert isinstance(nodes, dict)
        node = nodes["node"]
        assert isinstance(node, dict)
        node["importing"] = True
        node["last_import_age_seconds"] = 10
        state = {
            "node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_STALL_CONFIRM_SECONDS,
            "node_peer_lead_active_import_by_node": {
                "node": {
                    "since": now - watchdog.DEFAULT_NODE_PEER_LEAD_ACTIVE_IMPORT_SUPPRESS_SECONDS - 1,
                    "first_lead": 40,
                    "best_lead": 40,
                    "worst_lead": 40,
                }
            },
        }
        restarts: list[tuple[str, str]] = []
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=lambda node_name, reason: restarts.append((node_name, reason)) or True
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual(1, len(restarts))
        self.assertEqual("node", restarts[0][0])
        self.assertIn("peer-lead", restarts[0][1])
        self.assertIn("last_node_peer_lead_stall_restart", result["watchdog_state"])
        self.assertTrue(written)

    def test_peer_lead_stall_active_import_grace_expires_when_lead_worsens(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status()
        nodes = status["nodes"]
        assert isinstance(nodes, dict)
        node = nodes["node"]
        assert isinstance(node, dict)
        node["importing"] = True
        node["last_import_age_seconds"] = 10
        pool_health = status["pool_health"]
        assert isinstance(pool_health, dict)
        selected = pool_health["selected_backend_source_health"]
        assert isinstance(selected, dict)
        selected["node_p2p_best_peer_lead_blocks"] = 180
        state = {
            "node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_STALL_CONFIRM_SECONDS,
            "node_peer_lead_active_import_by_node": {
                "node": {
                    "since": now - watchdog.DEFAULT_NODE_PEER_LEAD_STALL_CONFIRM_SECONDS,
                    "first_lead": 40,
                    "best_lead": 40,
                    "worst_lead": 40,
                }
            },
        }
        restarts: list[tuple[str, str]] = []
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=lambda node_name, reason: restarts.append((node_name, reason)) or True
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual(1, len(restarts))
        self.assertEqual("node", restarts[0][0])
        self.assertIn("last_node_peer_lead_stall_restart", result["watchdog_state"])
        self.assertTrue(written)

    def test_peer_lead_stall_repair_suppressed_by_cooldown(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status()
        pool_health = status["pool_health"]
        assert isinstance(pool_health, dict)
        source_job = pool_health["source_job_health"]
        assert isinstance(source_job, dict)
        source_job["ready_miners"] = 4
        job_state = status["pool_job_state"]
        assert isinstance(job_state, dict)
        job_state["ready_connections"] = 4
        job_state["connections_without_current_job"] = 0
        job_state["reason_code"] = "ok"
        state = {
            "node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_STALL_CONFIRM_SECONDS,
            "last_node_peer_lead_stall_restart_at": now - watchdog.DEFAULT_NODE_PEER_LEAD_STALL_REPAIR_COOLDOWN + 10,
        }
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("cooldown suppresses peer-lead restart")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("node_peer_lead_stall", result["watchdog_state"]["last_status"])
        self.assertIn("node_peer_lead_stall_since", result["watchdog_state"])
        self.assertTrue(written)

    def test_hard_peer_lead_mining_outage_uses_shorter_repair_cooldown(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status()
        state = {
            "node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_HARD_STALL_CONFIRM_SECONDS,
            "last_node_peer_lead_stall_restart_at": now
            - watchdog.DEFAULT_NODE_PEER_LEAD_HARD_STALL_REPAIR_COOLDOWN
            - 1,
        }
        restarts: list[tuple[str, str]] = []
        written: list[dict[str, object]] = []

        self.assertTrue(
            now - state["last_node_peer_lead_stall_restart_at"]
            < watchdog.DEFAULT_NODE_PEER_LEAD_STALL_REPAIR_COOLDOWN
        )

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=lambda node_name, reason: restarts.append((node_name, reason)) or True
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual(1, len(restarts))
        self.assertEqual("node", restarts[0][0])
        self.assertIn("hard peer-lead mining outage", restarts[0][1])
        self.assertTrue(result["watchdog_state"]["last_node_peer_lead_hard_mining_outage"])
        self.assertTrue(written)

    def test_hard_peer_lead_mining_outage_suppressed_by_hard_cooldown(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status()
        state = {
            "node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_HARD_STALL_CONFIRM_SECONDS,
            "last_node_peer_lead_stall_restart_at": now
            - watchdog.DEFAULT_NODE_PEER_LEAD_HARD_STALL_REPAIR_COOLDOWN
            + 10,
        }
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("hard cooldown suppresses peer-lead restart")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("node_peer_lead_stall", result["watchdog_state"]["last_status"])
        self.assertTrue(result["watchdog_state"]["last_node_peer_lead_hard_mining_outage"])
        self.assertTrue(written)

    def test_peer_lead_stall_repair_suppressed_by_node_startup_grace(self) -> None:
        now = 1_779_200_000
        status = peer_lead_stall_status()
        containers = status["containers"]
        assert isinstance(containers, dict)
        node_container = containers["node"]
        assert isinstance(node_container, dict)
        started_at = datetime.fromtimestamp(now - 30, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
        node_container["started_at"] = started_at
        state = {"node_peer_lead_stall_since": now - watchdog.DEFAULT_NODE_PEER_LEAD_STALL_CONFIRM_SECONDS}
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("startup grace suppresses peer-lead restart")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("node_peer_lead_stall", result["watchdog_state"]["last_status"])
        self.assertIn("node_peer_lead_stall_since", result["watchdog_state"])
        self.assertTrue(written)

    def test_peer_lead_stall_state_clears_when_backend_recovers(self) -> None:
        now = 1_779_200_000
        status = advisory_evm_sync_with_ready_native_mining_status()
        state = {"node_peer_lead_stall_since": now - 300}
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertNotIn("node_peer_lead_stall_since", result["watchdog_state"])
        self.assertTrue(written)

    def test_node_rpc_refused_evidence_ignores_unrelated_connection_refused_warning(self) -> None:
        status = {
            "warnings": [
                'Could not read dashboard logs: Get "http://host.docker.internal:8088/api/logs/node": '
                "dial tcp 172.17.0.1:8088: connect: connection refused"
            ],
            "pool_health": {
                "source_job_health": {"ok": True, "reason_code": "ok"},
                "source_backend_health": {"ok": True, "reason_code": "ok"},
            },
            "pool_job_state": {"reason_code": "ok"},
        }

        self.assertEqual({"active": False}, watchdog.node_rpc_refused_evidence(status))

    def test_check_once_restarts_pool_after_node_rpc_recovers_but_pool_has_no_current_jobs(self) -> None:
        now = 1_779_200_000
        status = {
            **node_status(importing=False, last_import_age_seconds=5),
            "failures": [],
            "stack_failures": [],
            "miner_failures": [],
            "warnings": [],
            "mode": "mining",
            "overall": "degraded",
            "containers": {
                "node": {"running": True, "started_at": "2026-01-14T12:00:00.000000000Z"},
                watchdog.POOL_CONTAINER: {"running": True, "started_at": "2026-01-14T12:00:00.000000000Z"},
            },
            "pool_health": {},
            "sync_health": {"needs_fast_sync_repair": False},
            "pool_job_state": {
                "active_connections": 1,
                "authorized_connections": 1,
                "ready_connections": 0,
                "connections_without_current_job": 1,
                "current_template_seq": 0,
                "reason_code": "miners_without_current_job",
            },
            "miner_health": {"connected_count": 1, "connected_count_effective": 1, "miners": []},
        }
        state = {
            "node_rpc_refused_pool_restart_pending": {
                "since": now - watchdog.DEFAULT_NODE_RPC_REFUSED_POOL_RESTART_GRACE_SECONDS,
                "node": "node",
                "evidence": {"rpc_refused_recent": True},
            }
        }
        restarts: list[str] = []
        written: list[dict[str, object]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", side_effect=lambda payload: written.append(dict(payload))
        ), mock.patch.object(
            watchdog, "collect_stack_status", return_value=status
        ), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(
            watchdog, "record_earnings_snapshot", return_value={}
        ), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_node_restart", side_effect=AssertionError("node RPC has recovered")
        ), mock.patch.object(
            watchdog, "run_pool_restart", side_effect=lambda reason: restarts.append(reason) or True
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual("pool_restarted_after_node_rpc_refused", result["watchdog_state"]["last_status"])
        self.assertEqual(1, len(restarts))
        self.assertIn("miners without current work", restarts[0])
        self.assertNotIn("node_rpc_refused_pool_restart_pending", result["watchdog_state"])
        self.assertTrue(written)

    def test_targeted_node_restart_uses_runtime_container_name(self) -> None:
        commands: list[list[str]] = []

        class Result:
            ok = True
            stdout = ""

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(
            watchdog, "automation_mutation_allowed", return_value=True
        ), mock.patch.object(
            watchdog, "acquire_lock", return_value=mock.Mock(close=lambda: None)
        ), mock.patch.object(
            watchdog, "action_log_path", return_value=pathlib.Path(tmpdir) / "restart.log"
        ), mock.patch.object(
            watchdog, "write_action_state", lambda _payload: None
        ), mock.patch.object(
            watchdog, "record_failed_repair", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_logged", side_effect=lambda command, *_args, **_kwargs: commands.append(command) or Result()
        ):
            ok = watchdog.run_node_restart("node", "unit test")

        self.assertTrue(ok)
        self.assertEqual(
            [
                ["docker", "inspect", "-f", "{{json .Config.Env}}", "node"],
                ["docker", "inspect", "-f", "{{.State.Running}}", watchdog.POOL_CONTAINER],
                ["docker", "restart", "node"],
            ],
            commands,
        )

    def test_targeted_node_restart_pauses_running_pool(self) -> None:
        commands: list[list[str]] = []
        states: list[dict[str, object]] = []

        class Result:
            def __init__(self, stdout: str = "") -> None:
                self.ok = True
                self.stdout = stdout

        def fake_run_logged(command: list[str], *_args, **_kwargs):
            commands.append(command)
            if command == ["docker", "inspect", "-f", "{{json .Config.Env}}", "node"]:
                return Result('["BOOTSTRAP_PEER_ADDRESSES=/ip4/good/tcp/8150/p2p/16Good"]')
            if command[:3] == ["docker", "inspect", "-f"]:
                return Result("true\n")
            return Result()

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(
            watchdog, "POOL_ENV_FILE", pathlib.Path(tmpdir) / ".env"
        ), mock.patch.object(
            watchdog, "automation_mutation_allowed", return_value=True
        ), mock.patch.object(
            watchdog, "acquire_lock", return_value=mock.Mock(close=lambda: None)
        ), mock.patch.object(
            watchdog, "action_log_path", return_value=pathlib.Path(tmpdir) / "restart.log"
        ), mock.patch.object(
            watchdog, "write_action_state", side_effect=lambda payload: states.append(dict(payload))
        ), mock.patch.object(
            watchdog, "record_failed_repair", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_logged", side_effect=fake_run_logged
        ):
            watchdog.POOL_ENV_FILE.write_text(
                "BOOTSTRAP_PEER_ADDRESSES=/ip4/good/tcp/8150/p2p/16Good\n",
                encoding="utf-8",
            )
            ok = watchdog.run_node_restart("node", "unit test")

        self.assertTrue(ok)
        self.assertEqual(
            [
                ["docker", "inspect", "-f", "{{json .Config.Env}}", "node"],
                ["docker", "inspect", "-f", "{{.State.Running}}", watchdog.POOL_CONTAINER],
                ["docker", "stop", watchdog.POOL_CONTAINER],
                ["docker", "restart", "node"],
                ["docker", "start", watchdog.POOL_CONTAINER],
            ],
            commands,
        )
        self.assertTrue(states[-1]["pool_paused"])
        self.assertTrue(states[-1]["pool_stop_ok"])
        self.assertTrue(states[-1]["pool_start_ok"])
        self.assertEqual("docker-restart", states[-1]["restart_method"])

    def test_targeted_node_restart_recreates_when_bootstrap_env_is_stale(self) -> None:
        commands: list[list[str]] = []
        states: list[dict[str, object]] = []

        class Result:
            def __init__(self, stdout: str = "") -> None:
                self.ok = True
                self.stdout = stdout
                self.stderr = ""

        def fake_run_logged(command: list[str], *_args, **_kwargs):
            commands.append(command)
            if command == ["docker", "inspect", "-f", "{{json .Config.Env}}", "node"]:
                return Result('["BOOTSTRAP_PEER_ADDRESSES=/ip4/stale/tcp/8151/p2p/16Stale"]')
            if command == ["docker", "inspect", "-f", "{{.State.Running}}", watchdog.POOL_CONTAINER]:
                return Result("false\n")
            return Result()

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(
            watchdog, "POOL_ENV_FILE", pathlib.Path(tmpdir) / ".env"
        ), mock.patch.object(
            watchdog, "automation_mutation_allowed", return_value=True
        ), mock.patch.object(
            watchdog, "acquire_lock", return_value=mock.Mock(close=lambda: None)
        ), mock.patch.object(
            watchdog, "action_log_path", return_value=pathlib.Path(tmpdir) / "restart.log"
        ), mock.patch.object(
            watchdog, "write_action_state", side_effect=lambda payload: states.append(dict(payload))
        ), mock.patch.object(
            watchdog, "record_failed_repair", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "compose_service_name", return_value="node"
        ), mock.patch.object(
            watchdog, "docker_compose_command", side_effect=lambda *args: ["compose", *args]
        ), mock.patch.object(
            watchdog, "run_logged", side_effect=fake_run_logged
        ):
            watchdog.POOL_ENV_FILE.write_text(
                "BOOTSTRAP_PEER_ADDRESSES=/ip4/fresh/tcp/8150/p2p/16Fresh\n",
                encoding="utf-8",
            )
            ok = watchdog.run_node_restart("node", "unit test")

        self.assertTrue(ok)
        self.assertEqual(
            [
                ["docker", "inspect", "-f", "{{json .Config.Env}}", "node"],
                ["docker", "inspect", "-f", "{{.State.Running}}", watchdog.POOL_CONTAINER],
                [
                    "compose",
                    "up",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "--no-build",
                    "--pull",
                    "never",
                    "node",
                ],
            ],
            commands,
        )
        self.assertEqual("compose-recreate", states[-1]["restart_method"])
        self.assertEqual("BOOTSTRAP_PEER_ADDRESSES", states[-1]["env_recreate_mismatches"][0]["key"])

    def test_node_log_marks_missing_dag_tip_as_critical_repairable_damage(self) -> None:
        parsed = pool_ops.parse_node_log(
            "\n".join(
                [
                    "2026-06-04|17:34:18.911 [INFO ] Loading dag ... module=CHAIN",
                    "2026-06-04|17:34:18.911 [ERROR] The dag data was damaged (Can't find tip:10089356). you can cleanup your block data base by '--cleanup'.",
                ]
            )
        )

        self.assertTrue(parsed["critical"])
        self.assertTrue(parsed["dag_tip_damage"])
        self.assertIn("Can't find tip:10089356", parsed["dag_tip_damage_lines"][0])

    def test_node_dag_tip_cleanup_runs_narrow_cleanuptips_repair(self) -> None:
        commands: list[list[str]] = []

        class Result:
            ok = True

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(
            watchdog, "automation_mutation_allowed", return_value=True
        ), mock.patch.object(
            watchdog, "acquire_lock", return_value=mock.Mock(close=lambda: None)
        ), mock.patch.object(
            watchdog, "action_log_path", return_value=pathlib.Path(tmpdir) / "cleanuptips.log"
        ), mock.patch.object(
            watchdog, "write_action_state", lambda _payload: None
        ), mock.patch.object(
            watchdog, "record_failed_repair", lambda *_args, **_kwargs: None
        ), mock.patch.object(
            watchdog, "log", lambda _message: None
        ), mock.patch.object(
            watchdog, "run_logged", side_effect=lambda command, *_args, **_kwargs: commands.append(command) or Result()
        ):
            ok = watchdog.run_node_dag_tip_cleanup("node", "unit test")

        self.assertTrue(ok)
        self.assertEqual("bash", commands[0][0])
        script = commands[0][2]
        self.assertIn("--cleanuptips", script)
        self.assertNotIn("--cleanup\n", script)
        self.assertIn("docker stop", script)
        self.assertIn("docker start", script)

    def test_check_once_repairs_missing_dag_tip_before_generic_restart(self) -> None:
        now = 1_779_200_000
        status = {
            "failures": [
                "node wrapper is up but bdag child is not running",
                "node has critical log entries",
            ],
            "stack_failures": [
                "node wrapper is up but bdag child is not running",
                "node has critical log entries",
            ],
            "miner_failures": [],
            "warnings": [],
            "overall": "down",
            "mining_address": "0x1111111111111111111111111111111111111111",
            "pool_health": {},
            "miner_health": {"connected_count": 0, "connected_count_effective": 0, "miners": []},
            "nodes": {
                "node": {
                    "child_running": False,
                    "dag_tip_damage": True,
                    "dag_tip_damage_lines": ["The dag data was damaged (Can't find tip:10089356)."],
                }
            },
            "sync_progress": {"nodes": {}},
        }
        state: dict[str, object] = {}
        cleanup_calls: list[tuple[str, str]] = []

        with mock.patch.object(watchdog.time, "time", return_value=now), mock.patch.object(
            watchdog, "NODES", ["node"]
        ), mock.patch.object(watchdog, "read_state", return_value=state), mock.patch.object(
            watchdog, "write_state", lambda _payload: None
        ), mock.patch.object(watchdog, "collect_stack_status", return_value=status), mock.patch.object(
            watchdog, "lock_is_held", return_value=False
        ), mock.patch.object(watchdog, "record_earnings_snapshot", return_value={}), mock.patch.object(
            watchdog, "status_payload_has_tracking_gap", return_value=False
        ), mock.patch.object(
            watchdog, "node_mining_template_support_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "fastsync_peer_quarantine_should_repair", return_value=False
        ), mock.patch.object(
            watchdog, "record_efficiency_event", lambda *_args, **_kwargs: None
        ), mock.patch.object(watchdog, "log", lambda _message: None), mock.patch.object(
            watchdog,
            "run_node_dag_tip_cleanup",
            side_effect=lambda node, reason: cleanup_calls.append((node, reason)) or True,
        ), mock.patch.object(
            watchdog, "run_repair", side_effect=AssertionError("missing DAG tip should use cleanuptips first")
        ):
            result = watchdog.check_once(3, 1800, 5, 900, repair=True)

        self.assertEqual([("node", cleanup_calls[0][1])], cleanup_calls)
        self.assertIn("--cleanuptips", cleanup_calls[0][1])
        self.assertEqual(0, result["watchdog_state"]["consecutive_failures"])
        self.assertEqual({"node": now}, result["watchdog_state"]["last_node_dag_tip_cleanup_at_by_node"])

    def test_legacy_single_node_watchdog_skips_pool_restart_when_node_syncing(self) -> None:
        script = pathlib.Path("scripts/bdag-single-node-watchdog.sh").resolve()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = pathlib.Path(tmpdir)
            root = tmp / "root"
            root.mkdir()
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            restart_marker = tmp / "pool-restart-called"

            (fake_bin / "docker").write_text(
                """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  info)
    exit 0
    ;;
  inspect)
    template="${3:-}"
    if [[ "$template" == *State.Status* ]]; then
      printf 'running\\n'
    else
      printf '\\n'
    fi
    ;;
  logs)
    container="${@: -1}"
    if [[ "$container" == "bdagminer-pool-1" ]]; then
      for _ in {1..30}; do
        printf 'Submit Error not found in acceptedJobs Expired\\n'
      done
      printf 'pool is waiting for node sync to finish\\n'
    else
      printf 'Client in initial download\\n'
    fi
    ;;
  compose)
    printf 'restart called\\n' >> "$BDAG_SINGLE_NODE_WATCHDOG_RESTART_MARKER"
    ;;
esac
""",
                encoding="utf-8",
            )
            (fake_bin / "date").write_text(
                """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  --iso-8601=seconds)
    printf '2026-06-17T12:00:00+00:00\\n'
    ;;
  +%s)
    printf '1779200000\\n'
    ;;
  +%Y%m%d-%H%M%S)
    printf '20260617-120000\\n'
    ;;
  *)
    /bin/date "$@"
    ;;
esac
""",
                encoding="utf-8",
            )
            (fake_bin / "flock").write_text(
                """#!/usr/bin/env bash
exit 0
""",
                encoding="utf-8",
            )
            for fake in fake_bin.iterdir():
                fake.chmod(0o755)

            env = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "BDAG_SINGLE_NODE_WATCHDOG_ROOT": str(root),
                "BDAG_SINGLE_NODE_WATCHDOG_STATE_DIR": str(tmp / "state"),
                "BDAG_SINGLE_NODE_WATCHDOG_LOCK_FILE": str(tmp / "watchdog.lock"),
                "BDAG_SINGLE_NODE_WATCHDOG_RESTART_MARKER": str(restart_marker),
            }
            result = subprocess.run(
                ["bash", str(script)],
                cwd=pathlib.Path(__file__).resolve().parents[2],
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            self.assertFalse(restart_marker.exists())
            log = (root / "logs" / "single-node-watchdog.log").read_text(encoding="utf-8")
            self.assertIn("node sync mode active; leaving pool running", log)


if __name__ == "__main__":
    unittest.main()
