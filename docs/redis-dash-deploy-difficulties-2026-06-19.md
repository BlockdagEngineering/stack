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
  defaults for EVM RPC, pool metrics, and dashboard status API.
- Several TCP-open peers rejected libp2p security negotiation because the stored
  peer ID was stale or the peer reset the handshake.
- The peer `/ip4/13.140.165.186/tcp/8150/p2p/16Uiu2HAm4hHD7Ht5LJrLgaKXr7YP2RzHHjrrCLNt8zv8FQ9s3gBU`
  handshook and was ahead briefly, then dropped.
- The corrected peer
  `/ip4/16.28.133.168/tcp/8151/p2p/16Uiu2HAkx4trymxQDexfzCNrtWokprH49vNg8shhEhtPMYdq2CtY`
  handshook, but it was far behind the local chain tip, so it is not a
  mining-safe sync reference.
- Enabling node mining exposed a config-file permission trap. Local `.env` and
  `node.conf` were mode `0600` owned by the host user; after the entrypoint
  dropped privileges to `bdagStack`, `blockdag-node` could not read
  `/etc/bdagStack/node.conf`, logged `permission denied`, and silently fell back
  to the default `data/mainnet` datadir. That created
  `data/node/data/mainnet` and replayed from genesis instead of using the synced
  `data/node/mainnet` snapshot.
- A release fix now prepares a `bdagStack`-owned runtime copy of `node.conf`
  before dropping privileges and rewrites `--configfile` to that copy. The
  wrong fresh replay tree was parked locally as
  `data/node/data.wrong-fresh-20260620-001923`.
- With the config readable, direct RPC returned `isCurrent=true`,
  `getBlockCount=11964435`, and pool-style `getBlockTemplate` returned a
  usable mining template. The pool then subscribed to the node
  `blockTemplate` stream and reported a healthy backend.
- The ASIC inventory rows are not proof that hardware firmware has accepted the
  Stratum config. On this host the pool showed `pool_active_connections=0` and
  `pool_connections_total=0` after restart, while all four ASIC HTTP endpoints
  were reachable. Miner-control writes remain disabled locally with
  `BDAG_MINER_CONTROL_ENABLED=0`; previous unauthenticated ASIC config writes
  returned HTTP 401, so applying/restarting ASIC firmware still requires valid
  ASIC admin credentials or an enabled authenticated control path.

## Safety Outcome

- `node`, `postgres`, `dashboard`, and `pool` were left running after direct
  native RPC reported `isCurrent=true`, at least one active mainnet peer was
  present, and `getBlockTemplate` returned a usable result.
- `pool` was listening on `192.168.1.100:3334` with fresh node work, but no
  ASICs were connected. The status page state was therefore
  `Waiting for a Miner`, not `Mining`.
- Do not interpret dashboard inventory rows with `status=configured` as active
  miner connections. Require Stratum connection/share evidence before calling
  the pool mining.
