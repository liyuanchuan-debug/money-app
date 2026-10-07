r"""开奖历史的信息审计 —— 纯统计函数，不访问数据库（供 ``scripts/dist_audit.py`` 与测试复用）。

回答使用者的问题：**手里这 210 期历史，对下一期到底含不含可利用的信息？**

本模块只提供**工具**：卡方（含不全 Gamma 实现）、互信息、自相关、游程、熵与置信区间、
周期 / 谱扫描、热冷 / 遗漏 / 生肖 / 点阵的样本外命中统计、以及生产引擎自身特征的
逐项消融。所有置换零分布与多重比较校正的**编排**在脚本层完成，因为那需要真实的
开奖文件与多进程。

口径铁律（违反即为缺陷）：

- 所有结论只针对「本池已导入的 N 期数据」这个样本；**禁止写「全市场」**，
  本池没有全市场数据，任何升格都是编造。
- 样本量为 0 或某项分析样本过小时，显式返回 ``data_status = INSUFFICIENT``
  （英文码）+ ``data_status_label = 数据不足``，绝不用 0 值冒充结论。
- 判定一律英文枚举：``signal`` / ``no_signal`` / ``insufficient_data``；
  汉字只出现在 ``*_label`` 与提示文案里。
- 小样本功效工具**复用** ``services.analytics``（``minimum_detectable_delta`` /
  ``holm_adjusted_p`` / ``binomial_tail_p``），不在本模块重写。
"""

from __future__ import annotations

import math
import random
from collections import Counter
from statistics import NormalDist
from typing import Any, Iterable, Mapping, Sequence

from services.analytics import (
    DATA_STATUS_INSUFFICIENT,
    DATA_STATUS_LABELS,
    DATA_STATUS_OK,
    binomial_tail_p,
    holm_adjusted_p,
    minimum_detectable_delta,
)
from services.dist_engine import (
    COMPONENT_FREQUENCY,
    COMPONENT_GAP,
    COMPONENT_MARKOV,
    COMPONENT_PERIODIC,
    COMPONENT_REPEAT,
    COMPONENT_STRUCTURE,
    COMPONENT_WAVE,
    COMPONENT_ZODIAC,
    component_topk,
)
from services.lottery import DEFAULT_SETTINGS, clamp_settings, derive_big_min
from services.max_fit import (
    K_DEFAULT,
    NUMBER_MAX,
    NUMBER_MIN,
    NUM_STATES,
    ODDS_DEFAULT,
)

# --------------------------------------------------------------------------- #
# 常量与英文枚举
# --------------------------------------------------------------------------- #
UNIFORM_RATE = K_DEFAULT / NUM_STATES  # 10/49 = 20.4082%

VERDICT_SIGNAL = "signal"
VERDICT_NO_SIGNAL = "no_signal"
VERDICT_INSUFFICIENT = "insufficient_data"
VERDICT_LABELS: dict[str, str] = {
    VERDICT_SIGNAL: "校正后仍显著（需样本外复核）",
    VERDICT_NO_SIGNAL: "无信号（落在抽样噪声内）",
    VERDICT_INSUFFICIENT: "数据不足",
}

# 审计假设族：先声明条数，再统一做 Holm 校正。
HANDLE_UNIFORMITY = "uniformity_chi2"
HANDLE_INDEPENDENCE = "independence_chi2"
HANDLE_MUTUAL_INFORMATION = "mutual_information"
HANDLE_AUTOCORR_SPECIAL = "autocorr_special"
HANDLE_AUTOCORR_PARITY = "autocorr_parity"
HANDLE_AUTOCORR_BIGSMALL = "autocorr_bigsmall"
HANDLE_AUTOCORR_TAIL = "autocorr_tail_digit"
HANDLE_AUTOCORR_ZODIAC = "autocorr_zodiac"
HANDLE_RUNS_PARITY = "runs_parity"
HANDLE_RUNS_BIGSMALL = "runs_bigsmall"
HANDLE_HOT_COLD = "hot_cold_persistence"
HANDLE_GAP = "gap_overdue"
HANDLE_ZODIAC_HOT = "zodiac_hot"
HANDLE_WAVE_LATTICE = "wave_lattice"
HANDLE_MARKOV = "markov_order1"
HANDLE_STRUCTURE = "structure_constraints"
HANDLE_REPEAT = "repeat_effects"
HANDLE_PERIODIC = "periodic_scan"
HANDLE_ENTROPY = "entropy_gap"
HANDLE_ENGINE_REPEAT_NUMBER = "engine_repeat_number"
HANDLE_ENGINE_REPEAT_ZODIAC = "engine_repeat_zodiac"
HANDLE_ENGINE_COLD = "engine_cold"
HANDLE_ENGINE_LATTICE = "engine_lattice"

PERMUTATION_HANDLES: tuple[str, ...] = (
    HANDLE_INDEPENDENCE,
    HANDLE_MUTUAL_INFORMATION,
    HANDLE_AUTOCORR_SPECIAL,
    HANDLE_AUTOCORR_PARITY,
    HANDLE_AUTOCORR_BIGSMALL,
    HANDLE_AUTOCORR_TAIL,
    HANDLE_AUTOCORR_ZODIAC,
    HANDLE_RUNS_PARITY,
    HANDLE_RUNS_BIGSMALL,
    HANDLE_HOT_COLD,
    HANDLE_GAP,
    HANDLE_ZODIAC_HOT,
    HANDLE_WAVE_LATTICE,
    HANDLE_MARKOV,
    HANDLE_STRUCTURE,
    HANDLE_REPEAT,
    HANDLE_PERIODIC,
    HANDLE_ENGINE_REPEAT_NUMBER,
    HANDLE_ENGINE_REPEAT_ZODIAC,
    HANDLE_ENGINE_COLD,
    HANDLE_ENGINE_LATTICE,
)
PARAMETRIC_HANDLES: tuple[str, ...] = (HANDLE_UNIFORMITY, HANDLE_ENTROPY)
ALL_HANDLES: tuple[str, ...] = PARAMETRIC_HANDLES + PERMUTATION_HANDLES

