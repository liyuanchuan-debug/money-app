"""号码波动推荐算法 —— 纯函数实现，不依赖数据库。

规则来源见 README「业务规则」。所有金额单位为元。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

NUMBER_MIN = 1
NUMBER_MAX = 49
ZODIAC_STEP = 12

WAVE_SMALL = "small"
WAVE_NORMAL = "normal"
WAVE_BIG = "big"
WAVE_LABELS = {
    WAVE_SMALL: "小波动",
    WAVE_NORMAL: "常规波动",
    WAVE_BIG: "大跳",
}
WAVE_ORDER = [WAVE_SMALL, WAVE_NORMAL, WAVE_BIG]

ROLE_PRIMARY = "primary"
ROLE_SECONDARY = "secondary"
ROLE_DEFENSE = "defense"
ROLE_LABELS = {
    ROLE_PRIMARY: "主推",
    ROLE_SECONDARY: "次选",
    ROLE_DEFENSE: "防守",
}
ROLE_ORDER = [ROLE_PRIMARY, ROLE_SECONDARY, ROLE_DEFENSE]

MODE_EVEN = "even"
MODE_WEIGHTED = "weighted"
MODE_SINGLE = "single"
MODE_LABELS = {
    MODE_EVEN: "均注",
    MODE_WEIGHTED: "侧重",
    MODE_SINGLE: "单挑",
}
MODES = [MODE_EVEN, MODE_WEIGHTED, MODE_SINGLE]

# 波动回补逻辑：上期波动类型 → 本期侧重顺序（第一个即主推方向）
FOCUS_BY_PREV_WAVE: dict[str, list[str]] = {
    WAVE_SMALL: [WAVE_NORMAL, WAVE_BIG, WAVE_SMALL],
    WAVE_NORMAL: [WAVE_SMALL, WAVE_BIG, WAVE_NORMAL],
    WAVE_BIG: [WAVE_SMALL, WAVE_NORMAL, WAVE_BIG],
}
DEFAULT_FOCUS = [WAVE_SMALL, WAVE_NORMAL, WAVE_BIG]

DEFAULT_SETTINGS: dict[str, Any] = {
    "small_max": 10,
    "normal_max": 30,
    "bet_unit": 10,
    "mode": MODE_EVEN,
}


# --------------------------------------------------------------------------- #
# 基础规则
# --------------------------------------------------------------------------- #
def zodiac_group(number: int) -> int:
    """同肖分组键：n 与 n±12 同肖，1..49 共 12 组。"""
    return (number - 1) % ZODIAC_STEP


def zodiac_numbers(number: int, include_self: bool = True) -> list[int]:
    """返回与 number 同肖的全部号码（01/13/25/37/49 形式）。"""
    group = zodiac_group(number)
    found = [
        n for n in range(NUMBER_MIN, NUMBER_MAX + 1) if zodiac_group(n) == group
    ]
    if not include_self:
        found = [n for n in found if n != number]
    return found


def classify_wave(diff: int, small_max: int = 10, normal_max: int = 30) -> str:
    """相邻两期差值绝对值 → 波动类型。"""
    diff = abs(diff)
    if diff <= small_max:
        return WAVE_SMALL
    if diff <= normal_max:
        return WAVE_NORMAL
    return WAVE_BIG


def clamp_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    """合并用户配置与默认值，并做基本校验。"""
    merged = dict(DEFAULT_SETTINGS)
    for key, value in (raw or {}).items():
        if key in merged and value is not None and value != "":
            merged[key] = value

    merged["small_max"] = max(0, int(merged["small_max"]))
    merged["normal_max"] = max(merged["small_max"] + 1, int(merged["normal_max"]))
    merged["bet_unit"] = max(1, int(merged["bet_unit"]))
    if merged["mode"] not in MODES:
        merged["mode"] = MODE_EVEN
    return merged


# --------------------------------------------------------------------------- #
# 候选池
# --------------------------------------------------------------------------- #
def build_candidate_pools(
    latest: int, small_max: int, normal_max: int
) -> dict[str, list[dict[str, int]]]:
    """构造三期波动候选池，已排除最新号本身及其全部重肖号码。"""
    latest_group = zodiac_group(latest)
    pools: dict[str, list[dict[str, int]]] = {wave: [] for wave in WAVE_ORDER}

    for number in range(NUMBER_MIN, NUMBER_MAX + 1):
        if number == latest:
            continue
        if zodiac_group(number) == latest_group:  # 重肖，必须避开
            continue
        diff = abs(number - latest)
        wave = classify_wave(diff, small_max, normal_max)
        pools[wave].append({"number": number, "diff": diff})

    return pools


def order_pool(
    pool: list[dict[str, int]], history_counts: Counter[int]
) -> list[dict[str, int]]:
    """池内排序：先取历史遗漏最久的（出现次数少），再取与最新号差值最小的。"""
    return sorted(
        pool,
        key=lambda item: (
            history_counts.get(item["number"], 0),
            item["diff"],
            item["number"],
        ),
    )


def allocate_amounts(
    mode: str, roles: list[str], total: int
) -> list[int]:
    """按筹码模式把总额分配到各推荐号上。"""
    if not roles:
        return []

    if mode == MODE_SINGLE:
        return [total] + [0] * (len(roles) - 1)

    if mode == MODE_WEIGHTED:
        if len(roles) == 1:
            return [total]
        # 主推占 4/6（默认 20 元），其余均分（默认各 5 元）
        primary = total * 4 // 6
        rest = total - primary
        others = len(roles) - 1
        base = rest // others
        amounts = [primary] + [base] * others
        amounts[1] += rest - base * others  # 余数补给第一顺位次选
        return amounts

    # 均注
    base = total // len(roles)
    amounts = [base] * len(roles)
    amounts[0] += total - base * len(roles)
    return amounts


def build_copy_text(mode: str, picks: list[dict[str, Any]]) -> str:
    """生成一键复制的竞猜投注串。"""
    if not picks:
        return ""

    total = sum(p["amount"] for p in picks)
    joined = ",".join(f"{p['number']:02d}" for p in picks)

    if mode == MODE_EVEN:
        unit = picks[0]["amount"]
        return f"{joined} 各{unit}元 共{total}元"

    if mode == MODE_SINGLE:
        return f"{joined} {total}元"

    detail = ",".join(
        f"{p['number']:02d} {p['role_label']}{p['amount']}元" for p in picks
    )
    return f"{detail} 共{total}元"


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def recommend(
    latest: int,
    previous: int | None,
    history_numbers: Iterable[int],
    settings: dict[str, Any] | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """生成一期推荐。latest/previous 为号码本身，history_numbers 为历史开奖号序列。"""
    cfg = clamp_settings(settings)
    mode = mode if mode in MODES else cfg["mode"]
    unit = cfg["bet_unit"]
    total = unit * 3  # 默认 10 元/注 × 3 注

    history = list(history_numbers)
    # 最新号本身不计入遗漏统计
    history_counts: Counter[int] = Counter(
        n for n in history if n != latest
    )

    pools = build_candidate_pools(latest, cfg["small_max"], cfg["normal_max"])

    # 上期波动类型 → 本期侧重顺序
    prev_wave: dict[str, Any] | None = None
    if previous is not None:
        prev_diff = abs(latest - previous)
        prev_type = classify_wave(prev_diff, cfg["small_max"], cfg["normal_max"])
        prev_wave = {
            "number": previous,
            "diff": prev_diff,
            "type": prev_type,
            "label": WAVE_LABELS[prev_type],
        }
        focus = FOCUS_BY_PREV_WAVE[prev_type]
    else:
        focus = list(DEFAULT_FOCUS)

    pick_count = 1 if mode == MODE_SINGLE else 3
    ordered_pools = {w: order_pool(pools[w], history_counts) for w in WAVE_ORDER}

    # 规则 5：无论是否被选中，只要有某类波动彻底无解就明确标注
    missing_waves = [
        {
            "type": wave,
            "label": WAVE_LABELS[wave],
            "note": f"本期{WAVE_LABELS[wave]}无号",
        }
        for wave in WAVE_ORDER
        if not ordered_pools[wave]
    ]

    picks: list[dict[str, Any]] = []

    # 第一轮：按侧重顺序，每类波动取一个，优先保证分散
    for wave in focus:
        if len(picks) >= pick_count:
            break
        if ordered_pools[wave]:
            picks.append({**ordered_pools[wave].pop(0), "wave_type": wave})

    # 第二轮：某类无解导致不足注数时，从仍有候选的池中补齐（允许同类多取）
    for wave in focus:
        if len(picks) >= pick_count:
            break
        while len(picks) < pick_count and ordered_pools[wave]:
            picks.append({**ordered_pools[wave].pop(0), "wave_type": wave})

    roles = ROLE_ORDER[: len(picks)]
    amounts = allocate_amounts(mode, roles, total)

    for pick, role, amount in zip(picks, roles, amounts):
        pick["role"] = role
        pick["role_label"] = ROLE_LABELS[role]
        pick["amount"] = amount
        pick["wave_label"] = WAVE_LABELS[pick["wave_type"]]
        pick["is_repeat_zodiac"] = zodiac_group(pick["number"]) == zodiac_group(latest)

    notes: list[str] = []
    if previous is None:
        notes.append("历史不足两期，无法计算上期波动，本期按平均分散处理。")
    else:
        assert prev_wave is not None
        notes.append(
            f"上期{prev_wave['label']}（|{latest}-{previous}|={prev_wave['diff']}），"
            f"本期侧重{WAVE_LABELS[focus[0]]}。"
        )
    for item in missing_waves:
        notes.append(item["note"])

    return {
        "latest": latest,
        "latest_zodiac": zodiac_numbers(latest),
        "previous": previous,
        "prev_wave": prev_wave,
        "settings": cfg,
        "mode": mode,
        "mode_label": MODE_LABELS[mode],
        "bet_unit": unit,
        "total_amount": total,
        "focus_order": [{"type": w, "label": WAVE_LABELS[w]} for w in focus],
        "picks": picks,
        "missing_waves": missing_waves,
        "notes": notes,
        "copy_text": build_copy_text(mode, picks),
    }
