#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path


ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker" / "entrypoint-nodeworker.sh"


class EntrypointFastSnapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = ENTRYPOINT.read_text(encoding="utf-8")

    def test_bootstrap_passes_all_ordered_peers_to_one_fastsnap_run(self) -> None:
        self.assertIn('fastsnap_args+=(--peer "$peer")', self.source)
        self.assertIn('trying P2P snapshot bootstrap from $peer_count ordered peer candidate(s)', self.source)
        self.assertNotIn('log "trying P2P snapshot bootstrap from $peer"', self.source)

    def test_bootstrap_uses_discovery_and_resume_ledger_by_default(self) -> None:
        self.assertIn('BDAG_FASTSNAP_DISCOVERY:-1', self.source)
        self.assertIn('--discover', self.source)
        self.assertIn('ledger="${BDAG_FASTSNAP_LEDGER:-$archive.artifact-ledger.json}"', self.source)

    def test_bootstrap_wires_trusted_signers(self) -> None:
        self.assertIn('BDAG_FASTSNAP_TRUSTED_SIGNERS', self.source)
        self.assertIn('--trusted-signer', self.source)


if __name__ == "__main__":
    unittest.main()
