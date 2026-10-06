"""号码波动推荐算法 —— 纯函数实现，不依赖数据库。

规则来源见 README「业务规则」。所有金额单位为元。
"""

from __future__ import annotations

import hashlib
import math
import random
from collections import Counter
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

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
# 用户口径：按 5 元一档（最低 5 元），因此下限就是步长；写入时向下取到步长的整数倍。
AMOUNT_UNIT_MIN = 5
AMOUNT_UNIT_MAX = 10000
# 注码粒度步长：金额最小单位必须是它的整数倍
AMOUNT_UNIT_STEP = 5
DEFAULT_AMOUNT_UNIT = 5
# 每注最低金额（元）：所有模式下每一注的**实际金额**都不得低于它。
# 预算不足以让每注都达到该下限时，按「能覆盖几注就出几注」降级，绝不产出低于下限的注。
MIN_BET_AMOUNT = 5
# 最大投注金额：**唯一的预算真值**（旧 bet_unit 已降级为派生展示值，不再是输入真值）
# 用户口径：资金最大 100 元、最小 5 元（每注最低 5 元、按 5 元一档）
TOTAL_AMOUNT_MIN = 5
TOTAL_AMOUNT_MAX = 100
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
DEFAULT_AVOID_COLD_ENABLED = False
DEFAULT_AVOID_COLD_DAYS = 60
AVOID_COLD_DAYS_MIN = 1
AVOID_COLD_DAYS_MAX = 999
AVOID_COLD_FLOOR_WEIGHT = 0.0

# --------------------------------------------------------------------------- #
# 三类「软降权」（排序靠后 + 金额打折；**不排除、不归零**）
#
# 与避冷加权（avoid_cold_*）的区别：避冷按**自然日**且把冷号排到队尾 + 金额归 0；
# 本节按**期数**，只降权不排除，且金额保留「每注最低金额」下限。
# 三类可叠加（同一号同时命中多类时权重相乘）。
#
# 口径：样本内偏好，不是概率、不是收益承诺，也不承诺提高命中率。
# --------------------------------------------------------------------------- #
# 重号：与上期特码完全相同的号码（上期出过的号）→ 不排除，降权
DEFAULT_REPEAT_NUMBER_WEIGHT = 0.5
# 同肖：与上期特码同肖（不含重号本身）→ 不排除，降权
DEFAULT_REPEAT_ZODIAC_WEIGHT = 0.8
# 冷号（按**期数**）：本池样本内连续未出现的期数超过该阈值 → 一律降权
DEFAULT_STALE_PERIODS = 60
STALE_PERIODS_MIN = 1
STALE_PERIODS_MAX = 999
DEFAULT_STALE_WEIGHT = 0.3
# 权重取值范围（1.0 = 不降权）
SOFT_WEIGHT_MIN = 0.0
SOFT_WEIGHT_MAX = 1.0

# --------------------------------------------------------------------------- #
# 角色金额配额（主推 / 次选 / 防守）
#
# 「均注」不再严格均分：先给每注保底 1 个注码单位（= 每注最低金额），剩余预算按
# **角色**权重分给 主推 / 次选 / 防守 三个分组，组内再均分。
# 默认 3 : 2 : 1 —— 主推最多、防守最低（用户口径：防守的配额低一些）。
# 三项都填相同的值（如 1:1:1）即回到旧的「严格均分」行为。
# 口径：样本内偏好，不是概率、不是收益承诺，也不承诺提高命中率。
# --------------------------------------------------------------------------- #
DEFAULT_ROLE_WEIGHT_PRIMARY = 3.0
DEFAULT_ROLE_WEIGHT_SECONDARY = 2.0
DEFAULT_ROLE_WEIGHT_DEFENSE = 1.0
ROLE_WEIGHT_MIN = 0.0
ROLE_WEIGHT_MAX = 10.0
# 角色权重缺失 / 脏值时用的兜底（1.0 = 该角色不额外加权，不影响其它角色）
ROLE_WEIGHT_FALLBACK = 1.0
# 角色 → 设置项键名（落库键，英文小写下划线；见 rules/database-enums-english）
ROLE_WEIGHT_SETTING_KEYS = {
    ROLE_PRIMARY: "role_w_primary",
    ROLE_SECONDARY: "role_w_secondary",
    ROLE_DEFENSE: "role_w_defense",
}

# --------------------------------------------------------------------------- #
# 预测波动线 + 号码点阵
#
# 用最近 W 期相邻差值的分布估一条「预测波动线」：中心取中位数、带宽取四分位区间
# [P25, P75]。号码点阵 = 1..49 每个号相对最新特码的差值落在这条线（带）内的程度：
# 带内 = 1.0（满权重），带外按离带边缘的距离衰减。
#
# 开启后点阵权重进入选号排序的**最前档**，因此出号优先落在预测波动带内；
# 带内号码不足以凑满注数时才自然向带外扩散（绝不报「无号」）。
# 口径：样本内经验分布，不是真实概率。
# --------------------------------------------------------------------------- #
DEFAULT_LATTICE_ENABLED = True
DEFAULT_LATTICE_WINDOW = 30
LATTICE_WINDOW_MIN = 0
LATTICE_WINDOW_MAX = 500
# 带宽分位（下/上）：0.25 / 0.75 → 覆盖近窗约一半的差值样本
LATTICE_BAND_LOW_Q = 0.25
LATTICE_BAND_HIGH_Q = 0.75
# 带外衰减尺度：离带边缘 d 个号位 → 权重 1 / (1 + d / LATTICE_DECAY_SCALE)
LATTICE_DECAY_SCALE = 6.0

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
# 角色带方向的白话说明（notes / 推送用）；必须与 _band_rank_prefix 的实际排序一致
_BAND_ORDER_NOTE = {
    TREND_BIAS_HOT: "主推=最热段、次选=中段、防守=最冷段",
    TREND_BIAS_COLD: "主推=最冷段、次选=中段、防守=最热段",
    TREND_BIAS_MID: "主推=最接近中频段、次选=次接近段、防守=离中频最远段",
}
TREND_BIAS_PATTERN = "^(" + "|".join(TREND_BIASES) + ")$"
# 0 = 用全部样本；默认 20（本池 6 注对照常用近窗，设置页首档）
DEFAULT_TREND_WINDOW = 20
TREND_WINDOW_MIN = 0
TREND_WINDOW_MAX = 500
# 「是否由用户**手动设置过**走势加权」的内部元数据键（落在 settings 表里）。
# 背景：旧版本默认值是 TREND_BIAS_HOT，存量库里可能残留「不是用户主动选择」的 hot。
# 读取时只有该标记为 True 才按存值生效；否则一律回退 neutral（不加权）。
# 该键是内部标记，不进入对外设置契约（SettingsOut / 前端类型）。
TREND_BIAS_EXPLICIT_KEY = "trend_bias_explicit"

