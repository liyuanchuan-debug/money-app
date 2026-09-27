"""records → draws 迁移（幂等，可重复执行）。

背景：``records``（「快捷录入」草稿表：``(id, number, created_at)``）与
``draws``（开奖总表：``(id, draw_date, period, special_number, created_at)``）
曾经各存一份「特码」，形成双事实来源，导致 ``/api/recommend`` 只读到几乎为空的
``records``。现在 ``draws`` 是唯一事实来源，本脚本负责：

1. 读出 ``records`` 的全部行；
2. 逐行搬进 ``draws``（``draw_date`` 由 ``created_at`` 的 UTC+8 日期推导，
   ``period`` 由 ``max(period)+n`` 推导），按 ``draw_date`` 去重覆盖；
3. 删除 ``records`` 表。

**准确性警告**：``records`` 只存了号码与写入时间，开奖日期 / 期号无法可靠还原。
如果表里有行，脚本会打印醒目警告；派生值只作兜底，请人工核对后再依赖分析结果。

用法：
    backend\\.venv\\Scripts\\python.exe backend\\scripts\\migrate_records_to_draws.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

# 显式路径：脚本可能从任意工作目录启动
ENV_PATH = Path(r"D:\myproject\wave-money\backend\.env")
load_dotenv(ENV_PATH if ENV_PATH.exists() else BACKEND_DIR / ".env")

import asyncpg  # noqa: E402

# 与快捷录入同口径：created_at 是 timestamptz，取 UTC+8 的日期
UTC8 = timezone(timedelta(hours=8), "Asia/Shanghai")


async def migrate() -> dict:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL 未配置，请检查 backend/.env")

    pool = await asyncpg.create_pool(dsn=url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            # to_regclass 让「表已被删除」的重复执行安全通过
            records_exists = await conn.fetchval("SELECT to_regclass('public.records')")
            rows = []
            if records_exists:
                rows = await conn.fetch(
                    "SELECT id, number, created_at FROM records "
                    "ORDER BY created_at ASC, id ASC"
                )

            warnings: list[str] = []
            migrated = updated = 0

            if rows:
                warnings.append(
                    f"records 有 {len(rows)} 行：draw_date 由 created_at（UTC+8）"
                    f"推导、period 由 max(period)+n 推导，可能与真实开奖不一致，"
                    f"请人工核对。"
                )
                max_period = await conn.fetchval(
                    "SELECT COALESCE(MAX(period), 0) FROM draws"
                )
                next_period = int(max_period)
                async with conn.transaction():
                    for row in rows:
                        next_period += 1
                        created: datetime = row["created_at"]
                        draw_date = created.astimezone(UTC8).date()
                        existing = await conn.fetchrow(
                            "SELECT id FROM draws WHERE draw_date = $1", draw_date
                        )
                        if existing is not None:
                            await conn.execute(
                                "UPDATE draws SET period = $2, special_number = $3 "
                                "WHERE id = $1",
                                existing["id"],
                                next_period,
                                int(row["number"]),
                            )
                            updated += 1
                        else:
                            await conn.execute(
                                "INSERT INTO draws (draw_date, period, special_number) "
                                "VALUES ($1, $2, $3)",
                                draw_date,
                                next_period,
                                int(row["number"]),
                            )
                            migrated += 1

            # 无论是否有行，都把 records 删掉（IF EXISTS 保证可重复执行）
            await conn.execute("DROP TABLE IF EXISTS records CASCADE")

            draw_rows = await conn.fetchval("SELECT COUNT(*) FROM draws")
            records_left = await conn.fetchval("SELECT to_regclass('public.records')")

        return {
            "records_existed": bool(records_exists),
            "records_rows": len(rows),
            "migrated": migrated,
            "updated": updated,
            "warnings": warnings,
            "draws_total": int(draw_rows),
            "records_table_present_after": records_left is not None,
        }
    finally:
        await pool.close()


def main() -> None:
    result = asyncio.run(migrate())
    print("records → draws 迁移完成（幂等）：")
    print(f"  records 表是否存在     : {result['records_existed']}")
    print(f"  records 行数           : {result['records_rows']}")
    print(f"  新增 draws 行          : {result['migrated']}")
    print(f"  覆盖已有 draws 行      : {result['updated']}")
    print(f"  迁移后 draws 总行数    : {result['draws_total']}")
    print(f"  迁移后 records 是否还在: {result['records_table_present_after']}")
    for warning in result["warnings"]:
        print(f"  [警告] {warning}")
    if not result["records_existed"]:
        print("  records 表本就不存在，无需迁移（重复执行安全）。")


if __name__ == "__main__":
    main()
