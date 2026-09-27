"""数据访问层：优先 Supabase Postgres，未配置 DATABASE_URL 时退化为内存存储。

内存存储仅用于本地预览与联调，进程重启即丢失。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from typing import Any

from db import init_pool
from services.auth import (
    GLOBAL_SETTINGS_USER_ID,
    ROLE_ADMIN,
    ROLE_USER,
    ROLES,
    STATUS_APPROVED,
    STATUSES,
    STATUS_PENDING,
)
from services.lottery import (
    DEFAULT_SETTINGS,
    clamp_settings,
    merge_settings_patch,
    resolve_trend_bias,
    with_derived_settings,
)
from services.mark_six import ZODIAC_YEARS, today_local, zodiac_table_for_year
from services.pnl import (
    STAKE_MODE_SIMULATED,
    STATUS_PENDING,
    apply_settlement,
    money,
    normalize_stake_mode,
)

# 开奖纠正动作（落库英文枚举）
DRAW_ACTION_CORRECT = "CORRECT"
DRAW_ACTION_LABELS = {DRAW_ACTION_CORRECT: "纠正开奖"}

DRAW_COLUMNS = "id, draw_date, period, special_number, created_at"

RECOMMEND_ROUND_COLUMNS = (
    "id, user_id, period, base_period, draw_date, special_number, "
    "odds, cost, payout, profit, hit, mode, picks_json, status, "
    "stake_mode, created_at, settled_at"
)

# 用户表对外（可安全返回）的列；口令字段绝不出现在这里
USER_COLUMNS = (
    "id, phone, role, status, created_at, approved_at, last_login_at"
)
# 仅登录校验需要的列（含口令散列），只在 get_user_credentials 里使用
USER_CREDENTIAL_COLUMNS = "id, phone, role, status, password_hash, password_salt"

# DDL 里的 CHECK 约束由常量拼出来，避免枚举值在多处手写导致漂移
_ROLE_CHECK_SQL = ", ".join(f"'{role}'" for role in ROLES)
_STATUS_CHECK_SQL = ", ".join(f"'{status}'" for status in STATUSES)

# 注意：``records``（快捷录入草稿表）已彻底废弃，``draws`` 是特码的唯一事实来源。
# 遗留的 records 表由 backend/scripts/migrate_records_to_draws.py 迁移并删除，
# 这里不再声明它的 DDL（否则 ensure_schema 会把已删除的表重新建出来）。
SCHEMA_STATEMENTS = (
    # ---------------------------------------------------------------------- #
    # 用户配置：**按用户隔离**，``user_id = 0`` 是保留的「全局模板」哨兵值。
    # 新用户没有任何自己的行 → 读到的就是全局模板（全局默认值）。
    # ---------------------------------------------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS settings (
        user_id BIGINT NOT NULL DEFAULT 0,
        key     TEXT NOT NULL,
        value   TEXT NOT NULL,
        PRIMARY KEY (user_id, key)
    )
    """,
    # 幂等收敛存量旧表（旧形状：``PRIMARY KEY (key)``，没有 user_id 列）。
    # 这些都是 IF EXISTS / DO 块，可重复执行；同时由
    # backend/scripts/migrate_users_and_per_user_settings.py 显式跑一遍。
    "ALTER TABLE settings ADD COLUMN IF NOT EXISTS user_id BIGINT NOT NULL DEFAULT 0",
    """
    DO $$
    DECLARE
        pk_columns TEXT;
    BEGIN
        SELECT string_agg(a.attname, ',' ORDER BY array_position(c.conkey, a.attnum))
          INTO pk_columns
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY (c.conkey)
         WHERE t.relname = 'settings' AND c.contype = 'p';

        IF pk_columns IS NULL THEN
            ALTER TABLE settings ADD PRIMARY KEY (user_id, key);
        ELSIF pk_columns <> 'user_id,key' THEN
            ALTER TABLE settings DROP CONSTRAINT settings_pkey;
            ALTER TABLE settings ADD PRIMARY KEY (user_id, key);
        END IF;
    END
    $$;
    """,
    # ---------------------------------------------------------------------- #
    # 用户表（手机号 + 口令登录，三角色 RBAC）
    # 角色 / 状态一律英文枚举（项目铁律：落库禁止汉字）
    # ---------------------------------------------------------------------- #
    f"""
    CREATE TABLE IF NOT EXISTS users (
        id            BIGSERIAL PRIMARY KEY,
        phone         TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        password_salt TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT '{ROLE_USER}',
        status        TEXT NOT NULL DEFAULT '{STATUS_PENDING}',
        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        approved_at   TIMESTAMPTZ,
        last_login_at TIMESTAMPTZ,
        CONSTRAINT uq_users_phone UNIQUE (phone),
        CONSTRAINT ck_users_role CHECK (role IN ({_ROLE_CHECK_SQL})),
        CONSTRAINT ck_users_status CHECK (status IN ({_STATUS_CHECK_SQL}))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_users_status ON users (status, created_at DESC)",
    # ---------------------------------------------------------------------- #
    # 六合彩开奖记录（schema.sql 与本处保持同步）
    # 数据模型：每期只存一个特码；正码 / 波色 / 五行 / 河合码 均已移除。
    # ---------------------------------------------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS draws (
        id             BIGSERIAL PRIMARY KEY,
        draw_date      DATE NOT NULL,
        period         INTEGER NOT NULL,
        special_number INTEGER NOT NULL CHECK (special_number BETWEEN 1 AND 49),
        created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_draws_draw_date UNIQUE (draw_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_draws_draw_date ON draws (draw_date DESC)",
    # 农历年生肖边界表（生肖随农历年轮转，01 号所属生肖每年不同）
    """
    CREATE TABLE IF NOT EXISTS zodiac_years (
        lunar_year   INTEGER PRIMARY KEY,
        starts_on    DATE NOT NULL,
        animal_of_01 TEXT NOT NULL
    )
    """,
    # 农历年边界种子：2025 乙巳蛇年（春节 2025-01-29）、2026 丙午马年（春节 2026-02-17）
    """
    INSERT INTO zodiac_years (lunar_year, starts_on, animal_of_01) VALUES
        (2025, DATE '2025-01-29', 'SNAKE'),
        (2026, DATE '2026-02-17', 'HORSE')
    ON CONFLICT (lunar_year) DO NOTHING
    """,
    # 49 号码关联表：派生表，永不手工填报。
    # 行数据由 services.mark_six.zodiac_table_for_year() 生成
    # （PostgresStore.ensure_schema / backend/scripts/migrate_clean_model.py）。
    """
    CREATE TABLE IF NOT EXISTS zodiac_numbers (
        lunar_year INTEGER NOT NULL REFERENCES zodiac_years (lunar_year) ON DELETE CASCADE,
        number     SMALLINT NOT NULL CHECK (number BETWEEN 1 AND 49),
        zodiac     TEXT NOT NULL,
        PRIMARY KEY (lunar_year, number)
    )
    """,
    # ---------------------------------------------------------------------- #
    # 财富密码采用快照：每用户每目标期一条；开奖后回填 hit / 兑付 / 盈亏
    # status 英文枚举 PENDING|SETTLED；stake_mode 英文枚举 SIMULATED|REAL|SKIPPED
    # （默认 SIMULATED=模拟买入；后期可标真实买入 / 未买并逐期修正）
    # 金额与赔率为数字，禁止汉字落库
    # ---------------------------------------------------------------------- #
    """
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
    )
    """,
    # 存量库幂等补列（新库 CREATE 已含；重复执行安全）
    "ALTER TABLE recommend_rounds ADD COLUMN IF NOT EXISTS "
    "stake_mode TEXT NOT NULL DEFAULT 'SIMULATED'",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_recommend_rounds_stake_mode'
        ) THEN
            ALTER TABLE recommend_rounds
                ADD CONSTRAINT ck_recommend_rounds_stake_mode
                CHECK (stake_mode IN ('SIMULATED', 'REAL', 'SKIPPED'));
        END IF;
    END $$
    """,
    "CREATE INDEX IF NOT EXISTS idx_recommend_rounds_user_period "
    "ON recommend_rounds (user_id, period DESC)",
    "CREATE INDEX IF NOT EXISTS idx_recommend_rounds_status "
    "ON recommend_rounds (status, period DESC)",
    # ---------------------------------------------------------------------- #
    # 开奖纠正审计：记录修正前后特码（英文 action 枚举）
    # ---------------------------------------------------------------------- #
    """
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_draw_corrections_period "
    "ON draw_corrections (period DESC, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_draw_corrections_draw_id "
    "ON draw_corrections (draw_id, created_at DESC)",
)


class Store(ABC):
    backend: str

    # ----------------------------------------------------------------- #
    # 六合彩开奖记录（唯一事实来源：每期只有特码）
    # ----------------------------------------------------------------- #
    @abstractmethod
    async def import_draws(
        self, draws: list[dict[str, Any]], replace_existing: bool = True
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def list_draws(
        self, limit: int | None = None, offset: int = 0
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_latest_draw(self) -> dict[str, Any] | None: ...

    @abstractmethod
    async def get_draw(self, draw_id: int) -> dict[str, Any] | None: ...

    @abstractmethod
    async def delete_draw(self, draw_id: int) -> int: ...

    @abstractmethod
    async def clear_draws(self) -> int: ...

    @abstractmethod
    async def suggest_next_period(self) -> int:
        """建议的下一期期号：max(period) + 1，表为空时返回 1。"""
        ...

    @abstractmethod
    async def add_draw(
        self,
        special_number: int,
        period: int | None = None,
        draw_date: date | None = None,
    ) -> dict[str, Any]:
        """快捷录入一期开奖（自动补期号 / 日期，同一条覆盖）。

        - ``period`` 为空 → max(period) + 1；``draw_date`` 为空 → 今天（UTC+8）。
        - 先按 ``draw_date`` 找同一条（同一天的重复提交 = 修正），
          没找到再按显式给出的 ``period`` 找，都没有才 INSERT。命中即 UPDATE，
          因此用户改错号码 / 期号不会产生重复行。
        - 唯一约束只有 ``draws.draw_date``；为防歧义，本方法额外保证期号不重复，
          冲突时抛 ``ValueError``（路由转 400）。
        """
        ...

    @abstractmethod
    async def correct_draw(
        self,
        draw_id: int,
        special_number: int,
        *,
        draw_date: date | None = None,
        actor_user_id: int | None = None,
    ) -> dict[str, Any]:
        """纠正已保存开奖的特码（可顺带改日期），并重算该期采用快照盈亏。

        返回体含 ``draw``、新旧特码、``resettled_rounds``、``correction_id``。
        期号不存在 / 特码越界 → ``ValueError``。
        """
        ...

    # ----------------------------------------------------------------- #
    # 49 号码关联表（派生数据：来自生肖表推导，永不手工填报）
    # ----------------------------------------------------------------- #
    @abstractmethod
    async def list_zodiac_numbers(self, lunar_year: int) -> list[dict[str, Any]]: ...

    # ----------------------------------------------------------------- #
    # 用户（认证 / RBAC）
    # ----------------------------------------------------------------- #
    @abstractmethod
    async def create_user(
        self,
        phone: str,
        password_hash: str,
        password_salt: str,
        role: str = ROLE_USER,
        status: str = STATUS_PENDING,
    ) -> dict[str, Any] | None:
        """新建用户；手机号已存在时返回 ``None``（不抛错、不覆盖）。

        返回**对外可安全返回**的用户行（不含口令字段）。
        """
        ...

    @abstractmethod
    async def get_user_by_phone(self, phone: str) -> dict[str, Any] | None:
        """按手机号取用户（对外字段，不含口令）。"""
        ...

    @abstractmethod
    async def get_user_credentials(self, phone: str) -> dict[str, Any] | None:
        """按手机号取登录校验所需字段（含 ``password_hash`` / ``password_salt``）。

        仅 ``POST /api/auth/login`` 使用；路由必须把结果转成 ``public_user()`` 再返回。
        """
        ...

    @abstractmethod
    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        """按主键取用户（对外字段，不含口令）。"""
        ...

    @abstractmethod
    async def list_users(self, status: str | None = None) -> list[dict[str, Any]]:
        """用户列表（最新注册在前），可选按 ``status`` 过滤。"""
        ...

    @abstractmethod
    async def update_user_status(
        self, phone: str, status: str
    ) -> dict[str, Any] | None:
        """改审批状态；转 ``APPROVED`` 时写 ``approved_at = NOW()``，否则清空。"""
        ...

    @abstractmethod
    async def update_user_role(self, phone: str, role: str) -> dict[str, Any] | None:
        """改角色。"""
        ...

    @abstractmethod
    async def update_user_password(
        self, phone: str, password_hash: str, password_salt: str
    ) -> dict[str, Any] | None:
        """重置口令（管理员自助脚本用）。"""
        ...

    @abstractmethod
    async def touch_user_login(self, phone: str) -> None:
        """记录最近登录时间。"""
        ...

    @abstractmethod
    async def count_approved_admins(self) -> int:
        """已审批的管理员数量 —— 用于「最后一个管理员」防锁死判断。"""
        ...

    # ----------------------------------------------------------------- #
    # 用户配置（按用户隔离）
    # ----------------------------------------------------------------- #
    @abstractmethod
    async def get_settings(self, user_id: int | None = None) -> dict[str, Any]:
        """读取配置。

        - ``user_id is None``（匿名 / 灰度期）→ 全局模板（``user_id = 0``）；
        - 指定用户 → 全局模板打底，再叠加该用户自己的行（新用户 = 全局默认值）。

        存量旧行（只有 ``bet_unit``、没有 ``total_amount``）由 ``clamp_settings``
        回退成 ``bet_unit × pick_count``，不报错、不把预算变成 0。
        """
        ...

    @abstractmethod
    async def update_settings(
        self, patch: dict[str, Any], user_id: int | None = None
    ) -> dict[str, Any]:
        """写入配置，**只影响 ``user_id`` 自己的行**，全局模板不受影响。"""
        ...

    # ----------------------------------------------------------------- #
    # 财富密码采用快照（每用户每目标期一条）
    # ----------------------------------------------------------------- #
    @abstractmethod
    async def upsert_recommend_round(
        self, round_row: dict[str, Any]
    ) -> dict[str, Any]:
        """写入/覆盖一期采用快照（同一 user_id + period 唯一）。"""
        ...

    @abstractmethod
    async def list_recommend_rounds(
        self,
        user_id: int | None = None,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """列出用户的采用快照（期号倒序）。``user_id is None`` → 全局模板 0。"""
        ...

    @abstractmethod
    async def settle_recommend_rounds_for_draw(
        self,
        period: int,
        special_number: int,
        draw_date: date | None = None,
    ) -> int:
        """开奖入库后：按期号回填所有用户该期快照的 hit / 兑付 / 盈亏。返回结算条数。"""
        ...

    async def export_data(self, user_id: int | None = None) -> dict[str, Any]:
        """导出开奖总表 + 设置。

        决策：``records`` 已废弃，导出**只含 draws**（version 2）。
        旧版导出文件里的 ``records`` 键不再被 /api/import 接受——它只存了
        号码与写入时间，无法可靠还原开奖日期，强行迁移会污染唯一事实来源。

        ``settings`` 导出的是 ``user_id`` 视角的**有效配置**，
        并附带只读派生字段（``bet_unit`` / ``big_min``），与 ``GET /api/settings`` 同形，
        保证老调用方在导出里仍能读到 ``bet_unit``。
        """
        draws = await self.list_draws()  # 最新在前
        return {
            "version": 2,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "draws": [
                {
                    "draw_date": draw["draw_date"].isoformat()
                    if isinstance(draw["draw_date"], (date, datetime))
                    else draw["draw_date"],
                    "period": draw["period"],
                    "special_number": draw["special_number"],
                }
                for draw in reversed(draws)  # 按日期正序导出
            ],
            "settings": with_derived_settings(await self.get_settings(user_id)),
        }

    @abstractmethod
    async def import_data(
        self, payload: dict[str, Any], replace: bool, user_id: int | None = None
    ) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# 用户行白名单映射（两种存储共用）
# --------------------------------------------------------------------------- #
def public_user_row(row: dict[str, Any]) -> dict[str, Any]:
    """只保留对外字段，绝不带出 ``password_hash`` / ``password_salt``。"""
    return {column.strip(): row.get(column.strip()) for column in USER_COLUMNS.split(",")}


# --------------------------------------------------------------------------- #
# 开奖记录载荷归一化（两种存储共用）
# --------------------------------------------------------------------------- #
def _as_date(value: Any) -> date:
    """把 date / datetime / ISO 字符串统一成 date。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def next_period_from_draws(draws: list[dict[str, Any]]) -> int:
    """下一期期号 = max(period) + 1；表为空时为 1。"""
    periods = [int(d["period"]) for d in draws if d.get("period") is not None]
    return max(periods) + 1 if periods else 1


