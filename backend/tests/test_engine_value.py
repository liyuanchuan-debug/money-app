"""``scripts/engine_value_analysis`` 的纯函数与口径守卫单元测试。

只测纯逻辑：不连数据库、不依赖 ``backend/data/*``（该目录被 .gitignore 忽略），
所有开奖序列都在测试内合成。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from services import analytics as A
from scripts import engine_value_analysis as E


# --------------------------------------------------------------------------- #
# 合成样本
# --------------------------------------------------------------------------- #
def synthetic_draws(count: int = 120) -> list[dict]:
    """递增日期 + 与 49 互质的步长，保证号码铺满 1..49（确定性、可复现）。"""
    start = date(2025, 1, 1)
    return [
        {
            "period": index + 1,
            "draw_date": (start + timedelta(days=index)).isoformat(),
            "special_number": ((index * 17 + 5) % 49) + 1,
        }
        for index in range(count)
    ]


def flat_rows(stake: int, amount: int, count: int) -> list[dict]:
    """合成「k=1、固定注额」的逐期结果，供 bootstrap / 空号模拟测试。"""
    return [
        {
            "period": index + 1,
            "draw_date": "2026-01-01",
            "predicted": [1],
            "actual": 1,
            "hit": False,
            "picks": [{"number": 1, "amount": amount}],
            "staked": float(stake),
        }
        for index in range(count)
    ]


# --------------------------------------------------------------------------- #
# ev_per_100 / breakeven_hit_rate
# --------------------------------------------------------------------------- #
def test_ev_per_100_is_locked_to_odds() -> None:
    assert E.ev_per_100(47) == pytest.approx(-100 * (1 - 47 / 49))
    assert E.ev_per_100(47) == pytest.approx(-4.081632653061229)
    assert E.ev_per_100(48) == pytest.approx(-2.0408163265306145)
    assert E.ev_per_100(49) == pytest.approx(0.0)
    assert E.ev_per_100(50) == pytest.approx(2.0408163265306145)
    # 只有赔率能改变期望：换赔率期望会变，其它旋钮不会（本函数只读赔率）
    assert E.ev_per_100(47) != E.ev_per_100(48)


def test_breakeven_hit_rate_matches_k_over_odds() -> None:
    assert E.breakeven_hit_rate(10, 47) == pytest.approx(10 / 47)
    assert E.breakeven_hit_rate(1, 49) == pytest.approx(1 / 49)
    assert E.breakeven_hit_rate(0, 47) == pytest.approx(0.0)
    assert E.breakeven_hit_rate(10, 0) is None
    assert E.breakeven_hit_rate(10, -1) is None
    assert E.breakeven_hit_rate(-1, 47) is None


# --------------------------------------------------------------------------- #
# flat_equal_amounts
# --------------------------------------------------------------------------- #
def test_flat_equal_amounts_divides_exactly() -> None:
    assert E.flat_equal_amounts(100, 10, 5) == [10] * 10
    assert E.flat_equal_amounts(50, 10, 5) == [5] * 10
    assert sum(E.flat_equal_amounts(50, 10, 5)) == 50


def test_flat_equal_amounts_remainder_to_earlier_picks() -> None:
    assert E.flat_equal_amounts(100, 3, 5) == [35, 35, 30]
    assert sum(E.flat_equal_amounts(100, 3, 5)) == 100
    assert E.flat_equal_amounts(100, 6, 5) == [20, 20, 15, 15, 15, 15]


def test_flat_equal_amounts_floor_and_insufficient_budget() -> None:
    # 预算不是粒度整数倍 → 向下取整后不分配差额
    assert E.flat_equal_amounts(102, 10, 5) == [10] * 10
    # 预算只够覆盖 1 注 → 只输出 1 注，绝不产出 0 元注
    assert E.flat_equal_amounts(5, 10, 5) == [5]
    assert E.flat_equal_amounts(4, 10, 5) == []
    assert E.flat_equal_amounts(0, 10, 5) == []
    assert E.flat_equal_amounts(100, 0, 5) == []


# --------------------------------------------------------------------------- #
# skewness / max_drawdown / min_cumulative / period_profit_stats
# --------------------------------------------------------------------------- #
def test_skewness_symmetric_and_degenerate() -> None:
    assert E.skewness([-1.0, 0.0, 1.0]) == pytest.approx(0.0, abs=1e-12)
    assert E.skewness([5.0, 5.0, 5.0]) == pytest.approx(0.0)
    assert E.skewness([1.0, 2.0]) == 0.0  # 样本 < 3
    assert E.skewness([1.0, 2.0, 3.0, 4.0, 100.0]) > 0.0  # 右偏


def test_max_drawdown_and_min_cumulative() -> None:
    assert E.max_drawdown([1.0, -3.0, 2.0]) == pytest.approx(3.0)
    assert E.min_cumulative([1.0, -3.0, 2.0]) == pytest.approx(-2.0)
    assert E.max_drawdown([-5.0, 1.0]) == pytest.approx(5.0)
    assert E.max_drawdown([1.0, 2.0, 3.0]) == pytest.approx(0.0)
    assert E.min_cumulative([]) == pytest.approx(0.0)


def test_period_profit_stats_basic() -> None:
    stat = E.period_profit_stats([1.0, -1.0, 1.0, -1.0])
    assert stat["periods"] == 4
    assert stat["mean"] == pytest.approx(0.0)
    assert stat["sd"] == pytest.approx(1.0)
    assert stat["min"] == pytest.approx(-1.0)
    assert stat["max"] == pytest.approx(1.0)
    assert stat["p_period_up"] == pytest.approx(0.5)
    assert E.period_profit_stats([])["p_period_up"] is None


# --------------------------------------------------------------------------- #
# bootstrap_session
# --------------------------------------------------------------------------- #
def test_bootstrap_session_deterministic_and_degenerate() -> None:
    first = E.bootstrap_session([1.0, 1.0, 1.0], resamples=200, seed=7)
    again = E.bootstrap_session([1.0, 1.0, 1.0], resamples=200, seed=7)
    assert first == again
    assert first["p_session_up"] == pytest.approx(1.0)
    assert first["mdd_mean"] == pytest.approx(0.0)

    losing = E.bootstrap_session([-1.0, -1.0, -1.0], resamples=200, seed=7)
    assert losing["p_session_up"] == pytest.approx(0.0)

    empty = E.bootstrap_session([], resamples=10, seed=1)
    assert empty["p_session_up"] is None
    assert empty["p_touch_stop"] == {}


def test_bootstrap_session_touch_stop_probability() -> None:
    block = E.bootstrap_session(
        [-100.0, -100.0, -100.0], resamples=50, seed=3, stop_levels=(200, 400)
    )
    # 每段累计 −300：必然触及 200，永远不触及 400
    assert block["p_touch_stop"]["200"] == pytest.approx(1.0)
    assert block["p_touch_stop"]["400"] == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# uniform_null_session
# --------------------------------------------------------------------------- #
def test_uniform_null_session_hit_probability_is_k_over_49() -> None:
    # 单期、k=1、命中 +4600 / 未中 −100 ⇒ P(整段盈) = 1/49
    rows = flat_rows(stake=100, amount=100, count=1)
    block = E.uniform_null_session(rows, odds=47, trials=20000, seed=11)
    assert block["hit_probability_used"] == "k / 49"
    assert block["p_session_up"] == pytest.approx(1 / 49, abs=0.01)


def test_uniform_null_session_deterministic_and_empty() -> None:
    rows = flat_rows(stake=100, amount=100, count=5)
    first = E.uniform_null_session(rows, odds=47, trials=500, seed=5)
    again = E.uniform_null_session(rows, odds=47, trials=500, seed=5)
    assert first == again
    assert 0.0 <= first["p_session_up"] <= 1.0
    empty = E.uniform_null_session([], odds=47, trials=10, seed=5)
    assert empty["p_session_up"] is None


def test_uniform_null_session_detects_deep_stop() -> None:
    # k=1、注额极小相对止损：整段必然跌破 500（几乎每期都亏）
    rows = flat_rows(stake=100, amount=1, count=50)
    block = E.uniform_null_session(
        rows, odds=47, trials=200, seed=9, stop_levels=(500,)
    )
    assert block["p_touch_stop"]["500"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# percentile_of
# --------------------------------------------------------------------------- #
def test_percentile_of() -> None:
    assert E.percentile_of(2.0, [1.0, 2.0, 3.0]) == pytest.approx(100 / 3)
    assert E.percentile_of(-1.0, [1.0, 2.0]) == pytest.approx(0.0)
    assert E.percentile_of(9.0, [1.0, 2.0]) == pytest.approx(100.0)
    assert E.percentile_of(1.0, []) is None


# --------------------------------------------------------------------------- #
# 口径守卫：手工走步必须与 backtest_stats 逐期一致
# --------------------------------------------------------------------------- #
def test_engine_rows_match_backtest_stats() -> None:
    draws = synthetic_draws(120)
    series = A.normalize_draws(draws)
    settings = dict(A.DEFAULT_SETTINGS)
    rows = E.engine_rows(series, settings)
    backtest = A.backtest_stats(
        series, base_settings=settings, include_results=True,
        include_wave_breakdown=False,
    )
    check = E.verify_engine_rows(rows, backtest)
    assert check["matches"] is True
    assert check["walk_forward_hits"] == check["backtest_hits"]
    assert check["walk_forward_periods"] == check["backtest_periods"]


def test_engine_rows_reports_cost_from_settlement() -> None:
    draws = synthetic_draws(60)
    series = A.normalize_draws(draws)
    settings = {
        **A.DEFAULT_SETTINGS,
        "total_amount": 50,
        "amount_unit": 5,
        "pick_count": 5,
        "mode": "even",
    }
    rows = E.engine_rows(series, settings)
    assert rows
    for row in rows:
        assert row["staked"] == sum(p["amount"] for p in row["picks"])
        assert row["profit"] == pytest.approx(row["payout"] - row["staked"])
        assert row["hit"] == (row["actual"] in row["predicted"])


# --------------------------------------------------------------------------- #
# flatten_rows / random_rows
# --------------------------------------------------------------------------- #
def test_flatten_rows_keeps_numbers_and_equalizes_amounts() -> None:
    draws = synthetic_draws(60)
    series = A.normalize_draws(draws)
    settings = {**A.DEFAULT_SETTINGS, "total_amount": 50, "pick_count": 6}
    rows = E.engine_rows(series, settings)
    flat = E.flatten_rows(rows, budget=100, unit=5, odds=47)
    assert len(flat) == len(rows)
    for original, flattened in zip(rows, flat):
        assert [p["number"] for p in original["picks"]] == [
            p["number"] for p in flattened["picks"]
        ]
        assert sum(p["amount"] for p in flattened["picks"]) == 100
        assert flattened["profit"] == pytest.approx(
            flattened["payout"] - flattened["staked"]
        )


def test_random_rows_deterministic_and_flat_stake() -> None:
    draws = synthetic_draws(40)
    series = A.normalize_draws(draws)
    first = E.random_rows(
        series, pick_count=6, budget=50, unit=5, odds=47, seed=123
    )
    again = E.random_rows(
        series, pick_count=6, budget=50, unit=5, odds=47, seed=123
    )
    assert [r["predicted"] for r in first] == [r["predicted"] for r in again]
    for row in first:
        assert len(row["predicted"]) == 6
        assert len(set(row["predicted"])) == 6
        assert 1 <= min(row["predicted"]) and max(row["predicted"]) <= 49
        assert row["staked"] == 50
        assert row["hit"] == (row["actual"] in row["predicted"])


# --------------------------------------------------------------------------- #
# random_seed_summary
# --------------------------------------------------------------------------- #
def test_random_seed_summary_reports_distribution() -> None:
    draws = synthetic_draws(40)
    series = A.normalize_draws(draws)
    block = E.random_seed_summary(
        series, pick_count=6, budget=50, unit=5, odds=47, seeds=range(3)
    )
    assert block["seeds"] == 3
    assert block["hit_rate_distribution"]["mean"] is not None
    assert 0.0 <= block["p_session_up"] <= 1.0
    assert block["analytical_ev_per_100_staked"] == pytest.approx(E.ev_per_100(47))
    assert len(block["hit_rates"]) == 3
    assert len(block["realised_ev_per_100_list"]) == 3
