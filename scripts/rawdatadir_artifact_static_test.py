#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "ops" / "build-rawdatadir-artifact.sh"
FETCH = ROOT / "ops" / "fetch-rawdatadir-artifact.sh"
SIDECAR = ROOT / "ops" / "maintain-rawdatadir-sidecar.sh"
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
        "--artifact-type raw_datadir_checkpoint",
        "--legacy-fallback=false",
        "BDAG_RAWDATADIR_IMPORT_REPLACE",
        "before-rawdatadir",
        "preserved local identity path",
    ):
        assert_contains(fetch, needle, FETCH)

    for needle in (
        "rsync",
        "--delete-excluded",
        "--exclude=/network.key",
        "--exclude=/bdageth/nodekey",
    ):
        assert_contains(sidecar, needle, SIDECAR)

    for needle in (
        "Use the existing Fast Artifact Sync V2 libp2p protocol",
        "Trust signer public keys, not peer IDs",
        "No deltas",
    ):
        assert_contains(doc, needle, DOC)


if __name__ == "__main__":
    main()
