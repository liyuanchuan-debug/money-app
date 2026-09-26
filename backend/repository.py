"""数据访问层：优先 Supabase Postgres，未配置 DATABASE_URL 时退化为内存存储。

内存存储仅用于本地预览与联调，进程重启即丢失。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from db import init_pool
from services.lottery import DEFAULT_SETTINGS, clamp_settings

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS records (
        id          BIGSERIAL PRIMARY KEY,
        number      INTEGER NOT NULL CHECK (number BETWEEN 1 AND 49),
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_records_created_at ON records (created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS settings (
        key     TEXT PRIMARY KEY,
        value   TEXT NOT NULL
    )
    """,
)


class Store(ABC):
    backend: str

    @abstractmethod
    async def list_records(self, limit: int | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def add_record(self, number: int) -> dict[str, Any]: ...

    @abstractmethod
    async def clear_records(self) -> int: ...

    @abstractmethod
    async def get_settings(self) -> dict[str, Any]: ...

    @abstractmethod
    async def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]: ...

    async def export_data(self) -> dict[str, Any]:
        records = await self.list_records()
        return {
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "records": [
                {
                    "number": r["number"],
                    "created_at": r["created_at"].isoformat()
                    if isinstance(r["created_at"], datetime)
                    else r["created_at"],
                }
                for r in reversed(records)  # 按时间正序导出
            ],
            "settings": await self.get_settings(),
        }

    @abstractmethod
    async def import_data(self, payload: dict[str, Any], replace: bool) -> dict[str, Any]: ...


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

    async def list_records(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id, number, created_at FROM records ORDER BY created_at DESC, id DESC"
        args: list[Any] = []
        if limit is not None:
            sql += " LIMIT $1"
            args.append(limit)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]

    async def add_record(self, number: int) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO records (number) VALUES ($1) "
                "RETURNING id, number, created_at",
                number,
            )
        return dict(row)

    async def clear_records(self) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM records")
        return int(result.split()[-1])

    async def _raw_settings(self) -> dict[str, str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM settings")
        raw: dict[str, Any] = {}
        for row in rows:
            try:
                raw[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                raw[row["key"]] = row["value"]
        return raw

    async def get_settings(self) -> dict[str, Any]:
        return clamp_settings(await self._raw_settings())

    async def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = await self._raw_settings()
        current.update({k: v for k, v in patch.items() if v is not None})
        merged = clamp_settings(current)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for key in DEFAULT_SETTINGS:
                    await conn.execute(
                        "INSERT INTO settings (key, value) VALUES ($1, $2) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                        key,
                        json.dumps(merged[key]),
                    )
        return merged

    async def import_data(self, payload: dict[str, Any], replace: bool) -> dict[str, Any]:
        records = payload.get("records") or []
        settings = payload.get("settings") or {}

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if replace:
                    await conn.execute("DELETE FROM records")
                for item in records:
                    number = int(item["number"])
                    created = item.get("created_at")
                    if created:
                        await conn.execute(
                            "INSERT INTO records (number, created_at) VALUES ($1, $2)",
                            number,
                            datetime.fromisoformat(created),
                        )
                    else:
                        await conn.execute(
                            "INSERT INTO records (number) VALUES ($1)", number
                        )
                for key in DEFAULT_SETTINGS:
                    if key in settings:
                        await conn.execute(
                            "INSERT INTO settings (key, value) VALUES ($1, $2) "
                            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                            key,
                            json.dumps(settings[key]),
                        )

        return {
            "imported_records": len(records),
            "replaced": replace,
            "settings": await self.get_settings(),
        }


# --------------------------------------------------------------------------- #
# In-memory（未配置 DATABASE_URL 时的本地回退）
# --------------------------------------------------------------------------- #
class MemoryStore(Store):
    backend = "memory"

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []
        self._next_id = 1
        self._settings: dict[str, Any] = dict(DEFAULT_SETTINGS)

    async def list_records(self, limit: int | None = None) -> list[dict[str, Any]]:
        ordered = sorted(
            self._records, key=lambda r: (r["created_at"], r["id"]), reverse=True
        )
        return ordered[:limit] if limit is not None else ordered

    async def add_record(self, number: int) -> dict[str, Any]:
        record = {
            "id": self._next_id,
            "number": number,
            "created_at": datetime.now(timezone.utc),
        }
        self._next_id += 1
        self._records.append(record)
        return dict(record)

    async def clear_records(self) -> int:
        count = len(self._records)
        self._records.clear()
        return count

    async def get_settings(self) -> dict[str, Any]:
        return clamp_settings(self._settings)

    async def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        self._settings.update({k: v for k, v in patch.items() if v is not None})
        self._settings = clamp_settings(self._settings)
        return dict(self._settings)

    async def import_data(self, payload: dict[str, Any], replace: bool) -> dict[str, Any]:
        records = payload.get("records") or []
        if replace:
            self._records.clear()
        for item in records:
            created = item.get("created_at")
            self._records.append(
                {
                    "id": self._next_id,
                    "number": int(item["number"]),
                    "created_at": datetime.fromisoformat(created)
                    if created
                    else datetime.now(timezone.utc),
                }
            )
            self._next_id += 1
        self._settings.update(
            {k: v for k, v in (payload.get("settings") or {}).items() if k in DEFAULT_SETTINGS}
        )
        self._settings = clamp_settings(self._settings)
        return {
            "imported_records": len(records),
            "replaced": replace,
            "settings": dict(self._settings),
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
