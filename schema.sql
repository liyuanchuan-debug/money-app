-- Wave Money: initial schema for Supabase PostgreSQL
-- Run this in the Supabase SQL Editor
--
-- 存量库升级请**不要**手工改 SQL，跑幂等迁移脚本（见 README「认证与权限」）：
--     backend\.venv\Scripts\python.exe backend\scripts\migrate_users_and_per_user_settings.py
--     backend\.venv\Scripts\python.exe backend\scripts\migrate_clean_model.py
-- 本文件是新库初始化用的一次性入口。

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
-- 用户配置（按用户隔离）
-- user_id = 0 是**保留的全局模板**（不是真实用户）：
--   新用户没有自己的行 → 读到的就是全局模板；匿名请求（AUTH_ENFORCED 关闭时）也读它。
-- 存量旧表（PRIMARY KEY (key)，没有 user_id 列）由
--   backend\scripts\migrate_users_and_per_user_settings.py 幂等收敛。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    user_id BIGINT NOT NULL DEFAULT 0,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

INSERT INTO settings (user_id, key, value) VALUES
    (0, 'small_max',     '10'),
    (0, 'normal_max',    '30'),
    (0, 'total_amount',  '50'),
    (0, 'amount_unit',   '5'),
    (0, 'mode',          '"even"'),
    (0, 'pick_count',    '6'),
    (0, 'odds',          '47'),
    (0, 'exclude_repeat_zodiac', 'false')
ON CONFLICT (user_id, key) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 用户（手机号 + 口令登录，三角色 RBAC）
-- 角色 / 状态一律英文枚举（落库禁止汉字）；汉字只在后端接口的 label 字段里。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    phone         TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'USER',
    status        TEXT NOT NULL DEFAULT 'PENDING',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at   TIMESTAMPTZ,
    last_login_at TIMESTAMPTZ,
    CONSTRAINT uq_users_phone UNIQUE (phone),
    CONSTRAINT ck_users_role CHECK (role IN ('USER', 'VIP', 'ADMIN')),
    CONSTRAINT ck_users_status CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'DISABLED'))
);

CREATE INDEX IF NOT EXISTS idx_users_status ON users (status, created_at DESC);

-- 首个管理员：**不要**在这里插入。跑脚本（幂等，且不会把口令打印出来）：
--     $env:ADMIN_PHONE='13800138000'; $env:ADMIN_PASSWORD='...'
--     backend\.venv\Scripts\python.exe backend\scripts\create_admin.py

-- ---------------------------------------------------------------------------
-- 六合彩开奖记录（与 backend/repository.py 的 SCHEMA_STATEMENTS 保持同步）
-- 数据模型：每期只存一个特码；正码 / 波色 / 五行 / 河合码 均已移除。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS draws (
    id             BIGSERIAL PRIMARY KEY,
    draw_date      DATE NOT NULL,
    period         INTEGER NOT NULL,
    special_number INTEGER NOT NULL CHECK (special_number BETWEEN 1 AND 49),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_draws_draw_date UNIQUE (draw_date)
);

CREATE INDEX IF NOT EXISTS idx_draws_draw_date ON draws (draw_date DESC);

-- 财富密码采用快照：每用户每目标期一条；开奖后回填 hit / 兑付 / 盈亏
-- stake_mode：SIMULATED（模拟买入，默认）| REAL（真实买入）| SKIPPED（未买）
-- 后期可标记真实买入与逐期修正；当前收益均为模拟试算
CREATE TABLE IF NOT EXISTS recommend_rounds (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL DEFAULT 0,
    period          INTEGER NOT NULL,
    base_period     INTEGER,
    draw_date       DATE,
    special_number  INTEGER CHECK (
        special_number IS NULL OR special_number BETWEEN 1 AND 49
    ),
    odds            DOUBLE PRECISION NOT NULL DEFAULT 47,
    cost            DOUBLE PRECISION NOT NULL DEFAULT 0,
    payout          DOUBLE PRECISION NOT NULL DEFAULT 0,
    profit          DOUBLE PRECISION NOT NULL DEFAULT 0,
    hit             BOOLEAN,
    mode            TEXT NOT NULL DEFAULT '',
    picks_json      TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'PENDING',
    stake_mode      TEXT NOT NULL DEFAULT 'SIMULATED',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at      TIMESTAMPTZ,
    CONSTRAINT uq_recommend_rounds_user_period UNIQUE (user_id, period),
    CONSTRAINT ck_recommend_rounds_status CHECK (status IN ('PENDING', 'SETTLED')),
    CONSTRAINT ck_recommend_rounds_stake_mode
        CHECK (stake_mode IN ('SIMULATED', 'REAL', 'SKIPPED'))
);

CREATE INDEX IF NOT EXISTS idx_recommend_rounds_user_period
    ON recommend_rounds (user_id, period DESC);
CREATE INDEX IF NOT EXISTS idx_recommend_rounds_status
    ON recommend_rounds (status, period DESC);

-- 开奖纠正审计（英文 action 枚举）
CREATE TABLE IF NOT EXISTS draw_corrections (
    id                  BIGSERIAL PRIMARY KEY,
    draw_id             BIGINT NOT NULL,
    period              INTEGER NOT NULL,
    old_special_number  INTEGER NOT NULL CHECK (old_special_number BETWEEN 1 AND 49),
    new_special_number  INTEGER NOT NULL CHECK (new_special_number BETWEEN 1 AND 49),
    old_draw_date       DATE,
    new_draw_date       DATE,
    actor_user_id       BIGINT,
    action              TEXT NOT NULL DEFAULT 'CORRECT',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_draw_corrections_action CHECK (action IN ('CORRECT'))
);

CREATE INDEX IF NOT EXISTS idx_draw_corrections_period
    ON draw_corrections (period DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_draw_corrections_draw_id
    ON draw_corrections (draw_id, created_at DESC);

-- 农历年生肖边界表（生肖随农历年轮转，01 号所属生肖每年不同）
CREATE TABLE IF NOT EXISTS zodiac_years (
    lunar_year   INTEGER PRIMARY KEY,
    starts_on    DATE NOT NULL,
    animal_of_01 TEXT NOT NULL
);

-- 农历年边界种子：2025 乙巳蛇年（春节 2025-01-29）、2026 丙午马年（春节 2026-02-17）
INSERT INTO zodiac_years (lunar_year, starts_on, animal_of_01) VALUES
    (2025, DATE '2025-01-29', 'SNAKE'),
    (2026, DATE '2026-02-17', 'HORSE')
ON CONFLICT (lunar_year) DO NOTHING;

-- 49 号码关联表：派生表，永不手工填报。
-- 行数据由 services.mark_six.zodiac_table_for_year() 生成，请运行：
--     backend\scripts\migrate_clean_model.py
-- （或在应用启动时由 PostgresStore.ensure_schema 自动同步）
CREATE TABLE IF NOT EXISTS zodiac_numbers (
    lunar_year INTEGER NOT NULL REFERENCES zodiac_years (lunar_year) ON DELETE CASCADE,
    number     SMALLINT NOT NULL CHECK (number BETWEEN 1 AND 49),
    zodiac     TEXT NOT NULL,
    PRIMARY KEY (lunar_year, number)
);

