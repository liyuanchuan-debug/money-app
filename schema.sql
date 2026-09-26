-- Wave Money: initial schema for Supabase PostgreSQL
-- Run this in the Supabase SQL Editor

CREATE TABLE IF NOT EXISTS items (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_items_created_at ON items (created_at DESC);

-- Seed sample rows only when the table is empty
INSERT INTO items (name)
SELECT v.name
FROM (VALUES
    ('Wave Starter'),
    ('Money Flow'),
    ('Tide Tracker')
) AS v(name)
WHERE NOT EXISTS (SELECT 1 FROM items LIMIT 1);

-- ---------------------------------------------------------------------------
-- 历史开奖号
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS records (
    id          BIGSERIAL PRIMARY KEY,
    number      INTEGER NOT NULL CHECK (number BETWEEN 1 AND 49),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_records_created_at ON records (created_at DESC);

-- ---------------------------------------------------------------------------
-- 用户配置（波动阈值、筹码模式等）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

INSERT INTO settings (key, value) VALUES
    ('small_max',  '10'),
    ('normal_max', '30'),
    ('bet_unit',   '10'),
    ('mode',       '"even"')
ON CONFLICT (key) DO NOTHING;
