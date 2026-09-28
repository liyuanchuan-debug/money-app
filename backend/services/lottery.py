"""号码波动推荐算法 —— 纯函数实现，不依赖数据库。

规则来源见 README「业务规则」。所有金额单位为元。
"""

from __future__ import annotations

import hashlib
import random
from collections import Counter
from datetime import date, datetime
from typing import Any, Iterable, Sequence

from services.mark_six import lunar_year_for, zodiac_label, zodiac_of

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

# 默认注数取值范围：1-10 注（单挑模式恒为 1 注，不受此值影响）
PICK_COUNT_MIN = 1
PICK_COUNT_MAX = 10

MODE_EVEN = "even"
MODE_WEIGHTED = "weighted"
MODE_SINGLE = "single"
# 随机分配：把最大投注金额按金额最小单位随机拆给各注（各注金额允许不等）。
MODE_RANDOM = "random"
MODE_LABELS = {
    MODE_EVEN: "均注",
    MODE_WEIGHTED: "侧重",
    MODE_SINGLE: "单挑",
    MODE_RANDOM: "随机分配",
}
MODES = [MODE_EVEN, MODE_WEIGHTED, MODE_SINGLE, MODE_RANDOM]
# 供 Pydantic / 前端复用的枚举正则（唯一取值表就是上面的 MODES）
MODE_PATTERN = "^(" + "|".join(MODES) + ")$"

# 金额最小单位（注码粒度）：**所有模式**下每一注金额必须是它的正整数倍
AMOUNT_UNIT_MIN = 1
AMOUNT_UNIT_MAX = 10000
DEFAULT_AMOUNT_UNIT = 5
# 最大投注金额：**唯一的预算真值**（旧 bet_unit 已降级为派生展示值，不再是输入真值）
TOTAL_AMOUNT_MIN = 1
# 上限对齐旧口径的可能极值（bet_unit 10000 × pick_count 10 = 100000）
TOTAL_AMOUNT_MAX = 100000
DEFAULT_TOTAL_AMOUNT = 50
DEFAULT_PICK_COUNT = 6
# 特码兑付倍数（用户设定；默认 47，不是收益承诺）
DEFAULT_ODDS = 47
ODDS_MIN = 1
ODDS_MAX = 999

# 避冷加权（软偏好；与 trend_bias **互相独立**）。两条规则，均按「距上次出现自然日」：
#   1）选号排后：距上次出现 > 阈值的号码（含样本内从未出现 = None）排到候选队列末尾，
#      非冷号优先被取到；冷号始终留在池内，某桶全是冷号时照常取用（不少出号）；
#   2）金额封顶：权重 = 阈值 / 天数（越久越低），金额最多给「保本金额」= 1 个最小单位，
#      向下取整到最小单位倍数，不足 1 个单位记 0；省下的预算不补给其它注。
# 口径：样本内偏好，不是概率、不是收益承诺，也不承诺提高命中率。
#   - 距上次出现 ≤ 阈值 → 不惩罚（权重 1.0，正好等于阈值也算不惩罚）；
#   - 距上次出现 > 阈值 → 权重 = 阈值 / 天数（越久越低：60 天 1.0、120 天 0.5、240 天 0.25）；
#   - 本池样本内从未出现（days_since_last 为 None）→ 视为最冷，取地板权重 0（金额归 0）。
DEFAULT_AVOID_COLD_ENABLED = True
DEFAULT_AVOID_COLD_DAYS = 60
AVOID_COLD_DAYS_MIN = 1
AVOID_COLD_DAYS_MAX = 999
AVOID_COLD_FLOOR_WEIGHT = 0.0

# 近期走势加权（软偏好）。口径：本池近 W 期经验频率，不是真实概率；
# 回测未证实相对随机有优势 —— notes / UI 禁止写「提高命中率」。
TREND_BIAS_NEUTRAL = "neutral"
TREND_BIAS_HOT = "hot"
TREND_BIAS_COLD = "cold"
TREND_BIAS_MID = "mid"
TREND_BIASES = [
    TREND_BIAS_NEUTRAL,
    TREND_BIAS_HOT,
    TREND_BIAS_COLD,
    TREND_BIAS_MID,
]
TREND_BIAS_LABELS = {
    TREND_BIAS_NEUTRAL: "不加权（旧排序）",
    TREND_BIAS_HOT: "近期频次加权（热号偏好）",
    TREND_BIAS_COLD: "近期频次加权（冷号偏好）",
    TREND_BIAS_MID: "近期频次加权（中频优先）",
}
TREND_BIAS_PATTERN = "^(" + "|".join(TREND_BIASES) + ")$"
# 0 = 用全部样本；默认 30 与首页走势窗一致
DEFAULT_TREND_WINDOW = 30
TREND_WINDOW_MIN = 0
TREND_WINDOW_MAX = 500
# 「是否由用户**手动设置过**走势加权」的内部元数据键（落在 settings 表里）。
# 背景：旧版本默认值是 TREND_BIAS_HOT，存量库里可能残留「不是用户主动选择」的 hot。
# 读取时只有该标记为 True 才按存值生效；否则一律回退 neutral（不加权）。
# 该键是内部标记，不进入对外设置契约（SettingsOut / 前端类型）。
TREND_BIAS_EXPLICIT_KEY = "trend_bias_explicit"

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
    # 预算真值；默认 50 元 / 单位 5 / 6 注 → 10 个单位均分 → 10/10/10/10/5/5
    "total_amount": DEFAULT_TOTAL_AMOUNT,
    "amount_unit": DEFAULT_AMOUNT_UNIT,
    "mode": MODE_EVEN,
    "pick_count": DEFAULT_PICK_COUNT,
    # 特码兑付倍数（用户设定；默认 47）
    "odds": DEFAULT_ODDS,
    # 避开重肖：默认关闭（同肖号可以入选）；旧数据缺失该字段时也回退为 False
    "exclude_repeat_zodiac": False,
    # 走势加权：默认不加权；neutral=关闭，等同旧池内排序
    "trend_bias": TREND_BIAS_NEUTRAL,
    "trend_window": DEFAULT_TREND_WINDOW,
    # 避冷加权：默认开启；距上次出现超过 avoid_cold_days 天的号越久权重越低、
    # 配置金额越低，最多只给「保本金额」（1 个金额最小单位）。旧数据缺失时按默认开启。
    "avoid_cold_enabled": DEFAULT_AVOID_COLD_ENABLED,
    "avoid_cold_days": DEFAULT_AVOID_COLD_DAYS,
    # 显式标记：False = 用户从未手动设置过走势加权（含存量旧默认 hot 遗留行）
    TREND_BIAS_EXPLICIT_KEY: False,
}
# 存量库里的旧字段（已降级为派生值）：只在「缺失 total_amount」时用于回退预算
LEGACY_BET_UNIT_KEY = "bet_unit"


