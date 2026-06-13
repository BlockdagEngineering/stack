#!/usr/bin/env bash
set -euo pipefail

root="${1:-.}"

fail() {
  printf 'release build validation failed: %s\n' "$*" >&2
  exit 1
}

need_file() {
  local file="$1"
  [[ -f "$root/$file" ]] || fail "missing $file"
}

need_grep() {
  local pattern="$1"
  local file="$2"
  grep -Eq "$pattern" "$root/$file" || fail "$file does not match required pattern: $pattern"
}

reject_grep() {
  local pattern="$1"
  local file="$2"
  [[ -f "$root/$file" ]] || return 0
  if grep -Eq "$pattern" "$root/$file"; then
    fail "$file still matches rejected pattern: $pattern"
  fi
}

need_file ".github/workflows/build.yml"
need_file "scripts/render-release-bootstrap.py"
need_file "scripts/release_bootstrap_static_test.py"
need_file "scripts/release_install_smoke.py"
need_file "scripts/verify-release-architecture.py"
need_file "scripts/check-release-archive.py"
need_file "scripts/release/install.sh"
need_file "scripts/release/install.ps1"
need_file "scripts/release/install.cmd"
need_file "scripts/release/installers/install-unix-common.sh"
need_file "scripts/release/installers/install-windows.ps1"
need_file "README.md"
need_file "docs/glossary.md"
need_file "docs/adr/0001-pinned-bootstrap-runtime-payload-zips.md"

need_grep 'target: linux-amd64' ".github/workflows/build.yml"
need_grep 'target: linux-arm64' ".github/workflows/build.yml"
need_grep 'BlockdagEngineering/dashboard2' ".github/workflows/build.yml"
need_grep 'cmd/bdag/bdag.go' ".github/workflows/build.yml"
need_grep 'scripts/verify-release-architecture.py --target' ".github/workflows/build.yml"
need_grep 'scripts/check-release-archive.py' ".github/workflows/build.yml"
need_grep 'release_bootstrap_static_test.py' ".github/workflows/build.yml"
need_grep 'scripts/render-release-bootstrap.py' ".github/workflows/build.yml"
need_grep 'release_install_smoke.py' ".github/workflows/build.yml"
need_grep 'release_install_smoke.py' ".github/workflows/rc-hardening.yml"
need_grep 'release-payload.env' ".github/workflows/build.yml"
need_grep 'pool-stack-docker-\*\.zip' ".github/workflows/build.yml"
need_grep '^SNAPSHOT_PATH=docker/no-snapshot\.marker$' ".env.example"
need_grep 'SNAPSHOT_PATH: \$\{SNAPSHOT_PATH:-docker/no-snapshot\.marker\}' "docker-compose.yml"
reject_grep 'SNAPSHOT_PATH=.*release-downloads/latest\.bdsnap' ".env.example"
reject_grep 'SNAPSHOT_PATH:-\./latest\.bdsnap' "docker-compose.yml"
need_grep '^DASHBOARD_HOST_PORT=8088$' ".env.example"
need_grep '\$\{DASHBOARD_HOST_PORT:-8088\}:8088' "docker-compose.yml"
reject_grep '^[A-Z0-9_]*CONTEXT=\.\.' ".env.example"
need_grep 'COLLECTOR_REPO: \$\{COLLECTOR_REPO:-https://github\.com/BlockdagEngineering/collector\.git\}' "docker-compose.yml"
need_grep 'COLLECTOR_REF: develop' "docker-compose.yml"
need_grep 'collector_src: \$\{COLLECTOR_SRC_CONTEXT:-\.\}' "docker-compose.yml"
need_grep 'dashboard_src: \$\{DASHBOARD_SRC_CONTEXT:-\.\}' "docker-compose.yml"
need_grep 'ARG COLLECTOR_REF=develop' "dockerfile"
need_grep 'git clone --depth 1 "\$repo" /src/collector' "dockerfile"
need_grep 'COPY --from=collector-source /src/collector /opt/collector' "dockerfile"
reject_grep 'COPY --from=collector_src \. /opt/collector' "dockerfile"
reject_grep 'COLLECTOR_SRC_CONTEXT:' ".github/workflows/build.yml"
need_grep '^BOOTSTRAP_PEER_ADDRESSES=/ip4/13\.57\.132\.47/tcp/8150/p2p/16Uiu2HAmDynYpWjWmgVGf9qVWvDdLnJ3ybVgDmFexizR4zMereus$' ".env.example"
need_grep 'BOOTSTRAP_PEER_ADDRESSES: \$\{BOOTSTRAP_PEER_ADDRESSES:-\}' "docker-compose.yml"
need_grep '^addpeer=/ip4/13\.57\.132\.47/tcp/8150/p2p/16Uiu2HAmDynYpWjWmgVGf9qVWvDdLnJ3ybVgDmFexizR4zMereus$' "node.conf.example"
reject_grep '^addpeer=/ip4/52\.8\.80\.249/tcp/8150/p2p/' "node.conf.example"
reject_grep '^addpeer=/ip4/192\.168\.' "node.conf.example"
need_grep 'pool-stack-docker-<tag>-linux-amd64\.zip' "README.md"
need_grep 'pool-stack-docker-<tag>-linux-arm64\.zip' "README.md"

need_grep 'release-payload.env' "scripts/release/installers/install-unix-common.sh"
need_grep 'release-payload.env' "scripts/release/installers/install-windows.ps1"
need_grep 'set_env_value .env DOCKER_PLATFORM "\$DOCKER_PLATFORM"' "scripts/release/installers/install-unix-common.sh"
need_grep 'Set-EnvValue .env DOCKER_PLATFORM \$dockerPlatform' "scripts/release/installers/install-windows.ps1"

reject_grep 'amd64 emulation' "scripts/release/installers/install-unix-common.sh"
reject_grep 'amd64 emulation' "scripts/release/installers/install-windows.ps1"
reject_grep 'build-pi5-arm64-release\.sh' ".github/workflows/build.yml"
reject_grep 'build-pi5-arm64-release\.sh' ".github/workflows/rc-hardening.yml"
reject_grep 'build-pi5-arm64-release\.sh' "README.md"
reject_grep 'build-pi5-arm64-release\.sh' "AGENTS.md"
reject_grep 'build-pi5-arm64-release\.sh' "docs/glossary.md"
reject_grep 'build-pi5-arm64-release\.sh' "docs/adr/0001-pinned-bootstrap-runtime-payload-zips.md"
reject_grep 'validate-pi5-restart-hardening\.sh' ".github/workflows/build.yml"
reject_grep 'validate-pi5-restart-hardening\.sh' ".github/workflows/rc-hardening.yml"

printf 'release build validation passed for %s\n' "$root"
