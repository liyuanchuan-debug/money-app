"""逆向博弈叠加层 —— 在引擎选号之上重排 10/5/3 档，允许少量换号。

口径（铁律 / NO_EDGE）：
- 样本内结构偏好 / 档位重排，**不是**庄家必开，不改变 P=k/49 与期望值；
- 「预字/明牌」= 点阵预测带内号；
- 遗漏 = ``days_since_last`` 自然日（``None`` = 样本内从未出现 → 极寒）；
- **大盘**：顺热=最新 N 肖；顺冷=非最新 N 肖；连 ≥3 → 防变盘反向；未满三 → 顺应原盘；
- **微观**：单肖连出 ≥3 → 该肖不做主攻；旺肖可抬热号权重，但上期肖受防重号铁律约束；
- **死穴**：极寒(100+)与预字只防不作主攻；
- **防重号 / 极寒接力**：
  上期原号开出前若遗漏 >80 天（破极寒），且同肖仍有 80–120 天极寒替补：
  替补强制进 10 元主攻；严禁同肖 10–40 天常规温号；原号最多进 3 元防重号。
  非极寒破局时：上期原号/同肖合适号仍二选一仅 1 个进 3 元。
"""

from __future__ import annotations

from datetime import date as Date
from typing import Any, Mapping, Sequence

from services.lottery import (
    DEFAULT_RECENT_ZODIAC_LIMIT,
    MODE_LADDER,
    NUMBER_MAX,
    NUMBER_MIN,
    ROLE_DEFENSE,
    ROLE_PRIMARY,
    ROLE_SECONDARY,
    WAVE_LABELS,
    classify_wave,
    recent_special_zodiac_groups,
    zodiac_group,
)

# 英文枚举：方向 / 宏观区（禁止汉字落库；汉字只进 notes / label）
DIRECTION_FOLLOW = "follow"
DIRECTION_REVERSE = "reverse"
DIRECTION_LABELS = {
    DIRECTION_FOLLOW: "顺应大盘（未到事不过三）",
    DIRECTION_REVERSE: "防变盘（大盘已满三）",
}

MACRO_HOT = "hot"  # 连续落在「最新 N 肖」= 热盘
MACRO_COLD = "cold"  # 连续落在「非最新 N 肖」= 冷盘
MACRO_MIXED = "mixed"
MACRO_LABELS = {
    MACRO_HOT: "热盘（最新N肖）",
    MACRO_COLD: "冷盘（非最新N肖）",
    MACRO_MIXED: "大盘未成形",
}

# prefer_zone 展示：顺热 / 顺冷
PREFER_HOT = "hot"
PREFER_COLD = "cold"
PREFER_LABELS = {
    PREFER_HOT: "顺热（最新N肖）",
    PREFER_COLD: "顺冷（非最新N肖）",
}

TAG_PREVIEW = "preview"
TAG_ULTRA_HOT = "ultra_hot"
TAG_ULTRA_COLD = "ultra_cold"
TAG_WARM_BLIND = "warm_blind"
TAG_ACTIVE_COLD = "active_cold"
TAG_OTHER = "other"

TAG_LABELS = {
    TAG_PREVIEW: "预字/带内",
    TAG_ULTRA_HOT: "极热/回马",
    TAG_ULTRA_COLD: "极寒",
    TAG_WARM_BLIND: "温号",
    TAG_ACTIVE_COLD: "活跃肖冷号",
    TAG_OTHER: "其它",
}

# 遗漏自然日阈值
MID_WARM_MIN = 20
MID_WARM_MAX = 50
COLD_TIP_MIN = 30
COLD_TIP_MAX = 60
WARM_DAYS_MIN = 15
WARM_DAYS_MAX = 60
ACTIVE_COLD_DAYS_MIN = 30
ULTRA_HOT_DAYS_MAX = 2
ULTRA_COLD_DAYS_MIN = 100
# 旺肖内遗漏超此天数的号禁止主攻
WANG_TIP_COLD_BAN_DAYS = 80
# 非极寒接力时：上期原号/同肖合适号共用防守名额有且仅 1 个
PREV_ZODIAC_MAX = 1
# 英文枚举
GUARD_KIND_REPEAT_NUMBER = "repeat_number"
GUARD_KIND_REPEAT_ZODIAC = "repeat_zodiac"
GUARD_KIND_COLD_RELAY = "cold_relay"
GUARD_KIND_LABELS = {
    GUARD_KIND_REPEAT_NUMBER: "防重号（上期原号）",
    GUARD_KIND_REPEAT_ZODIAC: "防重肖（同肖合适遗漏）",
    GUARD_KIND_COLD_RELAY: "极寒接力（同肖80-120替补）",
}
# 同肖号「遗漏最合适」窗口（非极寒接力时的 3 元防重肖）
SAME_ZODIAC_SUITABLE_MIN = 20
SAME_ZODIAC_SUITABLE_MAX = 60
SAME_ZODIAC_IDEAL_DAYS = 35
# 极寒破局 / 极寒替补 / 大众温号陷阱（自然日）
COLD_BREAK_MIN_DAYS = 80
COLD_RELAY_MIN_DAYS = 80
COLD_RELAY_MAX_DAYS = 120
COLD_RELAY_IDEAL_DAYS = 100
WARM_TRAP_MIN_DAYS = 10
WARM_TRAP_MAX_DAYS = 40

STREAK_REVERSE_AT = 3
# 近窗内同肖出现次数 ≥ 此值 → 旺肖（如 4 天出 3 天）
WANG_WINDOW = 4
WANG_MIN_HITS = 3

