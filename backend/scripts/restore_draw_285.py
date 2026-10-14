"""Restore overwritten period 285 (special 19) from recommend_rounds snapshot.

One-shot repair for the same-day period rename bug: draws row 285/19 was
UPDATE'd into 286/24 on 2026-10-13. recommend_rounds still holds the truth.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

from dotenv import load_dotenv
import asyncpg


async def main() -> None:
    load_dotenv()
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_draws_period ON draws (period)"
        )

        existing_285 = await conn.fetchrow("SELECT * FROM draws WHERE period = 285")
        row_286 = await conn.fetchrow("SELECT * FROM draws WHERE period = 286")
        snap = await conn.fetchrow(
            """
            SELECT period, draw_date, special_number
            FROM recommend_rounds
            WHERE period = 285 AND special_number IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """
        )
        print("before 285", dict(existing_285) if existing_285 else None)
        print("before 286", dict(row_286) if row_286 else None)
        print("snapshot", dict(snap) if snap else None)

        if existing_285 is not None:
            print("285 already present, skip restore")
            return
        if snap is None or int(snap["special_number"]) != 19:
            raise SystemExit(f"unexpected snapshot: {snap}")
        if row_286 is None:
            raise SystemExit("missing period 286")

        used = {r["draw_date"] for r in await conn.fetch("SELECT draw_date FROM draws")}
        new_286_date = date(2026, 10, 14)
        while new_286_date in used:
            new_286_date += timedelta(days=1)

        async with conn.transaction():
            await conn.execute(
                "UPDATE draws SET draw_date = $1 WHERE period = 286",
                new_286_date,
            )
            await conn.execute(
                "INSERT INTO draws (draw_date, period, special_number) "
                "VALUES ($1, 285, 19)",
                date(2026, 10, 13),
            )

        recent = await conn.fetch(
            "SELECT period, draw_date, special_number FROM draws "
            "WHERE period >= 284 ORDER BY period"
        )
        print("after", [dict(r) for r in recent])
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