def coerce_bool(value: Any, default: bool = False) -> bool:
    """把落库 / 请求里的真假值规整为 bool；缺省与无法识别时用 default。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off", ""):
            return False
    return default


# --------------------------------------------------------------------------- #
# 基础规则
# --------------------------------------------------------------------------- #
def zodiac_group(number: int) -> int:
    """同肖分组键：n 与 n±12 同肖，1..49 共 12 组。"""
    return (number - 1) % ZODIAC_STEP


def zodiac_numbers(number: int, include_self: bool = True) -> list[int]:
    """返回与 number 同肖的全部号码（01/13/25/37/49 形式）。

    与 ``services.mark_six.zodiac_of()`` 的分组恒等（都是 ``(n - 1) % 12``）：
    农历年只决定「这一组叫什么生肖」，不改变组内成员。所以同肖号列表与
    汉字生肖这两个口径永远指向同一组号码，不会互相矛盾。
    """
    group = zodiac_group(number)
    found = [
        n for n in range(NUMBER_MIN, NUMBER_MAX + 1) if zodiac_group(n) == group
    ]
    if not include_self:
        found = [n for n in found if n != number]
    return found


def resolve_zodiac_date(history_dates: Iterable[Any] | None) -> date | None:
    """推荐口径的「生肖参照日」= 历史序列里最新一期的开奖日（约定最新在前）。

    生肖归属随**农历年**轮转（春节换肖），汉字只能由某一期开奖日推出；
    这里取本池最新一期开奖日，与 ``GET /api/draws`` 给同一期补的 ``zodiac``
    同源（同 ``services.mark_six`` 农历年表）。

    拿不到日期（离线调用、日期缺失）时返回 ``None``，上游据此把生肖字段落成
    ``null`` —— 宁可不给，也不拿「今天」替猜农历年（春节前后会猜错）。
    """
    if history_dates is None:
        return None
    for raw in history_dates:
        try:
            return _as_date(raw)
        except (TypeError, ValueError):
            return None
    return None


def classify_wave(diff: int, small_max: int = 10, normal_max: int = 30) -> str:
    """相邻两期差值绝对值 → 波动类型。"""
    diff = abs(diff)
    if diff <= small_max:
        return WAVE_SMALL
    if diff <= normal_max:
        return WAVE_NORMAL
    return WAVE_BIG


def clamp_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    """合并用户配置与默认值，并做基本校验。

    预算真值口径：``total_amount`` 是唯一输入真值；``bet_unit`` 已降级为派生值，
    不再是可写设置项（即使 raw 里带了也会被忽略，只用于下面这一处旧数据回退）。

    存量数据迁移（不报错、不把预算变成 0）：读到**缺失 total_amount** 的旧行时，
    回退为 ``bet_unit × pick_count``（库里老行是 bet_unit=10、pick_count=3 → 30）。
    """
    raw = dict(raw) if raw else {}
    merged = dict(DEFAULT_SETTINGS)
    for key, value in raw.items():
        if key in merged and value is not None and value != "":
            merged[key] = value

    merged["small_max"] = max(0, int(merged["small_max"]))
    merged["normal_max"] = max(merged["small_max"] + 1, int(merged["normal_max"]))
    merged["pick_count"] = min(
        PICK_COUNT_MAX, max(PICK_COUNT_MIN, int(merged["pick_count"]))
    )
    merged["amount_unit"] = min(
        AMOUNT_UNIT_MAX, max(AMOUNT_UNIT_MIN, int(merged["amount_unit"]))
    )
    try:
        odds_value = float(merged["odds"])
    except (TypeError, ValueError):
        odds_value = float(DEFAULT_ODDS)
    merged["odds"] = min(ODDS_MAX, max(ODDS_MIN, odds_value))

    if raw.get("total_amount") in (None, ""):
        # 旧行没有 total_amount：回退为「旧单注金额 × 注数」，绝不落成 0
        legacy_unit = raw.get(LEGACY_BET_UNIT_KEY)
        if legacy_unit not in (None, ""):
            try:
                merged["total_amount"] = max(1, int(legacy_unit)) * merged["pick_count"]
            except (TypeError, ValueError):
                merged["total_amount"] = DEFAULT_TOTAL_AMOUNT
    merged["total_amount"] = min(
        TOTAL_AMOUNT_MAX, max(TOTAL_AMOUNT_MIN, int(merged["total_amount"]))
    )

    if merged["mode"] not in MODES:
        merged["mode"] = MODE_EVEN
    # 布尔设置：缺失 / 脏值一律回退为默认 False（不避开重肖）
    merged["exclude_repeat_zodiac"] = coerce_bool(
        merged.get("exclude_repeat_zodiac"),
        default=bool(DEFAULT_SETTINGS["exclude_repeat_zodiac"]),
    )

    bias = str(merged.get("trend_bias") or "").strip().lower()
    merged["trend_bias"] = bias if bias in TREND_BIASES else TREND_BIAS_NEUTRAL
    # 「手动设置过」标记：缺失 / 脏值一律回退 False（未手动设置）
    merged[TREND_BIAS_EXPLICIT_KEY] = coerce_bool(
        merged.get(TREND_BIAS_EXPLICIT_KEY), default=False
    )
    try:
        window = int(merged["trend_window"])
    except (TypeError, ValueError):
        window = DEFAULT_TREND_WINDOW
    merged["trend_window"] = min(TREND_WINDOW_MAX, max(TREND_WINDOW_MIN, window))

    # 避冷加权：布尔开关缺失 / 脏值一律回退为默认 True（默认开启）；
    # 阈值钳到 1..999，缺失 / 脏值回退为默认 60 天
    merged["avoid_cold_enabled"] = coerce_bool(
        merged.get("avoid_cold_enabled"),
        default=bool(DEFAULT_SETTINGS["avoid_cold_enabled"]),
    )
    try:
        cold_days = int(merged["avoid_cold_days"])
    except (TypeError, ValueError):
        cold_days = DEFAULT_AVOID_COLD_DAYS
    merged["avoid_cold_days"] = min(
        AVOID_COLD_DAYS_MAX, max(AVOID_COLD_DAYS_MIN, cold_days)
    )
    return merged


def effective_trend_bias(cfg: dict[str, Any] | None) -> str:
    """**读取口径**：没手动设置过走势加权就不加权。

    存量库里存的 ``hot`` 是旧版本的默认值，并不代表用户主动选择；只要缺少
    ``trend_bias_explicit=True``（用户在设置页手动改过才会写），不论存的是
    hot/cold/mid 都按 ``neutral``（不加权，等同旧池内排序）生效。
    取值非法同样回退 ``neutral``。
    """
    raw = cfg if isinstance(cfg, dict) else {}
    bias = str(raw.get("trend_bias") or "").strip().lower()
    if bias not in TREND_BIASES:
        return TREND_BIAS_NEUTRAL
    if not coerce_bool(raw.get(TREND_BIAS_EXPLICIT_KEY), default=False):
        return TREND_BIAS_NEUTRAL
    return bias


def resolve_trend_bias(cfg: dict[str, Any]) -> dict[str, Any]:
    """对配置应用读取口径（只改 ``trend_bias``，其余字段原样返回）。"""
    return {**cfg, "trend_bias": effective_trend_bias(cfg)}


def merge_settings_patch(
    current: dict[str, Any], patch: dict[str, Any]
) -> dict[str, Any]:
    """把可写 patch 叠加到当前配置，并维护「手动设置过走势加权」标记。

    - 只接受 ``DEFAULT_SETTINGS`` 里的键（``big_min`` 等派生字段永远写不进去）；
      避冷加权两项（``avoid_cold_enabled`` / ``avoid_cold_days``）同在
      ``DEFAULT_SETTINGS`` 内，因此由下面的通用循环原样接收（无需特殊分支）；
    - 本次**显式提交** ``trend_bias``（非 None）→ 打上
      ``trend_bias_explicit=True``，此后按用户选择生效；
    - 未提交则沿用当前值：``current`` 已按读取口径解析过，存量遗留的 hot
      在这里自然会落成 neutral（幂等自愈）。
    """
    merged = dict(current)
    for key, value in (patch or {}).items():
        if value is None or key not in DEFAULT_SETTINGS:
            continue
        merged[key] = value
        if key == "trend_bias":
            merged[TREND_BIAS_EXPLICIT_KEY] = True
    return clamp_settings(merged)


def derive_big_min(normal_max: int) -> int:
    """大跳下限由 normal_max 推导（恒为 normal_max + 1），不单独存储。"""
    return normal_max + 1


def effective_pick_count(mode: str, pick_count: int) -> int:
    """有效注数：单挑把整份预算押在 1 注上，其余模式取配置注数。"""
    return 1 if mode == MODE_SINGLE else max(1, int(pick_count))


def derive_bet_unit(
    total_amount: int,
    pick_count: int,
    mode: str,
    amount_unit: int = DEFAULT_AMOUNT_UNIT,
) -> int:
    """派生展示用「对齐后的均注参考」：最大投注换算为单位后按注数均分，再 × unit。

    已 deprecated 为权威输入；分配算法不得再用「裸除法余数补第一注」破坏倍数约束。
    例：total=50 / unit=5 / picks=6 → 10 单位 ÷ 6 = 每注 1 单位 → 5 元。
    """
    unit = max(1, int(amount_unit))
    picks = effective_pick_count(mode, pick_count)
    units_total = max(0, int(total_amount)) // unit
    return (units_total // picks) * unit


def with_derived_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """在配置上附加只读派生字段，仅用于接口返回，不落库。

    避冷加权两项（``avoid_cold_enabled`` / ``avoid_cold_days``）是**可写设置项**，
    由上面的 ``**cfg`` 原样带出（与 ``exclude_repeat_zodiac`` / ``trend_bias`` /
    ``trend_window`` 的既有模式一致），此处不再重复派生；「保本金额」恒等于
    ``amount_unit``（1 个最小注码单位），前端可据此展示，无需落库。
    """
    return {
        **cfg,
        "big_min": derive_big_min(cfg["normal_max"]),
        # 向后兼容：bet_unit 仍回（deprecated），语义见 derive_bet_unit
        "bet_unit": derive_bet_unit(
            cfg["total_amount"],
            cfg["pick_count"],
            cfg["mode"],
            cfg["amount_unit"],
        ),
    }


# --------------------------------------------------------------------------- #
# 候选池
# --------------------------------------------------------------------------- #
def build_candidate_pools(
    latest: int,
    small_max: int,
    normal_max: int,
    *,
    exclude_repeat_zodiac: bool = False,
) -> dict[str, list[dict[str, int]]]:
    """构造三类波动候选池；始终排除最新号本身。

    ``exclude_repeat_zodiac=True`` 时额外排除最新号的全部同肖（重肖）号码；
    默认 False：同肖号可以入选。
    """
    latest_group = zodiac_group(latest)
    pools: dict[str, list[dict[str, int]]] = {wave: [] for wave in WAVE_ORDER}

    for number in range(NUMBER_MIN, NUMBER_MAX + 1):
        if number == latest:
            continue
        if exclude_repeat_zodiac and zodiac_group(number) == latest_group:
            continue
        diff = abs(number - latest)
        wave = classify_wave(diff, small_max, normal_max)
        pools[wave].append({"number": number, "diff": diff})

    return pools


def recent_number_frequency(
    history_numbers: Iterable[int],
    window: int,
) -> tuple[Counter[int], list[dict[str, Any]], int]:
    """近 W 期（0=全部）特码出现次数。``history_numbers`` 约定最新在前。"""
    series = [int(n) for n in history_numbers]
    if window and window > 0:
        series = series[:window]
    used = len(series)
    counts: Counter[int] = Counter(series)
    rows = [
        {
            "number": number,
            "count": counts.get(number, 0),
            "rate": (counts.get(number, 0) / used) if used else 0.0,
        }
        for number in range(NUMBER_MIN, NUMBER_MAX + 1)
    ]
    return counts, rows, used


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def compute_days_since_last(
    history_numbers: Sequence[int],
    history_dates: Sequence[Any] | None = None,
) -> dict[int, int | None]:
    """本池样本内各号距最近一次出现的自然日数（最新一期开奖日为基准）。

    ``history_*`` 约定最新在前。返回 ``1..49 → int | None``：
    - 从未在样本内出现 → ``None``（前端写「样本内未出现」，勿编造）
    - 提供与号码等长的开奖日 → ``(基准日 - 最近出现日).days``
    - 无开奖日时回退为期数差（最近出现所在下标），避免把「窗外出现过」
      误标成从未出现
    """
    numbers = [int(n) for n in history_numbers]
    result: dict[int, int | None] = {
        n: None for n in range(NUMBER_MIN, NUMBER_MAX + 1)
    }
    if not numbers:
        return result

    last_index: dict[int, int] = {}
    for index, number in enumerate(numbers):
        if number not in last_index:
            last_index[number] = index

    dates: list[date] | None = None
    if history_dates is not None:
        parsed = [_as_date(d) for d in history_dates]
        if len(parsed) == len(numbers):
            dates = parsed

    if dates is not None:
        ref = dates[0]
        for number, index in last_index.items():
            if NUMBER_MIN <= number <= NUMBER_MAX:
                result[number] = (ref - dates[index]).days
    else:
        for number, index in last_index.items():
            if NUMBER_MIN <= number <= NUMBER_MAX:
                result[number] = index

    return result


def _band_sizes(n: int) -> tuple[int, int, int]:
    """把 n 个号码尽量均分成主推 / 次选 / 防守三段（余数优先补给主推）。"""
    if n <= 0:
        return 0, 0, 0
    primary = (n + 2) // 3
    secondary = (n + 1) // 3
    defense = n // 3
    return primary, secondary, defense


def role_for_focus_rank(rank: int) -> str:
    """侧重顺位 → 角色：第 1 侧重=主推，第 2=次选，其余=防守。"""
    if rank <= 0:
        return ROLE_PRIMARY
    if rank == 1:
        return ROLE_SECONDARY
    return ROLE_DEFENSE


def split_pool_into_role_bands(
    pool: list[dict[str, int]],
    trend_counts: Counter[int],
    sample_size: int,
    days_since_last: dict[int, int | None] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """同一波动桶内按近期频次切三角色带。

    规则（前后端一致、可解释）：
    1. 桶内按「近窗出现次数降序 → 与最新号差值升序 → 号码升序」排序；
    2. 连续切成三段，尺寸 ``(n+2)//3, (n+1)//3, n//3``；
    3. **主推** = 最热一段，**次选** = 中段，**防守** = 最冷一段。
    次数与占比相对「近窗样本期数」计算，是经验频率不是真实概率。
    近窗 count=0 的号额外带 ``days_since_last``（样本内距上次出现自然日；
    从未出现为 null）。
    """
    ranked = sorted(
        pool,
        key=lambda item: (
            -trend_counts.get(item["number"], 0),
            item["diff"],
            item["number"],
        ),
    )
    p_size, s_size, _ = _band_sizes(len(ranked))
    slices = {
        ROLE_PRIMARY: ranked[:p_size],
        ROLE_SECONDARY: ranked[p_size : p_size + s_size],
        ROLE_DEFENSE: ranked[p_size + s_size :],
    }
    days_map = days_since_last or {}
    bands: dict[str, list[dict[str, Any]]] = {role: [] for role in ROLE_ORDER}
    for role, items in slices.items():
        for item in items:
            number = item["number"]
            count = int(trend_counts.get(number, 0))
            entry: dict[str, Any] = {
                "number": number,
                "diff": item["diff"],
                "count": count,
                "rate": (count / sample_size) if sample_size else 0.0,
            }
            if count == 0:
                # 0 次必须带：历史上出现过则为自然日差，从未出现则为 null
                entry["days_since_last"] = days_map.get(number)
            bands[role].append(entry)
    return bands


def build_trend_distributions(
    pools: dict[str, list[dict[str, int]]],
    trend_counts: Counter[int],
    *,
    window: int,
    used_window: int,
    bias: str,
    days_since_last: dict[int, int | None] | None = None,
) -> dict[str, Any]:
    """结构化「波动 × 角色」走势分布参考（供 UI 分块展示）。"""
    waves: dict[str, Any] = {}
    for wave in WAVE_ORDER:
        bands = split_pool_into_role_bands(
            pools[wave], trend_counts, used_window, days_since_last
        )
        waves[wave] = {
            "type": wave,
            "label": WAVE_LABELS[wave],
            ROLE_PRIMARY: bands[ROLE_PRIMARY],
            ROLE_SECONDARY: bands[ROLE_SECONDARY],
            ROLE_DEFENSE: bands[ROLE_DEFENSE],
        }
    return {
        "window": window,
        "used_window": used_window,
        "bias": bias,
        "bias_label": TREND_BIAS_LABELS.get(bias, bias),
        "rule": (
            "同一波动桶内按近窗出现次数降序切三段："
            "主推=最热段，次选=中段，防守=最冷段；"
            "次数与占比为本池近窗经验频率，不是真实概率。"
            "近窗 0 次号附距上次出现自然日（样本内从未出现则为空）。"
        ),
        "waves": waves,
    }


def _trend_sort_prefix(
    number: int,
    *,
    bias: str,
    trend_counts: Counter[int],
    mid_target: float,
) -> tuple[float, ...]:
    """软加权前缀键；``neutral`` 返回空元组，排序回退为旧遗漏优先。"""
    count = float(trend_counts.get(number, 0))
    if bias == TREND_BIAS_HOT:
        return (-count,)
    if bias == TREND_BIAS_COLD:
        return (count,)
    if bias == TREND_BIAS_MID:
        return (abs(count - mid_target),)
    return ()


def order_pool(
    pool: list[dict[str, int]],
    history_counts: Counter[int],
    *,
    trend_bias: str = TREND_BIAS_NEUTRAL,
    trend_counts: Counter[int] | None = None,
    mid_target: float = 0.0,
    avoid_cold_enabled: bool = False,
    avoid_cold_days: int = DEFAULT_AVOID_COLD_DAYS,
    days_since_last: dict[int, int | None] | None = None,
) -> list[dict[str, int]]:
    """池内排序。

    ``neutral``：先取全历史遗漏最久（出现少），再取差值最小 —— 旧行为。
    非 ``neutral``：先按近窗频次偏好（热/冷/中频），再回落旧键。

    避冷加权（``avoid_cold_enabled``，与 ``trend_bias`` **互相独立**）：
    开启时在排序键最前面加一档「冷号标记」，把 ``days_since_last`` 超过
    ``avoid_cold_days``（含样本内从未出现 = ``None``）的号码整体排到队列**末尾**，
    让非冷号优先被取到。冷号始终留在池内（不删除），因此某桶全是冷号时
    仍能正常取号，不会少出号。关闭时（默认参数）排序与改造前逐元素一致。
    """
    counts = trend_counts or Counter()
    bias = trend_bias if trend_bias in TREND_BIASES else TREND_BIAS_NEUTRAL
    cold_enabled = bool(avoid_cold_enabled)
    cold_limit = _clamp_avoid_cold_days(avoid_cold_days)
    days_map = days_since_last or {}

    def sort_key(item: dict[str, int]) -> tuple:
        number = item["number"]
        cold_prefix: tuple = ()
        if cold_enabled:
            cold_prefix = (
                1
                if is_avoid_cold_number(
                    days_map.get(number), enabled=True, threshold=cold_limit
                )
                else 0,
            )
        prefix = _trend_sort_prefix(
            number,
            bias=bias,
            trend_counts=counts,
            mid_target=mid_target,
        )
        baseline = (
            history_counts.get(number, 0),
            item["diff"],
            item["number"],
        )
        return (*cold_prefix, *prefix, *baseline)

    return sorted(pool, key=sort_key)


def take_from_role_band(
    bands: dict[str, list[dict[str, Any]]],
    preferred_role: str,
    used: set[int],
    *,
    is_cold: Any = None,
) -> dict[str, Any] | None:
    """从偏好角色带取号；空则按 主推→次选→防守 回退。

    ``is_cold(number) -> bool`` 非空时（避冷加权开启）分两轮：
    先在角色优先顺序里挑**非冷号**；只有当所有角色带都只剩冷号时，
    才回退到冷号 —— 保证冷号排后但仍可选（不会少出号、不会误报「无号」）。
    ``is_cold`` 为 ``None``（关闭避冷）时行为与改造前逐元素一致。
    """
    order = [preferred_role] + [r for r in ROLE_ORDER if r != preferred_role]
    if is_cold is not None:
        for role in order:
            for item in bands.get(role, []):
                if item["number"] not in used and not is_cold(item["number"]):
                    return item
    for role in order:
        for item in bands.get(role, []):
            if item["number"] not in used:
                return item
    return None


def assign_roles(pick_count: int) -> list[str]:
    """按顺位分配角色：仅首位为主推，其后「次选 → 防守」交替。

    pick_count > 3 时沿用「主推 → 次选 → 防守 → 次选 → 防守 …」，
    保证任意注数下恰好一个主推，且不出现重复主推。
    """
    roles: list[str] = []
    for index in range(pick_count):
        if index == 0:
            roles.append(ROLE_PRIMARY)
        elif index % 2 == 1:
            roles.append(ROLE_SECONDARY)
        else:
            roles.append(ROLE_DEFENSE)
    return roles


def amount_seed_key(
    period: Any,
    latest: Any,
    previous: Any,
    total: int,
    count: int,
    unit: int,
) -> str:
    """随机分配的确定性种子材料：**同一期 + 同一组参数 → 同一份分配**。

    - 主键是「期」（``period``，来自最新一期开奖）：同期刷新 / 重算结果完全一致；
    - ``latest`` / ``previous`` 作为期号缺失时的兜底（保持确定性，不用时间）；
    - 把 total / count / unit 一起纳入：参数一变，分配也跟着变，
      否则「改了最大投注却拿到旧分配」会让用户以为没生效。
    """
    payload = (
        f"period={period}|latest={latest}|previous={previous}"
        f"|total={total}|count={count}|unit={unit}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _random_source(seed: Any) -> random.Random:
    """构造确定性随机源。

    ``random.Random(str)`` 内部用 sha512 展开种子（CPython 稳定、跨进程 / 跨平台一致），
    绝不使用无种子的全局 ``random``，也不依赖调用时间。
    """
    return random.Random(seed if seed is not None else "")


def random_amounts(
    total: int, count: int, unit: int, seed: Any = None
) -> list[int]:
    """随机分配（纯函数）：把最大投注按最小单位随机拆给各注。

    约束：
    - 每注金额是 ``unit`` 的整数倍；
    - 总和精确等于「向下取整到 unit 倍数」后的最大投注（不出现小数 / 差 1 元）；
    - 每注至少 1 个最小单位（不产生 0 元注）；
    - 最大投注不足以覆盖 ``count`` 注时，只输出**能被覆盖的注数**
      （0 元注等于这注不存在，所以宁可少输出注数，也不输出 0 元）。

    分配方式：先给每注 1 个最小单位，再把剩余单位用「隔板法」随机切成 ``count`` 份
    （每份等概率），因此各注金额会真实地不等，而不是只差 1 个单位。
    """
    unit = max(1, int(unit))
    count = max(1, int(count))
    units_total = max(0, int(total)) // unit
    if units_total <= 0:
        return []

    note_count = min(count, units_total)  # 覆盖不了的注数直接不输出
    extra = units_total - note_count  # 扣掉每注保底的 1 个单位后剩下的单位数
    rng = _random_source(seed)

    # 隔板法：extra 个相同小球放进 note_count 个盒子，等概率取一种组合
    positions = sorted(rng.sample(range(extra + note_count - 1), note_count - 1))
    bounds = [-1, *positions, extra + note_count - 1]
    parts = [bounds[i + 1] - bounds[i] - 1 for i in range(note_count)]
    return [(1 + part) * unit for part in parts]


def random_allocation(
    total: int, count: int, unit: int, seed: Any = None
) -> dict[str, Any]:
    """随机分配 + 如实记录降级说明（notes）。"""
    requested = max(0, int(total))
    unit = max(1, int(unit))
    count = max(1, int(count))
    amounts = random_amounts(requested, count, unit, seed)

    notes: list[str] = []
    floored = requested - (requested % unit)
    if requested > 0 and requested % unit != 0:
        notes.append(
            f"最大投注金额 {requested} 元不是金额最小单位 {unit} 元的整数倍，"
            f"已向下取整为 {floored} 元参与随机分配（差额 {requested - floored} 元未分配）。"
        )
    if len(amounts) < count:
        notes.append(
            f"最大投注金额 {requested} 元不足以按金额最小单位 {unit} 元覆盖 {count} 注"
            f"（至少需要 {count * unit} 元），本次只按可覆盖的 {len(amounts)} 注分配；"
            "请调高最大投注金额或减少注数。"
        )
    if not amounts:
        notes.append(
            f"最大投注金额 {requested} 元不足 1 个金额最小单位（{unit} 元），本次无法分配金额。"
        )

    return {
        "amounts": amounts,
        "allocated_total": sum(amounts),
        "notes": notes,
    }


def _distribute_units_even(units: int, count: int) -> list[int]:
    """把 ``units`` 个筹码尽量均分到 ``count`` 注；余数逐个补给前 rem 注。

    例：10 单位 / 6 注 → ``[2, 2, 2, 2, 1, 1]``。
    """
    count = max(0, int(count))
    units = max(0, int(units))
    if count <= 0 or units <= 0:
        return []
    note_count = min(count, units)  # 每注至少 1 单位，覆盖不了的注数不输出
    base = units // note_count
    rem = units % note_count
    return [base + (1 if i < rem else 0) for i in range(note_count)]


def _distribute_units_weighted(units: int, count: int) -> list[int]:
    """侧重：主推约占 4/6 单位，其余均分；并保证每注 ≥ 1 单位。

    若按 4/6 算完后其余注不够每人 1 单位，则压缩主推份额
    （``primary = units - (count - 1)``），再均分剩余。
    """
    count = max(0, int(count))
    units = max(0, int(units))
    if count <= 0 or units <= 0:
        return []
    note_count = min(count, units)
    if note_count == 1:
        return [units]

    primary = max(1, units * 4 // 6)
    others = note_count - 1
    remaining = units - primary
    if remaining < others:
        primary = units - others
        remaining = others
    if primary < 1:
        return _distribute_units_even(units, note_count)
    return [primary] + _distribute_units_even(remaining, others)


def allocate_amounts(
    mode: str,
    roles: list[str],
    total: int,
    *,
    amount_unit: int = DEFAULT_AMOUNT_UNIT,
    seed: Any = None,
) -> list[int]:
    """按筹码模式把最大投注分配到各号上（统一以 ``amount_unit`` 为注码粒度）。

    流程：``units = total // amount_unit`` → 按模式分单位 → 每注金额 = 单位数 × unit。
    每注金额必为 unit 的正整数倍且 ≥ 1 个单位；最大投注不足覆盖全部注数时少输出注数
    （与 ``random_amounts`` 一致，不产出 0 元注）。
    """
    if not roles:
        return []

    unit = max(1, int(amount_unit))
    count = len(roles)

    if mode == MODE_RANDOM:
        return random_amounts(total, count, unit, seed)

    units_total = max(0, int(total)) // unit
    if units_total <= 0:
        return []

    if mode == MODE_SINGLE:
        # 单挑：全部单位押在第一注；roles 长度在上游已钳为 1
        return [units_total * unit]

    if mode == MODE_WEIGHTED:
        parts = _distribute_units_weighted(units_total, count)
    else:
        parts = _distribute_units_even(units_total, count)
    return [part * unit for part in parts]


def _clamp_avoid_cold_days(threshold: Any) -> int:
    """避冷阈值统一钳到 1..999；非法值回退默认 60。"""
    try:
        limit = int(threshold)
    except (TypeError, ValueError):
        limit = DEFAULT_AVOID_COLD_DAYS
    return min(AVOID_COLD_DAYS_MAX, max(AVOID_COLD_DAYS_MIN, limit))


def is_avoid_cold_number(
    days_since_last: int | None,
    *,
    enabled: bool = True,
    threshold: int = DEFAULT_AVOID_COLD_DAYS,
) -> bool:
    """避冷判定：该号是否算「冷号」（间隔超过阈值，或样本内从未出现）。

    与 ``avoid_cold_weight`` 共用同一档口径：``d <= threshold`` 不算冷号；
    ``days_since_last is None``（本池样本内从未出现）算最冷。
    ``enabled=False`` 时恒为 ``False``（关闭 = 旧行为，严格 no-op）。
    """
    if not enabled:
        return False
    limit = _clamp_avoid_cold_days(threshold)
    if days_since_last is None:
        return True
    try:
        days = int(days_since_last)
    except (TypeError, ValueError):
        return True
    return days > limit


def avoid_cold_weight(
    days_since_last: int | None,
    *,
    enabled: bool = True,
    threshold: int = DEFAULT_AVOID_COLD_DAYS,
) -> float:
    """避冷加权（软偏好 / 只降不升）。

    口径（唯一实现，前后端文案必须与它一致）：

    - ``enabled=False`` → 恒为 ``1.0``，行为与改造前完全一致（关闭即旧行为）；
    - ``days_since_last`` 为**自然日**（见 ``compute_days_since_last``）：
      ``d <= threshold`` → ``1.0``（正好等于阈值不惩罚）；
      ``d > threshold`` → ``threshold / d``（60 天 1.0、120 天 0.5、240 天 0.25）；
    - ``days_since_last is None``（本池样本内从未出现）→ 视为最冷，取地板值
      ``AVOID_COLD_FLOOR_WEIGHT``（0.0 → 金额归 0），不编造天数。

    只影响该注的分配金额（选号排后由 ``order_pool`` / ``take_from_role_band`` 负责，
    见 ``is_avoid_cold_number``）；这是样本内偏好，不是概率，也不承诺收益。
    """
    if not enabled:
        return 1.0
    limit = _clamp_avoid_cold_days(threshold)

    if days_since_last is None:
        # 样本内从未出现：没有天数可算，按最冷处理
        return AVOID_COLD_FLOOR_WEIGHT
    try:
        days = int(days_since_last)
    except (TypeError, ValueError):
        return AVOID_COLD_FLOOR_WEIGHT
    if days <= limit:
        return 1.0
    return limit / days


def _floor_to_unit(value: float, unit: int) -> int:
    """向下取整到 ``unit`` 的整数倍；不足 1 个单位 → 0（该注不再分配金额）。"""
    unit = max(1, int(unit))
    if value is None or value <= 0:
        return 0
    return int(float(value) // unit) * unit


def apply_avoid_cold_amounts(
    amounts: list[int],
    weights: list[float],
    *,
    amount_unit: int = DEFAULT_AMOUNT_UNIT,
) -> tuple[list[int], int]:
    """按避冷加权压低各注金额（只降不升；省下的预算不补给其它注）。

    - ``target = 原金额 × 权重``；
    - ``cap = 保本金额 = 1 × amount_unit``（一个最小注码单位）；
    - 结果 = ``min(target, cap)`` 再向下取整到 ``amount_unit`` 的整数倍；
    - 不足 1 个单位 → ``0``（号码仍在 picks 里，只是不再分配金额）。

    返回 ``(新金额列表, 被压低的注数)``。权重 ``>= 1.0`` 的注金额原样保留，
    因此 ``enabled=False``（全 1.0）时输出与输入逐元素相同。
    """
    unit = max(1, int(amount_unit))
    cap = unit
    out: list[int] = []
    reduced = 0
    for index, base in enumerate(amounts):
        weight = float(weights[index]) if index < len(weights) else 1.0
        original = int(base)
        if weight >= 1.0:
            out.append(original)
            continue
        target = float(original) * weight
        value = _floor_to_unit(min(target, float(cap)), unit)
        if value != original:
            reduced += 1
        out.append(value)
    return out, reduced


def prepare_budget(
    mode: str,
    total: int,
    pick_count: int,
    amount_unit: int,
) -> dict[str, Any]:
    """分配前的预算规范化：向下取整到 unit 倍数，并在最大投注不够时降级注数。

    返回 ``allocated_total`` / ``pick_count`` / ``notes``。random 模式请继续走
    ``random_allocation``（含隔板法与同样的降级说明）。
    """
    requested = max(0, int(total))
    unit = max(1, int(amount_unit))
    count = max(1, int(pick_count))
    units_total = requested // unit
    floored = units_total * unit
    notes: list[str] = []

    if requested > 0 and requested % unit != 0:
        notes.append(
            f"最大投注金额 {requested} 元不是金额最小单位 {unit} 元的整数倍，"
            f"已向下取整为 {floored} 元参与分配（差额 {requested - floored} 元未分配）。"
        )

    if units_total <= 0:
        notes.append(
            f"最大投注金额 {requested} 元不足 1 个金额最小单位（{unit} 元），本次无法分配金额。"
        )
        return {"allocated_total": 0, "pick_count": 0, "notes": notes}

    if mode == MODE_SINGLE:
        return {"allocated_total": floored, "pick_count": 1, "notes": notes}

    if units_total < count:
        notes.append(
            f"最大投注金额 {requested} 元不足以按金额最小单位 {unit} 元覆盖 {count} 注"
            f"（至少需要 {count * unit} 元），本次只按可覆盖的 {units_total} 注分配；"
            "请调高最大投注金额或减少注数。"
        )
        return {
            "allocated_total": floored,
            "pick_count": units_total,
            "notes": notes,
        }

    return {"allocated_total": floored, "pick_count": count, "notes": notes}


def format_number(number: Any) -> str:
    """把号码格式化为定宽两位数字串（1-49 → ``01``…``49``）。

    仅用于展示 / 复制文案：个位数补前导 0（``9`` → ``09``），两位数原样返回
    （``13`` → ``13``，``49`` → ``49``）。金额等其它数字不使用本函数。
    """
    return f"{int(number):02d}"


def build_copy_text(mode: str, picks: list[dict[str, Any]]) -> str:
    """生成一键复制的竞猜投注串（金额文案必须跟各注实际金额一致）。

    格式（与分配模式无关）：
    - 单号独额：``号码：金额元；``
    - 相同金额合并：``号1、号2：各金额元；``
    - 组间用中文分号，分号后换行；末行 ``合计：N元。``

    号码一律经 ``format_number`` 补零为两位（``9`` → ``09``，``13`` 保持 ``13``）；
    ``合计`` 是金额，不做补零。
    """
    del mode  # 格式统一，不再按模式分支
    if not picks:
        return ""

    # 按金额分组：金额首次出现顺序为组序，组内保持出号顺序
    groups: list[tuple[int, list[int]]] = []
    amount_index: dict[int, int] = {}
    for pick in picks:
        amount = int(pick["amount"])
        number = int(pick["number"])
        idx = amount_index.get(amount)
        if idx is None:
            amount_index[amount] = len(groups)
            groups.append((amount, [number]))
        else:
            groups[idx][1].append(number)

    lines: list[str] = []
    for amount, numbers in groups:
        joined = "、".join(format_number(n) for n in numbers)
        if len(numbers) == 1:
            lines.append(f"{joined}：{amount}元；")
        else:
            lines.append(f"{joined}：各{amount}元；")

    total = sum(int(p["amount"]) for p in picks)
    lines.append(f"合计：{total}元。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def recommend(
    latest: int,
    previous: int | None,
    history_numbers: Iterable[int],
    settings: dict[str, Any] | None = None,
    mode: str | None = None,
    amount_seed: Any = None,
    period: Any = None,
    history_dates: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """生成一期推荐。latest/previous 为号码本身，history_numbers 为历史开奖号序列。

    预算口径（单一真值）：``total_amount`` 是唯一预算来源，**所有模式**都从它取预算
    （旧 ``bet_unit`` 已降级为派生展示值）。金额按 ``amount_unit`` 注码粒度分配：
    默认 total=50 / unit=5 / picks=6 均注为 10/10/10/10/5/5，单挑为 50。

    - ``amount_seed``：仅 random 模式的显式重掷种子；不传时由「期」+ 参数确定性派生。
    - ``period``：当前期号（用于派生随机分配种子，保证同期刷新结果稳定）。
    - ``history_dates``：与 ``history_numbers`` 等长、最新在前的开奖日；用于近窗 0 次号的
      ``days_since_last``（自然日差）。缺省时回退为期数差。
      同时用于生肖：参照日取 ``history_dates[0]``（本池最新一期开奖日）→ 农历年 →
      生肖表，给每注补上 ``zodiac`` / ``zodiac_label``（缺失则落 null，不猜年份）。

    走势加权（``trend_bias`` / ``trend_window``）：
    - 先按差值把候选分进小波动 / 常规 / 大跳三桶（阈值来自设置）；
    - 每桶内按近窗出现次数切主推/次选/防守三段（见 ``split_pool_into_role_bands``）；
    - 选号时：该波动在侧重顺序里对应的角色，优先从该桶对应角色带取号；
    - ``neutral`` 关闭加权，池内排序回退为旧的「全历史遗漏优先」。

    避冷加权（``avoid_cold_enabled`` / ``avoid_cold_days``，与 ``trend_bias`` 独立）：
    - 候选池排序把冷号（``days_since_last`` 超过阈值，或样本内从未出现）整体排到队尾；
    - 加权路径的取号也先在角色带里挑非冷号，整桶都冷才回退（冷号不删除、不少出号）；
    - 关闭时（``avoid_cold_enabled=False``）排序与金额均与改造前逐元素一致。
    """
    cfg = clamp_settings(settings)
    mode = mode if mode in MODES else cfg["mode"]
    budget_count = cfg["pick_count"]
    pick_count = effective_pick_count(mode, budget_count)
    total = cfg["total_amount"]
    unit = cfg["amount_unit"]
    allocation_notes: list[str] = []
    seed_key: str | None = None

    if mode == MODE_RANDOM:
        # 种子：由「期」+ 参数派生（见 amount_seed_key）；显式传入的 amount_seed 覆盖它
        seed_key = amount_seed_key(
            period, latest, previous, total, pick_count, unit
        )
        if amount_seed not in (None, ""):
            seed_key = f"{seed_key}|explicit={amount_seed}"
        plan = random_allocation(total, pick_count, unit, seed_key)
        # 降级信息（不可满足 / 非整数倍）如实进入 notes，绝不静默
        allocation_notes.extend(plan["notes"])
        total = plan["allocated_total"]
        pick_count = len(plan["amounts"])
    else:
        # even / weighted / single：同样先按单位规范化预算与可覆盖注数
        plan = prepare_budget(mode, total, pick_count, unit)
        allocation_notes.extend(plan["notes"])
        total = plan["allocated_total"]
        pick_count = plan["pick_count"]

    history = list(history_numbers)
    dates_list = list(history_dates) if history_dates is not None else None
    # 生肖参照日（只增不改）：本池最新一期开奖日 → 农历年 → 生肖表。
    # 与 GET /api/draws 给该期补的 zodiac 同源，且与 latest_zodiac 的号码分组恒等
    # （农历年只决定这一组叫什么生肖）；拿不到日期就整块留 null，绝不用「今天」猜。
    zodiac_date = resolve_zodiac_date(dates_list)
    zodiac_year = lunar_year_for(zodiac_date) if zodiac_date is not None else None
    latest_zodiac_code = (
        zodiac_of(int(latest), zodiac_date) if zodiac_date is not None else None
    )
    # 最新号本身不计入遗漏统计
    history_counts: Counter[int] = Counter(
        n for n in history if n != latest
    )

    trend_bias = cfg["trend_bias"]
    trend_window = cfg["trend_window"]
    trend_counts, number_frequency, used_window = recent_number_frequency(
        history, trend_window
    )
    days_since_last = compute_days_since_last(history, dates_list)
    # 避冷加权（选号排后 + 金额封顶）总开关；与 trend_bias 完全独立，关闭 = 旧行为
    cold_enabled = bool(cfg["avoid_cold_enabled"])
    cold_days = int(cfg["avoid_cold_days"])

    def _is_cold_number(number: int) -> bool:
        """该号是否为冷号（间隔 > 阈值，或样本内从未出现）。"""
        return is_avoid_cold_number(
            days_since_last.get(int(number)), enabled=True, threshold=cold_days
        )

    # 仅在开启时把判定函数交给取号逻辑（关闭时传 None → 严格 no-op）
    cold_predicate: Any = _is_cold_number if cold_enabled else None
    # 中频目标：近窗内「出现过的号码」的平均次数；全空则 0
    mid_target = (
        sum(trend_counts.values()) / max(1, len(set(trend_counts)))
        if trend_counts
        else 0.0
    )

    pools = build_candidate_pools(
        latest,
        cfg["small_max"],
        cfg["normal_max"],
        exclude_repeat_zodiac=bool(cfg["exclude_repeat_zodiac"]),
    )

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

    # 走势分布参考：始终按频次三段切分（展示与生成共用同一套切分）
    trend_distributions = build_trend_distributions(
        pools,
        trend_counts,
        window=trend_window,
        used_window=used_window,
        bias=trend_bias,
        days_since_last=days_since_last,
    )
    wave_bands = {
        wave: split_pool_into_role_bands(
            pools[wave], trend_counts, used_window, days_since_last
        )
        for wave in WAVE_ORDER
    }

    ordered_pools = {
        w: order_pool(
            pools[w],
            history_counts,
            trend_bias=trend_bias,
            trend_counts=trend_counts,
            mid_target=mid_target,
            # 避冷加权：冷号整体排到队列末尾（与 trend_bias 独立；关闭时严格 no-op）
            avoid_cold_enabled=cold_enabled,
            avoid_cold_days=cold_days,
            days_since_last=days_since_last,
        )
        for w in WAVE_ORDER
    }

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
    used_numbers: set[int] = set()

    def _annex_pick(wave: str, item: dict[str, Any]) -> None:
        number = int(item["number"])
        used_numbers.add(number)
        # 从有序池里摘掉，避免后续再取到同一号
        ordered_pools[wave] = [
            row for row in ordered_pools[wave] if row["number"] != number
        ]
        count = int(trend_counts.get(number, 0))
        pick_zodiac = (
            zodiac_of(number, zodiac_date) if zodiac_date is not None else None
        )
        picks.append(
            {
                "number": number,
                "diff": int(item["diff"]),
                "wave_type": wave,
                "trend_count": count,
                "trend_note": f"近{used_window}期出现{count}次",
                # 号码 → 生肖的固定映射（随农历年轮转）；没有农历年时为 null
                "zodiac": pick_zodiac,
                "zodiac_label": zodiac_label(pick_zodiac),
            }
        )

    def _take_for_wave(wave: str) -> bool:
        """从该波动桶取一个号。加权开启时优先走侧重角色对应的频次带。"""
        if not ordered_pools[wave]:
            return False
        if trend_bias == TREND_BIAS_NEUTRAL:
            item = ordered_pools[wave][0]
            _annex_pick(wave, item)
            return True
        preferred = role_for_focus_rank(focus.index(wave))
        band_item = take_from_role_band(
            wave_bands[wave], preferred, used_numbers, is_cold=cold_predicate
        )
        if band_item is None:
            item = ordered_pools[wave][0]
            _annex_pick(wave, item)
            return True
        _annex_pick(wave, band_item)
        return True

    # 决策 1：候选选取固定按 WAVE_ORDER（小波动 → 常规波动 → 大跳）逐类取一个，
    # 与侧重/回补顺序解耦；侧重顺序只用于角色分配，以及加权时的角色带映射。
    for wave in WAVE_ORDER:
        if len(picks) >= pick_count:
            break
        _take_for_wave(wave)

    # 决策 2：某类无解导致不足注数时，从仍有候选的池中补齐（允许同类多取）
    for wave in WAVE_ORDER:
        if len(picks) >= pick_count:
            break
        while len(picks) < pick_count and ordered_pools[wave]:
            # 补齐时按「即将落到的角色顺位」偏好对应频次带
            preferred = assign_roles(pick_count)[len(picks)]
            if trend_bias == TREND_BIAS_NEUTRAL:
                _annex_pick(wave, ordered_pools[wave][0])
            else:
                band_item = take_from_role_band(
                    wave_bands[wave], preferred, used_numbers, is_cold=cold_predicate
                )
                if band_item is None:
                    _annex_pick(wave, ordered_pools[wave][0])
                else:
                    _annex_pick(wave, band_item)

    # 角色按侧重顺序分配 —— 在侧重顺序中出现越靠前的波动类型越优先，
    # 依次获得 主推 / 次选 / 防守…（focus 恒含全部三类波动）。
    picks.sort(key=lambda pick: focus.index(pick["wave_type"]))
    roles = assign_roles(len(picks))
    amounts = allocate_amounts(
        mode,
        roles,
        total,
        amount_unit=cfg["amount_unit"],
        seed=seed_key,
    )

    # 避冷加权：把「距上次出现超过阈值」的注金额压低（选号排后已在 order_pool /
    # take_from_role_band 生效），省下的预算不补给其它注；关闭时权重全为 1.0。
    cold_weights = [
        avoid_cold_weight(
            days_since_last.get(int(pick["number"])),
            enabled=cold_enabled,
            threshold=cold_days,
        )
        for pick in picks
    ]
    budget_total = sum(int(amount) for amount in amounts)
    cold_reduced_count = 0
    if cold_enabled:
        amounts, cold_reduced_count = apply_avoid_cold_amounts(
            amounts, cold_weights, amount_unit=cfg["amount_unit"]
        )
    staked_total = sum(int(amount) for amount in amounts)

    for index, (pick, role, amount) in enumerate(zip(picks, roles, amounts)):
        cold_weight = cold_weights[index] if index < len(cold_weights) else 1.0
        pick["role"] = role
        pick["role_label"] = ROLE_LABELS[role]
        pick["amount"] = amount
        pick["wave_label"] = WAVE_LABELS[pick["wave_type"]]
        pick["is_repeat_zodiac"] = zodiac_group(pick["number"]) == zodiac_group(latest)
        # 避冷加权附加字段（只增不改）：距上次出现自然日 / 是否被压低 / 权重
        pick["days_since_last"] = days_since_last.get(int(pick["number"]))
        pick["avoid_cold_penalized"] = bool(cold_enabled and cold_weight < 1.0)
        pick["avoid_cold_weight"] = round(float(cold_weight), 4)

    notes: list[str] = [*allocation_notes]
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

    if trend_bias == TREND_BIAS_NEUTRAL:
        notes.append(
            "走势加权已关闭：池内仍按本池全历史遗漏优先排序（旧行为）；"
            "下方「走势分布参考」仅供对照，未参与选号。"
        )
    else:
        window_text = (
            f"近{used_window}期"
            if trend_window and trend_window > 0
            else f"本池全部{used_window}期"
        )
        notes.append(
            f"已按本池样本内{window_text}特码出现频次做"
            f"「{TREND_BIAS_LABELS[trend_bias]}」："
            "各波动桶内主推/次选/防守对应频次三段（热→中→冷）；"
            "这是样本内加权偏好，不是真实概率，也不承诺提高命中率。"
        )

    # 避冷加权：如实说明「选号排后 + 金额压顶」两条规则与口径，不做任何收益承诺
    if cold_enabled:
        notes.append(
            f"避冷加权已开启（阈值 {cold_days} 天）：距上次出现超过 {cold_days} 天的号码"
            "（含本池样本内从未出现的号码）已排到候选队列末尾，非冷号优先被取到；"
            "若某类波动桶内只剩冷号，仍照常取用，不会因此少出号或报「无号」。"
        )
        if cold_reduced_count:
            notes.append(
                f"避冷加权金额封顶：{cold_reduced_count} 注距上次出现超过 {cold_days} 天，"
                f"权重随天数递减（{cold_days} 天=1.0、{cold_days * 2} 天≈0.5、"
                f"{cold_days * 4} 天≈0.25），金额最多给到保本金额 "
                f"{cfg['amount_unit']} 元（1 个金额最小单位）；"
                "不足 1 个最小单位的注金额记 0（号码仍列出），"
                f"省下的 {budget_total - staked_total} 元不补给其它注。"
            )
        else:
            notes.append(
                f"避冷加权已开启（阈值 {cold_days} 天），本次没有距上次出现超过阈值的注，"
                "金额未受影响。"
            )
        notes.append(
            "避冷加权只把「距上次出现较久」的号码排到后面并压低其投注金额，"
            "是本池样本内的偏好，不是概率计算，也不承诺提高命中率或收益。"
        )
    else:
        notes.append(
            "避冷加权已关闭：距上次出现多久都不影响选号与金额（旧行为）。"
        )

    # 派生展示值（deprecated）：均分后再向下对齐到 amount_unit
    effective_notes = max(1, len(picks))
    return {
        "latest": latest,
        "latest_zodiac": zodiac_numbers(latest),
        # 生肖汉字（只增不改）：与 latest_zodiac 指向同一组号码（农历年只决定组名），
        # 供卡片/徽章展示；农历年未知（参照日缺失或早于已知表）时一律 null。
        "latest_zodiac_code": latest_zodiac_code,
        "latest_zodiac_label": zodiac_label(latest_zodiac_code),
        # 生肖口径溯源：参照日（本池最新一期开奖日）与其农历年；未知时为 null
        "zodiac_date": zodiac_date.isoformat() if zodiac_date is not None else None,
        "zodiac_year": zodiac_year,
        "previous": previous,
        "prev_wave": prev_wave,
        "settings": with_derived_settings(cfg),
        "mode": mode,
        "mode_label": MODE_LABELS[mode],
        # 向后兼容：bet_unit 仍回，语义见 derive_bet_unit
        "bet_unit": derive_bet_unit(
            total, effective_notes, MODE_EVEN, cfg["amount_unit"]
        ),
        "total_amount": total,
        "amount_unit": cfg["amount_unit"],
        # 实际分配到各注的合计（避冷加权压低后可能 < total_amount；只增不改旧字段）
        "staked_total": staked_total,
        # 避冷加权本次的生效摘要（只增不改；无被压低注时 penalized_picks = 0）
        "avoid_cold": {
            "enabled": cold_enabled,
            "days": cold_days,
            # 保本金额 = 1 个金额最小单位
            "cap_amount": cfg["amount_unit"],
            "penalized_picks": cold_reduced_count,
            "budget_total": budget_total,
            "reduced_total": budget_total - staked_total,
        },
        "focus_order": [{"type": w, "label": WAVE_LABELS[w]} for w in focus],
        "picks": picks,
        "missing_waves": missing_waves,
        "notes": notes,
        "copy_text": build_copy_text(mode, picks),
        "trend_window": trend_window,
        "trend_bias": trend_bias,
        "trend_bias_label": TREND_BIAS_LABELS[trend_bias],
        "number_frequency": number_frequency,
        "trend_distributions": trend_distributions,
    }
