-- Mining pool persistence for asic-pool (keep in sync with asic-pool/schema.sql).
-- Applied by scripts/init-pool-postgres.sh

CREATE TABLE IF NOT EXISTS miners (
    address TEXT PRIMARY KEY,
    joined_at TIMESTAMP DEFAULT NOW(),
    last_active TIMESTAMP
);

CREATE TABLE IF NOT EXISTS blocks (
    hash TEXT PRIMARY KEY,
    height BIGINT NOT NULL,
    reward NUMERIC(30,0) NOT NULL,
    fees NUMERIC(30,0) NOT NULL,
    status TEXT DEFAULT 'PENDING',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS credits (
    id SERIAL PRIMARY KEY,
    block_hash TEXT REFERENCES blocks(hash),
    miner_address TEXT NOT NULL,
    amount NUMERIC(30,0) NOT NULL,
    is_paid BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS credits_block_miner_unique
ON credits (block_hash, miner_address);

CREATE TABLE IF NOT EXISTS payouts (
    id SERIAL PRIMARY KEY,
    tx_hash TEXT UNIQUE NOT NULL,
    amount NUMERIC(30,0) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
