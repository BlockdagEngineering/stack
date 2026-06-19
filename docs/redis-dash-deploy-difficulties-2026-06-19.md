# Redis Dashboard Deploy Difficulties - 2026-06-19

Context: local source deployment of `redis-dash-0-0-1` on Ubuntu 26.04 with
chain data from `/home/jeremy/Downloads/bdag-latest-snapshot.tar.gz`.

## Difficulties

- Docker was not installed. This host needed `docker.io`, `docker-compose-v2`,
  and `docker-buildx`; sudo first required a validated passwordless sudoers
  drop-in.
- The local file was named `.tar.gz` but was Zstandard-compressed raw chain
  data, not a `.bdsnap`. It contained `BdagChain/`, `bdageth/`, metadata, and a
  peerstore backup.
- The release installer only handled `.bdsnap` staging. It did not provide an
  explicit raw datadir archive path.
- A clean Docker host did not have `postgres:15-bookworm`; starting with
  `--pull never` failed until `pool-db` was pulled explicitly.
- Installer/docs referred to `postgres node dashboard`, but the compose service
  name is `pool-db`; `postgres` is only the container name.
- `pool` uses host networking, so compose-service DNS names such as
  `pool-db` and `node` do not resolve there. The pool must use
  `127.0.0.1` for Postgres and node RPC.
- `dashboard` runs on the compose bridge while `node` and `pool` use host
  networking. It needs `host.docker.internal:host-gateway` and host-facing
  defaults for EVM RPC, pool metrics, and collector API.
- Several TCP-open peers rejected libp2p security negotiation because the stored
  peer ID was stale or the peer reset the handshake.
- The peer `/ip4/13.140.165.186/tcp/8150/p2p/16Uiu2HAm4hHD7Ht5LJrLgaKXr7YP2RzHHjrrCLNt8zv8FQ9s3gBU`
  handshook and was ahead briefly, then dropped.
- The corrected peer
  `/ip4/16.28.133.168/tcp/8151/p2p/16Uiu2HAkx4trymxQDexfzCNrtWokprH49vNg8shhEhtPMYdq2CtY`
  handshook, but it was far behind the local chain tip, so it is not a
  mining-safe sync reference.

## Safety Outcome

- `node`, `postgres`, and `dashboard` were left running.
- `pool` was stopped after the active ahead peer dropped. Do not expose Stratum
  or attach miners until `getPeerInfo` shows a useful current/ahead peer and
  `isCurrent` is true.
- Node mining remained disabled: no `--miner`, no CPU miner profile, and no
  unsynced mining bypass flags were used.
