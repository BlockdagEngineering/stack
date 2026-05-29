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


def test_empty_path_env_values_use_defaults() -> None:
    env = {
        "BDAG_NODE1_DATA_DIR": "",
        "BDAG_RAWDATADIR_SIDECAR_SOURCE": "",
        "BDAG_RAWDATADIR_SIDECAR_DIR": "",
        "BDAG_RAWDATADIR_ARTIFACT_BASE": "",
    }

    assert eligibility.node_data_dir(env, "bdag-miner-node-1") == eligibility.resolve_path("./data/node1")
    assert eligibility.env_path(env, "BDAG_RAWDATADIR_SIDECAR_SOURCE", "./data/node1/mainnet") == eligibility.resolve_path(
        "./data/node1/mainnet"
    )
    assert eligibility.env_path(env, "BDAG_RAWDATADIR_SIDECAR_DIR", "./data-restore/rawdatadir-sidecar/mainnet") == eligibility.resolve_path(
        "./data-restore/rawdatadir-sidecar/mainnet"
    )
    assert eligibility.env_path(env, "BDAG_RAWDATADIR_ARTIFACT_BASE", "./data-restore/rawdatadir") == eligibility.resolve_path(
        "./data-restore/rawdatadir"
    )


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


def test_evm_sync_sample_uses_reference_rpc_not_dag_height(monkeypatch) -> None:
    values = {
        ("http://local:18545", "eth_blockNumber"): 8_000,
        ("http://reference:18545", "eth_blockNumber"): 8_750,
    }

    def fake_quantity(url: str, method: str, timeout: float = 5.0) -> int:
        return values[(url, method)]

    monkeypatch.setattr(eligibility, "json_rpc_quantity", fake_quantity)

    payload = eligibility.source_evm_sync_sample(
        {
            "BDAG_RAWDATADIR_EVM_RPC_URL": "http://local:18545",
            "BDAG_RAWDATADIR_EVM_REFERENCE_RPC_URLS": "reference=http://reference:18545",
            "BDAG_RAWDATADIR_MAX_EVM_REFERENCE_LAG": "1000",
        }
    )

    assert payload["local_evm_block"] == 8_000
    assert payload["reference_evm_block"] == 8_750
    assert payload["lag_to_reference"] == 750
    assert payload["fresh"] is True


def test_evm_sync_sample_rejects_stale_local_evm(monkeypatch) -> None:
    values = {
        ("http://local:18545", "eth_blockNumber"): 8_000,
        ("http://reference:18545", "eth_blockNumber"): 9_500,
    }

    monkeypatch.setattr(eligibility, "json_rpc_quantity", lambda url, method, timeout=5.0: values[(url, method)])

    payload = eligibility.source_evm_sync_sample(
        {
            "BDAG_RAWDATADIR_EVM_RPC_URL": "http://local:18545",
            "BDAG_RAWDATADIR_EVM_REFERENCE_RPC_URLS": "reference=http://reference:18545",
            "BDAG_RAWDATADIR_MAX_EVM_REFERENCE_LAG": "1000",
        }
    )

    assert payload["lag_to_reference"] == 1500
    assert payload["fresh"] is False
