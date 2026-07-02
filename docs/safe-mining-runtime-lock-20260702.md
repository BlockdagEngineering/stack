# Safe Mining Runtime Lock - 2026-07-02

This document records the exact stack state deployed to Local, Office, and Josh on
2026-07-02 after the reproducible safe-mining rebuild.

## Source Commits

| Repo | Branch | Commit |
| --- | --- | --- |
| stack | integration/blocklist-hardening-on-redis-dash-0.0.2 | 50fe21baeb72a9a36bc08fa074893aa5c4e964bf |
| blockdag-corechain | fix/consensus-evm-state-guard | 60828492b11df6894ef1390bed1799fb6ccdee11 |
| pool | blocklist-consensus-hardening | a839d5ccd891c1285a2a42955f4c2d5772d2c058 |
| redis-dash | blocklist-consensus-hardening | 3136b17ba3208d3a6f395f81605cc3b68afa183e |

## Deployed Image IDs

All three systems use these same image IDs. Josh also has project-specific tag
aliases such as `blockdag-stack-v110-node:latest`; those aliases point to the
same IDs below.

| Service | Image ID |
| --- | --- |
| node | sha256:295a03e1cf73e2375702b2be1d3bfc9d95dc48a963db99feae6aa03797c7e136 |
| pool | sha256:b69716bf2340049622dadb57adfa88c541f8c0549a4587a1e0bccc7d93a5fb53 |
| dashboard | sha256:355e46118980c1d9a3e9b8c2206d249f437c6115a3840907f25eab6658694a0a |
| watchdog | sha256:1d43fd83a2950a2b463cb71d20ca2734a755b756f7326783a238b18c5c7485bb |
| status-sampler | sha256:ef7a0608ef5b57da7296ed87199482f07f2eeeda3fadaaabe07efc442a5d4a26 |
| sentinel | sha256:85443bd701e9ae65548e2155b4dca7d3167a9e8bcd1c498354910e93d4af9306 |

Image labels record the source commits with:

- `org.blockdag.source.stack`
- `org.blockdag.source.corechain`
- `org.blockdag.source.pool`
- `org.blockdag.source.redis-dash`
- `org.blockdag.runtime.manifest`

## Binary Hashes

| Binary | SHA-256 |
| --- | --- |
| `/usr/local/bin/blockdag-node` | eadaadf2db988a1db2cd101e3c72b9c8f3cc62433f2a57bc8f1c1dd7179bd467 |
| `/usr/local/bin/nodeworker` | 6dc0731596e2e1c3a4e6ad67bfe2171e0ab0e678b38375051170545d4d88d5b4 |
| `/usr/local/bin/mining-pool` | 0d7966fea23717ad2e899ecea5e3ef382c34cdde332dad474364595360aecf1c |
| `/usr/local/bin/dashboard-api` | b094592bfa61c529115ec6976acf1b946f49622acd8ddd1d0e64c882c328e393 |
| `/usr/local/bin/dashboard` | f0a91ee534659643974066a56a7edcaeff4d17f51931638500ef1a25e2e0376c |

The committed `checksums.txt` in this stack commit records these build outputs.

## Dashboard Lock

The preferred local dashboard look was retained. The rendered CSS block hash on
Local, Office, and Josh is:

`fd9527e35f799cce525e9490ba3457d1f5e024b6c750d2b7387b666eac40e93c`

The dashboard image uses redis-dash commit:

`3136b17ba3208d3a6f395f81605cc3b68afa183e`

## Runtime Configuration

The stack runtime files and binaries were synchronized across all three systems
from the same local runtime folder, excluding host-owned mutable state:

- `.env`
- `node.conf`
- `data/`
- `node-data/`
- `ops/runtime/`
- local source checkout caches

All three systems now use:

`NODE_DATA_DIR=./data/node`

Office and Josh previously used `NODE_DATA_DIR=./node-data`; their `node-data`
directories were moved to `data/node` on the same filesystem before restart.

Host-specific values that must remain outside the repo:

- RPC credentials and other secrets in `.env`
- host LAN/ASIC routing settings
- node identity and chain data under `data/node`
- host-specific compose project tag aliases, especially Josh
- runtime savepoints under `ops/runtime/savepoints`

## Image Bundle

The exact image bundle copied to Office and Josh was:

`safe-mining-runtime-20260702-images.tar.zst`

SHA-256:

`2ad5797623114ea8ceeda180571e49c3f4bdff4a7f94427763a530781cd5e51b`

## Verification Snapshot

Post-deploy checks on 2026-07-02:

- Local, Office, and Josh had matching node, pool, and dashboard image IDs.
- Local, Office, and Josh had matching node, nodeworker, pool, and dashboard binary hashes.
- Local, Office, and Josh had matching dashboard CSS hash.
- Local, Office, and Josh reported `getTemplateHealth.reason_code=ok`.
- Local, Office, and Josh reported `mineable_now=true`, `submit_ready=true`, and `template_usable=true`.
- EVM block `12957502` matched on all three:
  - hash `0xa83fc2c68c43cb7d71ca76e5589610549b8dc9f1c916f4c6bc6305aa2792695a`
  - state root `0x310390ab577d10c5f34a1e19608ce85e2c29f8e1d1e26f65ce7b946c5982c0a4`
- Pools on all three systems showed recent accepted block-related events after restart.

## Savepoints

Before the runtime replacement, image/config savepoints were created:

| System | Savepoint |
| --- | --- |
| Local | `/home/jeremy/github/BlockdagEngineering/stack-runtime-stack-v1.1.0/ops/runtime/savepoints/20260702-050900-pre-reproducible-stack-lock` |
| Office | `/home/jeremy/github/BlockdagEngineering/stack/ops/runtime/savepoints/20260702-052300-pre-exact-stack-lock` |
| Josh | `/mnt/bdag-ssd/blockdag-stack-v1.1.0/ops/runtime/savepoints/20260702-052300-pre-exact-stack-lock` |
