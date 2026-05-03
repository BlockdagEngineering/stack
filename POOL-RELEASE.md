# Pool release tarball — how to run

Instructions for operators who receive a **pool-only** GitHub release of **pool-stack-docker** (tag pattern **`pool-v*`**, tarball **`pool-stack-docker-<tag>.tar.gz`**). That bundle ships **`dockerfile-pool-release`**, **`bin/`**, Compose files, and examples — **not** the chain snapshot file.

---

## Requirements

- **Docker** and **Docker Compose v2** (`docker compose`, not legacy `docker-compose`)
- Enough disk space for the snapshot (multi‑GB) and container images
- Shell access on the host where you unpack the tarball

---

## Steps

### 1. Unpack and go to the project root

```bash
tar -xzf pool-stack-docker-pool-v*.tar.gz
cd pool-stack-docker-pool-v*
```

Stay in this directory for every command below (the folder that contains **`docker-compose.yml`**, **`dockerfile-pool-release`**, **`bin/`**, **`.env.pool.example`**, **`node.conf.example`**).

---

### 2. Copy the example env and node config

```bash
cp .env.pool.example .env
cp node.conf.example node.conf
```

| File | Role |
|------|------|
| **`.env`** | Compose variables: ports, Postgres, pool tuning, **`NODE_RPC_USER`** / **`NODE_RPC_PASS`**. |
| **`node.conf`** | Mounted read-only into the node container; defines RPC auth as **`rpcuser`** / **`rpcpass`** (same names as in the example). |

Those RPC credentials **must match**:

- **`node.conf`**: `rpcuser` and `rpcpass`
- **`.env`**: `NODE_RPC_USER` and `NODE_RPC_PASS`

Also set a strong **`POSTGRES_PASSWORD`** in **`.env`** (and anything else your deployment needs: peers, pool fee, bind addresses).

---

### 3. Put the latest snapshot on disk

The tarball does not include **`.bdsnap`** files. Add your snapshot under a **`snapshots`** directory beside **`docker-compose.yml`**, replacing the old file when you refresh it.

```bash
mkdir -p snapshots
cp /path/to/your/latest.bdsnap snapshots/latest.bdsnap
```

Use any filename you like; the next step must point at it.

---

### 4. Point the build at that snapshot

Edit **`.env`** and set **`SNAPSHOT_PATH`** to a path **relative to this directory** (release layout uses **`BUILD_CONTEXT=.`**):

```dotenv
SNAPSHOT_PATH=snapshots/latest.bdsnap
```

If you skipped a snapshot (sync from genesis instead), leave the example default **`SNAPSHOT_PATH=docker/no-snapshot.marker`** — do **not** point at a missing `.bdsnap`.

Chain state for the node image is imported **during `docker compose build`** for the **`node`** service.

---

### 5. Build images

```bash
docker compose build
```

Whenever you **replace** the `.bdsnap` file or change **`SNAPSHOT_PATH`**, run **`docker compose build`** again (at minimum rebuild **`node`**).

---

### 6. Start the stack

Foreground (logs in the terminal):

```bash
docker compose up
```

Detached:

```bash
docker compose up -d
```

Useful follow-ups:

```bash
docker compose ps
docker compose logs -f node
docker compose logs -f pool
```

Typical ports from **`.env.pool.example`** include BDAG RPC **38131**, Stratum **3334**, pool HTTP API **8080**, Netdata **19999** — confirm with your **`.env`**.

---

## If you already ran the stack and then changed the snapshot

Compose keeps node chain data in a **named Docker volume** (`node-data`). An existing volume can **hide** newly imported snapshot data that exists only in rebuilt image layers.

After rebuilding **`node`** with a new snapshot, if the chain still looks wrong:

```bash
docker compose down
docker volume ls
docker volume rm <compose-project>_node-data
docker compose up -d
```

The volume name prefix matches Compose’s **project name** (often the directory name). Pick the volume ending in **`_node-data`**.

---

## Quick checklist

1. **`cp .env.pool.example .env`** and **`cp node.conf.example node.conf`**
2. **`rpcuser` / `rpcpass`** in **`node.conf`** match **`NODE_RPC_USER` / `NODE_RPC_PASS`** in **`.env`**
3. **`POSTGRES_PASSWORD`** set in **`.env`**
4. **`snapshots/<file>.bdsnap`** present and **`SNAPSHOT_PATH`** set accordingly (or marker file for no snapshot)
5. **`docker compose build`**
6. **`docker compose up`** or **`docker compose up -d`**
