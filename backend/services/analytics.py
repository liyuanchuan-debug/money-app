"""样本内统计分析 —— 纯函数实现，不访问数据库（不依赖 repository / fastapi）。

口径铁律（与项目规则一致，违反即为缺陷）：
- **所有统计只针对「本池已导入的 N 期数据」这个样本**，不描述更大范围的历史，
  也不得升格为任何全量 / 市场结论；每个返回值都带 ``scope`` 与 ``sample_size``。
- 样本量为 0 或某项分析样本过小时，显式返回 ``data_status = INSUFFICIENT``
  与 ``data_status_label = 数据不足``，并附 ``notes`` 说明，绝不用 0 值冒充结论。
- 落库 / 传输的枚举一律英文码（生肖 RAT…PIG、波动 small/normal/big、号码 1-49），
  中文只出现在展示标签（``*_label``）与提示文案里。
- 波动分类与推荐完全复用 ``services.lottery``（``classify_wave`` / ``recommend``），
  本模块不重新定义任何波动 / 注数 / 角色语义。
- 所有函数都把入参按 ``(draw_date, period)`` 升序重排后再计算，与接口「最新在前」
  的返回顺序解耦，避免调用方顺序假设。
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime
from typing import Any, Iterable, Sequence

from services.lottery import (
    DEFAULT_AVOID_COLD_DAYS,
    DEFAULT_SETTINGS,
    DEFAULT_TREND_WINDOW,
    MODE_SINGLE,
    NUMBER_MAX,
    NUMBER_MIN,
    PICK_STRATEGY_SCORE_TOP,
    PICK_STRATEGY_WAVE_ROUND,
    TREND_BIAS_COLD,
    TREND_BIAS_HOT,
    TREND_BIAS_LABELS,
    TREND_BIAS_MID,
    TREND_BIAS_NEUTRAL,
    TREND_BIASES,
    WAVE_LABELS,
    WAVE_ORDER,
    clamp_score_weights,
    clamp_settings,
    classify_wave,
    derive_big_min,
    recommend,
    zodiac_numbers as same_group_numbers,
)
from services.mark_six import (
    ZODIAC_LABELS,
    ZODIAC_ORDER,
    zodiac_label,
    zodiac_of,
    zodiac_table,
)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
NUMBERS: tuple[int, ...] = tuple(range(NUMBER_MIN, NUMBER_MAX + 1))
ZODIACS: tuple[str, ...] = tuple(ZODIAC_ORDER)

DATA_STATUS_OK = "OK"
DATA_STATUS_INSUFFICIENT = "INSUFFICIENT"
DATA_STATUS_LABELS: dict[str, str] = {
    DATA_STATUS_OK: "样本可用",
    DATA_STATUS_INSUFFICIENT: "数据不足",
}

# 回测起步所需的最少前置期数（至少要有「前一期」才能算上一期波动）
MIN_PRIOR_DRAWS = 2
# 单号码期望出现次数 >= 10 次（即 490 期）才谈得上支持强结论
STRONG_CONCLUSION_MIN_DRAWS = 490
# 生肖走势默认观察窗口（最近 N 期）
DEFAULT_RECENT_WINDOW = 12

# 参数扫描默认轴（方案 A：样本内对照，不自动写回设置）
# 网格 = bias × window × avoid_cold ≈ 4 × 4 × 2 = 32 组；其余旋钮沿用已保存设置
SWEEP_TREND_BIASES: tuple[str, ...] = (
    TREND_BIAS_NEUTRAL,
    TREND_BIAS_HOT,
    TREND_BIAS_MID,
    TREND_BIAS_COLD,
)
SWEEP_TREND_WINDOWS: tuple[int, ...] = (20, 30, 60, 100, 0)
SWEEP_AVOID_COLD_ENABLED: tuple[bool, ...] = (False, True)

# 回测判定码（英文落库/传输；汉字只出现在 label / text）
VERDICT_INSUFFICIENT = "insufficient"
VERDICT_NOISE = "noise"
VERDICT_BEYOND = "beyond"
VERDICT_LABELS: dict[str, str] = {
    VERDICT_INSUFFICIENT: "数据不足",
    VERDICT_NOISE: "差值落在抽样噪声内 · 不能证明优于随机",
    VERDICT_BEYOND: "差值超过 2 倍标准误 · 仍不构成承诺",
}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def normalize_draws(draws: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把任意顺序的期记录整理成升序、带生肖的规范序列。

    只保留每期特码与由开奖日推导的生肖（中文标签仅供展示）。
    缺少特码 / 号码越界的行直接跳过，不抛异常。
    """
    normalized: list[dict[str, Any]] = []
    for draw in draws:
        raw_number = draw.get("special_number")
        if raw_number is None:
            continue
        number = int(raw_number)
        if not NUMBER_MIN <= number <= NUMBER_MAX:
            continue
        draw_date = _as_date(draw["draw_date"])
        code = zodiac_of(number, draw_date)
        normalized.append(
            {
                "period": int(draw["period"]),
                "draw_date": draw_date,
                "special_number": number,
                "zodiac": code,
                "zodiac_label": zodiac_label(code),
            }
        )
    normalized.sort(key=lambda item: (item["draw_date"], item["period"]))
    return normalized


def _scope(sample_size: int) -> str:
    """把每条结论锁死在本池样本内（禁止升格为全量 / 市场口径）。"""
    return f"本池已导入 {sample_size} 期数据内"


def _envelope(sample_size: int) -> dict[str, Any]:
    code = DATA_STATUS_OK if sample_size > 0 else DATA_STATUS_INSUFFICIENT
    return {
        "scope": _scope(sample_size),
        "sample_size": sample_size,
        "data_status": code,
        "data_status_label": DATA_STATUS_LABELS[code],
    }


