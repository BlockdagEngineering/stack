#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "ops" / "build-rawdatadir-artifact.sh"
FETCH = ROOT / "ops" / "fetch-rawdatadir-artifact.sh"
SIDECAR = ROOT / "ops" / "maintain-rawdatadir-sidecar.sh"
ELIGIBILITY = ROOT / "ops" / "fastartifact_source_eligibility.py"
PUBLISH = ROOT / "ops" / "publish-rawdatadir-artifact.sh"
DOC = ROOT / "docs" / "rawdatadir-libp2p-sync.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def assert_contains(text: str, needle: str, path: Path) -> None:
    if needle not in text:
        raise AssertionError(f"{path} missing {needle!r}")


def main() -> None:
    build = read(BUILD)
    fetch = read(FETCH)
    sidecar = read(SIDECAR)
    eligibility = read(ELIGIBILITY)
    publish = read(PUBLISH)
    doc = read(DOC)

    for needle in (
        "raw_datadir_checkpoint",
        "admin/rpc-backend-maintenance",
        "wait_db_lock_free",
        "BDAG_RAWDATADIR_REQUIRE_SIGNED",
        "--exclude=./network.key",
        "--exclude=./bdageth/nodekey",
        "--exclude=./keystore",
        "--exclude=./peerstore",
        "restore_export_backend",
    ):
        assert_contains(build, needle, BUILD)

    for needle in (
        "--artifact-type",
        "raw_datadir_checkpoint",
        "--dir-out",
        "--legacy-fallback=false",
        "BDAG_RAWDATADIR_IMPORT_REPLACE",
        "before-rawdatadir",
        "preserved local identity path",
    ):
        assert_contains(fetch, needle, FETCH)

    for needle in (
        "rsync",
        "--delete-excluded",
        "--one-file-system",
        "--delay-updates",
        "--exclude=/network.key",
        "--exclude=/bdageth/nodekey",
    ):
        assert_contains(sidecar, needle, SIDECAR)

    for needle in (
        "usb_or_removable",
        "BDAG_RAWDATADIR_MIN_FREE_GIB",
        "publish_requires_finalization",
        "docker_root",
    ):
        assert_contains(eligibility, needle, ELIGIBILITY)

    for needle in (
        "BDAG_RAWDATADIR_SINGLE_NODE_FINALIZE=1",
        "single-node artifact publish requires",
        "BDAG_RAWDATADIR_SOURCE_DIR",
    ):
        assert_contains(publish, needle, PUBLISH)

    for needle in (
        "Use the existing Fast Artifact Sync V2 libp2p protocol",
        "Trust signer public keys, not peer IDs",
        "No deltas",
    ):
        assert_contains(doc, needle, DOC)


if __name__ == "__main__":
    main()
