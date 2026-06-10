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
need_grep 'scripts/verify-release-architecture.py --target' ".github/workflows/build.yml"
need_grep 'scripts/check-release-archive.py' ".github/workflows/build.yml"
need_grep 'release_bootstrap_static_test.py' ".github/workflows/build.yml"
need_grep 'scripts/render-release-bootstrap.py' ".github/workflows/build.yml"
need_grep 'release_install_smoke.py' ".github/workflows/build.yml"
need_grep 'release_install_smoke.py' ".github/workflows/rc-hardening.yml"
need_grep 'release-payload.env' ".github/workflows/build.yml"
need_grep 'pool-stack-docker-\*\.zip' ".github/workflows/build.yml"
reject_grep 'DASHBOARD_REF=' ".env.example"
reject_grep 'DASHBOARD_REPO:' "docker-compose.yml"
reject_grep 'DASHBOARD_REF:' "docker-compose.yml"
need_grep 'test -f \./bin/dashboard' "dockerfile"
need_grep 'COPY --from=dashboard-build /out/dashboard /usr/local/bin/dashboard' "dockerfile"
need_grep 'COPY --from=dashboard_src \. \.' "dockerfile-dev"
reject_grep 'DASHBOARD_REF:-' "docker-compose.yml"
reject_grep 'DASHBOARD_REF:-' "dockerfile"
retired_terms=(
  'Fast''Artifact'
  'Fast''Sync'
  'Fast''Snap'
  'fast''artifact'
  'fast''sync'
  'fast''snap'
  'SNAP''SHOT_PATH'
  'BDAG_''SNAP''SHOT'
  'latest\.bd''snap'
  'snap''shot\.bd''snap'
  'snap'' import'
)
retired_scope=(
  ".env.example"
  "docker-compose.yml"
  "dockerfile"
  "dockerfile-dev"
  "docker/entrypoint-nodeworker.sh"
  "scripts/release/installers/install-unix-common.sh"
  "scripts/release/installers/install-windows.ps1"
  "scripts/release/installers/install-macos.sh"
)
for retired_pattern in "${retired_terms[@]}"; do
  for retired_file in "${retired_scope[@]}"; do
    reject_grep "$retired_pattern" "$retired_file"
  done
done
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

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  docker compose --env-file "$root/.env.example" -f "$root/docker-compose.yml" config --services >/dev/null
else
  printf 'warning: docker compose unavailable; skipped compose syntax validation\n' >&2
fi

printf 'release build validation passed for %s\n' "$root"
