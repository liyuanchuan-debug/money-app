from datetime import datetime, timezone

from fastapi import APIRouter

from db import init_pool
from models.item import Item

router = APIRouter(prefix="/api")

SAMPLE_ITEMS = [
    Item(id=1, name="Wave Starter", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
    Item(id=2, name="Money Flow", created_at=datetime(2026, 1, 15, tzinfo=timezone.utc)),
    Item(id=3, name="Tide Tracker", created_at=datetime(2026, 2, 1, tzinfo=timezone.utc)),
]


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/items", response_model=list[Item])
async def list_items():
    pool = await init_pool()
    if pool is None:
        return SAMPLE_ITEMS

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, created_at FROM items ORDER BY id ASC"
            )
        if not rows:
            return SAMPLE_ITEMS
        return [
            Item(id=row["id"], name=row["name"], created_at=row["created_at"])
            for row in rows
        ]
    except Exception:
        return SAMPLE_ITEMS
