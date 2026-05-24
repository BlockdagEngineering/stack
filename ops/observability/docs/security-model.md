# Security Model

This document defines Phase 2 security assumptions for the parallel observability stack. The defaults are intentionally conservative because the mining dashboard and pool are live production controls.

## Default Exposure

All published observability ports must bind to localhost:

| Service | Default binding |
| --- | --- |
| Grafana | `127.0.0.1:3001` |
| Prometheus | `127.0.0.1:9091` |
| Alertmanager | `127.0.0.1:9094` |
| Loki | `127.0.0.1:3101` |

Exporter ports should not be published to the host. They should be reachable only on `bdag-observability-net`.

No service should bind to `0.0.0.0` without an explicit later deployment decision. LAN access, if needed, should be added behind a reverse proxy or tunnel with TLS, authentication, and source allowlisting.

## Authentication Assumptions

Grafana:

- Disable anonymous access.
- Use a generated admin password supplied through local environment or a Docker secret mechanism, not committed files.
- Keep file-provisioned dashboards and datasources read-only in the container where practical.
- Use viewer/editor/admin roles deliberately; do not share the admin credential for routine monitoring.

Prometheus, Loki, and Alertmanager:

- Treat localhost binding as the default access control for Phase 3 and staged run.
- If exposed beyond localhost later, put them behind authenticated reverse proxy access rather than exposing native UIs directly.

Existing dashboard:

- The existing dashboard may require a token for action endpoints.
- Observability must use only GET endpoints for `/api/status`, `/api/earnings`, and `/api/global`.
- Observability must never call `/api/action`, `/api/miners/scan`, `/api/miners/configure`, or `/api/miners/save-auth`.

## Secrets

Do not commit or ingest:

- `asic-pool/.env`
- wallet/private key material
- dashboard token files
- PostgreSQL superuser credentials
- ASIC credentials
- runtime files that contain credentials or action tokens

PostgreSQL metrics should use a dedicated read-only monitoring account when the project reaches a gated setup phase. If that account does not already exist, account creation is a separate explicit production-change task and is not part of Phase 2.

Grafana datasource provisioning should use stable internal datasource names and avoid embedding secret values in Git. Passwords should be passed through environment variables or mounted secret files excluded from log ingestion.

## Data Access Rules

Allowed by default:

- Read-only HTTP GET requests to existing dashboard APIs.
- Read-only TCP/HTTP blackbox probes.
- Direct Prometheus scrapes of node metrics already published on host ports `6061` and `6062`.
- Read-only Docker socket access only for metrics/log collection components that require it.
- Read-only host filesystem mounts for node_exporter.
- Read-only log mounts for Alloy/Promtail with explicit exclusion rules.

Not allowed by default:

- Dashboard POST action endpoints.
- ASIC write/configuration endpoints.
- Docker control operations from exporters.
- Mounting production database data directories into observability containers.
- Mounting wallet/private key files into observability containers.
- Scraping labels that expose full secrets, tokens, or private keys.

## Log Ingestion

Loki ingestion should prefer low-cardinality labels:

- `job`
- `container`
- `service`
- `source`
- `level` only if reliably parsed

Do not use miner IP, wallet address, full error text, block hash, request ID, or job ID as labels. Those belong in log bodies or metrics labels only when cardinality is bounded and operationally necessary.

Log collectors must exclude:

- `.env` files
- token files
- wallet/key files
- database files
- raw backup/snapshot directories
- any file path not needed for monitoring

## Network Isolation

Default network model:

- `bdag-observability-net` for all observability services.
- Production `pool-net` remains owned by root compose.
- Observability does not require production containers to join any new network.
- Host-published read-only ports are reached through `host.docker.internal` plus `host-gateway`.

Fallback model if host-gateway access fails:

- Attach only the minimum necessary observability service to existing `pool-net`.
- Do not edit production services.
- Keep all exposed observability ports localhost-only.

## Least Privilege By Component

| Component | Privilege target |
| --- | --- |
| Grafana | No Docker socket, no host mounts, no production secrets |
| Prometheus | Config read-only, data volume writable, no Docker socket unless absolutely needed |
| Loki | Config read-only, data volume writable, no production secrets |
| Alloy/Promtail | Read-only logs; no write access to production paths |
| node_exporter | Read-only host mounts; no command execution path |
| cAdvisor | Read-only Docker socket and cgroup mounts if used |
| postgres_exporter | Read-only DB account only |
| json_exporter | Read-only dashboard GET endpoints only |
| `bdag_exporter` | Read-only APIs/files only; no POSTs, no Docker exec, no repair commands |

## Security Acceptance Criteria

- Published services bind only to `127.0.0.1`.
- Grafana anonymous auth is disabled.
- No production secrets are committed or mounted into Grafana/Prometheus/Loki.
- Log ingestion excludes credential paths.
- Exporters do not perform writes.
- Any future LAN exposure is gated as a separate operations task.
