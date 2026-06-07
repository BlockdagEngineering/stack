# BlockDAG Pool Ops

This folder contains support tools for the BlockDAG ASIC pool stack. The
authoritative operator UI is the compose `dashboard` service exposed at
`http://127.0.0.1:8088` by default.

## Control Plane

Use compose service names as stable identities:

- `postgres`
- `node`
- `pool`
- `dashboard`

Concrete Docker container names are observations only. They may include the
compose project name and ordinal suffix, so tools must use compose commands or
label-based resolution before calling `docker inspect`, `docker logs`,
`docker top`, or `docker exec`.

## Status

```bash
curl -fsS http://127.0.0.1:8088/api/status
```

The compose dashboard owns routine status collection through its bounded shared
cache. `ops/status_sampler.py` remains a support utility for explicit repair and
diagnostic workflows, not a separate dashboard freshness dependency.

Boot repair preserves current node data by default: automatic clean restore is disabled unless an operator explicitly enables it for a controlled recovery.

## Support Services

Host user services and timers are support paths for guards, peer refresh,
IPFS/raw-datadir sidecars, and watchdog logic. Install or refresh them through:

```bash
bash ops/install-p2p-services.sh
```

Restart support services only when their files or environment changed:

```bash
systemctl --user restart bdag-watchdog.service
```

Restart the operator UI through compose:

```bash
docker compose up -d --no-build --pull never dashboard
```

## Runtime Updates

For live dashboard/watchdog runtime updates:

```bash
ops/deploy-live-runtime-update.sh --target /path/to/installed/runtime --mark-runtime-compose
```

The helper validates the source and target, copies only a whitelist, restarts
the compose dashboard, optionally restarts user support services, and verifies
the single `/api/status` endpoint.

## Miners

Managed ASIC miners are stored in `ops/runtime/miners.json`. The MAC address is
the persistent hardware identity. IP address, worker name, and pool-log labels
are observations only.

Default miner settings are derived from `.env`:

- Pool URL: `BDAG_POOL_URL`
- Worker/wallet: `MINING_ADDRESS`
- Pool password: `1234`