REVERSE_ROLE_LABELS = {
    ROLE_PRIMARY: "主攻",
    ROLE_SECONDARY: "中坚",
    ROLE_DEFENSE: "防守",
}

AMOUNT_TIP = 10
AMOUNT_MID = 5
AMOUNT_DEF = 3


def consecutive_zodiac_streak(history_numbers: Sequence[int]) -> tuple[int, int | None]:
    """历史最新在前：从最新一期起相邻同肖连出长度与生肖组。"""
    series = [
        int(n)
        for n in history_numbers
        if isinstance(n, (int, float)) and NUMBER_MIN <= int(n) <= NUMBER_MAX
    ]
    if not series:
        return 0, None
    group = zodiac_group(series[0])
    streak = 1
    for number in series[1:]:
        if zodiac_group(number) != group:
            break
        streak += 1
    return streak, group


def detect_wang_zodiac(
    history_numbers: Sequence[int],
    *,
    window: int = WANG_WINDOW,
    min_hits: int = WANG_MIN_HITS,
) -> tuple[bool, int | None, int]:
    """近窗高频旺肖：如近 4 期同肖 ≥3 次。

    返回 ``(is_wang, group, hits)``；无则 ``(False, None, 0)``。
    """
    series = [
        int(n)
        for n in history_numbers
        if isinstance(n, (int, float)) and NUMBER_MIN <= int(n) <= NUMBER_MAX
    ][: max(1, int(window))]
    if not series:
        return False, None, 0
    counts: dict[int, int] = {}
    for number in series:
        g = zodiac_group(number)
        counts[g] = counts.get(g, 0) + 1
    best_group, best_hits = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))
    if best_hits >= int(min_hits):
        return True, best_group, best_hits
    return False, None, 0


def analyze_macro_zone(
    history_numbers: Sequence[int],
    *,
    limit: int = DEFAULT_RECENT_ZODIAC_LIMIT,
) -> dict[str, Any]:
    """宏观大盘：最新 N 肖（热）vs 非最新 N 肖（冷）。

    「最新 N 肖」冻结为**不含最近 3 期**的更早历史上的 recent-N 不重复生肖
    （避免「最近 3 期必然落在含自身的最新 6 肖里」的同义反复）。
    未满三：顺应当前盘（热盘→顺热，冷盘→顺冷）；满三：反向防变盘。
    """
    series = [
        int(n)
        for n in history_numbers
        if isinstance(n, (int, float)) and NUMBER_MIN <= int(n) <= NUMBER_MAX
    ]
    prior = series[3:] if len(series) >= 3 else series
    hot_groups = recent_special_zodiac_groups(prior, limit=limit)
    if not hot_groups and series:
        hot_groups = recent_special_zodiac_groups(series, limit=limit)
    hot_set = set(hot_groups)

    if len(series) < 1 or not hot_set:
        return {
            "zone": MACRO_MIXED,
            "streak": 0,
            "hot_groups": sorted(hot_set),
            "prefer_zone": PREFER_HOT,
            "prefer_label": PREFER_LABELS[PREFER_HOT],
            "reverse": False,
        }

    tip_in_hot = zodiac_group(series[0]) in hot_set
    streak = 0
    if tip_in_hot:
        for number in series:
            if zodiac_group(number) in hot_set:
                streak += 1
            else:
                break
        zone = MACRO_HOT
    else:
        for number in series:
            if zodiac_group(number) not in hot_set:
                streak += 1
            else:
                break
        zone = MACRO_COLD

    reverse = streak >= STREAK_REVERSE_AT
    if reverse:
        # 热盘满三 → 防变盘顺冷；冷盘满三 → 防变盘顺热
        prefer_zone = PREFER_COLD if zone == MACRO_HOT else PREFER_HOT
    else:
        # 未满三：顺应原盘
        prefer_zone = PREFER_HOT if zone == MACRO_HOT else PREFER_COLD

    return {
        "zone": zone,
        "streak": int(streak),
        "hot_groups": sorted(hot_set),
        "prefer_zone": prefer_zone,
        "prefer_label": PREFER_LABELS[prefer_zone],
        "reverse": bool(reverse),
    }


def classify_number_tag(
    number: int,
    *,
    days_since_last: int | None,
    in_band: bool,
    hot_zodiac_group: int | None,
) -> str:
    """互斥标签：预字 > 极热 > 极寒 > 温号 > 活跃肖冷号 > 其它。"""
    if in_band:
        return TAG_PREVIEW
    if days_since_last is not None and 0 <= int(days_since_last) <= ULTRA_HOT_DAYS_MAX:
        return TAG_ULTRA_HOT
    if days_since_last is None or int(days_since_last) > ULTRA_COLD_DAYS_MIN:
        return TAG_ULTRA_COLD
    days = int(days_since_last)
    if WARM_DAYS_MIN <= days <= WARM_DAYS_MAX:
        return TAG_WARM_BLIND
    if (
        hot_zodiac_group is not None
        and zodiac_group(number) == int(hot_zodiac_group)
        and days >= ACTIVE_COLD_DAYS_MIN
    ):
        return TAG_ACTIVE_COLD
    return TAG_OTHER


def stake_tier_counts(pick_count: int) -> tuple[int, int, int]:
    """``(10元注数, 5元注数, 3元注数)``：2 / 最多3 / 其余。"""
    n = max(0, int(pick_count))
    tip_n = min(2, n)
    mid_n = min(3, max(0, n - tip_n))
    def_n = n - tip_n - mid_n
    return tip_n, mid_n, def_n