# 选号策略：wave_round = 旧「每波动桶轮流取号」；score_top = 全候选打分取 Top-N。
# score_top 只改「进前 N 注的号码集合」（影响命中/Δ）；金额分配仍走原模式。
# 口径：样本内对照用，禁止写成「已提高命中率」。
PICK_STRATEGY_WAVE_ROUND = "wave_round"
PICK_STRATEGY_SCORE_TOP = "score_top"
PICK_STRATEGIES = [PICK_STRATEGY_WAVE_ROUND, PICK_STRATEGY_SCORE_TOP]
PICK_STRATEGY_LABELS = {
    PICK_STRATEGY_WAVE_ROUND: "波动轮取（旧）",
    PICK_STRATEGY_SCORE_TOP: "打分 Top-N（对照Δ）",
}
PICK_STRATEGY_PATTERN = "^(" + "|".join(PICK_STRATEGIES) + ")$"
DEFAULT_PICK_STRATEGY = PICK_STRATEGY_WAVE_ROUND
# 打分权重（只影响 score_top）：正号 = 该项越大越优先被扣分后排后（见 score_candidate）。
# 默认接近「中频 + 侧重波段 + 近号差值」；walk-forward 调参可覆盖落库值。
DEFAULT_SCORE_W_FOCUS = 1.0
DEFAULT_SCORE_W_MID = 2.0
DEFAULT_SCORE_W_OMIT = 0.0
DEFAULT_SCORE_W_DIFF = 0.5
SCORE_WEIGHT_MIN = -5.0
SCORE_WEIGHT_MAX = 5.0

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
    # 避冷加权（按**自然日**，会把冷号排到队尾 + 金额归 0）：默认关闭 ——
    # 冷号口径已由上方「stale_periods / stale_weight」按**期数**软降权取代，
    # 两者同时开启会对冷号双重惩罚（本项更硬）。需要旧行为时可在设置页单独打开。
    "avoid_cold_enabled": DEFAULT_AVOID_COLD_ENABLED,
    "avoid_cold_days": DEFAULT_AVOID_COLD_DAYS,
    # 选号策略与打分权重（score_top 才读权重；wave_round 忽略）
    "pick_strategy": DEFAULT_PICK_STRATEGY,
    "score_w_focus": DEFAULT_SCORE_W_FOCUS,
    "score_w_mid": DEFAULT_SCORE_W_MID,
    "score_w_omit": DEFAULT_SCORE_W_OMIT,
    "score_w_diff": DEFAULT_SCORE_W_DIFF,
    # 显式标记：False = 用户从未手动设置过走势加权（含存量旧默认 hot 遗留行）
    TREND_BIAS_EXPLICIT_KEY: False,
    # ---- 三类软降权（不排除、只降权；口径见上方常量注释）----
    # 上期出过的号（重号）是否保留在候选池内：True = 不避开、只降权（用户口径）
    "include_repeat_number": True,
    # 重号（上期特码本身）：0.5 = 金额/排序权重打五折
    "repeat_number_weight": DEFAULT_REPEAT_NUMBER_WEIGHT,
    # 同肖（与上期同肖、非重号）
    "repeat_zodiac_weight": DEFAULT_REPEAT_ZODIAC_WEIGHT,
    # 冷号按**期数**：连续未出现超过该期数 → 降权
    "stale_periods": DEFAULT_STALE_PERIODS,
    "stale_weight": DEFAULT_STALE_WEIGHT,
    # ---- 预测波动线 + 号码点阵（参与选号）----
    "lattice_enabled": DEFAULT_LATTICE_ENABLED,
    "lattice_window": DEFAULT_LATTICE_WINDOW,
    # ---- 角色金额配额（均注模式；主推 : 次选 : 防守，默认 3:2:1）----
    "role_w_primary": DEFAULT_ROLE_WEIGHT_PRIMARY,
    "role_w_secondary": DEFAULT_ROLE_WEIGHT_SECONDARY,
    "role_w_defense": DEFAULT_ROLE_WEIGHT_DEFENSE,
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
    # 注码粒度按步长（默认 5 元）向下取整：用户口径「按 5 的倍数来」
    merged["amount_unit"] -= merged["amount_unit"] % AMOUNT_UNIT_STEP
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

    strategy = str(merged.get("pick_strategy") or "").strip().lower()
    merged["pick_strategy"] = (
        strategy if strategy in PICK_STRATEGIES else DEFAULT_PICK_STRATEGY
    )
    for weight_key, default in (
        ("score_w_focus", DEFAULT_SCORE_W_FOCUS),
        ("score_w_mid", DEFAULT_SCORE_W_MID),
        ("score_w_omit", DEFAULT_SCORE_W_OMIT),
        ("score_w_diff", DEFAULT_SCORE_W_DIFF),
    ):
        try:
            weight = float(merged[weight_key])
        except (TypeError, ValueError):
            weight = float(default)
        merged[weight_key] = min(SCORE_WEIGHT_MAX, max(SCORE_WEIGHT_MIN, weight))

    # 三类软降权：权重钳到 0..1（1.0 = 不降权），阈值按期数钳到 1..999
    merged["include_repeat_number"] = coerce_bool(
        merged.get("include_repeat_number"),
        default=bool(DEFAULT_SETTINGS["include_repeat_number"]),
    )
    for weight_key, default in (
        ("repeat_number_weight", DEFAULT_REPEAT_NUMBER_WEIGHT),
        ("repeat_zodiac_weight", DEFAULT_REPEAT_ZODIAC_WEIGHT),
        ("stale_weight", DEFAULT_STALE_WEIGHT),
    ):
        try:
            soft = float(merged[weight_key])
        except (TypeError, ValueError):
            soft = float(default)
        merged[weight_key] = min(SOFT_WEIGHT_MAX, max(SOFT_WEIGHT_MIN, soft))

    try:
        stale_periods = int(merged["stale_periods"])
    except (TypeError, ValueError):
        stale_periods = DEFAULT_STALE_PERIODS
    merged["stale_periods"] = min(
        STALE_PERIODS_MAX, max(STALE_PERIODS_MIN, stale_periods)
    )

    # 预测波动线 + 点阵：布尔开关与窗口
    merged["lattice_enabled"] = coerce_bool(
        merged.get("lattice_enabled"),
        default=bool(DEFAULT_SETTINGS["lattice_enabled"]),
    )
    try:
        lattice_window = int(merged["lattice_window"])
    except (TypeError, ValueError):
        lattice_window = DEFAULT_LATTICE_WINDOW
    merged["lattice_window"] = min(
        LATTICE_WINDOW_MAX, max(LATTICE_WINDOW_MIN, lattice_window)
    )

    # 角色金额配额（主推 / 次选 / 防守）：钳到 0..10，缺失 / 脏值回退为默认
    for role_key, default in (
        (ROLE_PRIMARY, DEFAULT_ROLE_WEIGHT_PRIMARY),
        (ROLE_SECONDARY, DEFAULT_ROLE_WEIGHT_SECONDARY),
        (ROLE_DEFENSE, DEFAULT_ROLE_WEIGHT_DEFENSE),
    ):
        setting_key = ROLE_WEIGHT_SETTING_KEYS[role_key]
        try:
            role_weight = float(merged[setting_key])
        except (TypeError, ValueError):
            role_weight = float(default)
        merged[setting_key] = min(ROLE_WEIGHT_MAX, max(ROLE_WEIGHT_MIN, role_weight))
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
    include_repeat_number: bool = False,
) -> dict[str, list[dict[str, int]]]:
    """构造三类波动候选池。

    - ``include_repeat_number=False``（默认 / 旧行为）：排除最新号本身；
      ``True``：**保留重号**（上期特码），由软降权把它排到同类之后并压低金额 ——
      即「上期出过的号不避开，但降权」。
    - ``exclude_repeat_zodiac=True``：排除最新号的全部同肖（重肖）号码；
      默认 ``False``：同肖号可以入选（只标记、不排除）。
    """
    latest_group = zodiac_group(latest)
    pools: dict[str, list[dict[str, int]]] = {wave: [] for wave in WAVE_ORDER}

    for number in range(NUMBER_MIN, NUMBER_MAX + 1):
        if number == latest and not include_repeat_number:
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


