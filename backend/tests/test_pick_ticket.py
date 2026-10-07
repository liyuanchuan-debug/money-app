"""出票单（选号工具）回归测试：纯核心 / CLI 同源逻辑 / 接口 / 口径防线。

## 这份测试在守什么

1. **纪律与花费控制**：每注金额是注码粒度正整数倍、逐注不低于 ``MIN_BET_AMOUNT``、
   合计 = ``budget.staked``、``staked + unspent == requested``，且**不静默改配**；
2. **号码卫生**：重号 / 同肖 / 冷号三类标记与 ``services.lottery`` 的判定口径逐位一致；
3. **覆盖透明**：覆盖报告的每条计数都能独立重算出来（生肖 / 大小 / 奇偶 / 尾数）；
4. **可复现**：同 seed + 同数据 + 同设置 ⇒ 逐字节相同；
5. **诚实披露**：``claim == "NO_EDGE"``，期望值与 ``services.analytics`` 的算术一致，
   且整张票里**找不到任何暗示「更容易中奖」的措辞**；
6. **单一事实来源**：不给 seed 时出票逐字段等于 ``recommend()``，本模块不重写引擎；
7. **生产行为不变**：``GET /api/health`` 与 ``POST /api/stats/backtest``
   （210 期 → 38/208、verdict=noise）与改造前完全一致。

全部用例强制内存存储（见 ``tests/conftest.py``），不连真实数据库。
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.analytics import DATA_STATUS_OK, NUMBERS, backtest_stats
from services.lottery import (
    DEFAULT_SETTINGS,
    MIN_BET_AMOUNT,
    MODE_EVEN,
    MODE_RANDOM,
    MODE_SINGLE,
    MODE_WEIGHTED,
    clamp_settings,
    recommend,
    zodiac_group,
)
from services.pick_ticket import (
    CLAIM_NO_EDGE,
    SELECTION_ENGINE_PICKS,
    SELECTION_ENGINE_RANKING,
    ROW_FIELDS,
    TicketDataError,
    batch_index,
    build_ticket,
    explain_effective_settings,
    normalize_ticket_draws,
    seed_key,
    simulate_ticket,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
# 210 期真实开奖（70..279，升序）；backend/data/* 被 .gitignore 忽略，缺失则跳过相关用例
REAL_DRAWS_FILE = BACKEND_DIR / "data" / "draws_70_279.json"

# 线上生效配置快照（与本地库 settings 的全局模板一致：small 15 / normal 20 / 避开重肖 /
# 走势中频 / 点阵开启 …）。用默认设置回测得到的是 24/208，用这一份才是 38/208 ——
# 「38/208」是**这一份配置**的性质，不是引擎的固有性质，所以必须显式钉住配置。
PRODUCTION_SETTINGS: dict = {
    "small_max": 15,
    "normal_max": 20,
    "amount_unit": 5,
    "mode": MODE_EVEN,
    "pick_count": 10,
    "odds": 47.0,
    "exclude_repeat_zodiac": True,
    "include_repeat_number": True,
    "repeat_number_weight": 0.5,
    "repeat_zodiac_weight": 0.8,
    "stale_periods": 60,
    "stale_weight": 0.3,
    "lattice_enabled": True,
    "lattice_window": 30,
    "role_w_primary": 3.0,
    "role_w_secondary": 2.0,
    "role_w_defense": 1.0,
    "trend_bias": "mid",
    "trend_window": 20,
    "avoid_cold_enabled": False,
    "avoid_cold_days": 60,
    "pick_strategy": "wave_round",
    "score_w_focus": 1.0,
    "score_w_mid": 2.0,
    "score_w_omit": 0.0,
    "score_w_diff": 0.5,
}

# 任何暗示「提高中奖能力」的措辞：出票单里一个都不许出现
BANNED_PHRASES = (
    "必中",
    "稳赚",
    "包中",
    "提高中奖",
    "提高命中",
    "命中率提升",
    "胜率提升",
    "更容易中",
    "更有机会",
    "大概率中",
    "预测中奖",
    "推荐指数",
    "置信度",
    "优选概率",
)

# 允许落进「枚举位」的字符：英文 / 数字 / 下划线；汉字一律不许出现在这些字段里
ENUM_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


# --------------------------------------------------------------------------- #
# 夹具 / 构造器
# --------------------------------------------------------------------------- #
def _synthetic_draws(length: int = 64, start: date = date(2026, 3, 1)) -> list[dict]:
    """确定性手工序列（升序，农历马年内 → 生肖映射稳定）。

    步长 17 与 49 互质，因此 49 期内正好遍历全部号码一次；60+ 期后必然出现
    「样本内从未出现」的号码 → 冷号标记可被覆盖到。
    """
    return [
        {
            "period": 100 + index,
            "draw_date": (start + timedelta(days=index)).isoformat(),
            "special_number": ((index * 17) % 49) + 1,
        }
        for index in range(length)
    ]


def _settings(**overrides) -> dict:
    return clamp_settings({**DEFAULT_SETTINGS, **overrides})


def _ticket(**kwargs) -> dict:
    draws = kwargs.pop("draws", None) or _synthetic_draws()
    settings = kwargs.pop("settings", None) or _settings()
    return build_ticket(draws, settings=settings, **kwargs)


def _real_draws() -> list[dict]:
    if not REAL_DRAWS_FILE.exists():  # pragma: no cover - 依赖本地 data/*
        pytest.skip(f"缺少本地真实开奖文件：{REAL_DRAWS_FILE}")
    raw = json.loads(REAL_DRAWS_FILE.read_text(encoding="utf-8"))
    return [
        {
            "period": int(row["period"]),
            "draw_date": str(row["draw_date"]),
            "special_number": int(row["special_number"]),
        }
        for row in raw
    ]


def _walk_values(payload, path: str = ""):
    """递归遍历票据里的所有值（含键名），供「措辞防线」扫描。"""
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield from _walk_values(key, f"{path}.{key}")
            yield from _walk_values(value, f"{path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            yield from _walk_values(value, f"{path}[{index}]")
    elif isinstance(payload, str):
        yield path, payload


# --------------------------------------------------------------------------- #
# 1. 纪律与花费控制
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("budget", "pick_count", "mode"),
    [
        (50, 10, MODE_EVEN),
        (35, 7, MODE_EVEN),
        (100, 10, MODE_WEIGHTED),
        (25, 3, MODE_SINGLE),
        (60, 8, MODE_RANDOM),
        (5, 1, MODE_EVEN),
    ],
)
def test_stake_is_granular_within_budget_and_never_silently_reallocated(
    budget: int, pick_count: int, mode: str
):
    ticket = _ticket(budget=budget, pick_count=pick_count, mode=mode)
    block = ticket["budget"]
    amounts = [pick["amount"] for pick in ticket["picks"]]

    assert block["requested"] == budget
    assert block["staked"] == sum(amounts)
    assert block["within_budget"] is True
    assert block["staked"] <= budget
    # 省下的金额如实计入 unspent，且账目自洽（不许静默改配到别的注）
    assert block["staked"] + block["unspent"] == budget
    assert block["unspent"] >= 0
    assert ticket["amounts"] == amounts
    # 预算覆盖不了的部分宁可少买，也不许出现 0 元或负数「占位注」
    assert all(amount > 0 for amount in amounts)
    assert len(amounts) <= pick_count


def test_every_pick_respects_the_per_pick_minimum():
    for budget, pick_count in ((5, 1), (50, 10), (100, 10), (35, 7)):
        ticket = _ticket(budget=budget, pick_count=pick_count)
        block = ticket["budget"]
        assert block["min_bet_amount"] == MIN_BET_AMOUNT
        assert block["per_pick_min_respected"] is True
        for pick in ticket["picks"]:
            assert pick["amount"] >= MIN_BET_AMOUNT
            assert pick["amount"] % block["amount_unit"] == 0


def test_budget_is_clamped_into_the_documented_range():
    # 请求越界数值时由 clamp_settings 兜底（这里直接调核心，路由层另有 Pydantic 校验）
    low = _ticket(budget=1, pick_count=10)
    high = _ticket(budget=10_000, pick_count=10)
    assert low["budget"]["requested"] == 5
    assert high["budget"]["requested"] == 100


# --------------------------------------------------------------------------- #
# 2. 单一事实来源：不给 seed 时逐字段等于生产引擎
# --------------------------------------------------------------------------- #
def test_without_seed_ticket_is_byte_equal_to_production_engine():
    draws = _synthetic_draws()
    settings = _settings()
    series = normalize_ticket_draws(draws)  # 最新在前
    engine = recommend(
        latest=series[0]["special_number"],
        previous=series[1]["special_number"],
        history_numbers=[row["special_number"] for row in series],
        settings=settings,
        mode=settings["mode"],
        period=series[0]["period"],
        history_dates=[row["draw_date"] for row in series],
    )
    ticket = _ticket(draws=draws, settings=settings)

    assert ticket["selection"] == SELECTION_ENGINE_PICKS
    assert [pick["number"] for pick in ticket["picks"]] == [
        pick["number"] for pick in engine["picks"]
    ]
    assert ticket["amounts"] == [pick["amount"] for pick in engine["picks"]]
    assert ticket["budget"]["staked"] == engine["staked_total"]
    # 行字段也逐位来自引擎（本模块只做字段整理，不重算）
    for row in ticket["picks"]:
        assert set(row) == set(ROW_FIELDS)


def test_seeded_ticket_uses_the_engine_ranking_without_changing_expected_value():
    ticket = _ticket(seed=3)
    assert ticket["selection"] == SELECTION_ENGINE_RANKING
    assert ticket["ranking_size"] == len(NUMBERS)
    assert ticket["seed"]["batch"] == batch_index(3) == 3
    # 滑动窗口只是换一批同带内的候选：注数与期望值都不变
    plain = _ticket()
    assert len(ticket["picks"]) == len(plain["picks"])
    assert ticket["honest"]["baseline_hit_rate"] == plain["honest"]["baseline_hit_rate"]
    assert ticket["honest"]["ev_per_100"] == plain["honest"]["ev_per_100"]


def test_ranking_window_is_a_rotation_inside_the_engine_order():
    first = _ticket(seed=1)
    second = _ticket(seed=2)
    first_numbers = [pick["number"] for pick in first["picks"]]
    second_numbers = [pick["number"] for pick in second["picks"]]
    assert first_numbers != second_numbers
    # 两批的号码仍出自同一份候选池（1..49 的子集，无越界、无重复）
    for numbers in (first_numbers, second_numbers):
        assert len(set(numbers)) == len(numbers)
        assert all(1 <= number <= 49 for number in numbers)


# --------------------------------------------------------------------------- #
# 3. 确定性：同 seed + 同数据 + 同设置 ⇒ 逐字节相同
# --------------------------------------------------------------------------- #
def test_same_seed_reproduces_the_same_ticket_byte_for_byte():
    first = _ticket(budget=50, pick_count=10, seed="abc")
    second = _ticket(budget=50, pick_count=10, seed="abc")
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )
    assert first["ticket_id"] == second["ticket_id"]
    assert first["ticket_text"] == second["ticket_text"]


def test_different_seed_changes_the_ticket_id():
    assert _ticket(seed=1)["ticket_id"] != _ticket(seed=2)["ticket_id"]


def test_seed_helpers_are_stable_and_total():
    assert seed_key(None) == ""
    assert seed_key("  7 ") == "7"
    assert batch_index(None) == 0
    assert batch_index("0") == 0
    assert batch_index("12") == 12
    # 非整数种子走 sha256 派生：可复现且非负
    assert batch_index("換一批") == batch_index("換一批")
    assert batch_index("換一批") >= 0


# --------------------------------------------------------------------------- #
# 4. 号码卫生：标记与引擎口径逐位一致
# --------------------------------------------------------------------------- #
def test_soft_flags_match_the_engine_semantics():
    draws = _synthetic_draws()
    settings = _settings()
    ticket = _ticket(draws=draws, settings=settings)
    series = normalize_ticket_draws(draws)
    latest = series[0]["special_number"]
    stale_periods = settings["stale_periods"]
    sample_size = len(series)

    assert sample_size >= stale_periods, "夹具必须满足冷号判定的样本门槛，否则断言无意义"

    for pick in ticket["picks"]:
        number = pick["number"]
        reasons = pick["soft_reasons"]
        assert pick["is_repeat_number"] is (number == latest)
        if "repeat_number" in reasons:
            assert number == latest
        if "repeat_zodiac" in reasons:
            assert zodiac_group(number) == zodiac_group(latest)
            assert number != latest  # 重号与同肖互斥
        # 重号与同肖不可能同时出现（与 soft_penalty_weight 的 elif 一致）
        assert not ("repeat_number" in reasons and "repeat_zodiac" in reasons)
        if "stale" in reasons:
            periods = pick["periods_since_last"]
            assert periods is None or periods >= stale_periods
            assert pick["is_stale"] is True
        else:
            assert pick["is_stale"] is False
        # 权重恒等于三类系数的乘积（只降不升）
        expected = 1.0
        if "repeat_number" in reasons:
            expected *= float(settings["repeat_number_weight"])
        if "repeat_zodiac" in reasons:
            expected *= float(settings["repeat_zodiac_weight"])
        if "stale" in reasons:
            expected *= float(settings["stale_weight"])
        assert pick["soft_weight"] == pytest.approx(expected, abs=1e-3)
        assert pick["soft_penalized"] is (expected < 1.0)


def test_all_weight_one_is_equivalent_to_no_penalty():
    """权重全 1.0：标记照旧如实列出（与引擎同口径），但权重与金额都不被压低。"""
    settings = _settings(
        repeat_number_weight=1.0, repeat_zodiac_weight=1.0, stale_weight=1.0
    )
    ticket = _ticket(settings=settings)
    assert all(pick["soft_weight"] == 1.0 for pick in ticket["picks"])
    assert all(pick["soft_penalized"] is False for pick in ticket["picks"])
    assert ticket["budget"]["unspent"] == 0  # 没有因降权而省下的钱


def test_repeat_number_and_repeat_zodiac_pool_switches_are_honoured():
    latest = normalize_ticket_draws(_synthetic_draws())[0]["special_number"]
    group = zodiac_group(latest)

    avoiding = _ticket(settings=_settings(exclude_repeat_zodiac=True))
    open_pool = _ticket(settings=_settings(exclude_repeat_zodiac=False))

    avoiding_pool = set(avoiding["coverage"]["candidate_pool_numbers"])
    open_numbers = set(open_pool["coverage"]["candidate_pool_numbers"])
    # 避开重肖：最新一期同肖整组被排除；不避开：整组都在池内
    assert not any(zodiac_group(n) == group for n in avoiding_pool)
    assert all(zodiac_group(n) == group for n in open_numbers - avoiding_pool)
    assert avoiding_pool < open_numbers

    # 「重号是否留在候选池」控制的是 latest 本身
    keeping = _ticket(
        settings=_settings(include_repeat_number=True, exclude_repeat_zodiac=False)
    )
    dropping = _ticket(
        settings=_settings(include_repeat_number=False, exclude_repeat_zodiac=False)
    )
    assert latest in set(keeping["coverage"]["candidate_pool_numbers"])
    assert latest not in set(dropping["coverage"]["candidate_pool_numbers"])


# --------------------------------------------------------------------------- #
# 5. 覆盖报告：每条计数都能独立重算
# --------------------------------------------------------------------------- #
def test_coverage_report_is_arithmetically_correct():
    ticket = _ticket(budget=50, pick_count=10)
    coverage = ticket["coverage"]
    picks = [pick["number"] for pick in ticket["picks"]]

    assert coverage["numbers"] == sorted(set(picks))
    assert coverage["covered_count"] == len(set(picks))
    assert coverage["total_numbers"] == len(NUMBERS) == 49
    assert coverage["covered_count"] + coverage["uncovered_count"] == 49
    assert coverage["big_small"]["big"] + coverage["big_small"]["small"] == len(picks)
    assert coverage["odd_even"]["odd"] + coverage["odd_even"]["even"] == len(picks)
    assert sum(row["count"] for row in coverage["tail_digit"]) == len(picks)
    assert sum(row["count"] for row in coverage["zodiac"]) == len(picks)
    assert [row["group"] for row in coverage["zodiac"]] == list(range(12))
    # 每注金额各不相同也可能相同：覆盖计数与「注」无关，只按号码去重
    assert len(coverage["numbers"]) == len(picks)

    # 交叉验证：生肖 / 大小 / 奇偶 / 尾数逐号重算
    big_min = coverage["big_small"]["big_min"]
    assert coverage["big_small"]["big"] == sum(1 for n in picks if n >= big_min)
    assert coverage["odd_even"]["odd"] == sum(1 for n in picks if n % 2 == 1)
    for row in coverage["tail_digit"]:
        assert row["count"] == sum(1 for n in picks if n % 10 == row["digit"])
    for row in coverage["zodiac"]:
        assert row["count"] == sum(1 for n in picks if zodiac_group(n) == row["group"])


def test_coverage_separates_candidate_pool_from_the_full_number_range():
    ticket = _ticket(draws=_synthetic_draws(), settings=_settings(exclude_repeat_zodiac=True))
    coverage = ticket["coverage"]
    pool = set(coverage["candidate_pool_numbers"])

    assert coverage["candidate_pool_size"] == len(pool)
    assert pool <= set(NUMBERS)
    assert set(coverage["excluded_numbers"]) == set(NUMBERS) - pool
    assert set(coverage["numbers"]) <= pool
    assert coverage["picks_within_candidate_pool"] == coverage["numbers"]


# --------------------------------------------------------------------------- #
# 6. 诚实页脚：期望值算术与 analytics 同源
# --------------------------------------------------------------------------- #
def test_honest_footer_ev_matches_the_analytics_arithmetic():
    ticket = _ticket(budget=50, pick_count=10)
    honest = ticket["honest"]
    odds = ticket["settings"]["odds"]
    count = len(NUMBERS)
    stake = ticket["budget"]["staked"]
    picks = len(ticket["picks"])

    assert honest["claim"] == CLAIM_NO_EDGE == ticket["claim"]
    assert honest["ev_per_100"] == pytest.approx((odds / count - 1.0) * 100.0)
    assert honest["ev_per_100"] == pytest.approx((47 / 49 - 1) * 100)
    assert honest["expected_return"] == pytest.approx(stake * odds / count)
    assert honest["expected_loss_for_this_ticket"] == pytest.approx(
        stake - honest["expected_return"]
    )
    assert honest["stake_total"] == stake
    assert honest["baseline_hit_rate"] == pytest.approx(picks / count)
    distribution = honest["hit_distribution"]
    assert distribution["kind"] == "bernoulli"
    assert distribution["p_zero_hits"] + distribution["p_at_least_one_hit"] == pytest.approx(1.0)
    assert distribution["p_at_least_one_hit"] == pytest.approx(picks / count)


def test_honest_footer_in_sample_block_comes_from_the_real_backtest():
    draws = _real_draws()
    settings = clamp_settings(PRODUCTION_SETTINGS)
    ticket = _ticket(draws=draws, settings=settings, budget=50, pick_count=10)
    in_sample = ticket["honest"]["in_sample"]

    assert in_sample["data_status"] == DATA_STATUS_OK
    outcome = backtest_stats(
        draws,
        base_settings=settings,
        include_wave_breakdown=False,
        include_results=True,
    )
    assert in_sample["evaluated"] == outcome["evaluated"] == 208
    assert in_sample["hits"] == outcome["hits"] == 38
    assert in_sample["hit_rate"] == pytest.approx(outcome["hit_rate"])
    assert in_sample["hit_rate"] == pytest.approx(0.18269, abs=5e-5)
    assert in_sample["random_baseline_hit_rate"] == pytest.approx(10 / 49)
    assert in_sample["verdict"] == "noise"
    assert in_sample["within_noise"] is True

    # 历史最久连续未中：独立重算一遍（不得留空、不得编造）
    worst = 0
    streak = 0
    for row in outcome["results"]:
        if row["hit"]:
            streak = 0
            continue
        streak += 1
        worst = max(worst, streak)
    assert in_sample["max_dry_streak_periods"] == worst == 20


def test_simulate_reports_an_honest_distribution_for_the_ticket_shape():
    ticket = _ticket(budget=50, pick_count=10)
    result = simulate_ticket(ticket, periods=208)
    odds = ticket["settings"]["odds"]
    stake = ticket["budget"]["staked"]
    picks = len(ticket["picks"])

    assert result["claim"] == CLAIM_NO_EDGE
    assert result["periods"] == 208
    assert result["orders"] == picks
    assert result["stake_total"] == stake
    assert result["hit_rate_per_period"] == pytest.approx(picks / 49)
    assert result["p_at_least_one_hit_period"] + result["p_no_hit_in_periods"] == pytest.approx(1.0)
    assert result["expected_staked"] == pytest.approx(stake * 208)
    assert result["expected_loss"] == pytest.approx(result["expected_staked"] * (1 - odds / 49))
    assert result["expected_profit"] == pytest.approx(-result["expected_loss"])
    assert result["expected_profit"] < 0  # 期望恒为负：这是本工具存在的理由
    assert result["profit_p05"] < result["expected_profit"] < result["profit_p95"]


# --------------------------------------------------------------------------- #
# 7. 措辞防线：不许出现任何「提高中奖能力」的暗示
# --------------------------------------------------------------------------- #
def test_claim_marker_is_exactly_no_edge():
    ticket = _ticket()
    assert ticket["claim"] == "NO_EDGE"
    assert ticket["honest"]["claim"] == "NO_EDGE"
    assert ticket["ticket_version"] == 1


def test_ticket_never_contains_predictive_wording():
    ticket = _ticket(budget=50, pick_count=10, seed=2)
    for path, value in _walk_values(ticket):
        for phrase in BANNED_PHRASES:
            assert phrase not in value, f"{path} 出现暗示性措辞：{phrase}"


def test_enum_fields_are_english_or_numeric():
    ticket = _ticket(budget=50, pick_count=10, seed=1)
    enum_values = [
        ("claim", ticket["claim"]),
        ("selection", ticket["selection"]),
        ("mode", ticket["mode"]),
        ("data.data_status", ticket["data"]["data_status"]),
        ("honest.in_sample.verdict", ticket["honest"]["in_sample"]["verdict"]),
        ("honest.hit_distribution.kind", ticket["honest"]["hit_distribution"]["kind"]),
        ("coverage.big_small.big_min", ticket["coverage"]["big_small"]["big_min"]),
    ]
    for pick in ticket["picks"]:
        enum_values.append(("picks.role", pick["role"]))
        enum_values.append(("picks.wave_type", pick["wave_type"]))
        enum_values.append(("picks.zodiac", pick["zodiac"]))
        for reason in pick["soft_reasons"]:
            enum_values.append(("picks.soft_reasons", reason))

    for path, value in enum_values:
        if value is None:
            continue
        assert ENUM_PATTERN.match(str(value)), f"{path} 写了非英文枚举：{value!r}"


# --------------------------------------------------------------------------- #
# 8. 数据不足 / 输入规整
# --------------------------------------------------------------------------- #
def test_insufficient_data_raises_instead_of_faking_a_ticket():
    with pytest.raises(TicketDataError):
        build_ticket([], settings=_settings())
    with pytest.raises(TicketDataError):
        build_ticket(_synthetic_draws(1), settings=_settings())


def test_draw_normalisation_accepts_any_order_and_drops_unusable_rows():
    rows = [
        {"period": 3, "draw_date": "2026-03-03", "special_number": 9},
        {"period": 1, "draw_date": "2026-03-01", "special_number": 7},
        {"period": 2, "draw_date": "2026-03-02", "number": 8},  # 兼容旧字段名
        {"period": 4, "draw_date": "", "special_number": 10},  # 缺日期 → 跳过
        {"period": 5, "draw_date": "2026-03-05", "special_number": 99},  # 越界 → 跳过
    ]
    series = normalize_ticket_draws(rows)
    assert [row["special_number"] for row in series] == [9, 8, 7]
    assert [row["period"] for row in series] == [3, 2, 1]


def test_explain_effective_settings_labels_every_source():
    settings = _settings(pick_count=7)
    report = explain_effective_settings(
        settings,
        stored=_settings(pick_count=3),
        overrides={"pick_count": 7, "total_amount": 50},
    )
    by_key = {row["key"]: row for row in report["fields"]}
    assert by_key["pick_count"]["source"] == "request_override"
    assert by_key["pick_count"]["override_value"] == 7
    assert by_key["total_amount"]["source"] == "request_override"
    assert by_key["odds"]["source"] == "stored_settings"
    assert by_key["mode"]["source"] == "stored_settings"
    assert report["ev_per_100"] == pytest.approx((47 / 49 - 1) * 100)
    assert "-4.0816" in report["ev_per_100_arithmetic"]
    assert report["settings"]["pick_count"] == 7


# --------------------------------------------------------------------------- #
# 9. 接口层：新端点 + 生产行为不变的守卫
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(monkeypatch):
    """接口用例固定走开发旁路，**不继承**开发机 ``.env`` 里的 ``AUTH_ENFORCED=true``。

    ``services.auth.auth_enforced()`` 每次调用都读 ``os.getenv``，所以这里用
    ``monkeypatch.setenv`` 就足够（交付说明里另有一份开启鉴权的全量跑法）。这样
    本文件的通过数不会被开发机 ``.env`` 污染成「环境假失败」。
    """
    monkeypatch.setenv("AUTH_ENFORCED", "false")
    from main import app

    with TestClient(app) as test_client:
        yield test_client


def _import_draws(client: TestClient, draws: list[dict]) -> None:
    response = client.post("/api/import", json={"draws": draws})
    assert response.status_code == 200, response.text


def _apply_production_settings(client: TestClient) -> None:
    response = client.put("/api/settings", json=PRODUCTION_SETTINGS)
    assert response.status_code == 200, response.text


def test_pick_ticket_endpoint_returns_the_same_object_as_the_core(client: TestClient):
    draws = _synthetic_draws()
    _import_draws(client, draws)

    response = client.post("/api/pick/ticket", json={"budget": 50, "pick_count": 10})
    assert response.status_code == 200, response.text
    body = response.json()
    # 接口只是编排：与直接调用纯核心（用同一份存储设置）必须逐字段相同
    stored = clamp_settings(client.get("/api/settings").json())
    expected = build_ticket(draws, settings=stored, budget=50, pick_count=10)
    assert body == expected


def test_pick_ticket_endpoint_honours_toggles_and_seed(client: TestClient):
    _import_draws(client, _synthetic_draws())

    body = client.post(
        "/api/pick/ticket",
        json={
            "budget": 50,
            "pick_count": 6,
            "seed": 4,
            "stale_weight": 1.0,
            "lattice_enabled": False,
        },
    ).json()
    assert body["seed"]["value"] == 4
    assert body["budget"]["requested"] == 50
    assert body["settings"]["stale_weight"] == 1.0
    assert body["settings"]["lattice_enabled"] is False
    assert all("stale" not in pick["soft_reasons"] or pick["soft_weight"] == 1.0
               for pick in body["picks"])


def test_pick_ticket_endpoint_reports_insufficient_data_as_400(client: TestClient):
    response = client.post("/api/pick/ticket", json={"budget": 50, "pick_count": 10})
    assert response.status_code == 400
    assert "数据不足" in response.json()["detail"]


def test_pick_ticket_endpoint_rejects_out_of_range_budget(client: TestClient):
    assert client.post("/api/pick/ticket", json={"budget": 1}).status_code == 422
    assert client.post("/api/pick/ticket", json={"budget": 1000}).status_code == 422
    assert client.post("/api/pick/ticket", json={"pick_count": 99}).status_code == 422


def test_pick_simulate_endpoint_matches_the_core(client: TestClient):
    _import_draws(client, _synthetic_draws())
    ticket = client.post("/api/pick/ticket", json={"budget": 50, "pick_count": 10}).json()

    body = client.post(
        "/api/pick/simulate", json={"ticket": ticket, "periods": 30}
    ).json()
    assert body == simulate_ticket(ticket, periods=30)


def test_pick_freeze_adapter_degrades_when_the_ledger_module_is_missing(monkeypatch):
    """账本模块不存在时必须能整层降级，且绝不抛错到 500。"""
    from services import pick_freeze

    monkeypatch.setattr(pick_freeze, "_load_attempted", True)
    monkeypatch.setattr(pick_freeze, "_cached_module", None)
    monkeypatch.setattr(pick_freeze, "_load_error", "ModuleNotFoundError: nope")

    assert pick_freeze.is_available() is False
    assert pick_freeze.ledger_path() is None
    assert pick_freeze.unavailable_reason() == "ModuleNotFoundError: nope"
    assert pick_freeze.status([]) == {
        "available": False,
        "reason": "ModuleNotFoundError: nope",
    }
    with pytest.raises(pick_freeze.FreezeUnavailable):
        pick_freeze.freeze_ticket(ticket={}, draws=[])


def test_ledger_and_freeze_endpoints_degrade_gracefully(client: TestClient, monkeypatch):
    from routers import pick as pick_router

    monkeypatch.setattr(pick_router.pick_freeze, "is_available", lambda: False)
    monkeypatch.setattr(
        pick_router.pick_freeze, "unavailable_reason", lambda: "ledger absent"
    )
    _import_draws(client, _synthetic_draws())

    ledger = client.get("/api/pick/ledger")
    assert ledger.status_code == 200
    assert ledger.json() == {
        "available": False,
        "reason": "ledger absent",
        "note": "账本模块当前不可用；出票与模拟不受影响。",
    }

    frozen = client.post("/api/pick/freeze", json={"ticket": {}})
    assert frozen.status_code == 501
    assert "ledger absent" in frozen.json()["detail"]

    # 降级不影响出票：票照常生成、照常可导出
    ticket = client.post("/api/pick/ticket", json={"budget": 50}).json()
    assert ticket["claim"] == "NO_EDGE"
    assert ticket["ticket_text"]


def test_health_endpoint_is_unchanged(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_backtest_on_210_real_draws_is_unchanged(client: TestClient):
    """生产行为守卫：线上配置 + 210 期 → 38/208、命中率 0.18269、verdict=noise。

    这条断言同时钉住了 ``services/lottery`` 的选号行为与 ``services/analytics``
    的统计口径：任何一边被改动，这里立刻红。
    """
    _import_draws(client, _real_draws())
    _apply_production_settings(client)

    body = client.post(
        "/api/stats/backtest", json={"pick_count": 10, "mode": "even"}
    ).json()
    assert body["evaluated"] == 208
    assert body["hits"] == 38
    assert body["hit_rate"] == pytest.approx(0.18269, abs=5e-5)
    assert body["verdict"]["kind"] == "noise"
    assert body["verdict"]["within_noise"] is True
    assert body["random_baseline_hit_rate"] == pytest.approx(10 / 49)


def test_production_paths_do_not_gain_a_pick_dependency():
    """线上路径守卫：``services/lottery.py`` / ``main.py`` / 其它 ``routers/*``
    里不许出现对出票模块的依赖（出票工具只**消费**引擎，绝不反向接入推荐路径）。"""
    for relative in ("main.py", "services/lottery.py"):
        text = (BACKEND_DIR / relative).read_text(encoding="utf-8")
        assert "pick_ticket" not in text
        assert "pick_freeze" not in text
    for path in sorted((BACKEND_DIR / "routers").rglob("*.py")):
        if path.name == "pick.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "pick_ticket" not in text, f"{path.name} 依赖了出票核心"
        assert "pick_freeze" not in text, f"{path.name} 依赖了台账适配层"