HANDLE_LABELS: dict[str, str] = {
    HANDLE_UNIFORMITY: "49 号频次是否均匀（卡方拟合优度）",
    HANDLE_INDEPENDENCE: "相邻两期是否独立（49×49 列联表卡方）",
    HANDLE_MUTUAL_INFORMATION: "相邻两期互信息（比特）—— 本期能从上一期学到的信息量",
    HANDLE_AUTOCORR_SPECIAL: "特码自相关（滞后 1..20 的最大 |r|）",
    HANDLE_AUTOCORR_PARITY: "奇偶序列自相关（最大 |r|）",
    HANDLE_AUTOCORR_BIGSMALL: "大小序列自相关（最大 |r|）",
    HANDLE_AUTOCORR_TAIL: "尾数序列自相关（最大 |r|）",
    HANDLE_AUTOCORR_ZODIAC: "生肖组序列自相关（最大 |r|）",
    HANDLE_RUNS_PARITY: "奇偶游程检验（Wald–Wolfowitz，|z|）",
    HANDLE_RUNS_BIGSMALL: "大小游程检验（Wald–Wolfowitz，|z|）",
    HANDLE_HOT_COLD: "热冷持续性（前半频次 top-10 打后半的命中数）",
    HANDLE_GAP: "遗漏 / 到期号预测力（最久未出 top-10 的命中数）",
    HANDLE_ZODIAC_HOT: "生肖热度 lift（近期最热生肖号 top-10 命中数）",
    HANDLE_WAVE_LATTICE: "波动 / 点阵 lift（点阵权重 top-10 命中数）",
    HANDLE_MARKOV: "一阶马尔可夫（上期号码的转移 top-10 命中数）",
    HANDLE_STRUCTURE: "结构约束（和值 / 跨度 / 大小 / 奇偶 / 尾数 top-10 命中数）",
    HANDLE_REPEAT: "重号 / 重肖效应（REPEAT 组件 top-10 命中数）",
    HANDLE_PERIODIC: "周期 / 谱扫描（周期 2..P 的最大谱功率）",
    HANDLE_ENTROPY: "经验熵与 ln 49 的差（越大越集中）",
    HANDLE_ENGINE_REPEAT_NUMBER: "生产引擎特征消融：重号（保留 vs 排除）命中率差",
    HANDLE_ENGINE_REPEAT_ZODIAC: "生产引擎特征消融：重肖（避开 vs 不避）命中率差",
    HANDLE_ENGINE_COLD: "生产引擎特征消融：避冷加权（开 vs 关）命中率差",
    HANDLE_ENGINE_LATTICE: "生产引擎特征消融：点阵（开 vs 关）命中率差",
}

ENGINE_FEATURE_HANDLES: tuple[str, ...] = (
    HANDLE_ENGINE_REPEAT_NUMBER,
    HANDLE_ENGINE_REPEAT_ZODIAC,
    HANDLE_ENGINE_COLD,
    HANDLE_ENGINE_LATTICE,
)

# 谱扫描默认最大周期
DEFAULT_MAX_PERIOD = 30
DEFAULT_MAX_LAG = 20
ENTROPY_SIMULATIONS = 4000
# 均匀性（49 格卡方）的蒙特卡洛零分布次数：本池每格期望仅 4.29 次，
# 渐近卡方近似处在 Cochran 严格规则边缘，用蒙特卡洛 p 兜底。
UNIFORMITY_SIMULATIONS = 4000
# 样本量少于号码种类数时不谈均匀性 / 频次结论
MIN_UNIFORMITY_SAMPLE = NUM_STATES


# --------------------------------------------------------------------------- #
# 卡方：不全 Gamma（Numerical Recipes 的级数 + 连分数），纯标准库
# --------------------------------------------------------------------------- #
def regularized_gamma_q(a: float, x: float) -> float:
    """上不完全 Gamma 的正则化值 ``Q(a, x) = Γ(a, x) / Γ(a)``（纯标准库实现）。"""
    a = float(a)
    x = float(x)
    if a <= 0.0:
        raise ValueError("a 必须为正")
    if x < 0.0:
        raise ValueError("x 不能为负")
    if x == 0.0:
        return 1.0
    gln = math.lgamma(a)
    if x < a + 1.0:
        # 级数展开求 P(a, x)，再取 Q = 1 - P
        ap = a
        term = 1.0 / a
        total = term
        for _ in range(10000):
            ap += 1.0
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-16:
                break
        p_value = total * math.exp(-x + a * math.log(x) - gln)
        return max(0.0, min(1.0, 1.0 - p_value))
    # 连分数展开求 Q(a, x)
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for index in range(1, 10000):
        an = -index * (index - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-16:
            break
    q_value = math.exp(-x + a * math.log(x) - gln) * h
    return max(0.0, min(1.0, q_value))


def chi2_sf(statistic: float, degrees_of_freedom: int) -> float | None:
    """卡方分布上尾概率 ``P(χ^2_df >= statistic)``；参数非法时返回 ``None``。"""
    df = int(degrees_of_freedom)
    if df <= 0:
        return None
    value = float(statistic)
    if value <= 0.0:
        return 1.0
    return max(0.0, min(1.0, regularized_gamma_q(df / 2.0, value / 2.0)))


def _normal_two_sided_p(z: float) -> float:
    normal = NormalDist()
    return max(0.0, min(1.0, 2.0 * (1.0 - normal.cdf(abs(float(z))))))


# --------------------------------------------------------------------------- #
# 频次 / 均匀性
# --------------------------------------------------------------------------- #
def number_counts(numbers: Iterable[int]) -> list[int]:
    """1..49 的出现次数（下标 0 对应号码 1）。"""
    counts = Counter()
    for value in numbers:
        number = int(value)
        if NUMBER_MIN <= number <= NUMBER_MAX:
            counts[number] += 1
    return [counts.get(number, 0) for number in range(NUMBER_MIN, NUMBER_MAX + 1)]


def uniformity_null_band(
    sample_size: int,
    *,
    simulations: int = UNIFORMITY_SIMULATIONS,
    seed: int = 20261007,
) -> dict[str, Any]:
    """均匀零假设下的 49 格卡方统计量分布（确定性 seed，蒙特卡洛）。

    置换零分布对均匀性**无效**（打乱顺序不改变出现次数），所以这里按「每期独立均匀
    落在 1..49」重抽 ``sample_size`` 期，得到卡方统计量的精确（蒙特卡洛）零分布 ——
    在每格期望只有 4.29 次的稀疏区间，它比 ``chi2_sf`` 的渐近近似更可靠。
    """
    if sample_size <= 0:
        return {
            "simulations": 0,
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
            "statistics": [],
        }
    rng = random.Random(int(seed))
    expected = sample_size / NUM_STATES
    statistics: list[float] = []
    for _ in range(int(simulations)):
        counts = [0] * NUM_STATES
        for _ in range(int(sample_size)):
            counts[rng.randrange(NUM_STATES)] += 1
        statistics.append(
            sum((count - expected) ** 2 / expected for count in counts)
        )
    ordered = sorted(statistics)
    mean = sum(ordered) / len(ordered)
    sd = math.sqrt(sum((value - mean) ** 2 for value in ordered) / len(ordered))
    return {
        "simulations": int(simulations),
        "seed": int(seed),
        "mean": mean,
        "sd": sd,
        "p50": ordered[len(ordered) // 2],
        "p95": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))],
        "max": ordered[-1],
        "statistics": statistics,
        "data_status": DATA_STATUS_OK,
        "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
    }


