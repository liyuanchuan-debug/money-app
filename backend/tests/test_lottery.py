"""算法回归测试。

覆盖：肖号分组、波动分类边界、全量推荐扫描、缺类补足、侧重角色分配、
派生大跳下限、金额分配与注数钳制。所有用例均不依赖真实数据库。
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date, datetime, timedelta

import pytest

from services.lottery import (
    AVOID_COLD_FLOOR_WEIGHT,
    DEFAULT_AMOUNT_UNIT,
    DEFAULT_AVOID_COLD_DAYS,
    DEFAULT_AVOID_COLD_ENABLED,
    DEFAULT_SETTINGS,
    DEFAULT_TOTAL_AMOUNT,
    MODE_EVEN,
    MODE_RANDOM,
    MODE_SINGLE,
    MODE_WEIGHTED,
    MODES,
    PICK_COUNT_MAX,
    PICK_COUNT_MIN,
    WAVE_BIG,
    WAVE_NORMAL,
    WAVE_SMALL,
    allocate_amounts,
    amount_seed_key,
    apply_avoid_cold_amounts,
    assign_roles,
    avoid_cold_weight,
    build_candidate_pools,
    build_copy_text,
    classify_wave,
    clamp_settings,
    compute_days_since_last,
    format_number,
    is_avoid_cold_number,
    order_pool,
    random_allocation,
    random_amounts,
    recommend,
    resolve_zodiac_date,
    with_derived_settings,
    zodiac_numbers,
)
from services.mark_six import ZODIAC_LABELS, zodiac_of

DEFAULT_SMALL_MAX = 10
DEFAULT_NORMAL_MAX = 30
BET_UNIT = 10
PREVIOUS_VALUES = [None, 1, 25, 49]
PICK_COUNTS = [1, 2, 3, 5, 10]


@pytest.fixture(autouse=True)
def memory_store_env(monkeypatch):
    """强制内存存储，避免测试进程连到真实数据库。

    注意用**空串**而不是 delenv：main.py 在 import 时 load_dotenv() 会把
    backend/.env 里的 DATABASE_URL 灌回来（python-dotenv 不覆盖已存在的变量，
    但会补上被删掉的）。详见 tests/conftest.py 的说明。
    """
    monkeypatch.setenv("DATABASE_URL", "")
    # 本模块的接口用例按「开发旁路」书写：显式关闭鉴权开关，避免本地 .env 里的
    # AUTH_ENFORCED=true 让匿名 GET/PUT 变成 401（auth_enforced() 在请求时读环境变量）。
    monkeypatch.setenv("AUTH_ENFORCED", "false")
    yield


# --------------------------------------------------------------------------- #
# 1. 肖号分组
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (1, [1, 13, 25, 37, 49]),
        (2, [2, 14, 26, 38]),
        (49, [1, 13, 25, 37, 49]),
    ],
)
def test_zodiac_numbers(number: int, expected: list[int]):
    assert zodiac_numbers(number) == expected


# --------------------------------------------------------------------------- #
# 2. 波动分类边界
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("diff", "expected"),
    [
        (0, WAVE_SMALL),
        (10, WAVE_SMALL),
        (11, WAVE_NORMAL),
        (30, WAVE_NORMAL),
        (31, WAVE_BIG),
        (48, WAVE_BIG),
    ],
)
def test_classify_wave_boundaries(diff: int, expected: str):
    assert classify_wave(diff, DEFAULT_SMALL_MAX, DEFAULT_NORMAL_MAX) == expected


# --------------------------------------------------------------------------- #
# 3. 全量扫描
# --------------------------------------------------------------------------- #
def _sweep_cases() -> list[tuple[int, int | None, str, int]]:
    cases: list[tuple[int, int | None, str, int]] = []
    for latest in range(1, 50):
        for previous in PREVIOUS_VALUES:
            for mode in MODES:
                for pick_count in PICK_COUNTS:
                    cases.append((latest, previous, mode, pick_count))
    return cases


@pytest.mark.parametrize(("latest", "previous", "mode", "pick_count"), _sweep_cases())
def test_recommend_sweep(
    latest: int, previous: int | None, mode: str, pick_count: int
):
    history = [latest] + ([previous] if previous is not None else [])
    result = recommend(
        latest=latest,
        previous=previous,
        history_numbers=history,
        settings={
            "total_amount": BET_UNIT * pick_count,
            "pick_count": pick_count,
            # 本用例校验分配口径（旧行为）：显式关闭避冷权重，
            # 否则默认开启的避冷权重会压低金额（另有专门用例覆盖新行为）。
            "avoid_cold_enabled": False,
        },
        mode=mode,
    )

    picks = result["picks"]
    expected_count = 1 if mode == MODE_SINGLE else pick_count

    assert len(picks) == expected_count
    # 预算真值 = total_amount（单挑是把整份预算押在一个号上）
    expected_total = BET_UNIT * pick_count
    assert result["total_amount"] == expected_total
    assert sum(pick["amount"] for pick in picks) == expected_total
    # 所有模式：每注是金额最小单位的整数倍，且至少 1 个单位
    unit = result["amount_unit"]
    assert all(pick["amount"] % unit == 0 for pick in picks)
    assert all(pick["amount"] >= unit for pick in picks)

    latest_zodiac = set(zodiac_numbers(latest))
    seen: set[int] = set()
    for pick in picks:
        number = pick["number"]
        assert 1 <= number <= 49
        assert number != latest
        # 默认不避开重肖：同肖号允许入选；仅最新号本身始终排除
        assert number not in seen
        seen.add(number)
        assert pick["diff"] == abs(number - latest)
        assert pick["wave_type"] == classify_wave(
            pick["diff"], DEFAULT_SMALL_MAX, DEFAULT_NORMAL_MAX
        )
        assert pick["is_repeat_zodiac"] is (number in latest_zodiac)

    assert sum(1 for pick in picks if pick["role"] == "primary") == 1


# --------------------------------------------------------------------------- #
# 4. 缺类上报但仍补足注数（决策 2）
# --------------------------------------------------------------------------- #
def test_missing_wave_reported_and_filled():
    result = recommend(
        latest=21,
        previous=1,
        history_numbers=[21, 1],
        settings={"pick_count": 5},
    )

    assert {item["type"] for item in result["missing_waves"]} == {WAVE_BIG}
    assert len(result["picks"]) == 5
    assert all(pick["wave_type"] != WAVE_BIG for pick in result["picks"])


# --------------------------------------------------------------------------- #
# 5. 侧重（回补）角色分配（决策 1）
# --------------------------------------------------------------------------- #
def test_prev_small_focus_puts_normal_first():
    # |10 - 5| = 5 → 上期小波动，侧重顺序 [常规, 大跳, 小波动]
    result = recommend(latest=10, previous=5, history_numbers=[10, 5])

    assert result["prev_wave"]["type"] == WAVE_SMALL
    primary = next(pick for pick in result["picks"] if pick["role"] == "primary")
    assert primary["wave_type"] == WAVE_NORMAL


def test_prev_big_focus_puts_small_first():
    # |1 - 40| = 39 → 上期大跳，侧重顺序 [小波动, 常规, 大跳]
    result = recommend(latest=1, previous=40, history_numbers=[1, 40])

    assert result["prev_wave"]["type"] == WAVE_BIG
    primary = next(pick for pick in result["picks"] if pick["role"] == "primary")
    assert primary["wave_type"] == WAVE_SMALL


# --------------------------------------------------------------------------- #
# 6. 派生大跳下限（决策 3）
# --------------------------------------------------------------------------- #
def test_settings_big_min_derived_and_not_writable():
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        body = client.get("/api/settings").json()
        assert body["big_min"] == body["normal_max"] + 1

        response = client.put(
            "/api/settings", json={"normal_max": 25, "big_min": 999}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["normal_max"] == 25
        assert body["big_min"] == 26

        # 再次读取仍是派生值，999 未被持久化
        assert client.get("/api/settings").json()["big_min"] == 26


def test_memory_store_ignores_derived_keys():
    from repository import MemoryStore

    store = MemoryStore()

    async def scenario() -> dict:
        await store.update_settings({"normal_max": 25, "big_min": 999})
        return await store.get_settings()

    settings = asyncio.run(scenario())

    assert settings["normal_max"] == 25
    assert "big_min" not in store._settings  # noqa: SLF001 - 直接校验落库内容


def test_recommend_reports_big_min():
    result = recommend(latest=7, previous=None, history_numbers=[7])
    assert result["settings"]["big_min"] == result["settings"]["normal_max"] + 1


# --------------------------------------------------------------------------- #
# 7. 金额分配
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("pick_count", list(range(1, 11)))
def test_allocate_amounts_sums_to_total(mode: str, pick_count: int):
    effective = 1 if mode == MODE_SINGLE else pick_count
    roles = assign_roles(effective)
    # 单挑：唯一 1 注拿走按「配置注数」算出的整份预算
    total = BET_UNIT * pick_count

    amounts = allocate_amounts(mode, roles, total, amount_unit=DEFAULT_AMOUNT_UNIT)

    assert len(amounts) == effective
    assert sum(amounts) == total
    assert all(amount % DEFAULT_AMOUNT_UNIT == 0 for amount in amounts)
    assert all(amount >= DEFAULT_AMOUNT_UNIT for amount in amounts)
    if mode == MODE_WEIGHTED and effective >= 3:
        assert amounts[0] == max(amounts)


# --------------------------------------------------------------------------- #
# 8. 注数钳制
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", [0, -1, 999, 11, 100])
def test_pick_count_clamped(raw: int):
    value = clamp_settings({"pick_count": raw})["pick_count"]
    assert PICK_COUNT_MIN <= value <= PICK_COUNT_MAX


def test_pick_count_default():
    assert DEFAULT_SETTINGS["pick_count"] == 6
    assert clamp_settings({})["pick_count"] == 6
    assert clamp_settings(None)["pick_count"] == 6


@pytest.mark.parametrize("raw", [0, -1, 999])
def test_recommend_tolerates_bad_pick_count(raw: int):
    result = recommend(
        latest=7,
        previous=None,
        history_numbers=[7],
        settings={"pick_count": raw},
    )
    assert PICK_COUNT_MIN <= len(result["picks"]) <= PICK_COUNT_MAX


# --------------------------------------------------------------------------- #
# 9. 文档规格钉死（默认配置下的三种筹码模式）
# --------------------------------------------------------------------------- #
def test_even_allocation_uses_amount_unit_multiples():
    """均注：先分单位再 × unit；余数补给前几注（10 单位 / 6 注 → 2,2,2,2,1,1）。"""
    roles = assign_roles(6)
    amounts = allocate_amounts(
        MODE_EVEN, roles, 50, amount_unit=5
    )
    assert amounts == [10, 10, 10, 10, 5, 5]
    assert sum(amounts) == 50
    assert all(a % 5 == 0 and a >= 5 for a in amounts)


def test_weighted_and_single_respect_amount_unit():
    roles = assign_roles(6)
    weighted = allocate_amounts(MODE_WEIGHTED, roles, 50, amount_unit=5)
    assert weighted == [25, 5, 5, 5, 5, 5]
    assert sum(weighted) == 50
    assert all(a % 5 == 0 and a >= 5 for a in weighted)

    single = allocate_amounts(MODE_SINGLE, assign_roles(1), 50, amount_unit=5)
    assert single == [50]


def test_documented_default_amounts():
    """默认配置（最大投注 50 元、单位 5、6 注）下三种模式的金额必须与产品默认一致。

    均注：50/5=10 个单位分给 6 注 → 2/2/2/2/1/1 单位 → 10/10/10/10/5/5。
    侧重：主推约 4/6，但须保证每注 ≥1 单位 → 5/1/1/1/1/1 → 25/5/5/5/5/5。
    单挑：整份预算押在一个号上 → 50。
    """
    history = [15, 20]
    expected = {
        MODE_EVEN: [10, 10, 10, 10, 5, 5],
        MODE_WEIGHTED: [25, 5, 5, 5, 5, 5],
        MODE_SINGLE: [50],
    }

    for mode, amounts in expected.items():
        result = recommend(
            latest=20,
            previous=15,
            history_numbers=history,
            mode=mode,
            # 校验产品默认分配口径（旧行为）：关闭避冷权重
            settings={"avoid_cold_enabled": False},
        )
        got = [pick["amount"] for pick in result["picks"]]
        assert got == amounts, f"{mode}: 期望 {amounts}，实际 {got}"
        assert result["total_amount"] == 50
        assert all(amount % 5 == 0 for amount in got)


def test_single_mode_bets_whole_total_amount():
    """单挑注数恒为 1，金额 = 整份最大投注金额（与配置注数无关）。"""
    for total, expected_amount in ((10, 10), (30, 30), (50, 50), (100, 100)):
        result = recommend(
            latest=20,
            previous=15,
            history_numbers=[15, 20],
            # 关闭避冷权重：本用例校验单挑的旧分配口径
            settings={"total_amount": total, "pick_count": 3, "avoid_cold_enabled": False},
            mode=MODE_SINGLE,
        )
        assert len(result["picks"]) == 1
        assert result["picks"][0]["amount"] == expected_amount
        assert result["total_amount"] == expected_amount
        num = result["picks"][0]["number"]
        assert result["copy_text"] == f"{num:02d}：{expected_amount}元；\n合计：{expected_amount}元。"


def test_single_mode_budget_ignores_configured_pick_count():
    """单挑的预算来自 total_amount，不再随 pick_count 缩放（预算只有一个真值）。"""
    amounts = set()
    for configured in (1, 3, 5, 10):
        result = recommend(
            latest=20,
            previous=15,
            history_numbers=[15, 20],
            settings={"total_amount": 30, "pick_count": configured, "avoid_cold_enabled": False},
            mode=MODE_SINGLE,
        )
        amounts.add(result["picks"][0]["amount"])
    assert amounts == {30}


# --------------------------------------------------------------------------- #
# 10. 最大投注金额 / 金额最小单位（random 随机分配）
# --------------------------------------------------------------------------- #
def test_default_settings_expose_total_amount_and_unit():
    assert DEFAULT_SETTINGS["total_amount"] == DEFAULT_TOTAL_AMOUNT == 50
    assert DEFAULT_SETTINGS["amount_unit"] == DEFAULT_AMOUNT_UNIT == 5
    assert DEFAULT_SETTINGS["exclude_repeat_zodiac"] is False
    assert clamp_settings({})["exclude_repeat_zodiac"] is False
    assert clamp_settings({"exclude_repeat_zodiac": True})["exclude_repeat_zodiac"] is True
    # 缺失 / 脏值回退为 False（与新产品默认一致）
    assert clamp_settings({"exclude_repeat_zodiac": None})["exclude_repeat_zodiac"] is False
    assert clamp_settings({"exclude_repeat_zodiac": "false"})["exclude_repeat_zodiac"] is False
    assert "bet_unit" not in DEFAULT_SETTINGS  # 已降级为派生值，不再是可写真值
    assert MODES == [MODE_EVEN, MODE_WEIGHTED, MODE_SINGLE, MODE_RANDOM]


def test_legacy_row_without_total_amount_falls_back_to_bet_unit_times_count():
    """存量旧行（只有 bet_unit）→ 预算回退为 bet_unit × pick_count，绝不是 0。"""
    cfg = clamp_settings({"bet_unit": 10, "pick_count": 3, "mode": MODE_EVEN})
    assert cfg["total_amount"] == 30
    assert cfg["amount_unit"] == DEFAULT_AMOUNT_UNIT
    # 派生：30/5=6 单位 ÷ 3 注 = 2 单位 → 10 元
    assert with_derived_settings(cfg)["bet_unit"] == 10

    other = clamp_settings({"bet_unit": 7, "pick_count": 6})
    assert other["total_amount"] == 42
    # 派生：42/5=8 单位 ÷ 6 注 = 1 单位 → 5 元（向下对齐到 amount_unit）
    assert with_derived_settings(other)["bet_unit"] == 5

    # 完全没有旧字段 → 用默认 50，而不是 0
    assert clamp_settings({})["total_amount"] == 50


def test_total_amount_is_not_overridden_by_legacy_bet_unit():
    """两个字段同时存在时，total_amount 是唯一真值，bet_unit 只能被忽略。"""
    cfg = clamp_settings({"total_amount": 100, "bet_unit": 999, "pick_count": 3})
    assert cfg["total_amount"] == 100


@pytest.mark.parametrize(
    ("total", "count", "unit"),
    [(100, 3, 5), (30, 3, 5), (103, 3, 5), (30, 1, 5), (5, 1, 5), (1000, 10, 25)],
)
def test_random_amounts_constraints(total: int, count: int, unit: int):
    amounts = random_amounts(total, count, unit, seed="unit-test")
    assert 1 <= len(amounts) <= count
    assert all(amount % unit == 0 for amount in amounts)
    assert all(amount >= unit for amount in amounts)  # 没有 0 元注
    # 总和精确等于「向下取整到最小单位倍数」后的最大投注
    assert sum(amounts) == (total // unit) * unit


def test_random_amounts_are_deterministic_and_reseedable():
    args = (100, 3, 5)
    first = random_amounts(*args, seed="period-1")
    second = random_amounts(*args, seed="period-1")
    assert first == second  # 同一期 + 同一参数 → 完全一致

    different = random_amounts(*args, seed="period-1|explicit=7")
    assert different != first  # 换种子 → 另一份分配
    assert sum(different) == 100
    assert all(amount % 5 == 0 and amount >= 5 for amount in different)


def test_random_amounts_vary_with_params():
    """参数（最大投注 / 注数 / 单位）一变，分配结果不应原样复刻。"""
    assert random_amounts(100, 3, 5, seed="s") != random_amounts(100, 4, 5, seed="s")
    assert random_amounts(100, 3, 5, seed="s") != random_amounts(105, 3, 5, seed="s")


def test_random_allocation_infeasible_degrades_and_reports():
    """最大投注不足以覆盖 N 注最小单位：不产生 0 元注，降级并在 notes 里如实说明。"""
    plan = random_allocation(10, 3, 5, seed="x")
    assert plan["amounts"] == [5, 5]  # 只输出可覆盖的 2 注
    assert plan["allocated_total"] == 10
    assert any("至少需要 15 元" in note for note in plan["notes"])


def test_random_allocation_floors_non_multiple_and_reports():
    """最大投注不是最小单位整数倍：向下取整分配并如实说明差额。"""
    plan = random_allocation(103, 3, 5, seed="x")
    assert sum(plan["amounts"]) == 100
    assert all(amount % 5 == 0 and amount >= 5 for amount in plan["amounts"])
    assert any("不是金额最小单位 5 元的整数倍" in note for note in plan["notes"])


def test_random_allocation_zero_below_one_unit():
    plan = random_allocation(4, 3, 5, seed="x")
    assert plan["amounts"] == []
    assert plan["allocated_total"] == 0
    assert any("不足 1 个金额最小单位" in note for note in plan["notes"])


def test_recommend_random_end_to_end():
    result = recommend(
        latest=20,
        previous=15,
        history_numbers=[15, 20, 3, 44],
        # 关闭避冷权重：校验随机分配的旧金额口径
        settings={
            "total_amount": 100,
            "amount_unit": 5,
            "pick_count": 3,
            "avoid_cold_enabled": False,
        },
        mode=MODE_RANDOM,
        period=270,
    )
    amounts = [pick["amount"] for pick in result["picks"]]
    assert len(amounts) == 3
    assert sum(amounts) == 100
    assert all(amount % 5 == 0 and amount >= 5 for amount in amounts)
    assert result["total_amount"] == 100
    assert result["amount_unit"] == 5
    # 派生：100/5=20 单位 ÷ 3 注 = 6 单位 → 30 元（向下对齐）
    assert result["bet_unit"] == 30
    assert result["mode_label"] == "随机分配"
    assert result["copy_text"].endswith("合计：100元。")


def test_recommend_random_is_reproducible_for_same_period():
    """同一期 + 同一参数：重算两次金额完全一致（刷新安全）。"""
    kwargs = dict(
        latest=20,
        previous=15,
        history_numbers=[15, 20, 3, 44],
        settings={
            "total_amount": 100,
            "amount_unit": 5,
            "pick_count": 3,
            "avoid_cold_enabled": False,
        },
        mode=MODE_RANDOM,
        period=270,
    )
    first = [pick["amount"] for pick in recommend(**kwargs)["picks"]]
    second = [pick["amount"] for pick in recommend(**kwargs)["picks"]]
    assert first == second


def test_recommend_random_respects_explicit_amount_seed():
    base = dict(
        latest=20,
        previous=15,
        history_numbers=[15, 20, 3, 44],
        settings={
            "total_amount": 100,
            "amount_unit": 5,
            "pick_count": 3,
            "avoid_cold_enabled": False,
        },
        mode=MODE_RANDOM,
        period=270,
    )
    first = [pick["amount"] for pick in recommend(**base)["picks"]]
    reseeded = [pick["amount"] for pick in recommend(**base, amount_seed=7)["picks"]]
    assert reseeded != first
    assert sum(reseeded) == 100
    assert all(amount % 5 == 0 and amount >= 5 for amount in reseeded)
    # 显式种子本身也必须可复现
    again = [pick["amount"] for pick in recommend(**base, amount_seed=7)["picks"]]
    assert again == reseeded


def test_recommend_random_notes_are_surfaced():
    result = recommend(
        latest=20,
        previous=15,
        history_numbers=[15, 20],
        # 关闭避冷权重：校验随机分配降级说明（避免避冷额外改写金额）
        settings={
            "total_amount": 10,
            "amount_unit": 5,
            "pick_count": 3,
            "avoid_cold_enabled": False,
        },
        mode=MODE_RANDOM,
        period=1,
    )
    assert len(result["picks"]) == 2  # 降级为可覆盖的注数
    assert any("至少需要 15 元" in note for note in result["notes"])
    assert all(pick["amount"] == 5 for pick in result["picks"])


def test_amount_seed_key_depends_on_period_and_params():
    assert amount_seed_key(1, 20, 15, 100, 3, 5) == amount_seed_key(1, 20, 15, 100, 3, 5)
    assert amount_seed_key(1, 20, 15, 100, 3, 5) != amount_seed_key(2, 20, 15, 100, 3, 5)
    assert amount_seed_key(1, 20, 15, 100, 3, 5) != amount_seed_key(1, 21, 15, 100, 3, 5)


def test_even_copy_text_lists_amounts_when_total_not_divisible():
    """最大投注不能被注数整除时，相同金额合并，独额单独成组。"""
    picks = [
        {"number": 9, "amount": 34, "role_label": "主推"},
        {"number": 19, "amount": 33, "role_label": "次选"},
        {"number": 31, "amount": 33, "role_label": "防守"},
    ]
    text = build_copy_text(MODE_EVEN, picks)
    assert "各34元" not in text
    assert text == "09：34元；\n19、31：各33元；\n合计：100元。"


def test_random_copy_text_lists_each_amount():
    picks = [
        {"number": 9, "amount": 15, "role_label": "主推"},
        {"number": 19, "amount": 35, "role_label": "次选"},
        {"number": 31, "amount": 50, "role_label": "防守"},
    ]
    text = build_copy_text(MODE_RANDOM, picks)
    assert text == "09：15元；\n19：35元；\n31：50元；\n合计：100元。"


def test_even_copy_text_merges_equal_amounts():
    picks = [
        {"number": 9, "amount": 10, "role_label": "主推"},
        {"number": 19, "amount": 10, "role_label": "次选"},
        {"number": 31, "amount": 10, "role_label": "防守"},
    ]
    assert build_copy_text(MODE_EVEN, picks) == "09、19、31：各10元；\n合计：30元。"


def test_copy_text_pads_single_digit_numbers():
    """个位号码补前导 0；两位号码原样；合计金额不补零。"""
    picks = [
        {"number": 7, "amount": 15, "role_label": "主推"},
        {"number": 39, "amount": 15, "role_label": "次选"},
    ]
    assert build_copy_text(MODE_WEIGHTED, picks) == (
        "07、39：各15元；\n合计：30元。"
    )


def test_copy_text_keeps_two_digit_numbers_unchanged():
    """两位数号码（13 / 49）不因补零被改写。"""
    picks = [
        {"number": 13, "amount": 20, "role_label": "主推"},
        {"number": 49, "amount": 30, "role_label": "次选"},
    ]
    assert build_copy_text(MODE_EVEN, picks) == "13：20元；\n49：30元；\n合计：50元。"


def test_format_number_helper():
    """format_number：个位补零到两位，两位数原样。"""
    assert format_number(1) == "01"
    assert format_number(9) == "09"
    assert format_number(13) == "13"
    assert format_number(49) == "49"
    assert format_number("7") == "07"


def test_copy_text_user_sample_format():
    """用户约定样例：独额 + 同额合并 + 分号换行 + 合计（两位数不补零）。"""
    picks = [
        {"number": 30, "amount": 20, "role_label": "主推"},
        {"number": 15, "amount": 15, "role_label": "次选"},
        {"number": 39, "amount": 15, "role_label": "防守"},
    ]
    assert build_copy_text(MODE_WEIGHTED, picks) == (
        "30：20元；\n15、39：各15元；\n合计：50元。"
    )


# --------------------------------------------------------------------------- #
# 11. 避开重肖开关（默认关闭）
# --------------------------------------------------------------------------- #
def test_build_candidate_pools_respects_exclude_repeat_zodiac():
    from services.lottery import build_candidate_pools

    latest = 22
    same = set(zodiac_numbers(latest)) - {latest}  # 10, 34, 46

    off = build_candidate_pools(latest, 10, 30, exclude_repeat_zodiac=False)
    off_nums = {item["number"] for pool in off.values() for item in pool}
    assert latest not in off_nums
    assert same <= off_nums  # 关闭时同肖号全部在池内

    on = build_candidate_pools(latest, 10, 30, exclude_repeat_zodiac=True)
    on_nums = {item["number"] for pool in on.values() for item in pool}
    assert latest not in on_nums
    assert on_nums.isdisjoint(same)  # 开启时同肖号全部被滤掉


def test_recommend_excludes_repeat_zodiac_when_enabled():
    latest = 22
    same = set(zodiac_numbers(latest))
    result = recommend(
        latest=latest,
        previous=40,
        history_numbers=[22, 40, 1, 2, 3],
        settings={"exclude_repeat_zodiac": True, "pick_count": 3},
    )
    picked = {pick["number"] for pick in result["picks"]}
    assert picked.isdisjoint(same)
    assert all(pick["is_repeat_zodiac"] is False for pick in result["picks"])
    assert result["settings"]["exclude_repeat_zodiac"] is True


def test_recommend_allows_repeat_zodiac_when_disabled():
    """关闭时不过滤同肖：把同肖号做成「遗漏最久」以迫使算法入选。"""
    latest = 22
    # 同肖：10/22/34/46；制造历史让 10/34/46 从未出现过 → 排序优先
    history = [22, 40] + [n for n in range(1, 50) if n not in (10, 22, 34, 46)]
    result = recommend(
        latest=latest,
        previous=40,
        history_numbers=history,
        # 关闭走势加权，才能走旧的「遗漏优先」把同肖冷号顶上来
        settings={
            "exclude_repeat_zodiac": False,
            "pick_count": 3,
            "trend_bias": "neutral",
            # 本用例只校验「避开重肖」开关：关闭避冷加权，
            # 否则默认开启的避冷加权会把同肖冷号排到队尾（另有专门用例覆盖）
            "avoid_cold_enabled": False,
        },
    )
    picked = {pick["number"] for pick in result["picks"]}
    # 至少有一个同肖号入选，证明关闭时不过滤
    assert picked & set(zodiac_numbers(latest))
    assert any(pick["is_repeat_zodiac"] for pick in result["picks"])
    assert result["settings"]["exclude_repeat_zodiac"] is False


def test_settings_api_exclude_repeat_zodiac_roundtrip():
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        body = client.get("/api/settings").json()
        assert body["exclude_repeat_zodiac"] is False

        updated = client.put(
            "/api/settings", json={"exclude_repeat_zodiac": True}
        ).json()
        assert updated["exclude_repeat_zodiac"] is True
        assert client.get("/api/settings").json()["exclude_repeat_zodiac"] is True

        # 只改回 False，其它字段保持
        restored = client.put(
            "/api/settings", json={"exclude_repeat_zodiac": False}
        ).json()
        assert restored["exclude_repeat_zodiac"] is False
        assert restored["total_amount"] == body["total_amount"]
        assert restored["mode"] == body["mode"]
        assert restored["normal_max"] == body["normal_max"]
        assert restored["pick_count"] == body["pick_count"]


# --------------------------------------------------------------------------- #
# 12. 走势加权：波动 × 角色分布 + 开关对照
# --------------------------------------------------------------------------- #
def test_trend_distributions_split_three_role_bands():
    """同一波动桶按近窗频次切三段：主推最热、防守最冷。"""
    latest = 25
    # 构造近窗：小波动号里 24 出现最多，26 次之，23 最少
    history = [25, 24, 24, 24, 26, 26, 23, 10, 40]
    result = recommend(
        latest=latest,
        previous=10,
        history_numbers=history,
        settings={
            "pick_count": 3,
            "trend_bias": "hot",
            "trend_window": 30,
            "small_max": 10,
            "normal_max": 30,
        },
    )
    dist = result["trend_distributions"]
    assert dist["window"] == 30
    assert "small" in dist["waves"]
    small = dist["waves"]["small"]
    assert small["primary"] and small["defense"]
    # 主推段最热次数 >= 防守段最冷次数
    hot_min = min(item["count"] for item in small["primary"])
    cold_max = max(item["count"] for item in small["defense"])
    assert hot_min >= cold_max
    assert result["trend_bias"] == "hot"
    assert all("trend_count" in pick for pick in result["picks"])
    assert any("经验频率" in note or "加权" in note for note in result["notes"])
    # 诚实口径：允许出现「不承诺提高命中率」，禁止正向宣称可提高命中
    joined = "；".join(result["notes"])
    assert "不承诺提高命中率" in joined
    assert "可提高命中" not in joined
    assert "提高命中率。" not in joined.replace("不承诺提高命中率。", "")
    # 近窗 0 次号必须带 days_since_last（无日期时回退期数差；从未出现为 null）
    zeros = [
        item
        for role in ("primary", "secondary", "defense")
        for item in small[role]
        if item["count"] == 0
    ]
    assert zeros
    assert all("days_since_last" in item for item in zeros)


def test_days_since_last_calendar_and_never_seen():
    """近窗 0 次：有开奖日用自然日差；全库从未出现 → null。"""
    from datetime import date, timedelta

    from services.lottery import compute_days_since_last

    # 最新 2026-09-27；号 18 出现在 12 天前；号 7 从未出现
    base = date(2026, 9, 27)
    history = [25, 24, 26, 10, 40, 18]
    dates = [base - timedelta(days=i) for i in range(len(history))]
    # 把 18 的日期钉在 12 天前（index 5 → 默认已是 5 天；改成明确 12）
    dates[5] = base - timedelta(days=12)

    days_map = compute_days_since_last(history, dates)
    assert days_map[18] == 12
    assert days_map[25] == 0
    assert days_map[7] is None

    result = recommend(
        latest=25,
        previous=10,
        history_numbers=history,
        history_dates=dates,
        settings={
            "pick_count": 3,
            "trend_bias": "hot",
            "trend_window": 3,  # 近 3 期：25,24,26 → 18 在窗外 count=0
            "small_max": 10,
            "normal_max": 30,
        },
    )
    # 18 相对 latest=25 差 7，属小波动；近窗 count=0，应带 days_since_last=12
    found = None
    for role in ("primary", "secondary", "defense"):
        for item in result["trend_distributions"]["waves"]["small"][role]:
            if item["number"] == 18:
                found = item
                break
    assert found is not None
    assert found["count"] == 0
    assert found["days_since_last"] == 12

    # 挑一个近窗 0 次且历史也从未出现的号（如 7，|7-25|=18 → 常规）
    never = None
    for role in ("primary", "secondary", "defense"):
        for item in result["trend_distributions"]["waves"]["normal"][role]:
            if item["number"] == 7 and item["count"] == 0:
                never = item
                break
    assert never is not None
    assert never["days_since_last"] is None


def test_trend_bias_neutral_matches_legacy_omission_order():
    """neutral 关闭加权后，池内仍按全历史遗漏优先（与旧行为一致）。"""
    latest = 25
    # 全历史里 24 从未出现、26 出现很多次 → neutral 应优先 24
    history = [25, 26, 26, 26, 26, 26, 10, 40]
    legacyish = recommend(
        latest=latest,
        previous=10,
        history_numbers=history,
        settings={"pick_count": 3, "trend_bias": "neutral", "trend_window": 30},
    )
    hot = recommend(
        latest=latest,
        previous=10,
        history_numbers=history,
        settings={"pick_count": 3, "trend_bias": "hot", "trend_window": 30},
    )
    legacy_nums = [p["number"] for p in legacyish["picks"]]
    hot_nums = [p["number"] for p in hot["picks"]]
    # 同一最新号下，开关应能产生可解释差异（至少号码集合或顺序不同）
    assert legacy_nums != hot_nums or set(legacy_nums) != set(hot_nums)
    assert legacyish["trend_bias"] == "neutral"
    assert any("不加权" in note or "旧" in note for note in legacyish["notes"])


def test_default_trend_bias_is_neutral():
    assert DEFAULT_SETTINGS["trend_bias"] == "neutral"
    assert DEFAULT_SETTINGS["trend_window"] == 30
    assert clamp_settings({})["trend_bias"] == "neutral"
    assert clamp_settings({"trend_bias": "hot"})["trend_bias"] == "hot"
    assert clamp_settings({"trend_bias": "bogus"})["trend_bias"] == "neutral"


def test_bet_count_override_changes_pick_length():
    """波浪买入法页临场注数：等同于本请求覆盖 pick_count（不落库）。"""
    from models.lottery import RecommendRequest

    req = RecommendRequest(bet_count=3)
    assert req.bet_count == 3

    history = [22, 40, 15, 30, 21, 16, 14, 19, 25, 18]
    base = clamp_settings(
        {
            "pick_count": 6,
            "total_amount": 50,
            "amount_unit": 5,
            "mode": MODE_EVEN,
            # 校验注数覆盖的旧分配口径：关闭避冷权重
            "avoid_cold_enabled": False,
        }
    )
    # 模拟 router：bet_count → settings["pick_count"]（仅本请求）
    overridden = dict(base)
    overridden["pick_count"] = req.bet_count
    result = recommend(
        latest=22,
        previous=40,
        history_numbers=history,
        settings=overridden,
        mode=MODE_EVEN,
    )
    assert len(result["picks"]) == 3
    assert sum(p["amount"] for p in result["picks"]) == result["total_amount"]
    assert all(p["amount"] % result["amount_unit"] == 0 for p in result["picks"])
    # 确认未改动默认配置常量
    assert DEFAULT_SETTINGS["pick_count"] == 6


def test_settings_api_trend_bias_roundtrip():
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        body = client.get("/api/settings").json()
        assert body["trend_bias"] == "neutral"
        assert body["trend_window"] == 30

        updated = client.put(
            "/api/settings",
            json={"trend_bias": "hot", "trend_window": 60},
        ).json()
        assert updated["trend_bias"] == "hot"
        assert updated["trend_window"] == 60
        assert client.get("/api/settings").json()["trend_bias"] == "hot"

        restored = client.put(
            "/api/settings",
            json={"trend_bias": "neutral", "trend_window": 30},
        ).json()
        assert restored["trend_bias"] == "neutral"
        assert restored["total_amount"] == body["total_amount"]
        assert restored["exclude_repeat_zodiac"] == body["exclude_repeat_zodiac"]


# --------------------------------------------------------------------------- #
# 13. 「近期走势加权」读取口径：没手动设置过就不加权（存量旧默认 hot 不生效）
# --------------------------------------------------------------------------- #
def test_effective_trend_bias_requires_explicit_marker():
    """只有「手动设置过」的标记为真，才按存值生效；否则一律 neutral。"""
    from services.lottery import effective_trend_bias

    # 存量旧默认 hot（无标记）→ 不加权
    assert effective_trend_bias({"trend_bias": "hot"}) == "neutral"
    assert effective_trend_bias({"trend_bias": "cold"}) == "neutral"
    # 标记为真 → 按用户选择生效
    assert (
        effective_trend_bias({"trend_bias": "hot", "trend_bias_explicit": True})
        == "hot"
    )
    # 脏值 / 字符串真值同样按 coerce_bool 处理
    assert (
        effective_trend_bias({"trend_bias": "mid", "trend_bias_explicit": "true"})
        == "mid"
    )
    assert (
        effective_trend_bias({"trend_bias": "mid", "trend_bias_explicit": False})
        == "neutral"
    )
    # 非法 bias 永远回退 neutral
    assert (
        effective_trend_bias({"trend_bias": "bogus", "trend_bias_explicit": True})
        == "neutral"
    )


def test_resolve_trend_bias_forces_neutral_on_legacy_hot():
    from services.lottery import resolve_trend_bias

    cfg = clamp_settings({"trend_bias": "hot", "trend_window": 60})
    assert cfg["trend_bias"] == "hot"  # clamp 只做校验，不套读取口径
    resolved = resolve_trend_bias(cfg)
    assert resolved["trend_bias"] == "neutral"
    assert resolved["trend_window"] == 60  # 其它字段原样

    explicit = resolve_trend_bias(
        clamp_settings({"trend_bias": "hot", "trend_bias_explicit": True})
    )
    assert explicit["trend_bias"] == "hot"


def test_recommend_uses_resolved_neutral_for_legacy_hot():
    """recommend 的入参默认来自 get_settings（已解析）→ 存量 hot 不该再加权。"""
    from services.lottery import resolve_trend_bias

    history = [25, 26, 26, 26, 10, 40]
    settings = resolve_trend_bias(clamp_settings({"trend_bias": "hot"}))
    result = recommend(latest=25, previous=10, history_numbers=history,
                       settings=settings)
    assert result["trend_bias"] == "neutral"
    assert any("不加权" in note or "旧" in note for note in result["notes"])


def test_memory_store_legacy_hot_reads_neutral_and_self_heals():
    from repository import MemoryStore
    from services.auth import GLOBAL_SETTINGS_USER_ID

    store = MemoryStore()
    # 模拟存量库：旧默认 hot 落库，但没有「手动设置过」标记
    bucket = store._settings[GLOBAL_SETTINGS_USER_ID]  # noqa: SLF001
    bucket["trend_bias"] = "hot"
    bucket.pop("trend_bias_explicit", None)

    assert asyncio.run(store.get_settings())["trend_bias"] == "neutral"

    # 只改别的设置项：不得把存量 hot 当成加权，落库也自然收敛成 neutral
    saved = asyncio.run(store.update_settings({"small_max": 9}))
    assert saved["trend_bias"] == "neutral"
    assert saved["small_max"] == 9
    assert asyncio.run(store.get_settings())["trend_bias"] == "neutral"

    # 用户手动选择热号偏好 → 打上标记，此后按用户选择生效
    manual = asyncio.run(store.update_settings({"trend_bias": "hot"}))
    assert manual["trend_bias"] == "hot"
    assert asyncio.run(store.get_settings())["trend_bias"] == "hot"
    # 之后再改别的设置项，手动选择的偏好必须保留
    kept = asyncio.run(store.update_settings({"small_max": 12}))
    assert kept["trend_bias"] == "hot"
    assert asyncio.run(store.get_settings())["trend_bias"] == "hot"


def test_settings_api_legacy_hot_reads_neutral_until_manually_set():
    import repository
    from fastapi.testclient import TestClient
    from services.auth import GLOBAL_SETTINGS_USER_ID

    from main import app

    with TestClient(app) as client:
        store = asyncio.run(repository.get_store())
        bucket = store._settings[GLOBAL_SETTINGS_USER_ID]  # noqa: SLF001
        bucket["trend_bias"] = "hot"
        bucket.pop("trend_bias_explicit", None)

        # 存量 hot 在接口上表现为「不加权」；内部标记不进入对外设置契约
        body = client.get("/api/settings").json()
        assert body["trend_bias"] == "neutral"
        assert "trend_bias_explicit" not in body
        # 只改其它设置项也不会让它生效
        assert client.put(
            "/api/settings", json={"normal_max": 28}
        ).json()["trend_bias"] == "neutral"
        assert client.get("/api/settings").json()["trend_bias"] == "neutral"
        # 手动选择后才生效
        assert client.put(
            "/api/settings", json={"trend_bias": "hot"}
        ).json()["trend_bias"] == "hot"
        assert client.get("/api/settings").json()["trend_bias"] == "hot"
        # 还原，避免影响其它用例（每个用例独立 store，这里只是幂等收尾）
        client.put("/api/settings", json={"trend_bias": "neutral"})


# --------------------------------------------------------------------------- #
# 14. 避冷加权：距上次出现越久 → 选号排后、金额越低（最多给保本金额）
# --------------------------------------------------------------------------- #
COLD_LATEST = 25
COLD_TARGET = 24  # |24-25|=1 → 小波动；桶内差值最小，桶内排序第一
COLD_REF = date(2026, 9, 27)

# 最新号为 25 时的小波动桶号码（差 ≤ 10），用于构造「整桶都是冷号」的场景
_COLD_SMALL = [
    n
    for n in range(1, 50)
    if n != COLD_LATEST
    and classify_wave(abs(n - COLD_LATEST), DEFAULT_SMALL_MAX, DEFAULT_NORMAL_MAX)
    == WAVE_SMALL
]
_COLD_OTHERS = [
    n for n in range(1, 50) if n != COLD_LATEST and n not in _COLD_SMALL
]


def _cold_history(days: int | None) -> tuple[list[int], list[date]]:
    """构造「小波动桶内**全部是冷号**、target 仍排桶内第一」的历史序列。

    这样无论避冷加权是否把冷号排后，``target``（24）都会被选中，
    金额口径才可稳定断言：
    - 常规 / 大跳桶的号码各出现两次、日期很近 → 非冷号；
    - 小波动桶的号码各出现一次、日期很久远（200 天）→ 整桶都是冷号；
    - ``days is None`` → target 样本内从未出现（最冷）；
      否则 target 出现在序列末尾，``days_since_last`` 恰好是 ``days``。
    """
    small_others = [n for n in _COLD_SMALL if n != COLD_TARGET]
    history = [COLD_LATEST, *_COLD_OTHERS, *_COLD_OTHERS, *small_others]
    dates = (
        [COLD_REF]
        + [COLD_REF - timedelta(days=1)] * (2 * len(_COLD_OTHERS))
        + [COLD_REF - timedelta(days=200)] * len(small_others)
    )
    if days is not None:
        history.append(COLD_TARGET)
        dates.append(COLD_REF - timedelta(days=int(days)))
    return history, dates


def _cold_recommend(
    days: int | None,
    *,
    enabled: bool = True,
    threshold: int = DEFAULT_AVOID_COLD_DAYS,
    total: int = 50,
    unit: int = DEFAULT_AMOUNT_UNIT,
    mode: str = MODE_SINGLE,
) -> dict:
    history, dates = _cold_history(days)
    return recommend(
        latest=COLD_LATEST,
        previous=15,
        history_numbers=history,
        history_dates=dates,
        settings={
            "total_amount": total,
            "amount_unit": unit,
            "pick_count": 1,
            "avoid_cold_enabled": enabled,
            "avoid_cold_days": threshold,
        },
        mode=mode,
    )


def test_avoid_cold_weight_rule():
    """权重口径：≤ 阈值不惩罚；> 阈值 inverse 衰减；从未出现取地板值。"""
    assert avoid_cold_weight(0, threshold=60) == 1.0
    assert avoid_cold_weight(59, threshold=60) == 1.0
    # 正好等于阈值：不惩罚（用户约定 60 天不降权重）
    assert avoid_cold_weight(60, threshold=60) == 1.0
    # 2 倍阈值 → 0.5；4 倍 → 0.25
    assert avoid_cold_weight(120, threshold=60) == pytest.approx(0.5)
    assert avoid_cold_weight(240, threshold=60) == pytest.approx(0.25)
    # 本池样本内从未出现 → 最冷，取地板权重
    assert avoid_cold_weight(None, threshold=60) == AVOID_COLD_FLOOR_WEIGHT
    # 关闭 → 恒 1.0（含「从未出现」与极冷值）
    assert avoid_cold_weight(None, enabled=False) == 1.0
    assert avoid_cold_weight(9999, enabled=False) == 1.0


def test_avoid_cold_defaults_on_and_clamped():
    assert DEFAULT_AVOID_COLD_ENABLED is True
    assert DEFAULT_AVOID_COLD_DAYS == 60
    assert DEFAULT_SETTINGS["avoid_cold_enabled"] is True
    assert DEFAULT_SETTINGS["avoid_cold_days"] == 60
    assert clamp_settings({})["avoid_cold_enabled"] is True
    assert clamp_settings({})["avoid_cold_days"] == 60
    # 缺失 / 脏值 → 默认开启；显式 false 才关闭
    assert clamp_settings({"avoid_cold_enabled": None})["avoid_cold_enabled"] is True
    assert clamp_settings({"avoid_cold_enabled": "false"})["avoid_cold_enabled"] is False
    # 阈值钳到 1..999，非数字回退默认 60
    assert clamp_settings({"avoid_cold_days": 0})["avoid_cold_days"] == 1
    assert clamp_settings({"avoid_cold_days": 99999})["avoid_cold_days"] == 999
    assert clamp_settings({"avoid_cold_days": "abc"})["avoid_cold_days"] == 60


def test_apply_avoid_cold_amounts_cap_and_floor():
    """金额规则：只降不升、cap = 1 个最小单位、不足 1 个单位归 0。"""
    # 权重 0.5：25 → min(12.5, 5 元保本) = 5；权重 1.0 的注原样保留
    assert apply_avoid_cold_amounts([25, 5], [0.5, 1.0], amount_unit=5) == ([5, 5], 1)
    # 权重 0：金额归 0
    assert apply_avoid_cold_amounts([25], [0.0], amount_unit=5) == ([0], 1)
    # 极冷（权重极小）：50×0.06=3 元 < 1 个单位 → 0
    assert apply_avoid_cold_amounts([50], [0.06], amount_unit=5) == ([0], 1)
    # 全 1.0（关闭避冷）→ 与输入逐元素相同
    base = [10, 10, 10, 10, 5, 5]
    assert apply_avoid_cold_amounts(base, [1.0] * 6, amount_unit=5) == (base, 0)


def test_avoid_cold_exactly_threshold_no_penalty():
    """正好 60 天：权重 1.0，金额与不可冷时完全一致。"""
    result = _cold_recommend(DEFAULT_AVOID_COLD_DAYS)
    pick = result["picks"][0]

    assert pick["number"] == COLD_TARGET
    assert pick["days_since_last"] == DEFAULT_AVOID_COLD_DAYS
    assert pick["avoid_cold_penalized"] is False
    assert pick["avoid_cold_weight"] == 1.0
    assert pick["amount"] == 50  # 单挑整份预算，未被压低
    assert result["avoid_cold"]["penalized_picks"] == 0
    assert result["staked_total"] == 50


def test_avoid_cold_double_threshold_halves_weight_and_reduces_amount():
    """120 天（2×阈值）：权重 0.5，金额被压到保本金额 5 元。"""
    result = _cold_recommend(120)
    pick = result["picks"][0]

    assert pick["days_since_last"] == 120
    assert pick["avoid_cold_weight"] == pytest.approx(0.5)
    assert pick["avoid_cold_penalized"] is True
    # 50 × 0.5 = 25 → cap（1×unit=5）压到 5
    assert pick["amount"] == 5
    assert pick["amount"] <= result["amount_unit"]
    assert result["avoid_cold"]["penalized_picks"] == 1
    assert result["avoid_cold"]["cap_amount"] == result["amount_unit"] == 5
    assert result["staked_total"] == 5
    # 省下的预算不补给其它注（本用例只有 1 注）
    assert result["avoid_cold"]["reduced_total"] == 45


def test_avoid_cold_very_cold_floors_down_to_zero():
    """极冷（999 天，权重 60/999≈0.06）与「样本内从未出现」都归 0，号码仍列出。"""
    for days in (999, None):
        result = _cold_recommend(days)
        pick = result["picks"][0]

        assert pick["number"] == COLD_TARGET
        assert pick["days_since_last"] == days  # None = 样本内未出现
        assert pick["avoid_cold_penalized"] is True
        assert pick["amount"] == 0
        assert result["staked_total"] == 0


def test_avoid_cold_disabled_matches_legacy_amounts():
    """关闭避冷：权重全 1.0、无惩罚标记，金额回到改造前的分配口径。"""
    enabled = _cold_recommend(120)
    disabled = _cold_recommend(120, enabled=False)
    pick = disabled["picks"][0]

    assert pick["amount"] == 50
    assert pick["avoid_cold_penalized"] is False
    assert pick["avoid_cold_weight"] == 1.0
    assert disabled["avoid_cold"]["enabled"] is False
    assert disabled["avoid_cold"]["penalized_picks"] == 0
    assert disabled["staked_total"] == disabled["total_amount"] == 50
    # 与直接调用基础分配算法一致 = 等同改造前行为
    assert [p["amount"] for p in disabled["picks"]] == allocate_amounts(
        MODE_SINGLE, assign_roles(1), 50, amount_unit=DEFAULT_AMOUNT_UNIT
    )
    # 同一天数下，开启避冷确实把金额压低
    assert enabled["picks"][0]["amount"] < pick["amount"]
    # 无论开关，picks 都带 days_since_last
    assert disabled["picks"][0]["days_since_last"] == 120


def test_avoid_cold_cap_holds_across_all_modes():
    """四种模式都不崩，且被压低的注金额 ≤ 保本金额、仍是最小单位倍数。"""
    for mode in MODES:
        for days in (120, 300):
            result = _cold_recommend(days, mode=mode)
            penalized = [p for p in result["picks"] if p["avoid_cold_penalized"]]
            assert penalized, f"{mode}/{days}: 应至少有一注被压低"
            for pick in penalized:
                assert pick["amount"] <= result["amount_unit"]
                assert pick["amount"] % result["amount_unit"] == 0
            # 未被压低时总投入不超过预算
            assert result["staked_total"] <= result["total_amount"]


def test_recommend_picks_carry_days_since_last():
    """picks 的每一项都必须带 days_since_last（int | None）。"""
    result = recommend(
        latest=20,
        previous=15,
        history_numbers=[15, 20, 3, 44],
        settings={"pick_count": 3, "avoid_cold_enabled": False},
    )
    assert result["picks"]
    for pick in result["picks"]:
        assert "days_since_last" in pick
        assert pick["days_since_last"] is None or isinstance(
            pick["days_since_last"], int
        )
    # 47 从未在样本内出现 → None（不编造天数）
    assert all(
        pick["days_since_last"] is None
        for pick in result["picks"]
        if pick["number"] not in (15, 20, 3, 44)
    )


def test_avoid_cold_notes_are_honest():
    """notes 必须说明「冷号排后 + 金额封顶」、样本偏好、非概率 / 非收益承诺。"""
    result = _cold_recommend(120)
    joined = "；".join(result["notes"])
    assert "避冷加权" in joined
    assert "保本金额" in joined
    assert "不是概率计算" in joined
    assert "不承诺提高命中率或收益" in joined
    # 选号排后必须如实说明（含「全桶冷号仍照常取用」的兜底口径）
    assert "排到候选队列末尾" in joined
    assert "不会因此少出号" in joined
    # 禁止任何确定性盈利表述
    for banned in ("稳赚", "必胜", "保证盈利", "保本盈利"):
        assert banned not in joined

    # 关闭时如实说明是旧行为
    off = _cold_recommend(120, enabled=False)
    assert any("避冷加权已关闭" in note for note in off["notes"])


def test_settings_api_avoid_cold_roundtrip():
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        body = client.get("/api/settings").json()
        assert body["avoid_cold_enabled"] is True
        assert body["avoid_cold_days"] == 60

        updated = client.put(
            "/api/settings",
            json={"avoid_cold_enabled": False, "avoid_cold_days": 90},
        ).json()
        assert updated["avoid_cold_enabled"] is False
        assert updated["avoid_cold_days"] == 90
        assert client.get("/api/settings").json()["avoid_cold_days"] == 90

        restored = client.put(
            "/api/settings",
            json={"avoid_cold_enabled": True, "avoid_cold_days": 60},
        ).json()
        assert restored["avoid_cold_enabled"] is True
        assert restored["avoid_cold_days"] == 60
        assert restored["total_amount"] == body["total_amount"]


def test_memory_store_persists_avoid_cold_settings():
    from repository import MemoryStore
    from services.auth import GLOBAL_SETTINGS_USER_ID

    store = MemoryStore()
    saved = asyncio.run(
        store.update_settings({"avoid_cold_enabled": False, "avoid_cold_days": 120})
    )
    assert saved["avoid_cold_enabled"] is False
    assert saved["avoid_cold_days"] == 120

    again = asyncio.run(store.get_settings())
    assert again["avoid_cold_enabled"] is False
    assert again["avoid_cold_days"] == 120
    # 落库值必须是英文 / 数字，禁止汉字
    bucket = store._settings[GLOBAL_SETTINGS_USER_ID]  # noqa: SLF001
    assert bucket["avoid_cold_enabled"] is False
    assert bucket["avoid_cold_days"] == 120


# --------------------------------------------------------------------------- #
# 15. 避冷加权 · 选号排后（与 trend_bias 独立；关闭 = 严格 no-op）
# --------------------------------------------------------------------------- #
def test_is_avoid_cold_number_rule():
    """冷号判定：> 阈值 或 样本内从未出现；≤ 阈值不算冷；关闭恒 False。"""
    assert is_avoid_cold_number(60, threshold=60) is False
    assert is_avoid_cold_number(61, threshold=60) is True
    assert is_avoid_cold_number(None, threshold=60) is True
    assert is_avoid_cold_number(9999, enabled=False) is False
    assert is_avoid_cold_number(None, enabled=False) is False


def test_order_pool_pushes_cold_numbers_to_back():
    """冷号（含样本内从未出现）排到队列末尾；关闭时与旧行为逐元素一致。"""
    pool = [{"number": 24, "diff": 1}, {"number": 26, "diff": 1}]
    # 24 遗漏最少 → 旧行为排最前；26 差值相同但出现次数多
    counts = Counter({24: 1, 26: 5})
    days = {24: 197, 26: 3}

    legacy = order_pool(pool, counts)
    assert [item["number"] for item in legacy] == [24, 26]

    pushed = order_pool(
        pool,
        counts,
        avoid_cold_enabled=True,
        avoid_cold_days=60,
        days_since_last=days,
    )
    assert [item["number"] for item in pushed] == [26, 24]

    # 样本内从未出现（None）同样算冷号，排到末尾
    never = order_pool(
        pool,
        counts,
        avoid_cold_enabled=True,
        avoid_cold_days=60,
        days_since_last={24: None, 26: 3},
    )
    assert [item["number"] for item in never] == [26, 24]

    # 关闭避冷 → 与旧行为逐元素一致（严格 no-op）
    off = order_pool(
        pool,
        counts,
        avoid_cold_enabled=False,
        avoid_cold_days=60,
        days_since_last=days,
    )
    assert [item["number"] for item in off] == [item["number"] for item in legacy]


@pytest.mark.parametrize("bias", ["neutral", "hot", "cold", "mid"])
def test_order_pool_cold_ordering_independent_of_trend_bias(bias: str):
    """避冷排后与 trend_bias 独立：非冷号整体在前，两组内部保持各自原相对顺序。"""
    pool = [
        {"number": 24, "diff": 1},
        {"number": 26, "diff": 1},
        {"number": 23, "diff": 2},
    ]
    counts = Counter({24: 1, 26: 5, 23: 3})
    trend_counts = Counter({24: 9, 23: 7, 26: 1})
    days = {24: 197, 26: 3, 23: 500}  # 26 非冷；24 / 23 冷

    kwargs = dict(
        trend_bias=bias,
        trend_counts=trend_counts,
        mid_target=5.0,
    )
    without = order_pool(pool, counts, **kwargs)
    with_cold = order_pool(
        pool,
        counts,
        avoid_cold_enabled=True,
        avoid_cold_days=60,
        days_since_last=days,
        **kwargs,
    )

    def is_cold(number: int) -> bool:
        return is_avoid_cold_number(days[number], threshold=60)

    # 期望 = 非冷号（保持 without 的相对顺序）在前，冷号（同样保持相对顺序）在后
    expected = [i for i in without if not is_cold(i["number"])] + [
        i for i in without if is_cold(i["number"])
    ]
    assert [i["number"] for i in with_cold] == [i["number"] for i in expected]
    assert len(with_cold) == len(without)  # 冷号不删除，始终留在池内


def _stale_and_fresh_history() -> tuple[list[int], list[date]]:
    """用户线上回归场景：24 距上次出现 197 天，26 刚出现过。

    除 24 外每个号都出现两次且日期很近（非冷号，遗漏计数 2）；24 只出现一次、
    日期在 197 天前（遗漏计数 1 → neutral 旧行为必然排最前）。
    """
    others = [n for n in range(1, 50) if n not in (COLD_LATEST, COLD_TARGET)]
    history = [COLD_LATEST, *others, *others, COLD_TARGET]
    dates = (
        [COLD_REF]
        + [COLD_REF - timedelta(days=1)] * (2 * len(others))
        + [COLD_REF - timedelta(days=197)]
    )
    return history, dates


def test_avoid_cold_regression_stale_number_not_taken_first():
    """回归：197 天未出现的号不能再因为「遗漏最久」被首选。"""
    history, dates = _stale_and_fresh_history()
    base = {
        "latest": COLD_LATEST,
        "previous": None,
        "history_numbers": history,
        "history_dates": dates,
        "mode": MODE_SINGLE,
    }
    off = recommend(
        **base, settings={"pick_count": 1, "avoid_cold_enabled": False}
    )
    on = recommend(**base, settings={"pick_count": 1, "avoid_cold_enabled": True})

    # 旧行为（关闭避冷）：遗漏最久的 24 被首选 —— 这正是用户反馈的问题
    assert off["picks"][0]["number"] == COLD_TARGET
    assert off["picks"][0]["days_since_last"] == 197
    assert off["picks"][0]["avoid_cold_penalized"] is False

    # 新行为（开启避冷）：197 天的冷号被排后，非冷号 26 先被取到
    assert on["picks"][0]["number"] == 26
    assert on["picks"][0]["days_since_last"] == 1
    assert on["picks"][0]["avoid_cold_penalized"] is False
    # 冷号仍在候选池内（只是排后），并未被删除
    pools = build_candidate_pools(COLD_LATEST, DEFAULT_SMALL_MAX, DEFAULT_NORMAL_MAX)
    small_numbers = {item["number"] for item in pools[WAVE_SMALL]}
    assert COLD_TARGET in small_numbers


def test_avoid_cold_all_cold_bucket_still_fills_picks():
    """某波动桶只剩冷号：仍能照常取号，不少出号、不误报「无号」。"""
    # 小波动桶全部样本内从未出现（整桶冷号）；其余桶都是近期的非冷号
    history = [COLD_LATEST, *_COLD_OTHERS, *_COLD_OTHERS]
    dates = [COLD_REF] + [COLD_REF - timedelta(days=1)] * (2 * len(_COLD_OTHERS))

    result = recommend(
        latest=COLD_LATEST,
        previous=None,
        history_numbers=history,
        history_dates=dates,
        settings={"pick_count": 5, "avoid_cold_enabled": True},
    )
    picks = result["picks"]

    assert len(picks) == 5  # 注数补齐，没有因为整桶冷号而少出号
    # 整桶冷号不误报「无号」（latest=25 时大跳桶本来就无号，与本用例无关）
    missing_types = {item["type"] for item in result["missing_waves"]}
    assert WAVE_SMALL not in missing_types
    assert not any("小波动无号" in note for note in result["notes"])
    # 小波动桶全是冷号 → 兜底仍然取到了小波动号
    assert picks[0]["wave_type"] == WAVE_SMALL
    assert picks[0]["days_since_last"] is None  # 样本内未出现
    assert picks[0]["avoid_cold_penalized"] is True
    # 冷号排在末尾：所有非冷号都排在该桶冷号之前
    small_indexes = [
        index for index, pick in enumerate(picks) if pick["wave_type"] == WAVE_SMALL
    ]
    assert small_indexes  # 冷号确实被取到（兜底生效）


def test_avoid_cold_disabled_ordering_matches_legacy_pool_order():
    """关闭避冷：每个波动桶的排序与「不带避冷参数」的旧排序逐元素一致。"""
    history, dates = _stale_and_fresh_history()
    pools = build_candidate_pools(
        COLD_LATEST, DEFAULT_SMALL_MAX, DEFAULT_NORMAL_MAX
    )
    counts = Counter(n for n in history if n != COLD_LATEST)
    days = compute_days_since_last(history, dates)

    for pool in pools.values():
        legacy = order_pool(pool, counts)
        off = order_pool(
            pool,
            counts,
            avoid_cold_enabled=False,
            avoid_cold_days=60,
            days_since_last=days,
        )
        assert [i["number"] for i in off] == [i["number"] for i in legacy]
        # 开启时长度不变（冷号不删除）
        on = order_pool(
            pool,
            counts,
            avoid_cold_enabled=True,
            avoid_cold_days=60,
            days_since_last=days,
        )
        assert len(on) == len(legacy)


def _weighted_interaction_history() -> tuple[list[int], list[date]]:
    """加权路径场景：24 是小波动桶内**唯一冷号**（200 天前），且桶内排序第一。

    小波动桶内每号各出现 1 次（计数相等）→ 三种偏好的频次排序前缀在本桶内退化为
    常数，hot / cold / mid 切出的角色带**完全一致**。因此本用例对三种偏好都成立，
    验证的是「避冷排后在加权路径同样生效」，与偏好方向解耦（修复前冷/中频被错误地
    走成热号偏好的切片，才会让「主推=最热段」这个前提对三者都看似成立）。

    24 相对最新号 25 的差值最小（|24-25|=1）→ 排桶内第一、落进主推带；
    其余小波动号日期很近（1 天前）→ 非冷号。
    """
    small_others = [n for n in _COLD_SMALL if n != COLD_TARGET]
    history = [COLD_LATEST, COLD_TARGET, *small_others, *_COLD_OTHERS]
    dates = (
        [COLD_REF, COLD_REF - timedelta(days=200)]
        + [COLD_REF - timedelta(days=1)] * (len(small_others) + len(_COLD_OTHERS))
    )
    return history, dates


@pytest.mark.parametrize("bias", ["hot", "cold", "mid"])
def test_avoid_cold_applies_in_weighted_path_for_every_bias(bias: str):
    """加权路径下避冷排后同样生效，且不改变所选 trend_bias。"""
    history, dates = _weighted_interaction_history()
    base = {
        "latest": COLD_LATEST,
        "previous": None,
        "history_numbers": history,
        "history_dates": dates,
        "mode": MODE_SINGLE,
    }
    common = {"pick_count": 1, "trend_bias": bias, "trend_window": 0}

    off = recommend(**base, settings={**common, "avoid_cold_enabled": False})
    on = recommend(**base, settings={**common, "avoid_cold_enabled": True})

    # 冷号 24 是小波动桶内最热（主推带）→ 关闭避冷时被首选
    assert off["picks"][0]["number"] == COLD_TARGET
    assert off["picks"][0]["days_since_last"] == 200
    # 开启避冷后，主推带里的非冷号 26 先被取到（冷号整体排后）
    assert on["picks"][0]["number"] == 26
    # 两个维度互不干扰：trend_bias 原样保留，选号仍走同一套角色带
    assert off["trend_bias"] == bias
    assert on["trend_bias"] == bias
    assert off["picks"][0]["role"] == on["picks"][0]["role"] == "primary"


def test_trend_bias_direction_changes_role_bands():
    """三种偏好方向必须切出**不同**的角色带。

    回归：``split_pool_into_role_bands`` 原先不接收 ``bias``，而加权路径取号只走角色带
    （``ordered_pools`` 仅作回退），于是 hot / cold / mid 退化成同一套「主推=最热段」，
    界面上三个方向选项形同虚设。
    """
    latest = 25
    # 小波动桶内计数刻意不等：24=3 次（最热）、26=2 次、23/27=1 次、其余=0 次
    history = [25, 24, 24, 24, 26, 26, 23, 27, 10, 40]

    def band_and_rule(bias: str) -> tuple[list[int], str]:
        result = recommend(
            latest=latest,
            previous=10,
            history_numbers=history,
            settings={
                "pick_count": 3,
                "trend_bias": bias,
                "trend_window": 0,
                "avoid_cold_enabled": False,
                "small_max": 10,
                "normal_max": 30,
            },
        )
        dist = result["trend_distributions"]
        assert dist["bias"] == bias
        primary = [item["number"] for item in dist["waves"]["small"]["primary"]]
        return primary, dist["rule"]

    (hot, hot_rule) = band_and_rule("hot")
    (cold, cold_rule) = band_and_rule("cold")
    (mid, mid_rule) = band_and_rule("mid")

    # 三个方向切出三个不同的主推段（修复前恒为同一个）
    assert len({tuple(hot), tuple(cold), tuple(mid)}) == 3, (hot, cold, mid)
    # 热号偏好：首位是桶内最热的 24
    assert hot[0] == 24
    # 冷号偏好：方向相反，主推段不含最热的 24
    assert 24 not in cold
    # 中频优先：首位不是最热的 24
    assert mid[0] != hot[0]
    # 展示口径必须跟着偏好转（否则 rule 与实际切片打架，UI 会自相矛盾）
    assert len({hot_rule, cold_rule, mid_rule}) == 3
    assert all("主推=" in rule for rule in (hot_rule, cold_rule, mid_rule))


def test_trend_window_ignored_for_selection_when_neutral():
    """不加权时 trend_window **不影响选号**，只影响「走势分布参考」的展示。

    回归：用户反馈「窗口从 60 改到 100，号码没变化」。neutral 走 ``ordered_pools``，
    排序键是**全历史**遗漏计数，``_trend_sort_prefix`` 返回空元组 ——
    近窗统计（``trend_counts``）根本没有进入选号排序键，窗口只在展示里出现。
    """
    latest, previous = 25, 10
    # 近 3 期最热是 26；全样本最热是 24 —— 让「窗口不同 → 频次不同 → 角色带不同」
    history = [25, 26, 26, 26, 24, 24, 24, 24, 23, 27]

    def run(bias: str, window: int) -> dict:
        return recommend(
            latest=latest,
            previous=previous,
            history_numbers=history,
            settings={
                "pick_count": 3,
                "trend_bias": bias,
                "trend_window": window,
                "avoid_cold_enabled": False,
                "small_max": 10,
                "normal_max": 30,
            },
        )

    def picks(bias: str, window: int) -> list[int]:
        return [p["number"] for p in run(bias, window)["picks"]]

    # neutral：窗口 3 / 10（全部）选号逐元素一致
    assert picks("neutral", 3) == picks("neutral", 10)
    # 但展示口径确实随窗口变（诚实：不是「完全没变」，是选号没变）
    assert run("neutral", 3)["trend_distributions"]["used_window"] == 3
    assert run("neutral", 10)["trend_distributions"]["used_window"] == 10

    # 开启偏好后，窗口才会改变选号（近 3 期最热的 26 vs 全样本最热的 24）
    assert picks("hot", 3) != picks("hot", 10)
    assert picks("hot", 3)[0] == 26
    assert picks("hot", 10)[0] == 24


@pytest.mark.parametrize("bias", ["neutral", "hot", "cold", "mid"])
def test_avoid_cold_does_not_change_trend_bias(bias: str):
    """避冷加权只影响排序 / 金额，绝不改写 trend_bias 取值。"""
    history, dates = _stale_and_fresh_history()
    result = recommend(
        latest=COLD_LATEST,
        previous=None,
        history_numbers=history,
        history_dates=dates,
        settings={
            "pick_count": 3,
            "trend_bias": bias,
            "avoid_cold_enabled": True,
            "avoid_cold_days": 60,
        },
    )
    assert result["trend_bias"] == bias
    assert result["settings"]["trend_bias"] == bias
    assert result["settings"]["avoid_cold_enabled"] is True
    assert result["avoid_cold"]["enabled"] is True




# --------------------------------------------------------------------------- #
# 9. 号码 → 生肖（固定映射，随农历年轮转；只增不改）
# --------------------------------------------------------------------------- #
# 2026-02-17 是丙午马年春节（生肖表边界）；下面统一用春节前后的开奖日做参照。
ZODIAC_REF_DATE = date(2026, 3, 11)          # 2026 马年内
ZODIAC_REF_NEW_YEAR = date(2026, 2, 17)      # 春节当天 → 马年
ZODIAC_REF_NEW_YEAR_EVE = date(2026, 2, 16)  # 春节前一天 → 仍属 2025 蛇年


def _zodiac_picks(history_dates: list[date] | None) -> dict:
    """latest=1 的确定性场景：前十注固定为 2..9 / 12 / 32（含个位与两位号）。

    关闭避冷加权只为让金额分配干净，不影响选号顺序（本用例不校验金额）。
    """
    return recommend(
        latest=1,
        previous=None,
        history_numbers=[1],
        history_dates=history_dates,
        settings={"pick_count": 10, "avoid_cold_enabled": False},
    )


def test_resolve_zodiac_date_reads_latest_draw_date():
    """生肖参照日 = 历史序列里最新一期的开奖日（最新在前）；拿不到就 None。"""
    assert resolve_zodiac_date([ZODIAC_REF_DATE, date(2026, 3, 10)]) == ZODIAC_REF_DATE
    assert resolve_zodiac_date([datetime(2026, 3, 11, 21, 30)]) == ZODIAC_REF_DATE
    assert resolve_zodiac_date(["2026-03-11"]) == ZODIAC_REF_DATE
    assert resolve_zodiac_date([]) is None
    assert resolve_zodiac_date(None) is None
    # 日期解析失败时不猜年份，直接留空
    assert resolve_zodiac_date(["not-a-date"]) is None


def test_recommend_picks_all_carry_zodiac():
    """每一注都必须带 zodiac / zodiac_label，且标签与英文码一一对应。"""
    result = _zodiac_picks([ZODIAC_REF_DATE])
    assert result["picks"]
    for pick in result["picks"]:
        assert "zodiac" in pick and "zodiac_label" in pick
        # 已知农历年内必须给出生肖（不是 null）
        assert pick["zodiac"] in ZODIAC_LABELS
        assert pick["zodiac_label"] == ZODIAC_LABELS[pick["zodiac"]]
    # 口径溯源：参照日 / 农历年 / 最新号生肖
    assert result["zodiac_date"] == ZODIAC_REF_DATE.isoformat()
    assert result["zodiac_year"] == 2026
    assert result["latest_zodiac_code"] == "HORSE"
    assert result["latest_zodiac_label"] == "马"
    # 旧字段一字不改（只增不改）
    assert result["latest_zodiac"] == zodiac_numbers(1)


def test_recommend_pick_zodiac_values_for_single_and_double_digit():
    """个位号与两位号都要给出正确生肖（2026 马年表）。"""
    result = _zodiac_picks([ZODIAC_REF_DATE])
    by_number = {pick["number"]: pick for pick in result["picks"]}
    assert {n for n in by_number if n < 10}, "用例必须覆盖个位号"
    assert {n for n in by_number if n >= 10}, "用例必须覆盖两位号"

    # 个位号：02 → 蛇、09 → 狗
    assert (by_number[2]["zodiac"], by_number[2]["zodiac_label"]) == ("SNAKE", "蛇")
    assert (by_number[9]["zodiac"], by_number[9]["zodiac_label"]) == ("DOG", "狗")
    # 两位号：12 → 羊
    assert (by_number[12]["zodiac"], by_number[12]["zodiac_label"]) == ("GOAT", "羊")

    # 逐号与权威生肖表核对（绝不另造一套映射）
    for number, pick in by_number.items():
        assert pick["zodiac"] == zodiac_of(number, ZODIAC_REF_DATE)


def test_recommend_pick_zodiac_agrees_with_latest_zodiac_chip():
    """卡片生肖与「最新同肖」号码组永远同组：号码在同肖组 ⟺ 生肖相同。

    号码分组（zodiac_numbers，与农历年无关）与生肖表（zodiac_of，随农历年轮转）
    都是 ``(n - 1) % 12`` 的分组，农历年只决定这一组叫什么名字。
    """
    result = _zodiac_picks([ZODIAC_REF_DATE])
    latest_group = set(result["latest_zodiac"])
    latest_code = result["latest_zodiac_code"]
    assert latest_code is not None
    for pick in result["picks"]:
        in_latest_group = pick["number"] in latest_group
        assert in_latest_group is (pick["zodiac"] == latest_code)
        # 同一分组口径下，重肖标记也必须与「生肖相同」一致
        assert in_latest_group is (pick["is_repeat_zodiac"] is True)


def test_recommend_zodiac_follows_lunar_new_year_boundary():
    """春节换肖：同一号码在春节前后属不同生肖，但同肖号码组不变。"""
    before = _zodiac_picks([ZODIAC_REF_NEW_YEAR_EVE])  # 仍属 2025 蛇年
    after = _zodiac_picks([ZODIAC_REF_NEW_YEAR])       # 已是 2026 马年

    assert before["zodiac_year"] == 2025
    assert after["zodiac_year"] == 2026
    # 01 号所属生肖：蛇年 = 蛇，马年 = 马
    assert before["latest_zodiac_code"] == "SNAKE"
    assert after["latest_zodiac_code"] == "HORSE"
    # 号码分组与农历年无关，两年完全一致
    assert before["latest_zodiac"] == after["latest_zodiac"] == zodiac_numbers(1)

    before_picks = {pick["number"]: pick for pick in before["picks"]}
    after_picks = {pick["number"]: pick for pick in after["picks"]}
    assert before_picks[9]["zodiac"] == "ROOSTER"   # 蛇年 09 → 鸡
    assert after_picks[9]["zodiac"] == "DOG"        # 马年 09 → 狗
    assert before_picks[12]["zodiac"] == "HORSE"    # 蛇年 12 → 马
    assert after_picks[12]["zodiac"] == "GOAT"      # 马年 12 → 羊


def test_recommend_zodiac_degrades_without_reference_date():
    """拿不到开奖日就不给生肖（字段仍在，值为 null），绝不拿「今天」猜农历年。"""
    result = recommend(latest=1, previous=None, history_numbers=[1])
    assert result["zodiac_date"] is None
    assert result["zodiac_year"] is None
    assert result["latest_zodiac_code"] is None
    assert result["latest_zodiac_label"] is None
    for pick in result["picks"]:
        assert pick["zodiac"] is None
        assert pick["zodiac_label"] is None
    # 与农历年无关的号码组照常提供
    assert result["latest_zodiac"] == zodiac_numbers(1)


def test_recommend_zodiac_degrades_outside_known_year_table():
    """参照日早于已知农历年表：如实留空，不按最接近的年份猜。"""
    result = _zodiac_picks([date(2024, 5, 1)])
    assert result["zodiac_date"] == "2024-05-01"
    assert result["zodiac_year"] is None
    assert result["latest_zodiac_code"] is None
    assert all(pick["zodiac"] is None for pick in result["picks"])
    assert all(pick["zodiac_label"] is None for pick in result["picks"])


def test_recommend_api_picks_carry_zodiac():
    """接口层回归：/api/recommend 每注都带生肖，与 /api/draws 的农历年表同源。"""
    from fastapi.testclient import TestClient

    import repository
    from main import app

    with TestClient(app) as client:
        store = asyncio.run(repository.get_store())
        asyncio.run(store.clear_draws())
        client.post(
            "/api/draws/quick",
            json={
                "special_number": 21,
                "period": 1,
                "draw_date": ZODIAC_REF_DATE.isoformat(),
            },
        )
        body = client.post("/api/recommend", json={"mode": "single"}).json()

    assert body["zodiac_date"] == ZODIAC_REF_DATE.isoformat()
    assert body["zodiac_year"] == 2026
    # 2026 马年表下 21（与 09 同组）→ 狗；号码组 [9, 21, 33, 45] 不变
    assert body["latest_zodiac_code"] == "DOG"
    assert body["latest_zodiac"] == zodiac_numbers(21)
    assert body["picks"]
    for pick in body["picks"]:
        assert pick["zodiac"] == zodiac_of(pick["number"], ZODIAC_REF_DATE)
        assert pick["zodiac_label"] == ZODIAC_LABELS[pick["zodiac"]]
