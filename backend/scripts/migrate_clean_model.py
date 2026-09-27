"""数据模型清理迁移（幂等，可重复执行）。

把 Supabase 数据库收敛到「干净数据模型」：

1. 删除已废弃的表：``draw_numbers``（正码明细）、``elements``（五行）、``hehe_table``（河合码）；
2. ``draws`` 收敛为「每期一个特码」：确保存在 ``special_number`` 列、
   清掉无特码的历史行、补齐 NOT NULL 与 1-49 约束；
3. 应用 ``repository.SCHEMA_STATEMENTS``（建表 + zodiac_years 种子）；
4. 按 ``zodiac_years`` 里每个农历年，用 ``services.mark_six.zodiac_table_for_year()``
   物化 ``zodiac_numbers`` 的 49 行（UPSERT，永远与推导一致）。

用法：
    backend\\.venv\\Scripts\\python.exe backend\\scripts\\migrate_clean_model.py
"""

from __future__ import annotations

import asyncio
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

from repository import SCHEMA_STATEMENTS, PostgresStore  # noqa: E402

# 本次清理要彻底删除的表
DROP_STATEMENTS = (
    "DROP TABLE IF EXISTS draw_numbers CASCADE",
    "DROP TABLE IF EXISTS elements CASCADE",
    "DROP TABLE IF EXISTS hehe_table CASCADE",
)

# draws 收敛为「每期一个特码」；老表没有 special_number 列，这里幂等补齐
DRAWS_RECONCILE = (
    "ALTER TABLE draws ADD COLUMN IF NOT EXISTS special_number INTEGER",
    "DELETE FROM draws WHERE special_number IS NULL",
    "ALTER TABLE draws ALTER COLUMN special_number SET NOT NULL",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'draws'::regclass
              AND conname = 'draws_special_number_check'
        ) THEN
            ALTER TABLE draws
                ADD CONSTRAINT draws_special_number_check
                CHECK (special_number BETWEEN 1 AND 49);
        END IF;
    END
    $$;
    """,
)


async def migrate() -> dict:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL 未配置，请检查 backend/.env")

    pool = await asyncpg.create_pool(dsn=url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            for statement in DROP_STATEMENTS:
                await conn.execute(statement)
            for statement in SCHEMA_STATEMENTS:
                await conn.execute(statement)
            for statement in DRAWS_RECONCILE:
                await conn.execute(statement)

        seeded = await PostgresStore(pool).sync_zodiac_numbers()

        async with pool.acquire() as conn:
            tables = [
                row["table_name"]
                for row in await conn.fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' ORDER BY table_name"
                )
            ]
            draw_rows = await conn.fetchval("SELECT COUNT(*) FROM draws")
            zodiac_rows = await conn.fetchval("SELECT COUNT(*) FROM zodiac_numbers")
            years = await conn.fetchval("SELECT COUNT(*) FROM zodiac_years")

        return {
            "dropped": ["draw_numbers", "elements", "hehe_table"],
            "tables": tables,
            "draws": draw_rows,
            "zodiac_years": years,
            "zodiac_numbers": zodiac_rows,
            "zodiac_rows_written": seeded,
        }
    finally:
        await pool.close()


def main() -> None:
    result = asyncio.run(migrate())
    print("迁移完成（幂等）：")
    print(f"  已删除表: {result['dropped']}")
    print(f"  当前表  : {result['tables']}")
    print(f"  draws            : {result['draws']} 行")
    print(f"  zodiac_years     : {result['zodiac_years']} 行")
    print(f"  zodiac_numbers   : {result['zodiac_numbers']} 行"
          f"（本次写入 {result['zodiac_rows_written']} 行）")


if __name__ == "__main__":
    main()
