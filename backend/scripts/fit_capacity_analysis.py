r"""拟合容量 vs 样本外命中率 —— 「已知每期开奖，为什么反推不出超越随机的规则」。

本脚本把使用者的直觉当成一个**可测量的命题**：给定本池已导入的开奖结果，能否反推出
一条「尽可能贴近开奖号」的生成规则，并且它在没见过的期上仍然优于随机？

做法：造一族**容量严格递增**的规则（从零参数的均匀基线，到 49^3 的二阶马尔可夫表，
再到 >= 208 个自由参数的记忆化规则），全部按观测数据拟合，然后：

1. **容量扫描**：对每条规则同时给出 自由参数数 / 样本内命中率 / 样本外命中率
   （严格 walk-forward，逐期只用该期之前的数据）/ 匹配置换零分布的 p 值。
2. **参数数 vs 样本数**：一阶 / 二阶马尔可夫与通用点阵的自由参数数对照 210 期观测，
   量化「参数比数据多一个数量级」意味着什么。
3. **点阵带宽演示**：把点阵带宽 w 从 1 扫到 48，给出样本内 / 样本外覆盖率与
   「定义期望 (2w+1)/49」，说明「加宽到必然盖住答案」与「推荐变得无信息」是同一件事。
4. **为什么『贴近』是自毁目标**：推导并数值验证盈利条件 ``p > m / odds``，以及
   「证明该优势需要多少期」的样本量公式（逐行使用该行自己的 p0/p1 方差）。
5. **对抗性尝试**：主动造滞后 / 星期 / 生肖转移 / 和值奇偶 / 间隔 / 集成 / 拟合点阵等规则，
   凡是 p<0.05 的都点名。
6. **拟合过去 vs 预测未来**：用测得的数字说明二者的区别（无免费午餐）。

口径铁律（违反即为缺陷）：
- 全部结论只针对 **本池已导入的 N 期数据** 这个样本（``backend/data/draws_70_279.json``），
  不描述更大范围的历史，**禁止写「全市场」**，禁止升格为任何全量结论。
- 样本不足时显式输出「数据不足」，绝不用 0 值冒充结论。
- 严格 walk-forward：预测第 i 期时只允许使用第 i 期之前（``series[:i]``）的数据。
- 本脚本**只读**：不改 ``DEFAULT_SETTINGS``、不改 ``recommend()`` 引擎、不写数据库。

样本量口径（``profitability_block`` / ``scripts/fit_capacity_analysis.py`` 内的纯函数）：

- 每行用自己的 ``p0 = m/49``（均匀命中率）与 ``p1 = m/47``（平衡命中率），提升为 ``δ = p1 − p0``；
- 2σ（双侧 5%，无功效项）：``n = 4 · p0·(1−p0) / δ²``；
- 80% 功效（双侧 5%）：``n = ( z_{1−α/2}·√(p0(1−p0)) + z_{power}·√(p1(1−p1)) )² / δ²``，
  其中 ``z_{0.975} = 1.9599639845…``、``z_{0.80} = 0.8416212336…``；
- **禁止**跨行复用某个固定 ``p`` 的方差（例如所有行都套 k=10 的 ``10/49``），那会让小注数行的
  所需期数被高估约 8 倍。表内数值为向上取整的「至少需要期数」，JSON 同时给出未取整实数。

运行（离线，不碰数据库）：
    cd backend
    .\.venv\Scripts\python.exe scripts\fit_capacity_analysis.py --stage all --perms 100 --jobs 8

输出：``backend/data/fit_capacity_analysis.json``（``backend/data/*`` 已被 .gitignore 忽略）。
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import random
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from statistics import NormalDist
from typing import Any, Callable, Sequence

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services import analytics as A  # noqa: E402
from services import lottery as L  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
NUMBER_MIN = int(L.NUMBER_MIN)
NUMBER_MAX = int(L.NUMBER_MAX)
NUMBERS: tuple[int, ...] = tuple(range(NUMBER_MIN, NUMBER_MAX + 1))
NUM_STATES = len(NUMBERS)  # 49

K_DEFAULT = 10
ODDS_DEFAULT = float(L.DEFAULT_ODDS)  # 47.0，仅作参照，不改设置
BASELINE = K_DEFAULT / NUM_STATES  # 10/49 = 20.408%
WARMUP = 2  # 与 analytics.MIN_PRIOR_DRAWS 一致：至少要有上一期才能算「贴近」
SEED = 20261007
PERM_SEED_BASE = 771_007
KNN_L = 3  # k-NN 模式向量长度（最近 L 期）
KNN_GRID = (1, 3, 5, 10, 30)
KNN_METRICS = ("l1", "l2")
HOT_WINDOW_GRID: tuple[int, ...] = (5, 10, 20, 40, 80, 0)  # 0 = 全部历史
ZODIAC_STEP = int(L.ZODIAC_STEP)  # 12
LADDER_M_VALUES: tuple[int, ...] = (0, 26, 52, 78, 104, 130, 156, 182, 208)


# --------------------------------------------------------------------------- #
# 纯工具
# --------------------------------------------------------------------------- #
def _quantile_sorted(values: Sequence[float], q: float) -> float:
    """升序序列的线性插值分位数（不依赖 numpy）。"""
    size = len(values)
    if size == 0:
        return 0.0
    if size == 1:
        return float(values[0])
    position = max(0.0, min(1.0, float(q))) * (size - 1)
    lower = int(math.floor(position))
    upper = min(lower + 1, size - 1)
    frac = position - lower
    return float(values[lower]) * (1.0 - frac) + float(values[upper]) * frac


def hit_rate(hits: Sequence[bool]) -> float | None:
    if not hits:
        return None
    return sum(1 for value in hits if value) / len(hits)


def rank_top_k(counts: Counter, k: int) -> list[int]:
    """按「出现次数降序、号码升序」取前 k 个号码（确定性，便于复现）。"""
    if not counts or k <= 0:
        return []
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [int(number) for number, _ in ordered[:k]]


def pad_to_k(base: Sequence[int], historic: Sequence[int], k: int) -> list[int]:
    """把不足 k 个的推荐补足到正好 k 个（先补历史热门，再补最小号码）。"""
    chosen: list[int] = []
    seen: set[int] = set()
    for number in base:
        number = int(number)
        if number not in seen:
            seen.add(number)
            chosen.append(number)
        if len(chosen) >= k:
            return chosen[:k]
    for number in rank_top_k(Counter(int(n) for n in historic), k):
        if number not in seen:
            seen.add(number)
            chosen.append(number)
        if len(chosen) >= k:
            return chosen[:k]
    for number in NUMBERS:
        if number not in seen:
            seen.add(number)
            chosen.append(number)
        if len(chosen) >= k:
            break
    return chosen[:k]


def build_transition_counts(sequence: Sequence[int], order: int) -> dict[tuple[int, ...], Counter]:
    """把 ``sequence`` 里所有 ``order`` 阶转移（状态 -> 后继计数）建成表。"""
    table: dict[tuple[int, ...], Counter] = {}
    for index in range(order, len(sequence)):
        state = tuple(int(n) for n in sequence[index - order : index])
        bucket = table.get(state)
        if bucket is None:
            bucket = Counter()
            table[state] = bucket
        bucket[int(sequence[index])] += 1
    return table


def markov_rank(sequence: Sequence[int], order: int, k: int) -> list[int]:
    """用 ``sequence`` 拟合 ``order`` 阶马尔可夫表，预测「下一个」号码（带逐级回退）。

    回退链：order → … → 1 → 全历史频次；结果一律补足到 k 个。
    """
    sequence = [int(n) for n in sequence]
    if not sequence:
        return list(NUMBERS[:k])
    for current_order in range(order, 0, -1):
        if len(sequence) < current_order:
            continue
        table = build_transition_counts(sequence, current_order)
        state = tuple(sequence[-current_order:])
        if state in table:
            base = rank_top_k(table[state], k)
            if len(base) >= k:
                return base[:k]
            return pad_to_k(base, sequence, k)
    return pad_to_k(rank_top_k(Counter(sequence), k), sequence, k)


def markov_rank_at(sequence_full: Sequence[int], target_index: int, order: int, k: int) -> list[int]:
    """在**含被预测期本身**的整段数据上拟合，再解释第 ``target_index`` 期（样本内口径）。

    与 ``markov_rank`` 的区别：状态取 ``target_index`` 之前 ``order`` 期（而不是序列末尾），
    但转移表建在 ``sequence_full[:target_index+1]`` 上 —— 因此包含了 (状态 → 答案) 这条转移，
    正是「反推已知开奖」的极限口径。
    """
    sequence_full = [int(n) for n in sequence_full]
    if target_index <= 0:
        return list(NUMBERS[:k])
    for current_order in range(order, 0, -1):
        if target_index < current_order:
            continue
        table = build_transition_counts(sequence_full[: target_index + 1], current_order)
        state = tuple(sequence_full[target_index - current_order : target_index])
        if state in table:
            base = rank_top_k(table[state], k)
            source = sequence_full[: target_index + 1]
            return base[:k] if len(base) >= k else pad_to_k(base, source, k)
    return pad_to_k(rank_top_k(Counter(sequence_full[:target_index]), k), sequence_full[:target_index], k)


def context_lookup_at(sequence_full: Sequence[int], target_index: int, order: int, k: int) -> list[int]:
    """纯上下文查表的样本内口径（不回退）：转移表含被预测期自身的转移。"""
    sequence_full = [int(n) for n in sequence_full]
    if target_index < order:
        return rank_top_k(Counter(sequence_full[:target_index]), k)
    table = build_transition_counts(sequence_full[: target_index + 1], order)
    state = tuple(sequence_full[target_index - order : target_index])
    source = sequence_full[: target_index + 1]
    if state in table:
        return pad_to_k(rank_top_k(table[state], k), source, k)
    return rank_top_k(Counter(sequence_full[:target_index]), k)


def context_lookup(sequence: Sequence[int], order: int, k: int) -> list[int]:
    """纯上下文查表（**不回退**到低阶）：命中则返回记忆的后继，否则回退全历史热门。"""
    sequence = [int(n) for n in sequence]
    if len(sequence) < order:
        return rank_top_k(Counter(sequence), k)
    table = build_transition_counts(sequence, order)
    state = tuple(sequence[-order:])
    if state in table:
        return pad_to_k(rank_top_k(table[state], k), sequence, k)
    return rank_top_k(Counter(sequence), k)


def fit_memorise_index(sequence: Sequence[int]) -> dict[int, int]:
    """按「期序号 -> 该期开奖号」的记忆表：参数数 = 观测期数，能复现每个训练答案。"""
    return {index: int(sequence[index]) for index in range(len(sequence))}


def fit_hot_window(sequence: Sequence[int], k: int, grid: Sequence[int] = HOT_WINDOW_GRID) -> dict[str, Any]:
    """拟合「近 W 期频次热门」的窗口 W（以样本内命中率为准则，一次增量扫描）。

    返回 ``{"window": W, "in_sample_rate": r, "in_sample_hits": h, "evaluated": t}``。
    """
    sequence = [int(n) for n in sequence]
    n = len(sequence)
    if n < 2:
        return {"window": grid[0], "in_sample_rate": None, "in_sample_hits": 0, "evaluated": 0}
    counters = {w: Counter() for w in grid if w > 0}
    all_counter: Counter = Counter()
    cum_hits = {w: 0 for w in grid}
    cum_tot = {w: 0 for w in grid}
    # 初始窗口：目标 i=1 的窗口是 sequence[max(0, 1-w):1]
    for w in grid:
        if w <= 0:
            continue
        counters[w][sequence[0]] += 1
    all_counter[sequence[0]] += 1
    for i in range(1, n):
        for w in grid:
            counts = all_counter if w == 0 else counters[w]
            if sequence[i] in rank_top_k(counts, k):
                cum_hits[w] += 1
            cum_tot[w] += 1
        for w in grid:
            if w == 0:
                all_counter[sequence[i]] += 1
                continue
            counters[w][sequence[i]] += 1
            drop = i - w
            if drop >= 0:
                leaving = sequence[drop]
                counters[w][leaving] -= 1
                if counters[w][leaving] <= 0:
                    del counters[w][leaving]
    best_window = grid[0]
    best_rate = -1.0
    for w in grid:
        rate = (cum_hits[w] / cum_tot[w]) if cum_tot[w] else 0.0
        # 命中率相同则偏好更大的窗口（更稳）
        if rate > best_rate or (rate == best_rate and w > best_window):
            best_rate = rate
            best_window = w
    return {
        "window": int(best_window),
        "in_sample_rate": best_rate if cum_tot[best_window] else None,
        "in_sample_hits": cum_hits[best_window],
        "evaluated": cum_tot[best_window],
    }


def hot_picks(sequence: Sequence[int], window: int, k: int) -> list[int]:
    sequence = [int(n) for n in sequence]
    if window and window > 0:
        used = sequence[-int(window) :]
    else:
        used = sequence
    return pad_to_k(rank_top_k(Counter(used), k), used, k)


def fit_lattice_band(sequence: Sequence[int], coverage: float = 0.75) -> dict[str, Any]:
    """拟合点阵带：中心 = 有符号差值中位数，半宽 = |差值−中心| 的 P75 分位。

    与项目 ``services.lottery.predict_wave_band`` 同型（中位数中心 + 四分位带宽）。
    """
    sequence = [int(n) for n in sequence]
    diffs = [sequence[i] - sequence[i - 1] for i in range(1, len(sequence))]
    if not diffs:
        return {"half_width": 0, "offset": 0, "coverage": None, "samples": 0}
    offset = int(round(_quantile_sorted(sorted(diffs), 0.5)))
    adjusted = sorted(abs(d - offset) for d in diffs)
    half_width = max(0, int(math.ceil(_quantile_sorted(adjusted, coverage))))
    covered = sum(1 for value in adjusted if value <= half_width)
    return {
        "half_width": half_width,
        "offset": offset,
        "coverage": covered / len(adjusted),
        "samples": len(adjusted),
    }


def lattice_picks(latest: int, half_width: int, offset: int, k: int) -> list[int]:
    """点阵推荐：以 ``latest + offset`` 为中心、半宽 ``half_width`` 的带内最近 k 个号。"""
    centre = min(NUMBER_MAX, max(NUMBER_MIN, int(latest) + int(offset)))
    inside = [n for n in NUMBERS if abs(n - centre) <= int(half_width)]
    inside.sort(key=lambda n: (abs(n - centre), n))
    if len(inside) >= k:
        return inside[:k]
    chosen = set(inside)
    outside = [n for n in NUMBERS if n not in chosen]
    outside.sort(key=lambda n: (abs(n - centre), n))
    return (inside + outside)[:k]


def lattice_band_size(latest: int, half_width: int) -> int:
    """以 ``latest`` 为中心、半宽 w 的带在 1..49 内实际覆盖的号码数。"""
    return sum(1 for n in NUMBERS if abs(n - int(latest)) <= int(half_width))


# --------------------------------------------------------------------------- #
# 盈利条件（纯函数）
# --------------------------------------------------------------------------- #
def breakeven_probability(numbers: int, odds: float) -> float:
    """每期押 m 注（成本 m）、命中概率 p 时的平衡命中率 ``m / odds``。"""
    return numbers / odds


def required_relative_uplift(odds: float, number_space: int = NUM_STATES) -> float:
    """相对均匀概率密度需要的最小提升 ``49 / odds − 1``（与注数 m 无关）。"""
    return number_space / odds - 1.0


def required_absolute_edge(numbers: int, odds: float, number_space: int = NUM_STATES) -> float:
    """在均匀命中率 m/49 之上，还需要增加的**绝对**命中率 ``m/odds − m/49``。"""
    return numbers / odds - numbers / number_space


def expected_value_per_period(numbers: int, odds: float, hit_probability: float) -> float:
    """每期期望盈亏（单位投注额 = 1）：``odds * p − m``。"""
    return odds * hit_probability - numbers


def sigma_squared_for_rate(probability: float) -> float:
    """伯努利命中率的方差 ``p·(1−p)``。

    样本量推导里**必须逐行使用该行自己的 p**。把某一行的 ``p0`` 换成别的行的
    （例如所有行都恒定用 k=10 的 10/49）会系统性高估小注数行的所需期数。
    """
    return probability * (1.0 - probability)


def draws_to_detect_two_sigma_exact(p0: float, p1: float) -> float | None:
    """``p1 − p0`` 刚好等于 2 倍标准误所需的期数（实数，未取整）。

    公式（双侧 5%，无功效项）：

        n = 4 · p0·(1−p0) / (p1 − p0)²

    分母用该行自己的提升幅度 ``p1 − p0``，分子用该行自己的零假设方差 ``p0·(1−p0)``；
    两者必须是同一行的 ``p0``，否则口径不一致。
    """
    delta = p1 - p0
    if delta <= 0.0:
        return None
    return 4.0 * sigma_squared_for_rate(p0) / (delta * delta)


def draws_to_detect_exact(
    p0: float,
    p1: float,
    *,
    alpha: float = 0.05,
    power: float = 0.8,
) -> float | None:
    """双侧 ``alpha`` + 指定功效下检出提升 ``p1 − p0`` 所需期数（实数，未取整）。

    公式（两个 z 各自配上**自己那一侧**的方差）：

        n = ( z_{1−α/2}·√(p0(1−p0)) + z_{power}·√(p1(1−p1)) )² / (p1 − p0)²

    固定 z 值（``statistics.NormalDist``）：``z_{0.975} = 1.9599639845…``、
    ``z_{0.80} = 0.8416212336…``。注意**不能**把它简化成
    ``(z_a + z_b)²·p·(1−p)/δ²`` —— 那等于假设两侧方差相同且等于某个固定的 p。
    """
    delta = p1 - p0
    if delta <= 0.0:
        return None
    normal = NormalDist()
    z_alpha = normal.inv_cdf(1.0 - alpha / 2.0)
    z_power = normal.inv_cdf(power)
    numerator = (
        z_alpha * math.sqrt(sigma_squared_for_rate(p0))
        + z_power * math.sqrt(sigma_squared_for_rate(p1))
    ) ** 2
    return numerator / (delta * delta)


def draws_to_detect(
    p0: float,
    p1: float,
    *,
    alpha: float = 0.05,
    power: float = 0.8,
) -> int | None:
    """``draws_to_detect_exact`` 向上取整：至少需要这么多期。"""
    exact = draws_to_detect_exact(p0, p1, alpha=alpha, power=power)
    return None if exact is None else math.ceil(exact)


def draws_to_detect_two_sigma(p0: float, p1: float) -> int | None:
    """``draws_to_detect_two_sigma_exact`` 向上取整：至少需要这么多期。"""
    exact = draws_to_detect_two_sigma_exact(p0, p1)
    return None if exact is None else math.ceil(exact)


def sample_size_z_values(alpha: float = 0.05, power: float = 0.8) -> dict[str, float]:
    """样本量公式里用到的 z 值（写进 JSON / 画布标注，避免口径漂移）。"""
    normal = NormalDist()
    return {
        "z_two_sided_alpha": normal.inv_cdf(1.0 - alpha / 2.0),
        "z_power": normal.inv_cdf(power),
        "alpha": alpha,
        "power": power,
    }



# --------------------------------------------------------------------------- #
# k-NN 规则（纯函数）
# --------------------------------------------------------------------------- #
def _pattern_distance(left: Sequence[int], right: Sequence[int], metric: str) -> float:
    if metric == "l2":
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)))
    return float(sum(abs(a - b) for a, b in zip(left, right)))


def knn_picks(
    sequence: Sequence[int],
    k: int,
    *,
    neighbours: int,
    metric: str,
    pattern_length: int = KNN_L,
    include_self: bool = False,
) -> list[int]:
    """用最近 ``neighbours`` 个历史模式（后继）投票，取前 k 个号码。

    ``include_self=True`` 时把「以 sequence 末尾结尾的模式」也放进近邻池 ——
    那等于让规则引用被预测的观测本身（训练误差口径），用于演示记忆化。
    """
    sequence = [int(n) for n in sequence]
    if len(sequence) < pattern_length + 1:
        return rank_top_k(Counter(sequence), k)
    query = sequence[-pattern_length:]
    limit = len(sequence) if include_self else len(sequence) - 1
    scored: list[tuple[float, int]] = []
    for end in range(pattern_length, limit + 1):
        pattern = sequence[end - pattern_length : end]
        distance = _pattern_distance(query, pattern, metric)
        scored.append((distance, end))
    if not scored:
        return rank_top_k(Counter(sequence), k)
    scored.sort(key=lambda item: (item[0], item[1]))
    votes: Counter = Counter()
    for distance, end in scored[: max(1, int(neighbours))]:
        weight = 1.0 / (distance + 0.25)
        votes[int(sequence[end])] += weight
    if not votes:
        return rank_top_k(Counter(sequence), k)
    ordered = [number for number, _ in sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))]
    return pad_to_k(ordered, sequence, k)


def fit_knn(
    sequence: Sequence[int],
    k: int,
    *,
    pattern_length: int = KNN_L,
    grid: Sequence[int] = KNN_GRID,
    metrics: Sequence[str] = KNN_METRICS,
) -> dict[str, Any]:
    """在 ``sequence`` 上拟合近邻数 k 与距离度量（以样本内命中率为准则）。

    评估时近邻池只含被预测期**之前**的模式（不含自身），所以这是「插值」而非记忆化。
    """
    sequence = [int(n) for n in sequence]
    best: dict[str, Any] | None = None
    n = len(sequence)
    for metric in metrics:
        for neighbours in grid:
            hits = 0
            total = 0
            for index in range(pattern_length, n):
                picks = knn_picks(
                    sequence[:index],
                    k,
                    neighbours=neighbours,
                    metric=metric,
                    pattern_length=pattern_length,
                )
                if sequence[index] in picks:
                    hits += 1
                total += 1
            rate = (hits / total) if total else 0.0
            candidate = {
                "neighbours": int(neighbours),
                "metric": metric,
                "in_sample_rate": rate if total else None,
                "in_sample_hits": hits,
                "evaluated": total,
            }
            if best is None or rate > best["in_sample_rate"] or (
                rate == best["in_sample_rate"] and neighbours < best["neighbours"]
            ):
                best = candidate
    return best or {"neighbours": grid[0], "metric": metrics[0], "in_sample_rate": None,
                    "in_sample_hits": 0, "evaluated": 0}


# --------------------------------------------------------------------------- #
# 走步（walk-forward）规则实现：预测第 i 期只读 series[:i]
# --------------------------------------------------------------------------- #
def _uniform_picks(index: int, k: int) -> set[int]:
    """零参数均匀取号：起点随期号平移、步长 10（与 49 互质）取 k 个号。

    这是「没有任何拟合信息」的基线取号方式；步长取 10 是为了让 k 个号铺开在号码面上。
    """
    return {((index + j * 10) % NUM_STATES) + 1 for j in range(k)}


def wf_uniform(series, dates, k, warmup):
    """零参数均匀基线（定义期望 = k/49 = 20.408%，具体实现会因抽样略偏）。"""
    hits = []
    for i in range(warmup, len(series)):
        hits.append(int(series[i]) in _uniform_picks(i, k))
    return hits


def wf_markov(series, dates, k, warmup, order):
    hits = []
    for i in range(warmup, len(series)):
        hits.append(series[i] in markov_rank(series[:i], order, k))
    return hits


def wf_context(series, dates, k, warmup, order):
    hits = []
    for i in range(warmup, len(series)):
        hits.append(series[i] in context_lookup(series[:i], order, k))
    return hits


def wf_memorise_index(series, dates, k, warmup):
    """按「期序号 -> 开奖号」记忆：样本内每期都能复现，样本外序号从未见过。"""
    hits = []
    for i in range(warmup, len(series)):
        table = fit_memorise_index(series[:i])
        if i in table:
            picks: set[int] = {int(table[i])}
        else:
            picks = _uniform_picks(i, k)
        hits.append(int(series[i]) in picks)
    return hits


def wf_hot_window(series, dates, k, warmup, grid=HOT_WINDOW_GRID):
    """近窗热门：窗口 W 在**每一步**上用该步之前的数据重新拟合（增量扫描）。"""
    n = len(series)
    counters = {w: Counter() for w in grid if w > 0}
    all_counter: Counter = Counter()
    cum_hits = {w: 0 for w in grid}
    cum_tot = {w: 0 for w in grid}
    for w in grid:
        if w > 0:
            counters[w][series[0]] += 1
    all_counter[series[0]] += 1
    hits: list[bool] = []
    for i in range(1, n):
        if i >= warmup:
            best_window = grid[0]
            best_key = (-1.0, -1)
            for w in grid:
                rate = (cum_hits[w] / cum_tot[w]) if cum_tot[w] else 0.0
                key = (rate, w)
                if key > best_key:
                    best_key = key
                    best_window = w
            counts = all_counter if best_window == 0 else counters[best_window]
            hits.append(series[i] in rank_top_k(counts, k))
        for w in grid:
            counts = all_counter if w == 0 else counters[w]
            if series[i] in rank_top_k(counts, k):
                cum_hits[w] += 1
            cum_tot[w] += 1
        for w in grid:
            if w == 0:
                all_counter[series[i]] += 1
                continue
            counters[w][series[i]] += 1
            drop = i - w
            if drop >= 0:
                leaving = series[drop]
                counters[w][leaving] -= 1
                if counters[w][leaving] <= 0:
                    del counters[w][leaving]
    return hits


def wf_knn(series, dates, k, warmup, train_ratio=0.7):
    """k-NN：近邻数与距离度量在训练段一次拟合并冻结，之后逐期用前缀重估近邻。"""
    n = len(series)
    split = max(KNN_L + 2, int(n * train_ratio))
    fitted = fit_knn(series[:split], k)
    neighbours = fitted["neighbours"]
    metric = fitted["metric"]
    hits = []
    for i in range(warmup, n):
        picks = knn_picks(series[:i], k, neighbours=neighbours, metric=metric)
        hits.append(series[i] in picks)
    return hits, fitted


def wf_lattice(series, dates, k, warmup):
    """点阵：中心偏移与半宽在每一步用前缀重新拟合（中位数中心 + P75 带宽）。"""
    hits = []
    for i in range(warmup, len(series)):
        band = fit_lattice_band(series[:i])
        picks = lattice_picks(series[i - 1], band["half_width"], band["offset"], k)
        hits.append(series[i] in picks)
    return hits


def _zodiac_group(number: int) -> int:
    return (int(number) - 1) % ZODIAC_STEP


def wf_lag_markov(series, dates, k, warmup, lag):
    """一阶马尔可夫，但状态取「倒数第 lag 期」而不是上一期（探更长滞后的结构）。"""
    hits = []
    for i in range(warmup, len(series)):
        prefix = series[:i]
        if len(prefix) < lag:
            hits.append(series[i] in rank_top_k(Counter(prefix), k))
            continue
        state = int(prefix[-lag])
        table = defaultdict(Counter)
        for j in range(1, len(prefix)):
            table[int(prefix[j - 1])][int(prefix[j])] += 1
        bucket = table.get(state)
        picks = pad_to_k(rank_top_k(bucket, k) if bucket else [], prefix, k)
        hits.append(series[i] in picks)
    return hits


def wf_gap_mode(series, dates, k, warmup):
    """间隔众数：取历史有符号差值的众数 g，推荐 ``latest + g`` 附近的 k 个号。"""
    hits = []
    for i in range(warmup, len(series)):
        prefix = series[:i]
        if len(prefix) < 2:
            hits.append(series[i] in rank_top_k(Counter(prefix), k))
            continue
        gaps = Counter(prefix[j] - prefix[j - 1] for j in range(1, len(prefix)))
        gap = min(gaps.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        hits.append(series[i] in lattice_picks(prefix[-1], half_width=0, offset=int(gap), k=k))
    return hits


def wf_day_of_week(series, dates, k, warmup):
    """星期几热门：按目标期的星期几，取历史上同一星期几最热的 k 个号。"""
    hits = []
    for i in range(warmup, len(series)):
        weekday = dates[i].weekday()
        bucket: Counter = Counter()
        for j in range(i):
            if dates[j].weekday() == weekday:
                bucket[int(series[j])] += 1
        if not bucket:
            bucket = Counter(int(n) for n in series[:i])
        picks = pad_to_k(rank_top_k(bucket, k), series[:i], k)
        hits.append(series[i] in picks)
    return hits


def wf_zodiac_transition(series, dates, k, warmup):
    """生肖转移：取历史上最常跟随「最新号生肖组」的那个组，先取该组号码。"""
    hits = []
    for i in range(warmup, len(series)):
        prefix = series[:i]
        if len(prefix) < 2:
            hits.append(series[i] in rank_top_k(Counter(prefix), k))
            continue
        transitions: Counter = Counter()
        for j in range(1, len(prefix)):
            if _zodiac_group(prefix[j - 1]) == _zodiac_group(prefix[-1]):
                transitions[_zodiac_group(prefix[j])] += 1
        if not transitions:
            picks = pad_to_k(rank_top_k(Counter(prefix), k), prefix, k)
        else:
            target_group = min(transitions.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            group_numbers = [n for n in NUMBERS if _zodiac_group(n) == target_group]
            group_numbers.sort(key=lambda n: -Counter(prefix)[n])
            picks = pad_to_k(group_numbers, prefix, k)
        hits.append(series[i] in picks)
    return hits


def wf_sum_parity(series, dates, k, warmup):
    """和值奇偶：最近两期同奇偶则反押另一奇偶，否则延续上期奇偶（取该奇偶历史热门）。"""
    hits = []
    for i in range(warmup, len(series)):
        prefix = series[:i]
        if len(prefix) < 2:
            hits.append(series[i] in rank_top_k(Counter(prefix), k))
            continue
        last, prev = prefix[-1], prefix[-2]
        if (last % 2) == (prev % 2):
            target_parity = 1 - (last % 2)
        else:
            target_parity = last % 2
        bucket = Counter(int(n) for n in prefix if int(n) % 2 == target_parity)
        picks = pad_to_k(rank_top_k(bucket, k), prefix, k)
        hits.append(series[i] in picks)
    return hits


def wf_repeat_around(series, dates, k, warmup):
    """贴近上一期：直接推荐离 ``latest`` 最近的 k 个号（「贴近」本身当规则）。"""
    hits = []
    for i in range(warmup, len(series)):
        hits.append(series[i] in lattice_picks(series[i - 1], half_width=0, offset=0, k=k))
    return hits


def wf_hot_all(series, dates, k, warmup):
    hits = []
    for i in range(warmup, len(series)):
        hits.append(series[i] in rank_top_k(Counter(int(n) for n in series[:i]), k))
    return hits


# --------------------------------------------------------------------------- #
# 规则登记表：容量扫描 + 对抗性尝试
# --------------------------------------------------------------------------- #
def _rule_table() -> dict[str, dict[str, Any]]:
    """名字 -> {family, params, cells, memory, wf}。``wf`` 返回逐期命中布尔列表。"""
    table: dict[str, dict[str, Any]] = {
        "uniform": {
            "family": "基线",
            "params": 0,
            "cells": 0,
            "memory": False,
            "wf": lambda s, d, k, w: wf_uniform(s, d, k, w),
            "desc": "恒定均匀：固定推荐 01..10，零参数",
        },
        "hot_window": {
            "family": "频次启发",
            "params": 1,
            "cells": 0,
            "memory": False,
            "wf": lambda s, d, k, w: wf_hot_window(s, d, k, w),
            "desc": "近窗热门，窗口 W 逐步拟合",
        },
        "lattice_fitted": {
            "family": "点阵",
            "params": 2,
            "cells": 0,
            "memory": False,
            "wf": lambda s, d, k, w: wf_lattice(s, d, k, w),
            "desc": "点阵：中心偏移 + 半宽逐步拟合",
        },
        "knn": {
            "family": "近邻",
            "params": 2,
            "cells": 0,
            "memory": False,
            "wf": lambda s, d, k, w: wf_knn(s, d, k, w)[0],
            "desc": "k-NN 历史模式，k 与距离度量在训练段拟合",
        },
        "memorise_context": {
            "family": "记忆化",
            "params": NUM_STATES**3 - NUM_STATES**2,
            "cells": NUM_STATES**3,
            "memory": True,
            "wf": lambda s, d, k, w: wf_context(s, d, k, w, 2),
            "desc": "二阶上下文查表（不回退），可复现每个训练答案",
        },
        "memorise_index": {
            "family": "记忆化",
            "params": 208,
            "cells": 208,
            "memory": True,
            "wf": lambda s, d, k, w: wf_memorise_index(s, d, k, w),
            "desc": "按期序号记忆开奖号，208 个自由参数，训练答案 100% 复现",
        },
        "markov1": {
            "family": "马尔可夫",
            "params": NUM_STATES * (NUM_STATES - 1),
            "cells": NUM_STATES * NUM_STATES,
            "memory": True,
            "wf": lambda s, d, k, w: wf_markov(s, d, k, w, 1),
            "desc": "一阶转移表 49×49",
        },
        "markov2": {
            "family": "马尔可夫",
            "params": NUM_STATES**3 - NUM_STATES**2,
            "cells": NUM_STATES**3,
            "memory": True,
            "wf": lambda s, d, k, w: wf_markov(s, d, k, w, 2),
            "desc": "二阶转移表 49^3（带逐级回退）",
        },
    }
    return table


def _adversarial_table() -> dict[str, dict[str, Any]]:
    return {
        "lag2_markov": {
            "family": "更长滞后",
            "params": NUM_STATES * (NUM_STATES - 1),
            "wf": lambda s, d, k, w: wf_lag_markov(s, d, k, w, 2),
            "desc": "状态取倒数第 2 期的一阶转移",
        },
        "lag3_markov": {
            "family": "更长滞后",
            "params": NUM_STATES * (NUM_STATES - 1),
            "wf": lambda s, d, k, w: wf_lag_markov(s, d, k, w, 3),
            "desc": "状态取倒数第 3 期的一阶转移",
        },
        "gap_mode": {
            "family": "间隔建模",
            "params": 1,
            "wf": lambda s, d, k, w: wf_gap_mode(s, d, k, w),
            "desc": "有符号差值众数当预测间隔",
        },
        "day_of_week": {
            "family": "日历",
            "params": NUM_STATES,
            "wf": lambda s, d, k, w: wf_day_of_week(s, d, k, w),
            "desc": "按目标期星期几取历史最热号",
        },
        "zodiac_transition": {
            "family": "生肖转移",
            "params": ZODIAC_STEP,
            "wf": lambda s, d, k, w: wf_zodiac_transition(s, d, k, w),
            "desc": "取最常跟随最新生肖组的生肖组",
        },
        "sum_parity": {
            "family": "和值奇偶",
            "params": 2,
            "wf": lambda s, d, k, w: wf_sum_parity(s, d, k, w),
            "desc": "最近两期同奇偶则反押，否则延续",
        },
        "repeat_around_latest": {
            "family": "贴近",
            "params": 0,
            "wf": lambda s, d, k, w: wf_repeat_around(s, d, k, w),
            "desc": "直接推荐离上期最近的 k 个号",
        },
        "hot_all_history": {
            "family": "频次",
            "params": 0,
            "wf": lambda s, d, k, w: wf_hot_all(s, d, k, w),
            "desc": "全历史频次前 k 个号",
        },
        "fitted_lattice": {
            "family": "点阵",
            "params": 2,
            "wf": lambda s, d, k, w: wf_lattice(s, d, k, w),
            "desc": "拟合点阵（同容量扫描的 lattice_fitted）",
        },
        "ensemble": {
            "family": "集成",
            "params": 4,
            "wf": lambda s, d, k, w: wf_ensemble_vote(s, d, k, w),
            "desc": "滞后 / 间隔 / 生肖 / 星期 / 热门投票",
        },
    }


def wf_ensemble_vote(series, dates, k, warmup):  # pragma: no cover - 见下
    """集成投票：逐期把 5 个组件规则的推荐并起来投票。"""
    n = len(series)
    hits = []
    for i in range(warmup, n):
        prefix = series[:i]
        votes: Counter = Counter()
        components: list[list[int]] = []
        # 滞后 2
        if len(prefix) >= 2:
            state = int(prefix[-2])
            table = defaultdict(Counter)
            for j in range(1, len(prefix)):
                table[int(prefix[j - 1])][int(prefix[j])] += 1
            bucket = table.get(state)
            components.append(pad_to_k(rank_top_k(bucket, k) if bucket else [], prefix, k))
        # 间隔众数
        if len(prefix) >= 2:
            gaps = Counter(prefix[j] - prefix[j - 1] for j in range(1, len(prefix)))
            gap = min(gaps.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            components.append(lattice_picks(prefix[-1], half_width=0, offset=int(gap), k=k))
        # 生肖转移
        if len(prefix) >= 2:
            transitions: Counter = Counter()
            for j in range(1, len(prefix)):
                if _zodiac_group(prefix[j - 1]) == _zodiac_group(prefix[-1]):
                    transitions[_zodiac_group(prefix[j])] += 1
            if transitions:
                group = min(transitions.items(), key=lambda kv: (-kv[1], kv[0]))[0]
                group_numbers = [n for n in NUMBERS if _zodiac_group(n) == group]
                group_numbers.sort(key=lambda n: -Counter(prefix)[n])
                components.append(pad_to_k(group_numbers, prefix, k))
        # 星期热门
        weekday = dates[i].weekday()
        bucket = Counter(int(series[j]) for j in range(i) if dates[j].weekday() == weekday)
        if not bucket:
            bucket = Counter(int(x) for x in prefix)
        components.append(pad_to_k(rank_top_k(bucket, k), prefix, k))
        # 全历史热门
        components.append(pad_to_k(rank_top_k(Counter(int(x) for x in prefix), k), prefix, k))
        for component in components:
            for number in component:
                votes[int(number)] += 1
        ordered = [n for n, _ in sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))]
        hits.append(series[i] in pad_to_k(ordered, prefix, k))
    return hits


def run_all_rules(series: Sequence[int], dates: Sequence[date], k: int, warmup: int) -> dict[str, list[bool]]:
    """对容量扫描 + 对抗性两组规则各跑一次 walk-forward，返回逐期命中布尔。"""
    out: dict[str, list[bool]] = {}
    for name, spec in _rule_table().items():
        out[name] = list(spec["wf"](list(series), list(dates), k, warmup))
    for name, spec in _adversarial_table().items():
        if name == "fitted_lattice":
            out[name] = out["lattice_fitted"]
            continue
        if name == "ensemble":
            out[name] = wf_ensemble_vote(list(series), list(dates), k, warmup)
            continue
        out[name] = list(spec["wf"](list(series), list(dates), k, warmup))
    return out


# --------------------------------------------------------------------------- #
# 样本内评估（允许看到被预测的观测本身 —— 「反推」的极限口径）
# --------------------------------------------------------------------------- #
def in_sample_rates(series: Sequence[int], dates: Sequence[date], k: int, warmup: int) -> dict[str, dict[str, Any]]:
    """在整池数据上拟合并**逐期解释**观测答案（允许引用被预测的观测本身）。

    这是使用者直觉里的「反推」口径：观测就是拟合目标，所以查表型规则可以复现答案。
    """
    series = [int(n) for n in series]
    n = len(series)
    out: dict[str, dict[str, Any]] = {}

    hits = []
    for i in range(warmup, n):
        hits.append(series[i] in _uniform_picks(i, k))
    out["uniform"] = {"hits": sum(hits), "evaluated": len(hits), "rate": hit_rate(hits)}

    for order, name in ((1, "markov1"), (2, "markov2")):
        hits = [series[i] in markov_rank_at(series, i, order, k) for i in range(warmup, n)]
        out[name] = {"hits": sum(hits), "evaluated": len(hits), "rate": hit_rate(hits)}

    hits = [series[i] in context_lookup_at(series, i, 2, k) for i in range(warmup, n)]
    out["memorise_context"] = {"hits": sum(hits), "evaluated": len(hits), "rate": hit_rate(hits)}

    table = fit_memorise_index(series)
    hits = [series[i] in [table[i]] for i in range(warmup, n)]
    out["memorise_index"] = {"hits": sum(hits), "evaluated": len(hits), "rate": hit_rate(hits)}

    window = fit_hot_window(series, k)
    hits = [series[i] in hot_picks(series[:i], window["window"], k) for i in range(warmup, n)]
    out["hot_window"] = {
        "hits": sum(hits),
        "evaluated": len(hits),
        "rate": hit_rate(hits),
        "fitted_window": window["window"],
    }

    band = fit_lattice_band(series)
    hits = [
        series[i] in lattice_picks(series[i - 1], band["half_width"], band["offset"], k)
        for i in range(warmup, n)
    ]
    out["lattice_fitted"] = {
        "hits": sum(hits),
        "evaluated": len(hits),
        "rate": hit_rate(hits),
        "fitted_half_width": band["half_width"],
        "fitted_offset": band["offset"],
    }

    fitted_knn = fit_knn(series, k)
    hits = [
        series[i]
        in knn_picks(
            series[:i],
            k,
            neighbours=fitted_knn["neighbours"],
            metric=fitted_knn["metric"],
        )
        for i in range(warmup, n)
    ]
    out["knn"] = {
        "hits": sum(hits),
        "evaluated": len(hits),
        "rate": hit_rate(hits),
        "fitted_neighbours": fitted_knn["neighbours"],
        "fitted_metric": fitted_knn["metric"],
    }
    return out


def capacity_ladder(
    series: Sequence[int], k: int, warmup: int, m_values: Sequence[int]
) -> dict[str, Any]:
    """显式记忆化阶梯：前 m 个被评估期直接背下答案，其余用零参数均匀取号。

    自由参数数 = m（每多背一期就多一个参数）。样本内命中率 = (m + 均匀命中)/N 随 m
    单调升到 100%；样本外始终是「序号从未见过」的均匀基线 —— 这就是「容量 → 样本内 100%，
    样本外不动」的最直接演示。
    """
    series = [int(n) for n in series]
    targets = list(range(warmup, len(series)))
    uniform_hits = [series[i] in _uniform_picks(i, k) for i in targets]
    total = len(targets)
    uniform_rate = (sum(1 for value in uniform_hits if value) / total) if total else None
    rows = []
    for m in m_values:
        memorised = max(0, min(int(m), total))
        hits = [True if index < memorised else uniform_hits[index] for index in range(total)]
        rows.append(
            {
                "memorised_targets": memorised,
                "free_parameters": memorised,
                "in_sample_hits": sum(1 for value in hits if value),
                "in_sample_hit_rate": (sum(1 for value in hits if value) / total) if total else None,
                "out_of_sample_hit_rate": uniform_rate,
                "out_of_sample_note": "样本外期序号从未被背过，退化为零参数均匀基线",
            }
        )
    return {
        "evaluated_targets": total,
        "uniform_rate": uniform_rate,
        "rows": rows,
        "note": (
            "样本内命中率随参数数（= 背下的期数）单调升到 100%；样本外恒为均匀基线 "
            "k/49 的实测值。参数数每加一，样本内就多对一期 —— 但对未见期的信息量为零。"
        ),
    }


# --------------------------------------------------------------------------- #
# 置换零分布（多进程）
# --------------------------------------------------------------------------- #
_WORKER: dict[str, Any] = {}


def _shuffle_series(series: Sequence[int], seed: int) -> list[int]:
    rng = random.Random(seed)
    values = [int(n) for n in series]
    rng.shuffle(values)
    return values


def _init_worker(series, dates, k, warmup) -> None:
    _WORKER["series"] = list(series)
    _WORKER["dates"] = list(dates)
    _WORKER["k"] = int(k)
    _WORKER["warmup"] = int(warmup)


def _perm_worker(perm_index: int) -> dict[str, Any]:
    series = _shuffle_series(_WORKER["series"], PERM_SEED_BASE + perm_index)
    rates = {}
    try:
        result = run_all_rules(series, _WORKER["dates"], _WORKER["k"], _WORKER["warmup"])
        for name, hits in result.items():
            rates[name] = hit_rate(hits)
    except Exception as error:  # pragma: no cover - 让失败可见，不静默
        return {"perm_index": perm_index, "error": repr(error), "rates": {}}
    return {"perm_index": perm_index, "rates": rates}


def run_permutation_null(series, dates, k, warmup, perms, jobs, flush_path=None, flush_every=10):
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    with mp.Pool(
        processes=max(1, int(jobs)),
        initializer=_init_worker,
        initargs=(list(series), list(dates), int(k), int(warmup)),
    ) as pool:
        for done, row in enumerate(pool.imap_unordered(_perm_worker, range(perms)), start=1):
            rows.append(row)
            if (flush_path is not None) and (done % flush_every == 0 or done == perms):
                elapsed = time.perf_counter() - started
                print(f"  [null] {done}/{perms} 置换完成（{elapsed:.0f}s）", flush=True)
                _write_json_atomic(
                    flush_path,
                    {"partial": True, "perms_done": done, "perms_total": perms,
                     "elapsed_seconds": round(elapsed, 1),
                     "rows": sorted(rows, key=lambda r: r["perm_index"])},
                )
    rows.sort(key=lambda r: r["perm_index"])
    return {
        "perms": int(perms),
        "jobs": int(jobs),
        "elapsed_seconds": round(time.perf_counter() - started, 1),
        "rows": rows,
    }


def summarize_null(rates: Sequence[float | None], observed: float | None) -> dict[str, Any]:
    clean = [float(v) for v in rates if v is not None]
    if not clean:
        return {"n": 0, "p_one_sided": None}
    ordered = sorted(clean)
    exceed = sum(1 for v in clean if observed is not None and v >= observed)
    return {
        "n": len(clean),
        "mean": sum(clean) / len(clean),
        "sd": (math.sqrt(sum((v - sum(clean) / len(clean)) ** 2 for v in clean) / len(clean))
               if len(clean) > 1 else 0.0),
        "min": ordered[0],
        "p05": _quantile_sorted(ordered, 0.05),
        "p50": _quantile_sorted(ordered, 0.50),
        "p95": _quantile_sorted(ordered, 0.95),
        "max": ordered[-1],
        "observed": observed,
        "exceed_count": exceed,
        "p_one_sided": ((exceed + 1) / (len(clean) + 1)) if observed is not None else None,
    }


# --------------------------------------------------------------------------- #
# 专项实验
# --------------------------------------------------------------------------- #
def parameter_count_table(sample_size: int, k: int) -> dict[str, Any]:
    """参数数 vs 观测数：马尔可夫各阶与通用点阵。

    每行的 ``observations_used`` 是**该行真正吃进去的观测数**（不跨行套用池内总数）：
    马尔可夫 / 点阵用全池 ``sample_size``；记忆化只吃被评估的 ``sample_size - WARMUP``
    期（前 WARMUP 期没有上一期可用）。``observations_per_parameter`` 与
    ``mean_count_per_cell`` 一律由该行自己的分子分母推出，不写死常数。
    """
    observations = sample_size
    memorise_targets = sample_size - WARMUP
    rows = []
    for order in (1, 2, 3):
        cells = NUM_STATES ** (order + 1)
        free = cells - (NUM_STATES ** order)
        rows.append(
            {
                "name": f"{order} 阶马尔可夫转移表",
                "cells": cells,
                "free_parameters": free,
                "observations": observations,
                "observations_used": observations,
                "observations_per_parameter": observations / free,
                "mean_count_per_cell": observations / cells,
                "note": (
                    "参数比观测多 %.1f 倍，绝大多数单元计数为 0，表由噪声填满"
                    % (free / observations)
                ),
            }
        )
    rows.append(
        {
            "name": "通用点阵（每号一个权重）",
            "cells": NUM_STATES,
            "free_parameters": NUM_STATES,
            "observations": observations,
            "observations_used": observations,
            "observations_per_parameter": observations / NUM_STATES,
            "mean_count_per_cell": observations / NUM_STATES,
            "note": "49 个权重、210 期观测，平均每号约 4.3 次",
        }
    )
    rows.append(
        {
            "name": "点阵带（中心 + 带宽）",
            "cells": 0,
            "free_parameters": 2,
            "observations": observations,
            "observations_used": observations,
            "observations_per_parameter": observations / 2,
            "mean_count_per_cell": None,
            "note": "只 2 个自由参数，容量小到无法拟合噪声",
        }
    )
    rows.append(
        {
            "name": "按期序号记忆（复现每个训练答案）",
            "cells": memorise_targets,
            "free_parameters": memorise_targets,
            "observations": observations,
            "observations_used": memorise_targets,
            "observations_per_parameter": memorise_targets / memorise_targets,
            "mean_count_per_cell": memorise_targets / memorise_targets,
            "note": (
                "只有被评估的 %d 期各背下 1 个答案（前 %d 期没有上一期可用）；"
                "每个参数恰好对 1 个观测 —— 可以完美复现，但下一个序号从未见过"
                % (memorise_targets, WARMUP)
            ),
        }
    )
    return {
        "sample_size": sample_size,
        "evaluated_targets": sample_size - WARMUP,
        "k": k,
        "principle": (
            "要描述 49 路转移，一阶需约 49^2 个单元、二阶需约 49^3 个单元；"
            "本池只有 %d 期观测。参数比数据多一个数量级以上时，拟合出来的表由噪声主导，"
            "对未见期的预测精度必然回到「与 49 路等概率不可区分」。" % sample_size
        ),
        "rows": rows,
    }


def lattice_band_sweep(series: Sequence[int], k: int, w_values: Sequence[int]) -> dict[str, Any]:
    """点阵带宽扫描：中心固定为上一期，扫半宽 w。"""
    series = [int(n) for n in series]
    n = len(series)
    split = max(1, int(n * 0.7))
    train_diffs = [series[i] - series[i - 1] for i in range(1, split)]
    valid_diffs = [series[i] - series[i - 1] for i in range(split, n)]
    all_diffs = [series[i] - series[i - 1] for i in range(1, n)]
    latest_values = [series[i - 1] for i in range(1, n)]

    def coverage(values: Sequence[int], w: int) -> float | None:
        if not values:
            return None
        return sum(1 for d in values if abs(d) <= w) / len(values)

    rows = []
    for w in w_values:
        band_size = 2 * w + 1
        definitional = min(1.0, band_size / NUM_STATES)
        boundary = sum(lattice_band_size(v, w) for v in latest_values) / (len(latest_values) * NUM_STATES)
        valid_coverage = coverage(valid_diffs, w)
        rows.append(
            {
                "half_width": int(w),
                "band_size": band_size,
                "in_sample_coverage": coverage(all_diffs, w),
                "train_coverage": coverage(train_diffs, w),
                "out_of_sample_coverage": valid_coverage,
                "definitional_coverage": definitional,
                "boundary_adjusted_coverage": boundary,
                "selectivity": definitional,
                "ev_observed": (
                    None if valid_coverage is None else expected_value_per_period(band_size, ODDS_DEFAULT, valid_coverage)
                ),
                "ev_definitional": expected_value_per_period(band_size, ODDS_DEFAULT, definitional),
            }
        )
    band = fit_lattice_band(series[:split])
    fitted_train = coverage(train_diffs, band["half_width"])
    fitted_valid = coverage(valid_diffs, band["half_width"])
    fitted = {
        "half_width": band["half_width"],
        "offset": band["offset"],
        "band_size": 2 * band["half_width"] + 1,
        "train_coverage_with_offset": (
            sum(1 for d in train_diffs if abs(d - band["offset"]) <= band["half_width"]) / len(train_diffs)
            if train_diffs else None
        ),
        "out_of_sample_coverage_with_offset": (
            sum(1 for d in valid_diffs if abs(d - band["offset"]) <= band["half_width"]) / len(valid_diffs)
            if valid_diffs else None
        ),
        "definitional_coverage": min(1.0, (2 * band["half_width"] + 1) / NUM_STATES),
        "fitted_on": "训练段（前 70%）",
        "note": "中心偏移 + 半宽均由训练段拟合；样本外只按拟合带的宽度落到观测差值上",
    }
    return {
        "centre": "上一期开奖号（offset = 0）",
        "definitional_formula": "(2w+1) / 49",
        "train_transitions": len(train_diffs),
        "out_of_sample_transitions": len(valid_diffs),
        "rows": rows,
        "fitted_band": fitted,
        "note": (
            "「样本内覆盖率」是把同一批观测差值拿去和固定带宽比；w 越大必然越高，"
            "w >= 24 时定义期望已到 100%（等于把 49 个号全报一遍），"
            "w 到 48 时盖住号码面任意两点最大差值，覆盖率恒为 1。"
        ),
    }


def profitability_block(k: int, odds: float, evaluated: int) -> dict[str, Any]:
    edge = required_absolute_edge(k, odds)
    ev_rows = []
    for m in range(1, NUM_STATES + 1):
        uniform_rate = m / NUM_STATES
        ev_rows.append(
            {
                "numbers": m,
                "stake": m,
                "uniform_hit_rate": uniform_rate,
                "breakeven_hit_rate": breakeven_probability(m, odds),
                "ev_per_period": expected_value_per_period(m, odds, uniform_rate),
            }
        )
    k_table = []
    for count in (1, 3, 5, 10):
        # 逐行用自己的 p0 = m/49 与 p1 = m/47：方差与提升幅度都必须是同一行的
        p0 = count / NUM_STATES
        p1 = breakeven_probability(count, odds)
        k_edge = p1 - p0
        two_sigma_exact = draws_to_detect_two_sigma_exact(p0, p1)
        power_exact = draws_to_detect_exact(p0, p1)
        k_table.append(
            {
                "numbers": count,
                "uniform_hit_rate": p0,
                "breakeven_hit_rate": p1,
                "required_absolute_edge_pp": k_edge * 100.0,
                "draws_2sigma": draws_to_detect_two_sigma(p0, p1),
                "draws_2sigma_exact": two_sigma_exact,
                "draws_80pct_power": draws_to_detect(p0, p1),
                "draws_80pct_power_exact": power_exact,
                "variance_p0_p1": sigma_squared_for_rate(p0),
                "variance_p1_q1": sigma_squared_for_rate(p1),
            }
        )
    return {
        "odds": odds,
        "k": k,
        "evaluated": evaluated,
        "uniform_hit_rate": BASELINE,
        "breakeven_hit_rate": breakeven_probability(k, odds),
        "required_relative_uplift": required_relative_uplift(odds),
        "required_absolute_edge": edge,
        "required_absolute_edge_pp": edge * 100.0,
        "draws_to_establish_80pct_power": draws_to_detect(k / NUM_STATES, breakeven_probability(k, odds)),
        "draws_to_establish_80pct_power_exact": draws_to_detect_exact(k / NUM_STATES, breakeven_probability(k, odds)),
        "draws_to_establish_2sigma": draws_to_detect_two_sigma(k / NUM_STATES, breakeven_probability(k, odds)),
        "draws_to_establish_2sigma_exact": draws_to_detect_two_sigma_exact(
            k / NUM_STATES, breakeven_probability(k, odds)
        ),
        "current_draws": evaluated,
        "table_by_k": k_table,
        "ev_table": ev_rows,
        "z_values": sample_size_z_values(),
        "sample_size_formulas": {
            "two_sigma": "n = 4 · p0·(1−p0) / (p1 − p0)²（双侧 5%，无功效项）",
            "eighty_pct_power": (
                "n = ( z_{1−α/2}·√(p0(1−p0)) + z_{power}·√(p1(1−p1)) )² / (p1 − p0)²，"
                "z_{0.975}=1.9599639845、z_{0.80}=0.8416212336"
            ),
            "per_row": "每行用自己的 p0 = m/49 与 p1 = m/47；禁止跨行复用某个固定 p 的方差",
        },
        "condition": "利润条件：p > m / odds（每期押 m 注、命中概率 p、赔率 odds）",
        "note": (
            "均匀数据下 p = m/49，代入得 EV = m·(odds/49 − 1) < 0，对任意注数 m 成立；"
            "想要翻身必须既有真实的信息优势（提高 p）、又把注数压窄（压低 m）——"
            "而『带足够宽所以样本内必中』恰好是把 m 推到 49，直接摧毁 EV。"
        ),
    }


def adversarial_findings(rows: Sequence[dict[str, Any]], alpha: float = 0.05) -> list[dict[str, Any]]:
    """列出原始 p < alpha 的检验，并用 Holm 逐步下降校正标注是否经得起多重比较。

    ``rows`` 里凡带 ``null_p_one_sided``（置换零分布）或 ``binomial_p_greater``
    （精确二项）的规则都算一个假设；把全部假设放在一起做 Holm，避免「试了 N 个
    规则、挑最小的 p 报喜」。
    """
    tests: list[tuple[dict[str, Any], str, float]] = []
    for row in rows:
        for kind, p in (("置换零分布", row.get("null_p_one_sided")), ("精确二项", row.get("binomial_p_greater"))):
            if p is not None:
                tests.append((row, kind, float(p)))
    total = len(tests)
    ordered = sorted(tests, key=lambda item: item[2])
    passed: set[tuple[int, str]] = set()
    holm_threshold: dict[tuple[int, str], float] = {}
    for rank, (row, kind, p) in enumerate(ordered):
        threshold = alpha / (total - rank)
        holm_threshold[(id(row), kind)] = threshold
        if p <= threshold:
            passed.add((id(row), kind))
        else:
            break  # Holm 逐步下降：一旦不通过，后续更宽松的门槛也不再算通过

    findings = []
    for row, kind, p in tests:
        if p >= alpha:
            continue
        key = (id(row), kind)
        findings.append(
            {
                "rule": row["rule"],
                "family": row.get("family"),
                "test": kind,
                "observed_rate": row.get("out_of_sample_hit_rate"),
                "null_mean": row.get("null_mean"),
                "p": p,
                "hypotheses_tried": total,
                "holm_threshold": holm_threshold.get(key),
                "holm_significant": key in passed,
                "params": row.get("free_parameters"),
                "note": "需要点名：这是低于 0.05 的检验（是否经得起 Holm 见 holm_significant）",
            }
        )
    return sorted(findings, key=lambda item: item["p"])


# --------------------------------------------------------------------------- #
# IO
# --------------------------------------------------------------------------- #
def load_draws(path: str) -> tuple[list[int], list[date]]:
    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(f"开奖数据文件不存在：{file}（backend/data/* 被 .gitignore 忽略）")
    raw = json.loads(file.read_text(encoding="utf-8"))
    raw.sort(key=lambda row: (str(row["draw_date"]), int(row["period"])))
    series = [int(row["special_number"]) for row in raw]
    dates = [date.fromisoformat(str(row["draw_date"])) for row in raw]
    return series, dates


def _write_json_atomic(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _pct(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value * 100:+.{digits}f}%"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="拟合容量 vs 样本外命中率（本池样本内）")
    parser.add_argument("--draws-json", default=str(BACKEND_ROOT / "data" / "draws_70_279.json"))
    parser.add_argument("--stage", choices=["observed", "null", "all"], default="all")
    parser.add_argument("--perms", type=int, default=100)
    parser.add_argument("--jobs", type=int, default=max(1, (mp.cpu_count() or 2) - 1))
    parser.add_argument("--k", type=int, default=K_DEFAULT)
    parser.add_argument("--odds", type=float, default=ODDS_DEFAULT)
    parser.add_argument("--out", default=str(BACKEND_ROOT / "data" / "fit_capacity_analysis.json"))
    args = parser.parse_args()

    series, dates = load_draws(args.draws_json)
    sample_size = len(series)
    k = int(args.k)
    baseline = k / NUM_STATES
    out_path = Path(args.out)

    result: dict[str, Any] = {
        "scope": f"本池已导入 {sample_size} 期样本内",
        "sample_size": sample_size,
        "evaluated": sample_size - WARMUP,
        "k": k,
        "odds": args.odds,
        "baseline": baseline,
        "standard_error": math.sqrt(baseline * (1.0 - baseline) / (sample_size - WARMUP)),
        "seed": SEED,
        "perm_seed_base": PERM_SEED_BASE,
        "warmup": WARMUP,
        "first_period": int(json.loads(Path(args.draws_json).read_text(encoding="utf-8"))[0]["period"]),
        "first_date": dates[0].isoformat(),
        "last_date": dates[-1].isoformat(),
    }

    observed_rules = run_all_rules(series, dates, k, WARMUP)
    insample = in_sample_rates(series, dates, k, WARMUP)

    print(f"本池已导入 {sample_size} 期；可评估 {sample_size - WARMUP} 期；k={k}，随机基线 {baseline:.4%}")
    print("=== 容量扫描（样本内 vs 样本外）===")

    capacity_rows = []
    adversarial_rows = []
    rule_table = _rule_table()
    adversarial_table = _adversarial_table()
    for name, spec in rule_table.items():
        hits = observed_rules[name]
        oos = hit_rate(hits)
        ins = insample.get(name, {})
        row = {
            "rule": name,
            "family": spec["family"],
            "desc": spec["desc"],
            "free_parameters": spec["params"],
            "cells": spec["cells"],
            "memory_capable": spec["memory"],
            "in_sample_hit_rate": ins.get("rate"),
            "in_sample_hits": ins.get("hits"),
            "out_of_sample_hit_rate": oos,
            "out_of_sample_hits": sum(1 for value in hits if value),
            "out_of_sample_evaluated": len(hits),
            "binomial_p_greater": A.binomial_tail_p(
                sum(1 for value in hits if value), len(hits), baseline, alternative="greater"
            ),
        }
        for extra in ("fitted_window", "fitted_half_width", "fitted_offset",
                      "fitted_neighbours", "fitted_metric"):
            if extra in ins:
                row[extra] = ins[extra]
        capacity_rows.append(row)
        print(
            f"{name:20s} 参数={spec['params']:>7d} 样本内={_pct(ins.get('rate'))} "
            f"样本外={_pct(oos)} 命中={sum(1 for v in hits if v)}/{len(hits)}"
        )

    for name, spec in adversarial_table.items():
        hits = observed_rules[name]
        oos = hit_rate(hits)
        adversarial_rows.append(
            {
                "rule": name,
                "family": spec["family"],
                "desc": spec["desc"],
                "free_parameters": spec["params"],
                "out_of_sample_hit_rate": oos,
                "out_of_sample_hits": sum(1 for value in hits if value),
                "out_of_sample_evaluated": len(hits),
                "binomial_p_greater": A.binomial_tail_p(
                    sum(1 for value in hits if value), len(hits), baseline, alternative="greater"
                ),
            }
        )

    result["capacity_sweep"] = {
        "baseline": baseline,
        "rows": sorted(capacity_rows, key=lambda row: row["free_parameters"]),
        "notes": [
            "样本内 = 在整池数据上拟合并逐期解释观测答案（允许引用被预测的观测本身，即『反推』口径）。",
            "样本外 = 严格 walk-forward：预测第 i 期只读 series[:i]，逐期重估模型。",
            "匹配置换零分布 = 打乱全池号码位置 100+ 次，每次重跑同一套拟合+预测流程。",
        ],
    }

    print("\n=== 参数数 vs 观测数 ===")
    param_block = parameter_count_table(sample_size, k)
    for row in param_block["rows"]:
        print(
            f"{row['name']:28s} 单元={row['cells']:>7d} 自由参数={row['free_parameters']:>7d} "
            f"每参数观测={row['observations_per_parameter']:.5f}"
        )
    result["parameter_counts"] = param_block

    print("\n=== 显式记忆化阶梯（容量 → 样本内 100%）===")
    ladder = capacity_ladder(series, k, WARMUP, LADDER_M_VALUES)
    for row in ladder["rows"]:
        print(
            f"背下 {row['memorised_targets']:>3d} 期 参数={row['free_parameters']:>3d} "
            f"样本内={_pct(row['in_sample_hit_rate'])} 样本外={_pct(row['out_of_sample_hit_rate'])}"
        )
    result["capacity_ladder"] = ladder

    print("\n=== 点阵带宽扫描 ===")
    sweep = lattice_band_sweep(series, k, list(range(1, 25)) + [30, 40, 48])
    for row in sweep["rows"]:
        print(
            f"w={row['half_width']:>2d} 带宽={row['band_size']:>2d} "
            f"样本内覆盖率={_pct(row['in_sample_coverage'], 1)} "
            f"样本外覆盖率={_pct(row['out_of_sample_coverage'], 1)} "
            f"定义期望={_pct(row['definitional_coverage'], 1)}"
        )
    result["lattice_sweep"] = sweep

    print("\n=== 盈利条件 ===")
    profit = profitability_block(k, args.odds, sample_size - WARMUP)
    print(
        f"相对提升门槛={profit['required_relative_uplift']:.4%}；"
        f"绝对命中率门槛=+{profit['required_absolute_edge_pp']:.4f}pp；"
        f"80% 功效需 {profit['draws_to_establish_80pct_power']} 期，"
        f"2σ 需 {profit['draws_to_establish_2sigma']} 期"
    )
    result["profitability"] = profit

    print("\n=== 对抗性尝试（样本外）===")
    for row in sorted(adversarial_rows, key=lambda r: -(r["out_of_sample_hit_rate"] or 0)):
        print(
            f"{row['rule']:22s} 样本外={_pct(row['out_of_sample_hit_rate'])} "
            f"命中={row['out_of_sample_hits']}/{row['out_of_sample_evaluated']} "
            f"二项p={row['binomial_p_greater']:.4f}"
        )
    result["adversarial"] = {
        "rows": adversarial_rows,
        "hypotheses_tried": len(adversarial_rows) + len(capacity_rows),
        "findings": [],
    }

    if args.stage in ("null", "all"):
        print(f"\n=== 置换零分布（{args.perms} 次，{args.jobs} 进程）===")
        null = run_permutation_null(
            series,
            dates,
            k,
            WARMUP,
            args.perms,
            args.jobs,
            flush_path=out_path.with_name(out_path.stem + ".partial" + out_path.suffix),
            flush_every=10,
        )
        for row in capacity_rows:
            rates = [r["rates"].get(row["rule"]) for r in null["rows"]]
            summary = summarize_null(rates, row["out_of_sample_hit_rate"])
            row["null"] = summary
            row["null_p_one_sided"] = summary.get("p_one_sided")
            row["null_mean"] = summary.get("mean")
            row["null_sd"] = summary.get("sd")
        for row in adversarial_rows:
            rates = [r["rates"].get(row["rule"]) for r in null["rows"]]
            summary = summarize_null(rates, row["out_of_sample_hit_rate"])
            row["null"] = summary
            row["null_p_one_sided"] = summary.get("p_one_sided")
            row["null_mean"] = summary.get("mean")
            row["null_sd"] = summary.get("sd")
        print("\n=== 容量扫描终表（含置换 p）===")
        for row in sorted(capacity_rows, key=lambda r: r["free_parameters"]):
            print(
                f"{row['rule']:20s} 参数={row['free_parameters']:>7d} "
                f"样本内={_pct(row['in_sample_hit_rate'])} 样本外={_pct(row['out_of_sample_hit_rate'])} "
                f"零点均值={_pct(row['null_mean'])} 单侧p={row['null_p_one_sided']}"
            )
        uniform_null = next((row.get("null") for row in capacity_rows if row["rule"] == "uniform"), None)
        if uniform_null is not None:
            for row in ladder["rows"]:
                row["null"] = uniform_null
                row["null_p_one_sided"] = uniform_null.get("p_one_sided")
                row["null_mean"] = uniform_null.get("mean")
                row["null_sd"] = uniform_null.get("sd")
                row["null_note"] = "阶梯样本外退化为均匀基线，零分布沿用 uniform 规则"
            result["capacity_ladder"]["rows"] = ladder["rows"]
        findings = adversarial_findings(adversarial_rows, alpha=0.05)
        result["adversarial"]["findings"] = findings
        result["null"] = {
            "perms": null["perms"],
            "jobs": null["jobs"],
            "elapsed_seconds": null["elapsed_seconds"],
            "rows": null["rows"],
        }
        print(f"\n对抗性项里 p<0.05 的检验共 {len(findings)} 个")
        for finding in findings:
            print(f"  - {finding['rule']}（{finding['test']}）p={finding['p']:.4f}")

    result["capacity_sweep"]["rows"] = sorted(capacity_rows, key=lambda row: row["free_parameters"])
    result["adversarial"]["rows"] = adversarial_rows
    result["headline_chart"] = {
        "title": "容量阶梯：样本内命中率 vs 样本外命中率",
        "x_label": "自由参数数（对数刻度）",
        "y_label": "命中率（%）",
        "baseline": round(baseline * 100.0, 4),
        "uniform_series": [
            {
                "rule": row["rule"],
                "free_parameters": row["free_parameters"],
                "in_sample_hit_rate": (
                    None if row["in_sample_hit_rate"] is None else round(row["in_sample_hit_rate"] * 100.0, 4)
                ),
                "out_of_sample_hit_rate": round(row["out_of_sample_hit_rate"] * 100.0, 4),
            }
            for row in sorted(result["capacity_sweep"]["rows"], key=lambda r: r["free_parameters"])
        ],
        "memorisation_series": [
            {
                "memorised_targets": row["memorised_targets"],
                "free_parameters": row["free_parameters"],
                "in_sample_hit_rate": (row["in_sample_hit_rate"] or 0.0) * 100.0,
                "out_of_sample_hit_rate": (row["out_of_sample_hit_rate"] or 0.0) * 100.0,
            }
            for row in ladder["rows"]
        ],
    }
    result["notes"] = [
        result["scope"] + "；禁止升格为任何全量 / 市场结论。",
        "样本内命中率可以随容量升到 100%（查表 / 记忆化），样本外一律回到 10/49 附近。",
        "置换零分布与观测走同一套拟合 + 预测流程，是「纯噪声能刷到多高」的标尺。",
        "本脚本只读，不改动 DEFAULT_SETTINGS 与 recommend() 引擎。",
    ]
    _write_json_atomic(out_path, result)
    print(f"\n[written] {out_path}")


if __name__ == "__main__":
    main()
