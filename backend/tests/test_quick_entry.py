"""快捷录入 + 「draws 是特码唯一事实来源」回归测试。

覆盖：
- ``Store.add_draw`` 自动补期号（max(period)+1）/ 日期（今天 UTC+8）、
  同期号或同日覆盖（用户改错号不产生重复行）、1..49 校验、期号 / 日期冲突报错；
- ``POST /api/draws/quick`` 与 ``GET /api/draws/next-period`` 的返回形状；
- **回归**：``/api/recommend`` 的历史序列来自 ``draws``，不再读已废弃的 ``records``
  （同一 latest 号下，推荐结果必须随 draws 历史变化）；
- ``/api/history`` 读 ``draws``；``/api/records`` 已下线；
- 导出 / 导入只搬 ``draws``（version 2）。

全部用例不依赖真实数据库（强制内存存储）。
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import repository
from repository import MemoryStore, plan_quick_draw
from services.mark_six import today_local

FIXED_TODAY = date(2026, 9, 27)


@pytest.fixture(autouse=True)
def memory_store_env(monkeypatch):
    """强制内存存储，避免测试进程连到真实数据库。

    注意用**空串**而不是 delenv：main.py 在 import 时 load_dotenv() 会把
    backend/.env 里的 DATABASE_URL 灌回来（python-dotenv 不覆盖已存在的变量，
    但会补上被删掉的）。详见 tests/conftest.py 的说明。
    """
    monkeypatch.setenv("DATABASE_URL", "")
    yield


@pytest.fixture()
def client():
    from main import app

    with TestClient(app) as test_client:
        yield test_client


def _seed_store(*, periods_dates: list[tuple[int, date]], specials: list[int]) -> MemoryStore:
    store = MemoryStore()

    async def scenario():
        await store.import_draws(
            [
                {"draw_date": d, "period": p, "special_number": n}
                for (p, d), n in zip(periods_dates, specials)
            ]
        )

    asyncio.run(scenario())
    return store


# --------------------------------------------------------------------------- #
# 1. 纯函数：写入计划（两种存储共用，保证语义一致）
# --------------------------------------------------------------------------- #
def test_plan_uses_today_when_date_omitted(monkeypatch):
    monkeypatch.setattr(repository, "today_local", lambda: FIXED_TODAY)
    plan = plan_quick_draw([], 21, None, None)

    assert plan == {
        "action": "insert",
        "target_id": None,
        "draw_date": FIXED_TODAY,
        "period": 1,  # 空表第一期
        "special_number": 21,
    }


@pytest.mark.parametrize("number", [0, -1, 50, 99])
def test_plan_rejects_out_of_range(number: int):
    with pytest.raises(ValueError):
        plan_quick_draw([], number, None, FIXED_TODAY)


# --------------------------------------------------------------------------- #
# 2. MemoryStore：自动补期号 / 日期
# --------------------------------------------------------------------------- #
def test_add_draw_autofills_period_and_date(monkeypatch):
    monkeypatch.setattr(repository, "today_local", lambda: FIXED_TODAY)
    store = _seed_store(
        periods_dates=[(268, date(2026, 9, 24)), (269, date(2026, 9, 26))],
        specials=[7, 22],
    )

    async def scenario():
        suggested = await store.suggest_next_period()
        draw = await store.add_draw(19)
        return suggested, draw, await store.list_draws()

    suggested, draw, draws = asyncio.run(scenario())

    assert suggested == 270  # max(period) + 1
    assert draw["period"] == 270
    assert draw["draw_date"] == FIXED_TODAY
    assert draw["special_number"] == 19
    assert len(draws) == 3
    assert draws[0]["special_number"] == 19  # 最新在前


def test_suggest_next_period_defaults_to_one_on_empty_store():
    store = MemoryStore()
    assert asyncio.run(store.suggest_next_period()) == 1


def test_add_draw_upserts_when_period_already_exists(monkeypatch):
    monkeypatch.setattr(repository, "today_local", lambda: FIXED_TODAY)
    store = _seed_store(periods_dates=[(269, date(2026, 9, 26))], specials=[22])

    async def scenario():
        first = await store.add_draw(19, period=270, draw_date=date(2026, 9, 27))
        second = await store.add_draw(31, period=270, draw_date=date(2026, 9, 27))
        return first, second, await store.list_draws()

    first, second, draws = asyncio.run(scenario())

    assert first["id"] == second["id"]  # 同一条被更新，而不是新增
    assert len(draws) == 2  # 269 + 270
    assert draws[0]["special_number"] == 31
    assert draws[0]["period"] == 270


def test_add_draw_repeat_same_day_does_not_bump_period(monkeypatch):
    """连点两次保存（不带期号）只应修正同一天那一期，不会涨成两期。"""
    monkeypatch.setattr(repository, "today_local", lambda: FIXED_TODAY)
    store = _seed_store(periods_dates=[(269, date(2026, 9, 26))], specials=[22])

    async def scenario():
        first = await store.add_draw(19, draw_date=FIXED_TODAY)
        second = await store.add_draw(31, draw_date=FIXED_TODAY)
        return first, second, await store.list_draws()

    first, second, draws = asyncio.run(scenario())

    assert first["id"] == second["id"]
    assert second["period"] == 270  # 期号保持不变
    assert len(draws) == 2
    assert draws[0]["special_number"] == 31


@pytest.mark.parametrize("number", [0, 50])
def test_add_draw_rejects_out_of_range(number: int):
    store = MemoryStore()

    async def scenario():
        await store.add_draw(number)

    with pytest.raises(ValueError):
        asyncio.run(scenario())
    assert asyncio.run(store.list_draws()) == []


def test_add_draw_correcting_period_without_date_keeps_existing_date(monkeypatch):
    """只改号码、不传日期时，不能把历史那一期搬成今天。"""
    monkeypatch.setattr(repository, "today_local", lambda: FIXED_TODAY)
    store = _seed_store(periods_dates=[(269, date(2026, 9, 26))], specials=[22])

    async def scenario():
        return await store.add_draw(31, period=269)

    draw = asyncio.run(scenario())

    assert draw["draw_date"] == date(2026, 9, 26)
    assert draw["special_number"] == 31
    assert draw["period"] == 269


def test_add_draw_rejects_period_conflict(monkeypatch):
    """显式改期号时若与另一期撞号，必须明确报错而不是静默覆盖。"""
    monkeypatch.setattr(repository, "today_local", lambda: FIXED_TODAY)
    store = _seed_store(
        periods_dates=[(269, date(2026, 9, 26)), (270, date(2026, 9, 27))],
        specials=[22, 31],
    )

    async def scenario():
        # 目标日期是 269 那条，但又要求改成 270 → 与 270 那条撞号
        await store.add_draw(19, period=270, draw_date=date(2026, 9, 26))

    with pytest.raises(ValueError, match="期号 270"):
        asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 3. HTTP：快捷录入
# --------------------------------------------------------------------------- #
def test_quick_endpoint_autofills_and_decorates(client, monkeypatch):
    monkeypatch.setattr(repository, "today_local", lambda: FIXED_TODAY)
    client.post(
        "/api/draws/quick",
        json={"special_number": 22, "period": 269, "draw_date": "2026-09-26"},
    )

    response = client.post("/api/draws/quick", json={"special_number": 19})
    assert response.status_code == 200
    body = response.json()

    assert body["period"] == 270  # 自动 max(period)+1
    assert body["draw_date"] == FIXED_TODAY.isoformat()  # 自动今天（UTC+8）
    assert body["special_number"] == 19
    assert body["zodiac"] and body["zodiac_label"]  # 生肖即刻可用
    assert len(client.get("/api/draws").json()) == 2


def test_quick_endpoint_returns_zodiac_for_explicit_date(client):
    body = client.post(
        "/api/draws/quick",
        json={"special_number": 22, "period": 269, "draw_date": "2026-09-26"},
    ).json()

    assert body["draw_date"] == "2026-09-26"
    assert body["zodiac"] == "ROOSTER"
    assert body["zodiac_label"] == "鸡"


def test_quick_endpoint_overwrites_same_period(client):
    first = client.post(
        "/api/draws/quick", json={"special_number": 22, "period": 269}
    ).json()
    second = client.post(
        "/api/draws/quick", json={"special_number": 9, "period": 269}
    ).json()

    assert first["id"] == second["id"]
    assert second["special_number"] == 9
    assert len(client.get("/api/draws").json()) == 1


@pytest.mark.parametrize("payload", [{"special_number": 0}, {"special_number": 50}, {}])
def test_quick_endpoint_validates_special_number(client, payload):
    assert client.post("/api/draws/quick", json=payload).status_code == 422


def test_next_period_endpoint_prefills_form(client, monkeypatch):
    # routers.draws 直接 import 了 today_local，因此要 patch 该模块的引用
    monkeypatch.setattr("routers.draws.today_local", lambda: FIXED_TODAY)

    empty = client.get("/api/draws/next-period").json()
    assert empty["suggested_period"] == 1
    assert empty["has_draws"] is False
    assert empty["latest_draw"] is None
    assert empty["suggested_draw_date"] == FIXED_TODAY.isoformat()
    assert empty["timezone"] == "Asia/Shanghai"

    client.post(
        "/api/draws/quick",
        json={"special_number": 22, "period": 269, "draw_date": "2026-09-26"},
    )

    body = client.get("/api/draws/next-period").json()
    assert body["suggested_period"] == 270
    assert body["has_draws"] is True
    assert body["latest_draw"]["special_number"] == 22
    assert body["latest_draw"]["zodiac_label"] == "鸡"


# --------------------------------------------------------------------------- #
# 4. 回归：/api/recommend 的历史序列来自 draws
# --------------------------------------------------------------------------- #
def test_recommend_number_branch_uses_draws_history(client):
    """前瞻观察号：历史必须来自 draws。

    latest=25 时小波动池里差值最小的是 24(差1) 与 26(差1)。
    若历史包含 24（遗漏计数 > 0），引擎应把 24 排到后面 → 主推 26。
    这正是「推荐随 draws 历史变化」的证明（早期版本读空 records，永远出 24）。
    """
    client.post(
        "/api/draws/quick",
        json={"special_number": 24, "period": 1, "draw_date": "2026-06-01"},
    )
    client.post(
        "/api/draws/quick",
        json={"special_number": 12, "period": 2, "draw_date": "2026-06-02"},
    )

    # 本用例只证明「历史序列来自 draws」：关闭避冷加权，
    # 否则刚出现过的 24 会按「非冷号优先」被直接选中（与空历史时的结果相同），
    # 反而看不出历史对遗漏排序的影响。
    client.put("/api/settings", json={"avoid_cold_enabled": False})

    body = client.post("/api/recommend", json={"number": 25, "mode": "single"}).json()

    # 观察号叠在真实序列之上 → 它之前的一期是「当前最新一期」
    assert body["previous"] == 12
    assert [pick["number"] for pick in body["picks"]] == [26]


def test_recommend_number_branch_without_history_still_works(client):
    """没有任何 draws 时前瞻观察号也必须可用（previous=None，按平均分散）。"""
    body = client.post("/api/recommend", json={"number": 25, "mode": "single"}).json()

    assert body["previous"] is None
    # 无历史 → 所有号码遗漏计数为 0 → 差值相同时按号码升序 → 24
    assert [pick["number"] for pick in body["picks"]] == [24]
    assert body["notes"]  # 引擎提示「历史不足两期」


def test_recommend_without_number_uses_draws(client):
    client.post(
        "/api/draws/quick",
        json={"special_number": 24, "period": 1, "draw_date": "2026-06-01"},
    )
    client.post(
        "/api/draws/quick",
        json={"special_number": 12, "period": 2, "draw_date": "2026-06-02"},
    )

    body = client.post("/api/recommend", json={}).json()

    assert body["latest"] == 12
    assert body["previous"] == 24


def test_recommend_without_any_draws_returns_400(client):
    response = client.post("/api/recommend", json={})
    assert response.status_code == 400
    assert "开奖总表" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# 5. /api/history 读 draws
# --------------------------------------------------------------------------- #
def test_history_reads_draws(client):
    client.post(
        "/api/draws/quick",
        json={"special_number": 24, "period": 1, "draw_date": "2026-06-01"},
    )
    client.post(
        "/api/draws/quick",
        json={"special_number": 12, "period": 2, "draw_date": "2026-06-02"},
    )

    history = client.get("/api/history").json()

    assert len(history) == 2
    newest, oldest = history
    # number 仍是兼容字段名（值 = 特码），另附 draw_date / period
    assert newest["number"] == 12
    assert newest["draw_date"] == "2026-06-02"
    assert newest["period"] == 2
    assert newest["diff"] == 12  # |12 - 24|
    assert newest["wave_type"] == "normal"
    assert newest["wave_label"] == "常规波动"
    assert oldest["number"] == 24
    assert oldest["wave_type"] is None  # 没有更早的一期


# --------------------------------------------------------------------------- #
# 6. records 已下线；导出 / 导入只搬 draws
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("method", ["get", "post", "delete"])
def test_records_routes_are_gone(client, method: str):
    kwargs = {"json": {"number": 21}} if method == "post" else {}
    assert getattr(client, method)("/api/records", **kwargs).status_code == 404
    assert client.delete("/api/records/1").status_code == 404


def test_export_and_import_only_carry_draws(client):
    client.post(
        "/api/draws/quick",
        json={"special_number": 24, "period": 1, "draw_date": "2026-06-01"},
    )
    client.post(
        "/api/draws/quick",
        json={"special_number": 12, "period": 2, "draw_date": "2026-06-02"},
    )

    exported = client.get("/api/export").json()
    assert exported["version"] == 2
    assert "records" not in exported
    assert [item["special_number"] for item in exported["draws"]] == [24, 12]  # 日期正序
    assert exported["storage"] == "memory"

    # 空库导入同一份导出文件 → 2 期
    assert client.delete("/api/draws").json() == {"deleted": 2}
    imported = client.post("/api/import", json=exported)
    assert imported.status_code == 200
    assert imported.json()["imported_draws"] == 2
    assert len(client.get("/api/draws").json()) == 2


def test_import_rejects_legacy_records_payload(client):
    response = client.post("/api/import", json={"records": [{"number": 21}]})
    assert response.status_code == 422
    assert "draws" in response.json()["detail"]


def test_import_rejects_out_of_range_special_number(client):
    response = client.post(
        "/api/import",
        json={
            "draws": [
                {"draw_date": "2026-06-01", "period": 1, "special_number": 50}
            ]
        },
    )
    assert response.status_code == 422
    assert "特码越界" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# 7. 时区口径：默认日期用 UTC+8
# --------------------------------------------------------------------------- #
def test_default_date_uses_utc_plus_eight(monkeypatch):
    """服务器跑在 UTC 时，UTC+8 的凌晨不能被算成前一天。"""
    from datetime import datetime, timezone as tz

    import services.mark_six as mark_six

    # UTC 2026-09-26 17:30 == UTC+8 2026-09-27 01:30
    fake_now = datetime(2026, 9, 26, 17, 30, tzinfo=tz.utc)
    monkeypatch.setattr(mark_six, "datetime", _FrozenDatetime(fake_now))

    assert today_local() == date(2026, 9, 27)


class _FrozenDatetime:
    """替身：只实现 today_local() 用到的 datetime.now(tz)。"""

    def __init__(self, now):
        self._now = now

    def now(self, tz=None):
        if tz is None:
            return self._now
        return self._now.astimezone(tz)
