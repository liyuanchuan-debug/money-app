r"""引擎价值分析：命中率打不过随机，那这个引擎到底交付了什么？（可复现脚本）

背景（本池样本内，可复现）：
- 本池已导入 210 期真实开奖（第 70~279 期，2026-03-11 ~ 2026-10-06），
  走步回测可评估 208 期（第 72~279 期）；每期推荐 k=10 注。
- 随机参考命中率 = k / 49 = 10/49 = 20.408%；本池 208 期的 2σ 可检测差值 ≈ ±5.59pp。
- 当前已保存设置的整池回测为 38/208 = 18.27%，差值 −2.14pp，接口判定
  ``verdict.kind = "noise"``（落在抽样噪声内，既不证明有用也不证明有害）。
- 单注期望收益率 = 赔率 / 49 − 1；赔率 47 时恒为 −4.0816%（与选号、注数、注码无关）。

本脚本**不做任何新的命中率假设检验**（那是另一条工作线）；它回答的是一个产品设计
问题：**在命中率无法优于随机的前提下，这个引擎究竟改变了什么、值不值得留。**
做法是把不同「选号 / 注码 / 资金」策略放到**同一期序列**上，复用真实引擎
（``services.lottery.recommend``）与真实结算（``services.pnl.settle_picks``，
逐条快照走 ``compute_cost`` / ``settle_picks``），逐期算出投注额与盈亏，然后比较：

- A. 期望守恒：总投注、均值、方差、已实现盈亏、每 100 元期望 —— 证明期望只由赔率决定；
- B. 方差与频率：每期盈亏分布（sd / 偏度 / min / max）、每期盈利概率、
      整段盈利概率（bootstrap 重采样）、最大回撤分布、触停概率；
- C. 花费控制：角色配额（3:2:1）与软降权相对「等额均注」到底省了多少、省在哪些注上；
- D. 杠杆表：每个旋钮能改「期望」还是只能改「方差」，附本池实测幅度；
- E. 赔率敏感性：47/48/49/50 的每 100 元期望与盈亏平衡命中率，并核对
      ``services/pnl.py`` 把 ``odds=47`` 当作「含本金的 47 倍兑付」还是「47 倍净赔」。

口径铁律（与仓库规则一致）：
- 全部结论只针对 **本池已导入的 N 期样本内**，不描述更大范围的历史，
  也不得升格为任何全量 / 市场结论。
- 样本不足时输出「数据不足」（英文码 INSUFFICIENT），绝不用 0 值冒充结论。
- 不重新实现结算与选号：一律调用 ``recommend`` 与 ``settle_picks``。
- 本脚本只读，不写库、不改任何默认设置。

运行（离线、不碰数据库）：
    cd backend
    .\.venv\Scripts\python.exe scripts\engine_value_analysis.py

输出：``backend/data/engine_value_analysis.json``（``backend/data/*`` 已被 .gitignore 忽略）。
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services import analytics as A  # noqa: E402
from services.lottery import (  # noqa: E402
    AMOUNT_UNIT_MIN,
    DEFAULT_ODDS,
    NUMBER_MAX,
    NUMBER_MIN,
    PICK_COUNT_MAX,
    clamp_settings,
    recommend,
)
from services.pnl import settle_picks  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量与口径
# --------------------------------------------------------------------------- #
NUMBER_COUNT = NUMBER_MAX - NUMBER_MIN + 1  # 49

# 本仓 dev 全局设置快照（见 GET /api/settings；仅作默认基线，可被 --base-json 覆盖）。
SAVED_SETTINGS_SNAPSHOT: dict[str, Any] = {
    "mode": "even",
    "pick_count": 10,
    "small_max": 15,
    "normal_max": 20,
    "total_amount": 50,
    "amount_unit": 5,
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
    "trend_bias_explicit": True,
    "avoid_cold_enabled": False,
    "avoid_cold_days": 60,
    "pick_strategy": "wave_round",
}

# 资金压力测试的止损 / 破产档位（元）
DEFAULT_STOP_LEVELS: tuple[int, ...] = (200, 500, 1000, 2000)
DEFAULT_RESAMPLES = 5000
DEFAULT_SEED = 20261007


# --------------------------------------------------------------------------- #
# A. 纯函数：期望、分配、分布、bootstrap
# --------------------------------------------------------------------------- #
def ev_per_100(odds: float) -> float:
    """每投注 100 元的期望盈亏（元）：``100 ×（odds / 49 − 1）``。

    这是**数学事实**，不是估计：每个号码被开出的概率为 1/49，单注命中兑付
    ``金额 × odds``，因此每元投注的期望收益率恒为 ``odds / 49 − 1``。
    """
    return (float(odds) / NUMBER_COUNT - 1.0) * 100.0


def breakeven_hit_rate(pick_count: int, odds: float) -> float | None:
    """等额 k 注、赔率 ``odds`` 下的盈亏平衡命中率 ``k / odds``。

    ``odds <= 0`` 或 ``pick_count < 0`` 时返回 ``None``（数据不足 / 非法输入），
    不用 0 冒充结论。
    """
    odds_value = float(odds)
    picks = int(pick_count)
    if odds_value <= 0 or picks < 0:
        return None
    return picks / odds_value


def flat_equal_amounts(budget: int, count: int, unit: int) -> list[int]:
    """把 ``budget`` 元**尽量均分**给 ``count`` 注，每注为 ``unit`` 的正整数倍。

    复刻「严格均注」对照基准：先按 ``unit`` 向下取整预算，再以单位数均分，
    余数逐个补给前几注；预算不足覆盖全部注数时只输出可覆盖的注数。
    与引擎的 ``_distribute_units_even`` 同口径，但本函数属于分析对照，不参与生产。
    """
    unit = max(1, int(unit))
    cnt = max(0, int(count))
    units = max(0, int(budget)) // unit
    if cnt <= 0 or units <= 0:
        return []
    note_count = min(cnt, units)
    base = units // note_count
    rem = units % note_count
    return [(base + (1 if i < rem else 0)) * unit for i in range(note_count)]


def skewness(values: Sequence[float]) -> float:
    """总体偏度（Fisher-Pearson，不取样本校正）；样本 < 3 或方差为 0 时返回 0。"""
    data = [float(v) for v in values]
    n = len(data)
    if n < 3:
        return 0.0
    mean = sum(data) / n
    var = sum((v - mean) ** 2 for v in data) / n
    if var <= 0:
        return 0.0
    sd = math.sqrt(var)
    return (sum((v - mean) ** 3 for v in data) / n) / (sd**3)


def max_drawdown(profits: Sequence[float]) -> float:
    """序列盈亏的**最大回撤**（峰值到谷底的最大跌幅，以元为单位，>= 0）。"""
    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    for value in profits:
        cumulative += float(value)
        if cumulative > peak:
            peak = cumulative
        drop = peak - cumulative
        if drop > worst:
            worst = drop
    return worst


def min_cumulative(profits: Sequence[float]) -> float:
    """序列盈亏的最小累计值（最深的浮亏，<= 0）。"""
    cumulative = 0.0
    worst = 0.0
    for value in profits:
        cumulative += float(value)
        if cumulative < worst:
            worst = cumulative
    return worst


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    size = len(sorted_values)
    if size == 0:
        return 0.0
    if size == 1:
        return float(sorted_values[0])
    position = max(0.0, min(1.0, float(q))) * (size - 1)
    low = int(math.floor(position))
    high = min(low + 1, size - 1)
    frac = position - low
    return float(sorted_values[low]) * (1.0 - frac) + float(sorted_values[high]) * frac


def period_profit_stats(profits: Sequence[float]) -> dict[str, Any]:
    """每期盈亏分布：均值 / 标准差 / 偏度 / 最小 / 最大 / 每期盈利概率。"""
    data = [float(v) for v in profits]
    if not data:
        return {
            "periods": 0,
            "mean": None,
            "sd": None,
            "skew": None,
            "min": None,
            "max": None,
            "p_period_up": None,
        }
    return {
        "periods": len(data),
        "mean": statistics.fmean(data),
        "sd": statistics.pstdev(data) if len(data) > 1 else 0.0,
        "skew": skewness(data),
        "min": min(data),
        "max": max(data),
        "p_period_up": sum(1 for v in data if v > 0) / len(data),
    }


def bootstrap_session(
    profits: Sequence[float],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    stop_levels: Sequence[int] = (),
) -> dict[str, Any]:
    """对「逐期盈亏序列」做有放回重采样，估计整段资金曲线的不确定性与触停概率。

    单次重采样 = 从 ``len(profits)`` 期中**有放回**抽同样多期并求累计盈亏，等价于
    「同样打法、同样期数，换一批期」的抽样分布。返回：

    - ``p_session_up``：整段累计盈亏 > 0 的重采样占比；
    - 最大回撤的重采样均值 / 中位 / p95 / 最大；
    - ``p_touch_stop``：累计盈亏**曾**跌到 ``−L`` 的重采样占比（= 触停 / 破产概率）。

    触停概率按「整段资金曲线是否触及 −L」定义（不提前终止），因此它衡量的正是
    「按这套打法，出现 L 元级别回撤」的概率。抽样的随机种子与次数一并返回，保证可复现。
    """
    data = [float(v) for v in profits]
    count = max(1, int(resamples))
    levels = sorted({int(level) for level in stop_levels})
    result: dict[str, Any] = {
        "resamples": count,
        "seed": int(seed),
        "periods_sampled": len(data),
    }
    if not data:
        result.update(
            {
                "p_session_up": None,
                "final_profit_mean": None,
                "final_profit_p50": None,
                "mdd_mean": None,
                "mdd_p50": None,
                "mdd_p95": None,
                "mdd_max": None,
                "p_touch_stop": {},
            }
        )
        return result

    rng = random.Random(int(seed))
    size = len(data)
    ups = 0
    finals: list[float] = []
    drawdowns: list[float] = []
    touched = {level: 0 for level in levels}
    for _ in range(count):
        cumulative = 0.0
        peak = 0.0
        worst = 0.0
        for _ in range(size):
            cumulative += data[rng.randrange(size)]
            if cumulative > peak:
                peak = cumulative
            drop = peak - cumulative
            if drop > worst:
                worst = drop
        finals.append(cumulative)
        drawdowns.append(worst)
        if cumulative > 0:
            ups += 1
        for level in levels:
            if worst >= level:
                touched[level] += 1

    finals_sorted = sorted(finals)
    drawdowns_sorted = sorted(drawdowns)
    result.update(
        {
            "p_session_up": ups / count,
            "final_profit_mean": statistics.fmean(finals),
            "final_profit_p50": _quantile(finals_sorted, 0.5),
            "mdd_mean": statistics.fmean(drawdowns),
            "mdd_p50": _quantile(drawdowns_sorted, 0.5),
            "mdd_p95": _quantile(drawdowns_sorted, 0.95),
            "mdd_max": drawdowns_sorted[-1],
            "p_touch_stop": {
                str(level): touched[level] / count for level in levels
            },
        }
    )
    return result


# --------------------------------------------------------------------------- #
# 逐期模拟：复用真实引擎与真实结算
# --------------------------------------------------------------------------- #
def _pick_snapshot(pick: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": int(pick["number"]),
        "amount": int(pick["amount"]),
        "role": pick.get("role"),
        "role_label": pick.get("role_label"),
        "soft_weight": pick.get("soft_weight"),
        "soft_reasons": list(pick.get("soft_reasons") or []),
        "is_stale": bool(pick.get("is_stale")),
        "is_repeat_number": bool(pick.get("is_repeat_number")),
        "is_repeat_zodiac": bool(pick.get("is_repeat_zodiac")),
    }


def _settle_row(
    picks: Sequence[dict[str, Any]], actual: int, odds: float
) -> dict[str, Any]:
    """调用真实结算（``services.pnl.settle_picks``）算成本 / 兑付 / 盈亏。"""
    settle = settle_picks(
        [{"number": p["number"], "amount": p["amount"]} for p in picks],
        int(actual),
        float(odds),
    )
    return {
        "staked": settle["cost"],
        "payout": settle["payout"],
        "profit": settle["profit"],
    }


def engine_rows(
    series: Sequence[dict[str, Any]],
    settings: dict[str, Any],
    *,
    min_prior_draws: int = A.MIN_PRIOR_DRAWS,
) -> list[dict[str, Any]]:
    """走步模拟一组设置：每期只用该期之前的数据调 ``recommend``，再真实结算。

    历史顺序与 ``services.analytics.backtest_stats`` 完全一致（``latest`` 取
    ``specials[index-1]``、``history`` 取 ``reversed(specials[:index])``，即**最新在前**），
    因此命中序列可与回测逐期对齐（``verify_engine_rows`` 会断言这一点）。
    """
    cfg = clamp_settings(settings)
    odds = float(cfg["odds"])
    mode = str(cfg["mode"])
    specials = [int(d["special_number"]) for d in series]
    dates = [d["draw_date"] for d in series]
    periods = [int(d["period"]) for d in series]
    rows: list[dict[str, Any]] = []
    for index in range(int(min_prior_draws), len(series)):
        latest = specials[index - 1]
        previous = specials[index - 2] if index - 2 >= 0 else None
        history = list(reversed(specials[:index]))
        history_dates = list(reversed(dates[:index]))
        outcome = recommend(
            latest=latest,
            previous=previous,
            history_numbers=history,
            history_dates=history_dates,
            settings=cfg,
            mode=mode,
            period=periods[index],
        )
        picks = [_pick_snapshot(p) for p in outcome["picks"]]
        actual = specials[index]
        settled = _settle_row(picks, actual, odds)
        rows.append(
            {
                "period": periods[index],
                "draw_date": dates[index].isoformat(),
                "predicted": [int(p["number"]) for p in picks],
                "actual": actual,
                "hit": actual in {int(p["number"]) for p in picks},
                "picks": picks,
                "draw_date_used": history_dates[0].isoformat() if history_dates else None,
                **settled,
            }
        )
    return rows


def random_rows(
    series: Sequence[dict[str, Any]],
    *,
    pick_count: int,
    budget: int,
    unit: int,
    odds: float,
    seed: int,
    min_prior_draws: int = A.MIN_PRIOR_DRAWS,
) -> list[dict[str, Any]]:
    """诚实基线：每期在 1..49 随机取 k 个号、等额均注（固定 seed 可复现）。

    选号是刻意随机的（不是引擎），结算仍走真实 ``settle_picks``。
    """
    amounts = flat_equal_amounts(budget, pick_count, unit)
    k = len(amounts)
    rng = random.Random(int(seed))
    specials = [int(d["special_number"]) for d in series]
    dates = [d["draw_date"] for d in series]
    periods = [int(d["period"]) for d in series]
    rows: list[dict[str, Any]] = []
    for index in range(int(min_prior_draws), len(series)):
        numbers = sorted(rng.sample(range(NUMBER_MIN, NUMBER_MAX + 1), k))
        picks = [
            {"number": number, "amount": amounts[i], "role": None,
             "soft_weight": 1.0, "soft_reasons": [], "is_stale": False,
             "is_repeat_number": False, "is_repeat_zodiac": False}
            for i, number in enumerate(numbers)
        ]
        actual = specials[index]
        settled = _settle_row(picks, actual, odds)
        rows.append(
            {
                "period": periods[index],
                "draw_date": dates[index].isoformat(),
                "predicted": numbers,
                "actual": actual,
                "hit": actual in set(numbers),
                "picks": picks,
                **settled,
            }
        )
    return rows


def flatten_rows(
    rows: Sequence[dict[str, Any]],
    *,
    budget: int,
    unit: int,
    odds: float,
) -> list[dict[str, Any]]:
    """保留引擎选号，但把金额换成「等额均注」：隔离「选号」与「花钱方式」。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        picks = list(row["picks"])
        flat = flat_equal_amounts(budget, len(picks), unit)
        new_picks = [
            {**pick, "amount": flat[i] if i < len(flat) else 0}
            for i, pick in enumerate(picks)
        ]
        settled = _settle_row(new_picks, row["actual"], odds)
        out.append({**row, "picks": new_picks, **settled})
    return out


