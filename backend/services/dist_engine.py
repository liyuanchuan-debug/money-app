r"""号码分布预测引擎 —— 输出 1..49 的**完整概率向量**，由可开关的信号组件合成。

本模块正面实现使用者的诉求：「做一个预测号码分布的选号算法」。它不是一个排序黑箱，
而是 8 个**可单独开关、可单独评估**的信号组件的加权对数线性池：

1. ``FREQUENCY`` —— 长窗 / 短窗出现频次（热冷）；
2. ``GAP`` —— 遗漏期数（含「到期号」变体：权重符号决定偏冷还是偏热）；
3. ``ZODIAC`` —— 生肖轮转位置（组内近期频次 + 轮转步长推断的下一个生肖）；
4. ``WAVE`` —— 波动 / 点阵连续性，**复用** ``services.lottery`` 的波动带与点阵权重；
5. ``MARKOV`` —— 一阶转移（上一期特码 → 本期）；
6. ``STRUCTURE`` —— 结构约束（大小 / 奇偶 / 尾数 / 和值尾数 / 跨度波动）；
7. ``REPEAT`` —— 重号与重肖效应；
8. ``PERIODIC`` —— 周期扫描（自相关最强的滞后）。

口径铁律（违反即为缺陷）：

- **只做样本内诚实评估，禁止进入线上推荐路径**：``services.lottery``、``routers/*``、
  ``main.py`` 一律不得 import / 引用本模块（有回归测试守着）。本模块只读开奖序列，
  不读写任何数据库，也不修改任何既有设置。
- 所有结论只针对「本池已导入的 N 期数据」这个样本；**禁止写「全市场」**。
- 每个返回值都带英文诚实标记 ``claim = "NO_EDGE"`` 与中文 ``disclaimer``：本引擎的
  样本外表现与均匀随机基线（命中率 10/49、平均排名 25、log-loss ln 49、
  Brier (1/49)(1-1/49)）不可区分 —— 它交付的是**透明可控的分布**，不是概率优势。
- 四个尺子（命中率 / 平均排名 / log-loss / Brier）与均匀参考值**复用**
  ``services.max_fit`` 的实现，保证与仓库既有口径逐位一致；小样本功效工具**复用**
  ``services.analytics``（``minimum_detectable_delta`` / ``holm_adjusted_p`` 等）。
- 落库 / 传输的枚举一律英文码（组件名、``claim``、``data_status``），中文只出现在
  展示标签（``*_label``）与提示文案里。

严格 walk-forward 契约：``predict`` 只能看见 ``numbers[:target_index]``（严格早于目标
期）；``fit_weights`` 只在训练窗内层选权，评估窗从不参与选参。两个函数都不读全序列
以外的任何数据。
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from services.analytics import (
    DATA_STATUS_INSUFFICIENT,
    DATA_STATUS_LABELS,
    DATA_STATUS_OK,
    holm_adjusted_p,
    minimum_detectable_delta,
)
from services.lottery import (
    DEFAULT_SETTINGS,
    WAVE_LABELS,
    WAVE_ORDER,
    classify_wave,
    clamp_settings,
    derive_big_min,
    lattice_weight,
    predict_wave_band,
    zodiac_group,
)
from services.max_fit import (
    K_DEFAULT,
    NUMBER_MAX,
    NUMBER_MIN,
    NUMBERS,
    NUM_STATES,
    ODDS_DEFAULT,
    PROBABILITY_FLOOR,
    UNIFORM_BRIER,
    UNIFORM_LOG_LOSS,
    UNIFORM_MEAN_RANK,
    UNIFORM_PROB,
    WARMUP,
    brier_score,
    log_loss,
    mean_rank,
    rank_bucket_shares,
    ranking_from_probabilities,
    uniform_ranking,
)

# --------------------------------------------------------------------------- #
# 常量与英文枚举
# --------------------------------------------------------------------------- #
ENGINE_VERSION = 1

# 诚实标记：与 services.pick_ticket 的「不改变期望值」同源
CLAIM_NO_EDGE = "NO_EDGE"
CLAIM_LABELS: dict[str, str] = {
    CLAIM_NO_EDGE: "本池样本内无可利用优势（与均匀随机不可区分）",
}

# 组件 id（英文枚举；中文只进 label）
COMPONENT_FREQUENCY = "FREQUENCY"
COMPONENT_GAP = "GAP"
COMPONENT_ZODIAC = "ZODIAC"
COMPONENT_WAVE = "WAVE"
COMPONENT_MARKOV = "MARKOV"
COMPONENT_STRUCTURE = "STRUCTURE"
COMPONENT_REPEAT = "REPEAT"
COMPONENT_PERIODIC = "PERIODIC"

COMPONENT_IDS: tuple[str, ...] = (
    COMPONENT_FREQUENCY,
    COMPONENT_GAP,
    COMPONENT_ZODIAC,
    COMPONENT_WAVE,
    COMPONENT_MARKOV,
    COMPONENT_STRUCTURE,
    COMPONENT_REPEAT,
    COMPONENT_PERIODIC,
)

COMPONENT_LABELS: dict[str, str] = {
    COMPONENT_FREQUENCY: "长窗 / 短窗频次（热冷）",
    COMPONENT_GAP: "遗漏期数（到期号）",
    COMPONENT_ZODIAC: "生肖轮转位置",
    COMPONENT_WAVE: "波动 / 点阵连续性",
    COMPONENT_MARKOV: "一阶马尔可夫转移",
    COMPONENT_STRUCTURE: "结构约束（大小 / 奇偶 / 尾数）",
    COMPONENT_REPEAT: "重号 / 重肖效应",
    COMPONENT_PERIODIC: "周期扫描（自相关最强滞后）",
}

COMPONENT_NOTES: dict[str, str] = {
    COMPONENT_FREQUENCY: "近 long 窗与短窗的出现率平滑估计；权重为正偏热、为负偏冷。",
    COMPONENT_GAP: "距最近一次出现的期数（未出现 = 全窗）；权重为正偏到期号，为负偏近号。",
    COMPONENT_ZODIAC: "同肖组近期频次 + 由组号轮转步长推断的下一个生肖。",
    COMPONENT_WAVE: "复用 predict_wave_band / lattice_weight：带内权重高，带外按距离衰减。",
    COMPONENT_MARKOV: "上一期特码到本期的转移计数，向全历史频次回退平滑。",
    COMPONENT_STRUCTURE: "大小 / 奇偶 / 尾数 / 和值尾数 / 跨度波动的近期经验率相乘。",
    COMPONENT_REPEAT: "重号与重肖的经验提升倍数，外加近窗出现次数。",
    COMPONENT_PERIODIC: "扫描周期 p 的历史同滞后一致率，取最优滞后的号码命中计数。",
}

# 权重搜索网格与内层 walk-forward 默认值（确定性、可复现）
WEIGHT_GRID: tuple[float, ...] = (-1.0, -0.5, 0.0, 0.5, 1.0, 2.0)
DEFAULT_TRAIN_RATIO = 0.7
DEFAULT_INNER_HOLDOUT = 30
DEFAULT_MAX_PASSES = 2

DEFAULT_PARAMS: dict[str, Any] = {
    "freq_long": 100,
    "freq_short": 20,
    "zodiac_window": 24,
    "zodiac_rotation_weight": 0.5,
    "lattice_window": int(DEFAULT_SETTINGS["lattice_window"]),
    "small_max": int(DEFAULT_SETTINGS["small_max"]),
    "normal_max": int(DEFAULT_SETTINGS["normal_max"]),
    "markov_alpha": 1.0,
    "structure_window": 60,
    "repeat_window": 5,
    "period_max": 30,
}

DISCLAIMER = (
    "这是本池样本内的分布演示，不是预测：本引擎的样本外表现与均匀随机基线"
    "（命中率 10/49 = 20.4082%、平均排名 25、log-loss ln 49 = 3.89182、"
    "Brier (1/49)(1-1/49) = 0.019992）不可区分。它交付的是透明、可开关、可复现的"
    "号码分布控制，不是中奖概率优势；禁止用于投注决策。"
)

_ENGINE_NOTE = (
    "口径：本池已导入 N 期样本内；每个组件可单独开关与单独评估，"
    "禁止进入线上推荐路径，样本外表现即均匀基线。"
)

_EPS = 1e-12


# --------------------------------------------------------------------------- #
# 基础工具（纯函数）
# --------------------------------------------------------------------------- #
def resolve_params(params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """把组件超参数合并到默认值上（未知键忽略，已知键类型规范化）。"""
    resolved = dict(DEFAULT_PARAMS)
    for key, value in (params or {}).items():
        if key in resolved and value is not None:
            resolved[key] = value
    return resolved


def _clamp_number(value: int) -> int:
    return min(NUMBER_MAX, max(NUMBER_MIN, int(value)))


def _centered_log(raw: Mapping[int, float]) -> dict[int, float]:
    """把正权重向量抬成「均值为 0 的对数权重」，均匀向量映射为全 0。"""
    logs = {number: math.log(float(raw.get(number, 0.0)) + _EPS) for number in NUMBERS}
    mean = sum(logs.values()) / NUM_STATES
    return {number: logs[number] - mean for number in NUMBERS}


def _zero_logweights() -> dict[int, float]:
    return {number: 0.0 for number in NUMBERS}


def _recent(history: Sequence[int], window: int) -> list[int]:
    if window and window > 0:
        return list(history[-int(window) :])
    return list(history)


def _context(
    numbers: Sequence[int], target_index: int, params: Mapping[str, Any]
) -> dict[str, Any]:
    """组装组件上下文：只含 ``numbers[:target_index]``（严格早于目标期）。"""
    history = [int(value) for value in numbers[: int(target_index)]]
    return {
        "history": history,
        "length": len(history),
        "latest": history[-1] if history else None,
        "params": params,
    }


def pool_probabilities(
    logweights: Mapping[str, Mapping[int, float]],
    weights: Mapping[str, float],
    *,
    floor: float = PROBABILITY_FLOOR,
) -> dict[int, float]:
    """加权对数线性池 → 1..49 的合法分布（处处为正、和恰为 1）。

    ``log q(n) = Σ_c weights[c] · logweights[c][n]``，再 softmax。
    所有组件权重为 0 时退化为严格均匀分布（这是本引擎的默认状态）。
    """
    totals = {number: 0.0 for number in NUMBERS}
    for name, table in logweights.items():
        weight = float(weights.get(name, 0.0))
        if weight == 0.0:
            continue
        for number in NUMBERS:
            totals[number] += weight * float(table.get(number, 0.0))
    peak = max(totals.values())
    exps = {number: math.exp(totals[number] - peak) for number in NUMBERS}
    total = sum(exps.values())
    safe_floor = max(0.0, min(1.0, float(floor)))
    return {
        number: (1.0 - safe_floor) * exps[number] / total + safe_floor * UNIFORM_PROB
        for number in NUMBERS
    }


def order_from_probabilities(
    probabilities: Mapping[int, float], target_index: int
) -> list[int]:
    """概率降序的 1..49 完整排序；并列时用 ``max_fit.uniform_ranking`` 打散。"""
    return ranking_from_probabilities(
        probabilities, tie_break=uniform_ranking(int(target_index))
    )


def reference_block() -> dict[str, float]:
    """均匀参考值（与仓库其它分析脚本同口径）。"""
    return {
        "hit_rate": K_DEFAULT / NUM_STATES,
        "mean_rank": UNIFORM_MEAN_RANK,
        "log_loss": UNIFORM_LOG_LOSS,
        "brier": UNIFORM_BRIER,
    }


# --------------------------------------------------------------------------- #
# 组件：每个返回 1..49 的「居中对数权重」（均匀 = 全 0）
# --------------------------------------------------------------------------- #
def component_frequency(ctx: Mapping[str, Any]) -> dict[int, float]:
    """长窗 / 短窗出现频次（加一平滑的出现率之和）。"""
    history = ctx["history"]
    if not history:
        return _zero_logweights()
    params = ctx["params"]
    long_window = _recent(history, int(params["freq_long"]))
    short_window = _recent(history, int(params["freq_short"]))
    long_counts = Counter(long_window)
    short_counts = Counter(short_window)
    long_total = len(long_window) + NUM_STATES
    short_total = len(short_window) + NUM_STATES
    raw = {
        number: (long_counts.get(number, 0) + 1) / long_total
        + (short_counts.get(number, 0) + 1) / short_total
        for number in NUMBERS
    }
    return _centered_log(raw)


def component_gap(ctx: Mapping[str, Any]) -> dict[int, float]:
    """遗漏期数：距最近一次出现的期数（样本内未出现按全窗计）。

    权重符号决定口径：正值偏「到期号」（gambler's fallacy 口径），
    负值偏「近号」。两者都由内层 walk-forward 在同一份数据上裁定。
    """
    history = ctx["history"]
    if not history:
        return _zero_logweights()
    length = len(history)
    last_seen: dict[int, int] = {}
    for offset, number in enumerate(reversed(history)):
        if number not in last_seen:
            last_seen[number] = offset
    raw = {number: float(last_seen.get(number, length) + 1) for number in NUMBERS}
    return _centered_log(raw)


def component_zodiac(ctx: Mapping[str, Any]) -> dict[int, float]:
    """生肖轮转位置：同肖组近期频次 + 轮转步长推断的下一个生肖。"""
    history = ctx["history"]
    if not history:
        return _zero_logweights()
    params = ctx["params"]
    window = _recent(history, int(params["zodiac_window"]))
    if not window:
        return _zero_logweights()
    groups = [zodiac_group(number) for number in window]
    group_counts = Counter(groups)
    steps = [
        (groups[index] - groups[index - 1]) % 12 for index in range(1, len(groups))
    ]
    raw = {number: float(group_counts.get(zodiac_group(number), 0) + 1) for number in NUMBERS}
    if steps:
        step_counts = Counter(steps)
        modal_step = max(
            range(12), key=lambda step: (step_counts.get(step, 0), -step)
        )
        next_group = (groups[-1] + modal_step) % 12
        bonus = 1.0 + float(params["zodiac_rotation_weight"]) * len(steps)
        for number in NUMBERS:
            if zodiac_group(number) == next_group:
                raw[number] += bonus
    return _centered_log(raw)


def component_wave(ctx: Mapping[str, Any]) -> dict[int, float]:
    """波动 / 点阵连续性：复用生产引擎的波动带与点阵衰减，再乘近窗差值密度。"""
    history = ctx["history"]
    if len(history) < 2:
        return _zero_logweights()
    params = ctx["params"]
    latest = int(ctx["latest"])
    small_max = int(params["small_max"])
    normal_max = int(params["normal_max"])
    band = predict_wave_band(
        list(reversed(history)),
        window=int(params["lattice_window"]),
        small_max=small_max,
        normal_max=normal_max,
    )
    recent = _recent(history, int(params["lattice_window"]))
    diffs = [abs(recent[index] - recent[index - 1]) for index in range(1, len(recent))]
    diff_counts = Counter(diffs)
    diff_total = len(diffs) + 1
    raw: dict[int, float] = {}
    for number in NUMBERS:
        diff = abs(number - latest)
        density = (diff_counts.get(diff, 0) + 1) / diff_total
        raw[number] = lattice_weight(diff, band) * density
    return _centered_log(raw)


def component_markov(ctx: Mapping[str, Any]) -> dict[int, float]:
    """一阶转移：上一期特码 → 本期，向全历史频次回退平滑。"""
    history = ctx["history"]
    if len(history) < 2:
        return _zero_logweights()
    params = ctx["params"]
    alpha = float(params["markov_alpha"])
    latest = int(ctx["latest"])
    row: Counter[int] = Counter()
    overall: Counter[int] = Counter()
    for index in range(1, len(history)):
        overall[history[index]] += 1
        if history[index - 1] == latest:
            row[history[index]] += 1
    row_total = sum(row.values())
    overall_total = sum(overall.values()) or 1
    raw = {
        number: (
            row.get(number, 0) + alpha * (overall.get(number, 0) / overall_total)
        )
        / (row_total + alpha)
        for number in NUMBERS
    }
    return _centered_log(raw)


def component_structure(ctx: Mapping[str, Any]) -> dict[int, float]:
    """结构约束：大小 / 奇偶 / 尾数 / 和值尾数 / 跨度波动的近期经验率相乘。

    注意本池数据只含**特码**（没有 6 个正码），因此「和值」用相邻两期特码之和的
    尾数代替，并在返回值 ``notes`` 里如实说明，不冒充完整和值。
    """
    history = ctx["history"]
    if not history:
        return _zero_logweights()
    params = ctx["params"]
    window = _recent(history, int(params["structure_window"]))
    if not window:
        return _zero_logweights()
    small_max = int(params["small_max"])
    normal_max = int(params["normal_max"])
    big_min = derive_big_min(normal_max)
    latest = int(ctx["latest"])
    size = len(window)

    tail_counts = Counter(number % 10 for number in window)
    parity_counts = Counter(number % 2 for number in window)
    big_counts = Counter(1 if number >= big_min else 0 for number in window)
    span_counts = Counter(
        classify_wave(abs(window[index] - window[index - 1]), small_max, normal_max)
        for index in range(1, len(window))
    )
    span_total = sum(span_counts.values())
    split_tail = Counter(
        (window[index] + window[index - 1]) % 10 for index in range(1, len(window))
    )
    split_total = sum(split_tail.values())

    raw: dict[int, float] = {}
    for number in NUMBERS:
        tail_rate = (tail_counts.get(number % 10, 0) + 1) / (size + 10)
        parity_rate = (parity_counts.get(number % 2, 0) + 1) / (size + 2)
        size_rate = (big_counts.get(1 if number >= big_min else 0, 0) + 1) / (size + 2)
        diff = abs(number - latest)
        wave = classify_wave(diff, small_max, normal_max)
        span_rate = (
            (span_counts.get(wave, 0) + 1) / (span_total + len(WAVE_ORDER))
            if span_total
            else 1.0
        )
        split_rate = (
            (split_tail.get((number + latest) % 10, 0) + 1) / (split_total + 10)
            if split_total
            else 1.0
        )
        raw[number] = tail_rate * parity_rate * size_rate * span_rate * split_rate
    return _centered_log(raw)


def component_repeat(ctx: Mapping[str, Any]) -> dict[int, float]:
    """重号 / 重肖效应：经验提升倍数 + 近窗出现次数。"""
    history = ctx["history"]
    if len(history) < 2:
        return _zero_logweights()
    params = ctx["params"]
    latest = int(ctx["latest"])
    repeat_number_hits = sum(
        1 for index in range(1, len(history)) if history[index] == history[index - 1]
    )
    repeat_zodiac_hits = sum(
        1
        for index in range(1, len(history))
        if history[index] != history[index - 1]
        and zodiac_group(history[index]) == zodiac_group(history[index - 1])
    )
    pairs = len(history) - 1
    # 经验提升相对于均匀率 1/49 与同肖率（约 4/49，生肖分组数为 12 时非均分）
    number_lift = max(0.0, (repeat_number_hits / pairs) / UNIFORM_PROB - 1.0)
    zodiac_uniform = 4.0 / NUM_STATES
    zodiac_lift = max(0.0, (repeat_zodiac_hits / pairs) / zodiac_uniform - 1.0)
    recent = _recent(history, int(params["repeat_window"]))
    recent_counts = Counter(recent)
    recent_total = len(recent) or 1
    raw: dict[int, float] = {}
    for number in NUMBERS:
        value = 1.0 + recent_counts.get(number, 0) / recent_total
        if number == latest:
            value += number_lift
        elif zodiac_group(number) == zodiac_group(latest):
            value += zodiac_lift
        raw[number] = value
    return _centered_log(raw)


def component_periodic(ctx: Mapping[str, Any]) -> dict[int, float]:
    """周期扫描：取历史同滞后一致率最高的周期，给该周期相位的号码加分。"""
    history = ctx["history"]
    if len(history) < 6:
        return _zero_logweights()
    params = ctx["params"]
    period_max = max(2, min(int(params["period_max"]), len(history) // 2))
    best_period = None
    best_score = -1.0
    for period in range(2, period_max + 1):
        matches = sum(
            1
            for index in range(period, len(history))
            if history[index] == history[index - period]
        )
        score = matches / (len(history) - period)
        if score > best_score + 1e-12:
            best_score = score
            best_period = period
    if best_period is None:
        return _zero_logweights()
    counts = Counter(
        history[-(offset + 1)]
        for offset in range(0, len(history), best_period)
        if len(history) - 1 - offset >= 0
    )
    raw = {number: float(counts.get(number, 0) + 1) for number in NUMBERS}
    return _centered_log(raw)


_COMPONENT_FUNCS = {
    COMPONENT_FREQUENCY: component_frequency,
    COMPONENT_GAP: component_gap,
    COMPONENT_ZODIAC: component_zodiac,
    COMPONENT_WAVE: component_wave,
    COMPONENT_MARKOV: component_markov,
    COMPONENT_STRUCTURE: component_structure,
    COMPONENT_REPEAT: component_repeat,
    COMPONENT_PERIODIC: component_periodic,
}


def component_weights(name: str, ctx: Mapping[str, Any]) -> dict[int, float]:
    """按组件 id 派发到具体实现（未知 id 抛错，不用空表冒充）。"""
    if name not in _COMPONENT_FUNCS:
        raise ValueError(f"未知组件：{name}")
    return _COMPONENT_FUNCS[name](ctx)


def component_logweights_at(
    numbers: Sequence[int],
    target_index: int,
    components: Sequence[str],
    params: Mapping[str, Any] | None = None,
) -> dict[str, dict[int, float]]:
    """对 ``target_index`` 期算出指定组件的居中对数权重（只看严格过去）。"""
    resolved = resolve_params(params)
    ctx = _context(numbers, target_index, resolved)
    return {name: component_weights(name, ctx) for name in components}


def component_topk(
    numbers: Sequence[int],
    target_index: int,
    component: str,
    *,
    k: int = K_DEFAULT,
    params: Mapping[str, Any] | None = None,
) -> list[int]:
    """单个组件单独成分布时的 top-k（供同组件单独评估 / 审计用）。"""
    logweights = component_logweights_at(numbers, target_index, (component,), params)
    probabilities = pool_probabilities(logweights, {component: 1.0})
    return order_from_probabilities(probabilities, target_index)[: max(1, int(k))]


# --------------------------------------------------------------------------- #
# 预测入口
# --------------------------------------------------------------------------- #
def predict(
    numbers: Sequence[int],
    target_index: int,
    *,
    weights: Mapping[str, float] | None = None,
    params: Mapping[str, Any] | None = None,
    k: int = K_DEFAULT,
    components: Sequence[str] | None = None,
) -> dict[str, Any]:
    """对 ``target_index`` 期给出完整分布 / 排序 / top-k。

    ``numbers`` 是升序开奖序列。``weights`` 缺省为「全 0」= 严格均匀分布。
    ``components`` 缺省为全部 8 个组件；权重为 0 的组件不会被计算。
    """
    index = int(target_index)
    if index < 0 or index > len(numbers):
        raise ValueError(f"target_index 越界：{index}（序列长度 {len(numbers)}）")
    resolved = resolve_params(params)
    active = tuple(components if components is not None else COMPONENT_IDS)
    effective_weights = {
        name: float((weights or {}).get(name, 0.0)) for name in COMPONENT_IDS
    }
    used = tuple(
        name for name in active if effective_weights.get(name, 0.0) != 0.0
    )
    logweights = (
        component_logweights_at(numbers, index, used, resolved) if used else {}
    )
    probabilities = pool_probabilities(logweights, effective_weights)
    ranking = order_from_probabilities(probabilities, index)
    ranks = {number: position + 1 for position, number in enumerate(ranking)}
    return {
        "engine_version": ENGINE_VERSION,
        "claim": CLAIM_NO_EDGE,
        "claim_label": CLAIM_LABELS[CLAIM_NO_EDGE],
        "target_index": index,
        "available_length": index,
        "k": max(1, min(NUM_STATES, int(k))),
        "components_used": list(used),
        "weights": effective_weights,
        "probabilities": probabilities,
        "ranking": ranking,
        "ranks": ranks,
        "picks": ranking[: max(1, min(NUM_STATES, int(k)))],
        "notes": [
            _ENGINE_NOTE,
            f"目标期为序列第 {index} 个位置；引擎只使用前 {index} 期数据。",
            f"实际参与合成的组件：{list(used) or ['（无，退化为均匀分布）']}",
        ],
        "disclaimer": DISCLAIMER,
    }


def predict_walk_forward(
    numbers: Sequence[int],
    target_index: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """严格样本外：只把 ``numbers[:target_index]`` 交给引擎。"""
    return predict(numbers, target_index, **kwargs)


# --------------------------------------------------------------------------- #
# 评分
# --------------------------------------------------------------------------- #
def _score_rows(rows: Sequence[Mapping[str, Any]], k: int) -> dict[str, Any]:
    evaluated = len(rows)
    if evaluated == 0:
        return {
            "evaluated": 0,
            "hits": 0,
            "hit_rate": None,
            "mean_rank": None,
            "rank_buckets": None,
            "log_loss": None,
            "brier": None,
            "reference": reference_block(),
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    hits = sum(1 for row in rows if row["rank"] <= int(k))
    ranks = [int(row["rank"]) for row in rows]
    return {
        "evaluated": evaluated,
        "hits": hits,
        "hit_rate": hits / evaluated,
        "mean_rank": mean_rank(ranks),
        "rank_buckets": rank_bucket_shares(ranks, int(k)),
        "log_loss": sum(float(row["log_loss"]) for row in rows) / evaluated,
        "brier": sum(float(row["brier"]) for row in rows) / evaluated,
        "reference": reference_block(),
        "data_status": DATA_STATUS_OK,
        "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
    }


def calibration_block(
    rows: Sequence[Mapping[str, Any]], *, bins: int = 5
) -> dict[str, Any]:
    """校准 / 可靠性：预测分布 vs 观测频率（逐号 + 分箱 + GOF 统计量）。

    - ``per_number``：49 个点 —— 每号在评估窗内的**平均预测概率** vs **观测频率**；
    - ``bins``：把 49 个号按平均预测概率分位分箱，比较箱内预测均值与观测频率；
    - ``ece``：期望校准误差 ``Σ (n_b/49)·|mean_pred_b - observed_b|``；
    - ``chi_square_statistic``：``Σ (观测计数 - 期望计数)^2 / 期望计数``，自由度 48
      （49 类、总数固定）；p 值由审计模块用 ``chi2_sf`` 补上，避免循环依赖。
    - ``mean_predicted``：``(1/N)·Σ p(实际特码)`` —— 预测分布压在真相上的平均质量，
      均匀基线恰为 1/49。

    均匀分布下逐号预测概率恒为 1/49，于是该 GOF 统计量**恰好退化为「号码是否均匀」**
    的卡方统计量 —— 这说明「校准」与「公平性」在本池是同一个检验，不能互相冒充证据。
    """
    evaluated = len(rows)
    if evaluated == 0:
        return {
            "evaluated": 0,
            "mean_predicted": None,
            "reference_mean_predicted": UNIFORM_PROB,
            "ece": None,
            "bins": [],
            "per_number": [],
            "chi_square_statistic": None,
            "degrees_of_freedom": NUM_STATES - 1,
            "notes": ["数据不足：可评估期数为 0，无法做校准检查。"],
        }
    mean_probs = {
        number: sum(float(row["probabilities"][number]) for row in rows) / evaluated
        for number in NUMBERS
    }
    observed_counts = Counter(int(row["actual"]) for row in rows)
    per_number = [
        {
            "number": number,
            "mean_predicted_p": mean_probs[number],
            "observed_rate": observed_counts.get(number, 0) / evaluated,
            "expected_count": mean_probs[number] * evaluated,
            "observed_count": observed_counts.get(number, 0),
        }
        for number in NUMBERS
    ]
    chi_square = sum(
        (row["observed_count"] - row["expected_count"]) ** 2
        / max(row["expected_count"], 1e-12)
        for row in per_number
    )

    ordered = sorted(per_number, key=lambda row: row["mean_predicted_p"])
    bin_count = max(1, min(int(bins), NUM_STATES))
    size = NUM_STATES / bin_count
    edges = [int(round(index * size)) for index in range(bin_count + 1)]
    bin_rows: list[dict[str, Any]] = []
    ece = 0.0
    for index in range(bin_count):
        chunk = ordered[edges[index] : edges[index + 1]]
        if not chunk:
            continue
        count = len(chunk)
        mean_pred = sum(row["mean_predicted_p"] for row in chunk) / count
        observed = sum(row["observed_rate"] for row in chunk) / count
        gap = abs(mean_pred - observed)
        ece += (count / NUM_STATES) * gap
        bin_rows.append(
            {
                "index": index,
                "count": count,
                "mean_predicted_p": mean_pred,
                "observed_rate": observed,
                "abs_gap": gap,
                "expected_count_total": sum(row["expected_count"] for row in chunk),
                "observed_count_total": sum(row["observed_count"] for row in chunk),
            }
        )
    return {
        "evaluated": evaluated,
        "mean_predicted": sum(float(row["p_actual"]) for row in rows) / evaluated,
        "reference_mean_predicted": UNIFORM_PROB,
        "ece": ece,
        "bins": bin_rows,
        "per_number": per_number,
        "chi_square_statistic": chi_square,
        "degrees_of_freedom": NUM_STATES - 1,
        "notes": [
            "校准口径：逐号比较平均预测概率与观测频率，再按预测概率分位分箱；"
            "ece = Σ (n_b/49)·|预测均值 - 观测频率|。",
            "均匀分布下逐号预测概率恒为 1/49，GOF 统计量退化为「号码是否均匀」的卡方；"
            "p 值由 scripts/dist_audit.py 用 chi2_sf 补上（自由度 48）。",
        ],
    }


def evaluate(
    numbers: Sequence[int],
    *,
    weights: Mapping[str, float] | None = None,
    params: Mapping[str, Any] | None = None,
    k: int = K_DEFAULT,
    warmup: int = WARMUP,
    start_index: int | None = None,
    stop_index: int | None = None,
    components: Sequence[str] | None = None,
    mode: str = "walk_forward",
) -> dict[str, Any]:
    """逐期评估：``walk_forward`` 严格无未来函数，``in_sample`` 允许反推答案。

    ``start_index`` / ``stop_index`` 用于只在「留出窗」上打分（权重在训练窗拟合后，
    评估窗只打分）。每期预测只读 ``numbers[:position]``。
    """
    if mode not in ("walk_forward", "in_sample"):
        raise ValueError(f"未知评估口径：{mode}")
    series = [int(value) for value in numbers]
    length = len(series)
    begin = max(0, int(warmup if start_index is None else start_index))
    end = length if stop_index is None else min(length, int(stop_index))
    resolved = resolve_params(params)
    effective_weights = {
        name: float((weights or {}).get(name, 0.0)) for name in COMPONENT_IDS
    }
    rows: list[dict[str, Any]] = []
    for position in range(begin, end):
        if mode == "walk_forward":
            visible = series[:position]
        else:
            visible = series[: position + 1]
        logweights = (
            component_logweights_at(visible, position, [
                name for name in (components or COMPONENT_IDS)
                if effective_weights.get(name, 0.0) != 0.0
            ], resolved)
        )
        probabilities = pool_probabilities(logweights, effective_weights)
        ranking = order_from_probabilities(probabilities, position)
        actual = series[position]
        rows.append(
            {
                "index": position,
                "actual": actual,
                "rank": ranking.index(actual) + 1,
                "p_actual": probabilities[actual],
                "log_loss": log_loss(probabilities, actual),
                "brier": brier_score(probabilities, actual),
                "probabilities": probabilities,
                "picks": ranking[: max(1, int(k))],
            }
        )
    metrics = _score_rows(rows, k)
    return {
        "engine_version": ENGINE_VERSION,
        "mode": mode,
        "claim": CLAIM_NO_EDGE,
        "claim_label": CLAIM_LABELS[CLAIM_NO_EDGE],
        "weights": effective_weights,
        "window": {
            "start_index": begin,
            "stop_index": end,
            "evaluated": len(rows),
        },
        **metrics,
        "calibration": calibration_block(rows),
        "rows": rows,
        "notes": [
            _ENGINE_NOTE,
            "每期预测严格只使用该期之前的数据（walk-forward），无未来函数。",
        ],
        "disclaimer": DISCLAIMER,
    }


def evaluate_components(
    numbers: Sequence[int],
    *,
    components: Sequence[str] | None = None,
    params: Mapping[str, Any] | None = None,
    k: int = K_DEFAULT,
    warmup: int = WARMUP,
    start_index: int | None = None,
    stop_index: int | None = None,
) -> dict[str, Any]:
    """逐组件单独评估（one-hot 权重 = 1.0，不做权重拟合）。

    这是「每个组件自己的想法单独拿出来，样本外到底行不行」的直接答案。
    额外附一个严格均匀基线（全 0 权重）作为对照。
    """
    names = list(components if components is not None else COMPONENT_IDS)
    entries: list[dict[str, Any]] = []
    for name in names:
        outcome = evaluate(
            numbers,
            weights={name: 1.0},
            params=params,
            k=k,
            warmup=warmup,
            start_index=start_index,
            stop_index=stop_index,
        )
        entries.append(
            {
                "component": name,
                "component_label": COMPONENT_LABELS[name],
                "component_note": COMPONENT_NOTES[name],
                "hit_rate": outcome["hit_rate"],
                "hits": outcome["hits"],
                "evaluated": outcome["evaluated"],
                "mean_rank": outcome["mean_rank"],
                "log_loss": outcome["log_loss"],
                "brier": outcome["brier"],
                "data_status": outcome["data_status"],
                "data_status_label": outcome["data_status_label"],
            }
        )
    uniform = evaluate(
        numbers,
        weights={},
        params=params,
        k=k,
        warmup=warmup,
        start_index=start_index,
        stop_index=stop_index,
    )
    entries.append(
        {
            "component": "UNIFORM",
            "component_label": "严格均匀基线（全 0 权重）",
            "component_note": "不做任何加权的对照。",
            "hit_rate": uniform["hit_rate"],
            "hits": uniform["hits"],
            "evaluated": uniform["evaluated"],
            "mean_rank": uniform["mean_rank"],
            "log_loss": uniform["log_loss"],
            "brier": uniform["brier"],
            "data_status": uniform["data_status"],
            "data_status_label": uniform["data_status_label"],
        }
    )
    return {
        "engine_version": ENGINE_VERSION,
        "claim": CLAIM_NO_EDGE,
        "claim_label": CLAIM_LABELS[CLAIM_NO_EDGE],
        "reference": reference_block(),
        "entries": entries,
        "notes": [
            _ENGINE_NOTE,
            "逐组件评估为 one-hot 权重 1.0、不做权重拟合；组件不叠加，互不干扰。",
        ],
        "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------- #
# 权重拟合（只在训练窗内层 walk-forward 选参）
# --------------------------------------------------------------------------- #
def _fit_split(length: int, train_ratio: float) -> int:
    ratio = min(0.9, max(0.5, float(train_ratio)))
    return max(3, min(length, int(round(length * ratio))))


def fit_weights(
    numbers: Sequence[int],
    *,
    k: int = K_DEFAULT,
    params: Mapping[str, Any] | None = None,
    components: Sequence[str] | None = None,
    train_ratio: float = DEFAULT_TRAIN_RATIO,
    inner_holdout: int = DEFAULT_INNER_HOLDOUT,
    grid: Sequence[float] = WEIGHT_GRID,
    max_passes: int = DEFAULT_MAX_PASSES,
) -> dict[str, Any]:
    """在**训练窗内**用内层 walk-forward 做坐标上升选权（评估窗从不参与）。

    训练窗 = ``numbers[:split_index]``；内层留出 = 训练窗末尾 ``inner_holdout`` 期。
    目标是最小化内层平均 log-loss（对整条分布敏感，比二值命中更严格），
    以 (log-loss, 平均排名, |权重|) 的字典序稳定裁定平手。
    """
    series = [int(value) for value in numbers]
    length = len(series)
    resolved = resolve_params(params)
    names = list(components if components is not None else COMPONENT_IDS)
    split_index = _fit_split(length, train_ratio)
    train = series[:split_index]
    holdout = max(3, min(int(inner_holdout), max(3, len(train) - WARMUP)))
    start = max(WARMUP, len(train) - holdout)
    positions = list(range(start, len(train)))

    envelope: dict[str, Any] = {
        "engine_version": ENGINE_VERSION,
        "claim": CLAIM_NO_EDGE,
        "claim_label": CLAIM_LABELS[CLAIM_NO_EDGE],
        "train_ratio": min(0.9, max(0.5, float(train_ratio))),
        "split_index": split_index,
        "train_length": len(train),
        "inner_holdout": holdout,
        "inner_positions": positions,
        "components": names,
        "grid": [float(value) for value in grid],
        "max_passes": int(max_passes),
    }
    if not positions:
        envelope.update(
            {
                "weights": {name: 0.0 for name in names},
                "inner_log_loss": None,
                "inner_hit_rate": None,
                "inner_mean_rank": None,
                "passes": 0,
                "data_status": DATA_STATUS_INSUFFICIENT,
                "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
                "notes": ["数据不足：训练窗内层留出期数为 0，无法选权，权重回落全 0。"],
                "disclaimer": DISCLAIMER,
            }
        )
        return envelope

    # 组件对数权重与网格无关 → 预计算一次，坐标上升只做算术
    cache = {
        position: component_logweights_at(series, position, names, resolved)
        for position in positions
    }

    def objective(candidate: Mapping[str, float]) -> tuple[float, float, int, int]:
        loss_sum = 0.0
        rank_sum = 0
        hits = 0
        for position in positions:
            probabilities = pool_probabilities(cache[position], candidate)
            actual = series[position]
            loss_sum += log_loss(probabilities, actual)
            ranking = order_from_probabilities(probabilities, position)
            rank = ranking.index(actual) + 1
            rank_sum += rank
            if rank <= int(k):
                hits += 1
        count = len(positions)
        return loss_sum / count, rank_sum / count, hits, count

    weights = {name: 0.0 for name in names}
    loss, mean_rank_value, hits, count = objective(weights)
    passes = 0
    moves: list[dict[str, Any]] = []
    for _ in range(max(1, int(max_passes))):
        changed = False
        for name in names:
            base_value = weights[name]
            best_value = base_value
            best_key = (loss, mean_rank_value, abs(base_value))
            for value in grid:
                candidate = dict(weights)
                candidate[name] = float(value)
                new_loss, new_rank, _, _ = objective(candidate)
                key = (new_loss, new_rank, abs(float(value)))
                if key[0] < best_key[0] - 1e-15 or (
                    abs(key[0] - best_key[0]) <= 1e-15 and key[1:] < best_key[1:]
                ):
                    best_key = key
                    best_value = float(value)
                    loss, mean_rank_value = new_loss, new_rank
            if best_value != base_value:
                weights[name] = best_value
                changed = True
                moves.append({"component": name, "value": best_value})
        passes += 1
        if not changed:
            break

    final_loss, final_rank, final_hits, final_count = objective(weights)
    envelope.update(
        {
            "weights": weights,
            "active_weights": {
                name: value for name, value in weights.items() if value != 0.0
            },
            "inner_log_loss": final_loss,
            "inner_mean_rank": final_rank,
            "inner_hit_rate": (final_hits / final_count) if final_count else None,
            "inner_hits": final_hits,
            "inner_evaluated": final_count,
            "moves": moves,
            "passes": passes,
            "data_status": DATA_STATUS_OK,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
            "notes": [
                _ENGINE_NOTE,
                "权重只在训练窗的内层留出上做坐标上升，评估窗从不参与选参。",
                "目标函数是内层平均 log-loss（越小越好），平手按平均排名与权重绝对值裁定。",
            ],
            "disclaimer": DISCLAIMER,
        }
    )
    return envelope


def select_and_evaluate(
    numbers: Sequence[int],
    *,
    k: int = K_DEFAULT,
    params: Mapping[str, Any] | None = None,
    components: Sequence[str] | None = None,
    train_ratio: float = DEFAULT_TRAIN_RATIO,
    inner_holdout: int = DEFAULT_INNER_HOLDOUT,
    grid: Sequence[float] = WEIGHT_GRID,
    max_passes: int = DEFAULT_MAX_PASSES,
) -> dict[str, Any]:
    """诚实主流程：训练窗内层选权 → 评估窗只打分（严格 walk-forward）。

    返回 ``selected``（内层选出的权重）、``holdout``（评估窗成绩）与
    ``components_holdout``（同评估窗上的逐组件对照）。
    """
    series = [int(value) for value in numbers]
    fit = fit_weights(
        series,
        k=k,
        params=params,
        components=components,
        train_ratio=train_ratio,
        inner_holdout=inner_holdout,
        grid=grid,
        max_passes=max_passes,
    )
    split_index = fit["split_index"]
    selected = {name: float(value) for name, value in fit["weights"].items()}
    holdout = evaluate(
        series,
        weights=selected,
        params=params,
        k=k,
        start_index=split_index,
        stop_index=len(series),
    )
    uniform_holdout = evaluate(
        series,
        weights={},
        params=params,
        k=k,
        start_index=split_index,
        stop_index=len(series),
    )
    component_holdout = evaluate_components(
        series,
        components=components,
        params=params,
        k=k,
        start_index=split_index,
        stop_index=len(series),
    )
    return {
        "engine_version": ENGINE_VERSION,
        "claim": CLAIM_NO_EDGE,
        "claim_label": CLAIM_LABELS[CLAIM_NO_EDGE],
        "fit": fit,
        "selected_weights": selected,
        "selected_label": (
            "全 0 权重 = 均匀分布"
            if not fit["active_weights"]
            else " / ".join(
                f"{name}={value:g}" for name, value in fit["active_weights"].items()
            )
        ),
        "holdout": {
            key: holdout[key]
            for key in (
                "evaluated",
                "hits",
                "hit_rate",
                "mean_rank",
                "rank_buckets",
                "log_loss",
                "brier",
                "reference",
                "data_status",
                "data_status_label",
            )
        },
        "holdout_calibration": holdout["calibration"],
        "uniform_holdout": {
            key: uniform_holdout[key]
            for key in (
                "evaluated",
                "hits",
                "hit_rate",
                "mean_rank",
                "log_loss",
                "brier",
                "reference",
            )
        },
        "components_holdout": component_holdout["entries"],
        "notes": [
            _ENGINE_NOTE,
            "selected 是训练窗内层选出的权重；holdout 是同一批权重在评估窗上的"
            "严格 walk-forward 成绩 —— 这才是「能不能用」的尺子。",
            "逐组件对照与 selected 用同一个评估窗，可直接横向比较。",
        ],
        "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------- #
# 小样本功效（复用 services.analytics 的口径）
# --------------------------------------------------------------------------- #
def power_block(
    evaluated: int,
    *,
    baseline_rate: float | None = None,
    deltas: Sequence[float] = (),
    alpha: float = 0.05,
    power: float = 0.8,
) -> dict[str, Any]:
    """本样本能排除多大的真实优势 + 目标 Δ 所需期数（算术全部展示）。

    复用 ``minimum_detectable_delta`` 得到标准误与 ``z_alpha`` / ``z_power``；
    「需要多少期」用同一条两比例公式反解，不引入新的口径：

        n ≈ ( z_{1-α/2}·√(p0(1-p0)) + z_{power}·√(p1(1-p1)) )^2 / (p1 - p0)^2
    """
    base = float(baseline_rate if baseline_rate is not None else K_DEFAULT / NUM_STATES)
    reference = minimum_detectable_delta(evaluated, base, alpha=alpha, power=power)
    z_alpha = reference.get("z_alpha")
    z_power = reference.get("z_power")
    targets: list[dict[str, Any]] = []
    for delta in deltas:
        delta = float(delta)
        p1 = base + delta
        if delta <= 0 or p1 >= 1.0 or z_alpha is None or z_power is None:
            continue
        required = (
            (z_alpha * math.sqrt(base * (1.0 - base))
             + z_power * math.sqrt(p1 * (1.0 - p1))) ** 2
        ) / (delta * delta)
        two_sigma = 4.0 * base * (1.0 - base) / (delta * delta)
        targets.append(
            {
                "delta": delta,
                "p1": p1,
                "required_draws_80_power": math.ceil(required),
                "required_draws_2sigma": math.ceil(two_sigma),
                "arithmetic": (
                    f"n = ( {z_alpha:.10f}·√({base:.10f}·{1 - base:.10f}) + "
                    f"{z_power:.10f}·√({p1:.10f}·{1 - p1:.10f}) )^2 / {delta:.10f}^2 "
                    f"= {required:.2f}"
                ),
            }
        )
    return {
        "evaluated": int(evaluated),
        "baseline_rate": base,
        "alpha": alpha,
        "power": power,
        "minimum_detectable_delta": reference.get("minimum_detectable_delta"),
        "two_sigma_delta": reference.get("two_sigma_delta"),
        "standard_error": reference.get("standard_error"),
        "required_draws": reference.get("required_draws"),
        "targets": targets,
        "data_status": reference.get("data_status"),
        "data_status_label": reference.get("data_status_label"),
        "claim": CLAIM_NO_EDGE,
        "claim_label": CLAIM_LABELS[CLAIM_NO_EDGE],
        "disclaimer": DISCLAIMER,
        "notes": [
            "MDD = 在本次可评估期数下，80% 功效能检出的最小真实优势；"
            "真实优势 >= MDD 却未被检出，才能说「至少排除了这么大的优势」。",
            "所需期数用同一条两比例公式反解，z 值直接取自 minimum_detectable_delta。",
        ],
    }


def holm_block(pvalues: Sequence[float], alpha: float = 0.05) -> dict[str, Any]:
    """复用的 Holm 校正入口（薄封装，统一在本模块暴露口径）。"""
    return holm_adjusted_p(pvalues, alpha=alpha)


# --------------------------------------------------------------------------- #
# 说明材料（供 CLI / canvas 复用）
# --------------------------------------------------------------------------- #
def component_catalog() -> list[dict[str, Any]]:
    """组件目录：id / 中文标签 / 说明 / 默认开关状态。"""
    return [
        {
            "component": name,
            "component_label": COMPONENT_LABELS[name],
            "component_note": COMPONENT_NOTES[name],
            "toggle": f"weights[{name}] 取 0 即关闭；取 ±1/±2 即接入池",
        }
        for name in COMPONENT_IDS
    ]


def bottom_line() -> dict[str, Any]:
    """一句话口径（英文枚举 + 中文白话），供 CLI / canvas 直接展示。"""
    return {
        "claim": CLAIM_NO_EDGE,
        "claim_label": CLAIM_LABELS[CLAIM_NO_EDGE],
        "k_default": K_DEFAULT,
        "odds_default": ODDS_DEFAULT,
        "reference": reference_block(),
        "statement": (
            "本引擎交付的是「1..49 的完整概率分布 + 可开关组件 + 可复现评估」；"
            "它的样本外表现与均匀随机基线不可区分，因此它控制的是分布形状与花费，"
            "不是中奖概率。"
        ),
        "disclaimer": DISCLAIMER,
    }


__all__ = [
    "CLAIM_NO_EDGE",
    "CLAIM_LABELS",
    "COMPONENT_FREQUENCY",
    "COMPONENT_GAP",
    "COMPONENT_IDS",
    "COMPONENT_LABELS",
    "COMPONENT_MARKOV",
    "COMPONENT_NOTES",
    "COMPONENT_PERIODIC",
    "COMPONENT_REPEAT",
    "COMPONENT_STRUCTURE",
    "COMPONENT_WAVE",
    "COMPONENT_ZODIAC",
    "DEFAULT_INNER_HOLDOUT",
    "DEFAULT_MAX_PASSES",
    "DEFAULT_PARAMS",
    "DEFAULT_TRAIN_RATIO",
    "DISCLAIMER",
    "ENGINE_VERSION",
    "WEIGHT_GRID",
    "bottom_line",
    "calibration_block",
    "component_catalog",
    "component_logweights_at",
    "component_topk",
    "component_weights",
    "evaluate",
    "evaluate_components",
    "fit_weights",
    "holm_block",
    "order_from_probabilities",
    "pool_probabilities",
    "power_block",
    "predict",
    "predict_walk_forward",
    "reference_block",
    "resolve_params",
    "select_and_evaluate",
]
