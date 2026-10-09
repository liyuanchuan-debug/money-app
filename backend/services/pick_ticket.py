"""出票单（选号工具）—— 纯函数核心，不依赖数据库。

## 这个工具交付什么（以及**不**交付什么）

已独立验证的事实：本游戏的每个号码等概率（1/49），赔率 47 时**单注期望收益率恒为**
``47 / 49 − 1 = −4.0816%``，**与选号方法、权重、注数、注码无关**。本池样本内走步回测
（见 ``services.analytics.backtest_stats``）命中率落在抽样噪声内（verdict = ``noise``）。
因此本模块**绝不**声称、暗示或包装「更容易中奖」。

它真正交付的五件事：

1. **纪律与花费控制**：每注金额精确到注码粒度，合计不超预算，逐注下限受保护，
   省下的预算如实披露、**不静默改配**；
2. **号码卫生**：重号 / 同肖 / 冷号 / 点阵等既有软偏好（沿用设置页口径），如实打标；
3. **覆盖透明**：本票覆盖了 1..49 里的哪些号、生肖 / 大小 / 奇偶 / 尾数分布如何；
4. **可复现与可审计**：记录 ``seed`` 与数据摘要，同种子 + 同数据 + 同设置
   **逐字节可复现**，并给出 ``ticket_id`` 与冻结用的纯文本文案；
5. **诚实风险披露**：真实期望值、随机基线、本票结果分布、历史最久连续未中期数。

选号排序**一律复用** ``services.lottery`` 的公开函数（``recommend`` / ``order_pool`` /
``allocate_amounts`` / ``apply_soft_weights`` …），本模块不重写任何排序或金额算法，
也不修改生产引擎的行为。

## 选号来源（两种，均在票据里以英文枚举标注）

- ``selection = "engine_picks"``（默认，``seed`` 缺省）：**逐字段**等于
  ``recommend()`` 的输出 —— 生产引擎的既定名次与金额；
- ``selection = "engine_ranking"``（显式给了 ``seed``）：在引擎**自己**的候选排序
  （``order_pool`` 的输出，按引擎的点阵优先取号顺序展平）上，按 ``seed`` 派生的
  批号滑动一个长度为注数的窗口。

两种来源的期望值完全相同：滑动窗口只是「换一批」同一名次带内的候选，
**不改变中奖概率，也不改变期望亏损**。

所有写进 API / 落库的枚举一律英文或数字；汉字只出现在 ``*_label`` 与展示文案里。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, datetime
from functools import lru_cache
from typing import Any, Iterable, Mapping, Sequence

from services.analytics import (
    DATA_STATUS_INSUFFICIENT,
    DATA_STATUS_OK,
    NUMBERS,
    backtest_stats,
    binomial_tail_p,
    minimum_detectable_delta,
)
from services.lottery import (
    DEFAULT_FOCUS,
    DEFAULT_SETTINGS,
    FOCUS_BY_PREV_WAVE,
    MIN_BET_AMOUNT,
    MODE_EVEN,
    MODE_LABELS,
    MODE_RANDOM,
    MODES,
    NUMBER_MAX,
    NUMBER_MIN,
    PICK_COUNT_MAX,
    PICK_COUNT_MIN,
    ROLE_LABELS,
    ROLE_ORDER,
    STATUS_NO_TICKET,
    STATUS_OK,
    WAVE_LABELS,
    WAVE_ORDER,
    allocate_amounts,
    amount_seed_key,
    apply_avoid_cold_amounts,
    apply_soft_weights,
    assign_roles,
    avoid_cold_weight,
    build_candidate_pools,
    build_number_lattice,
    classify_wave,
    clamp_settings,
    compute_days_since_last,
    compute_periods_since_last,
    derive_big_min,
    effective_pick_count,
    lattice_primary_wave,
    order_pool,
    plan_budget,
    predict_wave_band,
    recent_number_frequency,
    recommend,
    resolve_zodiac_date,
    role_amount_weights,
    soft_penalty_weight,
    zodiac_group,
)
from services.mark_six import zodiac_label, zodiac_of

TICKET_VERSION = 1

# --------------------------------------------------------------------------- #
# 英文枚举（禁止汉字落库 / 出站）
# --------------------------------------------------------------------------- #
# 「本工具不改变期望值」的机器可读标记：任何下游层都不得把它当预测结果包装
CLAIM_NO_EDGE = "NO_EDGE"
CLAIM_LABELS = {CLAIM_NO_EDGE: "不改变期望值（无可利用优势）"}

SELECTION_ENGINE_PICKS = "engine_picks"
SELECTION_ENGINE_RANKING = "engine_ranking"
SELECTION_LABELS = {
    SELECTION_ENGINE_PICKS: "生产引擎既定名次",
    SELECTION_ENGINE_RANKING: "生产引擎候选排序内滑动窗口",
}

SOFT_REASON_LABELS = {
    "repeat_number": "重号",
    "repeat_zodiac": "同肖",
    "stale": "冷号",
}

# 票据里逐注保留的字段（顺序固定，便于逐字节复现与人工核对）
ROW_FIELDS: tuple[str, ...] = (
    "number",
    "amount",
    "role",
    "role_label",
    "wave_type",
    "wave_label",
    "diff",
    "zodiac",
    "zodiac_label",
    "trend_count",
    "days_since_last",
    "periods_since_last",
    "is_repeat_number",
    "is_repeat_zodiac",
    "is_stale",
    "soft_weight",
    "soft_reasons",
    "soft_penalized",
    "amount_reduced",
    "lattice_weight",
    "in_lattice_band",
    "avoid_cold_penalized",
    "avoid_cold_weight",
)


class TicketDataError(ValueError):
    """数据不足 / 无法出票（调用方转 400 或直接提示，绝不用 0 值冒充）。"""


# --------------------------------------------------------------------------- #
# 数据规整
# --------------------------------------------------------------------------- #
def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def normalize_ticket_draws(
    draws: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """把任意顺序的开奖记录整理成**最新在前**的规范序列。

    接受 ``special_number`` 或兼容旧字段名 ``number``；缺特码 / 越界 / 缺日期的行跳过
    （缺日期无法定生肖参照日，不猜）。
    """
    rows: list[dict[str, Any]] = []
    for draw in draws:
        raw_number = draw.get("special_number", draw.get("number"))
        if raw_number is None:
            continue
        try:
            number = int(raw_number)
        except (TypeError, ValueError):
            continue
        if not NUMBER_MIN <= number <= NUMBER_MAX:
            continue
        raw_date = draw.get("draw_date")
        if raw_date in (None, ""):
            continue
        try:
            draw_date = _as_date(raw_date)
        except (TypeError, ValueError):
            continue
        raw_period = draw.get("period")
        try:
            period = int(raw_period) if raw_period not in (None, "") else None
        except (TypeError, ValueError):
            period = None
        rows.append(
            {
                "period": period,
                "draw_date": draw_date,
                "special_number": number,
            }
        )
    rows.sort(key=lambda row: (row["draw_date"], row["period"] or 0))
    return list(reversed(rows))


# --------------------------------------------------------------------------- #
# 种子 → 批号
# --------------------------------------------------------------------------- #
def seed_key(seed: Any) -> str:
    """把种子规范成稳定的字符串（``None`` → 空串 = 生产引擎既定名次）。"""
    if seed is None:
        return ""
    return str(seed).strip()


def batch_index(seed: Any) -> int:
    """种子 → 批号（非负整数）。

    - 非负整数字符串（前端「换一批」用的 0/1/2…）直接当批号；
    - 其它种子（如哈希串）取 sha256 前 8 字节派生，保证任意种子都能复现。
    """
    key = seed_key(seed)
    if not key:
        return 0
    try:
        value = int(key)
        if value >= 0:
            return value
    except (TypeError, ValueError):
        pass
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)


# --------------------------------------------------------------------------- #
# 引擎上下文（只调用 services.lottery 的公开函数，不重写任何算法）
# --------------------------------------------------------------------------- #
def _settings_digest(cfg: Mapping[str, Any]) -> str:
    return json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str)


def _engine_context(
    cfg: dict[str, Any],
    latest: int,
    history_numbers: Sequence[int],
    history_dates: Sequence[Any] | None,
) -> dict[str, Any]:
    """算出与 ``recommend()`` 同源的选号上下文（全部来自引擎公开函数）。"""
    history = list(history_numbers)
    dates_list = list(history_dates) if history_dates is not None else None

    zodiac_date = resolve_zodiac_date(dates_list)
    trend_bias = cfg["trend_bias"]
    trend_window = cfg["trend_window"]
    trend_counts, _, used_window = recent_number_frequency(history, trend_window)
    mid_target = (
        sum(trend_counts.values()) / max(1, len(set(trend_counts)))
        if trend_counts
        else 0.0
    )
    days_since_last = compute_days_since_last(history, dates_list)
    periods_since_last = compute_periods_since_last(history)

    latest_zodiac = zodiac_group(latest)
    # 三类软降权：**已停用为权重**，只保留信息标签（soft_weights 恒 1.0）
    soft_weights: dict[int, float] = {}
    soft_reasons: dict[int, list[str]] = {}
    # 与 recommend() 同口径：sample_size 用完整 history 长度
    for number in range(NUMBER_MIN, NUMBER_MAX + 1):
        value, why = soft_penalty_weight(
            number,
            latest=int(latest),
            periods_since_last=periods_since_last.get(number),
            sample_size=len(history),
            repeat_number_weight=float(cfg["repeat_number_weight"]),
            repeat_zodiac_weight=float(cfg["repeat_zodiac_weight"]),
            stale_periods=int(cfg["stale_periods"]),
            stale_weight=float(cfg["stale_weight"]),
        )
        soft_weights[number] = round(value, 6)
        soft_reasons[number] = why

    lattice_enabled = bool(cfg["lattice_enabled"])
    band = (
        predict_wave_band(
            history,
            window=int(cfg["lattice_window"]),
            small_max=cfg["small_max"],
            normal_max=cfg["normal_max"],
        )
        if lattice_enabled
        else None
    )
    lattice_rows = build_number_lattice(
        latest,
        band,
        small_max=cfg["small_max"],
        normal_max=cfg["normal_max"],
    )
    lattice_scores = (
        {row["number"]: float(row["lattice_weight"]) for row in lattice_rows}
        if lattice_enabled
        else None
    )
    lattice_primary = (
        lattice_primary_wave(
            band,
            small_max=cfg["small_max"],
            normal_max=cfg["normal_max"],
        )
        if lattice_enabled
        else None
    )
    history_counts: Counter[int] = Counter(
        n for n in history if int(n) != int(latest)
    )
    return {
        "history": history,
        "dates": dates_list,
        "zodiac_date": zodiac_date,
        "latest_zodiac": latest_zodiac,
        "trend_counts": trend_counts,
        "used_window": used_window,
        "mid_target": mid_target,
        "days_since_last": days_since_last,
        "periods_since_last": periods_since_last,
        "soft_weights": soft_weights,
        "soft_reasons": soft_reasons,
        "lattice_enabled": lattice_enabled,
        "band": band,
        "lattice_rows": lattice_rows,
        "lattice_scores": lattice_scores,
        "lattice_primary": lattice_primary,
        "history_counts": history_counts,
    }


def _focus_order(
    previous: int | None, latest: int, cfg: dict[str, Any]
) -> tuple[list[str], dict[str, Any] | None]:
    """上期波动类型 → 本期侧重顺序（与 ``recommend()`` 逐位一致）。"""
    if previous is None:
        return list(DEFAULT_FOCUS), None
    diff = abs(int(latest) - int(previous))
    prev_type = classify_wave(diff, cfg["small_max"], cfg["normal_max"])
    prev_wave = {
        "number": int(previous),
        "diff": diff,
        "type": prev_type,
        "label": WAVE_LABELS[prev_type],
    }
    return FOCUS_BY_PREV_WAVE[prev_type], prev_wave


def _engine_ranking(
    cfg: dict[str, Any],
    latest: int,
    ctx: dict[str, Any],
) -> list[int]:
    """引擎候选排序展平为 1..49 的号码序列（与 ``recommend()`` 同口径）。

    **变更（点阵分布化）**：点阵不再决定取号顺序，因此展平顺序退回固定的
    ``WAVE_ORDER``（小→常→大）。这里只是「1..49 的展示/候选名次」，
    不代表线上选号顺序（线上是配额 + 期号种子随机）。
    """
    pools = build_candidate_pools(
        latest,
        cfg["small_max"],
        cfg["normal_max"],
        exclude_repeat_zodiac=bool(cfg["exclude_repeat_zodiac"]),
        include_repeat_number=bool(cfg["include_repeat_number"]),
    )
    cold_enabled = bool(cfg["avoid_cold_enabled"])
    ordered = {
        wave: order_pool(
            pools[wave],
            ctx["history_counts"],
            trend_bias=cfg["trend_bias"],
            trend_counts=ctx["trend_counts"],
            mid_target=ctx["mid_target"],
            avoid_cold_enabled=cold_enabled,
            avoid_cold_days=int(cfg["avoid_cold_days"]),
            days_since_last=ctx["days_since_last"],
        )
        for wave in WAVE_ORDER
    }
    return [int(row["number"]) for wave in WAVE_ORDER for row in ordered[wave]]


# --------------------------------------------------------------------------- #
# 逐注行（两条选号来源共用同一行结构）
# --------------------------------------------------------------------------- #
def _pack_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in ROW_FIELDS}


def _row_from_engine_pick(pick: Mapping[str, Any]) -> dict[str, Any]:
    return _pack_row(
        {
            **pick,
            "soft_reasons": list(pick.get("soft_reasons") or []),
            "role_label": pick.get("role_label") or ROLE_LABELS.get(
                str(pick.get("role") or ""), ""
            ),
            "wave_label": pick.get("wave_label")
            or WAVE_LABELS.get(str(pick.get("wave_type") or ""), ""),
        }
    )


def _row_from_number(
    number: int,
    *,
    cfg: dict[str, Any],
    latest: int,
    ctx: dict[str, Any],
) -> dict[str, Any]:
    """按引擎口径给一个号补全行字段（仅用于 ranking 路径）。"""
    diff = abs(int(number) - int(latest))
    wave_type = classify_wave(diff, cfg["small_max"], cfg["normal_max"])
    # 「降权去除」：权重恒 1.0（命中标签只进 soft_reasons）
    soft = 1.0
    zodiac_date = ctx["zodiac_date"]
    code = zodiac_of(int(number), zodiac_date) if zodiac_date is not None else None
    cold_enabled = bool(cfg["avoid_cold_enabled"])
    cold_days = int(cfg["avoid_cold_days"])
    reasons = list(ctx["soft_reasons"].get(number, []))
    return _pack_row(
        {
            "number": int(number),
            "amount": 0,
            "role": None,
            "role_label": None,
            "wave_type": wave_type,
            "wave_label": WAVE_LABELS[wave_type],
            "diff": diff,
            "zodiac": code,
            "zodiac_label": zodiac_label(code),
            "trend_count": int(ctx["trend_counts"].get(number, 0)),
            "days_since_last": ctx["days_since_last"].get(number),
            "periods_since_last": ctx["periods_since_last"].get(number),
            "is_repeat_number": int(number) == int(latest),
            "is_repeat_zodiac": bool(
                int(number) != int(latest)
                and zodiac_group(int(number)) == zodiac_group(int(latest))
            ),
            "is_stale": "stale" in reasons,
            "soft_weight": round(soft, 4),
            "soft_reasons": reasons,
            "soft_penalized": bool(soft < 1.0),
            "amount_reduced": False,
            "lattice_weight": round(
                float(ctx["lattice_scores"].get(number, 1.0))
                if ctx["lattice_scores"]
                else 1.0,
                4,
            ),
            "in_lattice_band": bool(
                ctx["lattice_enabled"]
                and ctx["band"]
                and float(ctx["band"]["low"]) <= diff <= float(ctx["band"]["high"])
            ),
            "avoid_cold_penalized": bool(
                cold_enabled
                and avoid_cold_weight(
                    ctx["days_since_last"].get(number),
                    enabled=True,
                    threshold=cold_days,
                )
                < 1.0
            ),
            "avoid_cold_weight": round(
                float(
                    avoid_cold_weight(
                        ctx["days_since_last"].get(number),
                        enabled=cold_enabled,
                        threshold=cold_days,
                    )
                ),
                4,
            ),
        }
    )


def _allocate_ranking_rows(
    rows: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
    focus: list[str],
    plan: dict[str, Any],
    seed_key_value: str,
    ctx: dict[str, Any],
) -> list[dict[str, Any]]:
    """给 ranking 路径的行分配角色与金额（逐位复用引擎的分配函数）。"""
    rows = sorted(rows, key=lambda row: focus.index(row["wave_type"]))
    roles = assign_roles(len(rows))
    unit = int(cfg["amount_unit"])
    amounts = allocate_amounts(
        plan["mode"],
        roles,
        int(plan["allocated_total"]),
        amount_unit=unit,
        seed=seed_key_value or None,
        role_weights=role_amount_weights(cfg),
    )
    cold_enabled = bool(cfg["avoid_cold_enabled"])
    cold_days = int(cfg["avoid_cold_days"])
    cold_weights = [
        avoid_cold_weight(
            ctx["days_since_last"].get(row["number"]),
            enabled=cold_enabled,
            threshold=cold_days,
        )
        for row in rows
    ]
    if cold_enabled:
        amounts, _ = apply_avoid_cold_amounts(amounts, cold_weights, amount_unit=unit)
    before_soft = list(amounts)
    # 「降权去除」：不再对金额做软降权打折（apply_soft_weights 已从本路径移除）；
    # amount_reduced 今天只可能来自避冷封顶。
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        amount = int(amounts[index]) if index < len(amounts) else 0
        updated = dict(row)
        updated["role"] = roles[index]
        updated["role_label"] = ROLE_LABELS[roles[index]]
        updated["amount"] = amount
        updated["amount_reduced"] = bool(
            index < len(before_soft) and amount < int(before_soft[index])
        )
        out.append(_pack_row(updated))
    return out


def _amount_plan(
    mode: str, total: int, pick_count: int, unit: int, seed_key_value: str
) -> dict[str, Any]:
    """预算规范化（与 ``recommend()`` **同一入口** ``plan_budget``）。

    返回 ``allocated_total`` / ``pick_count`` / ``notes`` / ``reason_code`` /
    ``reason_message``。``reason_code`` 非空 ⇔ 预算连 1 个注码单位都覆盖不了
    （有效注数 0）：调用方必须按「零注」出票，不用 0 值或 ``max(1, ...)`` 冒充。
    """
    plan = plan_budget(mode, total, pick_count, unit, seed=seed_key_value or None)
    return {
        "mode": mode,
        "allocated_total": int(plan["allocated_total"]),
        "pick_count": int(plan["pick_count"]),
        "notes": list(plan["notes"]),
        "reason_code": plan["reason_code"],
        "reason_message": plan["reason_message"],
    }


# --------------------------------------------------------------------------- #
# 覆盖报告
# --------------------------------------------------------------------------- #
def _coverage(
    numbers: Sequence[int],
    *,
    cfg: dict[str, Any],
    latest: int,
    zodiac_date: date | None,
) -> dict[str, Any]:
    picks = sorted({int(n) for n in numbers})
    pools = build_candidate_pools(
        latest,
        cfg["small_max"],
        cfg["normal_max"],
        exclude_repeat_zodiac=bool(cfg["exclude_repeat_zodiac"]),
        include_repeat_number=bool(cfg["include_repeat_number"]),
    )
    universe = sorted({int(row["number"]) for rows in pools.values() for row in rows})
    universe_set = set(universe)
    big_min = derive_big_min(cfg["normal_max"])

    zodiac_buckets = []
    # zodiac_group() 的分组键是 (n - 1) % 12 → 0..11；同组最小号 = group + 1
    for group in range(0, 12):
        reference = group + 1
        code = zodiac_of(reference, zodiac_date) if zodiac_date is not None else None
        zodiac_buckets.append(
            {
                "group": group,
                "code": code,
                "label": zodiac_label(code),
                "count": sum(1 for n in picks if zodiac_group(n) == group),
            }
        )

    return {
        "numbers": picks,
        "covered_count": len(picks),
        "total_numbers": len(NUMBERS),
        "uncovered_count": len(NUMBERS) - len(picks),
        "candidate_pool_size": len(universe),
        "candidate_pool_numbers": universe,
        "excluded_numbers": sorted(set(NUMBERS) - universe_set),
        "picks_within_candidate_pool": sorted(set(picks) & universe_set),
        "candidate_pool_note": (
            "候选池为引擎在本池样本内的可取号范围；池外号码不可能被选到"
            "（当前开启避重肖时，池外含最新一期同肖整组）。"
        ),
        "zodiac": zodiac_buckets,
        "big_small": {
            "big_min": big_min,
            "big": sum(1 for n in picks if n >= big_min),
            "small": sum(1 for n in picks if n < big_min),
        },
        "odd_even": {
            "odd": sum(1 for n in picks if n % 2 == 1),
            "even": sum(1 for n in picks if n % 2 == 0),
        },
        "tail_digit": [
            {"digit": digit, "count": sum(1 for n in picks if n % 10 == digit)}
            for digit in range(10)
        ],
    }


# --------------------------------------------------------------------------- #
# 诚实页脚（含样本内走步回测，全部数字都如实来自引擎 / 统计工具）
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=32)
def _in_sample_summary(draws_asc_json: str, settings_json: str) -> tuple:
    """本池样本内走步回测摘要（缓存：同一份数据 + 同一组设置只算一次）。"""
    draws_asc = json.loads(draws_asc_json)
    settings = json.loads(settings_json)
    outcome = backtest_stats(
        draws_asc,
        base_settings=settings,
        include_wave_breakdown=False,
        include_results=True,
    )
    rows = outcome.get("results") or []
    worst_streak = 0
    worst_streak_end_period = None
    streak = 0
    for row in rows:
        if row.get("hit"):
            streak = 0
            continue
        streak += 1
        if streak > worst_streak:
            worst_streak = streak
            worst_streak_end_period = row.get("period")
    verdict = outcome.get("verdict") or {}
    evaluated = int(outcome.get("evaluated") or 0)
    baseline = float(outcome.get("random_baseline_hit_rate") or 0.0)
    p_value = (
        binomial_tail_p(int(outcome.get("hits") or 0), evaluated, baseline)
        if evaluated > 0
        else None
    )
    power = minimum_detectable_delta(evaluated, baseline) if evaluated > 0 else None
    return (
        int(outcome.get("hits") or 0),
        evaluated,
        outcome.get("hit_rate"),
        baseline,
        str(verdict.get("kind") or "insufficient"),
        str(verdict.get("label") or ""),
        str(verdict.get("text") or ""),
        bool(verdict.get("within_noise", True)),
        p_value,
        json.dumps(power, ensure_ascii=False) if power else "",
        worst_streak,
        worst_streak_end_period,
    )


def _in_sample(draws_asc: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any]) -> dict[str, Any]:
    draws_asc_json = json.dumps(
        [
            {
                "period": row.get("period"),
                "draw_date": str(row.get("draw_date")),
                "special_number": int(row["special_number"]),
            }
            for row in draws_asc
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    settings_json = _settings_digest(cfg)
    (
        hits,
        evaluated,
        hit_rate,
        baseline,
        verdict_kind,
        verdict_label,
        verdict_text,
        within_noise,
        p_value,
        power_json,
        worst_streak,
        worst_streak_end_period,
    ) = _in_sample_summary(draws_asc_json, settings_json)
    return {
        "data_status": DATA_STATUS_OK if evaluated > 0 else DATA_STATUS_INSUFFICIENT,
        "hits": hits,
        "evaluated": evaluated,
        "hit_rate": hit_rate,
        "random_baseline_hit_rate": baseline,
        "p_value": p_value,
        "verdict": verdict_kind,
        "verdict_label": verdict_label,
        "verdict_text": verdict_text,
        "within_noise": within_noise,
        "power": json.loads(power_json) if power_json else None,
        "max_dry_streak_periods": int(worst_streak),
        "max_dry_streak_end_period": worst_streak_end_period,
        "note": (
            "走步回测：每期只用该期之前的数据生成候选，再与该期实际特码比对；"
            "样本内表现，不构成对未来命中能力的任何承诺。"
        ),
    }


def _honest_footer(
    *,
    cfg: Mapping[str, Any],
    pick_count: int,
    stake_total: int,
    in_sample: Mapping[str, Any],
) -> dict[str, Any]:
    odds = float(cfg["odds"])
    number_count = len(NUMBERS)
    ev_per_100 = (odds / number_count - 1.0) * 100.0
    expected_return = stake_total * odds / number_count
    expected_loss = stake_total - expected_return
    baseline = pick_count / number_count if number_count else None
    return {
        "claim": CLAIM_NO_EDGE,
        "claim_label": CLAIM_LABELS[CLAIM_NO_EDGE],
        "odds": odds,
        "odds_note": "赔率是设定兑付倍数（含本金），不是收益承诺。",
        "ev_per_100": ev_per_100,
        "ev_note": (
            f"每投注 100 元的期望盈亏恒为 100 ×（{odds:g} / {number_count} − 1）"
            f" = {ev_per_100:+.4f} 元 —— 与选号方法、权重、注数、注码无关。"
        ),
        "expected_return": expected_return,
        "expected_loss_for_this_ticket": expected_loss,
        "stake_total": stake_total,
        "baseline_hit_rate": baseline,
        "baseline_note": (
            f"随机基线 = 注数 / {number_count} = {pick_count} / {number_count}；"
            "选号方法不改变它。"
        ),
        "hit_distribution": {
            "kind": "bernoulli",
            "p_zero_hits": (1.0 - baseline) if baseline is not None else None,
            "p_at_least_one_hit": baseline,
            "note": "单期只开一个特码，故命中数只能是 0 或 1。",
        },
        "in_sample": dict(in_sample),
    }


# --------------------------------------------------------------------------- #
# 纯文本出票单
# --------------------------------------------------------------------------- #
def _format_ticket_text(ticket: Mapping[str, Any]) -> str:
    picks = ticket.get("picks") or []
    budget = ticket.get("budget") or {}
    coverage = ticket.get("coverage") or {}
    honest = ticket.get("honest") or {}
    data = ticket.get("data") or {}
    in_sample = honest.get("in_sample") or {}

    target = data.get("target_period")
    lines = [
        f"出票单 · 目标第 {target} 期" if target else "出票单",
        f"口径：{ticket.get('scope')}",
    ]
    lines.append("")
    lines.append(
        f"预算 {budget.get('requested')} 元 → 投注 {budget.get('staked')} 元"
        f"（{len(picks)} 注，最小单位 {budget.get('amount_unit')} 元）"
    )
    if ticket.get("status") == STATUS_NO_TICKET:
        reason = ticket.get("reason_message") or ticket.get("reason_code") or ""
        lines.append(f"本次不出票：{reason}".rstrip("："))
    for pick in picks:
        flags = [
            SOFT_REASON_LABELS[reason]
            for reason in pick.get("soft_reasons") or []
            if reason in SOFT_REASON_LABELS
        ]
        if pick.get("in_lattice_band"):
            flags.append("预测带内")
        suffix = f"  [{'/'.join(flags)}]" if flags else ""
        zodiac = pick.get("zodiac_label") or ""
        lines.append(
            f"  {int(pick['number']):02d}  {int(pick['amount']):>3} 元"
            f"  {pick.get('role_label') or ''}  {pick.get('wave_label') or ''}"
            f"  {zodiac}{suffix}"
        )
    lines.append(f"合计 {budget.get('staked')} 元")
    if budget.get("unspent"):
        lines.append(
            f"（软降权/避冷省下 {budget.get('unspent')} 元未再分配，不补给其它注）"
        )
    lines.append("")
    lines.append(
        "角色 / 波动 / 带内 / 冷号都是既有设置的打标口径："
        "只影响筹码分配与号码卫生偏好，不代表中奖概率差异。"
    )
    lines.append("")
    lines.append(
        f"覆盖 {coverage.get('covered_count')}/{coverage.get('total_numbers')} 号；"
        f"大 {coverage.get('big_small', {}).get('big')} · "
        f"小 {coverage.get('big_small', {}).get('small')}；"
        f"奇 {coverage.get('odd_even', {}).get('odd')} · "
        f"偶 {coverage.get('odd_even', {}).get('even')}"
        f"（{coverage.get('covered_count')}/{coverage.get('total_numbers')} 只表示"
        f"引擎按名次选出的不同号个数，非摊开覆盖全部 {coverage.get('total_numbers')} 号）"
    )
    hit_rate = honest.get("baseline_hit_rate")
    hit_text = f"{hit_rate * 100:.2f}%" if hit_rate is not None else "n/a"
    lines.append(
        "诚实提示："
        f"每 100 元期望 {honest.get('ev_per_100'):+.2f} 元；"
        f"本票期望亏损 {honest.get('expected_loss_for_this_ticket'):.2f} 元；"
        f"命中概率 {hit_text}（不因选号改变）"
    )
    lines.append(
        f"历史参考：{in_sample.get('evaluated')} 期走步回测命中 {in_sample.get('hits')} 期"
        f"（{(in_sample.get('hit_rate') or 0) * 100:.2f}%）vs 随机 "
        f"{(in_sample.get('random_baseline_hit_rate') or 0) * 100:.2f}%；"
        f"判定 {in_sample.get('verdict')}"
    )
    lines.append(f"最久连续未中：{in_sample.get('max_dry_streak_periods')} 期")
    seed_value = ticket.get("seed", {}).get("value")
    seed_text = seed_value if seed_value is not None else "未指定（生产引擎既定名次）"
    lines.append(f"种子 {seed_text}；同数据 + 同设置 => 逐字节可复现")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def build_ticket(
    draws: Iterable[Mapping[str, Any]],
    *,
    settings: Mapping[str, Any] | None = None,
    budget: int | None = None,
    pick_count: int | None = None,
    seed: Any = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """生成一张出票单。

    ``draws`` 为开奖记录（任意顺序；字段 ``special_number`` / ``period`` /
    ``draw_date``）。``settings`` 为**有效设置**（通常是存储设置合并全局模板后的结果，
    与 ``POST /api/recommend`` 同源）；``None`` 时用仓库默认设置。
    ``budget`` / ``pick_count`` / ``mode`` 是本次出票的覆盖项（不写库）。

    同 ``seed`` + 同数据 + 同设置 ⇒ 逐字节相同的票据（无时间戳、无随机源）。
    """
    series = normalize_ticket_draws(draws)
    if len(series) < 2:
        raise TicketDataError(
            "数据不足：至少需要 2 期已导入开奖才能出票（无法计算上一期波动）。"
        )

    cfg = clamp_settings(dict(settings) if settings else None)
    if budget is not None:
        cfg["total_amount"] = budget
    if pick_count is not None:
        cfg["pick_count"] = pick_count
    if mode is not None and mode in MODES:
        cfg["mode"] = mode
    cfg = clamp_settings(cfg)

    latest = int(series[0]["special_number"])
    previous = int(series[1]["special_number"]) if len(series) > 1 else None
    history = [int(row["special_number"]) for row in series]
    dates = [row["draw_date"] for row in series]
    draws_asc = list(reversed(series))

    key = seed_key(seed)
    batch = batch_index(seed)
    effective_mode = cfg["mode"]
    unit = int(cfg["amount_unit"])
    requested_budget = int(cfg["total_amount"])
    plan_count = effective_pick_count(effective_mode, cfg["pick_count"])

    # 引擎既定名次：始终调用生产引擎；batch == 0 时直接采用它的号码与金额
    engine = recommend(
        latest=latest,
        previous=previous,
        history_numbers=history,
        settings=cfg,
        mode=effective_mode,
        amount_seed=key or None,
        period=series[0].get("period"),
        history_dates=dates,
    )

    # 出票状态（英文枚举）：ok = 正常出票；no_ticket = 零注（预算不足 1 个注码单位）。
    # 原因码与中文说明直接沿用生产引擎的结论，本层不另造口径。
    status = str(engine.get("status") or STATUS_OK)
    no_ticket_reason_code = engine.get("reason_code")
    no_ticket_reason_message = engine.get("reason_message")

    ctx = _engine_context(cfg, latest, history, dates)
    focus, prev_wave = _focus_order(previous, latest, cfg)

    if batch == 0:
        selection = SELECTION_ENGINE_PICKS
        picks = [_row_from_engine_pick(pick) for pick in engine.get("picks") or []]
        seed_key_value = engine.get("amount_seed") or ""
        plan = {
            "mode": effective_mode,
            "allocated_total": int(engine.get("staked_total") or 0),
            "pick_count": len(picks),
            "notes": [],
        }
        ranking: list[int] = []
        raw_picks = engine.get("picks") or []
    else:
        selection = SELECTION_ENGINE_RANKING
        ranking = _engine_ranking(cfg, latest, ctx)
        if not ranking:
            raise TicketDataError("数据不足：引擎候选池为空，无法出票。")
        plan = _amount_plan(effective_mode, requested_budget, plan_count, unit, key)
        count = min(int(plan["pick_count"]), len(ranking))
        offset = (batch * max(1, count)) % len(ranking)
        chosen = [ranking[(offset + index) % len(ranking)] for index in range(count)]
        raw_picks = [
            _row_from_number(number, cfg=cfg, latest=latest, ctx=ctx)
            for number in chosen
        ]
        picks = _allocate_ranking_rows(
            raw_picks,
            cfg=cfg,
            focus=focus,
            plan=plan,
            seed_key_value=key,
            ctx=ctx,
        )

    amounts = [int(pick.get("amount") or 0) for pick in picks]
    staked_total = sum(amounts)
    unspent = requested_budget - staked_total
    per_pick_min_respected = all(
        amount == 0 or amount >= MIN_BET_AMOUNT for amount in amounts
    )
    budget_block = {
        "requested": requested_budget,
        "staked": staked_total,
        "unspent": unspent,
        "currency": "CNY",
        "amount_unit": unit,
        "min_bet_amount": MIN_BET_AMOUNT,
        "per_pick_min_respected": per_pick_min_respected,
        "within_budget": staked_total <= requested_budget,
        "note": (
            "每注金额均为注码粒度（amount_unit）的正整数倍；数额合计等于 staked。"
            "被避冷加权省下的金额如实计入 unspent，绝不静默改配到其它注。"
            "（软降权已停用：重号 / 同肖 / 冷号只打标签，不再压低金额。）"
        ),
    }

    coverage = _coverage(
        [int(pick["number"]) for pick in picks],
        cfg=cfg,
        latest=latest,
        zodiac_date=ctx["zodiac_date"],
    )
    in_sample = _in_sample(draws_asc, cfg)
    honest = _honest_footer(
        cfg=cfg,
        pick_count=len(picks),
        stake_total=staked_total,
        in_sample=in_sample,
    )

    target_period = max(
        [row["period"] for row in series if row.get("period") is not None] or [0]
    ) + 1

    ticket: dict[str, Any] = {
        "ticket_version": TICKET_VERSION,
        "claim": CLAIM_NO_EDGE,
        "claim_label": CLAIM_LABELS[CLAIM_NO_EDGE],
        # 出票状态（英文枚举）：ok | no_ticket（零注 = 预算不足 1 个注码单位）
        "status": status,
        # 机器可读原因码（ok 时 null）；no_ticket 时为 BUDGET_TOO_SMALL_FOR_ONE_UNIT
        "reason_code": no_ticket_reason_code,
        # 中文人读说明（ok 时 null）；前端可直接展示
        "reason_message": no_ticket_reason_message,
        "scope": f"本池已导入 {len(series)} 期数据内",
        "data": {
            "data_status": DATA_STATUS_OK,
            "draws_used": len(series),
            "latest_number": latest,
            "latest_period": series[0].get("period"),
            "latest_draw_date": series[0]["draw_date"].isoformat(),
            "previous_number": previous,
            "target_period": target_period,
            "target_period_note": "按 max(period) + 1 建议，实际期号以后端录入为准。",
        },
        "selection": selection,
        "selection_label": SELECTION_LABELS[selection],
        "seed": {
            "value": seed,
            "key": key,
            "batch": batch,
            "reproducible": True,
            "note": (
                "同 seed + 同数据 + 同设置 => 逐字节可复现。"
                "seed 只决定「取引擎排序里的哪一段」，不改变中奖概率与期望值。"
            ),
        },
        "mode": effective_mode,
        "mode_label": MODE_LABELS.get(effective_mode, effective_mode),
        "settings": {**cfg, "big_min": derive_big_min(cfg["normal_max"])},
        "focus_order": [{"type": wave, "label": WAVE_LABELS[wave]} for wave in focus],
        "prev_wave": prev_wave,
        "picks": picks,
        "amounts": amounts,
        "budget": budget_block,
        "coverage": coverage,
        "honest": honest,
        "notes": [
            "本单只做花费控制、号码卫生、覆盖透明与留痕复现；"
            "不改变中奖概率，也不改变期望值。",
            "选号名次与金额分配全部来自生产引擎（services.lottery），本模块只做整理与披露。",
            "赔率是设定兑付倍数（含本金），不是收益承诺；请量力而行。",
        ],
    }
    if batch != 0:
        ticket["ranking_size"] = len(ranking)
    digest_payload = {
        "claim": ticket["claim"],
        "selection": selection,
        "seed": key,
        "batch": batch,
        "settings": _settings_digest(cfg),
        "latest": latest,
        "picks": [{"number": p["number"], "amount": p["amount"]} for p in picks],
        "data": f"{len(series)}|{series[0]['draw_date'].isoformat()}|{latest}",
    }
    ticket["ticket_id"] = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:32]
    ticket["ticket_text"] = _format_ticket_text(ticket)
    return ticket


def explain_effective_settings(
    settings: Mapping[str, Any] | None,
    *,
    stored: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """逐项说明「生效值来自哪里」+ 期望值算式（供 CLI / 调试用）。

    来源只有三种（英文枚举）：``default``（仓库默认）/ ``stored_settings``（存储设置）
    / ``request_override``（本次请求覆盖）。只做来源标注与算术展示，不写库。
    """
    cfg = clamp_settings(dict(settings) if settings else None)
    stored_cfg = dict(stored) if stored else {}
    override_cfg = {k: v for k, v in (overrides or {}).items() if v is not None}
    rows: list[dict[str, Any]] = []
    for key in sorted(DEFAULT_SETTINGS):
        effective = cfg.get(key)
        default = DEFAULT_SETTINGS.get(key)
        if key in override_cfg:
            source = "request_override"
        elif key in stored_cfg and stored_cfg.get(key) is not None:
            source = "stored_settings"
        else:
            source = "default"
        rows.append(
            {
                "key": key,
                "source": source,
                "stored_value": stored_cfg.get(key),
                "override_value": override_cfg.get(key),
                "default_value": default,
                "effective_value": effective,
                "changed_from_default": effective != default,
            }
        )
    odds = float(cfg["odds"])
    number_count = len(NUMBERS)
    return {
        "settings": cfg,
        "settings_source": {
            "provided": settings is not None,
            "stored_provided": bool(stored_cfg),
            "overrides": sorted(override_cfg),
        },
        "fields": rows,
        "odds": odds,
        "number_count": number_count,
        "ev_per_100_arithmetic": (
            f"ev_per_100 = 100 * (odds / {number_count} - 1) = "
            f"100 * ({odds:g} / {number_count} - 1) = "
            f"{(odds / number_count - 1.0) * 100.0:+.4f} 元"
        ),
        "ev_per_100": (odds / number_count - 1.0) * 100.0,
        "note": "生效值是「仓库默认 → 存储设置 → 本次请求覆盖」依次合并的结果。",
    }


def simulate_ticket(
    ticket: Mapping[str, Any], *, periods: int
) -> dict[str, Any]:
    """按这张票的**形状**（注数 / 金额 / 赔率）给出诚实的结果分布与期望亏损。

    单期只开一个特码，因此单期结果只有两种：命中所选 k 个号之一（概率 ``k/49``，
    净盈亏 = 命中那一注的金额 × 赔率 − 本期成本），或未中（净亏本期成本）。
    逐注精确加权求出单期期望与方差，再把 ``periods`` 期按独立同分布求和。
    """
    count = max(1, int(periods))
    picks = ticket.get("picks") or []
    honest = ticket.get("honest") or {}
    orders = len(picks)
    amounts = [int(p.get("amount") or 0) for p in picks]
    stake = int(honest.get("stake_total") or sum(amounts))
    number_count = len(NUMBERS)
    odds = float(honest.get("odds") or 0.0)
    hit_rate = orders / number_count if number_count else None

    def profit_on(amount: int) -> float:
        return amount * odds - stake

    per_pick = [
        {
            "number": int(p["number"]),
            "amount": int(p.get("amount") or 0),
            "profit_if_hit": profit_on(int(p.get("amount") or 0)),
        }
        for p in picks
    ]
    winners = sum(1 for row in per_pick if row["profit_if_hit"] > 0)

    # 单期分布（按 1/49 逐注加权）
    ps = 1.0 / number_count if number_count else 0.0
    mean_single = sum(ps * row["profit_if_hit"] for row in per_pick) + (
        1.0 - ps * orders
    ) * (-stake)
    second_moment = sum(ps * (row["profit_if_hit"] ** 2) for row in per_pick) + (
        1.0 - ps * orders
    ) * (stake**2)
    var_single = max(0.0, second_moment - mean_single**2)

    mean_total = mean_single * count
    sd_total = (var_single * count) ** 0.5
    p_no_hit = (1.0 - (hit_rate or 0.0)) ** count
    expected_staked = stake * count
    expected_loss = -mean_total
    from statistics import NormalDist

    # 注意方向：正态近似下「整段盈利」= P(总盈亏 > 0) = 1 - CDF(0)
    p_profit_positive = (
        (1.0 - NormalDist(mu=mean_total, sigma=sd_total).cdf(0.0))
        if sd_total > 0
        else None
    )
    return {
        "claim": CLAIM_NO_EDGE,
        "periods": count,
        "orders": orders,
        "stake_total": stake,
        "odds": odds,
        "hit_rate_per_period": hit_rate,
        "p_at_least_one_hit_period": 1.0 - p_no_hit,
        "p_no_hit_in_periods": p_no_hit,
        "hit_periods_mean": (hit_rate or 0.0) * count,
        "hit_periods_sd": (
            ((hit_rate or 0.0) * (1.0 - (hit_rate or 0.0)) * count) ** 0.5
        ),
        "expected_staked": expected_staked,
        "expected_profit": mean_total,
        "expected_loss": expected_loss,
        "profit_sd": sd_total,
        "profit_p05": mean_total - 1.6448536269514722 * sd_total,
        "profit_p95": mean_total + 1.6448536269514722 * sd_total,
        "p_profit_positive_normal_approx": p_profit_positive,
        "per_pick": per_pick,
        "net_positive_picks": winners,
        "note": (
            "这是等概率假设下的数学期望与波动，不是历史预测。"
            "命中期数多寡不改变「期望亏损为负」这一事实："
            "每 100 元期望仍为 odds/49 - 1。"
            "p05/p95 与 p_profit_positive 为正态近似，期数很少时仅供参考。"
        ),
    }


__all__ = [
    "CLAIM_NO_EDGE",
    "SELECTION_ENGINE_PICKS",
    "SELECTION_ENGINE_RANKING",
    "SOFT_REASON_LABELS",
    "TICKET_VERSION",
    "TicketDataError",
    "batch_index",
    "build_ticket",
    "explain_effective_settings",
    "normalize_ticket_draws",
    "seed_key",
    "simulate_ticket",
]
