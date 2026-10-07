r"""本池 210 期样本的「统计功效上限」分析（纯标准库，不依赖 numpy/scipy）。

用途：回答「就这两百多期数据，能不能调优出超过随机的优势，越高越好」。

口径铁律：
- 所有结论只针对 **本池已导入的 N 期样本内**，不得升格为任何全量 / 市场结论。
- 样本不足以支持结论时，显式打印「数据不足」，绝不用 0 值或编造结论冒充。

运行（离线，不碰数据库）：
    cd backend
    .\.venv\Scripts\python.exe scripts\lottery_power_analysis.py `
        --draws-json data/draws_70_279.json --pick-count 10
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

NUMBER_MIN = 1
NUMBER_MAX = 49
NUMBERS: tuple[int, ...] = tuple(range(NUMBER_MIN, NUMBER_MAX + 1))
ZODIAC_STEP = 12


# --------------------------------------------------------------------------- #
# 基础数学（标准库实现；与 scipy 的 chi2.sf / norm.cdf / binom 同口径）
# --------------------------------------------------------------------------- #
def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _gamma_lower_reg(s: float, x: float) -> float:
    """正则化下不完全 Gamma P(s, x)（级数展开）。"""
    if x <= 0.0:
        return 0.0
    if x < s + 1.0:
        ap = s
        total = 1.0 / s
        delta = total
        for _ in range(10_000):
            ap += 1.0
            delta *= x / ap
            total += delta
            if abs(delta) < abs(total) * 1e-15:
                break
        return total * math.exp(-x + s * math.log(x) - math.lgamma(s))
    return 1.0 - _gamma_upper_reg(s, x)


def _gamma_upper_reg(s: float, x: float) -> float:
    """正则化上不完全 Gamma Q(s, x)（连分数展开）。"""
    if x <= 0.0:
        return 1.0
    tiny = 1e-300
    b = x + 1.0 - s
    c = 1.0 / tiny
    d = 1.0 / b if b != 0 else 1.0 / tiny
    h = d
    for i in range(1, 10_000):
        an = -i * (i - s)
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
        if abs(delta - 1.0) < 1e-15:
            break
    return math.exp(-x + s * math.log(x) - math.lgamma(s)) * h


def chi2_sf(stat: float, df: int) -> float:
    """卡方分布上尾概率 P(X > stat)，df 自由度。"""
    if stat <= 0:
        return 1.0
    return _gamma_upper_reg(df / 2.0, stat / 2.0)


def _log_binom_pmf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 0.0 if k == 0 else -math.inf
    if p >= 1.0:
        return 0.0 if k == n else -math.inf
    return (
        math.lgamma(n + 1)
        - math.lgamma(k + 1)
        - math.lgamma(n - k + 1)
        + k * math.log(p)
        + (n - k) * math.log(1.0 - p)
    )


def binom_two_sided_p(k: int, n: int, p: float) -> float:
    """精确二项双尾 p 值（按概率质量 <= 观测质量的集合求和，mid-p-free）。"""
    obs = _log_binom_pmf(k, n, p)
    threshold = obs + 1e-9
    total = 0.0
    for i in range(n + 1):
        lp = _log_binom_pmf(i, n, p)
        if lp <= threshold:
            total += math.exp(lp)
    return min(1.0, total)


def benjamini_hochberg(pvalues: Sequence[float], alpha: float = 0.05) -> dict[str, Any]:
    """BH 法：返回调整后 p 值与被判为显著（FDR <= alpha）的个数。"""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted = [1.0] * m
    running = 1.0
    for rank in range(m - 1, -1, -1):
        idx = order[rank]
        value = pvalues[idx] * m / (rank + 1)
        running = min(running, value)
        adjusted[idx] = min(1.0, running)
    significant = [i for i in range(m) if adjusted[i] <= alpha]
    return {
        "alpha": alpha,
        "adjusted": adjusted,
        "significant_count": len(significant),
        "significant_indices": significant,
    }


def lag_k_autocorr(series: Sequence[float], lag: int) -> float:
    n = len(series)
    if n <= lag + 1:
        return 0.0
    mean = sum(series) / n
    denom = sum((x - mean) ** 2 for x in series)
    if denom == 0:
        return 0.0
    num = sum((series[t] - mean) * (series[t + lag] - mean) for t in range(n - lag))
    return num / denom


def zodiac_group(number: int) -> int:
    """同肖分组键：n 与 n±12 同肖，1..49 共 12 组（与 services.lottery 恒等）。"""
    return (number - 1) % ZODIAC_STEP


def runs_test(series: Sequence[float]) -> dict[str, Any]:
    """Wald–Wolfowitz 游程检验（以中位数为分界，中位数取两中间值平均 → 无并列）。"""
    ordered = sorted(series)
    n = len(ordered)
    mid = (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0 if n % 2 == 0 else ordered[n // 2]
    bits = [1 if x > mid else 0 for x in series if x != mid]
    n1 = sum(bits)
    n2 = len(bits) - n1
    if n1 == 0 or n2 == 0:
        return {"runs": None, "z": None, "p": None, "n_above": n1, "n_below": n2}
    runs = 1 + sum(1 for i in range(1, len(bits)) if bits[i] != bits[i - 1])
    expected = 2.0 * n1 * n2 / (n1 + n2) + 1.0
    var = (
        2.0 * n1 * n2 * (2.0 * n1 * n2 - n1 - n2)
        / ((n1 + n2) ** 2 * (n1 + n2 - 1))
    )
    z = (runs - expected) / math.sqrt(var) if var > 0 else 0.0
    p = 2.0 * (1.0 - normal_cdf(abs(z)))
    return {
        "median_cut": mid,
        "runs": runs,
        "expected_runs": expected,
        "z": z,
        "p": p,
        "n_above": n1,
        "n_below": n2,
    }


def transition_chi2(groups: Sequence[int], lag: int, n_states: int = ZODIAC_STEP) -> float:
    """生肖组(lag 步转移) 的卡方统计量（12×12 转移矩阵）。"""
    n = len(groups)
    if n <= lag:
        return 0.0
    matrix = [[0] * n_states for _ in range(n_states)]
    for t in range(n - lag):
        matrix[groups[t]][groups[t + lag]] += 1
    row_totals = [sum(row) for row in matrix]
    col_totals = [sum(matrix[i][j] for i in range(n_states)) for j in range(n_states)]
    total = sum(row_totals)
    if total == 0:
        return 0.0
    stat = 0.0
    for i in range(n_states):
        for j in range(n_states):
            expected = row_totals[i] * col_totals[j] / total
            if expected > 0:
                stat += (matrix[i][j] - expected) ** 2 / expected
    return stat


# --------------------------------------------------------------------------- #
# 载入
# --------------------------------------------------------------------------- #
def load_specials(path: str) -> list[int]:
    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(
            f"开奖数据文件不存在：{file}\n"
            "本仓 backend/data/* 被 .gitignore 忽略，换机器需先从运行中的后端导出一份："
            "见 scripts/tune_walk_forward.py 的 load_draws 错误信息里的导出命令。"
        )
    raw = json.loads(file.read_text(encoding="utf-8"))
    rows = sorted(
        ({"period": int(r["period"]), "draw_date": str(r["draw_date"]), "n": int(r["special_number"])} for r in raw),
        key=lambda r: (r["draw_date"], r["period"]),
    )
    return [r["n"] for r in rows]


# --------------------------------------------------------------------------- #
# 各项检验
# --------------------------------------------------------------------------- #
def uniformity(specials: Sequence[int]) -> dict[str, Any]:
    n = len(specials)
    counts = Counter(specials)
    expected = n / len(NUMBERS)
    stat = sum((counts.get(k, 0) - expected) ** 2 / expected for k in NUMBERS)
    df = len(NUMBERS) - 1
    p = chi2_sf(stat, df)
    ordered = sorted(((counts.get(k, 0), k) for k in NUMBERS), reverse=True)
    return {
        "sample_size": n,
        "chi2": stat,
        "df": df,
        "p_value": p,
        "expected_count_per_number": expected,
        "observed_min": ordered[-1][0],
        "observed_min_number": ordered[-1][1],
        "observed_max": ordered[0][0],
        "observed_max_number": ordered[0][1],
    }


def per_number_binomial(specials: Sequence[int], alpha: float = 0.05) -> dict[str, Any]:
    n = len(specials)
    counts = Counter(specials)
    p0 = 1.0 / len(NUMBERS)
    pvalues = [binom_two_sided_p(counts.get(k, 0), n, p0) for k in NUMBERS]
    bh = benjamini_hochberg(pvalues, alpha)
    bonferroni_threshold = alpha / len(NUMBERS)
    worst = min(range(len(pvalues)), key=lambda i: pvalues[i])
    return {
        "n": n,
        "expected_per_number": n * p0,
        "min_pvalue": pvalues[worst],
        "min_pvalue_number": NUMBERS[worst],
        "min_pvalue_count": counts.get(NUMBERS[worst], 0),
        "bonferroni_threshold": bonferroni_threshold,
        "bonferroni_significant_count": sum(1 for p in pvalues if p <= bonferroni_threshold),
        "bh_significant_count": bh["significant_count"],
        "bh_significant_numbers": [NUMBERS[i] for i in bh["significant_indices"]],
        # 最偏离的 5 个号码（样本内观察，不构成「更热」结论）
        "top_deviations": [
            {
                "number": NUMBERS[i],
                "count": counts.get(NUMBERS[i], 0),
                "p_value": pvalues[i],
                "bh_adjusted_p": bh["adjusted"][i],
            }
            for i in sorted(range(len(NUMBERS)), key=lambda i: pvalues[i])[:5]
        ],
    }


def _same_group_rate(groups: Sequence[int], lag: int) -> tuple[float, int, int]:
    hits = sum(1 for t in range(len(groups) - lag) if groups[t] == groups[t + lag])
    total = len(groups) - lag
    return (hits / total if total else 0.0), hits, total


def _same_group_expected() -> float:
    sizes = Counter(zodiac_group(k) for k in NUMBERS)
    total = len(NUMBERS)
    return sum((c / total) ** 2 for c in sizes.values())


def serial_dependence(specials: Sequence[int], perm_rounds: int = 2000, seed: int = 20261007) -> dict[str, Any]:
    n = len(specials)
    specials_f = [float(x) for x in specials]
    groups = [zodiac_group(x) for x in specials]
    diffs_abs = [abs(specials[t + 1] - specials[t]) for t in range(n - 1)]

    rng = random.Random(seed)
    p0_same = _same_group_expected()
    out: dict[str, Any] = {"sample_size": n}

    # 1) 序列自相关（特码本身 / |相邻差|）
    out["autocorr_special"] = {}
    for lag in (1, 2, 3):
        r = lag_k_autocorr(specials_f, lag)
        se = 1.0 / math.sqrt(n)
        z = r / se
        out["autocorr_special"][f"lag{lag}"] = {
            "r": r,
            "z": z,
            "p_two_sided": 2.0 * (1.0 - normal_cdf(abs(z))),
        }
    out["autocorr_absdiff"] = {}
    for lag in (1, 2):
        r = lag_k_autocorr([float(x) for x in diffs_abs], lag)
        se = 1.0 / math.sqrt(len(diffs_abs))
        z = r / se
        out["autocorr_absdiff"][f"lag{lag}"] = {
            "r": r,
            "z": z,
            "p_two_sided": 2.0 * (1.0 - normal_cdf(abs(z))),
        }

    # 2) 游程检验（时间顺序随机性）
    out["runs_test"] = runs_test(specials_f)

    # 3) 同肖复现（lag1 / lag2）—— 期望 = Σ p_g^2（组大小不均，非 1/12）
    out["same_zodiac"] = {}
    for lag in (1, 2):
        rate, hits, total = _same_group_rate(groups, lag)
        p = binom_two_sided_p(hits, total, p0_same)
        out["same_zodiac"][f"lag{lag}"] = {
            "observed_rate": rate,
            "expected_rate": p0_same,
            "hits": hits,
            "trials": total,
            "p_value": p,
        }

    # 3b) 同肖但非重号（剔除与上期完全相同的号码）
    hits = 0
    trials = 0
    for t in range(n - 1):
        if specials[t + 1] == specials[t]:
            continue
        trials += 1
        if groups[t + 1] == groups[t]:
            hits += 1
    p_cond = p0_same  # 近似：以「同肖」边缘率作零假设
    out["same_zodiac_not_repeat"] = {
        "observed_rate": hits / trials if trials else None,
        "expected_rate": p_cond,
        "hits": hits,
        "trials": trials,
        "p_value": binom_two_sided_p(hits, trials, p_cond) if trials else None,
    }

    # 4) 重号率（上期特码重复出现）
    repeat_hits = sum(1 for t in range(n - 1) if specials[t + 1] == specials[t])
    out["repeat_number"] = {
        "observed_rate": repeat_hits / (n - 1),
        "expected_rate": 1.0 / len(NUMBERS),
        "hits": repeat_hits,
        "trials": n - 1,
        "p_value": binom_two_sided_p(repeat_hits, n - 1, 1.0 / len(NUMBERS)),
    }

    # 5) 生肖 lag 转移矩阵卡方 + 置换检验（期望频次低，用置换而非渐近卡方）
    for lag in (1, 2):
        stat = transition_chi2(groups, lag)
        exceed = 0
        shuffled = list(groups)
        for _ in range(perm_rounds):
            rng.shuffle(shuffled)
            if transition_chi2(shuffled, lag) >= stat:
                exceed += 1
        out[f"zodiac_transition_lag{lag}"] = {
            "chi2": stat,
            "df": (ZODIAC_STEP - 1) ** 2,
            "perm_rounds": perm_rounds,
            "p_value": (exceed + 1) / (perm_rounds + 1),
        }
    return out


# --------------------------------------------------------------------------- #
# 功效 / 可检测效应
# --------------------------------------------------------------------------- #
def power_block(
    sample_size: int,
    pick_count: int,
    pool_size: float | None = None,
    baseline_rate: float | None = None,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """可检测 Δ 与功效。

    ``baseline_rate`` 直接给定无技能基线（优先）；``pool_size`` 给定时用
    pick_count / pool_size（引擎只能在候选池内取号）；都不给则用产品口径
    pick_count / 49。
    """
    total = NUMBER_MAX - NUMBER_MIN + 1
    p = pick_count / total
    baseline_source = "pick_count / 49（产品口径随机参考）"
    if baseline_rate is not None:
        p = baseline_rate
        baseline_source = "从候选池均匀随机取 k 个的无技能基线 E[k/M·1(实际在池内)]"
    elif pool_size is not None:
        p = pick_count / pool_size
        baseline_source = f"pick_count / {pool_size:.3f}（池内随机基线）"
    se = math.sqrt(p * (1.0 - p) / sample_size)
    z_crit = 1.959963984540054  # 双侧 alpha=0.05

    def power_for(delta: float) -> float:
        z_eff = delta / se
        return (1.0 - normal_cdf(z_crit - z_eff)) + normal_cdf(-z_crit - z_eff)

    def required_n(delta: float) -> int:
        # 双侧 2σ 可检测：2*sqrt(p(1-p)/N) <= delta
        return math.ceil(4.0 * p * (1.0 - p) / (delta * delta))

    z_power = 0.8416212335729143  # 单侧 80% 功效

    def required_n_80(delta: float) -> int:
        # 双侧 alpha=0.05 + 80% 功效：N >= (z_{a/2}+z_{power})^2 * p(1-p) / delta^2
        return math.ceil(
            ((z_crit + z_power) ** 2) * p * (1.0 - p) / (delta * delta)
        )

    return {
        "baseline_hit_rate": p,
        "baseline_source": baseline_source,
        "sample_size": sample_size,
        "pick_count": pick_count,
        "standard_error": se,
        "detectable_delta_2sigma": 2.0 * se,
        "detectable_delta_1sigma": se,
        "required_n": {
            "+1%": required_n(0.01),
            "+2%": required_n(0.02),
            "+5%": required_n(0.05),
            "+10%": required_n(0.10),
        },
        "required_n_80pct_power": {
            "+1%": required_n_80(0.01),
            "+2%": required_n_80(0.02),
            "+5%": required_n_80(0.05),
            "+10%": required_n_80(0.10),
        },
        "power_at_current_n": {
            "+1%": power_for(0.01),
            "+2%": power_for(0.02),
            "+5%": power_for(0.05),
            "+10%": power_for(0.10),
        },
    }


def in_pool_baseline(specials: Sequence[int], pick_count: int) -> dict[str, Any]:
    """exclude_repeat_zodiac=true 时「从候选池均匀随机取 k 个」的无技能基线。

    P(命中) = E[ k / M_t · 1(实际特码落在池内) ]；M_t = 49 − 同肖组大小。
    实际特码落在池外时该期必不命中（不乘 1(·) 会把池外期也按 k/M 记分，高估基线）。
    """
    sizes = Counter(zodiac_group(k) for k in NUMBERS)
    rates: list[float] = []
    pool_sizes: list[int] = []
    in_pool = 0
    for t in range(len(specials) - 1):
        group = zodiac_group(specials[t])
        m = len(NUMBERS) - sizes[group]
        pool_sizes.append(m)
        if zodiac_group(specials[t + 1]) == group:
            rates.append(0.0)
        else:
            rates.append(pick_count / m)
            in_pool += 1
    return {
        "evaluated": len(rates),
        "mean_pool_size": sum(pool_sizes) / len(pool_sizes),
        "actual_in_pool_rate": in_pool / len(rates),
        "mean_in_pool_random_rate": sum(rates) / len(rates),
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="本池样本统计功效上限分析")
    parser.add_argument("--draws-json", default="data/draws_70_279.json")
    parser.add_argument("--pick-count", type=int, default=10)
    parser.add_argument("--perm-rounds", type=int, default=2000)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    specials = load_specials(args.draws_json)
    n = len(specials)

    report: dict[str, Any] = {
        "scope": f"本池已导入 {n} 期样本内",
        "sample_size": n,
        "pick_count": args.pick_count,
        "uniformity": uniformity(specials),
        "per_number": per_number_binomial(specials),
        "serial": serial_dependence(specials, perm_rounds=args.perm_rounds),
    }
    pool = in_pool_baseline(specials, args.pick_count)
    evaluated = n - 2  # 回测从第 3 期起（min_prior_draws=2）
    report["in_pool_baseline"] = {**pool, "backtest_evaluated": evaluated}
    report["power_product_baseline"] = power_block(evaluated, args.pick_count)
    report["power_in_pool_baseline"] = power_block(
        evaluated,
        args.pick_count,
        baseline_rate=pool["mean_in_pool_random_rate"],
    )

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"\n[written] {args.out}")


if __name__ == "__main__":
    main()