def plan_quick_draw(
    rows: list[dict[str, Any]],
    special_number: Any,
    period: int | None,
    draw_date: date | datetime | str | None,
) -> dict[str, Any]:
    """把「快捷录入一期」解析成确定性的写入计划（Postgres / 内存两种存储共用）。

    语义（与用户约定一致，两种存储行为完全一致）：

    - ``draw_date`` 缺省 → 今天（UTC+8）；``period`` 缺省 → max(period) + 1；
    - **日期是主键**：同一天重复提交视为「修正」，只更新那一行，且不打乱它的期号
      （用户连点两次保存不会变成两期，也不会把期号 +1）；
    - 日期没命中、但显式给了期号且该期号已存在 → 同样视为修正（更新那一行）；
      此时若调用方**没有**指定日期，保留那一期原有的日期（不把历史开奖搬成今天）；
    - 都没命中 → 新增；
    - 期号在此强制唯一（DB 只对 ``draw_date`` 建了唯一索引），否则「按日期 /
      按期号修正」会产生歧义；冲突时抛 ``ValueError``（路由转 400）。
    """
    special = int(special_number)
    if not 1 <= special <= 49:
        raise ValueError(f"特码越界: {special}")

    requested_date = None if draw_date is None else _as_date(draw_date)
    resolved_date = requested_date if requested_date is not None else today_local()
    explicit_period = None if period is None else int(period)

    date_row = next(
        (r for r in rows if _as_date(r["draw_date"]) == resolved_date), None
    )
    if date_row is not None:
        target = date_row
        resolved_period = (
            explicit_period if explicit_period is not None else int(date_row["period"])
        )
    elif explicit_period is not None:
        target = next((r for r in rows if int(r["period"]) == explicit_period), None)
        resolved_period = explicit_period
        if target is not None and requested_date is None:
            # 只改号码、没给日期 → 保留该期原有日期，不把历史开奖搬成今天
            resolved_date = _as_date(target["draw_date"])
    else:
        target = None
        resolved_period = next_period_from_draws(rows)

    if resolved_period < 1:
        raise ValueError(f"期号越界: {resolved_period}")

    target_id = int(target["id"]) if target is not None else None
    for row in rows:
        if target_id is not None and int(row["id"]) == target_id:
            continue
        if _as_date(row["draw_date"]) == resolved_date:
            raise ValueError(
                f"日期 {resolved_date} 已被第 {row['period']} 期占用，无法改期"
            )
        if int(row["period"]) == resolved_period:
            raise ValueError(
                f"期号 {resolved_period} 已被 {_as_date(row['draw_date'])} 占用"
            )

    return {
        "action": "update" if target is not None else "insert",
        "target_id": target_id,
        "draw_date": resolved_date,
        "period": resolved_period,
        "special_number": special,
    }


