import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ops" / "fastartifact_source_eligibility.py"


spec = importlib.util.spec_from_file_location("fastartifact_source_eligibility", MODULE_PATH)
eligibility = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(eligibility)


def test_active_node_defaults_to_node1() -> None:
    env = {"BDAG_NODE_SERVICES": "bdag-miner-node-1", "BDAG_NODE1_DATA_DIR": "./data/node1"}

    assert eligibility.active_node_service(env) == "bdag-miner-node-1"
    assert str(eligibility.node_data_dir(env, "bdag-miner-node-1")).endswith("data/node1")


def test_path_classification_flags_usb_transport(monkeypatch, tmp_path: Path) -> None:
    def fake_mount_info(_path: Path) -> dict[str, str]:
        return {"source": "/dev/sda1", "fstype": "ext4", "target": str(tmp_path), "options": "rw"}

    def fake_device_facts(_source: str) -> dict[str, object]:
        return {"disk": "sda", "transport": "usb", "removable": False, "hotplug": False}

    monkeypatch.setattr(eligibility, "mount_info", fake_mount_info)
    monkeypatch.setattr(eligibility, "block_device_facts", fake_device_facts)

    payload = eligibility.classify_path("active_node_datadir", tmp_path)

    assert payload["unsafe"] is True
    assert "usb_or_removable" in payload["unsafe_reasons"]


def test_path_classification_flags_removable_mount_path(monkeypatch) -> None:
    path = Path("/media/user/USB/data")

    monkeypatch.setattr(
        eligibility,
        "mount_info",
        lambda _path: {"source": "/dev/nvme0n1p2", "fstype": "ext4", "target": "/media/user/USB", "options": "rw"},
    )
    monkeypatch.setattr(
        eligibility,
        "block_device_facts",
        lambda _source: {"disk": "nvme0n1", "transport": "nvme", "removable": False, "hotplug": False},
    )

    payload = eligibility.classify_path("artifact_base", path)

    assert payload["unsafe"] is True
    assert "removable_mount_path" in payload["unsafe_reasons"]
