import os
import time
from collections.abc import AsyncGenerator

import asyncpg

_pool: asyncpg.Pool | None = None
_pool_failed_at: float | None = None

# 建池失败后的退避窗口（秒）。没有这层保护时，每个请求都会重新尝试建池，
# 而 Supabase（Supavisor）对「同一来源 IP 反复认证失败」会触发熔断并**持续续期**，
# 于是服务自己把对方打成了长期封禁。退避让封禁有机会自然过期。
_POOL_RETRY_BACKOFF_SECONDS = 15.0


def database_url() -> str | None:
    return os.getenv("DATABASE_URL") or None


async def init_pool() -> asyncpg.Pool | None:
    global _pool, _pool_failed_at

    url = database_url()
    if not url:
        return None
    if _pool is not None:
        return _pool

    now = time.monotonic()
    if _pool_failed_at is not None and (now - _pool_failed_at) < _POOL_RETRY_BACKOFF_SECONDS:
        raise RuntimeError("数据库连接池不可用（退避中，稍后自动重试）")

    try:
        _pool = await asyncpg.create_pool(
            dsn=url,
            min_size=1,
            max_size=5,
            # Supabase 的 Supavisor 在 transaction 模式（:6543）下不保证连接复用，
            # asyncpg 默认的 prepared statement 缓存会在并发时撞上「已存在」错误；
            # 关掉缓存即可，这个设置同时兼容 session 模式（:5432）。
            statement_cache_size=0,
        )
    except Exception:
        _pool_failed_at = now
        _pool = None
        raise
    _pool_failed_at = None
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