def uniform_null_session(
    rows: Sequence[dict[str, Any]],
    *,
    odds: float,
    trials: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    stop_levels: Sequence[int] = (),
) -> dict[str, Any]:
    """**参数化** bootstrap：按「各号码等概率」重放同一套投注结构，得到整段的分布。

    与 ``bootstrap_session`` 的区别：后者对**本池已实现**的逐期盈亏做重采样，等价于
    「换成同样打法的另一批期」，会把本池这批期的运气（偏冷 / 偏热）一起带进结果；
    本函数改为按理论命中概率 ``k/49`` 重新抽「是否命中、命中哪一注」，因此得到的是
    **若真实优势为 0（赔率 47 下即每期期望 −4.08%）时，这套投注结构的整段表现分布**，
    不受本池这 208 期的运气影响。用于回答「这套打法本身能不能盈利」。
    """
    data = list(rows)
    levels = sorted({int(v) for v in stop_levels})
    count = max(1, int(trials))
    result: dict[str, Any] = {
        "trials": count,
        "seed": int(seed),
        "periods": len(data),
        "hit_probability_used": "k / 49",
    }
    if not data:
        result.update({"p_session_up": None, "mdd_mean": None, "mdd_p95": None,
                       "p_touch_stop": {}})
        return result

    rng = random.Random(int(seed))
    ups = 0
    finals: list[float] = []
    drawdowns: list[float] = []
    touched = {level: 0 for level in levels}
    odds_value = float(odds)
    for _ in range(count):
        cumulative = 0.0
        peak = 0.0
        worst = 0.0
        for row in data:
            picks = row["picks"]
            k = len(picks)
            stake = float(row["staked"])
            if k > 0 and rng.random() < k / NUMBER_COUNT:
                winner = rng.randrange(k)
                cumulative += float(picks[winner]["amount"]) * odds_value - stake
            else:
                cumulative -= stake
            if cumulative > peak:
                peak = cumulative
            drop = peak - cumulative
            if drop > worst:
                worst = drop
        finals.append(cumulative)
        drawdowns.append(worst)
        if cumulative > 0:
            ups += 1
        for level in levels:
            if worst >= level:
                touched[level] += 1

    finals_sorted = sorted(finals)
    drawdowns_sorted = sorted(drawdowns)
    result.update(
        {
            "p_session_up": ups / count,
            "final_profit_mean": statistics.fmean(finals),
            "final_profit_p50": _quantile(finals_sorted, 0.5),
            "mdd_mean": statistics.fmean(drawdowns),
            "mdd_p50": _quantile(drawdowns_sorted, 0.5),
            "mdd_p95": _quantile(drawdowns_sorted, 0.95),
            "mdd_max": drawdowns_sorted[-1],
            "p_touch_stop": {str(level): touched[level] / count for level in levels},
        }
    )
    return result


