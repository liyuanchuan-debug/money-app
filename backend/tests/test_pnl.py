"""赔率默认值、特码命中兑付与收益仪表汇总。"""

from __future__ import annotations

import asyncio

from services.lottery import DEFAULT_ODDS, DEFAULT_SETTINGS, clamp_settings
from services.pnl import (
    STATUS_PENDING,
    STATUS_SETTLED,
    apply_settlement,
    build_pending_round,
    compute_cost,
    settle_picks,
    summarize_rounds,
)
from repository import MemoryStore, reset_store


def test_default_odds_is_47():
    assert DEFAULT_ODDS == 47
    assert DEFAULT_SETTINGS["odds"] == 47
    assert clamp_settings({})["odds"] == 47
    assert clamp_settings({"odds": 50})["odds"] == 50
    assert clamp_settings({"odds": 0})["odds"] == 1
    assert clamp_settings({"odds": 9999})["odds"] == 999


def test_settle_hit_formula():
    """命中：兑付 = 金额 × 赔率；净盈亏 = 兑付 − 成本。"""
    picks = [
        {"number": 7, "amount": 10, "role": "primary", "role_label": "主推"},
        {"number": 12, "amount": 20, "role": "secondary", "role_label": "次选"},
        {"number": 25, "amount": 20, "role": "defense", "role_label": "防守"},
    ]
    odds = 47
    cost = compute_cost(picks)
    assert cost == 50

    hit = settle_picks(picks, special_number=12, odds=odds)
    assert hit["hit"] is True
    assert hit["cost"] == 50
    assert hit["payout"] == 20 * 47  # 仅中次选那一注
    assert hit["profit"] == 20 * 47 - 50
    assert [p["hit"] for p in hit["picks"]] == [False, True, False]


def test_settle_miss_formula():
    """未命中：兑付 0，净盈亏 = −成本。"""
    picks = [
        {"number": 1, "amount": 15, "role": "primary"},
        {"number": 2, "amount": 15, "role": "secondary"},
    ]
    miss = settle_picks(picks, special_number=49, odds=47)
    assert miss["hit"] is False
    assert miss["cost"] == 30
    assert miss["payout"] == 0
    assert miss["profit"] == -30


def test_memory_store_adopt_then_settle_roundtrip():
    async def scenario() -> dict:
        reset_store()
        store = MemoryStore()
        pending = build_pending_round(
            user_id=1,
            period=101,
            base_period=100,
            picks=[
                {"number": 8, "amount": 10, "role": "primary", "role_label": "主推"},
                {"number": 19, "amount": 40, "role": "secondary", "role_label": "次选"},
            ],
            odds=47,
            mode="even",
        )
        await store.upsert_recommend_round(pending)
        # 开奖入库触发回填
        await store.add_draw(special_number=8, period=101, draw_date=None)
        rounds = await store.list_recommend_rounds(1)
        return rounds[0]

    row = asyncio.run(scenario())
    assert row["status"] == STATUS_SETTLED
    assert row["hit"] is True
    assert row["special_number"] == 8
    assert row["cost"] == 50
    assert row["payout"] == 10 * 47
    assert row["profit"] == 10 * 47 - 50
    assert row["stake_mode"] == "SIMULATED"


def test_summarize_rounds_cumulative():
    settled_hit = apply_settlement(
        build_pending_round(
            user_id=0,
            period=1,
            base_period=0,
            picks=[{"number": 3, "amount": 10, "role": "primary"}],
            odds=47,
            mode="single",
        ),
        special_number=3,
    )
    settled_hit["id"] = 1
    settled_miss = apply_settlement(
        build_pending_round(
            user_id=0,
            period=2,
            base_period=1,
            picks=[{"number": 4, "amount": 10, "role": "primary"}],
            odds=47,
            mode="single",
        ),
        special_number=9,
    )
    settled_miss["id"] = 2
    pending = build_pending_round(
        user_id=0,
        period=3,
        base_period=2,
        picks=[{"number": 5, "amount": 10, "role": "primary"}],
        odds=47,
        mode="single",
    )
    pending["id"] = 3
    pending["status"] = STATUS_PENDING

    body = summarize_rounds(
        [settled_hit, settled_miss, pending],
        current_odds=47,
        recent_limit=10,
    )
    assert body["odds"] == 47
    assert body["summary"]["settled_rounds"] == 2
    assert body["summary"]["hit_rounds"] == 1
    assert body["summary"]["pending_rounds"] == 1
    assert body["summary"]["hit_rate"] == 0.5
    assert body["summary"]["total_cost"] == 20
    assert body["summary"]["total_payout"] == 10 * 47
    assert body["summary"]["total_profit"] == 10 * 47 - 20
    assert len(body["series"]) == 2
    assert body["series"][-1]["cumulative_profit"] == 10 * 47 - 20
    assert body["recent"][0]["stake_mode"] == "SIMULATED"
    joined = " ".join(body["notes"])
    assert "模拟买入" in joined
    assert "稳赚" not in joined
    assert "非真实投注" in joined

def test_stake_mode_defaults_simulated():
    from services.pnl import STAKE_MODE_SIMULATED, STAKE_MODE_LABELS, normalize_stake_mode

    assert normalize_stake_mode(None) == STAKE_MODE_SIMULATED
    assert normalize_stake_mode("real") == "REAL"
    assert normalize_stake_mode("SKIPPED") == "SKIPPED"
    assert normalize_stake_mode("中文") == STAKE_MODE_SIMULATED
    assert STAKE_MODE_LABELS[STAKE_MODE_SIMULATED] == "模拟买入"
    pending = build_pending_round(
        user_id=0,
        period=9,
        base_period=8,
        picks=[{"number": 1, "amount": 10}],
        odds=47,
        mode="single",
    )
    assert pending["stake_mode"] == STAKE_MODE_SIMULATED