def backtest_verdict_payload(
    *,
    evaluated: int,
    hit_rate: float | None,
    baseline: float,
    difference: float | None,
    standard_error: float | None,
) -> dict[str, Any]:
    """回测判定（与前端 ``backtestVerdict`` 同口径，供 sweep / 单次回测共用）。

    规则：``|命中率 − 随机参考| ≤ 2 × 抽样标准误`` → ``noise``（无法证明优于随机）；
    超过 → ``beyond``（仍不构成承诺）；缺数 → ``insufficient``。
    返回体含英文 ``kind`` + 汉字 ``label`` / ``text``；禁止把 beyond 包装成正面结论。
    """
    if (
        evaluated <= 0
        or hit_rate is None
        or difference is None
        or standard_error is None
    ):
        return {
            "kind": VERDICT_INSUFFICIENT,
            "label": VERDICT_LABELS[VERDICT_INSUFFICIENT],
            "text": (
                "数据不足：可评估期数不足，无法把引擎命中率与随机参考值做比较。"
            ),
            "within_noise": True,
        }
    if abs(difference) <= 2 * standard_error:
        return {
            "kind": VERDICT_NOISE,
            "label": VERDICT_LABELS[VERDICT_NOISE],
            "text": (
                f"命中率 {hit_rate:.2%} 与随机参考值 {baseline:.2%} 相差 "
                f"{difference:+.2%}，在 {evaluated} 期样本下抽样标准误约 "
                f"{standard_error:.2%}；差值落在抽样噪声内"
                f"（未超过 2 倍标准误 ≈ {2 * standard_error:.2%}），"
                "目前无法证明该策略优于随机。"
            ),
            "within_noise": True,
        }
    return {
        "kind": VERDICT_BEYOND,
        "label": VERDICT_LABELS[VERDICT_BEYOND],
        "text": (
            f"命中率 {hit_rate:.2%} 与随机参考值 {baseline:.2%} 相差 "
            f"{difference:+.2%}，在 {evaluated} 期样本下标准差约 "
            f"{standard_error:.2%}；差值超过 2 倍标准误"
            f"（≈ {2 * standard_error:.2%}），但单一小样本内的偏离仍可能由其他因素造成，"
            "不构成对未来命中能力的任何承诺。"
        ),
        "within_noise": False,
    }


def _empty_note() -> list[str]:
    return ["数据不足：本池尚未导入任何开奖数据，无法计算统计量。"]


def _expected_zodiac_rates(
    series: Sequence[dict[str, Any]]
) -> tuple[dict[str, float | None], int]:
    """按每期开奖日的农历年生肖表，算出各生肖的期望出现率。

    49 号码并非均分给 12 生肖（含 5 个号码的生肖期望更高），所以直接用 1/12
    会失真。这里对每期取该期生肖表下各生肖号码数 / 49，再对样本求平均。
    """
    totals = {code: 0.0 for code in ZODIACS}
    mapped = 0
    total_numbers = NUMBER_MAX - NUMBER_MIN + 1
    for draw in series:
        table = zodiac_table(draw["draw_date"])
        if not table:
            continue
        mapped += 1
        for code in ZODIACS:
            totals[code] += len(table.get(code, [])) / total_numbers
    if mapped == 0:
        return {code: None for code in ZODIACS}, 0
    return {code: totals[code] / mapped for code in ZODIACS}, mapped


def _rate(count: int, total: int) -> float | None:
    return (count / total) if total else None


def _breakdown(
    results: Sequence[dict[str, Any]], key: str
) -> dict[str, Any]:
    """按波动类型聚合命中率（保持 WAVE_ORDER 顺序，空桶也返回）。"""
    buckets = {
        wave: {"type": wave, "label": WAVE_LABELS[wave], "evaluated": 0, "hits": 0, "hit_rate": None}
        for wave in WAVE_ORDER
    }
    for row in results:
        wave = row.get(key)
        if wave not in buckets:
            continue
        buckets[wave]["evaluated"] += 1
        if row["hit"]:
            buckets[wave]["hits"] += 1
    for bucket in buckets.values():
        bucket["hit_rate"] = _rate(bucket["hits"], bucket["evaluated"])
    return {"items": [buckets[wave] for wave in WAVE_ORDER]}