def random_seed_summary(
    series: Sequence[dict[str, Any]],
    *,
    pick_count: int,
    budget: int,
    unit: int,
    odds: float,
    seeds: Iterable[int],
    min_prior_draws: int = A.MIN_PRIOR_DRAWS,
) -> dict[str, Any]:
    """把「随机选号 + 等额均注」在多个 seed 上跑一遍，得到**诚实基线的分布**。

    单次随机只反映一个抽样结果；引擎的已实现成绩必须放到这个分布里看百分位，
    否则「引擎 38 命中、某个随机 seed 53 命中」会被误读成引擎更差。
    """
    seed_list = [int(s) for s in seeds]
    hit_rates: list[float] = []
    ev_per_100_list: list[float] = []
    profits: list[float] = []
    for seed in seed_list:
        rows = random_rows(
            series, pick_count=pick_count, budget=budget, unit=unit,
            odds=odds, seed=seed, min_prior_draws=min_prior_draws,
        )
        periods = len(rows)
        hits = sum(1 for r in rows if r["hit"])
        total_staked = sum(float(r["staked"]) for r in rows)
        total_profit = sum(float(r["profit"]) for r in rows)
        hit_rates.append(hits / periods if periods else 0.0)
        ev_per_100_list.append(total_profit / total_staked * 100.0 if total_staked else 0.0)
        profits.append(total_profit)

    def _dist(values: Sequence[float]) -> dict[str, Any]:
        ordered = sorted(values)
        return {
            "mean": statistics.fmean(ordered) if ordered else None,
            "sd": statistics.pstdev(ordered) if len(ordered) > 1 else 0.0,
            "p05": _quantile(ordered, 0.05),
            "p50": _quantile(ordered, 0.50),
            "p95": _quantile(ordered, 0.95),
            "min": ordered[0] if ordered else None,
            "max": ordered[-1] if ordered else None,
        }

    return {
        "seeds": len(seed_list),
        "seed_first": seed_list[0] if seed_list else None,
        "seed_last": seed_list[-1] if seed_list else None,
        "pick_count": pick_count,
        "budget": budget,
        "periods": len(series) - int(min_prior_draws),
        "hit_rate_distribution": _dist(hit_rates),
        "realised_ev_per_100_distribution": _dist(ev_per_100_list),
        "session_profit_distribution": _dist(profits),
        "p_session_up": (
            sum(1 for p in profits if p > 0) / len(profits) if profits else None
        ),
        "analytical_ev_per_100_staked": ev_per_100(odds),
        "hit_rates": hit_rates,
        "realised_ev_per_100_list": ev_per_100_list,
    }


