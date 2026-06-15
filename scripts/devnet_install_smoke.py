#!/usr/bin/env python3
"""Smoke-test devnet release installers without starting Docker."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def host_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64", "x64"}:
        return "amd64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    raise ValueError(f"unsupported CPU architecture: {platform.machine()}")


def copy_payload_root(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in (
        ".env.devnet.example",
        "docker-compose.yml",
        "docker-compose.devnet.yml",
        "dockerfile",
        "node.devnet.conf",
    ):
        shutil.copy2(ROOT / name, dest / name)
    shutil.copy2(ROOT / "scripts" / "devnet" / "install.sh", dest / "install.sh")
    shutil.copy2(ROOT / "scripts" / "devnet" / "install.ps1", dest / "install.ps1")
    (dest / "release-payload.env").write_text(
        "\n".join(
            [
                "BDAG_RELEASE_VERSION=devnet-smoke",
                f"BDAG_RELEASE_PAYLOAD_TARGET=linux-{host_arch()}",
                f"BDAG_RELEASE_PAYLOAD_ARCH={host_arch()}",
                f"DOCKER_PLATFORM=linux/{host_arch()}",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )


def run_command(args: list[str], cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    result = subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return {
        "args": args,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, help="existing devnet payload root to smoke-test")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    cleanup = None
    package_root = args.package_root
    if package_root is None:
        cleanup = tempfile.TemporaryDirectory(prefix="stack-devnet-smoke-")
        package_root = Path(cleanup.name)
        copy_payload_root(package_root)

    env = os.environ.copy()
    env["BDAG_DEVNET_INSTALL_TEST_WRITE_ENV_ONLY"] = "1"

    if os.name == "nt":
        result = run_command(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                "install.ps1",
            ],
            package_root,
            env,
        )
    else:
        result = run_command(["sh", "install.sh"], package_root, env)

    env_path = package_root / ".env.devnet"
    env_text = env_path.read_text(encoding="utf-8-sig") if env_path.exists() else ""
    expected = {
        "DOCKERFILE=dockerfile",
        f"DOCKER_PLATFORM=linux/{host_arch()}",
        "BLOCKDAG_CORECHAIN_CONTEXT=.",
        "POOL_SRC_CONTEXT=.",
        "COLLECTOR_SRC_CONTEXT=./collector",
        "DASHBOARD_SRC_CONTEXT=.",
    }
    missing = sorted(line for line in expected if line not in env_text)
    ok = result["returncode"] == 0 and not missing
    payload: dict[str, Any] = {
        "package_root": str(package_root),
        "env_written": env_path.exists(),
        "missing_expected_lines": missing,
        "result": result,
        "ok": ok,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"devnet installer smoke: {'ok' if ok else 'failed'} rc={result['returncode']}")
        if missing:
            print("missing expected .env.devnet lines: " + ", ".join(missing))
    if cleanup is not None:
        cleanup.cleanup()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
