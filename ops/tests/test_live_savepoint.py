from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "create-live-savepoint.sh"


def make_root(tmp: Path, docker_body: str) -> tuple[Path, Path]:
    root = tmp / "stack"
    root.mkdir()
    (root / ".env").write_text("DOCKERFILE=dockerfile\nNODE_DATA_DIR=./data/node\n", encoding="utf-8")
    (root / "dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (root / "node.conf").write_text("# test\n", encoding="utf-8")

    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(docker_body, encoding="utf-8")
    docker.chmod(0o755)
    return root, docker


def run_savepoint(root: Path, docker: Path, stamp: str = "teststamp") -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "DOCKER": str(docker),
            "BDAG_SAVEPOINT_SERVICES": "node pool",
            "BDAG_SAVEPOINT_COMPRESS": "0",
        }
    )
    return subprocess.run(
        [str(SCRIPT), "--root", str(root), "--stamp", stamp],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class LiveSavepointTests(unittest.TestCase):
    def test_savepoint_exports_required_images(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            docker_body = textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                if [[ "$1" == "compose" ]]; then
                  printf 'services: {}\\n'
                  exit 0
                fi
                if [[ "$1" == "ps" ]]; then
                  printf 'CONTAINER ID IMAGE\\n'
                  exit 0
                fi
                if [[ "$1" == "images" ]]; then
                  printf 'REPOSITORY TAG DIGEST\\n'
                  exit 0
                fi
                if [[ "$1" == "inspect" && "$2" == "-f" && "$3" == "{{.Image}}" ]]; then
                  printf 'sha256:%s-image\\n' "$4"
                  exit 0
                fi
                if [[ "$1" == "inspect" && "$2" == "-f" && "$3" == "{{.Config.Image}}" ]]; then
                  printf 'stack-%s:latest\\n' "$4"
                  exit 0
                fi
                if [[ "$1" == "image" && "$2" == "inspect" ]]; then
                  printf '{"Id":"%s"}\\n' "$3"
                  exit 0
                fi
                if [[ "$1" == "tag" ]]; then
                  exit 0
                fi
                if [[ "$1" == "save" && "$2" == "-o" ]]; then
                  printf 'fake image archive\\n' > "$3"
                  exit 0
                fi
                printf 'unexpected docker call: %s\\n' "$*" >&2
                exit 2
                """
            )
            root, docker = make_root(tmp, docker_body)

            result = run_savepoint(root, docker)

            self.assertEqual(result.returncode, 0, result.stderr)
            savepoint = root / "ops" / "runtime" / "savepoints" / "teststamp"
            archive = savepoint / "images" / "stack-images-savepoint-teststamp.tar"
            self.assertTrue(archive.is_file())
            self.assertTrue((archive.with_suffix(archive.suffix + ".sha256")).is_file())
            manifest = (savepoint / "manifest.tsv").read_text(encoding="utf-8")
            self.assertIn("stack-node:savepoint-teststamp", manifest)
            self.assertIn("stack-pool:savepoint-teststamp", manifest)

    def test_custom_stamp_is_sanitized_for_docker_tags(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            docker_body = textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                if [[ "$1" == "compose" ]]; then
                  printf 'services: {}\\n'
                  exit 0
                fi
                if [[ "$1" == "ps" || "$1" == "images" ]]; then
                  exit 0
                fi
                if [[ "$1" == "inspect" && "$2" == "-f" && "$3" == "{{.Image}}" ]]; then
                  printf 'sha256:%s-image\\n' "$4"
                  exit 0
                fi
                if [[ "$1" == "inspect" && "$2" == "-f" && "$3" == "{{.Config.Image}}" ]]; then
                  printf 'stack-%s:latest\\n' "$4"
                  exit 0
                fi
                if [[ "$1" == "image" && "$2" == "inspect" ]]; then
                  printf '{"Id":"%s"}\\n' "$3"
                  exit 0
                fi
                if [[ "$1" == "tag" ]]; then
                  case "$3" in
                    *+*) printf 'invalid unsanitized tag: %s\\n' "$3" >&2; exit 9 ;;
                  esac
                  exit 0
                fi
                if [[ "$1" == "save" && "$2" == "-o" ]]; then
                  printf 'fake image archive\\n' > "$3"
                  exit 0
                fi
                printf 'unexpected docker call: %s\\n' "$*" >&2
                exit 2
                """
            )
            root, docker = make_root(tmp, docker_body)

            result = run_savepoint(root, docker, "20260625-164535+0200-pre-live-update")

            self.assertEqual(result.returncode, 0, result.stderr)
            savepoint = root / "ops" / "runtime" / "savepoints" / "20260625-164535+0200-pre-live-update"
            archive = savepoint / "images" / "stack-images-savepoint-20260625-164535-0200-pre-live-update.tar"
            self.assertTrue(archive.is_file())
            self.assertEqual(
                "20260625-164535-0200-pre-live-update",
                (savepoint / "docker-tag-stamp").read_text(encoding="utf-8").strip(),
            )
            manifest = (savepoint / "manifest.tsv").read_text(encoding="utf-8")
            self.assertIn("stack-node:savepoint-20260625-164535-0200-pre-live-update", manifest)

    def test_savepoint_fails_closed_when_image_cannot_be_inspected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            docker_body = textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                if [[ "$1" == "compose" ]]; then
                  printf 'services: {}\\n'
                  exit 0
                fi
                if [[ "$1" == "ps" || "$1" == "images" ]]; then
                  exit 0
                fi
                if [[ "$1" == "inspect" && "$2" == "-f" && "$3" == "{{.Image}}" ]]; then
                  printf 'sha256:%s-image\\n' "$4"
                  exit 0
                fi
                if [[ "$1" == "inspect" && "$2" == "-f" && "$3" == "{{.Config.Image}}" ]]; then
                  printf 'stack-%s:latest\\n' "$4"
                  exit 0
                fi
                if [[ "$1" == "image" && "$2" == "inspect" ]]; then
                  exit 1
                fi
                printf 'unexpected docker call: %s\\n' "$*" >&2
                exit 2
                """
            )
            root, docker = make_root(tmp, docker_body)

            result = run_savepoint(root, docker)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("image preservation failed", result.stderr)
            savepoint = root / "ops" / "runtime" / "savepoints" / "teststamp"
            self.assertFalse((savepoint / "images" / "stack-images-savepoint-teststamp.tar").exists())

    def test_savepoint_rejects_missing_configured_dockerfile(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            root, docker = make_root(tmp, "#!/usr/bin/env bash\nexit 0\n")
            (root / ".env").write_text("DOCKERFILE=dockerfile-dev\n", encoding="utf-8")

            result = run_savepoint(root, docker)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("configured Dockerfile is missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
