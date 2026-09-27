"""全局测试夹具：**任何测试都不允许连到真实数据库**。

背景（一个真实的踩坑）：``main.py`` 在 import 时调用 ``load_dotenv()``，
而 ``python-dotenv`` 的默认行为是「不覆盖已存在的环境变量」。
如果测试夹具用 ``monkeypatch.delenv("DATABASE_URL")`` 把变量**删掉**，
那么后续第一次 ``from main import app`` 会把 ``backend/.env`` 里的
``DATABASE_URL`` 重新灌回 ``os.environ`` —— 测试就悄悄连上了生产 Supabase，
并在真实数据上跑 ``ensure_schema()`` 与写操作。

    10|所以这里统一改成 ``monkeypatch.setenv("DATABASE_URL", "")``：

- 变量**存在但为空** → ``load_dotenv(override=False)`` 不会覆盖它；
- ``db.database_url()`` 里的 ``os.getenv(...) or None`` 把空串当 None →
  永远不会建连接池 → 恒定退化到 ``MemoryStore``。

各测试模块自己的 autouse 夹具也用同样写法（而不是 delenv），双保险。

除此之外，conftest 还加了一道**硬闸**（``asyncpg.create_pool`` 被替换为抛错），
即便将来某个用例/夹具把 ``DATABASE_URL`` 重新灌成真实 DSN，也会立刻炸掉测试，
而不是静默写进真实库。
"""

from __future__ import annotations

import asyncpg
import pytest


@pytest.fixture(autouse=True)
def force_memory_store(monkeypatch):
    """用空串（而非删除）屏蔽 DATABASE_URL，并硬闸任何真实连接。"""

    async def _blocked_create_pool(*_args, **_kwargs):
        raise RuntimeError(
            "测试进程试图建立真实数据库连接：DATABASE_URL 必须在测试期间为空；"
            "若确实需要连库，请在用例内显式 mock，不要指向真实 DSN。"
        )

    # 硬闸：db.init_pool() 只有在 DATABASE_URL 非空时才会走到这里，
    # 因此正常用例（空串 → 直接返回 None）不受影响。
    monkeypatch.setattr(asyncpg, "create_pool", _blocked_create_pool)
    monkeypatch.setenv("DATABASE_URL", "")
    yield