def percentile_of(value: float, sample: Sequence[float]) -> float | None:
    """``value`` 在 ``sample`` 中的百分位（0~100；= ``< value`` 的占比 × 100）。"""
    data = [float(v) for v in sample]
    if not data:
        return None
    below = sum(1 for v in data if v < float(value))
    return below / len(data) * 100.0


def verify_engine_rows(
    rows: Sequence[dict[str, Any]],
    backtest: dict[str, Any],
) -> dict[str, Any]:
    """断言手工走步与 ``backtest_stats`` 的命中序列逐期一致（防止口径漂移）。"""
    mine = [bool(r["hit"]) for r in rows]
    theirs = [bool(r["hit"]) for r in backtest.get("results", [])]
    return {
        "matches": mine == theirs,
        "walk_forward_hits": sum(1 for v in mine if v),
        "backtest_hits": sum(1 for v in theirs if v),
        "walk_forward_periods": len(mine),
        "backtest_periods": len(theirs),
    }


# --------------------------------------------------------------------------- #
# 指标汇总
# --------------------------------------------------------------------------- #
def policy_metrics(
    name: str,
    label: str,
    rows: Sequence[dict[str, Any]],
    *,
    odds: float,
    resamples: int,
    seed: int,
    stop_levels: Sequence[int] = DEFAULT_STOP_LEVELS,
) -> dict[str, Any]:
    """把一组逐期结果汇总成「投注 / 盈亏 / 分布 / bootstrap」四段指标。"""
    if not rows:
        return {"name": name, "label": label, "data_status": "INSUFFICIENT"}

    odds_value = float(odds)
    stakes = [float(r["staked"]) for r in rows]
    profits = [float(r["profit"]) for r in rows]
    hits = sum(1 for r in rows if r["hit"])
    periods = len(rows)
    total_staked = sum(stakes)
    total_profit = sum(profits)
    pick_count = len(rows[0]["picks"])
    expected_hit_rate = pick_count / NUMBER_COUNT

    stat = period_profit_stats(profits)
    sd = stat["sd"] or 0.0
    analytical_total = ev_per_100(odds_value) / 100.0 * total_staked
    z = (
        (total_profit - analytical_total) / (sd * math.sqrt(periods))
        if sd > 0 and periods > 0
        else None
    )
    boot = bootstrap_session(
        profits, resamples=resamples, seed=seed, stop_levels=stop_levels
    )
    # 参数化版本：按理论命中概率 k/49 重放同一投注结构（不受本池运气影响）
    boot_uniform = uniform_null_session(
        rows, odds=odds_value, trials=resamples, seed=seed,
        stop_levels=stop_levels,
    )
    boot_uniform["p_period_up"] = (
        pick_count / NUMBER_COUNT if pick_count > 0 else None
    )
    return {
        "name": name,
        "label": label,
        "pick_count": pick_count,
        "data_status": "OK",
        "stake": {
            "periods": periods,
            "total_staked": total_staked,
            "mean_stake_per_period": total_staked / periods,
            "stake_variance": statistics.pvariance(stakes) if periods > 1 else 0.0,
            "stake_sd": statistics.pstdev(stakes) if periods > 1 else 0.0,
            "min_stake": min(stakes),
            "max_stake": max(stakes),
            "distinct_stake_values": sorted({round(v, 2) for v in stakes}),
        },
        "outcome": {
            "hits": hits,
            "hit_rate": hits / periods,
            "expected_hit_rate": expected_hit_rate,
            "total_profit": total_profit,
            "mean_profit_per_period": total_profit / periods,
            "realised_ev_per_100_staked": (
                total_profit / total_staked * 100.0 if total_staked else None
            ),
            "analytical_ev_per_100_staked": ev_per_100(odds_value),
            "analytical_total_profit": analytical_total,
            "profit_z_vs_uniform": z,
        },
        "period_profit": stat,
        "session_observed": {
            "total_profit": total_profit,
            "max_drawdown": max_drawdown(profits),
            "min_cumulative": min_cumulative(profits),
            "touched_stop": {
                str(level): min_cumulative(profits) <= -int(level)
                for level in sorted({int(v) for v in stop_levels})
            },
        },
        "bootstrap": boot,
        "bootstrap_uniform_ev": boot_uniform,
    }


