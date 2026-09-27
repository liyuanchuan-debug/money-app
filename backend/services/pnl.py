"""特码投注快照结算与收益汇总 —— 纯函数，不依赖数据库。

口径（诚实 · 当前默认模拟）：
- 当前收益按**模拟买入**试算：用当期生成号码与设定赔率估算兑付 / 盈亏，
  **不是**真实投注记录，也不构成兑付承诺；
- 赔率 ``odds`` 是用户设定的兑付倍数，不是收益承诺；
- 特码玩法：中一注拿该注 ``金额 × 赔率``，未中为 0；
- 整期净盈亏 = 兑付合计 − 投注成本；
- 命中率是已结算期的经验频率，不是真实概率；
- 不做「稳赚」等表述。

预留：``stake_mode``（``SIMULATED`` / ``REAL`` / ``SKIPPED``）便于后期标记
真实买入或「未买」，以及逐期修正；本轮仅常量与落库默认，不做完整 UI。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# 状态英文枚举（落库禁止汉字）
STATUS_PENDING = "PENDING"
STATUS_SETTLED = "SETTLED"
STATUSES = (STATUS_PENDING, STATUS_SETTLED)

STATUS_LABELS = {
    STATUS_PENDING: "待开奖",
    STATUS_SETTLED: "已结算",
}

# 买入口径英文枚举（落库禁止汉字；展示用汉字 label）
# SIMULATED=模拟买入（当前默认）；REAL=真实买入；SKIPPED=未买（后期逐期修正用）
STAKE_MODE_SIMULATED = "SIMULATED"
STAKE_MODE_REAL = "REAL"
STAKE_MODE_SKIPPED = "SKIPPED"
STAKE_MODES = (STAKE_MODE_SIMULATED, STAKE_MODE_REAL, STAKE_MODE_SKIPPED)

STAKE_MODE_LABELS = {
    STAKE_MODE_SIMULATED: "模拟买入",
    STAKE_MODE_REAL: "真实买入",
    STAKE_MODE_SKIPPED: "未买",
}


def normalize_stake_mode(value: Any) -> str:
    """落库 / 出站统一为英文枚举；非法值回落 SIMULATED。"""
    raw = str(value or "").strip().upper()
    return raw if raw in STAKE_MODES else STAKE_MODE_SIMULATED


def money(value: Any) -> float:
    """金额规整为两位小数（元）。"""
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def snapshot_picks(picks: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """从 recommend() 的 picks 抽出可落库的精简快照。"""
    out: list[dict[str, Any]] = []
    for pick in picks or []:
        try:
            number = int(pick["number"])
            amount = money(pick.get("amount", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        out.append(
            {
                "number": number,
                "amount": amount,
                "role": str(pick.get("role") or ""),
                "role_label": str(pick.get("role_label") or ""),
                "hit": None,
            }
        )
    return out


def compute_cost(picks: list[dict[str, Any]] | None) -> float:
    """投注成本 = 各注金额之和。"""
    return money(sum(money(p.get("amount", 0)) for p in (picks or [])))


def settle_picks(
    picks: list[dict[str, Any]] | None,
    special_number: int,
    odds: float,
) -> dict[str, Any]:
    """按开奖特码结算一期快照。

    公式：
    - 单注兑付 = 金额 × 赔率（仅当 number == special_number）
    - 整期兑付 = 各注兑付之和
    - 净盈亏 = 兑付 − 成本
    """
    odds_value = money(odds)
    settled: list[dict[str, Any]] = []
    payout = 0.0
    any_hit = False
    for pick in snapshot_picks(picks):
        hit = int(pick["number"]) == int(special_number)
        pick_payout = money(pick["amount"] * odds_value) if hit else 0.0
        if hit:
            any_hit = True
        payout = money(payout + pick_payout)
        settled.append({**pick, "hit": hit, "payout": pick_payout})
    cost = compute_cost(settled)
    return {
        "picks": settled,
        "cost": cost,
        "payout": payout,
        "profit": money(payout - cost),
        "hit": any_hit,
        "odds": odds_value,
        "special_number": int(special_number),
        "status": STATUS_SETTLED,
    }


def build_pending_round(
    *,
    user_id: int,
    period: int,
    base_period: int | None,
    picks: list[dict[str, Any]],
    odds: float,
    mode: str,
    stake_mode: str = STAKE_MODE_SIMULATED,
) -> dict[str, Any]:
    """构造待开奖的一期快照（尚未对奖；默认模拟买入）。"""
    snap = snapshot_picks(picks)
    cost = compute_cost(snap)
    return {
        "user_id": int(user_id),
        "period": int(period),
        "base_period": int(base_period) if base_period is not None else None,
        "draw_date": None,
        "special_number": None,
        "odds": money(odds),
        "cost": cost,
        "payout": 0.0,
        "profit": money(0.0 - cost),  # 未开奖时暂记为 −成本（仪表对 PENDING 可另算）
        "hit": None,
        "mode": str(mode or ""),
        "picks": snap,
        "status": STATUS_PENDING,
        "stake_mode": normalize_stake_mode(stake_mode),
        "settled_at": None,
    }


def apply_settlement(
    round_row: dict[str, Any],
    special_number: int,
    draw_date: Any = None,
) -> dict[str, Any]:
    """把一期 PENDING（或已结算）行按特码重算并标为 SETTLED。"""
    odds = money(round_row.get("odds", 0))
    result = settle_picks(round_row.get("picks") or [], special_number, odds)
    updated = dict(round_row)
    updated.update(result)
    updated["draw_date"] = draw_date
    updated["settled_at"] = datetime.now(timezone.utc)
    return updated


def summarize_rounds(
    rounds: list[dict[str, Any]],
    *,
    current_odds: float,
    recent_limit: int = 20,
) -> dict[str, Any]:
    """汇总收益仪表数据。

    - 累计只统计 ``SETTLED`` 期（待开奖不计入命中率与累计盈亏）；
    - 命中率 = 命中期数 / 已结算期数（经验频率）。
    """
    settled = [r for r in rounds if r.get("status") == STATUS_SETTLED]
    pending = [r for r in rounds if r.get("status") == STATUS_PENDING]

    total_cost = money(sum(money(r.get("cost", 0)) for r in settled))
    total_payout = money(sum(money(r.get("payout", 0)) for r in settled))
    total_profit = money(total_payout - total_cost)
    hit_rounds = sum(1 for r in settled if r.get("hit") is True)
    settled_count = len(settled)
    hit_rate = (hit_rounds / settled_count) if settled_count else None

    # 按期号升序画累计盈亏走势
    ordered = sorted(settled, key=lambda r: (int(r.get("period") or 0), int(r.get("id") or 0)))
    cumulative = 0.0
    series: list[dict[str, Any]] = []
    for row in ordered:
        profit = money(row.get("profit", 0))
        cumulative = money(cumulative + profit)
        series.append(
            {
                "period": int(row["period"]),
                "profit": profit,
                "cumulative_profit": cumulative,
                "hit": bool(row.get("hit")),
            }
        )

    # 近期明细：期号倒序
    recent_source = sorted(
        rounds,
        key=lambda r: (int(r.get("period") or 0), int(r.get("id") or 0)),
        reverse=True,
    )[: max(1, int(recent_limit))]

    def _public(row: dict[str, Any]) -> dict[str, Any]:
        status = row.get("status") or STATUS_PENDING
        stake_mode = normalize_stake_mode(row.get("stake_mode"))
        return {
            "id": row.get("id"),
            "period": int(row["period"]),
            "base_period": row.get("base_period"),
            "draw_date": row.get("draw_date"),
            "special_number": row.get("special_number"),
            "odds": money(row.get("odds", 0)),
            "cost": money(row.get("cost", 0)),
            "payout": money(row.get("payout", 0)),
            "profit": money(row.get("profit", 0))
            if status == STATUS_SETTLED
            else None,
            "hit": row.get("hit"),
            "mode": row.get("mode"),
            "picks": row.get("picks") or [],
            "status": status,
            "status_label": STATUS_LABELS.get(status, status),
            "stake_mode": stake_mode,
            "stake_mode_label": STAKE_MODE_LABELS.get(stake_mode, stake_mode),
            "created_at": row.get("created_at"),
            "settled_at": row.get("settled_at"),
        }

    data_status = "OK" if settled_count > 0 else "INSUFFICIENT"
    notes = [
        "当前为模拟买入试算：按当期生成号码与设定赔率估算，非真实投注记录。",
        "赔率是用户设定的兑付倍数，不是收益承诺；不做确定性盈利表述。",
        "累计与命中率只统计已结算期；待开奖期单独列出。",
        "命中率是本池已结算期的经验频率，不是真实概率。",
    ]
    if settled_count == 0:
        notes.insert(
            0,
            "数据不足：还没有已结算的模拟采用期，无法计算累计模拟盈亏与命中率。",
        )

    return {
        "scope": f"本用户模拟买入快照内（已结算 {settled_count} 期）",
        "sample_size": settled_count,
        "data_status": data_status,
        "data_status_label": "样本可用" if data_status == "OK" else "数据不足",
        "odds": money(current_odds),
        "odds_note": (
            "当前设置中的兑付倍数；各期模拟兑付以采用当时的赔率为准。"
            "非真实下单兑付。"
        ),
        "summary": {
            "total_cost": total_cost,
            "total_payout": total_payout,
            "total_profit": total_profit,
            "settled_rounds": settled_count,
            "hit_rounds": hit_rounds,
            "miss_rounds": settled_count - hit_rounds,
            "hit_rate": hit_rate,
            "pending_rounds": len(pending),
        },
        "series": series,
        "recent": [_public(r) for r in recent_source],
        "notes": notes,
    }
