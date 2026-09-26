import os
from collections.abc import AsyncGenerator

import asyncpg

_pool: asyncpg.Pool | None = None


def database_url() -> str | None:
    return os.getenv("DATABASE_URL") or None


async def init_pool() -> asyncpg.Pool | None:
    global _pool
    url = database_url()
    if not url:
        return None
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=url, min_size=1, max_size=5)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def get_connection() -> AsyncGenerator[asyncpg.Connection, None]:
    pool = await init_pool()
    if pool is None:
        raise RuntimeError("DATABASE_URL is not configured")
    async with pool.acquire() as conn:
        yield conn