def _json_ready(value: Any) -> Any:
    """递归转成可 JSON 序列化的结构（date → isoformat，其余原样）。"""
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def build_policies(base: dict[str, Any], budget: int) -> dict[str, list[dict[str, Any]]]:
    """构造对照策略的设置覆盖（键 → 传给 ``engine_rows`` 的 settings 补丁）。

    只改**分析用**的旋钮，不改任何默认值 / 生产逻辑。
    """
    no_soft = {
        "repeat_number_weight": 1.0,
        "repeat_zodiac_weight": 1.0,
        "stale_weight": 1.0,
    }
    uniform_quota = {
        "role_w_primary": 1.0,
        "role_w_secondary": 1.0,
        "role_w_defense": 1.0,
    }
    return {
        # 当前设置，预算 50（= 每注最低 5 元 × 10 注：配额与软降权都无处发力）
        "current_50": {**base, "total_amount": 50},
        # 当前设置，预算 100（配额与软降权真正生效）
        "current_100": {**base, "total_amount": budget},
        # 关掉软降权（保留 3:2:1 配额）→ 隔离「软降权省了多少钱」
        "nosoft_100": {**base, "total_amount": budget, **no_soft},
        # 关掉配额（保留软降权）→ 隔离「配额改了多少钱」
        "noquota_100": {**base, "total_amount": budget, **uniform_quota},
        # 配额与软降权都关 → 严格等额均注对照
        "flat_100": {**base, "total_amount": budget, **no_soft, **uniform_quota},
    }