def normalize_draw_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """把外部传入的一期开奖整理成落库结构（日期/枚举码统一，只保留特码）。"""
    raw_date = payload["draw_date"]
    if isinstance(raw_date, datetime):
        draw_date = raw_date.date()
    elif isinstance(raw_date, date):
        draw_date = raw_date
    else:
        draw_date = date.fromisoformat(str(raw_date))

    raw_special = payload.get("special_number")
    if raw_special is None:
        raise ValueError("缺少特码 special_number")
    special_number = int(raw_special)
    if not 1 <= special_number <= 49:
        raise ValueError(f"特码越界: {special_number}")

    return {
        "draw_date": draw_date,
        "period": int(payload["period"]),
        "special_number": special_number,
    }


def _decode_picks(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def row_to_recommend_round(row: dict[str, Any] | Any) -> dict[str, Any]:
    """把存储行（含 picks_json）规整成服务层可用的 dict。"""
    data = dict(row)
    data["picks"] = _decode_picks(data.pop("picks_json", data.get("picks")))
    data["odds"] = money(data.get("odds", 0))
    data["cost"] = money(data.get("cost", 0))
    data["payout"] = money(data.get("payout", 0))
    data["profit"] = money(data.get("profit", 0))
    data["stake_mode"] = normalize_stake_mode(data.get("stake_mode"))
    return data


# --------------------------------------------------------------------------- #
# Postgres
# --------------------------------------------------------------------------- #
class PostgresStore(Store):
    backend = "postgres"

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def ensure_schema(self) -> None:
        async with self._pool.acquire() as conn:
            for statement in SCHEMA_STATEMENTS:
                await conn.execute(statement)
        await self.sync_zodiac_numbers()

    async def sync_zodiac_numbers(self) -> int:
        """把 zodiac_years 里每个农历年的 49 号码关联表物化进 zodiac_numbers。

        映射完全由 animal_of_01 推导（services.mark_six.zodiac_table_for_year），
        用 UPSERT 保证表内容永远与推导一致；可重复执行。
        """
        rows: list[tuple[int, int, str]] = []
        async with self._pool.acquire() as conn:
            years = await conn.fetch("SELECT lunar_year FROM zodiac_years ORDER BY lunar_year")
            for row in years:
                lunar_year = int(row["lunar_year"])
                table = zodiac_table_for_year(lunar_year)
                if table is None:
                    continue
                for zodiac, numbers in table.items():
                    rows.extend((lunar_year, number, zodiac) for number in numbers)
            if rows:
                async with conn.transaction():
                    await conn.executemany(
                        "INSERT INTO zodiac_numbers (lunar_year, number, zodiac) "
                        "VALUES ($1, $2, $3) "
                        "ON CONFLICT (lunar_year, number) "
                        "DO UPDATE SET zodiac = EXCLUDED.zodiac",
                        rows,
                    )
        return len(rows)

    # ------------------------------------------------------------------ #
    # 六合彩开奖记录（每期只有特码）—— draws 是唯一事实来源
    # ------------------------------------------------------------------ #
    async def import_draws(
        self, draws: list[dict[str, Any]], replace_existing: bool = True
    ) -> dict[str, Any]:
        inserted = updated = skipped = 0
        settled_payloads: list[dict[str, Any]] = []
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for payload in draws:
                    draw = normalize_draw_payload(payload)
                    existing = await conn.fetchrow(
                        "SELECT id FROM draws WHERE draw_date = $1", draw["draw_date"]
                    )
                    if existing is not None:
                        if not replace_existing:
                            skipped += 1
                            continue
                        await conn.execute(
                            "UPDATE draws SET period = $2, special_number = $3 WHERE id = $1",
                            existing["id"],
                            draw["period"],
                            draw["special_number"],
                        )
                        updated += 1
                        settled_payloads.append(draw)
                    else:
                        await conn.execute(
                            "INSERT INTO draws (draw_date, period, special_number) "
                            "VALUES ($1, $2, $3)",
                            draw["draw_date"],
                            draw["period"],
                            draw["special_number"],
                        )
                        inserted += 1
                        settled_payloads.append(draw)
        for draw in settled_payloads:
            await self.settle_recommend_rounds_for_draw(
                draw["period"], draw["special_number"], draw["draw_date"]
            )
        return {"imported": inserted, "updated": updated, "skipped": skipped}

    async def list_draws(
        self, limit: int | None = None, offset: int = 0
    ) -> list[dict[str, Any]]:
        sql = f"SELECT {DRAW_COLUMNS} FROM draws ORDER BY draw_date DESC, id DESC"
        args: list[Any] = []
        if limit is not None:
            sql += " LIMIT $1 OFFSET $2"
            args = [limit, offset]
        elif offset:
            sql += " OFFSET $1"
            args = [offset]
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]

    async def get_latest_draw(self) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {DRAW_COLUMNS} FROM draws "
                "ORDER BY draw_date DESC, id DESC LIMIT 1"
            )
        return dict(row) if row is not None else None

    async def get_draw(self, draw_id: int) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {DRAW_COLUMNS} FROM draws WHERE id = $1", draw_id
            )
        return dict(row) if row is not None else None

    async def delete_draw(self, draw_id: int) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM draws WHERE id = $1", draw_id)
        return int(result.split()[-1])

    async def clear_draws(self) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM draws")
        return int(result.split()[-1])

    async def suggest_next_period(self) -> int:
        async with self._pool.acquire() as conn:
            value = await conn.fetchval("SELECT MAX(period) FROM draws")
        return int(value) + 1 if value is not None else 1

    async def add_draw(
        self,
        special_number: int,
        period: int | None = None,
        draw_date: date | None = None,
    ) -> dict[str, Any]:
        # 计划在事务内基于全表快照计算，保证「查-改-写」不会与并发写入互相踩踏
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = [
                    dict(row)
                    for row in await conn.fetch(
                        "SELECT id, draw_date, period, special_number FROM draws "
                        "ORDER BY id"
                    )
                ]
                plan = plan_quick_draw(rows, special_number, period, draw_date)
                if plan["action"] == "update":
                    row = await conn.fetchrow(
                        f"UPDATE draws SET draw_date = $2, period = $3, "
                        f"special_number = $4 WHERE id = $1 RETURNING {DRAW_COLUMNS}",
                        plan["target_id"],
                        plan["draw_date"],
                        plan["period"],
                        plan["special_number"],
                    )
                else:
                    row = await conn.fetchrow(
                        f"INSERT INTO draws (draw_date, period, special_number) "
                        f"VALUES ($1, $2, $3) RETURNING {DRAW_COLUMNS}",
                        plan["draw_date"],
                        plan["period"],
                        plan["special_number"],
                    )
        draw = dict(row)
        await self.settle_recommend_rounds_for_draw(
            draw["period"], draw["special_number"], draw["draw_date"]
        )
        return draw

    async def correct_draw(
        self,
        draw_id: int,
        special_number: int,
        *,
        draw_date: date | None = None,
        actor_user_id: int | None = None,
    ) -> dict[str, Any]:
        special = int(special_number)
        if not 1 <= special <= 49:
            raise ValueError(f"特码越界: {special}")

        async with self._pool.acquire() as conn:
            existing = await conn.fetchrow(
                f"SELECT {DRAW_COLUMNS} FROM draws WHERE id = $1", int(draw_id)
            )
            if existing is None:
                raise ValueError("开奖记录不存在")

            old = dict(existing)
            old_special = int(old["special_number"])
            old_date = old["draw_date"]
            if isinstance(old_date, datetime):
                old_date = old_date.date()
            new_date = draw_date if draw_date is not None else old_date
            if isinstance(new_date, datetime):
                new_date = new_date.date()

            if new_date != old_date:
                conflict = await conn.fetchrow(
                    "SELECT id, period FROM draws WHERE draw_date = $1 AND id <> $2",
                    new_date,
                    int(draw_id),
                )
                if conflict is not None:
                    raise ValueError(
                        f"日期 {new_date} 已被第 {conflict['period']} 期占用，无法改期"
                    )

            row = await conn.fetchrow(
                f"UPDATE draws SET special_number = $2, draw_date = $3 "
                f"WHERE id = $1 RETURNING {DRAW_COLUMNS}",
                int(draw_id),
                special,
                new_date,
            )
            correction = await conn.fetchrow(
                """
                INSERT INTO draw_corrections (
                    draw_id, period, old_special_number, new_special_number,
                    old_draw_date, new_draw_date, actor_user_id, action
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
                """,
                int(draw_id),
                int(old["period"]),
                old_special,
                special,
                old_date,
                new_date,
                actor_user_id,
                DRAW_ACTION_CORRECT,
            )

        draw = dict(row)
        resettled = await self.settle_recommend_rounds_for_draw(
            int(draw["period"]), int(draw["special_number"]), draw["draw_date"]
        )
        return {
            "draw": draw,
            "action": DRAW_ACTION_CORRECT,
            "action_label": DRAW_ACTION_LABELS[DRAW_ACTION_CORRECT],
            "period": int(draw["period"]),
            "old_special_number": old_special,
            "new_special_number": special,
            "old_draw_date": old_date,
            "new_draw_date": new_date,
            "resettled_rounds": resettled,
            "correction_id": int(correction["id"]) if correction else None,
        }

    async def list_zodiac_numbers(self, lunar_year: int) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT lunar_year, number, zodiac FROM zodiac_numbers "
                "WHERE lunar_year = $1 ORDER BY number",
                lunar_year,
            )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 用户（认证 / RBAC）
    # ------------------------------------------------------------------ #
    async def create_user(
        self,
        phone: str,
        password_hash: str,
        password_salt: str,
        role: str = ROLE_USER,
        status: str = STATUS_PENDING,
    ) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO users (phone, password_hash, password_salt, role, status)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (phone) DO NOTHING
                RETURNING {USER_COLUMNS}
                """,
                phone,
                password_hash,
                password_salt,
                role,
                status,
            )
        return dict(row) if row is not None else None

    async def get_user_by_phone(self, phone: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {USER_COLUMNS} FROM users WHERE phone = $1", phone
            )
        return dict(row) if row is not None else None

    async def get_user_credentials(self, phone: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {USER_CREDENTIAL_COLUMNS} FROM users WHERE phone = $1", phone
            )
        return dict(row) if row is not None else None

    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {USER_COLUMNS} FROM users WHERE id = $1", user_id
            )
        return dict(row) if row is not None else None

    async def list_users(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT {USER_COLUMNS} FROM users"
        args: list[Any] = []
        if status is not None:
            sql += " WHERE status = $1"
            args.append(status)
        sql += " ORDER BY created_at DESC, id DESC"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]

    async def update_user_status(
        self, phone: str, status: str
    ) -> dict[str, Any] | None:
        # approved_at 只在「已通过」时写入，其余状态清空，避免陈旧的审批时间误导
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE users
                   SET status = $2,
                       approved_at = CASE WHEN $2 = '{STATUS_APPROVED}' THEN NOW() ELSE NULL END
                 WHERE phone = $1
                RETURNING {USER_COLUMNS}
                """,
                phone,
                status,
            )
        return dict(row) if row is not None else None

    async def update_user_role(self, phone: str, role: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE users SET role = $2 WHERE phone = $1 RETURNING {USER_COLUMNS}",
                phone,
                role,
            )
        return dict(row) if row is not None else None

    async def update_user_password(
        self, phone: str, password_hash: str, password_salt: str
    ) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE users
                   SET password_hash = $2, password_salt = $3
                 WHERE phone = $1
                RETURNING {USER_COLUMNS}
                """,
                phone,
                password_hash,
                password_salt,
            )
        return dict(row) if row is not None else None

    async def touch_user_login(self, phone: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET last_login_at = NOW() WHERE phone = $1", phone
            )

    async def count_approved_admins(self) -> int:
        async with self._pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE role = $1 AND status = $2",
                ROLE_ADMIN,
                STATUS_APPROVED,
            )
        return int(value or 0)

    # ------------------------------------------------------------------ #
    # 用户配置（按 user_id 隔离；0 = 全局模板）
    # ------------------------------------------------------------------ #
    async def _raw_settings(self, user_id: int) -> dict[str, str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM settings WHERE user_id = $1", user_id
            )
        raw: dict[str, Any] = {}
        for row in rows:
            try:
                raw[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                raw[row["key"]] = row["value"]
        return raw

    async def get_settings(self, user_id: int | None = None) -> dict[str, Any]:
        raw = await self._raw_settings(GLOBAL_SETTINGS_USER_ID)
        if user_id is not None and user_id != GLOBAL_SETTINGS_USER_ID:
            raw.update(await self._raw_settings(user_id))
        # 读取口径：没手动设置过走势加权 → 强制 neutral（存量旧默认 hot 也如此）
        return resolve_trend_bias(clamp_settings(raw))

    async def update_settings(
        self, patch: dict[str, Any], user_id: int | None = None
    ) -> dict[str, Any]:
        target = GLOBAL_SETTINGS_USER_ID if user_id is None else int(user_id)
        # 先解析「该用户当前的有效配置」，再叠加 patch —— 保证新用户首次保存
        # 时不会丢掉全局模板里的其它字段
        current = await self.get_settings(None if user_id is None else target)
        # 仅接受可写设置项，派生字段（如 big_min）永远不会落库；
        # 用户显式提交 trend_bias 时打上「手动设置过」标记
        merged = merge_settings_patch(current, patch)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for key in DEFAULT_SETTINGS:
                    await conn.execute(
                        "INSERT INTO settings (user_id, key, value) VALUES ($1, $2, $3) "
                        "ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value",
                        target,
                        key,
                        json.dumps(merged[key]),
                    )
        return merged

    async def upsert_recommend_round(
        self, round_row: dict[str, Any]
    ) -> dict[str, Any]:
        picks = round_row.get("picks") or []
        picks_json = json.dumps(picks, ensure_ascii=False)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO recommend_rounds (
                    user_id, period, base_period, draw_date, special_number,
                    odds, cost, payout, profit, hit, mode, picks_json, status,
                    stake_mode, settled_at
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9, $10, $11, $12, $13,
                    $14, $15
                )
                ON CONFLICT (user_id, period) DO UPDATE SET
                    base_period = EXCLUDED.base_period,
                    draw_date = EXCLUDED.draw_date,
                    special_number = EXCLUDED.special_number,
                    odds = EXCLUDED.odds,
                    cost = EXCLUDED.cost,
                    payout = EXCLUDED.payout,
                    profit = EXCLUDED.profit,
                    hit = EXCLUDED.hit,
                    mode = EXCLUDED.mode,
                    picks_json = EXCLUDED.picks_json,
                    status = EXCLUDED.status,
                    stake_mode = EXCLUDED.stake_mode,
                    settled_at = EXCLUDED.settled_at
                RETURNING {RECOMMEND_ROUND_COLUMNS}
                """,
                int(round_row["user_id"]),
                int(round_row["period"]),
                round_row.get("base_period"),
                round_row.get("draw_date"),
                round_row.get("special_number"),
                money(round_row.get("odds", 0)),
                money(round_row.get("cost", 0)),
                money(round_row.get("payout", 0)),
                money(round_row.get("profit", 0)),
                round_row.get("hit"),
                str(round_row.get("mode") or ""),
                picks_json,
                str(round_row.get("status") or STATUS_PENDING),
                normalize_stake_mode(round_row.get("stake_mode")),
                round_row.get("settled_at"),
            )
        return row_to_recommend_round(row)

    async def list_recommend_rounds(
        self,
        user_id: int | None = None,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        target = GLOBAL_SETTINGS_USER_ID if user_id is None else int(user_id)
        sql = (
            f"SELECT {RECOMMEND_ROUND_COLUMNS} FROM recommend_rounds "
            "WHERE user_id = $1 ORDER BY period DESC, id DESC"
        )
        args: list[Any] = [target]
        if limit is not None:
            sql += " LIMIT $2"
            args.append(int(limit))
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [row_to_recommend_round(row) for row in rows]

    async def settle_recommend_rounds_for_draw(
        self,
        period: int,
        special_number: int,
        draw_date: date | None = None,
    ) -> int:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {RECOMMEND_ROUND_COLUMNS} FROM recommend_rounds "
                "WHERE period = $1",
                int(period),
            )
        if not rows:
            return 0
        settled = 0
        for row in rows:
            current = row_to_recommend_round(row)
            updated = apply_settlement(current, int(special_number), draw_date)
            await self.upsert_recommend_round(updated)
            settled += 1
        return settled

    async def import_data(
        self, payload: dict[str, Any], replace: bool, user_id: int | None = None
    ) -> dict[str, Any]:
        """导入导出文件（version 2）：只接受 ``draws`` + ``settings``。"""
        draws = payload.get("draws") or []
        settings = payload.get("settings") or {}
        normalized = [normalize_draw_payload(item) for item in draws]
        target = GLOBAL_SETTINGS_USER_ID if user_id is None else int(user_id)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if replace:
                    await conn.execute("DELETE FROM draws")
                for key in DEFAULT_SETTINGS:
                    if key in settings:
                        await conn.execute(
                            "INSERT INTO settings (user_id, key, value) VALUES ($1, $2, $3) "
                            "ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value",
                            target,
                            key,
                            json.dumps(settings[key]),
                        )
        summary = await self.import_draws(normalized, replace_existing=True)

        return {
            "imported_draws": summary["imported"] + summary["updated"],
            "replaced": replace,
            "settings": await self.get_settings(user_id),
        }


