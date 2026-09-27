"""算法回归测试。

覆盖：肖号分组、波动分类边界、全量推荐扫描、缺类补足、侧重角色分配、
派生大跳下限、金额分配与注数钳制。所有用例均不依赖真实数据库。
"""

from __future__ import annotations

import asyncio

import pytest

from services.lottery import (
    DEFAULT_AMOUNT_UNIT,
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
    assign_roles,
    build_copy_text,
    classify_wave,
    clamp_settings,
    random_allocation,
    random_amounts,
    recommend,
    with_derived_settings,
    zodiac_numbers,
)

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
        settings={"total_amount": BET_UNIT * pick_count, "pick_count": pick_count},
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
            settings={"total_amount": total, "pick_count": 3},
            mode=MODE_SINGLE,
        )
        assert len(result["picks"]) == 1
        assert result["picks"][0]["amount"] == expected_amount
        assert result["total_amount"] == expected_amount
        num = result["picks"][0]["number"]
        assert result["copy_text"] == f"{num}：{expected_amount}元；\n合计：{expected_amount}元。"


def test_single_mode_budget_ignores_configured_pick_count():
    """单挑的预算来自 total_amount，不再随 pick_count 缩放（预算只有一个真值）。"""
    amounts = set()
    for configured in (1, 3, 5, 10):
        result = recommend(
            latest=20,
            previous=15,
            history_numbers=[15, 20],
            settings={"total_amount": 30, "pick_count": configured},
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
        settings={"total_amount": 100, "amount_unit": 5, "pick_count": 3},
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
        settings={"total_amount": 100, "amount_unit": 5, "pick_count": 3},
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
        settings={"total_amount": 100, "amount_unit": 5, "pick_count": 3},
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
        settings={"total_amount": 10, "amount_unit": 5, "pick_count": 3},
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
    assert text == "9：34元；\n19、31：各33元；\n合计：100元。"


def test_random_copy_text_lists_each_amount():
    picks = [
        {"number": 9, "amount": 15, "role_label": "主推"},
        {"number": 19, "amount": 35, "role_label": "次选"},
        {"number": 31, "amount": 50, "role_label": "防守"},
    ]
    text = build_copy_text(MODE_RANDOM, picks)
    assert text == "9：15元；\n19：35元；\n31：50元；\n合计：100元。"


def test_even_copy_text_merges_equal_amounts():
    picks = [
        {"number": 9, "amount": 10, "role_label": "主推"},
        {"number": 19, "amount": 10, "role_label": "次选"},
        {"number": 31, "amount": 10, "role_label": "防守"},
    ]
    assert build_copy_text(MODE_EVEN, picks) == "9、19、31：各10元；\n合计：30元。"


def test_copy_text_user_sample_format():
    """用户约定样例：独额 + 同额合并 + 分号换行 + 合计。"""
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
        {"pick_count": 6, "total_amount": 50, "amount_unit": 5, "mode": MODE_EVEN}
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
