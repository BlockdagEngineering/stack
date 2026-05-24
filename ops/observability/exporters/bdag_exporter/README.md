# BDAG Exporter

`bdag_exporter.py` is a read-only Prometheus exporter for BlockDAG-specific signals that are already available through the current local dashboard APIs.

It does not repair, restart, configure, or mutate anything.

Sources:

- `/api/status`
- `/api/earnings`
- `/api/global`

Default listen address:

```text
0.0.0.0:9108 inside the container
internal observability network only by default
```

Environment:

```text
BDAG_DASHBOARD_BASE_URL=http://host.docker.internal:8088
BDAG_EXPORTER_BIND=0.0.0.0
BDAG_EXPORTER_PORT=9108
BDAG_EXPORTER_TIMEOUT=8
BDAG_STATUS_CACHE_SECONDS=30
BDAG_EARNINGS_CACHE_SECONDS=300
BDAG_GLOBAL_CACHE_SECONDS=300
```