def compute_periods_since_last(
    history_numbers: Sequence[int],
) -> dict[int, int | None]:
    """本池样本内各号距最近一次出现的**期数**（最新一期 = 0 期以前）。

    ``history_numbers`` 约定最新在前。返回 ``1..49 → int | None``：
    - 本池样本内从未出现 → ``None``
    - 最近出现在下标 ``i`` → ``i``（即「i 期前出现过」）

    与 ``compute_days_since_last`` 的分工：后者按**自然日**（有日期时），
    本函数恒按**期数**，供「连续 N 期未出现」这类按期数判定的规则使用。
    """
    numbers = [int(n) for n in history_numbers]
    result: dict[int, int | None] = {
        n: None for n in range(NUMBER_MIN, NUMBER_MAX + 1)
    }
    for index, number in enumerate(numbers):
        if NUMBER_MIN <= number <= NUMBER_MAX and result[number] is None:
            result[number] = index
    return result


def soft_penalty_weight(
    number: int,
    *,
    latest: int,
    periods_since_last: int | None,
    sample_size: int,
    repeat_number_weight: float = DEFAULT_REPEAT_NUMBER_WEIGHT,
    repeat_zodiac_weight: float = DEFAULT_REPEAT_ZODIAC_WEIGHT,
    stale_periods: int = DEFAULT_STALE_PERIODS,
    stale_weight: float = DEFAULT_STALE_WEIGHT,
) -> tuple[float, list[str]]:
    """三类**软降权**的相乘权重与命中原因（不排除、不归零）。

    - 重号（与上期特码相同）→ ``repeat_number_weight``（默认 0.5）
    - 同肖（与上期同肖，**不含重号本身**）→ ``repeat_zodiac_weight``（默认 0.8）
    - 冷号（**最近 ``stale_periods`` 期内没出现过**）→ ``stale_weight``（默认 0.3）

    重号与同肖互斥（重号必然同肖，按重号计）。权重范围 0..1，1.0 = 不降权。
    返回 ``(权重, 原因列表)``，原因取值为 ``repeat_number`` / ``repeat_zodiac`` / ``stale``。

    **冷号判定的样本门槛**：只有 ``sample_size >= stale_periods`` 时才可能判为冷号。
    样本不足 60 期时无法证明「60 期未出现」，一律**不降权** —— 否则样本越短
    越多号会被误判成从未出现，规则就失去了意义。
    """
    reasons: list[str] = []
    weight = 1.0
    if int(number) == int(latest):
        weight *= float(repeat_number_weight)
        reasons.append("repeat_number")
    elif zodiac_group(int(number)) == zodiac_group(int(latest)):
        weight *= float(repeat_zodiac_weight)
        reasons.append("repeat_zodiac")
    stale = int(sample_size) >= int(stale_periods) and (
        periods_since_last is None or int(periods_since_last) >= int(stale_periods)
    )
    if stale:
        weight *= float(stale_weight)
        reasons.append("stale")
    return weight, reasons


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """线性插值分位数（``sorted_values`` 必须已升序、非空）。"""
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


def predict_wave_band(
    history_numbers: Iterable[int],
    *,
    window: int = DEFAULT_LATTICE_WINDOW,
    small_max: int = 10,
    normal_max: int = 30,
) -> dict[str, Any] | None:
    """用最近 W 期相邻差值估一条「预测波动线」。

    取最近 ``window`` 期（0 = 本池全部）相邻两期的差值绝对值，按分位数给出：
    - ``center`` = 中位数（预测波动中心）
    - ``low`` / ``high`` = P25 / P75（预测波动带，覆盖近窗约一半差值样本）

    样本不足两期时返回 ``None``（无预测，调用方须按「无预测」处理，不得编造）。
    """
    series = [int(n) for n in history_numbers]
    limit = int(window or 0)
    if limit > 0:
        series = series[:limit]
    if len(series) < 2:
        return None
    diffs = sorted(abs(series[i] - series[i - 1]) for i in range(1, len(series)))
    if not diffs:
        return None
    center = _quantile(diffs, 0.5)
    low = _quantile(diffs, LATTICE_BAND_LOW_Q)
    high = _quantile(diffs, LATTICE_BAND_HIGH_Q)
    center_num = int(round(center))
    wave = classify_wave(center_num, small_max, normal_max)
    return {
        "window": limit,
        "used_window": len(series) - 1,
        "samples": len(diffs),
        "center": round(center, 2),
        "center_number": center_num,
        "low": round(low, 2),
        "high": round(high, 2),
        "wave_type": wave,
        "wave_label": WAVE_LABELS[wave],
        "small_max": int(small_max),
        "normal_max": int(normal_max),
        "rule": (
            f"预测波动线取最近 {len(series) - 1} 对相邻差值的分布："
            "中心=中位数、带宽=P25~P75；这是样本内经验分布，不是真实概率。"
        ),
    }


def lattice_weight(diff: int, band: dict[str, Any] | None) -> float:
    """号码点阵权重：差值落在预测波动带内 = 1.0，带外按距离衰减。

    无预测（``band is None``）时恒为 1.0，保证「关掉点阵」与旧行为一致。
    """
    if not band:
        return 1.0
    low = float(band.get("low", 0.0))
    high = float(band.get("high", 0.0))
    value = float(diff)
    if low <= value <= high:
        return 1.0
    distance = (low - value) if value < low else (value - high)
    return 1.0 / (1.0 + max(0.0, distance) / LATTICE_DECAY_SCALE)


def lattice_primary_wave(
    band: dict[str, Any] | None,
    *,
    small_max: int = 10,
    normal_max: int = 30,
) -> str | None:
    """预测波动带**主要落在哪一类波动桶**（带内差值最多的那类）。

    取号时先从该桶（``ordered_pools`` 已按点阵权重排序）取满，带内优先才成立；
    带为空 / 无预测时返回 ``None``（调用方回退旧的「小→常→大」轮取顺序）。
    """
    if not band:
        return None
    low = int(math.floor(float(band.get("low", 0.0))))
    high = int(math.ceil(float(band.get("high", 0.0))))
    counts: Counter[str] = Counter()
    for diff in range(max(0, low), high + 1):
        counts[classify_wave(diff, small_max, normal_max)] += 1
    if not counts:
        return None
    # 并列时按 WAVE_ORDER 取靠前者（小波动 → 常规 → 大跳），保证可复现
    return max(WAVE_ORDER, key=lambda wave: (counts.get(wave, 0), -WAVE_ORDER.index(wave)))


