"""最大拟合算法族 —— 「尽可能高度拟合本池开奖」的极限演示（**仅拟合，非预测**）。

本模块正面实现使用者提出的目标：**造一条把本池 210 期开奖拟合到极限的规则**。
它把「拟合到什么程度」做成可测量的两把尺子，每一把都同时给**样本内**与**样本外**：

1. ``hit_rate``：top-k 推荐集合是否盖住实际特码（二值，最常用的粗糙口径）；
2. ``mean_rank``：实际特码在模型给出的 1..49 完整排序里排第几（1 = 排最前）。
   均匀随机的期望是 25.0。这是「尽可能接近开奖号码」的诚实度量，远比二值命中敏感；
3. ``log_loss`` / ``brier``：对整个 1..49 概率向量的密度拟合评分
   （均匀参考分别为 ``ln 49`` 与 ``(1/49)(1 − 1/49)``），能抓到二值命中看不到的密度增益。

口径铁律（违反即为缺陷）：

- **本模块只做样本内拟合演示，禁止进入线上推荐路径**：``services.lottery``、
  ``routers/*``、``main.py`` 一律不得 import / 引用 ``max_fit``（有回归测试守着）。
  线上推荐引擎与 ``DEFAULT_SETTINGS`` 完全不经本模块，本模块也不读写任何数据库。
- 所有结论只针对「本池已导入的 N 期数据」这个样本；**禁止写「全市场」**
  —— 本池没有全市场数据，任何升格都是编造。
- 每个模型的每次预测都带英文诚实标记 ``fit_role = "FIT_ONLY"`` 与中文
  ``notes`` / ``disclaimer``：本模块的样本外表现与均匀随机基线不可区分，
  它**不是预测**。
- 落库 / 传输的枚举一律英文码（``MEMORIZE`` / ``MARKOV_1`` / …），中文只出现在
  展示标签（``*_label``）与提示文案里。

严格 walk-forward 契约：``predict`` 只能看见调用方交给它的 ``available`` 序列，
并据此对 ``target_index`` 期给出完整排序。调用方传 ``available = series[:target_index]``
即为严格样本外（目标期从不进入拟合）；传 ``available = series[:target_index + 1]``
即为「反推」样本内口径（目标期本身被计入拟合）。模型自身不读全序列以外的任何数据。
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
NUMBER_MIN = 1
NUMBER_MAX = 49
NUMBERS: tuple[int, ...] = tuple(range(NUMBER_MIN, NUMBER_MAX + 1))
NUM_STATES = len(NUMBERS)  # 49

K_DEFAULT = 10
ODDS_DEFAULT = 47.0
# 与 services.analytics.MIN_PRIOR_DRAWS 一致：至少要有「前一期」才能算相邻差值
WARMUP = 2

# 诚实标记（英文枚举，落库 / 传输用英文码；汉字只出现在 label / 文案）
FIT_ROLE = "FIT_ONLY"
FIT_ROLE_LABEL = "仅拟合演示（不是预测）"

# 模型 id（英文枚举）
MODEL_UNIFORM = "UNIFORM"
MODEL_MEMORIZE = "MEMORIZE"
MODEL_MARKOV_1 = "MARKOV_1"
MODEL_MARKOV_2 = "MARKOV_2"
MODEL_NGRAM_SMOOTHED = "NGRAM_SMOOTHED"
MODEL_REGULARIZED_BEST_FIT = "REGULARIZED_BEST_FIT"
MODEL_LATTICE_FIT = "LATTICE_FIT"

MODEL_IDS: tuple[str, ...] = (
    MODEL_UNIFORM,
    MODEL_MEMORIZE,
    MODEL_MARKOV_1,
    MODEL_MARKOV_2,
    MODEL_NGRAM_SMOOTHED,
    MODEL_REGULARIZED_BEST_FIT,
    MODEL_LATTICE_FIT,
)

MODEL_LABELS: dict[str, str] = {
    MODEL_UNIFORM: "均匀基线（参照）",
    MODEL_MEMORIZE: "期序号记忆",
    MODEL_MARKOV_1: "一阶马尔可夫",
    MODEL_MARKOV_2: "二阶马尔可夫",
    MODEL_NGRAM_SMOOTHED: "平滑 3-gram",
    MODEL_REGULARIZED_BEST_FIT: "正则化最优拟合",
    MODEL_LATTICE_FIT: "拟合点阵带",
}

# 均匀参考值（1..49）
UNIFORM_PROB = 1.0 / NUM_STATES
UNIFORM_MEAN_RANK = (NUM_STATES + 1) / 2.0  # 25.0
UNIFORM_LOG_LOSS = math.log(NUM_STATES)  # ln 49
# 多分类 Brier 的「逐类平均」口径：(1/49)·Σ(p_n − y_n)²；均匀参考 = (1/49)(1 − 1/49)
UNIFORM_BRIER = (1.0 / NUM_STATES) * (1.0 - 1.0 / NUM_STATES)

# 概率下限：把任意权重向量抬成「处处为正、和恰为 1」的分布，
# 避免 log-loss 出现 ln(0)。它只影响末尾若干数量级，不改变排序。
PROBABILITY_FLOOR = 1e-6

# 拟合超参数网格（确定性、可复现）
NGRAM_ORDER_CHAIN = 3
NGRAM_ALPHA_GRID: tuple[float, ...] = (0.01, 0.1, 0.5, 1.0, 5.0, 20.0)
REG_ORDER_GRID: tuple[int, ...] = (1, 2, 3)
REG_ALPHA_GRID: tuple[float, ...] = (0.0, 0.01, 0.1, 0.5, 1.0, 5.0, 20.0)
LATTICE_COVERAGE_GRID: tuple[float, ...] = (0.5, 0.75, 0.9, 1.0)
LATTICE_DECAY_SCALE = 6.0

# 内层 walk-forward（选参）默认切分
DEFAULT_TRAIN_RATIO = 0.7
MAX_INNER_HOLDOUT = 30

DISCLAIMER = (
    "这是样本内拟合演示，不是预测：本算法的样本外表现与均匀随机基线（命中率 10/49、"
    "平均排名 25、log-loss ln49、Brier (1/49)(1−1/49)）不可区分；"
    "它把本池历史「解释」到极限，却没有任何对未来开奖的预测力，禁止用于投注决策。"
)

_FIT_ONLY_NOTE = (
    "口径：本池样本内拟合演示，禁止进入线上推荐路径；样本外表现即均匀基线，不构成预测。"
)


# --------------------------------------------------------------------------- #
# 基础工具（纯函数）
# --------------------------------------------------------------------------- #
def _clamp_number(value: int) -> int:
    return min(NUMBER_MAX, max(NUMBER_MIN, int(value)))


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """升序序列的线性插值分位数（``sorted_values`` 必须已升序）。"""
    size = len(sorted_values)
    if size == 0:
        return 0.0
    if size == 1:
        return float(sorted_values[0])
    position = max(0.0, min(1.0, float(q))) * (size - 1)
    lower = int(math.floor(position))
    upper = min(lower + 1, size - 1)
    frac = position - lower
    return float(sorted_values[lower]) * (1.0 - frac) + float(sorted_values[upper]) * frac


def probabilities_from_weights(
    weights: Mapping[int, float], floor: float = PROBABILITY_FLOOR
) -> dict[int, float]:
    """把任意非负权重抬成一个「处处为正、和恰为 1」的 1..49 分布。

    ``p(n) = (1 − floor)·w(n)/Σw + floor/49``。权重为空 / 全零 → 均匀分布。
    ``floor`` 只保证 log-loss 有限，不影响排序。
    """
    total = sum(float(value) for value in weights.values())
    floor = max(0.0, min(1.0, float(floor)))
    if total <= 0.0:
        return {number: UNIFORM_PROB for number in NUMBERS}
    scale = 1.0 / total
    return {
        number: (1.0 - floor) * (float(weights.get(number, 0.0)) * scale)
        + floor * UNIFORM_PROB
        for number in NUMBERS
    }


def uniform_probabilities() -> dict[int, float]:
    """严格均匀的 1..49 分布（本模块所有模型的兜底与参照）。"""
    return {number: UNIFORM_PROB for number in NUMBERS}


def uniform_ranking(target_index: int) -> list[int]:
    """均匀（无信息）时的完整排序：起点随期号平移、步长 10（与 49 互质）取满 1..49。

    与 ``scripts.fit_capacity_analysis._uniform_picks`` 同型（那里只取前 k 个），
    因此本模块的「均匀兜底」与仓库既有均匀基线口径一致 —— 均匀分布的 top-k 集合
    不应固定成 {1..10}，而应随期号平移。它仍是 1..49 的一个排列。
    """
    start = int(target_index)
    return [((start + step * 10) % NUM_STATES) + 1 for step in range(NUM_STATES)]


def ranking_from_probabilities(
    probabilities: Mapping[int, float],
    tie_break: Sequence[int] | None = None,
) -> list[int]:
    """概率降序的完整号码排序（1..49 的一个排列）。

    概率完全相同时（例如均匀兜底）用 ``tie_break`` 指定次序；缺省按号码升序。
    ``tie_break`` 必须是 1..49 的一个排列（``uniform_ranking`` 即为一例）。
    """
    order = (
        {int(number): position for position, number in enumerate(tie_break)}
        if tie_break is not None
        else {}
    )
    return [
        int(number)
        for number, _ in sorted(
            probabilities.items(),
            key=lambda item: (
                -round(float(item[1]), 12),
                order.get(int(item[0]), int(item[0])),
            ),
        )
    ]


def mean_rank(ranks: Sequence[int]) -> float | None:
    """平均排名；空样本返回 ``None``（数据不足不用 0 冒充）。"""
    values = [int(rank) for rank in ranks]
    if not values:
        return None
    return sum(values) / len(values)


def rank_bucket_shares(
    ranks: Sequence[int], k: int = K_DEFAULT
) -> dict[str, float] | None:
    """排名分布：top-k / 中间段 / 末段 的占比（1..49）。

    边界：``1..k`` 为 top 段，``k+1..25`` 为中间段，``26..49`` 为末段。
    """
    values = [int(rank) for rank in ranks]
    total = len(values)
    if total == 0:
        return None
    mid_end = (NUM_STATES + 1) // 2  # 25
    top = sum(1 for rank in values if rank <= int(k))
    mid = sum(1 for rank in values if int(k) < rank <= mid_end)
    bottom = total - top - mid
    return {
        "top_k": top / total,
        "mid": mid / total,
        "bottom": bottom / total,
    }


def log_loss(probabilities: Mapping[int, float], actual: int) -> float:
    """多分类对数损失 ``−ln p(actual)``。"""
    value = float(probabilities.get(int(actual), 0.0))
    if value <= 0.0:
        return math.inf
    return -math.log(value)


def brier_score(probabilities: Mapping[int, float], actual: int) -> float:
    """多分类 Brier 的逐类平均口径：``(1/49)·Σ_n (p_n − y_n)²``。

    均匀参考值恰为 ``(1/49)(1 − 1/49)``（见 ``UNIFORM_BRIER``）。
    """
    target = int(actual)
    total = 0.0
    for number in NUMBERS:
        y = 1.0 if number == target else 0.0
        diff = float(probabilities.get(number, 0.0)) - y
        total += diff * diff
    return total / NUM_STATES


# --------------------------------------------------------------------------- #
# 模型规格（自由参数 / 单元数 / 家族）
# --------------------------------------------------------------------------- #
_ORDER3_CELLS = NUM_STATES ** 3
_ORDER3_FREE = _ORDER3_CELLS - NUM_STATES ** 2
_ORDER2_CELLS = NUM_STATES ** 2
_ORDER2_FREE = _ORDER2_CELLS - NUM_STATES


def model_spec(model: str) -> dict[str, Any]:
    """模型规格：自由参数数 / 单元数 / 家族 / 说明（用于结果表表头）。"""
    if model == MODEL_UNIFORM:
        return {
            "model": model,
            "label": MODEL_LABELS[model],
            "family": "基线",
            "free_parameters": 0,
            "cells": 0,
            "dynamic_free_parameters": False,
            "desc": "零参数均匀基线：每个号等概率 1/49，供其余模型对照",
        }
    if model == MODEL_MEMORIZE:
        return {
            "model": model,
            "label": MODEL_LABELS[model],
            "family": "记忆化",
            "free_parameters": None,  # 动态：= 拟合期数
            "cells": None,
            "dynamic_free_parameters": True,
            "desc": "按期序号查表背下每一期答案（参数数 = 拟合的期数），样本内可复现 100%",
        }
    if model == MODEL_MARKOV_1:
        return {
            "model": model,
            "label": MODEL_LABELS[model],
            "family": "马尔可夫",
            "free_parameters": _ORDER2_FREE,
            "cells": _ORDER2_CELLS,
            "dynamic_free_parameters": False,
            "desc": "一阶转移表 49×49，逐级回退到全历史频次 / 均匀，输出完整排序",
        }
    if model == MODEL_MARKOV_2:
        return {
            "model": model,
            "label": MODEL_LABELS[model],
            "family": "马尔可夫",
            "free_parameters": _ORDER3_FREE,
            "cells": _ORDER3_CELLS,
            "dynamic_free_parameters": False,
            "desc": "二阶转移表 49³，回退链 2→1→均匀，输出完整排序",
        }
    if model == MODEL_NGRAM_SMOOTHED:
        return {
            "model": model,
            "label": MODEL_LABELS[model],
            "family": "n-gram",
            "free_parameters": _ORDER3_FREE + 1,
            "cells": _ORDER3_CELLS,
            "dynamic_free_parameters": False,
            "desc": "3→2→1→均匀 回退 n-gram，平滑强度 α 在训练窗内层 walk-forward 拟合",
        }
    if model == MODEL_REGULARIZED_BEST_FIT:
        return {
            "model": model,
            "label": MODEL_LABELS[model],
            "family": "正则化",
            "free_parameters": _ORDER3_FREE + 2,
            "cells": _ORDER3_CELLS,
            "dynamic_free_parameters": False,
            "desc": "阶数与平滑强度 α 由训练窗内层 walk-forward 选；α→0 退化为纯记忆化",
        }
    if model == MODEL_LATTICE_FIT:
        return {
            "model": model,
            "label": MODEL_LABELS[model],
            "family": "点阵",
            "free_parameters": 2,
            "cells": 0,
            "dynamic_free_parameters": False,
            "desc": "中心偏移 + 半宽 + 覆盖分位均由数据拟合（非默认值）的点阵带",
        }
    raise ValueError(f"未知模型：{model}")


# --------------------------------------------------------------------------- #
# 打分内核
# --------------------------------------------------------------------------- #
def _build_transition_tables(
    available: Sequence[int], max_order: int
) -> dict[int, dict[tuple[int, ...], dict[int, int]]]:
    """把 ``available`` 里 1..``max_order`` 阶的「状态 → 后继计数」一次建好。"""
    sequence = [int(value) for value in available]
    length = len(sequence)
    tables: dict[int, dict[tuple[int, ...], dict[int, int]]] = {}
    for order in range(1, max_order + 1):
        table: dict[tuple[int, ...], dict[int, int]] = {}
        for index in range(order, length):
            state = tuple(sequence[index - order : index])
            bucket = table.get(state)
            if bucket is None:
                bucket = {}
                table[state] = bucket
            successor = sequence[index]
            bucket[successor] = bucket.get(successor, 0) + 1
        tables[order] = table
    return tables


def ngram_probabilities(
    available: Sequence[int],
    target_index: int,
    order: int,
    alpha: float,
) -> dict[int, float]:
    """回退 n-gram 的条件分布：``order → … → 1 → 均匀``，逐级递归平滑。

    递归平滑（绝对平滑 / 贝叶斯回退）：

        p_0(n) = 1/49
        p_j(n) = (count_j(context_j → n) + α · p_{j-1}(n)) / (count_j(context_j) + α)

    ``α`` 是平滑（正则化）强度：α 越大越靠近均匀；``α = 0`` 时若上下文出现过，
    分布完全压在「已观测后继」上 —— 这正是「正则化 → 0 退化为记忆化」的极限。
    """
    sequence = [int(value) for value in available]
    max_order = max(1, int(order))
    tables = _build_transition_tables(sequence, max_order)
    probabilities = uniform_probabilities()
    alpha = max(0.0, float(alpha))
    for current_order in range(1, max_order + 1):
        start = target_index - current_order
        if start < 0:
            break  # 上下文不足，更高阶同样不足
        state = tuple(sequence[start:target_index])
        bucket = tables.get(current_order, {}).get(state)
        if not bucket:
            continue
        total = sum(bucket.values())
        if alpha <= 0.0:
            probabilities = {
                number: bucket.get(number, 0) / total for number in NUMBERS
            }
        else:
            probabilities = {
                number: (bucket.get(number, 0) + alpha * probabilities[number])
                / (total + alpha)
                for number in NUMBERS
            }
    return probabilities


def _memorize_probabilities(
    available: Sequence[int], target_index: int
) -> tuple[dict[int, float], dict[str, Any]]:
    """期序号记忆：``available`` 里第 ``target_index`` 期的答案若在表内，直接押它。"""
    table = {index: int(available[index]) for index in range(len(available))}
    if target_index in table:
        return {table[target_index]: 1.0}, {"memorised": True, "table_size": len(table)}
    return {}, {"memorised": False, "table_size": len(table)}


def fit_lattice_band(available: Sequence[int], coverage: float = 1.0) -> dict[str, Any]:
    """拟合点阵带：**中心偏移**（有符号相邻差值的中位数）与**半宽**（覆盖分位）都由数据定。

    - ``offset``：相邻差值的中位数（稳健中心，抗离群）；
    - ``half_width``：``|差值 − 偏移|`` 的 ``coverage`` 分位数向上取整 ——
      ``coverage = 1.0`` 即「半宽取到能盖住所有已观测差值」，这是「尽可能贴近开奖号」的极限；
    - ``fitted_coverage``：这批差值实际落在带内的比例（``coverage = 1.0`` 时恒为 1.0）。

    ``available`` 只用于拟合带本身；调用方负责保证它不含未来数据。
    """
    sequence = [int(value) for value in available]
    diffs = [sequence[index] - sequence[index - 1] for index in range(1, len(sequence))]
    if not diffs:
        return {
            "offset": 0,
            "half_width": 0,
            "fitted_coverage": None,
            "coverage_target": float(coverage),
            "samples": 0,
        }
    offset = int(round(_quantile(sorted(diffs), 0.5)))
    adjusted = sorted(abs(diff - offset) for diff in diffs)
    half_width = max(0, int(math.ceil(_quantile(adjusted, float(coverage)))))
    covered = sum(1 for value in adjusted if value <= half_width)
    return {
        "offset": offset,
        "half_width": half_width,
        "fitted_coverage": covered / len(adjusted),
        "coverage_target": float(coverage),
        "samples": len(adjusted),
    }


def _lattice_fit_probabilities(
    available: Sequence[int],
    target_index: int,
    coverage: float,
    k: int,
) -> tuple[dict[int, float], dict[str, Any]]:
    """拟合点阵带：中心偏移 = 有符号差值中位数；半宽 = |差值−偏移| 的覆盖分位。

    两个参数都**由数据拟合**（不是默认值），覆盖分位 ``coverage`` 也由内层
    walk-forward 选。中心 = ``available[target_index-1] + offset``（上一期开奖号
    + 拟合偏移）。``available`` 含目标期时（样本内口径）目标期的差值会进入拟合 ——
    这正是「尽可能贴近开奖号」的泄漏来源，样本外则不含。
    """
    if target_index < 1 or target_index >= len(available) + 1:
        return {}, {"offset": 0, "half_width": 0, "coverage": None, "samples": 0}
    history = [int(value) for value in available[: target_index + 1]]
    if len(history) < 2:
        return {}, {"offset": 0, "half_width": 0, "coverage": None, "samples": 0}
    latest = int(available[target_index - 1])
    band = fit_lattice_band(history, float(coverage))
    offset = int(band["offset"])
    half_width = int(band["half_width"])
    centre = _clamp_number(latest + offset)
    weights = {
        number: 1.0 / (1.0 + max(0, abs(number - centre) - half_width) / LATTICE_DECAY_SCALE)
        for number in NUMBERS
    }
    return weights, {
        "offset": offset,
        "half_width": half_width,
        "centre": centre,
        "latest": latest,
        "coverage_target": float(coverage),
        "fitted_coverage": band["fitted_coverage"],
        "samples": band["samples"],
    }


def lattice_picks_from_band(
    latest: int, offset: int, half_width: int, k: int
) -> list[int]:
    """点阵带的 top-k：带内 + 离中心最近的号（确定性，用于内层选参）。"""
    centre = _clamp_number(int(latest) + int(offset))
    ordered = sorted(NUMBERS, key=lambda number: (abs(number - centre), number))
    return ordered[: max(1, int(k))]


# --------------------------------------------------------------------------- #
# 主预测入口
# --------------------------------------------------------------------------- #
def predict(
    model: str,
    available: Sequence[int],
    target_index: int,
    k: int = K_DEFAULT,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """对 ``target_index`` 期给出完整排序 / 概率 / top-k。

    ``available`` 是模型**唯一**可见的数据；调用方决定它是严格样本外前缀
    （``series[:target_index]``）还是样本内前缀（``series[:target_index + 1]``）。
    ``params`` 为 ``fit_hyperparameters`` 拟合出的超参数（缺省时用内置默认）。
    """
    sequence = [int(value) for value in available]
    index = int(target_index)
    k = max(1, min(NUM_STATES, int(k)))
    fitted = dict(params or {})
    spec = model_spec(model)
    details: dict[str, Any] = {}

    if model == MODEL_MEMORIZE:
        weights, details = _memorize_probabilities(sequence, index)
        free_parameters = len(sequence)
    elif model == MODEL_UNIFORM:
        weights = {}
        free_parameters = 0
        details = {"uniform": True}
    elif model == MODEL_MARKOV_1:
        probabilities = ngram_probabilities(sequence, index, 1, 1.0)
        weights = probabilities
        free_parameters = int(spec["free_parameters"])
        details = {"order": 1, "alpha": 1.0}
    elif model == MODEL_MARKOV_2:
        probabilities = ngram_probabilities(sequence, index, 2, 1.0)
        weights = probabilities
        free_parameters = int(spec["free_parameters"])
        details = {"order": 2, "alpha": 1.0}
    elif model == MODEL_NGRAM_SMOOTHED:
        order = int(fitted.get("order", NGRAM_ORDER_CHAIN))
        alpha = float(fitted.get("alpha", 1.0))
        weights = ngram_probabilities(sequence, index, order, alpha)
        free_parameters = int(spec["free_parameters"])
        details = {"order": order, "alpha": alpha, "order_chain": "3→2→1→均匀"}
    elif model == MODEL_REGULARIZED_BEST_FIT:
        order = int(fitted.get("order", 1))
        alpha = float(fitted.get("alpha", 1.0))
        weights = ngram_probabilities(sequence, index, order, alpha)
        free_parameters = int(spec["free_parameters"])
        details = {"order": order, "alpha": alpha, "regularisation": alpha}
    elif model == MODEL_LATTICE_FIT:
        coverage = float(fitted.get("coverage", 0.75))
        weights, details = _lattice_fit_probabilities(sequence, index, coverage, k)
        free_parameters = int(spec["free_parameters"])
    else:
        raise ValueError(f"未知模型：{model}")

    probabilities = probabilities_from_weights(weights)
    ranking = ranking_from_probabilities(probabilities, tie_break=uniform_ranking(index))
    ranks = {number: position + 1 for position, number in enumerate(ranking)}
    picks = ranking[:k]
    return {
        "model": model,
        "model_label": MODEL_LABELS[model],
        "family": spec["family"],
        "fit_role": FIT_ROLE,
        "fit_role_label": FIT_ROLE_LABEL,
        "free_parameters": int(free_parameters),
        "available_length": len(sequence),
        "target_index": index,
        "k": k,
        "probabilities": probabilities,
        "ranking": ranking,
        "ranks": ranks,
        "picks": picks,
        "parameters": details,
        "notes": [
            _FIT_ONLY_NOTE,
            f"目标期为第 {index} 个位置；模型只使用调用方给出的 {len(sequence)} 期数据。",
        ],
        "disclaimer": DISCLAIMER,
    }


def predict_walk_forward(
    model: str,
    series: Sequence[int],
    target_index: int,
    k: int = K_DEFAULT,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """严格样本外：只把 ``series[:target_index]`` 交给模型（目标期从不进入拟合）。"""
    return predict(model, list(series[: int(target_index)]), target_index, k, params)


def predict_in_sample(
    model: str,
    series: Sequence[int],
    target_index: int,
    k: int = K_DEFAULT,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """样本内「反推」口径：把 ``series[:target_index + 1]`` 交给模型（含答案本身）。"""
    return predict(model, list(series[: int(target_index) + 1]), target_index, k, params)


# --------------------------------------------------------------------------- #
# 内层 walk-forward 选参（只在训练窗内做，评估窗从不参与）
# --------------------------------------------------------------------------- #
def _inner_holdout_count(length: int) -> int:
    return max(3, min(MAX_INNER_HOLDOUT, length // 5))


def _inner_score_ngram(
    train: Sequence[int], order: int, alpha: float, k: int, holdout: int
) -> tuple[int, float, int]:
    """内层准则：命中数、平均排名（越小越好）、样本数。只用 ``train`` 之前的数据。"""
    length = len(train)
    start = max(max(1, int(order)), length - holdout)
    hits = 0
    rank_sum = 0.0
    count = 0
    for position in range(start, length):
        probabilities = ngram_probabilities(train[:position], position, order, alpha)
        ranking = ranking_from_probabilities(probabilities, tie_break=uniform_ranking(position))
        actual = int(train[position])
        rank = ranking.index(actual) + 1
        rank_sum += rank
        if rank <= k:
            hits += 1
        count += 1
    return hits, rank_sum, count


def _inner_score_lattice(
    train: Sequence[int], coverage: float, k: int, holdout: int
) -> tuple[int, float, int]:
    length = len(train)
    start = max(2, length - holdout)
    hits = 0
    rank_sum = 0.0
    count = 0
    for position in range(start, length):
        weights, _ = _lattice_fit_probabilities(train[:position], position, coverage, k)
        probabilities = probabilities_from_weights(weights)
        ranking = ranking_from_probabilities(probabilities, tie_break=uniform_ranking(position))
        actual = int(train[position])
        rank = ranking.index(actual) + 1
        rank_sum += rank
        if rank <= k:
            hits += 1
        count += 1
    return hits, rank_sum, count


def fit_hyperparameters(
    series: Sequence[int],
    k: int = K_DEFAULT,
    *,
    train_ratio: float = DEFAULT_TRAIN_RATIO,
) -> dict[str, Any]:
    """在**训练窗内**用内层 walk-forward 选超参数（评估窗从不参与）。

    训练窗 = ``series[:split_index]``（``split_index = round(n · train_ratio)``）。
    返回每个模型的冻结超参数 + 选中依据（内层命中率 / 平均排名）与切分信息。
    """
    sequence = [int(value) for value in series]
    length = len(sequence)
    split_index = max(3, min(length, int(round(length * float(train_ratio)))))
    train = sequence[:split_index]
    holdout = _inner_holdout_count(len(train))
    result: dict[str, Any] = {
        "fit_role": FIT_ROLE,
        "train_ratio": float(train_ratio),
        "train_length": len(train),
        "holdout": holdout,
        "split_index": split_index,
        "notes": [
            _FIT_ONLY_NOTE,
            "超参数只在训练窗内用内层 walk-forward 选，评估窗从不参与选参。",
        ],
        "models": {},
    }
    if len(train) < 5:
        result["data_status"] = "INSUFFICIENT"
        result["data_status_label"] = "数据不足"
        result["models"] = {model: {} for model in MODEL_IDS}
        return result

    # NGRAM_SMOOTHED：阶数固定 3，拟合平滑强度 α
    best_ngram: dict[str, Any] | None = None
    for alpha in NGRAM_ALPHA_GRID:
        hits, rank_sum, count = _inner_score_ngram(
            train, NGRAM_ORDER_CHAIN, alpha, k, holdout
        )
        if count == 0:
            continue
        key = (hits / count, -(rank_sum / count), -abs(alpha - 1.0))
        if best_ngram is None or key > best_ngram["_key"]:
            best_ngram = {
                "_key": key,
                "params": {"order": NGRAM_ORDER_CHAIN, "alpha": float(alpha)},
                "inner_hit_rate": hits / count,
                "inner_mean_rank": rank_sum / count,
                "inner_evaluated": count,
            }
    result["models"][MODEL_NGRAM_SMOOTHED] = best_ngram or {}

    # REGULARIZED_BEST_FIT：阶数 × 平滑强度 α 一起选
    best_reg: dict[str, Any] | None = None
    for order in REG_ORDER_GRID:
        for alpha in REG_ALPHA_GRID:
            hits, rank_sum, count = _inner_score_ngram(train, order, alpha, k, holdout)
            if count == 0:
                continue
            # 平手偏好更大 α（更强正则化）与更小阶数（更简单）
            key = (hits / count, -(rank_sum / count), float(alpha), -int(order))
            if best_reg is None or key > best_reg["_key"]:
                best_reg = {
                    "_key": key,
                    "params": {"order": int(order), "alpha": float(alpha)},
                    "inner_hit_rate": hits / count,
                    "inner_mean_rank": rank_sum / count,
                    "inner_evaluated": count,
                }
    result["models"][MODEL_REGULARIZED_BEST_FIT] = best_reg or {}

    # LATTICE_FIT：拟合覆盖分位
    best_lattice: dict[str, Any] | None = None
    for coverage in LATTICE_COVERAGE_GRID:
        hits, rank_sum, count = _inner_score_lattice(train, coverage, k, holdout)
        if count == 0:
            continue
        # 平手偏好更窄的带（更小覆盖分位 = 更选择性）
        key = (hits / count, -(rank_sum / count), -float(coverage))
        if best_lattice is None or key > best_lattice["_key"]:
            best_lattice = {
                "_key": key,
                "params": {"coverage": float(coverage)},
                "inner_hit_rate": hits / count,
                "inner_mean_rank": rank_sum / count,
                "inner_evaluated": count,
            }
    result["models"][MODEL_LATTICE_FIT] = best_lattice or {}

    # 无超参数的模型（阶数 / α 固定 / 均匀基线）
    for model in (MODEL_UNIFORM, MODEL_MEMORIZE, MODEL_MARKOV_1, MODEL_MARKOV_2):
        result["models"][model] = {
            "params": {},
            "inner_hit_rate": None,
            "inner_mean_rank": None,
            "inner_evaluated": 0,
            "note": "无超参数需要拟合（模型结构固定）",
        }

    for model in MODEL_IDS:
        entry = result["models"].get(model, {})
        entry.pop("_key", None)
        result["models"][model] = entry
    return result


# --------------------------------------------------------------------------- #
# 评估（样本内 / 样本外）
# --------------------------------------------------------------------------- #
def evaluate(
    model: str,
    series: Sequence[int],
    k: int = K_DEFAULT,
    *,
    mode: str = "walk_forward",
    params: Mapping[str, Any] | None = None,
    warmup: int = WARMUP,
) -> dict[str, Any]:
    """逐期评估：``mode="walk_forward"`` 严格无未来函数，``"in_sample"`` 允许反推答案。

    返回聚合指标（命中率 / 平均排名 / 排名分布 / log-loss / Brier）与逐期明细。
    """
    if mode not in ("walk_forward", "in_sample"):
        raise ValueError(f"未知评估口径：{mode}")
    sequence = [int(value) for value in series]
    length = len(sequence)
    rows: list[dict[str, Any]] = []
    for index in range(int(warmup), length):
        if mode == "walk_forward":
            prediction = predict_walk_forward(model, sequence, index, k, params)
        else:
            prediction = predict_in_sample(model, sequence, index, k, params)
        actual = sequence[index]
        rank = int(prediction["ranks"][actual])
        probabilities = prediction["probabilities"]
        rows.append(
            {
                "index": index,
                "actual": actual,
                "rank": rank,
                "hit": rank <= int(k),
                "log_loss": log_loss(probabilities, actual),
                "brier": brier_score(probabilities, actual),
                "picks": list(prediction["picks"]),
            }
        )

    evaluated = len(rows)
    if evaluated == 0:
        return {
            "model": model,
            "mode": mode,
            "fit_role": FIT_ROLE,
            "evaluated": 0,
            "hit_rate": None,
            "hits": 0,
            "mean_rank": None,
            "rank_buckets": None,
            "log_loss": None,
            "brier": None,
            "data_status": "INSUFFICIENT",
            "data_status_label": "数据不足",
            "rows": rows,
            "notes": [_FIT_ONLY_NOTE],
            "disclaimer": DISCLAIMER,
        }
    hits = sum(1 for row in rows if row["hit"])
    ranks = [row["rank"] for row in rows]
    return {
        "model": model,
        "mode": mode,
        "fit_role": FIT_ROLE,
        "evaluated": evaluated,
        "hits": hits,
        "hit_rate": hits / evaluated,
        "mean_rank": sum(ranks) / evaluated,
        "rank_buckets": rank_bucket_shares(ranks, k),
        "log_loss": sum(row["log_loss"] for row in rows) / evaluated,
        "brier": sum(row["brier"] for row in rows) / evaluated,
        "reference": {
            "hit_rate": int(k) / NUM_STATES,
            "mean_rank": UNIFORM_MEAN_RANK,
            "log_loss": UNIFORM_LOG_LOSS,
            "brier": UNIFORM_BRIER,
        },
        "rows": rows,
        "notes": [_FIT_ONLY_NOTE],
        "disclaimer": DISCLAIMER,
    }
