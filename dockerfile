# syntax=docker/dockerfile:1.7
# Pool release build (GITHUB `pool-v*` tarball, build context is repo root).
#
# Expects unpacked layout:
#
#   ./
#   ├── bin/blockdag-node, bin/nodeworker, bin/mining-pool, bin/dashboard-api
#   ├── dashboard-source/
#   ├── docker/
#   ├── .env.example, docker-compose.yml, …
#
# Chain bootstrap is IPFS/P2P owned. Do not bake mutable chain artifacts into
# the image.

# ----------------------------------------------------------------------------
# Common base
# ----------------------------------------------------------------------------
FROM golang:1.26-bookworm AS base
RUN apt-get update && apt-get install -y --no-install-recommends \
    make git gcc g++ libc6-dev jq ca-certificates wget \
 && rm -rf /var/lib/apt/lists/*

# ----------------------------------------------------------------------------
# Node Build Stage (blockdag-corechain)
# ----------------------------------------------------------------------------
FROM base AS node-build
WORKDIR /src
COPY bin ./bin
RUN set -eu; mkdir -p /out; \
    test -f ./bin/blockdag-node || { echo 'ERROR: ./bin/blockdag-node missing'; exit 1; }; \
    test -f ./bin/nodeworker     || { echo 'ERROR: ./bin/nodeworker missing'; exit 1; }; \
    cp -f ./bin/blockdag-node /out/blockdag-node && \
    cp -f ./bin/nodeworker    /out/nodeworker && \
    chmod +x /out/blockdag-node /out/nodeworker

# ----------------------------------------------------------------------------
# Pool Build Stage (asic-pool) — binaries from tarball bin/
# ----------------------------------------------------------------------------
FROM base AS pool-build
WORKDIR /src
COPY bin ./bin
RUN set -eu; mkdir -p /out; \
    test -f ./bin/mining-pool    || { echo 'ERROR: ./bin/mining-pool missing'; exit 1; }; \
    cp -f ./bin/mining-pool    /out/mining-pool && \
    chmod +x /out/mining-pool 

# ----------------------------------------------------------------------------
# Collector Source Stage
# ----------------------------------------------------------------------------
FROM alpine:3.20 AS collector-source
ARG COLLECTOR_REPO
ARG COLLECTOR_REF=develop
RUN apk add --no-cache ca-certificates git
RUN --mount=type=secret,id=github_token,required=false set -eu; \
    repo="${COLLECTOR_REPO:-https://github.com/BlockdagEngineering/collector.git}"; \
    ref="${COLLECTOR_REF:-develop}"; \
    token="$(cat /run/secrets/github_token 2>/dev/null || true)"; \
    if [ -n "$token" ]; then \
      auth="$(printf 'x-access-token:%s' "$token" | base64 | tr -d '\n')"; \
      export GIT_CONFIG_COUNT=1; \
      export GIT_CONFIG_KEY_0=http.https://github.com/.extraheader; \
      export GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $auth"; \
    fi; \
    git clone --depth 1 "$repo" /src/collector; \
    cd /src/collector; \
    if git rev-parse --verify "$ref^{commit}" >/dev/null 2>&1; then \
      git checkout --detach "$ref"; \
    else \
      git fetch --depth 1 origin "$ref"; \
      git checkout --detach FETCH_HEAD; \
    fi; \
    rm -rf .git

# ----------------------------------------------------------------------------
# Node Runtime Stage
# ----------------------------------------------------------------------------
FROM ubuntu:24.04 AS node

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates tzdata curl \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd -r bdagStack && useradd -r -g bdagStack -d /var/lib/bdagStack -m bdagStack
RUN mkdir -p /etc/bdagStack /var/lib/bdagStack/node/mainnet /var/lib/bdagStack/nodeworker /var/log/bdagStack \
 && chown -R bdagStack:bdagStack /var/lib/bdagStack /var/log/bdagStack /etc/bdagStack

COPY --from=node-build /out/blockdag-node  /usr/local/bin/blockdag-node
COPY --from=node-build /out/nodeworker     /usr/local/bin/nodeworker
RUN chmod +x /usr/local/bin/blockdag-node /usr/local/bin/nodeworker

COPY docker/entrypoint-nodeworker.sh /usr/local/bin/docker-entrypoint-nodeworker.sh
RUN chmod +x /usr/local/bin/docker-entrypoint-nodeworker.sh

WORKDIR /var/lib/bdagStack/node
EXPOSE 8150 38131 38132 18545 18546 6060
# Start as root so entrypoint can chown Docker volumes (often created as uid 0);
# nodeworker and blockdag-node run as bdagStack after that.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint-nodeworker.sh", \
    "/usr/local/bin/nodeworker", \
    "--node-binary=/usr/local/bin/blockdag-node", \
    "--node-args=--configfile /etc/bdagStack/node.conf", \
    "--rpc-url=ws://127.0.0.1:18546", \
    "--dag-rpc-url=http://127.0.0.1:38131", \
    "--persist-root=/var/lib/bdagStack/nodeworker", \
    "--health-min-peers=1", \
    "--rollout-window=30m"]

# ----------------------------------------------------------------------------
# Pool Runtime Stage
# ----------------------------------------------------------------------------
FROM ubuntu:24.04 AS pool
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd -r bdagStack && useradd -r -g bdagStack -d /var/lib/bdagStack -m bdagStack \
 && mkdir -p /etc/bdagStack /var/lib/bdagStack/pool /var/log/bdagStack \
 && chown -R bdagStack:bdagStack /var/lib/bdagStack /var/log/bdagStack /etc/bdagStack

COPY --from=pool-build /out/mining-pool    /usr/local/bin/mining-pool
RUN chmod +x /usr/local/bin/mining-pool 

# godotenv loads this path at runtime; tarball uses committed .env.example. Copy a
# local `.env` on the host only if you want non-default keys in-file.
COPY .env.example /var/lib/bdagStack/pool/.env
RUN chown bdagStack:bdagStack /var/lib/bdagStack/pool/.env

USER bdagStack
WORKDIR /var/lib/bdagStack/pool
EXPOSE 3334 8080
ENTRYPOINT ["/usr/local/bin/mining-pool"]

# ----------------------------------------------------------------------------
# Collector Runtime Stage (read-only Python API)
# ----------------------------------------------------------------------------
FROM docker:27-cli AS collector

RUN apk add --no-cache \
    bash \
    ca-certificates \
    coreutils \
    curl \
    findutils \
    iproute2 \
    procps \
    py3-pip \
    python3 \
    shadow \
    tzdata

COPY --from=collector-source /src/collector /opt/collector
# Compose supplies collector_src from COLLECTOR_SRC_CONTEXT so local fresh builds
# can run a checked-out collector without silently cloning an older ref.
COPY --from=collector_src . /opt/collector
COPY docker/entrypoint-collector.sh /usr/local/bin/entrypoint-collector.sh
RUN chmod +x /usr/local/bin/entrypoint-collector.sh \
 && mkdir -p /var/lib/bdag-collector/runtime /workspace \
 && if [ -f /opt/collector/requirements.txt ]; then \
      python3 -m pip install --break-system-packages --no-cache-dir -r /opt/collector/requirements.txt; \
    fi

ENV PYTHONUNBUFFERED=1 \
    BDAG_PROJECT_ROOT=/workspace \
    BDAG_RUNTIME_DIR=/var/lib/bdag-collector/runtime \
    BDAG_POOL_ENV_FILE=/workspace/.env \
    BDAG_COLLECTOR_BIND=0.0.0.0 \
    BDAG_COLLECTOR_PORT=9280

WORKDIR /opt/collector
EXPOSE 9280
ENTRYPOINT ["/usr/local/bin/entrypoint-collector.sh"]

# ----------------------------------------------------------------------------
# Dashboard Runtime Stage (Python UI from packaged dashboard-source/)
# ----------------------------------------------------------------------------
FROM docker:27-cli AS dashboard

RUN apk add --no-cache \
    bash \
    ca-certificates \
    coreutils \
    curl \
    findutils \
    iproute2 \
    openssl \
    procps \
    py3-pip \
    python3 \
    shadow \
    tzdata

COPY dashboard-source /opt/dashboard
COPY docker/entrypoint-dashboard.sh /usr/local/bin/entrypoint-dashboard.sh
RUN chmod +x /usr/local/bin/entrypoint-dashboard.sh \
 && mkdir -p /workspace /workspace/ops/runtime \
 && if [ -f /opt/dashboard/requirements-dev.txt ]; then \
      python3 -m pip install --break-system-packages --no-cache-dir -r /opt/dashboard/requirements-dev.txt; \
    fi

ENV PYTHONUNBUFFERED=1 \
    BDAG_PROJECT_ROOT=/workspace \
    BDAG_RUNTIME_DIR=/workspace/ops/runtime \
    BDAG_POOL_ENV_FILE=/workspace/.env \
    BDAG_DASHBOARD_BIND=0.0.0.0 \
    BDAG_DASHBOARD_PORT=9290

EXPOSE 9290
ENTRYPOINT ["/usr/local/bin/entrypoint-dashboard.sh"]