def build_number_lattice(
    latest: int,
    band: dict[str, Any] | None,
    *,
    small_max: int = 10,
    normal_max: int = 30,
) -> list[dict[str, Any]]:
    """1..49 号码点阵：每个号的差值所属波动、点阵权重、是否落在预测波动带内。"""
    rows: list[dict[str, Any]] = []
    for number in range(NUMBER_MIN, NUMBER_MAX + 1):
        diff = abs(number - int(latest))
        wave = classify_wave(diff, small_max, normal_max)
        in_band = bool(band and float(band["low"]) <= diff <= float(band["high"]))
        rows.append(
            {
                "number": number,
                "diff": diff,
                "wave_type": wave,
                "wave_label": WAVE_LABELS[wave],
                "lattice_weight": round(lattice_weight(diff, band), 4),
                "in_band": in_band,
                "is_latest": number == int(latest),
            }
        )
    return rows


def apply_soft_weights(
    amounts: list[int],
    weights: list[float],
    *,
    amount_unit: int = DEFAULT_AMOUNT_UNIT,
    min_bet_amount: int = MIN_BET_AMOUNT,
) -> tuple[list[int], int]:
    """按软降权权重压低各注金额（只降不升；省下的预算**不补给**其它注）。

    - ``target = 原金额 × 权重``，向下取整到 ``amount_unit`` 的整数倍；
    - 结果不低于「每注最低金额」= ``max(min_bet_amount, amount_unit)``
      （金额必须是单位倍数，故单位大于最低金额时以单位为准）；
    - **只降不升**：被上游压到 0 的注（如避冷封顶）不会被抬回下限；
    - 权重 ``>= 1.0`` 的注原样保留，因此三类权重全为 1.0 时严格 no-op。

    返回 ``(新金额列表, 被压低的注数)``。
    """
    unit = max(1, int(amount_unit))
    floor_amount = max(int(min_bet_amount), unit)
    out: list[int] = []
    reduced = 0
    for index, base in enumerate(amounts):
        original = int(base)
        weight = float(weights[index]) if index < len(weights) else 1.0
        if weight >= 1.0 or original <= 0:
            out.append(original)
            continue
        value = _floor_to_unit(float(original) * weight, unit)
        value = min(original, max(floor_amount, value))
        if value != original:
            reduced += 1
        out.append(value)
    return out, reduced


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


def _band_rank_prefix(
    number: int,
    *,
    bias: str,
    trend_counts: Counter[int],
    mid_target: float,
) -> tuple[float, ...]:
    """角色带排序前缀；与 ``order_pool`` 的 ``_trend_sort_prefix`` 同键。

    这样「主推带」= 该偏好下 ``order_pool`` 排在最前的号，两处口径不会打架。
    ``neutral`` 没有偏好方向（``_trend_sort_prefix`` 返回空元组），回退为旧的
    「近窗最热在前」展示口径 —— 此时角色带只供「走势分布参考」对照，不参与选号。
    """
    prefix = _trend_sort_prefix(
        number, bias=bias, trend_counts=trend_counts, mid_target=mid_target
    )
    if prefix:
        return prefix
    return (-float(trend_counts.get(number, 0)),)


