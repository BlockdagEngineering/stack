#!/usr/bin/env python3

import pathlib
import subprocess
import unittest


ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]


def parse_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


class StackDefaultsTests(unittest.TestCase):
    def test_global_scan_window_is_stack_owned(self) -> None:
        defaults = parse_env(ROOT_DIR / "ops/config/stack-defaults.env")
        self.assertEqual(defaults["BDAG_GLOBAL_BLOCK_WINDOW"], "600")

        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn(
            f"BDAG_GLOBAL_BLOCK_WINDOW: ${{BDAG_GLOBAL_BLOCK_WINDOW:-{defaults['BDAG_GLOBAL_BLOCK_WINDOW']}}}",
            compose,
        )

    def test_compose_dashboard_is_authoritative_by_default(self) -> None:
        defaults = parse_env(ROOT_DIR / "ops/config/stack-defaults.env")
        self.assertEqual(defaults["DASHBOARD_HOST_PORT"], "8088")
        self.assertEqual(defaults["BDAG_DASHBOARD_PORT"], "8088")
        self.assertEqual(defaults["BDAG_STATUS_SAMPLER_ENABLED"], "0")
        self.assertEqual(defaults["BDAG_DASHBOARD_DIRECT_STATUS_FALLBACK"], "1")

        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('${DASHBOARD_HOST_BIND:-127.0.0.1}:${DASHBOARD_HOST_PORT:-8088}:${BDAG_DASHBOARD_PORT:-8088}', compose)
        self.assertIn("BDAG_STATUS_SAMPLER_ENABLED: ${BDAG_STATUS_SAMPLER_ENABLED:-0}", compose)
        self.assertIn("BDAG_DASHBOARD_DIRECT_STATUS_FALLBACK: ${BDAG_DASHBOARD_DIRECT_STATUS_FALLBACK:-1}", compose)

    def test_compose_tip_lag_fallback_matches_stack_default(self) -> None:
        defaults = parse_env(ROOT_DIR / "ops/config/stack-defaults.env")
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        expected = (
            "BDAG_GLOBAL_CACHE_MAX_TIP_LAG_BLOCKS: "
            f"${{BDAG_GLOBAL_CACHE_MAX_TIP_LAG_BLOCKS:-{defaults['BDAG_GLOBAL_CACHE_MAX_TIP_LAG_BLOCKS']}}}"
        )
        self.assertIn(expected, compose)

    def test_stack_defaults_validator_passes(self) -> None:
        result = subprocess.run(
            ["python3", "scripts/validate-stack-defaults.py"],
            cwd=ROOT_DIR,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