# --------------------------------------------------------------------------- #
# In-memory（未配置 DATABASE_URL 时的本地回退）
# --------------------------------------------------------------------------- #
class MemoryStore(Store):
    backend = "memory"

    def __init__(self) -> None:
        # 用户配置：按 user_id 隔离；0 = 全局模板（与 PostgresStore 同口径）
        self._settings: dict[int, dict[str, Any]] = {
            GLOBAL_SETTINGS_USER_ID: dict(DEFAULT_SETTINGS)
        }
        # 用户表：phone 唯一，行内含口令字段（对外一律过 public_user_row）
        self._users: dict[str, dict[str, Any]] = {}
        self._next_user_id = 1
        # 六合彩开奖记录（每期只有特码）—— 特码的唯一事实来源
        self._draws: list[dict[str, Any]] = []
        self._next_draw_id = 1
        # 财富密码采用快照
        self._recommend_rounds: list[dict[str, Any]] = []
        self._next_recommend_round_id = 1
        # 开奖纠正审计
        self._draw_corrections: list[dict[str, Any]] = []
        self._next_correction_id = 1
        # 49 号码关联表：物化派生数据（与 PostgresStore.ensure_schema 同源）
        self._zodiac_numbers: dict[int, list[dict[str, Any]]] = {}
        for year, _starts_on, _animal in ZODIAC_YEARS:
            table = zodiac_table_for_year(year)
            if table is None:
                continue
            self._zodiac_numbers[year] = [
                {"lunar_year": year, "number": number, "zodiac": zodiac}
                for zodiac, numbers in table.items()
                for number in numbers
            ]

    # ------------------------------------------------------------------ #
    # 六合彩开奖记录（语义与 PostgresStore 完全一致）
    # ------------------------------------------------------------------ #
    async def import_draws(
        self, draws: list[dict[str, Any]], replace_existing: bool = True
    ) -> dict[str, Any]:
        inserted = updated = skipped = 0
        settled_payloads: list[dict[str, Any]] = []
        for payload in draws:
            draw = normalize_draw_payload(payload)
            existing = next(
                (d for d in self._draws if d["draw_date"] == draw["draw_date"]), None
            )
            if existing is not None:
                if not replace_existing:
                    skipped += 1
                    continue
                existing["period"] = draw["period"]
                existing["special_number"] = draw["special_number"]
                updated += 1
                settled_payloads.append(draw)
            else:
                self._draws.append(
                    {
                        "id": self._next_draw_id,
                        "draw_date": draw["draw_date"],
                        "period": draw["period"],
                        "special_number": draw["special_number"],
                        "created_at": datetime.now(timezone.utc),
                    }
                )
                self._next_draw_id += 1
                inserted += 1
                settled_payloads.append(draw)
        for draw in settled_payloads:
            await self.settle_recommend_rounds_for_draw(
                draw["period"], draw["special_number"], draw["draw_date"]
            )
        return {"imported": inserted, "updated": updated, "skipped": skipped}

    def _ordered_draws(self) -> list[dict[str, Any]]:
        return sorted(
            self._draws, key=lambda d: (d["draw_date"], d["id"]), reverse=True
        )

    async def list_draws(
        self, limit: int | None = None, offset: int = 0
    ) -> list[dict[str, Any]]:
        ordered = self._ordered_draws()
        if limit is None:
            window = ordered[offset:] if offset else ordered
        else:
            window = ordered[offset : offset + limit]
        return [dict(draw) for draw in window]

    async def get_latest_draw(self) -> dict[str, Any] | None:
        ordered = self._ordered_draws()
        return dict(ordered[0]) if ordered else None

    async def get_draw(self, draw_id: int) -> dict[str, Any] | None:
        draw = next((d for d in self._draws if d["id"] == draw_id), None)
        return dict(draw) if draw is not None else None

    async def delete_draw(self, draw_id: int) -> int:
        before = len(self._draws)
        self._draws = [d for d in self._draws if d["id"] != draw_id]
        return before - len(self._draws)

    async def clear_draws(self) -> int:
        count = len(self._draws)
        self._draws.clear()
        return count

    async def suggest_next_period(self) -> int:
        return next_period_from_draws(self._draws)

    async def add_draw(
        self,
        special_number: int,
        period: int | None = None,
        draw_date: date | None = None,
    ) -> dict[str, Any]:
        # 与 PostgresStore 共用 plan_quick_draw，保证两种存储行为完全一致
        plan = plan_quick_draw(self._draws, special_number, period, draw_date)
        if plan["action"] == "update":
            draw = next(d for d in self._draws if d["id"] == plan["target_id"])
            draw["draw_date"] = plan["draw_date"]
            draw["period"] = plan["period"]
            draw["special_number"] = plan["special_number"]
            await self.settle_recommend_rounds_for_draw(
                draw["period"], draw["special_number"], draw["draw_date"]
            )
            return dict(draw)

        draw = {
            "id": self._next_draw_id,
            "draw_date": plan["draw_date"],
            "period": plan["period"],
            "special_number": plan["special_number"],
            "created_at": datetime.now(timezone.utc),
        }
        self._next_draw_id += 1
        self._draws.append(draw)
        await self.settle_recommend_rounds_for_draw(
            draw["period"], draw["special_number"], draw["draw_date"]
        )
        return dict(draw)

    async def correct_draw(
        self,
        draw_id: int,
        special_number: int,
        *,
        draw_date: date | None = None,
        actor_user_id: int | None = None,
    ) -> dict[str, Any]:
        special = int(special_number)
        if not 1 <= special <= 49:
            raise ValueError(f"特码越界: {special}")

        draw = next((d for d in self._draws if int(d["id"]) == int(draw_id)), None)
        if draw is None:
            raise ValueError("开奖记录不存在")

        old_special = int(draw["special_number"])
        old_date = draw["draw_date"]
        if isinstance(old_date, datetime):
            old_date = old_date.date()
        new_date = draw_date if draw_date is not None else old_date
        if isinstance(new_date, datetime):
            new_date = new_date.date()

        if new_date != old_date:
            conflict = next(
                (
                    d
                    for d in self._draws
                    if int(d["id"]) != int(draw_id)
                    and _as_date(d["draw_date"]) == new_date
                ),
                None,
            )
            if conflict is not None:
                raise ValueError(
                    f"日期 {new_date} 已被第 {conflict['period']} 期占用，无法改期"
                )

        draw["special_number"] = special
        draw["draw_date"] = new_date

        correction_id = self._next_correction_id
        self._next_correction_id += 1
        self._draw_corrections.append(
            {
                "id": correction_id,
                "draw_id": int(draw_id),
                "period": int(draw["period"]),
                "old_special_number": old_special,
                "new_special_number": special,
                "old_draw_date": old_date,
                "new_draw_date": new_date,
                "actor_user_id": actor_user_id,
                "action": DRAW_ACTION_CORRECT,
                "created_at": datetime.now(timezone.utc),
            }
        )

        resettled = await self.settle_recommend_rounds_for_draw(
            int(draw["period"]), special, new_date
        )
        return {
            "draw": dict(draw),
            "action": DRAW_ACTION_CORRECT,
            "action_label": DRAW_ACTION_LABELS[DRAW_ACTION_CORRECT],
            "period": int(draw["period"]),
            "old_special_number": old_special,
            "new_special_number": special,
            "old_draw_date": old_date,
            "new_draw_date": new_date,
            "resettled_rounds": resettled,
            "correction_id": correction_id,
        }

    async def list_zodiac_numbers(self, lunar_year: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self._zodiac_numbers.get(int(lunar_year), [])]

    # ------------------------------------------------------------------ #
    # 用户（语义与 PostgresStore 完全一致）
    # ------------------------------------------------------------------ #
    async def create_user(
        self,
        phone: str,
        password_hash: str,
        password_salt: str,
        role: str = ROLE_USER,
        status: str = STATUS_PENDING,
    ) -> dict[str, Any] | None:
        if phone in self._users:
            return None
        row: dict[str, Any] = {
            "id": self._next_user_id,
            "phone": phone,
            "password_hash": password_hash,
            "password_salt": password_salt,
            "role": role,
            "status": status,
            "created_at": datetime.now(timezone.utc),
            "approved_at": None,
            "last_login_at": None,
        }
        self._next_user_id += 1
        self._users[phone] = row
        return public_user_row(row)

    async def get_user_by_phone(self, phone: str) -> dict[str, Any] | None:
        row = self._users.get(phone)
        return public_user_row(row) if row is not None else None

    async def get_user_credentials(self, phone: str) -> dict[str, Any] | None:
        row = self._users.get(phone)
        if row is None:
            return None
        return {
            key.strip(): row.get(key.strip())
            for key in USER_CREDENTIAL_COLUMNS.split(",")
        }

    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        row = next(
            (u for u in self._users.values() if int(u["id"]) == int(user_id)), None
        )
        return public_user_row(row) if row is not None else None

    async def list_users(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self._users.values()
            if status is None or row["status"] == status
        ]
        rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return [public_user_row(row) for row in rows]

    async def update_user_status(
        self, phone: str, status: str
    ) -> dict[str, Any] | None:
        row = self._users.get(phone)
        if row is None:
            return None
        row["status"] = status
        row["approved_at"] = (
            datetime.now(timezone.utc) if status == STATUS_APPROVED else None
        )
        return public_user_row(row)

    async def update_user_role(self, phone: str, role: str) -> dict[str, Any] | None:
        row = self._users.get(phone)
        if row is None:
            return None
        row["role"] = role
        return public_user_row(row)

    async def update_user_password(
        self, phone: str, password_hash: str, password_salt: str
    ) -> dict[str, Any] | None:
        row = self._users.get(phone)
        if row is None:
            return None
        row["password_hash"] = password_hash
        row["password_salt"] = password_salt
        return public_user_row(row)

    async def touch_user_login(self, phone: str) -> None:
        row = self._users.get(phone)
        if row is not None:
            row["last_login_at"] = datetime.now(timezone.utc)

    async def count_approved_admins(self) -> int:
        return sum(
            1
            for row in self._users.values()
            if row["role"] == ROLE_ADMIN and row["status"] == STATUS_APPROVED
        )

    # ------------------------------------------------------------------ #
    # 用户配置（按 user_id 隔离；0 = 全局模板）
    # ------------------------------------------------------------------ #
    def _bucket(self, user_id: int) -> dict[str, Any]:
        return self._settings.setdefault(int(user_id), {})

    async def get_settings(self, user_id: int | None = None) -> dict[str, Any]:
        raw = dict(self._settings[GLOBAL_SETTINGS_USER_ID])
        if user_id is not None and int(user_id) != GLOBAL_SETTINGS_USER_ID:
            raw.update(self._bucket(int(user_id)))
        # 与 PostgresStore 同口径：没手动设置过走势加权 → 强制 neutral
        return resolve_trend_bias(clamp_settings(raw))

    async def update_settings(
        self, patch: dict[str, Any], user_id: int | None = None
    ) -> dict[str, Any]:
        target = GLOBAL_SETTINGS_USER_ID if user_id is None else int(user_id)
        # 与 PostgresStore 同口径：先取该用户的有效配置，再叠加 patch
        current = await self.get_settings(None if user_id is None else target)
        # 仅接受可写设置项；用户显式提交 trend_bias 时打上「手动设置过」标记
        merged = merge_settings_patch(current, patch)
        self._settings[target] = dict(merged)
        return dict(merged)

    async def upsert_recommend_round(
        self, round_row: dict[str, Any]
    ) -> dict[str, Any]:
        user_id = int(round_row["user_id"])
        period = int(round_row["period"])
        existing = next(
            (
                row
                for row in self._recommend_rounds
                if int(row["user_id"]) == user_id and int(row["period"]) == period
            ),
            None,
        )
        payload = {
            "user_id": user_id,
            "period": period,
            "base_period": round_row.get("base_period"),
            "draw_date": round_row.get("draw_date"),
            "special_number": round_row.get("special_number"),
            "odds": money(round_row.get("odds", 0)),
            "cost": money(round_row.get("cost", 0)),
            "payout": money(round_row.get("payout", 0)),
            "profit": money(round_row.get("profit", 0)),
            "hit": round_row.get("hit"),
            "mode": str(round_row.get("mode") or ""),
            "picks": list(round_row.get("picks") or []),
            "status": str(round_row.get("status") or STATUS_PENDING),
            "stake_mode": normalize_stake_mode(
                round_row.get("stake_mode", STAKE_MODE_SIMULATED)
            ),
            "settled_at": round_row.get("settled_at"),
        }
        if existing is None:
            payload["id"] = self._next_recommend_round_id
            payload["created_at"] = datetime.now(timezone.utc)
            self._next_recommend_round_id += 1
            self._recommend_rounds.append(payload)
            return dict(payload)

        payload["id"] = existing["id"]
        payload["created_at"] = existing.get("created_at") or datetime.now(timezone.utc)
        existing.clear()
        existing.update(payload)
        return dict(existing)

    async def list_recommend_rounds(
        self,
        user_id: int | None = None,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        target = GLOBAL_SETTINGS_USER_ID if user_id is None else int(user_id)
        rows = [
            {
                **dict(row),
                "stake_mode": normalize_stake_mode(row.get("stake_mode")),
            }
            for row in self._recommend_rounds
            if int(row["user_id"]) == target
        ]
        rows.sort(key=lambda r: (int(r["period"]), int(r["id"])), reverse=True)
        if limit is not None:
            rows = rows[: int(limit)]
        return rows

    async def settle_recommend_rounds_for_draw(
        self,
        period: int,
        special_number: int,
        draw_date: date | None = None,
    ) -> int:
        matched = [
            row
            for row in self._recommend_rounds
            if int(row["period"]) == int(period)
        ]
        for row in matched:
            updated = apply_settlement(row, int(special_number), draw_date)
            row.clear()
            row.update(updated)
        return len(matched)

    async def import_data(
        self, payload: dict[str, Any], replace: bool, user_id: int | None = None
    ) -> dict[str, Any]:
        """导入导出文件（version 2）：只接受 ``draws`` + ``settings``。"""
        draws = payload.get("draws") or []
        normalized = [normalize_draw_payload(item) for item in draws]
        target = GLOBAL_SETTINGS_USER_ID if user_id is None else int(user_id)
        if replace:
            self._draws.clear()
        summary = await self.import_draws(normalized, replace_existing=True)

        bucket = self._bucket(target)
        bucket.update(
            {
                k: v
                for k, v in (payload.get("settings") or {}).items()
                if k in DEFAULT_SETTINGS
            }
        )
        # 用户自己的行以全局模板打底（与 get_settings 同口径）
        if target == GLOBAL_SETTINGS_USER_ID:
            self._settings[target] = clamp_settings(bucket)
        else:
            self._settings[target] = clamp_settings(
                {**self._settings[GLOBAL_SETTINGS_USER_ID], **bucket}
            )
        return {
            "imported_draws": summary["imported"] + summary["updated"],
            "replaced": replace,
            "settings": await self.get_settings(user_id),
        }


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
_store: Store | None = None


async def get_store() -> Store:
    """返回当前存储实现；配置了 DATABASE_URL 则使用 Postgres。"""
    global _store
    if _store is not None:
        return _store

    pool = await init_pool()
    if pool is None:
        _store = MemoryStore()
        return _store

    postgres = PostgresStore(pool)
    try:
        await postgres.ensure_schema()
        _store = postgres
    except Exception:
        _store = MemoryStore()
    return _store


def reset_store() -> None:
    """仅供测试使用。"""
    global _store
    _store = None