# --------------------------------------------------------------------------- #
# 2. 频次
# --------------------------------------------------------------------------- #
def frequency_stats(draws: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """每号码 / 每生肖在样本内的出现次数与出现率，附期望值与偏差。"""
    series = normalize_draws(draws)
    sample_size = len(series)
    result = _envelope(sample_size)
    expected_number_rate = 1 / len(NUMBERS)

    if sample_size == 0:
        result.update(
            {
                "numbers": [],
                "zodiacs": [],
                "expected_number_rate": expected_number_rate,
                "sample_power": {
                    "draws": 0,
                    "expected_appearances_per_number": 0.0,
                    "expected_appearances_per_zodiac": 0.0,
                    "strong_conclusion_min_draws": STRONG_CONCLUSION_MIN_DRAWS,
                    "can_support_strong_conclusion": False,
                    "mapped_draws": 0,
                },
                "note": "数据不足：本池尚未导入任何开奖数据，无法计算频次。",
                "notes": _empty_note(),
            }
        )
        return result

    number_counts = Counter(draw["special_number"] for draw in series)
    zodiac_counts = Counter(draw["zodiac"] for draw in series)
    expected_zodiac, mapped_draws = _expected_zodiac_rates(series)
    latest_date = series[-1]["draw_date"]

    numbers: list[dict[str, Any]] = []
    for number in NUMBERS:
        count = number_counts.get(number, 0)
        rate = count / sample_size
        code = zodiac_of(number, latest_date)
        numbers.append(
            {
                "number": number,
                "zodiac": code,
                "zodiac_label": zodiac_label(code),
                "appearances": count,
                "rate": rate,
                "expected_rate": expected_number_rate,
                "expected_count": sample_size * expected_number_rate,
                "deviation": rate - expected_number_rate,
            }
        )

    zodiacs: list[dict[str, Any]] = []
    for code in ZODIACS:
        count = zodiac_counts.get(code, 0)
        rate = count / sample_size
        expected = expected_zodiac[code]
        zodiacs.append(
            {
                "zodiac": code,
                "zodiac_label": ZODIAC_LABELS.get(code),
                "appearances": count,
                "rate": rate,
                "expected_rate": expected,
                "deviation": (rate - expected) if expected is not None else None,
            }
        )

    can_conclude = sample_size >= STRONG_CONCLUSION_MIN_DRAWS
    note = (
        f"本池已导入 {sample_size} 期数据，每个号码理论期望约出现 "
        f"{sample_size / len(NUMBERS):.1f} 次；该样本量不足以支持强结论，"
        "以下仅为样本内观察，请勿外推。"
    )

    result.update(
        {
            "numbers": numbers,
            "zodiacs": zodiacs,
            "expected_number_rate": expected_number_rate,
            "sample_power": {
                "draws": sample_size,
                "expected_appearances_per_number": sample_size / len(NUMBERS),
                "expected_appearances_per_zodiac": sample_size / len(ZODIACS),
                "strong_conclusion_min_draws": STRONG_CONCLUSION_MIN_DRAWS,
                "can_support_strong_conclusion": can_conclude,
                "mapped_draws": mapped_draws,
            },
            "note": note,
            "notes": [
                note,
                "生肖期望值按各期开奖日所属农历年的生肖表（各生肖号码数 / 49）取平均，"
                "而非简单 1/12 —— 因为 49 个号码在 12 生肖上并非均分。",
                "出现次数偏差受样本量影响极大，请勿据此断言任何号码 / 生肖「更热」。",
            ],
        }
    )
    return result


# --------------------------------------------------------------------------- #
# 3. 特码走势 + 相邻波动
# --------------------------------------------------------------------------- #
def trend_stats(
    draws: Iterable[dict[str, Any]], small_max: int = 10, normal_max: int = 30
) -> dict[str, Any]:
    """特码时间序列（升序）+ 相邻两期波动分类与整体分布。"""
    series = normalize_draws(draws)
    sample_size = len(series)
    small_max = int(small_max)
    normal_max = int(normal_max)
    result = _envelope(sample_size)

    rows: list[dict[str, Any]] = []
    counts = {wave: 0 for wave in WAVE_ORDER}
    previous: int | None = None
    for draw in series:
        if previous is None:
            diff: int | None = None
            wave: str | None = None
        else:
            diff = abs(draw["special_number"] - previous)
            wave = classify_wave(diff, small_max, normal_max)
            counts[wave] += 1
        rows.append(
            {
                "period": draw["period"],
                "draw_date": draw["draw_date"].isoformat(),
                "special_number": draw["special_number"],
                "zodiac": draw["zodiac"],
                "zodiac_label": draw["zodiac_label"],
                "diff": diff,
                "wave_type": wave,
                "wave_label": WAVE_LABELS[wave] if wave is not None else None,
            }
        )
        previous = draw["special_number"]

    total_pairs = max(0, sample_size - 1)
    items = [
        {
            "type": wave,
            "label": WAVE_LABELS[wave],
            "count": counts[wave],
            "rate": _rate(counts[wave], total_pairs),
        }
        for wave in WAVE_ORDER
    ]
    dominant = (
        max(WAVE_ORDER, key=lambda wave: counts[wave]) if total_pairs else None
    )

    notes = [
        f"走势与波动只覆盖本池已导入的 {sample_size} 期特码样本。",
        f"波动阈值取自当前配置：小波动 ≤ {small_max}，常规波动 ≤ {normal_max}，"
        f"大跳 ≥ {derive_big_min(normal_max)}（与 /api/settings 口径一致）。",
    ]
    if total_pairs == 0:
        notes.append("数据不足：样本不足两期，无法计算相邻波动。")
    elif total_pairs < 30:
        notes.append(
            f"数据不足：相邻波动样本仅 {total_pairs} 对，波动分布不足以支持强结论。"
        )

    result.update(
        {
            "series": rows,
            "wave_distribution": {
                "total_pairs": total_pairs,
                "items": items,
                "dominant": dominant,
                "dominant_label": WAVE_LABELS[dominant] if dominant else None,
            },
            "settings": {
                "small_max": small_max,
                "normal_max": normal_max,
                "big_min": derive_big_min(normal_max),
            },
            "notes": notes,
        }
    )
    return result


# --------------------------------------------------------------------------- #
# 4. 生肖走势（连出 / 近期覆盖 / 轮转）
# --------------------------------------------------------------------------- #
def zodiac_trend_stats(
    draws: Iterable[dict[str, Any]], recent_window: int = DEFAULT_RECENT_WINDOW
) -> dict[str, Any]:
    """生肖序列 + 各生肖连出（最长 / 当前）+ 最近窗口覆盖与轮转情况。"""
    series = normalize_draws(draws)
    sample_size = len(series)
    window = max(1, int(recent_window))
    result = _envelope(sample_size)

    if sample_size == 0:
        result.update(
            {
                "series": [],
                "zodiacs": [],
                "max_streak_leaders": [],
                "recent_window": {
                    "requested": window,
                    "used": 0,
                    "sequence": [],
                    "present": [],
                    "present_labels": [],
                    "missing": list(ZODIACS),
                    "missing_labels": [ZODIAC_LABELS[code] for code in ZODIACS],
                    "distinct_count": 0,
                    "available": len(ZODIACS),
                },
                "rotation": {
                    "distinct_in_recent": 0,
                    "available": len(ZODIACS),
                    "note": "数据不足：本池尚未导入任何开奖数据，无法判断轮转。",
                },
                "notes": _empty_note(),
            }
        )
        return result

    sequence = [draw["zodiac"] for draw in series]

    appearances = {code: 0 for code in ZODIACS}
    max_streak = {code: 0 for code in ZODIACS}
    max_streak_end_period = {code: None for code in ZODIACS}
    run_length = 0
    run_zodiac: str | None = None
    for index, code in enumerate(sequence):
        if code == run_zodiac:
            run_length += 1
        else:
            run_zodiac = code
            run_length = 1
        if code in appearances:
            appearances[code] += 1
            if run_length > max_streak[code]:
                max_streak[code] = run_length
                max_streak_end_period[code] = series[index]["period"]

    last_zodiac = sequence[-1]
    zodiac_rows = [
        {
            "zodiac": code,
            "zodiac_label": ZODIAC_LABELS.get(code),
            "appearances": appearances[code],
            "max_streak": max_streak[code],
            "max_streak_end_period": max_streak_end_period[code],
            "current_streak": run_length if code == last_zodiac else 0,
        }
        for code in ZODIACS
    ]
    leaders = sorted(
        [row for row in zodiac_rows if row["max_streak"] > 0],
        key=lambda row: (-row["max_streak"], ZODIACS.index(row["zodiac"])),
    )[:5]

    used = min(window, sample_size)
    recent_sequence = sequence[sample_size - used :]
    present_set = {code for code in recent_sequence if code in ZODIACS}
    present = [code for code in ZODIACS if code in present_set]
    missing = [code for code in ZODIACS if code not in present_set]

    rotation_note = (
        f"本池样本最近 {used} 期里出现 {len(present)}/{len(ZODIACS)} 个生肖，"
        f"其余 {len(missing)} 个生肖在这 {used} 期内未出现。"
    )
    notes = [
        f"生肖走势只覆盖本池已导入的 {sample_size} 期特码样本。",
        "连出 = 连续多期特码落在同一生肖；当前连出仅指最新一期结尾处仍在延续的连续段。",
        rotation_note,
    ]
    if used < len(ZODIACS):
        notes.append(
            f"数据不足：最近窗口实际只有 {used} 期（不足 {len(ZODIACS)} 期），"
            "轮转覆盖率不具可比性。"
        )

    result.update(
        {
            "series": [
                {
                    "period": draw["period"],
                    "draw_date": draw["draw_date"].isoformat(),
                    "zodiac": draw["zodiac"],
                    "zodiac_label": draw["zodiac_label"],
                }
                for draw in series
            ],
            "zodiacs": zodiac_rows,
            "max_streak_leaders": leaders,
            "recent_window": {
                "requested": window,
                "used": used,
                "sequence": recent_sequence,
                "present": present,
                "present_labels": [ZODIAC_LABELS[code] for code in present],
                "missing": missing,
                "missing_labels": [ZODIAC_LABELS[code] for code in missing],
                "distinct_count": len(present),
                "available": len(ZODIACS),
            },
            "rotation": {
                "distinct_in_recent": len(present),
                "available": len(ZODIACS),
                "note": rotation_note,
            },
            "notes": notes,
        }
    )
    return result


# --------------------------------------------------------------------------- #
# 6. 走步回测（walk-forward，严格无未来函数）
# --------------------------------------------------------------------------- #
def backtest_stats(
    draws: Iterable[dict[str, Any]],
    mode: str | None = None,
    pick_count: int | None = None,
    small_max: int | None = None,
    normal_max: int | None = None,
    min_prior_draws: int = MIN_PRIOR_DRAWS,
    base_settings: dict[str, Any] | None = None,
    *,
    include_results: bool = True,
    include_wave_breakdown: bool = True,
) -> dict[str, Any]:
    """逐期走步回测推荐引擎：每期只用「该期之前」的数据，再与该期实际特码比对。

    ``base_settings`` 是调用方（HTTP 层）解析好的**有效配置**——通常是当前登录
    用户经 ``Store.get_settings`` 合并「全局模板 + 用户自己的行」后的结果；
    为 ``None`` 时退化为 ``DEFAULT_SETTINGS``，保持本函数可直接单测的纯函数性质。
    显式传入的 ``mode`` / ``pick_count`` / ``small_max`` / ``normal_max`` 覆盖
    ``base_settings`` 中的同名项。

    参数来源以 ``parameter_sources`` 原样回报（``saved_settings`` /
    ``request_override`` / ``default``），响应体的 ``settings`` 记录实际生效值，
    因此同一组入参仍可复现同一次回测。

    ``include_results`` / ``include_wave_breakdown`` 供参数扫描（``backtest_sweep``）
    关闭大块返回体，只保留命中率对照所需字段；单次回测保持默认 True。

    注意：覆盖字典必须**省略**未提供的键，绝不能写成 ``"small_max": None``——
    ``clamp_settings`` 会跳过 ``None``，若把 ``None`` 展开进合并字典就会覆盖掉
    ``base_settings`` 里的有效值（``/api/stats/trend`` 修过的同类 bug）。
    """
    series = normalize_draws(draws)
    sample_size = len(series)
    min_prior_draws = max(1, int(min_prior_draws))
    result = _envelope(sample_size)

    base = dict(DEFAULT_SETTINGS)
    if base_settings:
        base.update(
            {
                key: value
                for key, value in base_settings.items()
                if key in DEFAULT_SETTINGS and value is not None
            }
        )
    # 只收显式给出的覆盖项；None 一律省略，交由 clamp_settings 保留 base 值
    overrides = {
        key: value
        for key, value in {
            "small_max": small_max,
            "normal_max": normal_max,
            "pick_count": pick_count,
            "mode": mode,
        }.items()
        if value is not None
    }
    base_source = "default" if base_settings is None else "saved_settings"
    parameter_sources = {
        key: ("request_override" if key in overrides else base_source)
        for key in ("small_max", "normal_max", "mode", "pick_count")
    }

    cfg = clamp_settings({**base, **overrides})
    effective_mode = cfg["mode"]
    effective_picks = 1 if effective_mode == MODE_SINGLE else cfg["pick_count"]

    specials = [draw["special_number"] for draw in series]
    periods = [draw["period"] for draw in series]
    dates = [draw["draw_date"] for draw in series]

    rows: list[dict[str, Any]] = []
    available_sum = 0
    actual_in_pool_hits = 0
    for index in range(min_prior_draws, sample_size):
        latest = specials[index - 1]
        previous = specials[index - 2] if index - 2 >= 0 else None
        history = specials[:index]  # 严格早于 index
        history_dates = dates[:index]
        outcome = recommend(
            latest=latest,
            previous=previous,
            history_numbers=history,
            history_dates=history_dates,
            settings=cfg,
            mode=effective_mode,
            period=periods[index],
        )
        predicted = [pick["number"] for pick in outcome["picks"]]
        actual = specials[index]
        realized_diff = abs(actual - latest)
        realized_wave = classify_wave(realized_diff, cfg["small_max"], cfg["normal_max"])
        prev_wave = outcome["prev_wave"]
        # 候选池始终排除最新号本身；仅当「避开重肖」开启时再排除整组同肖。
        # 实际特码落在池外时引擎不可能命中 —— 这构成命中率的理论上限。
        pool = set(range(NUMBER_MIN, NUMBER_MAX + 1)) - {latest}
        if cfg["exclude_repeat_zodiac"]:
            pool -= set(same_group_numbers(latest))
        available_sum += len(pool)
        if actual in pool:
            actual_in_pool_hits += 1
        rows.append(
            {
                "period": periods[index],
                "draw_date": series[index]["draw_date"].isoformat(),
                "latest_used": latest,
                "previous_used": previous,
                "predicted": predicted,
                "actual": actual,
                "hit": actual in predicted,
                "prev_wave_type": prev_wave["type"] if prev_wave else None,
                "prev_wave_label": prev_wave["label"] if prev_wave else None,
                "realized_wave_type": realized_wave,
                "realized_wave_label": WAVE_LABELS[realized_wave],
                "realized_diff": realized_diff,
                "actual_in_candidate_pool": actual in pool,
            }
        )

    evaluated = len(rows)
    hits = sum(1 for row in rows if row["hit"])
    hit_rate = _rate(hits, evaluated)
    # 随机参考：在 1-49 中随机取 k 个号，命中概率恒为 k/49
    baseline = effective_picks / (NUMBER_MAX - NUMBER_MIN + 1)
    standard_error = (
        math.sqrt(baseline * (1 - baseline) / evaluated) if evaluated else None
    )
    difference = (hit_rate - baseline) if hit_rate is not None else None
    average_available = (available_sum / evaluated) if evaluated else None
    actual_in_pool_rate = _rate(actual_in_pool_hits, evaluated)

    window = {
        "draws_used": sample_size,
        "min_prior_draws": min_prior_draws,
        "first_evaluated_period": rows[0]["period"] if rows else None,
        "last_evaluated_period": rows[-1]["period"] if rows else None,
        "evaluated": evaluated,
        "skipped": sample_size - evaluated,
        "description": (
            f"按 draw_date 升序对本池已导入的 {sample_size} 期逐期回测："
            f"从第 {min_prior_draws + 1} 期起，每期只用该期之前的数据生成候选号码，"
            f"再与该期实际特码比对；共评估 {evaluated} 期，"
            f"跳过最早 {sample_size - evaluated} 期（前置历史不足 {min_prior_draws} 期）。"
        ),
    }

    if evaluated == 0:
        result.update(
            {
                "data_status": DATA_STATUS_INSUFFICIENT,
                "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
            }
        )

    notes = [
        f"回测只使用本池已导入的 {sample_size} 期数据，结果是样本内表现，"
        "不构成对未来命中能力的任何承诺。",
        "每期预测严格只使用该期之前的数据（walk-forward），实现上通过"
        " history = specials[:index] 与 latest = specials[index-1] 保证无未来函数。",
    ]
    if evaluated == 0:
        notes.append(
            f"数据不足：样本 {sample_size} 期不足以支撑回测（至少需要 "
            f"{min_prior_draws + 1} 期）。"
        )
    elif evaluated < 50:
        notes.append(
            f"数据不足：可评估期数仅 {evaluated} 期，命中率抽样误差很大，"
            "不足以支持强结论。"
        )
    if difference is not None and standard_error is not None:
        if abs(difference) <= 2 * standard_error:
            notes.append(
                f"命中率与随机参考值相差 {difference:+.2%}，在 {evaluated} 期样本下"
                f"抽样标准误约 {standard_error:.2%}；差异未超过 2 倍标准误，"
                "属于抽样波动范围，不能据此判断引擎是否有效。"
            )
        else:
            notes.append(
                f"命中率与随机参考值相差 {difference:+.2%}，超过 2 倍抽样标准误"
                f"（约 {standard_error:.2%}）；但单一小样本内的偏离仍可能由其他因素造成，"
                "请谨慎解读，不要当作未来收益依据。"
            )
    notes.append(
        "realized 波动分解（按当期实际与上一期的差值分类）属于事后归因，"
        "不参与预测；prev 波动分解用的是预测当时已知的上一期波动。"
    )

    verdict = backtest_verdict_payload(
        evaluated=evaluated,
        hit_rate=hit_rate,
        baseline=baseline,
        difference=difference,
        standard_error=standard_error,
    )

    payload: dict[str, Any] = {
        "settings": {
            **{
                key: cfg[key]
                for key in (
                    "small_max",
                    "normal_max",
                    "mode",
                    "pick_count",
                    "exclude_repeat_zodiac",
                    # 走势加权：扫描与单次回测都要如实回报生效值
                    "trend_bias",
                    "trend_window",
                    # 避冷加权（additive）：回测与财富密码同源，需如实回报生效值
                    "avoid_cold_enabled",
                    "avoid_cold_days",
                )
            },
            "big_min": derive_big_min(cfg["normal_max"]),
            "effective_pick_count": effective_picks,
            "trend_bias_label": TREND_BIAS_LABELS.get(
                cfg["trend_bias"], cfg["trend_bias"]
            ),
        },
        # 参数来源（additive）：saved_settings / request_override / default
        "parameter_sources": parameter_sources,
        "evaluation_window": window,
        "evaluated": evaluated,
        "hits": hits,
        "hit_rate": hit_rate,
        "random_baseline_hit_rate": baseline,
        "random_baseline_note": (
            "随机参考值 = 有效注数 / 49，相当于在 1-49 中随机取 k 个号；仅作对照。"
        ),
        "hit_rate_minus_baseline": difference,
        "hit_rate_standard_error": standard_error,
        "verdict": verdict,
        "average_available_numbers": average_available,
        "actual_in_pool_rate": actual_in_pool_rate,
        "actual_in_pool_note": (
            "实际特码落在候选池内的比例 —— 落入池外的期数引擎不可能命中，"
            "它是命中率的理论上限。"
            + (
                "（当前开启避开重肖：池外含最新同肖整组。）"
                if cfg["exclude_repeat_zodiac"]
                else "（当前未避开重肖：池外仅含上期特码本身。）"
            )
        ),
        "notes": notes,
    }
    if include_wave_breakdown:
        payload["wave_breakdown"] = _breakdown(rows, "realized_wave_type")
        payload["wave_breakdown_by_prev"] = _breakdown(rows, "prev_wave_type")
    if include_results:
        payload["results"] = rows
    result.update(payload)
    return result


def _trend_window_label(window: int) -> str:
    """近窗档位的展示标签（0 = 全部样本）。"""
    if window and window > 0:
        return f"近 {int(window)} 期"
    return "全部样本"


def backtest_sweep(
    draws: Iterable[dict[str, Any]],
    *,
    base_settings: dict[str, Any] | None = None,
    mode: str | None = None,
    pick_count: int | None = None,
    small_max: int | None = None,
    normal_max: int | None = None,
    trend_biases: Sequence[str] | None = None,
    trend_windows: Sequence[int] | None = None,
    avoid_cold_values: Sequence[bool] | None = None,
    min_prior_draws: int = MIN_PRIOR_DRAWS,
) -> dict[str, Any]:
    """参数扫描：在本池样本内对照多组配置的走步命中率。

    默认轴：``trend_bias × trend_window × avoid_cold_enabled``（约 32 组）；
    ``mode`` / ``pick_count`` / 波动阈值沿用 ``base_settings``（可被显式覆盖）。
    **只读**：不写库、不改设置；排序按命中率降序仅便于对照，**不是**「最优策略」。

    每行都带与单次回测同口径的 ``verdict``；顶部 notes 禁止任何「已优化命中率」
    类承诺。响应里的 ``baseline_settings`` 标明扫描时的固定旋钮，便于复现。
    """
    series = normalize_draws(draws)
    sample_size = len(series)
    envelope = _envelope(sample_size)

    base = dict(DEFAULT_SETTINGS)
    if base_settings:
        base.update(
            {
                key: value
                for key, value in base_settings.items()
                if key in DEFAULT_SETTINGS and value is not None
            }
        )
    held_overrides = {
        key: value
        for key, value in {
            "small_max": small_max,
            "normal_max": normal_max,
            "pick_count": pick_count,
            "mode": mode,
        }.items()
        if value is not None
    }
    held = clamp_settings({**base, **held_overrides})

    biases = [
        bias
        for bias in (trend_biases if trend_biases is not None else SWEEP_TREND_BIASES)
        if bias in TREND_BIASES
    ] or list(SWEEP_TREND_BIASES)
    windows = [
        int(window)
        for window in (
            trend_windows if trend_windows is not None else SWEEP_TREND_WINDOWS
        )
    ] or list(SWEEP_TREND_WINDOWS)
    cold_flags = list(
        avoid_cold_values
        if avoid_cold_values is not None
        else SWEEP_AVOID_COLD_ENABLED
    )
    if not cold_flags:
        cold_flags = list(SWEEP_AVOID_COLD_ENABLED)

    rows: list[dict[str, Any]] = []
    for bias in biases:
        for window in windows:
            for cold_on in cold_flags:
                combo = {
                    **held,
                    "trend_bias": bias,
                    # 扫描行视为「显式选择过偏好」，否则 effective_trend_bias 会把
                    # 未打标的 hot/cold/mid 一律回退成 neutral（设置页同款门闩）。
                    "trend_bias_explicit": True,
                    "trend_window": window,
                    "avoid_cold_enabled": bool(cold_on),
                }
                outcome = backtest_stats(
                    series,
                    base_settings=combo,
                    min_prior_draws=min_prior_draws,
                    include_results=False,
                    include_wave_breakdown=False,
                )
                cfg = outcome.get("settings") or {}
                rows.append(
                    {
                        "trend_bias": cfg.get("trend_bias", bias),
                        "trend_bias_label": cfg.get(
                            "trend_bias_label",
                            TREND_BIAS_LABELS.get(bias, bias),
                        ),
                        "trend_window": cfg.get("trend_window", window),
                        "trend_window_label": _trend_window_label(
                            int(cfg.get("trend_window", window) or 0)
                        ),
                        "avoid_cold_enabled": bool(
                            cfg.get("avoid_cold_enabled", cold_on)
                        ),
                        "avoid_cold_days": int(
                            cfg.get(
                                "avoid_cold_days",
                                held.get(
                                    "avoid_cold_days", DEFAULT_AVOID_COLD_DAYS
                                ),
                            )
                        ),
                        "mode": cfg.get("mode", held["mode"]),
                        "pick_count": cfg.get("pick_count", held["pick_count"]),
                        "effective_pick_count": cfg.get(
                            "effective_pick_count", held["pick_count"]
                        ),
                        "evaluated": outcome.get("evaluated", 0),
                        "hits": outcome.get("hits", 0),
                        "hit_rate": outcome.get("hit_rate"),
                        "random_baseline_hit_rate": outcome.get(
                            "random_baseline_hit_rate"
                        ),
                        "hit_rate_minus_baseline": outcome.get(
                            "hit_rate_minus_baseline"
                        ),
                        "hit_rate_standard_error": outcome.get(
                            "hit_rate_standard_error"
                        ),
                        "actual_in_pool_rate": outcome.get("actual_in_pool_rate"),
                        "verdict": outcome.get("verdict"),
                        "matches_baseline": (
                            cfg.get("trend_bias") == held.get("trend_bias")
                            and int(cfg.get("trend_window", -1))
                            == int(held.get("trend_window", DEFAULT_TREND_WINDOW))
                            and bool(cfg.get("avoid_cold_enabled"))
                            == bool(held.get("avoid_cold_enabled"))
                        ),
                    }
                )

    # 对照排序：命中率降序；平手按「是否当前设置 → bias → window」稳定化
    # 排序**只为浏览**，不代表「最优」—— notes 里必须说清楚
    def _sort_key(row: dict[str, Any]) -> tuple:
        rate = row.get("hit_rate")
        return (
            0 if rate is None else 1,
            -(rate if rate is not None else 0.0),
            0 if row.get("matches_baseline") else 1,
            str(row.get("trend_bias") or ""),
            int(row.get("trend_window") or 0),
            0 if row.get("avoid_cold_enabled") else 1,
        )

    rows.sort(key=_sort_key)

    beyond_count = sum(
        1
        for row in rows
        if (row.get("verdict") or {}).get("kind") == VERDICT_BEYOND
    )
    noise_count = sum(
        1
        for row in rows
        if (row.get("verdict") or {}).get("kind") == VERDICT_NOISE
    )
    insufficient_count = sum(
        1
        for row in rows
        if (row.get("verdict") or {}).get("kind") == VERDICT_INSUFFICIENT
    )

    notes = [
        f"参数扫描只对照本池已导入的 {sample_size} 期样本内表现，"
        "排序按命中率降序仅便于浏览，不是最优策略，也不构成对未来命中能力的承诺。",
        "每组配置都走与财富密码同源的 walk-forward 回测（严格无未来函数）；"
        "判定口径与单次回测相同：|命中率 − 随机参考| 是否落在 2 倍抽样标准误内。",
        "本接口只读，不会改写你的设置；若要把某一组写入设置，须由你在页面上显式确认。",
        "禁止把「样本内排名靠前」说成「已优化命中率」或「提高命中率」。",
    ]
    if sample_size < MIN_PRIOR_DRAWS + 1:
        notes.append(
            f"数据不足：样本 {sample_size} 期不足以支撑回测"
            f"（至少需要 {MIN_PRIOR_DRAWS + 1} 期）。"
        )
    elif beyond_count == 0 and noise_count > 0:
        notes.append(
            f"本轮 {len(rows)} 组配置里，有 {noise_count} 组的命中率差值落在抽样噪声内，"
            f"{insufficient_count} 组数据不足；**没有任何一组能证明优于随机**。"
        )
    elif beyond_count > 0:
        notes.append(
            f"本轮 {len(rows)} 组配置里，有 {beyond_count} 组的差值超过 2 倍标准误，"
            f"{noise_count} 组仍落在噪声内；超过噪声也只是单一小样本内的偏离，"
            "仍不构成对未来命中能力的承诺。"
        )

    envelope.update(
        {
            "axes": {
                "trend_bias": list(biases),
                "trend_window": list(windows),
                "avoid_cold_enabled": [bool(flag) for flag in cold_flags],
            },
            "held_settings": {
                "mode": held["mode"],
                "pick_count": held["pick_count"],
                "small_max": held["small_max"],
                "normal_max": held["normal_max"],
                "big_min": derive_big_min(held["normal_max"]),
                "exclude_repeat_zodiac": held["exclude_repeat_zodiac"],
                "avoid_cold_days": held["avoid_cold_days"],
                "trend_bias": held["trend_bias"],
                "trend_window": held["trend_window"],
                "avoid_cold_enabled": held["avoid_cold_enabled"],
            },
            "combo_count": len(rows),
            "beyond_count": beyond_count,
            "noise_count": noise_count,
            "insufficient_count": insufficient_count,
            "rows": rows,
            "notes": notes,
        }
    )
    if sample_size < MIN_PRIOR_DRAWS + 1:
        envelope["data_status"] = DATA_STATUS_INSUFFICIENT
        envelope["data_status_label"] = DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT]
    return envelope


