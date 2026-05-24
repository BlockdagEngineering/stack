# Logs Pipeline

The observability stack uses Loki with Grafana Alloy. The pipeline is read-only and does not write to, restart, or reconfigure production services.

## Sources

Configured in `alloy/config.alloy`:

- Docker logs from selected production containers: `asic-pool`, `bdag-miner-node-1`, `bdag-miner-node-2`, `rpc-failover`, and `pool-db`.
- Selected low-volume runtime logs:
  - `watchdog.log`
  - `cpu-thermal-guard.log`
  - `hourly-chain-snapshot.log`
  - `efficiency-events.jsonl`

Excluded by design:

- `.env` files
- wallet/private-key material
- backups and restored data trees
- token/password/auth files
- arbitrary filesystem log discovery

## Labels

The pipeline keeps labels low-cardinality:

- `job`
- `container`
- `service`
- `level`

It does not label by miner IP, wallet address, request path, or full log message.

## Secret Redaction

The Docker and runtime log pipelines drop lines matching common sensitive terms:

- `token`
- `password`
- `private key`
- `secret`

This is a safety net, not a substitute for excluding sensitive paths.

## Performance Filtering

Docker logs from production containers drop INFO/DEBUG lines by default. This keeps Loki focused on warning/error incident context and avoids spending CPU and disk on high-volume node import chatter. Runtime ingestion is limited to the watchdog, thermal guard, hourly chain snapshot, and efficiency-event files.

## Retention

Loki retention is configured in `loki/loki.yml` for `744h`, which is approximately 31 days. The architecture budget assumes roughly 20 GB for Loki data. Disk use must be checked during staged run because real log volume depends on node and pool verbosity.

## Useful LogQL

Pool and node errors:

```logql
{job="docker"} |= "ERROR"
```

Watchdog and repair events:

```logql
{job="bdag-runtime"} |~ "(repair|miner_down|share_stall|failed|critical)"
```

Pool share activity:

```logql
{container="asic-pool"} |~ "(share|submit|accepted|stale)"
```

Template and sync issues:

```logql
{container=~"bdag-miner-node-1|bdag-miner-node-2"} |~ "(template|sync|import|peer|p2p|stream)"
```

Thermal guard:

```logql
{job="bdag-runtime"} |~ "(thermal|temperature|cpu)"
```

## Operational Notes

- Loki is for incident context, not high-cardinality metrics.
- The old dashboard remains the control surface for repairs.
- If Loki causes disk pressure, stop only the observability compose project and keep production mining untouched.