def _pick_skeleton(
    number: int,
    *,
    latest: int,
    small_max: int,
    normal_max: int,
) -> dict[str, Any]:
    diff = abs(int(number) - int(latest))
    wave = classify_wave(diff, small_max, normal_max)
    return {
        "number": int(number),
        "diff": diff,
        "wave_type": wave,
        "wave_label": WAVE_LABELS[wave],
    }


def _force_into_defense(
    selected: list[int],
    *,
    number: int,
    tip_n: int,
    mid_n: int,
    def_n: int,
    used: set[int],
) -> list[int]:
    """把 ``number`` 放进 3 元防守档（替换末位防守或从主攻/中坚挪入）。"""
    if def_n <= 0:
        return selected
    target = tip_n + mid_n + def_n
    number = int(number)
    if number in selected[tip_n + mid_n : target]:
        return selected
    selected = [n for n in selected if n != number]
    used.discard(number)
    head = selected[: tip_n + mid_n]
    tail = selected[tip_n + mid_n : target - 1]
    out = head + tail
    filler = [
        n
        for n in range(NUMBER_MIN, NUMBER_MAX + 1)
        if n not in used and n != number and n not in out
    ]
    while len(out) < target - 1 and filler:
        n = filler.pop(0)
        out.append(n)
        used.add(n)
    out.append(number)
    used.add(number)
    return out[:target]


def compute_latest_pre_omit_days(
    history_numbers: Sequence[int],
    history_dates: Sequence[Any] | None,
    latest: int,
) -> int | None:
    """上期原号在开出前的遗漏自然日；样本内此前从未出现 → ``None``（视作极寒）。"""
    latest = int(latest)
    series = [
        int(n)
        for n in history_numbers
        if isinstance(n, (int, float)) and NUMBER_MIN <= int(n) <= NUMBER_MAX
    ]
    if not series or series[0] != latest:
        # 仍按值搜：找到最新一期该号后再找更早一次
        tip_idx = next((i for i, n in enumerate(series) if n == latest), None)
        if tip_idx is None:
            return None
    else:
        tip_idx = 0
    prev_idx = next(
        (i for i, n in enumerate(series[tip_idx + 1 :], tip_idx + 1) if n == latest),
        None,
    )
    if prev_idx is None:
        return None
    if history_dates is not None:
        parsed: list[Date | None] = []
        for raw in history_dates:
            if isinstance(raw, Date):
                parsed.append(raw)
            else:
                parsed.append(None)
        tip_d, prev_d = parsed[tip_idx] if tip_idx < len(parsed) else None, (
            parsed[prev_idx] if prev_idx < len(parsed) else None
        )
        if len(parsed) == len(series) and tip_d is not None and prev_d is not None:
            return int((tip_d - prev_d).days)
    return int(prev_idx - tip_idx)


def is_cold_break(latest_pre_omit_days: int | None) -> bool:
    """上期原号开出前遗漏 >80 天（或样本内首次出现）→ 极寒破局。"""
    if latest_pre_omit_days is None:
        return True
    return int(latest_pre_omit_days) > COLD_BREAK_MIN_DAYS


def _pick_same_zodiac_suitable(
    *,
    latest: int,
    prev_group: int,
    blocked: set[int],
    days_since_last: Mapping[int, int | None],
) -> int | None:
    """上期肖里选遗漏最合适的同肖号（不含原号）；窗口外返回 None。"""
    candidates: list[int] = []
    for number in range(NUMBER_MIN, NUMBER_MAX + 1):
        if number == int(latest):
            continue
        if number in blocked:
            continue
        if zodiac_group(number) != int(prev_group):
            continue
        days = days_since_last.get(number)
        if days is None:
            continue
        if not (SAME_ZODIAC_SUITABLE_MIN <= int(days) <= SAME_ZODIAC_SUITABLE_MAX):
            continue
        # 避开 10–40 大众温号陷阱
        if WARM_TRAP_MIN_DAYS <= int(days) <= WARM_TRAP_MAX_DAYS:
            continue
        candidates.append(number)
    if not candidates:
        return None

    def _suit_key(number: int) -> tuple:
        days = int(days_since_last.get(number) or SAME_ZODIAC_IDEAL_DAYS)
        return (abs(days - SAME_ZODIAC_IDEAL_DAYS), number)

    candidates.sort(key=_suit_key)
    return candidates[0]


def _pick_cold_relay_number(
    *,
    latest: int,
    prev_group: int,
    blocked: set[int],
    days_since_last: Mapping[int, int | None],
) -> int | None:
    """同肖极寒替补：遗漏落在 80–120 天，优先靠近 100。"""
    candidates: list[int] = []
    for number in range(NUMBER_MIN, NUMBER_MAX + 1):
        if number == int(latest):
            continue
        if number in blocked:
            continue
        if zodiac_group(number) != int(prev_group):
            continue
        days = days_since_last.get(number)
        if days is None:
            continue
        if not (COLD_RELAY_MIN_DAYS <= int(days) <= COLD_RELAY_MAX_DAYS):
            continue
        candidates.append(number)
    if not candidates:
        return None

    def _relay_key(number: int) -> tuple:
        days = int(days_since_last.get(number) or COLD_RELAY_IDEAL_DAYS)
        return (abs(days - COLD_RELAY_IDEAL_DAYS), number)

    candidates.sort(key=_relay_key)
    return candidates[0]


