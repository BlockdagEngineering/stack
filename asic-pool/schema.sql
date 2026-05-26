-- Miners table (optional, for stats/settings)
CREATE TABLE IF NOT EXISTS miners (
    address TEXT PRIMARY KEY,
    joined_at TIMESTAMP DEFAULT NOW(),
    last_active TIMESTAMP
);

-- Blocks table (Tracks maturity)
CREATE TABLE IF NOT EXISTS blocks (
    hash TEXT PRIMARY KEY,
    height BIGINT NOT NULL,
    reward NUMERIC(30,0) NOT NULL, -- Total Reward
    fees NUMERIC(30,0) NOT NULL,   -- Pool Fee Amount
    status TEXT DEFAULT 'PENDING', -- PENDING, ORPHANED, MATURE, PAID
    created_at TIMESTAMP DEFAULT NOW()
);

-- Durable history of solved block candidates submitted to backend nodes.
-- The node_block_hash is the chain-facing source of truth returned by the
-- node after submitBlockHeader accepts a candidate.
CREATE TABLE IF NOT EXISTS block_submissions (
    id SERIAL PRIMARY KEY,
    candidate_hash TEXT NOT NULL,
    node_block_hash TEXT,
    height BIGINT,
    backend TEXT,
    template_seq BIGINT,
    accepted BOOLEAN NOT NULL DEFAULT FALSE,
    outcome TEXT NOT NULL,
    message TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS block_submissions_created_at_idx
    ON block_submissions (created_at);

CREATE INDEX IF NOT EXISTS block_submissions_outcome_created_idx
    ON block_submissions (outcome, created_at);

-- Credits table (Who gets what for which block)
CREATE TABLE IF NOT EXISTS credits (
    id SERIAL PRIMARY KEY,
    block_hash TEXT REFERENCES blocks(hash),
    miner_address TEXT NOT NULL,
    amount NUMERIC(30,0) NOT NULL,
    is_paid BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Payouts (History)
CREATE TABLE IF NOT EXISTS payouts (
    id SERIAL PRIMARY KEY,
    tx_hash TEXT UNIQUE NOT NULL,
    amount NUMERIC(30,0) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_blocks_status_height
    ON blocks(status, height);

CREATE INDEX IF NOT EXISTS idx_credits_unpaid_block_hash
    ON credits(block_hash)
    WHERE is_paid = FALSE;
