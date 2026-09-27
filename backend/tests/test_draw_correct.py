"""纠正已保存开奖：覆盖特码 + 重算采用快照盈亏 + 审计。"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
from fastapi.testclient import TestClient

from repository import DRAW_ACTION_CORRECT, MemoryStore, reset_store
from services.pnl import STATUS_SETTLED, build_pending_round


@pytest.fixture(autouse=True)
def memory_store_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    reset_store()
    yield
    reset_store()


@pytest.fixture()
def client():
    from main import app

    with TestClient(app) as test_client:
        yield test_client


def test_correct_draw_resettles_pnl():
    """纠正特码后：原命中变未中（或反之），盈亏按新特码重算。"""

    async def scenario() -> dict:
        reset_store()
        store = MemoryStore()
        draw = await store.add_draw(
            special_number=8, period=200, draw_date=date(2026, 9, 20)
        )
        pending = build_pending_round(
            user_id=1,
            period=200,
            base_period=199,
            picks=[
                {"number": 8, "amount": 10, "role": "primary", "role_label": "主推"},
                {"number": 19, "amount": 40, "role": "secondary", "role_label": "次选"},
            ],
            odds=47,
            mode="even",
        )
        await store.upsert_recommend_round(pending)
        # 首次结算（命中 8）
        await store.settle_recommend_rounds_for_draw(200, 8, date(2026, 9, 20))
        before = (await store.list_recommend_rounds(1))[0]
        assert before["hit"] is True
        assert before["payout"] == 10 * 47

        # 纠正为 19 → 应命中次选
        result = await store.correct_draw(
            int(draw["id"]),
            special_number=19,
            actor_user_id=9,
        )
        after = (await store.list_recommend_rounds(1))[0]
        return {"result": result, "after": after, "before": before}

    out = asyncio.run(scenario())
    result = out["result"]
    after = out["after"]

    assert result["action"] == DRAW_ACTION_CORRECT
    assert result["old_special_number"] == 8
    assert result["new_special_number"] == 19
    assert result["resettled_rounds"] == 1
    assert result["correction_id"] is not None

    assert after["status"] == STATUS_SETTLED
    assert after["hit"] is True
    assert after["special_number"] == 19
    assert after["payout"] == 40 * 47
    assert after["profit"] == 40 * 47 - 50


def test_correct_draw_miss_to_hit_via_api(client):
    """HTTP：纠正接口返回新旧号，并重算该期快照。"""
    first = client.post(
        "/api/draws/quick",
        json={"special_number": 5, "period": 301, "draw_date": "2026-09-01"},
    )
    assert first.status_code == 200
    draw_id = first.json()["id"]

    async def seed() -> None:
        from repository import get_store

        store = await get_store()
        await store.upsert_recommend_round(
            build_pending_round(
                user_id=0,
                period=301,
                base_period=300,
                picks=[{"number": 11, "amount": 50, "role": "primary"}],
                odds=47,
                mode="single",
            )
        )
        await store.settle_recommend_rounds_for_draw(301, 5, date(2026, 9, 1))

    asyncio.run(seed())

    corrected = client.post(
        f"/api/draws/{draw_id}/correct",
        json={"special_number": 11},
    )
    assert corrected.status_code == 200, corrected.text
    body = corrected.json()
    assert body["old_special_number"] == 5
    assert body["new_special_number"] == 11
    assert body["period"] == 301
    assert body["resettled_rounds"] == 1
    assert body["action"] == "CORRECT"
    assert body["draw"]["special_number"] == 11

    async def check() -> dict:
        from repository import get_store

        store = await get_store()
        rows = await store.list_recommend_rounds(0)
        return rows[0]

    row = asyncio.run(check())
    assert row["hit"] is True
    assert row["payout"] == 50 * 47
    assert row["profit"] == 50 * 47 - 50


def test_correct_draw_not_found(client):
    response = client.post("/api/draws/999999/correct", json={"special_number": 7})
    assert response.status_code == 404
