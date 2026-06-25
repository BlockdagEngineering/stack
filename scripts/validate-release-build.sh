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

reject_file() {
  local file="$1"
  [[ ! -e "$root/$file" ]] || fail "retired release artifact still exists: $file"
}

need_grep() {
  local pattern="$1"
  local file="$2"
  grep -Eq -- "$pattern" "$root/$file" || fail "$file does not match required pattern: $pattern"
}

reject_grep() {
  local pattern="$1"
  local file="$2"
  [[ -f "$root/$file" ]] || return 0
  if grep -Eq -- "$pattern" "$root/$file"; then
    fail "$file still matches rejected pattern: $pattern"
  fi
}

reject_service_block_grep() {
  local service="$1"
  local pattern="$2"
  local file="$3"
  [[ -f "$root/$file" ]] || return 0
  if awk -v service="  ${service}:" -v pattern="$pattern" '
    $0 == service { in_block = 1; next }
    in_block && $0 ~ /^  [A-Za-z0-9_-]+:/ { in_block = 0 }
    in_block && $0 ~ pattern { found = 1 }
    END { exit found ? 0 : 1 }
  ' "$root/$file"; then
    fail "$file service $service still matches rejected pattern: $pattern"
  fi
}

need_file ".github/workflows/build.yml"
reject_file ".github/workflows/build-cpu.yml"
reject_file "dockerfile-dev"
need_file "scripts/render-release-bootstrap.py"
need_file "scripts/release_bootstrap_static_test.py"
need_file "scripts/release_install_smoke.py"
need_file "scripts/verify-release-architecture.py"
need_file "scripts/check-release-archive.py"
reject_file "docker/entrypoint-collector.sh"
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
need_grep 'BlockdagEngineering/redis-dash' ".github/workflows/build.yml"
need_grep 'verify_repo redis-dash src/dashboard/main[.]go' ".github/workflows/build.yml"
need_grep 'repository: BlockdagEngineering/blockdag-corechain' ".github/workflows/build.yml"
need_grep 'repository: BlockdagEngineering/pool' ".github/workflows/build.yml"
need_grep 'repository: BlockdagEngineering/redis-dash' ".github/workflows/build.yml"
need_grep 'ref: main' ".github/workflows/build.yml"
reject_grep 'BlockdagEngineering/dashboard2' ".github/workflows/build.yml"
reject_grep 'BlockdagEngineering/dashboard([.]git|$|[[:space:]/])' ".github/workflows/build.yml"
reject_grep 'BlockdagEngineering/collector' ".github/workflows/build.yml"
reject_grep 'BlockdagEngineering/cpu-miner' ".github/workflows/build.yml"
reject_grep 'BlockdagEngineering/gpu-miner' ".github/workflows/build.yml"
reject_grep 'Checkout collector repo' ".github/workflows/build.yml"
reject_grep 'path: src/collector' ".github/workflows/build.yml"
reject_grep 'src/collector/' ".github/workflows/build.yml"
reject_grep 'Release zip is missing collector[.]py' ".github/workflows/build.yml"
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
need_grep '^POOL_STRATUM_SERVER_FIRST_DIFFICULTY_PROBE=false$' ".env.example"
need_grep 'POOL_STRATUM_SERVER_FIRST_DIFFICULTY_PROBE: \$\{POOL_STRATUM_SERVER_FIRST_DIFFICULTY_PROBE:-false\}' "docker-compose.yml"
need_grep '^BDAG_NODE_MINING_NO_PENDING_TX=1$' ".env.example"
need_grep 'BDAG_NODE_MINING_NO_PENDING_TX: \$\{BDAG_NODE_MINING_NO_PENDING_TX:-1\}' "docker-compose.yml"
need_grep 'BDAG_NODE_MINING_NO_PENDING_TX=1' "ops/config/stack-defaults.env"
need_grep '--miningnopendingtx' "docker/entrypoint-nodeworker.sh"
need_grep '^DASHBOARD_SRC_CONTEXT=../redis-dash$' ".env.example"
need_grep '^DASHBOARD_REPO=https://github[.]com/BlockdagEngineering/redis-dash[.]git$' ".env.example"
need_grep 'dashboard_src: \$\{DASHBOARD_SRC_CONTEXT:-\.\./redis-dash\}' "docker-compose.yml"
reject_grep 'COLLECTOR_SRC_CONTEXT' ".env.example"
reject_grep 'CPU_MINER_SRC_CONTEXT' ".env.example"
reject_grep 'collector_src:' "docker-compose.yml"
reject_grep 'cpu_miner_src:' "docker-compose.yml"
reject_grep '^  collector:' "docker-compose.yml"
reject_grep '^  cpu-miner:' "docker-compose.yml"
reject_grep '^  collector:' "docker-compose.override.yml"
need_grep 'FROM docker:27-cli AS ops-runtime' "dockerfile"
need_grep 'FROM ops-runtime AS watchdog' "dockerfile"
need_grep 'FROM ops-runtime AS status-sampler' "dockerfile"
need_grep 'FROM ops-runtime AS sentinel' "dockerfile"
reject_grep 'FROM ops-runtime AS collector' "dockerfile"
reject_grep 'COPY --from=collector_src' "dockerfile"
reject_grep 'COPY --from=collector-source' "dockerfile"
reject_grep 'dashboard2' "dockerfile"
need_grep 'target: watchdog' "docker-compose.yml"
need_grep 'target: status-sampler' "docker-compose.yml"
need_grep 'target: sentinel' "docker-compose.yml"
reject_service_block_grep 'watchdog' 'collector_src' "docker-compose.yml"
reject_service_block_grep 'status-sampler' 'collector_src' "docker-compose.yml"
reject_service_block_grep 'sentinel' 'collector_src' "docker-compose.yml"
reject_grep 'git clone --depth 1' "dockerfile"
reject_grep 'ARG COLLECTOR_REF' "dockerfile"
reject_grep 'COPY --from=collector_src \. /opt/collector' "dockerfile"
need_grep '^BOOTSTRAP_PEER_ADDRESSES=.*3\.126\.64\.13/tcp/8152/p2p/16Uiu2HAmEFxRaBbbf3sRi43CCvMk5Y6zPkuGY9s4uRK2FKJVJkqo' ".env.example"
need_grep '^BOOTSTRAP_PEER_ADDRESSES=.*16\.28\.133\.168/tcp/8150/p2p/16Uiu2HAm9UcTayJDSajjJYsWwVaN2qqGeczcs9kXse3dMdvGDRjz' ".env.example"
need_grep '^BOOTSTRAP_PEER_ADDRESSES=.*102\.182\.77\.21/tcp/8150/p2p/16Uiu2HAm2uPLqM1dfd3ddbzg3FDAmvuvyo3vbBSXpVHurqAsUxWQ' ".env.example"
need_grep '^BOOTSTRAP_PEER_ADDRESSES=.*102\.182\.77\.16/tcp/8150/p2p/16Uiu2HAm99a8KYuUkL5LGEQbpRNhz5nzScrwYpEh47BfSaVMCg5G' ".env.example"
need_grep '^BOOTSTRAP_PEER_ADDRESSES=.*129\.121\.92\.232/tcp/8152/p2p/16Uiu2HAmQSJzJjXUxtyX5rc2bQBAsTvhcjp4GQBPmwqEYu9D8zA5' ".env.example"
need_grep '^BOOTSTRAP_PEER_ADDRESSES=.*3\.120\.205\.55/tcp/8150/p2p/16Uiu2HAm8tJ2Loxi1hc7Apg4v5i8mqpNxWyRknn1tZUcx8AvbNYj' ".env.example"
need_grep 'BOOTSTRAP_PEER_ADDRESSES: \$\{BOOTSTRAP_PEER_ADDRESSES:-\}' "docker-compose.yml"
need_grep '^addpeer=/ip4/3\.126\.64\.13/tcp/8152/p2p/16Uiu2HAmEFxRaBbbf3sRi43CCvMk5Y6zPkuGY9s4uRK2FKJVJkqo$' "node.conf.example"
need_grep '^addpeer=/ip4/16\.28\.133\.168/tcp/8150/p2p/16Uiu2HAm9UcTayJDSajjJYsWwVaN2qqGeczcs9kXse3dMdvGDRjz$' "node.conf.example"
need_grep '^addpeer=/ip4/102\.182\.77\.21/tcp/8150/p2p/16Uiu2HAm2uPLqM1dfd3ddbzg3FDAmvuvyo3vbBSXpVHurqAsUxWQ$' "node.conf.example"
need_grep '^addpeer=/ip4/102\.182\.77\.16/tcp/8150/p2p/16Uiu2HAm99a8KYuUkL5LGEQbpRNhz5nzScrwYpEh47BfSaVMCg5G$' "node.conf.example"
need_grep '^addpeer=/ip4/129\.121\.92\.232/tcp/8152/p2p/16Uiu2HAmQSJzJjXUxtyX5rc2bQBAsTvhcjp4GQBPmwqEYu9D8zA5$' "node.conf.example"
need_grep '^addpeer=/ip4/3\.120\.205\.55/tcp/8150/p2p/16Uiu2HAm8tJ2Loxi1hc7Apg4v5i8mqpNxWyRknn1tZUcx8AvbNYj$' "node.conf.example"
reject_grep '^addpeer=/ip4/52\.8\.80\.249/tcp/8150/p2p/' "node.conf.example"
reject_grep '^addpeer=/ip4/192\.168\.' "node.conf.example"
reject_grep '13\.140\.165\.186/tcp/8150/p2p/16Uiu2HAm4hHD7Ht5LJrLgaKXr7YP2RzHHjrrCLNt8zv8FQ9s3gBU' "node.conf.example"
reject_grep '13\.140\.165\.186/tcp/8150/p2p/16Uiu2HAm4hHD7Ht5LJrLgaKXr7YP2RzHHjrrCLNt8zv8FQ9s3gBU' ".env.example"
reject_grep '102\.182\.77\.21/tcp/8151/p2p/16Uiu2HAm2uPLqM1dfd3ddbzg3FDAmvuvyo3vbBSXpVHurqAsUxWQ' "node.conf.example"
reject_grep '102\.182\.77\.21/tcp/8151/p2p/16Uiu2HAm2uPLqM1dfd3ddbzg3FDAmvuvyo3vbBSXpVHurqAsUxWQ' ".env.example"
reject_grep '102\.182\.77\.16/tcp/8152/p2p/16Uiu2HAm99a8KYuUkL5LGEQbpRNhz5nzScrwYpEh47BfSaVMCg5G' "node.conf.example"
reject_grep '102\.182\.77\.16/tcp/8152/p2p/16Uiu2HAm99a8KYuUkL5LGEQbpRNhz5nzScrwYpEh47BfSaVMCg5G' ".env.example"
reject_grep '16Uiu2HAkx4trymxQDexfzCNrtWokprH49vNg8shhEhtPMYdq2CtY' "node.conf.example"
reject_grep '/tcp/52604/p2p/' "node.conf.example"
reject_grep '/tcp/34040/p2p/' "node.conf.example"
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

printf 'release build validation passed for %s\n' "$root"