def uniformity_test(
    numbers: Sequence[int],
    *,
    simulations: int = UNIFORMITY_SIMULATIONS,
    seed: int = 20261007,
) -> dict[str, Any]:
    """卡方拟合优度：49 号频次是否偏离均匀（df = 48）。

    这个检验在置换零分布下**完全不变**（打乱顺序不改变出现次数），因此它的正确零假设
    是「多项均匀分布」：``chi2_sf`` 给渐近 p，``uniformity_null_band`` 给
    蒙特卡洛 p（本池每格期望只有 4.29 次，渐近近似处于 Cochran 严格规则的边缘，
    以蒙特卡洛 p 为准 —— 它在这一稀疏区间仍然精确）。

    样本量少于号码种类数（49）时**不谈均匀性**：``data_status = INSUFFICIENT``。
    """
    values = [int(value) for value in numbers]
    sample_size = len(values)
    degrees_of_freedom = NUM_STATES - 1
    if sample_size < NUM_STATES:
        return {
            "handle": HANDLE_UNIFORMITY,
            "statistic": None,
            "degrees_of_freedom": degrees_of_freedom,
            "p_value": None,
            "p_asymptotic": None,
            "sample_size": sample_size,
            "minimum_sample_size": NUM_STATES,
            "expected_count": (sample_size / NUM_STATES) if sample_size else None,
            "min_count": None,
            "max_count": None,
            "null": None,
            "method": "chi_square_goodness_of_fit_multinomial",
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    counts = number_counts(values)
    expected = sample_size / NUM_STATES
    statistic = sum((count - expected) ** 2 / expected for count in counts)
    null = uniformity_null_band(sample_size, simulations=simulations, seed=seed)
    null_values = [float(value) for value in null.get("statistics", [])]
    exceed = sum(1 for value in null_values if value >= statistic)
    monte_carlo_p = (1 + exceed) / (1 + len(null_values)) if null_values else None
    return {
        "handle": HANDLE_UNIFORMITY,
        "statistic": statistic,
        "degrees_of_freedom": degrees_of_freedom,
        "p_value": monte_carlo_p,
        "p_asymptotic": chi2_sf(statistic, degrees_of_freedom),
        "sample_size": sample_size,
        "minimum_sample_size": NUM_STATES,
        "expected_count": expected,
        "min_count": min(counts),
        "max_count": max(counts),
        "observed_counts": counts,
        "null": {key: value for key, value in null.items() if key != "statistics"},
        "method": "chi_square_goodness_of_fit_multinomial",
        "approximation_warning": (
            f"每格期望次数仅 {expected:.2f} < 5（Cochran 严格规则）→ 以蒙特卡洛 p 为准"
            if expected < 5.0
            else None
        ),
        "data_status": DATA_STATUS_OK,
        "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
    }


# --------------------------------------------------------------------------- #
# 相邻两期：列联表 / 互信息
# --------------------------------------------------------------------------- #
def lag_pairs(numbers: Sequence[int], lag: int = 1) -> list[tuple[int, int]]:
    """``(draw_t, draw_{t+lag})`` 配对（严格按原顺序，不做任何重排）。"""
    values = [int(value) for value in numbers]
    step = max(1, int(lag))
    return [(values[i], values[i + step]) for i in range(len(values) - step)]


def contingency_matrix(
    pairs: Sequence[tuple[int, int]], states: int = NUM_STATES
) -> list[list[int]]:
    """把配对计成 ``states × states`` 列联表。"""
    matrix = [[0] * states for _ in range(states)]
    for first, second in pairs:
        i = int(first) - NUMBER_MIN
        j = int(second) - NUMBER_MIN
        if 0 <= i < states and 0 <= j < states:
            matrix[i][j] += 1
    return matrix


def mutual_information_bits(pairs: Sequence[tuple[int, int]]) -> float:
    """相邻两期的经验互信息（比特）：``Σ p(i,j)·log2( p(i,j)/(p(i)p(j)) )``。

    **这是本审计最重要的一个数**。它衡量「知道上一期，能对本期多知道多少」。
    注意：有限样本下**即使完全独立**，经验互信息也恒为正（每个格子都要摊一份噪声），
    所以观测值本身没有意义，必须与置换零分布比。
    """
    matrix = contingency_matrix(pairs)
    total = sum(sum(row) for row in matrix)
    if total <= 1:
        return 0.0
    row_totals = [sum(row) for row in matrix]
    col_totals = [sum(matrix[i][j] for i in range(len(matrix))) for j in range(len(matrix[0]))]
    information = 0.0
    for i, row in enumerate(matrix):
        if row_totals[i] == 0:
            continue
        for j, cell in enumerate(row):
            if cell == 0 or col_totals[j] == 0:
                continue
            pij = cell / total
            pi = row_totals[i] / total
            pj = col_totals[j] / total
            information += pij * math.log2(pij / (pi * pj))
    return information


def chi_square_independence(pairs: Sequence[tuple[int, int]]) -> dict[str, Any]:
    """49×49 列联表的卡方独立性统计量（大样本近似不成立，仅作统计量用）。"""
    matrix = contingency_matrix(pairs)
    total = sum(sum(row) for row in matrix)
    if total <= 1:
        return {"statistic": None, "degrees_of_freedom": None, "expected_cell": None}
    row_totals = [sum(row) for row in matrix]
    col_totals = [sum(matrix[i][j] for i in range(len(matrix))) for j in range(len(matrix[0]))]
    statistic = 0.0
    for i, row in enumerate(matrix):
        for j, cell in enumerate(row):
            expected = row_totals[i] * col_totals[j] / total
            if expected > 0:
                statistic += (cell - expected) ** 2 / expected
    return {
        "statistic": statistic,
        "degrees_of_freedom": (NUM_STATES - 1) ** 2,
        "expected_cell": total / (NUM_STATES * NUM_STATES),
        "pairs": total,
    }


def independence_test(numbers: Sequence[int], lag: int = 1) -> dict[str, Any]:
    """相邻两期独立性的完整结果（卡方统计量 + 互信息比特）。"""
    pairs = lag_pairs(numbers, lag)
    if len(pairs) <= 1:
        return {
            "handle": HANDLE_INDEPENDENCE,
            "statistic": None,
            "mutual_information_bits": None,
            "pairs": len(pairs),
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    chi = chi_square_independence(pairs)
    return {
        "handle": HANDLE_INDEPENDENCE,
        "statistic": chi["statistic"],
        "degrees_of_freedom": chi["degrees_of_freedom"],
        "expected_cell": chi["expected_cell"],
        "mutual_information_bits": mutual_information_bits(pairs),
        "pairs": chi["pairs"],
        "data_status": DATA_STATUS_OK,
        "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
    }


# --------------------------------------------------------------------------- #
# 自相关 / 游程
# --------------------------------------------------------------------------- #
def autocorrelation(values: Sequence[float], lag: int) -> float | None:
    """Pearson 自相关（滞后 ``lag``）；方差为 0 或样本不足时返回 ``None``。"""
    series = [float(value) for value in values]
    step = int(lag)
    if step <= 0 or len(series) <= step + 1:
        return None
    left = series[:-step]
    right = series[step:]
    mean_left = sum(left) / len(left)
    mean_right = sum(right) / len(right)
    numerator = sum(
        (left[index] - mean_left) * (right[index] - mean_right)
        for index in range(len(left))
    )
    denominator = math.sqrt(
        sum((left[index] - mean_left) ** 2 for index in range(len(left)))
        * sum((right[index] - mean_right) ** 2 for index in range(len(right)))
    )
    if denominator <= 0.0:
        return None
    return numerator / denominator


def derived_series(
    numbers: Sequence[int], *, big_min: int | None = None
) -> dict[str, list[float]]:
    """派生序列：奇偶 / 大小 / 尾数 / 生肖组（全部由特码本身推导）。"""
    threshold = int(big_min if big_min is not None else derive_big_min(DEFAULT_SETTINGS["normal_max"]))
    values = [int(value) for value in numbers]
    return {
        "parity": [value % 2 for value in values],
        "bigsmall": [1 if value >= threshold else 0 for value in values],
        "tail_digit": [value % 10 for value in values],
        "zodiac_group": [(value - 1) % 12 for value in values],
    }


def autocorrelation_scan(
    numbers: Sequence[int],
    *,
    max_lag: int = DEFAULT_MAX_LAG,
    big_min: int | None = None,
) -> dict[str, Any]:
    """对特码与四条派生序列做滞后 1..``max_lag`` 的自相关扫描，取最大 |r|。"""
    values = [int(value) for value in numbers]
    if len(values) <= max_lag + 1:
        return {
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
            "series": {},
            "max_abs_r": None,
        }
    series_map: dict[str, list[float]] = {"special": [float(value) for value in values]}
    series_map.update(derived_series(values, big_min=big_min))
    output: dict[str, Any] = {}
    overall = 0.0
    for name, series in series_map.items():
        rows = []
        best = 0.0
        best_lag = None
        for lag in range(1, int(max_lag) + 1):
            value = autocorrelation(series, lag)
            if value is None:
                continue
            rows.append({"lag": lag, "r": value})
            if abs(value) > abs(best):
                best = value
                best_lag = lag
        output[name] = {"max_abs_r": abs(best) if best_lag else 0.0, "best_lag": best_lag, "lags": rows}
        overall = max(overall, abs(best) if best_lag else 0.0)
    return {
        "max_lag": int(max_lag),
        "series": output,
        "max_abs_r": overall,
        "data_status": DATA_STATUS_OK,
        "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
    }


def runs_test(binary: Sequence[Any]) -> dict[str, Any]:
    """Wald–Wolfowitz 游程检验（正态近似），返回游程数、z 与双侧 p。"""
    values = [bool(value) for value in binary]
    total = len(values)
    ones = sum(1 for value in values if value)
    zeros = total - ones
    if total < 3 or ones == 0 or zeros == 0:
        return {
            "runs": None,
            "z": None,
            "p_value": None,
            "sample_size": total,
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    runs = 1
    for index in range(1, total):
        if values[index] != values[index - 1]:
            runs += 1
    mean = 2.0 * ones * zeros / total + 1.0
    variance = (
        2.0 * ones * zeros * (2.0 * ones * zeros - total) / (total * total * (total - 1.0))
    )
    if variance <= 0.0:
        return {
            "runs": runs,
            "z": None,
            "p_value": None,
            "sample_size": total,
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    z = (runs - mean) / math.sqrt(variance)
    return {
        "runs": runs,
        "expected_runs": mean,
        "z": z,
        "abs_z": abs(z),
        "p_value": _normal_two_sided_p(z),
        "sample_size": total,
        "data_status": DATA_STATUS_OK,
        "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
    }


# --------------------------------------------------------------------------- #
# 熵
# --------------------------------------------------------------------------- #
def entropy_nats(counts: Sequence[int]) -> dict[str, Any]:
    """经验熵（nats）：plug-in 估计 + Miller–Madow 修正 + 渐近 95% 区间。"""
    total = sum(int(value) for value in counts)
    if total <= 0:
        return {
            "plugin_nats": None,
            "miller_madow_nats": None,
            "ln_states": math.log(NUM_STATES),
            "observed_categories": 0,
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    positive = [int(value) for value in counts if int(value) > 0]
    plugin = -sum((value / total) * math.log(value / total) for value in positive)
    observed = len(positive)
    miller_madow = plugin + (observed - 1) / (2.0 * total)
    # 渐近方差：Var(H) ≈ (1/n)( Σ p (ln p)^2 - H^2 )
    second = sum((value / total) * math.log(value / total) ** 2 for value in positive)
    variance = max(0.0, (second - plugin * plugin) / total)
    sd = math.sqrt(variance)
    return {
        "plugin_nats": plugin,
        "miller_madow_nats": miller_madow,
        "ln_states": math.log(NUM_STATES),
        "observed_categories": observed,
        "asymptotic_sd": sd,
        "ci95_low": plugin - 1.959963984540054 * sd,
        "ci95_high": plugin + 1.959963984540054 * sd,
        "data_status": DATA_STATUS_OK,
        "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
    }


def entropy_null_band(
    sample_size: int,
    *,
    simulations: int = ENTROPY_SIMULATIONS,
    seed: int = 20261007,
) -> dict[str, Any]:
    """均匀零假设下的经验熵差 ``ln 49 - H`` 分布（确定性 seed）。

    置换零分布对熵**无效**（打乱顺序不改变出现次数），因此这里用「按均匀分布独立抽取
    N 期」的参数化零分布 —— 这才是「这 210 期看起来太集中了吗」的正确对照。
    """
    if sample_size <= 0:
        return {
            "simulations": 0,
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
            "gaps": [],
        }
    rng = random.Random(int(seed))
    ln_states = math.log(NUM_STATES)
    gaps: list[float] = []
    for _ in range(int(simulations)):
        counts = [0] * NUM_STATES
        for _ in range(int(sample_size)):
            counts[rng.randrange(NUM_STATES)] += 1
        positive = [value for value in counts if value > 0]
        plugin = -sum(
            (value / sample_size) * math.log(value / sample_size) for value in positive
        )
        gaps.append(ln_states - plugin)
    ordered = sorted(gaps)
    mean = sum(ordered) / len(ordered)
    sd = math.sqrt(sum((value - mean) ** 2 for value in ordered) / len(ordered))
    return {
        "simulations": int(simulations),
        "seed": int(seed),
        "mean": mean,
        "sd": sd,
        "p025": ordered[int(0.025 * len(ordered))],
        "p975": ordered[min(len(ordered) - 1, int(0.975 * len(ordered)))],
        "max": ordered[-1],
        "gaps": gaps,
        "data_status": DATA_STATUS_OK,
        "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
    }


def entropy_test(
    numbers: Sequence[int],
    *,
    simulations: int = ENTROPY_SIMULATIONS,
    seed: int = 20261007,
) -> dict[str, Any]:
    """经验熵审计：观测的 ``ln 49 - H`` 与参数化零分布比较。"""
    values = [int(value) for value in numbers]
    counts = number_counts(values)
    entropy = entropy_nats(counts)
    if entropy["plugin_nats"] is None:
        return {
            "handle": HANDLE_ENTROPY,
            "statistic": None,
            "p_value": None,
            "entropy": entropy,
            "null": None,
            "sample_size": len(values),
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    statistic = entropy["ln_states"] - entropy["plugin_nats"]
    null = entropy_null_band(len(values), simulations=simulations, seed=seed)
    exceed = sum(1 for value in null["gaps"] if value >= statistic)
    p_value = (1 + exceed) / (1 + len(null["gaps"])) if null["gaps"] else None
    return {
        "handle": HANDLE_ENTROPY,
        "statistic": statistic,
        "p_value": p_value,
        "entropy": entropy,
        "null": {key: value for key, value in null.items() if key != "gaps"},
        "sample_size": len(values),
        "data_status": DATA_STATUS_OK,
        "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
    }


# --------------------------------------------------------------------------- #
# 样本外命中统计（复用 dist_engine 的组件口径）
# --------------------------------------------------------------------------- #
def _pick_hits(values: Sequence[int], picks: Iterable[int], actual: int) -> int:
    return 1 if int(actual) in {int(value) for value in picks} else 0


def hot_cold_test(
    numbers: Sequence[int],
    *,
    k: int = K_DEFAULT,
    split_ratio: float = 0.5,
) -> dict[str, Any]:
    """热冷持续性：前半段频次 top-k 打后半段，看命中率能否超过 ``k/49``。"""
    values = [int(value) for value in numbers]
    split = int(len(values) * min(0.9, max(0.1, float(split_ratio))))
    train = values[:split]
    valid = values[split:]
    if len(train) < NUM_STATES or len(valid) < 5:
        return {
            "handle": HANDLE_HOT_COLD,
            "statistic": None,
            "hits": None,
            "evaluated": len(valid),
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    counts = Counter(train)
    ranked = sorted(
        range(NUMBER_MIN, NUMBER_MAX + 1),
        key=lambda number: (-counts.get(number, 0), number),
    )
    picks = ranked[: max(1, int(k))]
    pick_set = {int(value) for value in picks}
    hits = sum(1 for actual in valid if actual in pick_set)
    return {
        "handle": HANDLE_HOT_COLD,
        "statistic": float(hits),
        "hits": hits,
        "evaluated": len(valid),
        "hit_rate": hits / len(valid),
        "baseline_hit_rate": UNIFORM_RATE,
        "picks": picks,
        "train_size": len(train),
        "p_binomial": binomial_tail_p(hits, len(valid), UNIFORM_RATE, alternative="greater"),
        "data_status": DATA_STATUS_OK,
        "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
    }


def gap_test(
    numbers: Sequence[int],
    *,
    k: int = K_DEFAULT,
    warmup: int = 30,
) -> dict[str, Any]:
    """遗漏 / 到期号预测力：对每期取「最久未出」top-k，逐期命中计数。

    这是「赌徒谬误」的正确检验：真的把到期号选出来打样本外。
    """
    values = [int(value) for value in numbers]
    if len(values) <= warmup + 5:
        return {
            "handle": HANDLE_GAP,
            "statistic": None,
            "hits": None,
            "evaluated": 0,
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    hits = 0
    evaluated = 0
    for position in range(int(warmup), len(values)):
        history = values[:position]
        last_seen: dict[int, int] = {}
        for offset, number in enumerate(reversed(history)):
            if number not in last_seen:
                last_seen[number] = offset
        ranked = sorted(
            range(NUMBER_MIN, NUMBER_MAX + 1),
            key=lambda number: (-last_seen.get(number, len(history)), number),
        )
        picks = set(ranked[: max(1, int(k))])
        if values[position] in picks:
            hits += 1
        evaluated += 1
    return {
        "handle": HANDLE_GAP,
        "statistic": float(hits),
        "hits": hits,
        "evaluated": evaluated,
        "hit_rate": hits / evaluated,
        "baseline_hit_rate": UNIFORM_RATE,
        "p_binomial": binomial_tail_p(hits, evaluated, UNIFORM_RATE, alternative="greater"),
        "data_status": DATA_STATUS_OK,
        "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
    }


def component_topk_test(
    numbers: Sequence[int],
    component: str,
    handle: str,
    *,
    k: int = K_DEFAULT,
    warmup: int = 30,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把一个 ``dist_engine`` 组件当作独立选号器，逐期样本外命中计数。"""
    values = [int(value) for value in numbers]
    if len(values) <= warmup + 5:
        return {
            "handle": handle,
            "statistic": None,
            "hits": None,
            "evaluated": 0,
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    hits = 0
    evaluated = 0
    for position in range(int(warmup), len(values)):
        picks = set(component_topk(values, position, component, k=int(k), params=params))
        if values[position] in picks:
            hits += 1
        evaluated += 1
    return {
        "handle": handle,
        "component": component,
        "statistic": float(hits),
        "hits": hits,
        "evaluated": evaluated,
        "hit_rate": hits / evaluated,
        "baseline_hit_rate": UNIFORM_RATE,
        "p_binomial": binomial_tail_p(hits, evaluated, UNIFORM_RATE, alternative="greater"),
        "data_status": DATA_STATUS_OK,
        "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
    }


def zodiac_topk_test(
    numbers: Sequence[int], *, k: int = K_DEFAULT, warmup: int = 30
) -> dict[str, Any]:
    """生肖热度 lift（复用 ``ZODIAC`` 组件）。"""
    return component_topk_test(
        numbers, COMPONENT_ZODIAC, HANDLE_ZODIAC_HOT, k=k, warmup=warmup
    )


def wave_topk_test(
    numbers: Sequence[int], *, k: int = K_DEFAULT, warmup: int = 30
) -> dict[str, Any]:
    """波动 / 点阵 lift（复用 ``WAVE`` 组件）。"""
    return component_topk_test(numbers, COMPONENT_WAVE, HANDLE_WAVE_LATTICE, k=k, warmup=warmup)


def gap_component_test(
    numbers: Sequence[int], *, k: int = K_DEFAULT, warmup: int = 30
) -> dict[str, Any]:
    """遗漏 / 到期号 lift（复用 ``GAP`` 组件，与 ``gap_test`` 互为口径核对）。"""
    return component_topk_test(numbers, COMPONENT_GAP, HANDLE_GAP, k=k, warmup=warmup)


# 8 个引擎组件 → 审计 handle 的映射（保证「每个组件都能单独被审计」）。
# FREQUENCY 用前半频次打后半（hot_cold）；PERIODIC 用谱扫描（同一条「周期」假设）；
# 其余用 component_topk 逐期样本外。8 个组件恰好落进 8 个 handle，无遗漏、无重复。
COMPONENT_HANDLE_MAP: dict[str, str] = {
    COMPONENT_FREQUENCY: HANDLE_HOT_COLD,
    COMPONENT_GAP: HANDLE_GAP,
    COMPONENT_ZODIAC: HANDLE_ZODIAC_HOT,
    COMPONENT_WAVE: HANDLE_WAVE_LATTICE,
    COMPONENT_MARKOV: HANDLE_MARKOV,
    COMPONENT_STRUCTURE: HANDLE_STRUCTURE,
    COMPONENT_REPEAT: HANDLE_REPEAT,
    COMPONENT_PERIODIC: HANDLE_PERIODIC,
}

# 组件级独立检验的 handle（= COMPONENT_HANDLE_MAP 的值集合，供 canvas / 报告分栏）
COMPONENT_HANDLES: tuple[str, ...] = tuple(COMPONENT_HANDLE_MAP.values())


def standalone_component_test(
    numbers: Sequence[int], component: str, *, k: int = K_DEFAULT, warmup: int = 30
) -> dict[str, Any]:
    """单个 ``dist_engine`` 组件的独立样本外检验（把组件当独立选号器）。"""
    if component not in COMPONENT_HANDLE_MAP:
        raise ValueError(f"未知组件：{component}")
    if component == COMPONENT_FREQUENCY:
        result = dict(hot_cold_test(numbers, k=k))
        result["component"] = component
        result["test"] = "split_half_frequency"
        return result
    if component == COMPONENT_PERIODIC:
        result = dict(spectral_scan(numbers))
        result["component"] = component
        result["test"] = "spectral_scan"
        return result
    result = component_topk_test(
        numbers, component, COMPONENT_HANDLE_MAP[component], k=k, warmup=warmup
    )
    result["test"] = "component_topk"
    return result


# --------------------------------------------------------------------------- #
# 周期 / 谱扫描
# --------------------------------------------------------------------------- #
def spectral_scan(
    numbers: Sequence[int], *, max_period: int = DEFAULT_MAX_PERIOD
) -> dict[str, Any]:
    """对特码去均值序列做离散谱扫描（周期 2..P），返回最大功率与最佳周期。"""
    values = [float(value) for value in numbers]
    size = len(values)
    if size < 8:
        return {
            "handle": HANDLE_PERIODIC,
            "statistic": None,
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    mean = sum(values) / size
    centered = [value - mean for value in values]
    limit = max(2, min(int(max_period), size // 2))
    rows: list[dict[str, Any]] = []
    for period in range(2, limit + 1):
        omega = 2.0 * math.pi / period
        real = 0.0
        imag = 0.0
        for index, value in enumerate(centered):
            real += value * math.cos(omega * index)
            imag -= value * math.sin(omega * index)
        power = (real * real + imag * imag) / size
        rows.append({"period": period, "power": power})
    best = max(rows, key=lambda row: (row["power"], -row["period"]))
    ordered = sorted(rows, key=lambda row: (-row["power"], row["period"]))
    return {
        "handle": HANDLE_PERIODIC,
        "statistic": best["power"],
        "best_period": best["period"],
        "max_period": limit,
        "top_periods": ordered[:5],
        "data_status": DATA_STATUS_OK,
        "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
    }


# --------------------------------------------------------------------------- #
# 生产引擎自身特征的逐项消融
# --------------------------------------------------------------------------- #
def default_audit_settings(
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """审计用的统一基线配置：固定 ``pick_count = k``、``mode = even``。"""
    merged = {**DEFAULT_SETTINGS, "pick_count": K_DEFAULT, "mode": "even"}
    if settings:
        merged.update({key: value for key, value in settings.items() if value is not None})
    return clamp_settings(merged)


def engine_feature_definitions() -> list[dict[str, Any]]:
    """四个生产引擎特征的「开 / 关」配置（全部只改一个旋钮）。"""
    return [
        {
            "handle": HANDLE_ENGINE_REPEAT_NUMBER,
            "label": "重号：保留（降权）vs 排除",
            "on": {"include_repeat_number": True},
            "off": {"include_repeat_number": False},
        },
        {
            "handle": HANDLE_ENGINE_REPEAT_ZODIAC,
            "label": "重肖：避开 vs 不避",
            "on": {"exclude_repeat_zodiac": True},
            "off": {"exclude_repeat_zodiac": False},
        },
        {
            "handle": HANDLE_ENGINE_COLD,
            "label": "避冷加权：开 vs 关",
            "on": {"avoid_cold_enabled": True},
            "off": {"avoid_cold_enabled": False},
        },
        {
            "handle": HANDLE_ENGINE_LATTICE,
            "label": "点阵：开 vs 关",
            "on": {"lattice_enabled": True},
            "off": {"lattice_enabled": False},
        },
    ]


def engine_feature_hits(
    draws: Sequence[Mapping[str, Any]],
    *,
    base_settings: Mapping[str, Any] | None = None,
    min_prior_draws: int | None = None,
) -> dict[str, Any]:
    """对四个特征各跑一次开 / 关的 walk-forward 回测，返回命中数与提升 Δ。

    统计量 = ``hit_rate_on - hit_rate_off``（正 = 开启该特征有正提升）。
    这是直接回答「作者自己的这几个特征到底有没有样本外提升」的那张表。
    """
    from services import analytics as analytics  # 局部导入，避免模块级循环

    base = default_audit_settings(base_settings)
    prior = int(min_prior_draws if min_prior_draws is not None else analytics.MIN_PRIOR_DRAWS)
    output: dict[str, Any] = {}
    for definition in engine_feature_definitions():
        rows: dict[str, Any] = {}
        for side in ("on", "off"):
            config = {**base, **definition[side]}
            outcome = analytics.backtest_stats(
                draws,
                base_settings=config,
                min_prior_draws=prior,
                include_results=False,
                include_wave_breakdown=False,
            )
            rows[side] = {
                "hits": outcome.get("hits"),
                "evaluated": outcome.get("evaluated"),
                "hit_rate": outcome.get("hit_rate"),
                "delta_vs_random": outcome.get("hit_rate_minus_baseline"),
                "verdict": (outcome.get("verdict") or {}).get("kind"),
            }
        on_rate = rows["on"]["hit_rate"]
        off_rate = rows["off"]["hit_rate"]
        delta = (on_rate - off_rate) if (on_rate is not None and off_rate is not None) else None
        available = on_rate is not None and off_rate is not None
        output[definition["handle"]] = {
            "handle": definition["handle"],
            "label": definition["label"],
            "statistic": delta,
            "on": rows["on"],
            "off": rows["off"],
            "settings_on": {**base, **definition["on"]},
            "settings_off": {**base, **definition["off"]},
            "data_status": DATA_STATUS_OK if available else DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[
                DATA_STATUS_OK if available else DATA_STATUS_INSUFFICIENT
            ],
        }
    return output


# --------------------------------------------------------------------------- #
# 置换零分布辅助
# --------------------------------------------------------------------------- #
def permutation_p(
    observed: float | None, null_values: Sequence[float], *, direction: str = "greater"
) -> float | None:
    """置换单侧 p：``(1 + #{零分布至少一样极端}) / (1 + 置换次数)``。"""
    if observed is None:
        return None
    values = [float(value) for value in null_values]
    if not values:
        return None
    if direction == "greater":
        exceed = sum(1 for value in values if value >= observed)
    elif direction == "less":
        exceed = sum(1 for value in values if value <= observed)
    else:
        raise ValueError(f"未知方向：{direction}")
    return (1 + exceed) / (1 + len(values))


def summarize_null(values: Sequence[float], observed: float | None) -> dict[str, Any]:
    """零分布摘要：均值 / sd / p95 / 单侧 p（方向 = 越大越强）。"""
    samples = sorted(float(value) for value in values)
    if not samples:
        return {
            "n": 0,
            "mean": None,
            "sd": None,
            "p95": None,
            "observed": observed,
            "p_one_sided": None,
        }
    mean = sum(samples) / len(samples)
    sd = math.sqrt(sum((value - mean) ** 2 for value in samples) / len(samples))
    return {
        "n": len(samples),
        "mean": mean,
        "sd": sd,
        "p50": samples[len(samples) // 2],
        "p95": samples[min(len(samples) - 1, int(0.95 * len(samples)))],
        "max": samples[-1],
        "observed": observed,
        "p_one_sided": permutation_p(observed, samples),
    }


# --------------------------------------------------------------------------- #
# 多重比较校正与判定
# --------------------------------------------------------------------------- #
def correct_handles(
    handles: Sequence[Mapping[str, Any]], *, alpha: float = 0.05
) -> dict[str, Any]:
    """对整族 handle 做 Holm 逐步向下校正，并给出英文 verdict。

    - 先声明族大小 ``m``，再统一校正；**禁止把原始 p 当唯一次检验汇报**；
    - ``p_value`` 为 ``None``（数据不足）→ verdict = ``insufficient_data``；
    - ``p_adjusted <= alpha`` → ``signal``，否则 ``no_signal``。
    """
    sized = [handle for handle in handles if handle.get("p_value") is not None]
    pvalues = [float(handle["p_value"]) for handle in sized]
    correction = holm_adjusted_p(pvalues, alpha) if pvalues else {
        "alpha": alpha,
        "adjusted": [],
        "significant_count": 0,
        "significant_indices": [],
        "bonferroni_threshold": alpha,
    }
    adjusted = correction["adjusted"]
    rows: list[dict[str, Any]] = []
    cursor = 0
    for handle in handles:
        row = dict(handle)
        p_value = handle.get("p_value")
        if p_value is None:
            row["p_adjusted"] = None
            row["verdict"] = VERDICT_INSUFFICIENT
        else:
            row["p_adjusted"] = adjusted[cursor]
            cursor += 1
            row["verdict"] = (
                VERDICT_SIGNAL if row["p_adjusted"] <= alpha else VERDICT_NO_SIGNAL
            )
        row["verdict_label"] = VERDICT_LABELS[row["verdict"]]
        rows.append(row)
    survivors = [row["handle"] for row in rows if row["verdict"] == VERDICT_SIGNAL]
    return {
        "family_size": len(handles),
        "tested_size": len(sized),
        "alpha": alpha,
        "correction": "holm",
        "bonferroni_threshold": correction["bonferroni_threshold"],
        "min_p_raw": min(pvalues) if pvalues else None,
        "min_p_adjusted": min(adjusted) if adjusted else None,
        "survivors": survivors,
        "survivor_count": len(survivors),
        "rows": rows,
        "data_status": DATA_STATUS_OK if sized else DATA_STATUS_INSUFFICIENT,
        "data_status_label": DATA_STATUS_LABELS[
            DATA_STATUS_OK if sized else DATA_STATUS_INSUFFICIENT
        ],
    }


# --------------------------------------------------------------------------- #
# 状态 / 功效
# --------------------------------------------------------------------------- #
def assess_state(
    draws: Sequence[Mapping[str, Any]],
    numbers: Sequence[int],
    *,
    k: int = K_DEFAULT,
    odds: float = float(ODDS_DEFAULT),
) -> dict[str, Any]:
    """样本状态：期数、不重复号数、日期范围、基线、功效口径。"""
    values = [int(value) for value in numbers]
    periods = [int(row["period"]) for row in draws if row.get("period") is not None]
    dates = sorted(str(row["draw_date"])[:10] for row in draws if row.get("draw_date"))
    sample_size = len(values)
    baseline = k / NUM_STATES
    return {
        "sample_size": sample_size,
        "distinct_numbers": len(set(values)),
        "availability_ratio": (len(set(values)) / NUM_STATES) if sample_size else None,
        "expected_appearances_per_number": (sample_size / NUM_STATES) if sample_size else None,
        "period_first": periods[0] if periods else None,
        "period_last": periods[-1] if periods else None,
        "date_first": dates[0] if dates else None,
        "date_last": dates[-1] if dates else None,
        "k": int(k),
        "odds": float(odds),
        "baseline_hit_rate": baseline,
        "break_even_hit_rate": k / float(odds),
        "break_even_edge": k / float(odds) - baseline,
        "scope": f"本池已导入 {sample_size} 期样本内",
        "power": minimum_detectable_delta(sample_size, baseline),
    }


__all__ = [
    "ALL_HANDLES",
    "COMPONENT_HANDLES",
    "COMPONENT_HANDLE_MAP",
    "DATA_STATUS_INSUFFICIENT",
    "DATA_STATUS_LABELS",
    "DATA_STATUS_OK",
    "DEFAULT_MAX_LAG",
    "DEFAULT_MAX_PERIOD",
    "ENTROPY_SIMULATIONS",
    "ENGINE_FEATURE_HANDLES",
    "MIN_UNIFORMITY_SAMPLE",
    "UNIFORMITY_SIMULATIONS",
    "HANDLE_AUTOCORR_BIGSMALL",
    "HANDLE_AUTOCORR_PARITY",
    "HANDLE_AUTOCORR_SPECIAL",
    "HANDLE_AUTOCORR_TAIL",
    "HANDLE_AUTOCORR_ZODIAC",
    "HANDLE_ENGINE_COLD",
    "HANDLE_ENGINE_LATTICE",
    "HANDLE_ENGINE_REPEAT_NUMBER",
    "HANDLE_ENGINE_REPEAT_ZODIAC",
    "HANDLE_ENTROPY",
    "HANDLE_GAP",
    "HANDLE_HOT_COLD",
    "HANDLE_INDEPENDENCE",
    "HANDLE_LABELS",
    "HANDLE_MARKOV",
    "HANDLE_MUTUAL_INFORMATION",
    "HANDLE_PERIODIC",
    "HANDLE_REPEAT",
    "HANDLE_RUNS_BIGSMALL",
    "HANDLE_RUNS_PARITY",
    "HANDLE_STRUCTURE",
    "HANDLE_UNIFORMITY",
    "HANDLE_WAVE_LATTICE",
    "HANDLE_ZODIAC_HOT",
    "PARAMETRIC_HANDLES",
    "PERMUTATION_HANDLES",
    "UNIFORM_RATE",
    "VERDICT_INSUFFICIENT",
    "VERDICT_LABELS",
    "VERDICT_NO_SIGNAL",
    "VERDICT_SIGNAL",
    "assess_state",
    "autocorrelation",
    "autocorrelation_scan",
    "chi2_sf",
    "chi_square_independence",
    "component_topk_test",
    "contingency_matrix",
    "correct_handles",
    "default_audit_settings",
    "derived_series",
    "engine_feature_definitions",
    "engine_feature_hits",
    "entropy_nats",
    "entropy_null_band",
    "entropy_test",
    "gap_component_test",
    "gap_test",
    "hot_cold_test",
    "independence_test",
    "lag_pairs",
    "mutual_information_bits",
    "number_counts",
    "permutation_p",
    "regularized_gamma_q",
    "runs_test",
    "spectral_scan",
    "standalone_component_test",
    "summarize_null",
    "uniformity_null_band",
    "uniformity_test",
    "wave_topk_test",
    "zodiac_topk_test",
]
