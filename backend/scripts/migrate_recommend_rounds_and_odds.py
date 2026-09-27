"""recommend_rounds 表 + settings.odds 默认 47 —— 幂等迁移。

用法：
    backend\\.venv\\Scripts\\python.exe backend\\scripts\\migrate_recommend_rounds_and_odds.py
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

ENV_PATH = Path(r"D:\myproject\wave-money\backend\.env")
load_dotenv(ENV_PATH if ENV_PATH.exists() else BACKEND_DIR / ".env")

import asyncpg  # noqa: E402

from repository import SCHEMA_STATEMENTS  # noqa: E402
from services.auth import GLOBAL_SETTINGS_USER_ID  # noqa: E402
from services.lottery import DEFAULT_ODDS, DEFAULT_SETTINGS  # noqa: E402


async def migrate() -> dict:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL 未配置，请检查 backend/.env")

    pool = await asyncpg.create_pool(dsn=url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            draws_before = await conn.fetchval("SELECT COUNT(*) FROM draws")
            for statement in SCHEMA_STATEMENTS:
                await conn.execute(statement)

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

            # 显式确保全局模板有 odds（即使 DEFAULT_SETTINGS 漏同步也能补）
            odds_result = await conn.execute(
                "INSERT INTO settings (user_id, key, value) VALUES ($1, $2, $3) "
                "ON CONFLICT (user_id, key) DO NOTHING",
                GLOBAL_SETTINGS_USER_ID,
                "odds",
                json.dumps(DEFAULT_ODDS),
            )
            odds_seeded = odds_result.split()[-1] == "1"

            table_exists = await conn.fetchval(
                "SELECT EXISTS ("
                "  SELECT 1 FROM information_schema.tables "
                "  WHERE table_schema = 'public' AND table_name = 'recommend_rounds'"
                ")"
            )
            rounds_count = (
                int(await conn.fetchval("SELECT COUNT(*) FROM recommend_rounds"))
                if table_exists
                else 0
            )
            odds_row = await conn.fetchval(
                "SELECT value FROM settings WHERE user_id = $1 AND key = 'odds'",
                GLOBAL_SETTINGS_USER_ID,
            )
            draws_after = await conn.fetchval("SELECT COUNT(*) FROM draws")

        return {
            "recommend_rounds_exists": bool(table_exists),
            "recommend_rounds_rows": rounds_count,
            "global_keys_seeded": seeded,
            "odds_seeded": odds_seeded,
            "global_odds": odds_row,
            "draws_before": int(draws_before),
            "draws_after": int(draws_after),
        }
    finally:
        await pool.close()


def main() -> None:
    result = asyncio.run(migrate())
    print("recommend_rounds + odds 迁移完成（幂等）：")
    print(f"  recommend_rounds 表存在 : {result['recommend_rounds_exists']}")
    print(f"  recommend_rounds 行数   : {result['recommend_rounds_rows']}")
    print(f"  本次补齐全局模板键       : {result['global_keys_seeded']}")
    print(f"  本次补齐 odds           : {result['odds_seeded']}")
    print(f"  全局模板 odds 取值       : {result['global_odds']}")
    print(f"  draws 行数（必须不变）   : {result['draws_before']} → {result['draws_after']}")
    if result["draws_before"] != result["draws_after"]:
        print("  [错误] draws 行数发生变化，请立即检查！")
        raise SystemExit(1)
    print("  OK")


if __name__ == "__main__":
    main()
