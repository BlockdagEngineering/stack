# Pool release build (GITHUB `pool-v*` tarball, BUILD_CONTEXT=.).
#
# Expects unpacked layout:
#
#   ./
#   ├── bin/blockdag-node, bin/nodeworker, bin/mining-pool, bin/dashboard-api
#   ├── dashboard/          (Compose builds the dashboard Go binary here)
#   ├── docker/             (e.g. no-snapshot.marker)
#   ├── .env.example, docker-compose.yml, …
#
# Snapshot import is runtime-only via a read-only bind mount handled by
# docker/entrypoint-nodeworker.sh. Do not copy .bdsnap files into the image.

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
    test -f ./bin/fastsnap       || { echo 'ERROR: ./bin/fastsnap missing'; exit 1; }; \
    cp -f ./bin/blockdag-node /out/blockdag-node && \
    cp -f ./bin/nodeworker    /out/nodeworker && \
    cp -f ./bin/fastsnap      /out/fastsnap && \
    chmod +x /out/blockdag-node /out/nodeworker /out/fastsnap

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
# Dashboard Build Stage (dashboard)
# ----------------------------------------------------------------------------
FROM base AS dashboard-build
WORKDIR /src/dashboard
COPY dashboard .
# base removed apt lists; refresh index before npm (bookworm npm package installs node toolchain).
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    npm \
 && rm -rf /var/lib/apt/lists/* \
 && npm ci \
 && npm run css:build \
 && go mod tidy \
 && go build -o out/dashboard .

# ----------------------------------------------------------------------------
# Node Runtime Stage
# ----------------------------------------------------------------------------
FROM ubuntu:24.04 AS node

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd -r bdagStack && useradd -r -g bdagStack -d /var/lib/bdagStack -m bdagStack
RUN mkdir -p /etc/bdagStack /var/lib/bdagStack/node/mainnet /var/lib/bdagStack/nodeworker /var/log/bdagStack \
 && chown -R bdagStack:bdagStack /var/lib/bdagStack /var/log/bdagStack /etc/bdagStack

COPY --from=node-build /out/blockdag-node  /usr/local/bin/blockdag-node
COPY --from=node-build /out/nodeworker     /usr/local/bin/nodeworker
COPY --from=node-build /out/fastsnap       /usr/local/bin/fastsnap
RUN chmod +x /usr/local/bin/blockdag-node /usr/local/bin/nodeworker /usr/local/bin/fastsnap

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

# 
# Dashboard Runtime Stage
# ----------------------------------------------------------------------------
FROM ubuntu:24.04 AS dashboard
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd -r bdagStack && useradd -r -g bdagStack -d /var/lib/bdagStack -m bdagStack \
 && mkdir -p /app/logs \
 && chown bdagStack:bdagStack /app/logs

WORKDIR /app/logs
COPY --from=dashboard-build /src/dashboard/out/dashboard /usr/local/bin/dashboard
EXPOSE 9280

RUN chmod +x /usr/local/bin/dashboard
USER bdagStack
ENTRYPOINT [ "/usr/local/bin/dashboard" ]
