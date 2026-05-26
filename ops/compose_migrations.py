#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


DUPLICATE_SAFE_KEY = "POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT"
DUPLICATE_SAFE_VALUE = "${POOL_DUPLICATE_SAFE_MULTI_BACKEND_SUBMIT:-true}"


@dataclass(frozen=True)
class MigrationResult:
    text: str
    changed: bool
    inserted_count: int


def _service_ranges(lines: list[str]) -> list[tuple[str, int, int]]:
    ranges: list[tuple[str, int, int]] = []
    in_services = False
    current_name = ""
    current_start = -1

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not line.startswith(" ") and stripped == "services:":
            in_services = True
            continue
        if not in_services:
            continue
        if line and not line.startswith(" ") and stripped.endswith(":"):
            if current_start >= 0:
                ranges.append((current_name, current_start, index))
            break
        if line.startswith("  ") and not line.startswith("    ") and stripped.endswith(":"):
            if current_start >= 0:
                ranges.append((current_name, current_start, index))
            current_name = stripped[:-1]
            current_start = index

    if in_services and current_start >= 0:
        ranges.append((current_name, current_start, len(lines)))
    return ranges


def _pool_service(name: str) -> bool:
    return name == "pool" or name.startswith("asic-pool")


def ensure_duplicate_safe_submit_flag(text: str) -> MigrationResult:
    if f"{DUPLICATE_SAFE_KEY}:" in text:
        return MigrationResult(text=text, changed=False, inserted_count=0)

    lines = text.splitlines()
    trailing_newline = text.endswith("\n")
    inserted_count = 0

    for name, start, end in reversed(_service_ranges(lines)):
        if not _pool_service(name):
            continue
        node_urls_index: int | None = None
        in_environment = False
        for index in range(start + 1, end):
            line = lines[index]
            stripped = line.strip()
            if line.startswith("    ") and not line.startswith("      "):
                in_environment = stripped == "environment:"
                continue
            if not in_environment:
                continue
            if stripped.startswith("NODE_RPC_URLS:"):
                node_urls_index = index
                break
            if stripped and not line.startswith("      "):
                break
        if node_urls_index is None:
            continue
        indent = lines[node_urls_index][: len(lines[node_urls_index]) - len(lines[node_urls_index].lstrip())]
        lines.insert(node_urls_index + 1, f"{indent}{DUPLICATE_SAFE_KEY}: {DUPLICATE_SAFE_VALUE}")
        inserted_count += 1

    migrated = "\n".join(lines)
    if trailing_newline:
        migrated += "\n"
    return MigrationResult(text=migrated, changed=inserted_count > 0, inserted_count=inserted_count)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply idempotent live runtime compose migrations.")
    parser.add_argument("--ensure-duplicate-safe-submit", action="store_true")
    parser.add_argument("compose_file", type=Path)
    args = parser.parse_args()

    if not args.ensure_duplicate_safe_submit:
        parser.error("one migration flag is required")

    text = args.compose_file.read_text(encoding="utf-8")
    result = ensure_duplicate_safe_submit_flag(text)
    if not result.changed and f"{DUPLICATE_SAFE_KEY}:" not in result.text:
        raise SystemExit(f"could not insert {DUPLICATE_SAFE_KEY}; no eligible pool service was found")
    if result.changed:
        args.compose_file.write_text(result.text, encoding="utf-8")
        print(f"inserted {DUPLICATE_SAFE_KEY} into {result.inserted_count} pool service(s)")
    else:
        print(f"{DUPLICATE_SAFE_KEY} already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