def pick_prev_guard_slot(
    *,
    latest: int,
    blocked: set[int],
    days_since_last: Mapping[int, int | None],
) -> tuple[int, str]:
    """非极寒接力时二选一：有合适同肖号 → 防重肖；否则 → 防重号（原号）。"""
    latest = int(latest)
    prev_group = zodiac_group(latest)
    partner = _pick_same_zodiac_suitable(
        latest=latest,
        prev_group=prev_group,
        blocked=blocked,
        days_since_last=days_since_last,
    )
    if partner is not None:
        return int(partner), GUARD_KIND_REPEAT_ZODIAC
    return latest, GUARD_KIND_REPEAT_NUMBER


def _is_warm_trap(
    number: int,
    *,
    prev_group: int,
    days_since_last: Mapping[int, int | None],
) -> bool:
    if zodiac_group(number) != int(prev_group):
        return False
    days = days_since_last.get(number)
    if days is None:
        return False
    return WARM_TRAP_MIN_DAYS <= int(days) <= WARM_TRAP_MAX_DAYS


def _other_cold_zodiac_warm_pool(
    *,
    prev_group: int,
    hot_groups: set[int],
    blocked: set[int],
    days_since_last: Mapping[int, int | None],
    used: set[int],
) -> list[int]:
    """其它冷肖（非上期肖、优先非最新N肖）的温号，用于替换多余同肖。"""

    def _rank(number: int) -> tuple:
        days = days_since_last.get(number)
        group = zodiac_group(number)
        cold_zodiac = 0 if (not hot_groups or group not in hot_groups) else 1
        if days is not None and MID_WARM_MIN <= int(days) <= MID_WARM_MAX:
            warm = abs(int(days) - 35)
        elif days is not None and WARM_DAYS_MIN <= int(days) <= WARM_DAYS_MAX:
            warm = 50 + abs(int(days) - 35)
        else:
            warm = 900
        return (cold_zodiac, warm, number)

    pool = [
        n
        for n in range(NUMBER_MIN, NUMBER_MAX + 1)
        if n not in used
        and n not in blocked
        and zodiac_group(n) != int(prev_group)
    ]
    return sorted(pool, key=_rank)


def enforce_prev_zodiac_cap(
    selected: list[int],
    *,
    latest: int,
    tip_n: int,
    mid_n: int,
    def_n: int,
    blocked: set[int],
    days_since_last: Mapping[int, int | None],
    hot_groups: set[int],
    latest_pre_omit_days: int | None,
) -> tuple[list[int], int, str, int | None]:
    """上期肖重构：极寒接力优先，否则原号/同肖二选一进 3 元。

    返回 ``(新票, 防守号, defense_kind, 主攻极寒替补或 None)``。
    """
    latest = int(latest)
    prev_group = zodiac_group(latest)
    target = tip_n + mid_n + def_n

    cold_break = is_cold_break(latest_pre_omit_days)
    cold_relay = (
        _pick_cold_relay_number(
            latest=latest,
            prev_group=prev_group,
            blocked=blocked,
            days_since_last=days_since_last,
        )
        if cold_break
        else None
    )

    if cold_relay is not None:
        # 极寒接力：替补进 10 元；原号进 3 元；禁 10–40 温号
        tip_extra = int(cold_relay)
        guard_number = latest
        guard_kind = GUARD_KIND_REPEAT_NUMBER
        allowed_prev = {latest, tip_extra}
    else:
        tip_extra = None
        guard_number, guard_kind = pick_prev_guard_slot(
            latest=latest,
            blocked=blocked,
            days_since_last=days_since_last,
        )
        allowed_prev = {int(guard_number)}

    if target <= 0:
        return selected, int(guard_number), str(guard_kind), tip_extra

    # 清掉上期肖与温号陷阱，主攻/中坚先只用其它肖
    core = [
        n
        for n in selected
        if zodiac_group(n) != prev_group
        and not (
            cold_relay is not None
            and _is_warm_trap(n, prev_group=prev_group, days_since_last=days_since_last)
        )
    ]

    tip: list[int] = []
    mid: list[int] = []
    used: set[int] = set()

    # 极寒替补强制占主攻第一位
    if tip_extra is not None and tip_n > 0:
        tip.append(tip_extra)
        used.add(tip_extra)

    for number in core:
        if number in used:
            continue
        if len(tip) < tip_n:
            tip.append(number)
            used.add(number)
        elif len(mid) < mid_n:
            mid.append(number)
            used.add(number)
        else:
            break

    warm_pool = _other_cold_zodiac_warm_pool(
        prev_group=prev_group,
        hot_groups=hot_groups,
        blocked=blocked,
        days_since_last=days_since_last,
        used=used,
    )
    for number in warm_pool:
        if len(tip) >= tip_n and len(mid) >= mid_n:
            break
        if number in used:
            continue
        if cold_relay is not None and _is_warm_trap(
            number, prev_group=prev_group, days_since_last=days_since_last
        ):
            continue
        if len(tip) < tip_n:
            tip.append(number)
            used.add(number)
        elif len(mid) < mid_n:
            mid.append(number)
            used.add(number)

    # 防守：原号（或非接力时的唯一名额）
    defense: list[int] = []
    if def_n > 0 and int(guard_number) not in used:
        defense.append(int(guard_number))
        used.add(int(guard_number))

    defense_fill = [
        n for n in selected if n not in used and zodiac_group(n) != prev_group
    ] + [
        n
        for n in _other_cold_zodiac_warm_pool(
            prev_group=prev_group,
            hot_groups=hot_groups,
            blocked=blocked,
            days_since_last=days_since_last,
            used=used,
        )
        if n not in used
    ]
    for number in defense_fill:
        if len(defense) >= def_n:
            break
        if number in used:
            continue
        if cold_relay is not None and _is_warm_trap(
            number, prev_group=prev_group, days_since_last=days_since_last
        ):
            continue
        if zodiac_group(number) == prev_group and number not in allowed_prev:
            continue
        defense.append(number)
        used.add(number)

    if len(tip) + len(mid) + len(defense) < target:
        for number in range(NUMBER_MIN, NUMBER_MAX + 1):
            if number in used:
                continue
            if number in blocked and number not in allowed_prev:
                continue
            if zodiac_group(number) == prev_group and number not in allowed_prev:
                continue
            if cold_relay is not None and _is_warm_trap(
                number, prev_group=prev_group, days_since_last=days_since_last
            ):
                continue
            if len(tip) < tip_n:
                tip.append(number)
            elif len(mid) < mid_n:
                mid.append(number)
            elif len(defense) < def_n:
                defense.append(number)
            else:
                break
            used.add(number)

    out = (tip + mid + defense)[:target]
    return out, int(guard_number), str(guard_kind), tip_extra