def split_pool_into_role_bands(
    pool: list[dict[str, int]],
    trend_counts: Counter[int],
    sample_size: int,
    days_since_last: dict[int, int | None] | None = None,
    *,
    bias: str = TREND_BIAS_NEUTRAL,
    mid_target: float = 0.0,
    lattice_scores: dict[int, float] | None = None,
    penalties: dict[int, float] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """同一波动桶内按**该偏好**的近窗频次切三角色带。

    规则（前后端一致、可解释）：
    1. 桶内先按「号码点阵 → 软降权 → 偏好排序键」排序
       （点阵与降权传入 ``None`` 时退化为只看偏好键，与改造前逐元素一致），
       再按「与最新号差值升序 → 号码升序」回落；
    2. 连续切成三段，尺寸 ``(n+2)//3, (n+1)//3, n//3``；
    3. **主推** = 该偏好最靠前的一段，**次选** = 中段，**防守** = 最靠后的一段。

    ``bias`` 决定「最靠前」的含义，三种方向互不相同：
    - ``hot``：近窗次数**降序** → 主推=最热段，防守=最冷段；
    - ``cold``：近窗次数**升序** → 主推=最冷段，防守=最热段；
    - ``mid``：按 ``|次数 − 近窗均值|`` **升序** → 主推=最接近中频段，
      防守=离中频最远段（两端极值）；
    - ``neutral``：不加权，保持旧的「最热在前」展示口径（仅供对照）。

    次数与占比相对「近窗样本期数」计算，是经验频率不是真实概率。
    近窗 count=0 的号额外带 ``days_since_last``（样本内距上次出现自然日；
    从未出现为 null）。
    """
    lattice_map = lattice_scores or {}
    penalty_map = penalties or {}

    def rank_key(item: dict[str, int]) -> tuple:
        number = item["number"]
        lead: tuple = ()
        if lattice_scores is not None:
            lead += (-float(lattice_map.get(number, 1.0)),)
        if penalties is not None:
            lead += (-float(penalty_map.get(number, 1.0)),)
        return (*lead, *_band_rank_prefix(
            number,
            bias=bias,
            trend_counts=trend_counts,
            mid_target=mid_target,
        ))

    ranked = sorted(
        pool,
        key=lambda item: (
            *rank_key(item),
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


# 角色带口径说明：必须与 ``_band_rank_prefix`` 的实际排序一致，否则 UI 会骗人
_BAND_RULE_HEAD = {
    TREND_BIAS_HOT: (
        "同一波动桶内按近窗出现次数降序切三段："
        "主推=最热段，次选=中段，防守=最冷段；"
    ),
    TREND_BIAS_COLD: (
        "同一波动桶内按近窗出现次数升序切三段："
        "主推=最冷段，次选=中段，防守=最热段；"
    ),
    TREND_BIAS_MID: (
        "同一波动桶内按「近窗次数 − 近窗均值」绝对值升序切三段："
        "主推=最接近中频段，次选=次接近段，防守=离中频最远段；"
    ),
}
# neutral / 未知：不加权时角色带仅供对照，沿用旧展示口径
_BAND_RULE_HEAD_DEFAULT = (
    "同一波动桶内按近窗出现次数降序切三段："
    "主推=最热段，次选=中段，防守=最冷段；"
)


def build_trend_distributions(
    pools: dict[str, list[dict[str, int]]],
    trend_counts: Counter[int],
    *,
    window: int,
    used_window: int,
    bias: str,
    days_since_last: dict[int, int | None] | None = None,
    mid_target: float = 0.0,
    lattice_scores: dict[int, float] | None = None,
    penalties: dict[int, float] | None = None,
) -> dict[str, Any]:
    """结构化「波动 × 角色」走势分布参考（供 UI 分块展示）。

    角色带按 ``bias`` 切分，与选号用的 ``wave_bands`` 同源，展示与选号一致。
    """
    waves: dict[str, Any] = {}
    for wave in WAVE_ORDER:
        bands = split_pool_into_role_bands(
            pools[wave],
            trend_counts,
            used_window,
            days_since_last,
            bias=bias,
            mid_target=mid_target,
            lattice_scores=lattice_scores,
            penalties=penalties,
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
            _BAND_RULE_HEAD.get(bias, _BAND_RULE_HEAD_DEFAULT)
            + "次数与占比为本池近窗经验频率，不是真实概率。"
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
    lattice_scores: dict[int, float] | None = None,
    penalties: dict[int, float] | None = None,
) -> list[dict[str, int]]:
    """池内排序。

    排序键由前到后（越靠前越优先被取到）：

    1. **号码点阵**（``lattice_scores``）：落在预测波动带内的号（权重 1.0）排最前，
       带外按距离衰减 —— 开启后「出号优先落在预测波动线上」。
    2. **软降权**（``penalties``）：重号 / 同肖 / 冷号的权重越小越靠后
       （不排除，只是排到同类之后）。
    3. **避冷加权**（``avoid_cold_enabled``）：把 ``days_since_last`` 超过
       ``avoid_cold_days``（含从未出现 = ``None``）的号整体排到队列末尾。
    4. 走势偏好（``trend_bias``）：``neutral`` 时为全历史遗漏优先（旧行为）。
    5. 回落键：``(出现次数, 差值, 号码)``。

    点阵与降权传入 ``None`` 时不参与排序（严格保持旧行为）。
    """
    counts = trend_counts or Counter()
    bias = trend_bias if trend_bias in TREND_BIASES else TREND_BIAS_NEUTRAL
    cold_enabled = bool(avoid_cold_enabled)
    cold_limit = _clamp_avoid_cold_days(avoid_cold_days)
    days_map = days_since_last or {}
    lattice_map = lattice_scores or {}
    penalty_map = penalties or {}

    def sort_key(item: dict[str, int]) -> tuple:
        number = item["number"]
        lattice_prefix: tuple = ()
        if lattice_scores is not None:
            lattice_prefix = (-float(lattice_map.get(number, 1.0)),)
        penalty_prefix: tuple = ()
        if penalties is not None:
            penalty_prefix = (-float(penalty_map.get(number, 1.0)),)
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
        return (*lattice_prefix, *penalty_prefix, *cold_prefix, *prefix, *baseline)

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


def role_amount_weights(cfg: dict[str, Any] | None) -> dict[str, float]:
    """从配置取「角色金额配额」：主推 / 次选 / 防守（缺失 / 脏值回退默认 3:2:1）。"""
    raw = cfg if isinstance(cfg, dict) else {}
    weights: dict[str, float] = {}
    for role, setting_key in ROLE_WEIGHT_SETTING_KEYS.items():
        default = float(DEFAULT_SETTINGS[setting_key])
        try:
            value = float(raw.get(setting_key, default))
        except (TypeError, ValueError):
            value = default
        weights[role] = min(ROLE_WEIGHT_MAX, max(ROLE_WEIGHT_MIN, value))
    return weights


def role_weights_are_uniform(weights: Mapping[str, float] | None) -> bool:
    """三个角色权重是否完全相同；相同 = 退化回旧的「严格均分」。"""
    if not weights:
        return True
    values = [float(weights.get(role, ROLE_WEIGHT_FALLBACK)) for role in ROLE_ORDER]
    return all(value == values[0] for value in values)


def _format_weight(value: float) -> str:
    """角色权重展示：整数不显示小数点（3.0 → ``3``，2.5 → ``2.5``）。"""
    return f"{float(value):g}"


def distribute_units_by_role(
    units: int, roles: Sequence[str], weights: Mapping[str, float] | None
) -> list[int]:
    """按角色配额把 ``units`` 个筹码分到各注（主推 / 次选 / 防守）。

    流程：
      1. 每注先保底 1 个单位（= 每注最低金额），预算覆盖不了的尾注不输出；
      2. 余量按**角色**权重（不是按注数）分给三个角色分组 —— 默认 3:2:1，
         因此防守组整体分到的余量最少（用户口径：防守的配额低一些）；
      3. 组内再按注数均分。

    权重全相同（或 ``None``）时直接走 ``_distribute_units_even``：保证
    「1:1:1」与旧版均注**逐注完全一致**，不会因为分组顺序改变结果。

    注意：预算刚好等于「每注最低金额 × 注数」时余量为 0，此时角色配额不产生差异。
    """
    roles = list(roles)
    count = len(roles)
    units = max(0, int(units))
    if count <= 0 or units <= 0:
        return []
    note_count = min(count, units)
    if weights is None or role_weights_are_uniform(weights):
        return _distribute_units_even(units, note_count)

    note_units = [1] * note_count
    extra = units - note_count  # 保底之后可自由分配的余量
    if extra <= 0:
        return note_units

    # 用到的角色（按首次出现顺序）及其成员下标
    used_roles: list[str] = []
    members: dict[str, list[int]] = {}
    for index, role in enumerate(roles[:note_count]):
        if role not in members:
            members[role] = []
            used_roles.append(role)
        members[role].append(index)

    group_weights: list[float] = []
    for role in used_roles:
        try:
            value = float(weights.get(role, ROLE_WEIGHT_FALLBACK))
        except (TypeError, ValueError):
            value = ROLE_WEIGHT_FALLBACK
        group_weights.append(max(ROLE_WEIGHT_MIN, value))

    total_weight = sum(group_weights)
    if total_weight <= 0:
        shares = _distribute_units_even(extra, len(used_roles))
    else:
        quotas = [extra * weight / total_weight for weight in group_weights]
        shares = [int(quota) for quota in quotas]  # 向下取整
        leftover = extra - sum(shares)
        # 最大余数法：余量补给小数部分最大的组，平手按角色出现顺序
        order = sorted(
            range(len(used_roles)),
            key=lambda i: (-(quotas[i] - shares[i]), i),
        )
        for i in order[:leftover]:
            shares[i] += 1

    for role, share in zip(used_roles, shares):
        group = members[role]
        for index, part in zip(
            group, _distribute_units_even(len(group) + share, len(group))
        ):
            note_units[index] = part
    return note_units


def allocate_amounts(
    mode: str,
    roles: list[str],
    total: int,
    *,
    amount_unit: int = DEFAULT_AMOUNT_UNIT,
    seed: Any = None,
    role_weights: Mapping[str, float] | None = None,
) -> list[int]:
    """按筹码模式把最大投注分配到各号上（统一以 ``amount_unit`` 为注码粒度）。

    流程：``units = total // amount_unit`` → 按模式分单位 → 每注金额 = 单位数 × unit。
    每注金额必为 unit 的正整数倍且 ≥ 1 个单位；最大投注不足覆盖全部注数时少输出注数
    （与 ``random_amounts`` 一致，不产出 0 元注）。

    ``even`` 模式按 ``role_weights`` 做角色配额（主推 > 次选 > 防守；默认 3:2:1），
    权重全相同即回到严格均分；``weighted`` 仍是旧的「主推约占 4/6」。
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
        parts = distribute_units_by_role(units_total, roles, role_weights)
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


def clamp_score_weights(raw: dict[str, Any] | None = None) -> dict[str, float]:
    """把打分权重钳到 ``SCORE_WEIGHT_MIN..MAX``；缺省用默认值。"""
    src = raw if isinstance(raw, dict) else {}
    out: dict[str, float] = {}
    for key, default in (
        ("score_w_focus", DEFAULT_SCORE_W_FOCUS),
        ("score_w_mid", DEFAULT_SCORE_W_MID),
        ("score_w_omit", DEFAULT_SCORE_W_OMIT),
        ("score_w_diff", DEFAULT_SCORE_W_DIFF),
    ):
        try:
            weight = float(src.get(key, default))
        except (TypeError, ValueError):
            weight = float(default)
        out[key] = min(SCORE_WEIGHT_MAX, max(SCORE_WEIGHT_MIN, weight))
    return out


def score_candidate(
    *,
    wave: str,
    diff: int,
    number: int,
    focus: Sequence[str],
    trend_counts: Counter[int],
    mid_target: float,
    days_since_last: dict[int, int | None] | None,
    weights: dict[str, float],
) -> float:
    """候选打分：分数越高越优先进入 Top-N（仅 ``score_top`` 路径使用）。

    特征（越大越好，权重为正时强化该方向）：
    - focus：侧重顺序越靠前加分越多；
    - mid：越接近近窗中频加分越多；
    - omit：遗漏天数越少加分越多（权重为负则偏好更冷号）；
    - diff：与最新号差值越小加分越多。

    这是样本内排序键，不是概率，也不承诺提高命中率。
    """
    try:
        focus_rank = list(focus).index(wave)
    except ValueError:
        focus_rank = len(WAVE_ORDER)
    count = float(trend_counts.get(int(number), 0))
    mid_dist = abs(count - float(mid_target))
    days_map = days_since_last or {}
    raw_days = days_map.get(int(number))
    if raw_days is None:
        omit_feat = 4.0  # 从未出现：按「约 4 倍默认阈值」的冷度
    else:
        omit_feat = min(max(0, int(raw_days)), 365) / float(DEFAULT_AVOID_COLD_DAYS)

    w = clamp_score_weights(weights)
    # 统一写成「越高越好」
    return (
        -w["score_w_focus"] * float(focus_rank)
        - w["score_w_mid"] * mid_dist
        - w["score_w_omit"] * omit_feat
        - w["score_w_diff"] * (float(diff) / float(NUMBER_MAX))
    )


def select_score_top_candidates(
    pools: dict[str, list[dict[str, int]]],
    *,
    pick_count: int,
    focus: Sequence[str],
    trend_counts: Counter[int],
    mid_target: float,
    days_since_last: dict[int, int | None] | None,
    weights: dict[str, float],
    lattice_scores: dict[int, float] | None = None,
    penalties: dict[int, float] | None = None,
) -> list[dict[str, Any]]:
    """从三类波动池打分，取分数最高的 ``pick_count`` 个（稳定排序）。

    ``lattice_scores`` / ``penalties`` 非空时按**乘数**作用于分数
    （点阵带内 ×1.0、软降权重号 ×0.5 …），与 ``wave_round`` 路径的
    「点阵优先 → 降权靠后」口径一致；为 ``None`` 时与改造前逐元素一致。
    """
    scored: list[tuple[float, int, int, str, dict[str, int]]] = []
    for wave in WAVE_ORDER:
        for item in pools.get(wave) or []:
            number = int(item["number"])
            diff = int(item["diff"])
            points = score_candidate(
                wave=wave,
                diff=diff,
                number=number,
                focus=focus,
                trend_counts=trend_counts,
                mid_target=mid_target,
                days_since_last=days_since_last,
                weights=weights,
            )
            if lattice_scores is not None:
                points *= float(lattice_scores.get(number, 1.0))
            if penalties is not None:
                points *= float(penalties.get(number, 1.0))
            # 分数降序；平手按 diff、number 升序，保证可复现
            scored.append((points, -diff, -number, wave, item))
    scored.sort(reverse=True)
    selected: list[dict[str, Any]] = []
    used: set[int] = set()
    for points, _neg_diff, _neg_num, wave, item in scored:
        number = int(item["number"])
        if number in used:
            continue
        used.add(number)
        selected.append(
            {
                "number": number,
                "diff": int(item["diff"]),
                "wave_type": wave,
                "score": round(float(points), 6),
            }
        )
        if len(selected) >= max(1, int(pick_count)):
            break
    return selected


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

    **``history_numbers`` 必须「最新在前」**（``[最新一期, ..., 最早一期]``）：
    内部多处按此约定取值 —— ``predict_wave_band`` 取 ``series[:window]``、
    ``compute_periods_since_last`` / ``compute_days_since_last`` 取列表中**首次**
    出现、``window_frequency`` 取 ``series[:limit]``、``resolve_zodiac_date`` 取
    ``history_dates[0]``。传成时间正序会把「最近一次出现」算成「最早一次出现」。
    ``latest`` 通常等于 ``history_numbers[0]``（「假设下一期」预览时则是假设号）。

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
    - 每桶内按**该偏好**的近窗频次排序键切主推/次选/防守三段
      （见 ``split_pool_into_role_bands``）：``hot`` → 主推=最热段，
      ``cold`` → 主推=最冷段，``mid`` → 主推=最接近中频段；
    - 选号时：该波动在侧重顺序里对应的角色，优先从该桶对应角色带取号；
    - ``neutral`` 关闭加权，池内排序回退为旧的「全历史遗漏优先」，
      且**不读** ``trend_window``（窗口只影响展示，不影响选号）。

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
    # 三类软降权（重号 / 同肖 / 按期数冷号）：不排除，只降权（排序靠后 + 金额打折）
    periods_since_last = compute_periods_since_last(history)
    repeat_number_weight = float(cfg["repeat_number_weight"])
    repeat_zodiac_weight = float(cfg["repeat_zodiac_weight"])
    stale_periods = int(cfg["stale_periods"])
    stale_weight = float(cfg["stale_weight"])

    def soft_weight(number: int) -> tuple[float, list[str]]:
        return soft_penalty_weight(
            int(number),
            latest=int(latest),
            periods_since_last=periods_since_last.get(int(number)),
            sample_size=len(history),
            repeat_number_weight=repeat_number_weight,
            repeat_zodiac_weight=repeat_zodiac_weight,
            stale_periods=stale_periods,
            stale_weight=stale_weight,
        )

    soft_weights: dict[int, float] = {}
    soft_reasons: dict[int, list[str]] = {}
    for number in range(NUMBER_MIN, NUMBER_MAX + 1):
        value, why = soft_weight(number)
        soft_weights[number] = round(value, 6)
        soft_reasons[number] = why

    # 预测波动线 + 号码点阵（开启后参与选号：带内优先）
    lattice_enabled = bool(cfg["lattice_enabled"])
    lattice_band = (
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
        lattice_band,
        small_max=cfg["small_max"],
        normal_max=cfg["normal_max"],
    )
    lattice_scores: dict[int, float] | None = (
        {row["number"]: float(row["lattice_weight"]) for row in lattice_rows}
        if lattice_enabled
        else None
    )
    # 「预测波动桶」：点阵开启时先在它里面取满（带内优先），不足才向其余桶扩散
    lattice_primary = (
        lattice_primary_wave(
            lattice_band,
            small_max=cfg["small_max"],
            normal_max=cfg["normal_max"],
        )
        if lattice_enabled
        else None
    )
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
        # 上期出过的号不避开（由软降权排后 + 压金额），所以候选池保留重号
        include_repeat_number=bool(cfg["include_repeat_number"]),
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

    # 走势分布参考：按**同一偏好**切频次三段（展示与选号共用同一套切分，避免口径打架）
    trend_distributions = build_trend_distributions(
        pools,
        trend_counts,
        window=trend_window,
        used_window=used_window,
        bias=trend_bias,
        days_since_last=days_since_last,
        mid_target=mid_target,
        lattice_scores=lattice_scores,
        penalties=soft_weights,
    )
    wave_bands = {
        wave: split_pool_into_role_bands(
            pools[wave],
            trend_counts,
            used_window,
            days_since_last,
            # 角色带按 trend_bias 定向：hot→最热段主推，cold→最冷段主推，
            # mid→最接近中频段主推；neutral 不消费角色带（走 ordered_pools）
            bias=trend_bias,
            mid_target=mid_target,
            # 点阵与软降权同键前缀：角色带与池内排序口径一致
            lattice_scores=lattice_scores,
            penalties=soft_weights,
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
            # 点阵（带内优先）在最前，软降权（重号/同肖/冷号）紧随其后
            lattice_scores=lattice_scores,
            penalties=soft_weights,
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
    pick_strategy = str(cfg.get("pick_strategy") or DEFAULT_PICK_STRATEGY)
    if pick_strategy not in PICK_STRATEGIES:
        pick_strategy = DEFAULT_PICK_STRATEGY
    score_weights = clamp_score_weights(cfg)

    def _annex_pick(wave: str, item: dict[str, Any], *, score: float | None = None) -> None:
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
        row = {
            "number": number,
            "diff": int(item["diff"]),
            "wave_type": wave,
            "trend_count": count,
            "trend_note": f"近{used_window}期出现{count}次",
            # 号码 → 生肖的固定映射（随农历年轮转）；没有农历年时为 null
            "zodiac": pick_zodiac,
            "zodiac_label": zodiac_label(pick_zodiac),
        }
        if score is not None:
            row["score"] = float(score)
        picks.append(row)

    if pick_strategy == PICK_STRATEGY_SCORE_TOP:
        # 打分 Top-N：跨波动桶统一取分最高的 N 个（只改集合，不改金额规则）
        for selected in select_score_top_candidates(
            pools,
            pick_count=pick_count,
            focus=focus,
            trend_counts=trend_counts,
            mid_target=mid_target,
            days_since_last=days_since_last,
            weights=score_weights,
            lattice_scores=lattice_scores,
            penalties=soft_weights,
        ):
            wave = str(selected["wave_type"])
            _annex_pick(
                wave,
                {"number": selected["number"], "diff": selected["diff"]},
                score=float(selected.get("score") or 0.0),
            )
    else:

        def _take_one(wave: str, preferred_role: str | None = None) -> bool:
            """从该波动桶取一个号（返回是否取到）。

            ``trend_bias`` 关闭时直接取池内第一个（池已按「点阵 → 软降权 → 遗漏」排好）；
            开启时优先走指定角色对应的频次带，带内无可取号才回退池首。
            """
            if not ordered_pools[wave]:
                return False
            if trend_bias == TREND_BIAS_NEUTRAL:
                _annex_pick(wave, ordered_pools[wave][0])
                return True
            role = (
                preferred_role
                if preferred_role
                else role_for_focus_rank(focus.index(wave))
            )
            band_item = take_from_role_band(
                wave_bands[wave], role, used_numbers, is_cold=cold_predicate
            )
            if band_item is None:
                _annex_pick(wave, ordered_pools[wave][0])
                return True
            _annex_pick(wave, band_item)
            return True

        # 「预测波动桶」：点阵开启时先在该桶内取满（带内优先），不足才向其余桶扩散；
        # 关闭点阵 / 无预测时为 None → 完全走旧的「小→常→大」轮取（旧行为）。
        wave_pass_order = (
            [lattice_primary]
            + [wave for wave in WAVE_ORDER if wave != lattice_primary]
            if lattice_primary
            else list(WAVE_ORDER)
        )

        if lattice_primary is not None:
            # 点阵路径：先在预测波动桶取满，再按 wave_pass_order 向其余波动桶扩散。
            # 桶内已按点阵权重降序，因此带内号码先被取到。
            for wave in wave_pass_order:
                while len(picks) < pick_count:
                    role = assign_roles(pick_count)[len(picks)]
                    if not _take_one(wave, role):
                        break
        else:
            # 决策 1：候选选取固定按 WAVE_ORDER（小波动 → 常规波动 → 大跳）逐类取一个，
            # 与侧重/回补顺序解耦；侧重顺序只用于角色分配，以及加权时的角色带映射。
            for wave in WAVE_ORDER:
                if len(picks) >= pick_count:
                    break
                _take_one(wave)

            # 决策 2：某类无解导致不足注数时，从仍有候选的池中补齐（允许同类多取）
            for wave in WAVE_ORDER:
                if len(picks) >= pick_count:
                    break
                while len(picks) < pick_count and ordered_pools[wave]:
                    # 补齐时按「即将落到的角色顺位」偏好对应频次带
                    _take_one(wave, assign_roles(pick_count)[len(picks)])

    # 角色按侧重顺序分配 —— 在侧重顺序中出现越靠前的波动类型越优先，
    # 依次获得 主推 / 次选 / 防守…（focus 恒含全部三类波动）。
    picks.sort(key=lambda pick: focus.index(pick["wave_type"]))
    roles = assign_roles(len(picks))
    role_weights = role_amount_weights(cfg)
    amounts = allocate_amounts(
        mode,
        roles,
        total,
        amount_unit=cfg["amount_unit"],
        seed=seed_key,
        role_weights=role_weights,
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
    # 三类软降权：金额按权重打折（重号 0.5 / 同肖 0.8 / 冷号 0.3），
    # 下限为「每注最低金额」5 元；省下的预算**不补给**其它注。
    pick_soft_weights = [
        float(soft_weights.get(int(pick["number"]), 1.0)) for pick in picks
    ]
    cold_staked_total = sum(int(amount) for amount in amounts)
    # 软降权前的金额快照（用于逐注判断「金额是否真的被压低」，见 amount_reduced）
    budget_before_soft = list(amounts)
    amounts, soft_reduced_count = apply_soft_weights(
        amounts,
        pick_soft_weights,
        amount_unit=cfg["amount_unit"],
        min_bet_amount=MIN_BET_AMOUNT,
    )
    staked_total = sum(int(amount) for amount in amounts)

    for index, (pick, role, amount) in enumerate(zip(picks, roles, amounts)):
        cold_weight = cold_weights[index] if index < len(cold_weights) else 1.0
        number = int(pick["number"])
        reasons = soft_reasons.get(number, [])
        soft_weight_value = pick_soft_weights[index] if index < len(pick_soft_weights) else 1.0
        periods = periods_since_last.get(number)
        pick["role"] = role
        pick["role_label"] = ROLE_LABELS[role]
        pick["amount"] = amount
        pick["wave_label"] = WAVE_LABELS[pick["wave_type"]]
        # 三类软降权标记（用户要求：不排除，只在推荐结果里明确标出来）
        pick["is_repeat_number"] = number == int(latest)
        pick["is_repeat_zodiac"] = bool(
            number != int(latest) and zodiac_group(number) == zodiac_group(latest)
        )
        pick["is_stale"] = "stale" in reasons
        pick["periods_since_last"] = periods
        pick["soft_weight"] = round(float(soft_weight_value), 4)
        pick["soft_reasons"] = list(reasons)
        # soft_penalized = 命中了哪条降权规则（与金额是否真的降下来无关）；
        # amount_reduced = 这一注的金额**确实**被压低了（受每注最低金额下限保护时会是 False）
        pick["soft_penalized"] = bool(soft_weight_value < 1.0)
        pick["amount_reduced"] = bool(
            amount < (budget_before_soft[index] if index < len(budget_before_soft) else amount)
        )
        # 号码点阵：该注落在预测波动带内的程度（开启点阵时才有意义）
        pick["lattice_weight"] = round(
            float(lattice_scores.get(number, 1.0)) if lattice_scores else 1.0, 4
        )
        pick["in_lattice_band"] = bool(
            lattice_enabled
            and lattice_band
            and float(lattice_band["low"]) <= int(pick["diff"]) <= float(lattice_band["high"])
        )
        # 避冷加权附加字段（只增不改）：距上次出现自然日 / 是否被压低 / 权重
        pick["days_since_last"] = days_since_last.get(number)
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
            f"各波动桶内按该偏好切主推/次选/防守三段"
            f"（{_BAND_ORDER_NOTE.get(trend_bias, _BAND_ORDER_NOTE[TREND_BIAS_HOT])}）；"
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

    # 三类软降权：如实说明口径（不排除、只降权），不做任何收益承诺
    notes.append(
        f"重号/同肖/冷号软降权已生效：上期特码 {latest} 本身（重号）金额与排序权重 ×"
        f"{repeat_number_weight}；与上期同肖但不等于上期特码的号（同肖）×"
        f"{repeat_zodiac_weight}；本池样本内连续 {stale_periods} 期以上未出现的号（冷号）×"
        f"{stale_weight}。三类都不排除，只是排到同类之后并压低金额；"
        f"若被选中会在号码上分别标注「重号」「同肖」「冷号」。"
    )
    if soft_reduced_count:
        notes.append(
            f"软降权金额打折：{soft_reduced_count} 注按权重压低了金额，"
            f"下限为每注 {MIN_BET_AMOUNT} 元的金额下限；"
            f"省下的 {budget_total - staked_total} 元不补给其它注。"
        )

    # 角色金额配额（均注模式）：说明主推/次选/防守的分配口径
    if mode == MODE_EVEN and not role_weights_are_uniform(role_weights):
        role_text = "、".join(
            f"{ROLE_LABELS[role]} {_format_weight(role_weights[role])}"
            for role in ROLE_ORDER
        )
        notes.append(
            f"金额已按角色配额分配（{role_text}）：先给每注保底 {MIN_BET_AMOUNT} 元"
            f"（1 个注码单位），剩余预算按角色分给主推/次选/防守三组，组内再均分 —— "
            "因此防守组拿到的余量最少。这是样本内偏好，不是概率，也不承诺提高命中率。"
        )
        if sum(int(amount) for amount in amounts) <= MIN_BET_AMOUNT * len(amounts):
            notes.append(
                f"注意：本次预算 {total} 元刚好等于「每注最低 {MIN_BET_AMOUNT} 元 × "
                f"{len(amounts)} 注」，没有余量可分配，角色配额未产生任何金额差异；"
                "想拉开主推/防守的差距，请调高最大投注金额或减少注数。"
            )
    elif mode == MODE_EVEN:
        notes.append(
            "角色配额为 1:1:1（主推=次选=防守），金额按注数严格均分（旧行为）。"
        )

    # 预测波动线 + 号码点阵：说明口径（样本内经验分布，不是概率）
    if lattice_enabled and lattice_band:
        notes.append(
            f"预测波动线已开启（最近 {lattice_band['used_window']} 对相邻差值）："
            f"中心 {lattice_band['center']}（{lattice_band['wave_label']}）、"
            f"带宽 {lattice_band['low']}~{lattice_band['high']}；"
            "号码点阵按「与最新特码的差值落在这条线内的程度」铺权重，"
            "选号先在预测波动桶"
            f"（{WAVE_LABELS[lattice_primary] if lattice_primary else '无'}）内取满，"
            "带内不够时才向带外扩散（不会因此少出号）。"
            "这是样本内经验分布，不是真实概率，也不承诺提高命中率。"
        )
        notes.append(
            "排序优先级：号码点阵（带内优先）> 三类软降权（重号/同肖/冷号）。"
            "也就是说带外的新号排在带内的冷号之后 —— 若预测波动带内冷号偏多，"
            "本期就会较多压在冷号上，金额按冷号权重打折。"
        )
    elif lattice_enabled:
        notes.append(
            "数据不足：样本不足两对相邻差值，本期无预测波动线，号码点阵不参与选号。"
        )
    else:
        notes.append("预测波动线与号码点阵已关闭：选号不受波动带影响（旧行为）。")

    if pick_strategy == PICK_STRATEGY_SCORE_TOP:
        notes.append(
            "选号策略为打分 Top-N：在候选池内按侧重波段、中频接近度、遗漏与差值综合打分后取前 N 注；"
            "这是样本内排序对照（用于相对随机命中差），不是真实概率，也不承诺提高命中率。"
        )
    else:
        notes.append(
            "选号策略为波动轮取：点阵开启时先在预测波动桶内取满再向其余桶扩散；"
            "点阵关闭时为旧的「小/常/大跳各取一注，不足再按波动桶补齐」。"
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
        "pick_strategy": pick_strategy,
        "pick_strategy_label": PICK_STRATEGY_LABELS.get(
            pick_strategy, pick_strategy
        ),
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
            "reduced_total": budget_total - cold_staked_total,
        },
        # 三类软降权本次的生效摘要（不排除、只降权）
        "soft_weights": {
            "repeat_number_weight": repeat_number_weight,
            "repeat_zodiac_weight": repeat_zodiac_weight,
            "stale_periods": stale_periods,
            "stale_weight": stale_weight,
            "min_bet_amount": MIN_BET_AMOUNT,
            "penalized_picks": soft_reduced_count,
            "reduced_total": cold_staked_total - staked_total,
            "repeat_number_picks": [
                int(p["number"]) for p in picks if p.get("is_repeat_number")
            ],
            "repeat_zodiac_picks": [
                int(p["number"]) for p in picks if p.get("is_repeat_zodiac")
            ],
            "stale_picks": [int(p["number"]) for p in picks if p.get("is_stale")],
        },
        # 角色金额配额（均注模式）：主推 / 次选 / 防守三组的权重与实际合计金额
        "role_quota": {
            "mode": mode,
            "applied": bool(mode == MODE_EVEN and not role_weights_are_uniform(role_weights)),
            "weights": {
                role: round(float(role_weights[role]), 4) for role in ROLE_ORDER
            },
            "labels": {role: ROLE_LABELS[role] for role in ROLE_ORDER},
            "totals": {
                role: sum(
                    int(amount)
                    for pick, amount in zip(picks, amounts)
                    if pick.get("role") == role
                )
                for role in ROLE_ORDER
            },
            "counts": {
                role: sum(1 for pick in picks if pick.get("role") == role)
                for role in ROLE_ORDER
            },
        },
        # 预测波动线 + 号码点阵（点阵覆盖 1..49，供前端画点阵）
        "lattice": {
            "enabled": lattice_enabled,
            "window": int(cfg["lattice_window"]),
            "band": lattice_band,
            # 本期主要取号区间所在波动桶（带内优先取号用）；无预测时为 null
            "primary_wave": lattice_primary,
            "primary_wave_label": (
                WAVE_LABELS[lattice_primary] if lattice_primary else None
            ),
            "numbers": lattice_rows,
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
