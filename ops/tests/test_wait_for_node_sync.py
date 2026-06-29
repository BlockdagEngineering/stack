#!/usr/bin/env python3

import contextlib
import io
import pathlib
import sys
import unittest

OPS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import wait_for_node_sync  # noqa: E402


class WaitForNodeSyncTests(unittest.TestCase):
    def test_stream_node_logs_follows_compose_service_and_prints_lines(self) -> None:
        calls: list[list[str]] = []
        chunks = iter(
            [
                b"node  | booting node\nnode  | The sync of graph state has ended\n",
            ]
        )

        class FakeStdout:
            def fileno(self) -> int:
                return 42

        class FakeProc:
            def __init__(self, command: list[str], **_kwargs: object) -> None:
                calls.append(command)
                self.stdout = FakeStdout()
                self.returncode = None

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.returncode = 0

            def wait(self, timeout: float | None = None) -> int:
                self.returncode = 0
                return 0

            def kill(self) -> None:
                self.returncode = -9

        original_popen = wait_for_node_sync.subprocess.Popen
        original_select = wait_for_node_sync.select.select
        original_read = wait_for_node_sync.os.read
        wait_for_node_sync.subprocess.Popen = FakeProc  # type: ignore[assignment]
        wait_for_node_sync.select.select = lambda read, _write, _error, _timeout: (read, [], [])
        wait_for_node_sync.os.read = lambda _fd, _size: next(chunks, b"")
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = wait_for_node_sync.stream_node_logs(timeout=1.0)
        finally:
            wait_for_node_sync.subprocess.Popen = original_popen
            wait_for_node_sync.select.select = original_select
            wait_for_node_sync.os.read = original_read

        self.assertEqual(result, 0)
        self.assertEqual(
            calls[0],
            [
                "docker",
                "compose",
                "logs",
                "--no-color",
                "-f",
                "--tail",
                str(wait_for_node_sync.LOG_LINES),
                "node",
            ],
        )
        self.assertIn("following node logs from compose service 'node'", output.getvalue())
        self.assertIn("node  | booting node", output.getvalue())
        self.assertIn("The sync of graph state has ended", output.getvalue())

    def test_stream_node_logs_fails_on_node_startup_error(self) -> None:
        chunks = iter(
            [
                (
                    "node  | 2026-06-26|19:22:40.087 [INFO ] Shutdown complete\n"
                    "node  | 2026-06-26|19:22:40.087 [ERROR] persisted upgrade state AuthorizedMining has no definition\n"
                ).encode()
            ]
        )

        class FakeStdout:
            def fileno(self) -> int:
                return 42

        class FakeProc:
            def __init__(self, _command: list[str], **_kwargs: object) -> None:
                self.stdout = FakeStdout()
                self.returncode = None

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.returncode = 0

            def wait(self, timeout: float | None = None) -> int:
                self.returncode = 0
                return 0

            def kill(self) -> None:
                self.returncode = -9

        original_popen = wait_for_node_sync.subprocess.Popen
        original_select = wait_for_node_sync.select.select
        original_read = wait_for_node_sync.os.read
        wait_for_node_sync.subprocess.Popen = FakeProc  # type: ignore[assignment]
        wait_for_node_sync.select.select = lambda read, _write, _error, _timeout: (read, [], [])
        wait_for_node_sync.os.read = lambda _fd, _size: next(chunks, b"")
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = wait_for_node_sync.stream_node_logs(timeout=1.0)
        finally:
            wait_for_node_sync.subprocess.Popen = original_popen
            wait_for_node_sync.select.select = original_select
            wait_for_node_sync.os.read = original_read

        self.assertEqual(result, 1)
        self.assertIn("persisted upgrade state AuthorizedMining has no definition", output.getvalue())
        self.assertIn("node exited before sync completed", output.getvalue())

    def test_stream_node_logs_accepts_rpc_synced_after_node_ready(self) -> None:
        chunks = iter(
            [
                (
                    "node  | 2026-06-26|19:50:20.661 [INFO ] Start BdagChain... module=BDAG\n"
                    "node  | 2026-06-26|19:50:20.686 [INFO ] prepare evm environment module=CHAIN\n"
                ).encode()
            ]
        )
        probes = 0

        class FakeStdout:
            def fileno(self) -> int:
                return 42

        class FakeProc:
            def __init__(self, _command: list[str], **_kwargs: object) -> None:
                self.stdout = FakeStdout()
                self.returncode = None

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.returncode = 0

            def wait(self, timeout: float | None = None) -> int:
                self.returncode = 0
                return 0

            def kill(self) -> None:
                self.returncode = -9

        def synced_probe() -> bool:
            nonlocal probes
            probes += 1
            return True

        original_popen = wait_for_node_sync.subprocess.Popen
        original_select = wait_for_node_sync.select.select
        original_read = wait_for_node_sync.os.read
        wait_for_node_sync.subprocess.Popen = FakeProc  # type: ignore[assignment]
        wait_for_node_sync.select.select = lambda read, _write, _error, _timeout: (read, [], [])
        wait_for_node_sync.os.read = lambda _fd, _size: next(chunks, b"")
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = wait_for_node_sync.stream_node_logs(timeout=1.0, synced_probe=synced_probe)
        finally:
            wait_for_node_sync.subprocess.Popen = original_popen
            wait_for_node_sync.select.select = original_select
            wait_for_node_sync.os.read = original_read

        self.assertEqual(result, 0)
        self.assertGreaterEqual(probes, 1)
        self.assertIn("EVM RPC reports synced", output.getvalue())

    def test_stream_node_logs_probes_rpc_when_startup_marker_not_in_tail(self) -> None:
        probes = 0

        class FakeStdout:
            def fileno(self) -> int:
                return 42

        class FakeProc:
            def __init__(self, _command: list[str], **_kwargs: object) -> None:
                self.stdout = FakeStdout()
                self.returncode = None

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.returncode = 0

            def wait(self, timeout: float | None = None) -> int:
                self.returncode = 0
                return 0

            def kill(self) -> None:
                self.returncode = -9

        def synced_probe() -> bool:
            nonlocal probes
            probes += 1
            return True

        original_popen = wait_for_node_sync.subprocess.Popen
        original_select = wait_for_node_sync.select.select
        wait_for_node_sync.subprocess.Popen = FakeProc  # type: ignore[assignment]
        wait_for_node_sync.select.select = lambda _read, _write, _error, _timeout: ([], [], [])
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = wait_for_node_sync.stream_node_logs(timeout=1.0, synced_probe=synced_probe)
        finally:
            wait_for_node_sync.subprocess.Popen = original_popen
            wait_for_node_sync.select.select = original_select

        self.assertEqual(result, 0)
        self.assertEqual(probes, 1)
        self.assertIn("EVM RPC reports synced", output.getvalue())

    def test_log_snapshot_parses_sync_gap_from_syncing_graph_state(self) -> None:
        log = "\n".join(
            [
                "2026-06-23|15:07:07.606 [INFO ] Syncing graph state module=SYNC cur=(12397234,9487393,9500135,12493900,1) target=(12399668,9489347,9502090,12496787,1) peer=16Uiu2HAmEFxRaBbbf3sRi43CCvMk5Y6zPkuGY9s4uRK2FKJVJkqo protocol=45 services=Full|CF processID=1",
                "2026-06-23|15:07:14.640 [INFO ] Processed 13 blocks in the last 10.25s  module=CHAIN     transactions=17   order=12396663    time=2026-06-23T15:07:13+0000",
            ]
        )

        original = wait_for_node_sync.pool_ops.docker_logs
        wait_for_node_sync.pool_ops.docker_logs = lambda _name, lines=240: log
        try:
            snapshot = wait_for_node_sync.log_snapshot()
        finally:
            wait_for_node_sync.pool_ops.docker_logs = original

        self.assertEqual(snapshot["status"], "syncing")
        self.assertEqual(snapshot["current_block"], 12397234)
        self.assertEqual(snapshot["highest_block"], 12399668)
        self.assertEqual(snapshot["remaining_blocks"], 2434)
        self.assertGreater(snapshot["processed_rate_blocks_per_second"], 0)

    def test_log_snapshot_parses_sync_gap_from_startup_state_log(self) -> None:
        log = "2026-06-23|15:29:11.731 [INFO ] Start to find cur block state       module=BDAG      state.order=12397234 evm.Number=12040112 cur.number=0"

        original = wait_for_node_sync.pool_ops.docker_logs
        wait_for_node_sync.pool_ops.docker_logs = lambda _name, lines=240: log
        try:
            snapshot = wait_for_node_sync.log_snapshot()
        finally:
            wait_for_node_sync.pool_ops.docker_logs = original

        self.assertEqual(snapshot["status"], "syncing")
        self.assertEqual(snapshot["current_block"], 0)
        self.assertEqual(snapshot["highest_block"], 12397234)
        self.assertEqual(snapshot["remaining_blocks"], 12397234)

    def test_describe_progress_formats_gap_message(self) -> None:
        progress = {
            "status": "syncing",
            "current_block": 12397234,
            "highest_block": 12399668,
            "remaining_blocks": 2434,
            "processed_rate_blocks_per_second": 1.267,
        }

        message, state = wait_for_node_sync.describe_progress(progress, {}, 0.0)

        self.assertIn("gap 2,434 blocks", message)
        self.assertIn("ETA", message)
        self.assertEqual(state["remaining_blocks"], 2434)

    def test_describe_progress_marks_unchanged_log_snapshots(self) -> None:
        progress = {
            "status": "syncing",
            "current_block": 12397234,
            "highest_block": 12402058,
            "remaining_blocks": 4824,
            "processed_rate_blocks_per_second": None,
            "last_log_update_seconds": 30,
        }
        previous = {
            "status": "syncing",
            "current_block": 12397234,
            "highest_block": 12402058,
            "remaining_blocks": 4824,
            "epoch": 100.0,
            "poll_interval": 10.0,
        }

        message, _state = wait_for_node_sync.describe_progress(progress, previous, 110.0)

        self.assertIn("unchanged for 30s", message)

    def test_eth_sync_progress_marks_false_as_synced(self) -> None:
        original = wait_for_node_sync.pool_ops.eth_syncing_details
        wait_for_node_sync.pool_ops.eth_syncing_details = lambda _url, timeout=4.0: {
            "eth_syncing": False,
            "chain_syncing": False,
        }
        try:
            progress = wait_for_node_sync.eth_sync_progress("http://127.0.0.1:18545", timeout=1.0)
        finally:
            wait_for_node_sync.pool_ops.eth_syncing_details = original

        self.assertEqual(progress["status"], "synced")
        self.assertEqual(progress["remaining_blocks"], 0)

    def test_eth_sync_progress_preserves_evm_import_gap(self) -> None:
        original = wait_for_node_sync.pool_ops.eth_syncing_details
        wait_for_node_sync.pool_ops.eth_syncing_details = lambda _url, timeout=4.0: {
            "eth_syncing": {"currentBlock": "0x10", "highestBlock": "0x20"},
            "chain_syncing": True,
            "sync_current_block": 16,
            "sync_highest_block": 32,
        }
        try:
            progress = wait_for_node_sync.eth_sync_progress("http://127.0.0.1:18545", timeout=1.0)
        finally:
            wait_for_node_sync.pool_ops.eth_syncing_details = original

        message, state = wait_for_node_sync.describe_eth_sync_progress(progress, {}, 0.0)

        self.assertEqual(progress["status"], "syncing")
        self.assertEqual(progress["remaining_blocks"], 16)
        self.assertIn("EVM import syncing: gap 16 blocks", message)
        self.assertEqual(state["remaining_blocks"], 16)

    def test_eth_sync_progress_requires_eth_syncing_false_even_at_zero_gap(self) -> None:
        progress = {
            "status": "syncing",
            "current_block": 32,
            "highest_block": 32,
            "remaining_blocks": 0,
        }

        message, state = wait_for_node_sync.describe_eth_sync_progress(progress, {}, 0.0)

        self.assertIn("waiting for EVM import to finish reporting eth_syncing", message)
        self.assertEqual(state["status"], "syncing")
        self.assertEqual(state["remaining_blocks"], 0)

    def test_wait_for_eth_sync_waits_until_eth_syncing_false(self) -> None:
        snapshots = iter(
            [
                {
                    "status": "syncing",
                    "current_block": 16,
                    "highest_block": 32,
                    "remaining_blocks": 16,
                },
                {"status": "synced", "remaining_blocks": 0},
            ]
        )
        original_progress = wait_for_node_sync.eth_sync_progress
        original_sleep = wait_for_node_sync.time.sleep
        sleeps: list[float] = []
        wait_for_node_sync.eth_sync_progress = lambda _url, timeout=4.0: next(snapshots)
        wait_for_node_sync.time.sleep = lambda seconds: sleeps.append(seconds)
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = wait_for_node_sync.wait_for_eth_sync(
                    "http://127.0.0.1:18545",
                    interval=0.0,
                    rpc_timeout=1.0,
                )
        finally:
            wait_for_node_sync.eth_sync_progress = original_progress
            wait_for_node_sync.time.sleep = original_sleep

        self.assertEqual(result, 0)
        self.assertEqual(sleeps, [0.1])
        self.assertIn("EVM import syncing: gap 16 blocks", output.getvalue())
        self.assertIn("EVM import sync complete", output.getvalue())


if __name__ == "__main__":
    unittest.main()
