"""users 表 + settings 按用户隔离 —— 幂等迁移，可重复执行。

背景：``settings`` 原本是**全局单行**（``PRIMARY KEY (key)``）。引入手机号登录后
配置必须按用户隔离，本脚本负责把它收敛成：

    settings (user_id BIGINT NOT NULL DEFAULT 0, key TEXT, value TEXT,
              PRIMARY KEY (user_id, key))

``user_id = 0`` 是**保留的全局模板**（不是真实用户）：

- 存量那 5 行全局配置自动落到 ``user_id = 0``，成为「全局默认值」，**一行不删**；
- 新用户没有任何自己的行 → 读到的就是这份全局模板；
- 用户一旦 PUT /api/settings，只写自己 ``user_id`` 的行，全局模板不受影响。

同时建 ``users`` 表（角色 / 状态一律英文枚举）。

本脚本**只做加法**：不 DROP 表、不 DELETE 数据、不碰 ``draws``。
``draws`` / ``zodiac_years`` / ``zodiac_numbers`` 一个字节都不动。

用法：
    backend\\.venv\\Scripts\\python.exe backend\\scripts\\migrate_users_and_per_user_settings.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

# 显式路径：脚本可能从任意工作目录启动
ENV_PATH = Path(r"D:\myproject\wave-money\backend\.env")
load_dotenv(ENV_PATH if ENV_PATH.exists() else BACKEND_DIR / ".env")

import asyncpg  # noqa: E402

from repository import SCHEMA_STATEMENTS, USER_COLUMNS  # noqa: E402
from services.auth import GLOBAL_SETTINGS_USER_ID  # noqa: E402
from services.lottery import DEFAULT_SETTINGS  # noqa: E402


async def _pk_columns(conn: asyncpg.Connection, table: str) -> str | None:
    return await conn.fetchval(
        """
        SELECT string_agg(a.attname, ',' ORDER BY array_position(c.conkey, a.attnum))
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY (c.conkey)
         WHERE t.relname = $1 AND c.contype = 'p'
        """,
        table,
    )


async def migrate() -> dict:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL 未配置，请检查 backend/.env")

    pool = await asyncpg.create_pool(dsn=url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            settings_before = await conn.fetchval("SELECT COUNT(*) FROM settings")
            draws_before = await conn.fetchval("SELECT COUNT(*) FROM draws")
            pk_before = await _pk_columns(conn, "settings")

            # 应用 SCHEMA_STATEMENTS：建 users 表 + 幂等收敛 settings 主键
            for statement in SCHEMA_STATEMENTS:
                await conn.execute(statement)

            # 补齐全局模板（user_id = 0）缺失的键；已存在的值（用户改过的）保持不变
            seeded = 0
            for key, value in DEFAULT_SETTINGS.items():
                result = await conn.execute(
                    "INSERT INTO settings (user_id, key, value) VALUES ($1, $2, $3) "
                    "ON CONFLICT (user_id, key) DO NOTHING",
                    GLOBAL_SETTINGS_USER_ID,
                    key,
                    json.dumps(value),
                )
                if result.split()[-1] == "1":
                    seeded += 1

            pk_after = await _pk_columns(conn, "settings")
            settings_after = await conn.fetchval("SELECT COUNT(*) FROM settings")
            draws_after = await conn.fetchval("SELECT COUNT(*) FROM draws")

            per_user = await conn.fetch(
                "SELECT user_id, COUNT(*) AS rows FROM settings "
                "GROUP BY user_id ORDER BY user_id"
            )
            users = await conn.fetchval("SELECT COUNT(*) FROM users")
            global_values = await conn.fetch(
                "SELECT key, value FROM settings WHERE user_id = $1 ORDER BY key",
                GLOBAL_SETTINGS_USER_ID,
            )
            columns = await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'users' "
                "ORDER BY ordinal_position",
            )

        return {
            "settings_pk_before": pk_before,
            "settings_pk_after": pk_after,
            "settings_rows_before": int(settings_before),
            "settings_rows_after": int(settings_after),
            "global_template_seeded": seeded,
            "per_user": [(int(r["user_id"]), int(r["rows"])) for r in per_user],
            "global_values": [(r["key"], r["value"]) for r in global_values],
            "users": int(users),
            "users_columns": [r["column_name"] for r in columns],
            "draws_before": int(draws_before),
            "draws_after": int(draws_after),
        }
    finally:
        await pool.close()


def main() -> None:
    result = asyncio.run(migrate())
    print("users 表 + settings 按用户隔离迁移完成（幂等）：")
    print(f"  settings 主键            : {result['settings_pk_before']} → {result['settings_pk_after']}")
    print(f"  settings 行数            : {result['settings_rows_before']} → {result['settings_rows_after']}")
    print(f"  本次补齐全局模板键        : {result['global_template_seeded']}")
    print(f"  按 user_id 分布           : {result['per_user']}")
    print(f"  全局模板 (user_id=0) 取值 : {result['global_values']}")
    print(f"  users 行数               : {result['users']}")
    print(f"  users 列                 : {result['users_columns']}")
    print(f"  draws 行数（必须不变）    : {result['draws_before']} → {result['draws_after']}")
    if result["draws_before"] != result["draws_after"]:
        print("  [错误] draws 行数发生变化，请立即检查！")
        raise SystemExit(1)
    print("  OK：只做了加法（建表 / 加列 / 换主键 / 补默认值），未删除任何数据。")
    print(f"  提示：users 对外列白名单 = {USER_COLUMNS}")


if __name__ == "__main__":
    main()
