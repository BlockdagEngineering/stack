from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops" / "capability_profile.py"
SPEC = importlib.util.spec_from_file_location("capability_profile", SCRIPT)
capability_profile = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = capability_profile
SPEC.loader.exec_module(capability_profile)


def storage(path: str, storage_class: str) -> capability_profile.StoragePath:
    return capability_profile.StoragePath(
        path=path,
        mount={"source": "/dev/sda1", "fstype": "f2fs"},
        device="sda",
        facts={"transport": "usb", "removable": "1", "rotational": "0"},
        storage_class=storage_class,
    )


class CapabilityProfileTest(unittest.TestCase):
    def test_pi5_usb_asic_router_suppresses_bulk_sync_and_uses_ram(self) -> None:
        facts = capability_profile.HostFacts(
            os_name="linux",
            arch="arm64",
            cpu_count=4,
            memory_bytes=16 * capability_profile.GIB,
            host_profile="pi5",
            hardware_model="Raspberry Pi 5 Model B",
            topology="single-node-asic-router",
            node_mode="single",
            chain_paths=[storage("/mnt/bdag-usb/node2", "usb-removable-flash")],
        )
        profile = capability_profile.capability_profile_name(facts, {"BDAG_CAPABILITY_PROFILE": "auto"})
        recs = capability_profile.recommendations(profile, facts)

        self.assertEqual(profile, "pi5-usb-asic-router")
        self.assertEqual(recs["BDAG_NO_FASTSYNC_SERVE"], "1")
        self.assertEqual(recs["BDAG_NODE_CACHE_MB"], "6144")
        self.assertEqual(recs["BDAG_EVM_CACHE_MB"], "6144")
        self.assertEqual(recs["NODE_MAX_PEERS"], "96")
        self.assertEqual(recs["BDAG_BLOCK_READ_AHEAD_KB"], "256")
        self.assertEqual(recs["BDAG_VM_DIRTY_BYTES"], str(256 * capability_profile.MIB))

    def test_fragile_flash_keeps_sync_serving_auto_for_non_router(self) -> None:
        facts = capability_profile.HostFacts(
            os_name="linux",
            arch="arm64",
            cpu_count=4,
            memory_bytes=8 * capability_profile.GIB,
            host_profile="constrained",
            hardware_model="small host",
            topology="standard",
            node_mode="single",
            chain_paths=[storage("/data/node", "sd-card")],
        )
        profile = capability_profile.capability_profile_name(facts, {})
        recs = capability_profile.recommendations(profile, facts)

        self.assertEqual(profile, "fragile-flash")
        self.assertEqual(recs["BDAG_NO_FASTSYNC_SERVE"], "auto")
        self.assertEqual(recs["BDAG_FASTSYNC_PREPROCESS_WORKERS"], "1")
        self.assertEqual(recs["BDAG_FASTSNAP_PARALLELISM"], "2")

    def test_nvme_dual_node_raises_parallelism_and_peers(self) -> None:
        facts = capability_profile.HostFacts(
            os_name="linux",
            arch="amd64",
            cpu_count=16,
            memory_bytes=32 * capability_profile.GIB,
            host_profile="large",
            hardware_model="server",
            topology="standard",
            node_mode="double",
            chain_paths=[storage("/data/node1", "nvme-ssd"), storage("/data/node2", "nvme-ssd")],
        )
        profile = capability_profile.capability_profile_name(facts, {})
        recs = capability_profile.recommendations(profile, facts)

        self.assertEqual(profile, "nvme-dual-node")
        self.assertEqual(recs["NODE_MAX_PEERS"], "256")
        self.assertEqual(recs["BDAG_FASTSYNC_PREPROCESS_WORKERS"], "4")
        self.assertEqual(recs["BDAG_FASTSNAP_PARALLELISM"], "8")
        self.assertEqual(recs["BDAG_NODE_BD_CACHE_SIZE"], "16384")


if __name__ == "__main__":
    unittest.main()