def tune_delta_score_weights(
    draws: Iterable[dict[str, Any]],
    *,
    base_settings: dict[str, Any] | None = None,
    train_ratio: float = 0.7,
    min_prior_draws: int = MIN_PRIOR_DRAWS,
) -> dict[str, Any]:
    """Walk-forward 调参：在训练窗上扫打分权重，只在验证窗比较相对随机 Δ。

    - 固定 ``pick_strategy=score_top``；基线对照为同设置下的 ``wave_round``。
    - **只认验证窗 Δ** 是否优于基线；训练窗更好不算上线依据。
    - 禁止把结果说成「已提高命中率」；返回 notes 必须带样本内 / 无未来承诺口径。
    """
    series = normalize_draws(draws)
    sample_size = len(series)
    envelope = _envelope(sample_size)

    base = dict(DEFAULT_SETTINGS)
    if base_settings:
        base.update(
            {
                key: value
                for key, value in base_settings.items()
                if key in DEFAULT_SETTINGS and value is not None
            }
        )
    held = clamp_settings(base)
    held["trend_bias_explicit"] = True

    ratio = min(0.9, max(0.5, float(train_ratio)))
    # 可评估期从 min_prior_draws 起；按可评估长度切 train/valid
    eval_start = max(1, int(min_prior_draws))
    eval_count = max(0, sample_size - eval_start)
    train_eval = int(eval_count * ratio)
    # 至少留 20 期验证；训练至少 30 期可评估
    if eval_count < 50 or train_eval < 30 or (eval_count - train_eval) < 20:
        notes = [
            f"数据不足：可评估 {eval_count} 期，无法做 train/valid 切分调参"
            f"（建议至少约 50 期可评估）。",
            "本结果不构成对未来命中能力的任何承诺。",
        ]
        envelope.update(
            {
                "data_status": DATA_STATUS_INSUFFICIENT,
                "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
                "improved": False,
                "applied_suggestion": None,
                "baseline_valid": None,
                "best_valid": None,
                "notes": notes,
            }
        )
        return envelope

    split_index = eval_start + train_eval  # series 下标：valid 从这里开始评估
    train_draws = series[:split_index]
    # valid 回测需要前置历史，所以仍喂全序列，但只统计 split 之后的命中
    # 更干净：对 train / valid 各跑 backtest_stats 子序列
    # train：series[:split_index]；valid：用 series 全量但自定义窗口较复杂
    # 采用：train 子序列回测；valid = series[split_index - min_prior:] 以保留前置

    def _run(settings: dict[str, Any], subset: list[dict[str, Any]]) -> dict[str, Any]:
        return backtest_stats(
            subset,
            base_settings=settings,
            min_prior_draws=min_prior_draws,
            include_results=False,
            include_wave_breakdown=False,
        )

    baseline_settings = {
        **held,
        "pick_strategy": PICK_STRATEGY_WAVE_ROUND,
    }
    valid_subset = series[max(0, split_index - min_prior_draws) :]
    # 对齐：valid_subset 的前 min_prior 期只作历史，评估的是原 split 之后的期
    baseline_train = _run(baseline_settings, train_draws)
    baseline_valid = _run(baseline_settings, valid_subset)

    focus_grid = (0.0, 0.5, 1.0, 2.0)
    mid_grid = (0.0, 1.0, 2.0, 3.0)
    omit_grid = (-1.0, 0.0, 0.5, 1.0)
    diff_grid = (0.0, 0.5, 1.0)

    best_train: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = []

    for w_focus in focus_grid:
        for w_mid in mid_grid:
            for w_omit in omit_grid:
                for w_diff in diff_grid:
                    weights = clamp_score_weights(
                        {
                            "score_w_focus": w_focus,
                            "score_w_mid": w_mid,
                            "score_w_omit": w_omit,
                            "score_w_diff": w_diff,
                        }
                    )
                    cfg = {
                        **held,
                        "pick_strategy": PICK_STRATEGY_SCORE_TOP,
                        **weights,
                    }
                    train_out = _run(cfg, train_draws)
                    train_delta = train_out.get("hit_rate_minus_baseline")
                    if train_delta is None:
                        continue
                    row = {
                        "weights": weights,
                        "train_hits": train_out.get("hits"),
                        "train_evaluated": train_out.get("evaluated"),
                        "train_hit_rate": train_out.get("hit_rate"),
                        "train_delta": train_delta,
                    }
                    candidates.append(row)
                    if best_train is None or train_delta > best_train["train_delta"]:
                        best_train = row

    # 取训练窗 Δ 前几名，在验证窗上裁定（降过拟合）
    candidates.sort(key=lambda r: (-(r["train_delta"] or 0.0),))
    shortlist = candidates[:12]
    best_valid: dict[str, Any] | None = None
    for row in shortlist:
        cfg = {
            **held,
            "pick_strategy": PICK_STRATEGY_SCORE_TOP,
            **row["weights"],
        }
        valid_out = _run(cfg, valid_subset)
        valid_delta = valid_out.get("hit_rate_minus_baseline")
        if valid_delta is None:
            continue
        judged = {
            **row,
            "valid_hits": valid_out.get("hits"),
            "valid_evaluated": valid_out.get("evaluated"),
            "valid_hit_rate": valid_out.get("hit_rate"),
            "valid_delta": valid_delta,
            "valid_verdict": (valid_out.get("verdict") or {}).get("kind"),
        }
        if best_valid is None or valid_delta > best_valid["valid_delta"]:
            best_valid = judged

    baseline_valid_delta = baseline_valid.get("hit_rate_minus_baseline")
    improved = bool(
        best_valid
        and baseline_valid_delta is not None
        and best_valid["valid_delta"] is not None
        and best_valid["valid_delta"] > baseline_valid_delta + 1e-12
    )

    suggestion = None
    if improved and best_valid is not None:
        suggestion = {
            "pick_strategy": PICK_STRATEGY_SCORE_TOP,
            **best_valid["weights"],
        }

    notes = [
        f"打分权重调参只使用本池已导入的 {sample_size} 期；"
        f"训练可评估约 {baseline_train.get('evaluated')} 期，"
        f"验证可评估约 {baseline_valid.get('evaluated')} 期（walk-forward，无未来函数）。",
        "上线尺子只看验证窗相对随机 Δ 是否优于同设置下的波动轮取基线；"
        "训练窗更好不算数。",
        "禁止把本结果说成「已提高命中率」或对未来命中的承诺。",
    ]
    if improved and best_valid is not None:
        notes.append(
            f"验证窗 Δ 优于基线："
            f"score_top Δ={best_valid['valid_delta']:.4f} "
            f"> wave_round Δ={baseline_valid_delta:.4f}；"
            "可将 suggestion 写入设置（显式确认）。"
        )
    else:
        notes.append(
            "验证窗未能证明 score_top 相对波动轮取基线有更高的 Δ；"
            "保持 pick_strategy=wave_round 或仅作研究对照。"
        )

    envelope.update(
        {
            "train_ratio": ratio,
            "split_index": split_index,
            "baseline_train": {
                "hits": baseline_train.get("hits"),
                "evaluated": baseline_train.get("evaluated"),
                "hit_rate": baseline_train.get("hit_rate"),
                "delta": baseline_train.get("hit_rate_minus_baseline"),
            },
            "baseline_valid": {
                "hits": baseline_valid.get("hits"),
                "evaluated": baseline_valid.get("evaluated"),
                "hit_rate": baseline_valid.get("hit_rate"),
                "delta": baseline_valid_delta,
                "verdict": (baseline_valid.get("verdict") or {}).get("kind"),
            },
            "best_train": best_train,
            "best_valid": best_valid,
            "improved": improved,
            "applied_suggestion": suggestion,
            "shortlist_size": len(shortlist),
            "grid_size": len(candidates),
            "notes": notes,
        }
    )
    return envelope