def apply_reverse_game(
    picks: list[dict[str, Any]],
    *,
    enabled: bool,
    mode: str,
    latest: int,
    previous: int | None,
    history_numbers: Sequence[int],
    days_since_last: Mapping[int, int | None],
    in_band_numbers: Sequence[int] | None = None,
    small_max: int = 10,
    normal_max: int = 30,
    streak2_blocked: Sequence[int] | None = None,
    recent_zodiac_limit: int = DEFAULT_RECENT_ZODIAC_LIMIT,
    history_dates: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """在引擎 picks 上叠加逆向博弈定盘选号。"""
    del previous  # 防同号用 latest（上期特码）
    band_set = {int(n) for n in (in_band_numbers or [])}
    blocked = {int(n) for n in (streak2_blocked or [])}
    prev_group = zodiac_group(int(latest))
    latest_pre_omit_days = compute_latest_pre_omit_days(
        history_numbers, history_dates, int(latest)
    )
    cold_break_on = is_cold_break(latest_pre_omit_days)
    cold_relay_preview = (
        _pick_cold_relay_number(
            latest=int(latest),
            prev_group=prev_group,
            blocked=set(),
            days_since_last=days_since_last,
        )
        if cold_break_on
        else None
    )
    if cold_relay_preview is not None:
        _guard_preview, _guard_kind_preview = int(latest), GUARD_KIND_REPEAT_NUMBER
    else:
        _guard_preview, _guard_kind_preview = pick_prev_guard_slot(
            latest=int(latest),
            blocked=set(),
            days_since_last=days_since_last,
        )
    blocked.discard(int(latest))
    blocked.discard(int(_guard_preview))
    if cold_relay_preview is not None:
        blocked.discard(int(cold_relay_preview))

    micro_streak, micro_group = consecutive_zodiac_streak(history_numbers)
    wang_on, wang_group, wang_hits = detect_wang_zodiac(history_numbers)
    macro = analyze_macro_zone(
        history_numbers, limit=int(recent_zodiac_limit) or DEFAULT_RECENT_ZODIAC_LIMIT
    )
    hot_groups = set(macro["hot_groups"])
    prefer_zone = str(macro["prefer_zone"])  # hot | cold
    prefer_label = str(macro.get("prefer_label") or PREFER_LABELS.get(prefer_zone, prefer_zone))

    micro_reverse = micro_streak >= STREAK_REVERSE_AT
    macro_reverse = bool(macro["reverse"])
    direction = (
        DIRECTION_REVERSE if macro_reverse else DIRECTION_FOLLOW
    )
    comeback_tip_ok = False

    empty_summary: dict[str, Any] = {
        "enabled": bool(enabled),
        "applied": False,
        "direction": direction,
        "direction_label": DIRECTION_LABELS[direction],
        "zodiac_streak": int(micro_streak),
        "hot_zodiac_group": micro_group,
        "wang_zodiac": bool(wang_on),
        "wang_zodiac_group": wang_group,
        "wang_hits": int(wang_hits),
        "macro_zone": macro["zone"],
        "macro_zone_label": MACRO_LABELS.get(macro["zone"], macro["zone"]),
        "macro_streak": int(macro["streak"]),
        "prefer_zone": prefer_zone,
        "prefer_label": prefer_label,
        "hot_zodiac_groups": sorted(hot_groups),
        "preview_numbers": sorted(band_set),
        "tip_numbers": [],
        "mid_numbers": [],
        "defense_numbers": [],
        "repeat_guard_number": int(_guard_preview),
        "prev_guard_kind": _guard_kind_preview,
        "prev_guard_label": GUARD_KIND_LABELS.get(_guard_kind_preview),
        "cold_break": bool(cold_break_on),
        "latest_pre_omit_days": latest_pre_omit_days,
        "cold_relay_number": cold_relay_preview,
        "prev_zodiac_group": int(prev_group),
        "prev_zodiac_max": PREV_ZODIAC_MAX,
        "same_zodiac_cold_partner": (
            int(_guard_preview)
            if _guard_kind_preview == GUARD_KIND_REPEAT_ZODIAC
            else None
        ),
        "comeback_number": None,
        "replaced_numbers": [],
        "rule": (
            "大盘：顺热/顺冷；连≥3防变盘；"
            "极寒破局(>80天)：同肖80-120替补强制10元，禁10-40温号，原号仅3元；"
            "非破局：防重号/防重肖二选一仅3元；极寒/预字只防；不承诺命中率。"
        ),
    }

    if not enabled:
        return {
            "picks": picks,
            "summary": {
                **empty_summary,
                "enabled": False,
                "direction": None,
                "direction_label": None,
                "prefer_label": None,
            },
            "notes": ["逆向博弈叠加已关闭：本期沿用引擎选号与原档位金额。"],
            "applied": False,
        }

    if not picks:
        return {
            "picks": picks,
            "summary": empty_summary,
            "notes": [
                "逆向博弈叠加已开启，但本期零注，无法重排档位。"
                "这是样本内结构偏好，不是庄家必开，也不承诺提高命中率。"
            ],
            "applied": False,
        }

    tags: dict[int, str] = {}
    for number in range(NUMBER_MIN, NUMBER_MAX + 1):
        tags[number] = classify_number_tag(
            number,
            days_since_last=days_since_last.get(number),
            in_band=number in band_set,
            hot_zodiac_group=wang_group if wang_on else micro_group,
        )

    tip_n, mid_n, def_n = stake_tier_counts(len(picks))
    engine_numbers = [int(p["number"]) for p in picks]
    engine_set = set(engine_numbers)

    def _in_prefer_zone(number: int) -> bool:
        if not hot_groups:
            return True
        group = zodiac_group(number)
        if prefer_zone == PREFER_COLD:
            return group not in hot_groups
        return group in hot_groups

    def _in_reverse_zone(number: int) -> bool:
        """防变盘：与 prefer 相反的大盘区。"""
        if not hot_groups:
            return False
        return not _in_prefer_zone(number)

    def _allowed(number: int) -> bool:
        return NUMBER_MIN <= number <= NUMBER_MAX and number not in blocked

    def _days(number: int) -> int | None:
        return days_since_last.get(number)

    def _dead_tip(number: int) -> bool:
        """死穴：预字、极寒(100+)；旺肖内遗漏>80 也禁主攻。"""
        if tags[number] in (TAG_PREVIEW, TAG_ULTRA_COLD):
            return True
        days = _days(number)
        if (
            wang_on
            and wang_group is not None
            and zodiac_group(number) == int(wang_group)
            and (days is None or int(days) > WANG_TIP_COLD_BAN_DAYS)
        ):
            return True
        return False

    def _micro_tip_ban(number: int) -> bool:
        return bool(
            micro_reverse
            and micro_group is not None
            and zodiac_group(number) == micro_group
        )

    def _is_prev_zodiac(number: int) -> bool:
        return zodiac_group(number) == prev_group

    def _tip_ok(number: int) -> bool:
        # 极寒替补允许进主攻；上期原号 / 其它上期肖 / 10–40 温号禁主攻
        if cold_relay_preview is not None and number == int(cold_relay_preview):
            return True
        if number == int(latest) or _is_prev_zodiac(number):
            return False
        if cold_break_on and _is_warm_trap(
            number, prev_group=prev_group, days_since_last=days_since_last
        ):
            return False
        if _dead_tip(number):
            return False
        if _micro_tip_ban(number):
            return False
        if not _in_prefer_zone(number):
            return False
        days = _days(number)
        if prefer_zone == PREFER_HOT:
            return tags[number] in (
                TAG_ULTRA_HOT,
                TAG_WARM_BLIND,
                TAG_ACTIVE_COLD,
                TAG_OTHER,
            )
        if days is None:
            return False
        return COLD_TIP_MIN <= int(days) <= COLD_TIP_MAX

    def _mid_ok(number: int) -> bool:
        # 上期原号 / 上期肖 / 极寒替补 / 温号陷阱：严禁 5 元
        if number == int(latest) or _is_prev_zodiac(number):
            return False
        if cold_relay_preview is not None and number == int(cold_relay_preview):
            return False
        if cold_break_on and _is_warm_trap(
            number, prev_group=prev_group, days_since_last=days_since_last
        ):
            return False
        if tags[number] == TAG_PREVIEW:
            return False
        if not _in_prefer_zone(number):
            return False
        days = _days(number)
        if days is None:
            return False
        return MID_WARM_MIN <= int(days) <= MID_WARM_MAX

    def _rank_tip(number: int) -> tuple:
        days = _days(number)
        tag = tags[number]
        if prefer_zone == PREFER_HOT:
            if tag == TAG_ULTRA_HOT:
                tip_rank = 1
            elif tag == TAG_WARM_BLIND and days is not None and MID_WARM_MIN <= days <= MID_WARM_MAX:
                tip_rank = 2
            else:
                tip_rank = 3
            hotness = int(days) if days is not None else 999
            in_engine = 0 if number in engine_set else 1
            return (tip_rank, hotness, in_engine, number)
        # 顺冷：30–60 中段优先
        if days is not None and COLD_TIP_MIN <= days <= COLD_TIP_MAX:
            warm_dist = abs(int(days) - 45)
        else:
            warm_dist = 999
        in_engine = 0 if number in engine_set else 1
        return (0, warm_dist, in_engine, number)

    def _rank_mid(number: int) -> tuple:
        days = _days(number)
        if days is not None and MID_WARM_MIN <= days <= MID_WARM_MAX:
            warm_dist = abs(int(days) - 35)
        else:
            warm_dist = 999
        in_engine = 0 if number in engine_set else 1
        return (warm_dist, in_engine, number)

    def _rank_def(number: int) -> tuple:
        # 防守：上期原号 > 同肖冷号 > 预字 > 极寒 > 反向大盘 > 其它
        if number == int(latest):
            return (0, 0, number)
        if _is_prev_zodiac(number):
            days = _days(number)
            cold = 0 if days is None else -int(days)
            return (1, cold, number)
        tag = tags[number]
        tag_rank = {
            TAG_PREVIEW: 2,
            TAG_ULTRA_COLD: 3,
            TAG_ULTRA_HOT: 5,
            TAG_OTHER: 6,
            TAG_WARM_BLIND: 7,
            TAG_ACTIVE_COLD: 8,
        }.get(tag, 9)
        zone_boost = 0 if _in_reverse_zone(number) else 1
        in_engine = 0 if number in engine_set else 1
        return (tag_rank, zone_boost, in_engine, number)

    tip_pool = sorted(
        (n for n in range(NUMBER_MIN, NUMBER_MAX + 1) if _allowed(n) and _tip_ok(n)),
        key=_rank_tip,
    )
    mid_pool = sorted(
        (n for n in range(NUMBER_MIN, NUMBER_MAX + 1) if _allowed(n) and _mid_ok(n)),
        key=_rank_mid,
    )
    def_pool = sorted(
        (n for n in range(NUMBER_MIN, NUMBER_MAX + 1) if _allowed(n) or n == int(latest)),
        key=_rank_def,
    )

    selected: list[int] = []
    used: set[int] = set()

    def _take_from(pool: Sequence[int], need: int) -> None:
        for number in pool:
            if len(selected) >= need:
                return
            if number in used:
                continue
            used.add(number)
            selected.append(number)

    _take_from(tip_pool, tip_n)
    if len(selected) < tip_n:
        fallback_tip = sorted(
            (
                n
                for n in engine_numbers
                if n not in used
                and _allowed(n)
                and not _is_prev_zodiac(n)
                and not _dead_tip(n)
                and not _micro_tip_ban(n)
                and _in_prefer_zone(n)
            ),
            key=_rank_tip,
        )
        _take_from(fallback_tip, tip_n)

    _take_from(mid_pool, tip_n + mid_n)
    if len(selected) < tip_n + mid_n:
        _take_from(
            [
                n
                for n in engine_numbers
                if n not in used
                and _allowed(n)
                and not _is_prev_zodiac(n)
                and _in_prefer_zone(n)
            ],
            tip_n + mid_n,
        )

    _take_from(def_pool, tip_n + mid_n + def_n)
    if len(selected) < tip_n + mid_n + def_n:
        _take_from(
            [
                n
                for n in range(NUMBER_MIN, NUMBER_MAX + 1)
                if n not in used and (_allowed(n) or n == int(latest))
            ],
            tip_n + mid_n + def_n,
        )

    target_n = len(picks)
    selected, prev_guard_number, prev_guard_kind, cold_relay_number = (
        enforce_prev_zodiac_cap(
            selected[:target_n] if len(selected) > target_n else selected,
            latest=int(latest),
            tip_n=tip_n,
            mid_n=mid_n,
            def_n=def_n,
            blocked=blocked,
            days_since_last=days_since_last,
            hot_groups=hot_groups,
            latest_pre_omit_days=latest_pre_omit_days,
        )
    )
    same_zodiac_cold_partner = (
        int(prev_guard_number)
        if prev_guard_kind == GUARD_KIND_REPEAT_ZODIAC
        else None
    )
    allowed_prev_fill = {int(prev_guard_number)}
    if cold_relay_number is not None:
        allowed_prev_fill.add(int(cold_relay_number))
    used = set(selected)
    while len(selected) < target_n:
        progressed = False
        for number in engine_numbers + list(range(NUMBER_MIN, NUMBER_MAX + 1)):
            if number in used:
                continue
            if number in blocked and number not in allowed_prev_fill:
                continue
            if (
                zodiac_group(number) == prev_group
                and number not in allowed_prev_fill
            ):
                continue
            if cold_break_on and _is_warm_trap(
                number, prev_group=prev_group, days_since_last=days_since_last
            ):
                continue
            used.add(number)
            selected.append(number)
            progressed = True
            break
        if not progressed:
            break

    tip_numbers = selected[:tip_n]
    mid_numbers = selected[tip_n : tip_n + mid_n]
    defense_numbers = selected[tip_n + mid_n : tip_n + mid_n + def_n]

    old_by_number = {int(p["number"]): p for p in picks}
    new_picks: list[dict[str, Any]] = []
    ladder_mode = mode == MODE_LADDER
    amounts_plan = (
        [AMOUNT_TIP] * tip_n + [AMOUNT_MID] * mid_n + [AMOUNT_DEF] * def_n
    )
    roles_plan = (
        [ROLE_PRIMARY] * tip_n + [ROLE_SECONDARY] * mid_n + [ROLE_DEFENSE] * def_n
    )

    for index, number in enumerate(selected):
        base = old_by_number.get(number)
        if base is None:
            row = _pick_skeleton(
                number, latest=latest, small_max=small_max, normal_max=normal_max
            )
        else:
            row = dict(base)
            row["number"] = number
            row["diff"] = abs(number - int(latest))
            row["wave_type"] = classify_wave(row["diff"], small_max, normal_max)
            row["wave_label"] = WAVE_LABELS[row["wave_type"]]

        tag = tags[number]
        row["reverse_tag"] = tag
        row["reverse_tag_label"] = TAG_LABELS.get(tag, tag)
        row["days_since_last"] = days_since_last.get(number)
        row["in_lattice_band"] = bool(number in band_set)

        if ladder_mode and index < len(amounts_plan):
            role = roles_plan[index]
            row["role"] = role
            row["role_label"] = REVERSE_ROLE_LABELS[role]
            row["amount"] = amounts_plan[index]
        elif not ladder_mode:
            if base is None:
                if index < len(picks):
                    row["amount"] = int(picks[index].get("amount") or AMOUNT_DEF)
                    row["role"] = picks[index].get("role") or ROLE_DEFENSE
                    row["role_label"] = picks[index].get("role_label") or "防守"
                else:
                    row["amount"] = AMOUNT_DEF
                    row["role"] = ROLE_DEFENSE
                    row["role_label"] = "防守"
            row["reverse_tier_hint"] = (
                AMOUNT_TIP
                if number in tip_numbers
                else AMOUNT_MID
                if number in mid_numbers
                else AMOUNT_DEF
            )

        row["is_repeat_number"] = number == int(latest)
        row["is_repeat_zodiac"] = bool(
            number != int(latest) and zodiac_group(number) == zodiac_group(int(latest))
        )
        row["is_comeback_tip"] = False
        row["is_prev_guard"] = number == int(prev_guard_number)
        row["is_cold_relay"] = bool(
            cold_relay_number is not None and number == int(cold_relay_number)
        )
        row["is_same_zodiac_cold_partner"] = bool(
            same_zodiac_cold_partner is not None
            and number == int(same_zodiac_cold_partner)
        )
        new_picks.append(row)

    replaced = sorted(set(engine_numbers) - set(selected))
    applied = ladder_mode or bool(replaced) or selected != engine_numbers
    prev_zodiac_count = sum(
        1 for n in selected if zodiac_group(n) == prev_group
    )
    guard_label = GUARD_KIND_LABELS.get(prev_guard_kind, prev_guard_kind)

    if macro_reverse:
        knife = (
            f"大盘{MACRO_LABELS[macro['zone']]}已连 {macro['streak']} 期 → "
            f"防变盘改押「{prefer_label}」"
        )
    else:
        knife = f"大盘未满三，顺应「{prefer_label}」（连 {macro['streak']} 期）"
    if micro_reverse:
        knife += f"；微观单肖连 {micro_streak} 期，该肖不进主攻"
    if wang_on and wang_group is not None:
        knife += f"；旺肖组={wang_group}（近{WANG_WINDOW}期{wang_hits}次）"
    if cold_relay_number is not None:
        knife += (
            f"；极寒破局(开出前遗漏"
            f"{latest_pre_omit_days if latest_pre_omit_days is not None else '样本首见'}天)"
            f"→替补 {int(cold_relay_number):02d} 进10元；"
            f"原号 {int(prev_guard_number):02d} 仅3元；禁同肖10-40温号"
        )
    else:
        knife += (
            f"；{guard_label} {int(prev_guard_number):02d} 仅 3 元"
            f"（上期肖名额 {prev_zodiac_count}/{PREV_ZODIAC_MAX}，禁10/5）"
        )

    notes = [
        f"逆向博弈叠加已开启：{knife}。"
        f"最新{int(recent_zodiac_limit) or DEFAULT_RECENT_ZODIAC_LIMIT}肖组="
        f"{len(hot_groups)} 个；预字不进主攻。"
        "样本内档位重排，不是庄家必开，也不承诺提高命中率。"
    ]
    if replaced:
        notes.append(
            "微调换出引擎号：" + "、".join(f"{n:02d}" for n in replaced) + "。"
        )
    if ladder_mode:
        notes.append(
            f"档位已按 10×{tip_n} / 5×{mid_n} / 3×{def_n} 重排"
            f"（注数 {target_n}；不强制改为 9 注）。"
        )
    else:
        notes.append(
            "当前非档位模式：只标注逆向标签与建议档，不改写预算拆分金额。"
        )

    summary = {
        "enabled": True,
        "applied": bool(applied),
        "direction": direction,
        "direction_label": DIRECTION_LABELS[direction],
        "zodiac_streak": int(micro_streak),
        "hot_zodiac_group": micro_group,
        "wang_zodiac": bool(wang_on),
        "wang_zodiac_group": wang_group,
        "wang_hits": int(wang_hits),
        "macro_zone": macro["zone"],
        "macro_zone_label": MACRO_LABELS.get(macro["zone"], macro["zone"]),
        "macro_streak": int(macro["streak"]),
        "prefer_zone": prefer_zone,
        "prefer_label": prefer_label,
        "hot_zodiac_groups": sorted(hot_groups),
        "preview_numbers": sorted(band_set),
        "tip_numbers": list(tip_numbers),
        "mid_numbers": list(mid_numbers),
        "defense_numbers": list(defense_numbers),
        "repeat_guard_number": int(prev_guard_number),
        "prev_guard_kind": prev_guard_kind,
        "prev_guard_label": guard_label,
        "cold_break": bool(cold_break_on),
        "latest_pre_omit_days": latest_pre_omit_days,
        "cold_relay_number": cold_relay_number,
        "prev_zodiac_group": int(prev_group),
        "prev_zodiac_max": (
            2 if cold_relay_number is not None else PREV_ZODIAC_MAX
        ),
        "prev_zodiac_count": int(prev_zodiac_count),
        "same_zodiac_cold_partner": same_zodiac_cold_partner,
        "comeback_number": None,
        "replaced_numbers": replaced,
        "tier_counts": {"tip10": tip_n, "mid5": mid_n, "def3": def_n},
        "rule": empty_summary["rule"],
    }
    return {
        "picks": new_picks,
        "summary": summary,
        "notes": notes,
        "applied": bool(applied),
    }
