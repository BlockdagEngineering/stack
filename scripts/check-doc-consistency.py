#!/usr/bin/env python3
"""Check duplicated release documentation for drift."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
RELEASE_DOWNLOADS = ROOT / "release-downloads" / "index.html"
INSTALL_DOC_MARKERS = (
    "builds Docker images",
    "starts the node first",
    "waits for sync",
    "every remaining service declared by `docker-compose.yml`",
)
INSTALL_DOC_MARKERS_HTML = tuple(marker.replace("&", "&amp;") for marker in INSTALL_DOC_MARKERS)
STALE_INSTALL_RE = re.compile(
    r"docker compose build (?:&&|&amp;&amp;) docker compose up -d --no-build --pull never"
)


def fail(message: str) -> None:
    raise SystemExit(f"doc consistency check failed: {message}")


def main() -> int:
    readme = README.read_text(encoding="utf-8")
    normalized_readme = re.sub(r"\s+", " ", readme)

    for marker in INSTALL_DOC_MARKERS:
        if marker not in normalized_readme:
            fail(f"{README} does not mention {marker!r}")

    documents = [(README, readme)]
    if RELEASE_DOWNLOADS.exists():
        release_html = RELEASE_DOWNLOADS.read_text(encoding="utf-8")
        normalized_release_html = re.sub(r"\s+", " ", release_html)
        for marker in INSTALL_DOC_MARKERS_HTML:
            if marker not in normalized_release_html:
                fail(f"{RELEASE_DOWNLOADS} does not mention {marker!r}")
        documents.append((RELEASE_DOWNLOADS, release_html))

    for path, text in documents:
        stale = STALE_INSTALL_RE.search(text)
        if stale:
            fail(f"{path} still contains stale install command at byte {stale.start()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
