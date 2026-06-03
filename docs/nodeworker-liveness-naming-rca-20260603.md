# Nodeworker Liveness And Stack Naming RCA - 2026-06-03

## Incident

On 2026-06-03 the mining stack containers were running, but the node container
was not serving a live BlockDAG child process. The pool and dashboard could see
container liveness, but miners did not receive usable current jobs because the
nodeworker wrapper remained up after the `blockdag-node` child was killed.

The root-cause log sequence was:

- nodeworker liveness probe exceeded the 1 minute timeout while the node was
  doing heavy chain/state catch-up work.
- nodeworker stopped and killed the inner binary.
- the container stayed running with only nodeworker alive.
- watchdog defaults still targeted older Compose names and the child guard only
  detected the legacy `bdag` executable name, not the packaged
  `blockdag-node` child.

## Fix

The stack now treats `node`, `pool`, and `postgres` as the current container
names and keeps `bdag-miner-node-1`, `asic-pool`, and `pool-db` only as legacy
compatibility aliases where required.

The node child guard now:

- detects both `blockdag-node` and legacy `bdag` child executables;
- defaults to guarding `node` while retaining the legacy node alias;
- resolves the actual Compose service label before restart;
- falls back to direct `docker restart`/`docker start` when Compose targeting
  fails;
- omits missing legacy env files when building Compose commands.

The node entrypoint now adds `--health.liveness-timeout=5m` by default unless an
operator explicitly supplies another nodeworker liveness timeout. This avoids
turning normal constrained-host catch-up pressure into a stale running
container with no child node.

## Build Gates

CI now includes stack naming coherence tests that verify:

- Compose/dashboard defaults use `postgres,node,pool`;
- release installers and the ARM64 builder emit current names and `node` RPC
  URLs;
- watchdog and peer-refresh defaults know the current topology;
- the hardening validator fails if current naming checks drift.

## Live Deployment Note

This host still has legacy Compose service labels on existing containers, but
container names and Docker DNS resolve the current names. Runtime guards are
therefore label-aware: they report and act on `node`, `pool`, and `postgres`
while using Compose labels only as restart targets for existing installations.