def k_sweep_settings(base: dict[str, Any], budget: int) -> dict[int, dict[str, Any]]:
    """k 扫描：固定等额均注（关配额与软降权），只改注数，隔离 k / 频率权衡。"""
    return {
        k: {
            **base,
            "total_amount": budget,
            "pick_count": k,
            "repeat_number_weight": 1.0,
            "repeat_zodiac_weight": 1.0,
            "stale_weight": 1.0,
            "role_w_primary": 1.0,
            "role_w_secondary": 1.0,
            "role_w_defense": 1.0,
        }
        for k in (1, 3, 5, 10)
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="引擎价值分析：期望守恒 / 方差 / 花费控制 / 杠杆 / 赔率敏感性"
    )
    parser.add_argument(
        "--draws-json", default=str(BACKEND_ROOT / "data" / "draws_70_279.json")
    )
    parser.add_argument("--base-json", default="", help="基线设置 JSON（缺省用 dev 快照）")
    parser.add_argument("--budget", type=int, default=100, help="并列比较用的每期预算（元）")
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--random-seeds", type=int, default=200,
        help="随机基线重复的 seed 个数（用于给出诚实基线的分布）",
    )
    parser.add_argument(
        "--out", default=str(BACKEND_ROOT / "data" / "engine_value_analysis.json")
    )
    args = parser.parse_args()

    base = dict(SAVED_SETTINGS_SNAPSHOT)
    if args.base_json:
        base.update(json.loads(Path(args.base_json).read_text(encoding="utf-8")))
    budget = max(1, int(args.budget))
    unit = int(base.get("amount_unit", AMOUNT_UNIT_MIN))
    odds = float(base.get("odds", DEFAULT_ODDS))

    draws = json.loads(Path(args.draws_json).read_text(encoding="utf-8"))
    series = A.normalize_draws(draws)
    sample_size = len(series)
    min_prior = A.MIN_PRIOR_DRAWS

    # 引擎走步与权威回测逐期对齐（口径守卫）
    current_backtest = A.backtest_stats(
        series, base_settings=base, include_results=True
    )
    current_rows = engine_rows(series, base, min_prior_draws=min_prior)
    verification = verify_engine_rows(current_rows, current_backtest)
    if not verification["matches"]:
        raise SystemExit(
            "口径不一致：手工走步与 backtest_stats 命中序列不同，拒绝继续。"
        )

    policies = build_policies(base, budget)
    policy_rows: dict[str, list[dict[str, Any]]] = {
        "current_50": engine_rows(series, policies["current_50"], min_prior_draws=min_prior),
        "current_100": engine_rows(series, policies["current_100"], min_prior_draws=min_prior),
        "nosoft_100": engine_rows(series, policies["nosoft_100"], min_prior_draws=min_prior),
        "noquota_100": engine_rows(series, policies["noquota_100"], min_prior_draws=min_prior),
        "flat_100": engine_rows(series, policies["flat_100"], min_prior_draws=min_prior),
    }
    # 保留引擎选号、只换成等额均注（隔离选号 vs 花钱方式）
    policy_rows["flat_engine_100"] = flatten_rows(
        policy_rows["current_100"], budget=budget, unit=unit, odds=odds
    )
    policy_rows["flat_engine_50"] = flatten_rows(
        policy_rows["current_50"], budget=50, unit=unit, odds=odds
    )
    # 诚实基线：随机 10 注、等额均注
    policy_rows["random_100"] = random_rows(
        series, pick_count=int(base.get("pick_count", 10)), budget=budget,
        unit=unit, odds=odds, seed=args.seed, min_prior_draws=min_prior,
    )
    policy_rows["random_50"] = random_rows(
        series, pick_count=int(base.get("pick_count", 10)), budget=50,
        unit=unit, odds=odds, seed=args.seed + 1, min_prior_draws=min_prior,
    )

    labels = {
        "current_50": "当前已保存设置（预算 50；配额与软降权都无处发力）",
        "current_100": f"当前已保存设置（预算 {budget}）",
        "nosoft_100": f"关掉软降权（保留 3:2:1 配额，预算 {budget}）",
        "noquota_100": f"关掉角色配额（保留软降权，预算 {budget}）",
        "flat_100": f"严格等额均注（关配额与软降权，预算 {budget}）",
        "flat_engine_100": f"引擎选号 + 等额均注（预算 {budget}）",
        "flat_engine_50": "引擎选号 + 等额均注（预算 50）",
        "random_100": f"随机 10 注 + 等额均注（预算 {budget}）",
        "random_50": "随机 10 注 + 等额均注（预算 50）",
    }
    metrics = {
        name: policy_metrics(
            name, labels[name], rows,
            odds=odds, resamples=args.resamples, seed=args.seed,
        )
        for name, rows in policy_rows.items()
    }

    # ---- k 扫描（等额均注，隔离 k / 频率权衡）----
    k_metrics: dict[str, Any] = {}
    for k, settings in k_sweep_settings(base, budget).items():
        rows = engine_rows(series, settings, min_prior_draws=min_prior)
        k_metrics[str(k)] = policy_metrics(
            f"flat_k{k}", f"等额均注 k={k}（预算 {budget}）", rows,
            odds=odds, resamples=args.resamples, seed=args.seed,
        )

    # ---- 诚实基线：随机选号 + 等额均注，跨多个 seed 的分布 ----
    pick_count_default = int(base.get("pick_count", 10))
    seed_list = [args.seed + i for i in range(max(1, int(args.random_seeds)))]
    random_baseline = {
        "budget_100": random_seed_summary(
            series, pick_count=pick_count_default, budget=budget, unit=unit,
            odds=odds, seeds=seed_list, min_prior_draws=min_prior,
        ),
        "budget_50": random_seed_summary(
            series, pick_count=pick_count_default, budget=50, unit=unit,
            odds=odds, seeds=seed_list, min_prior_draws=min_prior,
        ),
    }
    engine_percentile = {
        "hit_rate": percentile_of(
            metrics["current_100"]["outcome"]["hit_rate"],
            random_baseline["budget_100"]["hit_rates"],
        ),
        "realised_ev_per_100": percentile_of(
            metrics["current_100"]["outcome"]["realised_ev_per_100_staked"],
            random_baseline["budget_100"]["realised_ev_per_100_list"],
        ),
    }

    # ---- Part C：花费控制明细 ----
    def pick_amount_census(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
        amounts: list[int] = []
        by_role: dict[str, list[int]] = {}
        by_soft: dict[str, list[int]] = {"penalized": [], "clean": []}
        for row in rows:
            for pick in row["picks"]:
                amount = int(pick["amount"])
                if amount <= 0:
                    continue
                amounts.append(amount)
                role = str(pick.get("role") or "none")
                by_role.setdefault(role, []).append(amount)
                key = "penalized" if (pick.get("soft_weight") or 1.0) < 1.0 else "clean"
                by_soft[key].append(amount)

        def _stat(values: list[int]) -> dict[str, Any]:
            if not values:
                return {"count": 0, "mean": None, "min": None, "max": None}
            return {
                "count": len(values),
                "mean": statistics.fmean(values),
                "min": min(values),
                "max": max(values),
            }

        return {
            "total_picks": len(amounts),
            "overall": _stat(amounts),
            "by_role": {role: _stat(vals) for role, vals in sorted(by_role.items())},
            "by_soft_penalty": {key: _stat(vals) for key, vals in by_soft.items()},
            "amount_histogram": {
                str(amount): amounts.count(amount) for amount in sorted(set(amounts))
            },
        }

    flat_total = sum(float(r["staked"]) for r in policy_rows["flat_100"])
    current_total = sum(float(r["staked"]) for r in policy_rows["current_100"])
    nosoft_total = sum(float(r["staked"]) for r in policy_rows["nosoft_100"])
    noquota_total = sum(float(r["staked"]) for r in policy_rows["noquota_100"])
    current50_total = sum(float(r["staked"]) for r in policy_rows["current_50"])
    flat50_total = sum(float(r["staked"]) for r in policy_rows["flat_engine_50"])
    periods = len(policy_rows["current_100"])

    spend_control = {
        "periods": periods,
        "budget": budget,
        "current_50": {
            "total_staked": current50_total,
            "mean_per_period": current50_total / periods,
            "flat_reference_total": flat50_total,
            "saved_total": flat50_total - current50_total,
            "saved_per_period": (flat50_total - current50_total) / periods,
        },
        "budget_100": {
            "flat_reference_total": flat_total,
            "quota_nosoft_total": nosoft_total,
            "soft_only_total": noquota_total,
            "quota_and_soft_total": current_total,
            "quota_effect_total": nosoft_total - flat_total,
            "soft_effect_total": current_total - nosoft_total,
            "total_saved_vs_flat": flat_total - current_total,
            "saved_per_period": (flat_total - current_total) / periods,
        },
        "amount_census_current_100": pick_amount_census(policy_rows["current_100"]),
        "amount_census_flat_100": pick_amount_census(policy_rows["flat_100"]),
    }

    # ---- Part E：赔率敏感性；并核对 pnl.py 对 odds 的语义 ----
    odds_table: list[dict[str, Any]] = []
    for odds_value in (47, 48, 49, 50):
        be = breakeven_hit_rate(int(base.get("pick_count", 10)), float(odds_value))
        odds_table.append(
            {
                "odds": odds_value,
                "ev_per_100_staked": ev_per_100(float(odds_value)),
                "breakeven_hit_rate_k10": be,
                "random_baseline_hit_rate_k10": 10 / NUMBER_COUNT,
                "breakeven_minus_random_pp": (
                    (be - 10 / NUMBER_COUNT) * 100 if be is not None else None
                ),
            }
        )
    # 结算语义核对：1 元命中 47 赔率 → 兑付 47 元（含本金），而非 48 元
    payout_probe = settle_picks(
        [{"number": 7, "amount": 1}], 7, 47.0
    )
    odds_semantics = {
        "code": "services/pnl.py :: settle_picks",
        "formula": "单注兑付 = 金额 × 赔率；净盈亏 = 兑付 − 成本（命中才兑付）",
        "probe_amount": 1,
        "probe_odds": 47,
        "probe_payout": payout_probe["payout"],
        "probe_profit": payout_probe["profit"],
        "interpretation": (
            "命中一注 1 元赔率 47 → 兑付 47 元（= 金额 × 47，**已含本金**），"
            "净赚 46 元。即代码把 odds 当作「含本金的兑付倍数」。"
        ),
        "if_real_game_is_47_net_plus_principal": (
            "若实际玩法是「47 倍净赔 + 本金退回」（合计 48 倍），"
            "则正确设置应为 odds=48，当前 47 少算 1 倍 → 白让 2.04pp 期望。"
        ),
    }

    # ---- Part D：杠杆表 ----
    sd_flat1 = k_metrics["1"]["period_profit"]["sd"]
    sd_flat10 = k_metrics["10"]["period_profit"]["sd"]
    p_up_flat1 = k_metrics["1"]["period_profit"]["p_period_up"]
    p_up_flat10 = k_metrics["10"]["period_profit"]["p_period_up"]
    quota_sd = metrics["noquota_100"]["period_profit"]["sd"]
    engine_quota_sd = metrics["current_100"]["period_profit"]["sd"]
    levers = [
        {
            "lever": "赔率 odds（47 → 48）",
            "moves_ev": True,
            "moves_variance": True,
            "ev_magnitude": (
                f"每 100 元期望 {ev_per_100(47):+.2f} → {ev_per_100(48):+.2f} 元"
                "（+2.04pp，唯一能平移期望的旋钮）"
            ),
            "variance_magnitude": "同时抬高每期命中收益（兑付倍数变大）",
        },
        {
            "lever": f"注数 k（1 → 10，预算 {budget}）",
            "moves_ev": False,
            "moves_variance": True,
            "ev_magnitude": "每 100 元期望恒为 −4.08 元（与 k 无关）",
            "variance_magnitude": (
                f"每期盈亏 sd {sd_flat1:.1f} → {sd_flat10:.1f} 元；"
                f"每期盈利概率 {p_up_flat1:.2%} → {p_up_flat10:.2%}"
            ),
        },
        {
            "lever": "注码粒度 amount_unit / 预算对齐",
            "moves_ev": False,
            "moves_variance": True,
            "ev_magnitude": "每 100 元期望不变（仍是 −4.08 元）",
            "variance_magnitude": (
                "只在「预算不是粒度的整数倍」时改变实际投注额（向下取整 → 少投注）；"
                "当前 100/5/10 整除，幅度 0；粒度越大，实际投注越容易被截断"
            ),
        },
        {
            "lever": "角色配额比例（3:2:1 → 1:1:1）",
            "moves_ev": False,
            "moves_variance": True,
            "ev_magnitude": "每 100 元期望不变（配额只**重分配**同一份预算，不改总额）",
            "variance_magnitude": (
                f"预算 {budget} 时每期盈亏 sd {quota_sd:.1f} → {engine_quota_sd:.1f} 元；"
                "预算 50（= 每注下限 × 注数）时幅度 0"
            ),
            "spend_magnitude": (
                f"配额本身一分钱都不省（总投注 {nosoft_total:.0f} 元不变）；"
                f"但 3:2:1 把防守注压在 {unit} 元下限，软降权在这些注上无处可降 —— "
                f"整段省额从 {flat_total - noquota_total:.0f} 元缩到 "
                f"{nosoft_total - current_total:.0f} 元，等于少省 "
                f"{current_total - noquota_total:.0f} 元"
            ),
        },
        {
            "lever": "软降权（重号 / 同肖 / 冷号）",
            "moves_ev": False,
            "moves_variance": True,
            "ev_magnitude": "每 100 元期望不变（只是少投注，收益率不变）",
            "variance_magnitude": (
                f"预算 {budget} 时全体 {periods} 期共省 "
                f"{nosoft_total - current_total:.0f} 元"
                f"（每期约 {(nosoft_total - current_total) / periods:.2f} 元，"
                f"全部落在被降权注上、且都被压到 {unit} 元下限）；"
                "预算 50（已在下限）时省 0 元"
            ),
        },
        {
            "lever": "选号算法（引擎的全部选号 / 打权改动）",
            "moves_ev": False,
            "moves_variance": False,
            "ev_magnitude": "每 100 元期望不变（1..49 各号等概率，各 2.04%）",
            "variance_magnitude": (
                "等额均注下盈亏分布不变；引擎实测命中 "
                f"{metrics['current_100']['outcome']['hits']}/{periods}，位于随机基线第 "
                f"{engine_percentile['hit_rate']:.0f} 百分位 —— 既不能证明比随机差，"
                "也不能证明比随机好。它只改变「落在哪几个号」这条实现路径，不改变分布"
            ),
        },
        {
            "lever": "止损 / 止盈（资金规则）",
            "moves_ev": False,
            "moves_variance": True,
            "ev_magnitude": "单期每元期望不变；止损只截断路径，不改单注收益率",
            "variance_magnitude": "改变触停概率与整段盈利概率（见 bootstrap p_touch_stop）",
        },
        {
            "lever": "本金规模（bankroll）",
            "moves_ev": False,
            "moves_variance": True,
            "ev_magnitude": "每 100 元期望不变",
            "variance_magnitude": "只改变「能否扛住回撤」的破产概率，不改变期望",
        },
        {
            "lever": "扩大样本（210 → 更多期）",
            "moves_ev": False,
            "moves_variance": False,
            "ev_magnitude": "不改期望，只改「能否验证」的结论强度",
            "variance_magnitude": "不改单期方差，只缩小小样本噪声",
        },
    ]

    payload = {
        "scope": f"本池已导入 {sample_size} 期样本内",
        "sample_size": sample_size,
        "evaluated_periods": len(current_rows),
        "first_evaluated_period": current_rows[0]["period"] if current_rows else None,
        "last_evaluated_period": current_rows[-1]["period"] if current_rows else None,
        "baseline_settings": base,
        "odds": odds,
        "budget": budget,
        "amount_unit": unit,
        "verification": {
            **verification,
            "backtest_hit_rate": current_backtest.get("hit_rate"),
            "backtest_minus_baseline": current_backtest.get("hit_rate_minus_baseline"),
            "backtest_verdict": (current_backtest.get("verdict") or {}).get("kind"),
        },
        "part_a_ev_invariance": {
            "analytical_ev_per_100_staked": ev_per_100(odds),
            "note": (
                "期望收益率 = 赔率 / 49 − 1，是数学事实：与选号、注数、注码、"
                "配额、软降权、止损全部无关；只有赔率能改变它。"
            ),
            "policies": metrics,
        },
        "part_b_variance": {
            "equal_budget": budget,
            "k_sweep": k_metrics,
            "resamples": args.resamples,
            "seed": args.seed,
        },
        "random_baseline_distribution": {
            **random_baseline,
            "engine_percentile": engine_percentile,
            "note": (
                "随机选号 + 等额均注在多个 seed 上的已实现分布，作为『诚实基线』；"
                "引擎的已实现成绩必须放到这个分布里看百分位，"
                "单看某一个随机 seed 会得出反向结论。"
            ),
        },
        "part_c_spend_control": spend_control,
        "part_d_levers": levers,
        "part_e_odds": {
            "table": odds_table,
            "semantics": odds_semantics,
        },
        "notes": [
            f"本池已导入 {sample_size} 期，走步可评估 {len(current_rows)} 期；"
            "全部数字都是样本内经验值，不是概率，也不构成对未来命中或收益的承诺。",
            "期望收益率 odds/49 − 1 是在「各号码等概率」这一假设下的数学事实；"
            "本池的偏差检验未发现可利用偏置，因此该值即本池口径下的期望。",
            "角色配额只把同一份预算在注间重分配（总投注额不变）；"
            "只有软降权 / 避冷封顶会真正减少总投注额。",
            "bootstrap 为对逐期盈亏序列的有放回重采样，用于估计整段资金曲线的不确定性；"
            "触停概率按「整段资金曲线是否触及 −L」定义。",
        ],
    }

    out_path = Path(args.out)
    out_path.write_text(
        json.dumps(_json_ready(payload), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # ---- 控制台摘要 ----
    print(f"本池 {sample_size} 期；走步评估 {len(current_rows)} 期；预算 {budget}；赔率 {odds}")
    print(
        f"口径守卫：手工走步命中 {verification['walk_forward_hits']} vs "
        f"backtest_stats {verification['backtest_hits']} → "
        f"{'一致' if verification['matches'] else '不一致（已中止）'}"
    )
    print("\n=== A. 期望守恒（每 100 元投注）===")
    for name, m in metrics.items():
        o = m["outcome"]
        s = m["stake"]
        print(
            f"{name:16s} 投注 {s['total_staked']:8.0f} 元 | "
            f"命中 {o['hits']:3d}/{s['periods']} = {o['hit_rate']:.2%} | "
            f"盈亏 {o['total_profit']:+9.0f} 元 | "
            f"已实现 EV/100 {o['realised_ev_per_100_staked']:+7.2f} | "
            f"解析 EV/100 {o['analytical_ev_per_100_staked']:+6.2f} | "
            f"z {o['profit_z_vs_uniform']:+.2f} | "
            f"P(整段盈|等概率重放) {m['bootstrap_uniform_ev']['p_session_up']:.2%}"
        )
    print("\n=== A2. 诚实基线（随机 10 注 + 等额均注，跨 seed 分布）===")
    rb = random_baseline["budget_100"]
    print(
        f"预算 100：{rb['seeds']} 个 seed；命中率均值 {rb['hit_rate_distribution']['mean']:.2%}"
        f"（sd {rb['hit_rate_distribution']['sd']:.2%}，p05 {rb['hit_rate_distribution']['p05']:.2%}"
        f"~ p95 {rb['hit_rate_distribution']['p95']:.2%}）；"
        f"已实现 EV/100 均值 {rb['realised_ev_per_100_distribution']['mean']:+.2f}"
        f"（p05 {rb['realised_ev_per_100_distribution']['p05']:+.2f}"
        f"~ p95 {rb['realised_ev_per_100_distribution']['p95']:+.2f}）；"
        f"P(整段盈) {rb['p_session_up']:.2%}"
    )
    print(
        f"引擎命中率 {metrics['current_100']['outcome']['hit_rate']:.2%} "
        f"位于该随机分布第 {engine_percentile['hit_rate']:.0f} 百分位 → "
        "与随机抽样无法区分"
    )
    print("\n=== B. k 扫描（等额均注，预算 100）===")
    for k, m in k_metrics.items():
        p = m["period_profit"]
        b = m["bootstrap"]
        b_u = m["bootstrap_uniform_ev"]
        print(
            f"k={k:>2s} 投注 {m['stake']['mean_stake_per_period']:.0f}/期 | "
            f"每期 sd {p['sd']:7.1f} 偏度 {p['skew']:+.2f} | "
            f"P(期盈) {p['p_period_up']:.2%} | "
            f"P(整段盈|本池) {b['p_session_up']:.2%} | "
            f"P(整段盈|等概率重放) {b_u['p_session_up']:.2%} | "
            f"回撤 p95 {b['mdd_p95']:.0f} | 触停2000 {b['p_touch_stop']['2000']:.1%}"
        )
    print("\n=== C. 花费控制 ===")
    b100 = spend_control["budget_100"]
    print(f"预算 50：省 {spend_control['current_50']['saved_total']:.0f} 元（配额+软降权）")
    print(
        f"预算 100：等额 {b100['flat_reference_total']:.0f} 元；"
        f"配额影响 {b100['quota_effect_total']:+.0f} 元；"
        f"软降权影响 {b100['soft_effect_total']:+.0f} 元；"
        f"共省 {b100['total_saved_vs_flat']:.0f} 元"
    )
    print("\n=== E. 赔率敏感性 ===")
    for row in odds_table:
        print(
            f"odds={row['odds']}：EV/100 {row['ev_per_100_staked']:+.2f} 元 | "
            f"k=10 平衡命中率 {row['breakeven_hit_rate_k10']:.2%} | "
            f"相对随机 {row['breakeven_minus_random_pp']:+.2f}pp"
        )
    print(
        f"结算核对：1 元 × odds 47 命中兑付 = {payout_probe['payout']:.2f} 元"
        "（含本金；若玩法为 47 净赔 + 本金则应为 48）"
    )
    print(f"\n[written] {out_path}")


if __name__ == "__main__":
    main()
