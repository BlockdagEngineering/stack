#!/usr/bin/env python3

import pathlib
import unittest


ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]


def read(rel: str) -> str:
    return (ROOT_DIR / rel).read_text(encoding="utf-8")


class MiningHostTuningTests(unittest.TestCase):
    def test_tuning_script_targets_compose_services_and_cgroup_controls(self) -> None:
        script = read("ops/apply-mining-host-tuning.sh")

        self.assertIn("pool_metrics_url=\"${BDAG_POOL_METRICS_URL:-http://127.0.0.1:9090/metrics}\"", script)
        self.assertIn("compose_service_container()", script)
        self.assertIn("label=com.docker.compose.service=$service", script)
        self.assertIn("container_cgroup_root()", script)
        self.assertIn("memory.low", script)
        self.assertIn("cpu.weight", script)
        self.assertIn("io.weight", script)
        self.assertIn("BDAG_TUNE_NET_QDISC", script)
        self.assertIn("active_lan_ifaces()", script)
        self.assertIn("network_ifaces()", script)
        self.assertIn("fq_codel target 5ms interval 100ms ecn", script)

    def test_compose_defaults_keep_critical_path_above_dashboard(self) -> None:
        compose = read("docker-compose.yml")

        self.assertIn("cpu_shares: ${BDAG_NODE_CPU_SHARES:-6144}", compose)
        self.assertIn("cpu_shares: ${BDAG_POOL_CPU_SHARES:-5120}", compose)
        self.assertIn("cpu_shares: ${BDAG_DASHBOARD_CPU_SHARES:-128}", compose)
        self.assertIn("weight: 1000", compose)
        self.assertIn("weight: 950", compose)
        self.assertIn("mem_swappiness: 0", compose)
        self.assertIn("/var/log/bdagStack:size=${BDAG_NODE_LOG_TMPFS_SIZE:-128m}", compose)
        self.assertIn(
            "/var/lib/bdagStack/node/mainnet/peerstore:size=${BDAG_NODE_PEERSTORE_TMPFS_SIZE:-64m}",
            compose,
        )
        self.assertIn('"${BDAG_DASHBOARD_HOST_BIND_IP:-127.0.0.1}:${DASHBOARD_HOST_PORT:-8088}:9280"', compose)
        self.assertIn("shm_size: ${BDAG_NODE_SHM_SIZE:-512m}", compose)
        self.assertIn("shm_size: ${BDAG_POOL_DB_SHM_SIZE:-256m}", compose)
        self.assertIn("shm_size: ${BDAG_POOL_SHM_SIZE:-256m}", compose)

    def test_env_example_exposes_priority_knobs(self) -> None:
        env_example = read(".env.example")

        for name in (
            "BDAG_NODE_CPU_SHARES=6144",
            "BDAG_POOL_CPU_SHARES=5120",
            "BDAG_DASHBOARD_CPU_SHARES=128",
            "BDAG_NODE_EPHEMERAL_TMPFS_SIZE=512m",
            "BDAG_NODE_LOG_TMPFS_SIZE=128m",
            "BDAG_NODE_PEERSTORE_TMPFS_SIZE=64m",
            "BDAG_DASHBOARD_HOST_BIND_IP=127.0.0.1",
            "DASHBOARD_HOST_PORT=8088",
            "BDAG_NODE_MEMORY_LOW=768M",
            "BDAG_POOL_MEMORY_LOW=256M",
            "BDAG_POOL_DB_MEMORY_LOW=512M",
            "BDAG_DASHBOARD_MEMORY_LOW=64M",
            "BDAG_TUNE_NET_QDISC=1",
        ):
            self.assertIn(name, env_example)


if __name__ == "__main__":
    unittest.main()
