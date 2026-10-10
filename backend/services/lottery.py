"""号码波动推荐算法 —— 纯函数实现，不依赖数据库。

规则来源见 README「业务规则」。所有金额单位为元。
"""

from __future__ import annotations

import hashlib
import json
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
# 三类「软降权」常量（**已停用为选择权重，仅保留为信息标签**）
#
# 变更（本版）：「降权去除」。这三类软降权曾经同时作用于 ①候选排序权重（`order_pool`
# 的排序基准）与 ②金额打折（`soft_penalty_weight`）；现在**两处都不再使用**：
#   * 选号只由 波浪配额(balanced) + 点阵概率分布 + 期号种子随机 决定；
#   * 金额只由 角色配额(主推/次选/防守) + 均注/余数分配 决定。
# 三个设置键（repeat_number_weight / repeat_zodiac_weight / stale_weight）与
# `stale_periods` **继续被接受、继续被 clamp**（向后兼容旧配置与旧审计快照），
# 但对选号与金额**没有任何影响**（inert）。
#
# 仍然保留、仍然生效的是**信息标签**：`is_repeat_number` / `is_repeat_zodiac` /
# `is_stale` / `soft_reasons` / `periods_since_last` —— 前端徽章照旧如实渲染，
# 只是不再被折算成权重。`soft_penalized` 恒为 False、`soft_weight` 恒为 1.0
# （报告事实、不产生效果）。
#
# 与避冷加权（avoid_cold_*）的区别：避冷**仍然生效**（按自然日把冷号排到队尾 +
# 金额归 0），不在本次「去除」范围内。
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
# 开启后点阵权重**不再进入选号排序**，而只定义 1..49 上的**抽样概率分布**：
# 带内号码抽样概率更高、带外按衰减更低，但最终出号由「期号种子 PRNG」在配额内随机抽出
# —— 因此不会再出现「10 注全挤进同一个波动桶」的一坨形状（形状由 balanced 配额决定）。
# 口径：样本内经验分布，不是真实概率；不改变单注期望值（−2.0408 元/期）。
#
# 默认开启（2026-10-09 用户决定「只保留波浪法 + 点阵随机分布选号」）：点阵负责「偏向
# 哪些号」，balanced 配额负责「形状不歪」，期号种子随机负责「期期不同且可复现」。
# 旧行为（点阵当排序键、把 10 注挤进同一桶）可用 `lattice_enabled=false` +
# `pick_sampling="ranked"` + `wave_alloc="drain"` 复现，仅用于历史审计对照。
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

# --------------------------------------------------------------------------- #
# 近期走势加权（**已停用为选择权重**，仅保留展示 / 兼容）
#
# 变更（本版）：「近期走势加权去掉」。``trend_bias`` / ``trend_window`` /
# ``trend_bias_explicit`` **继续被接受、继续被 clamp**（向后兼容旧配置与旧审计
# 快照），但对选号 / 抽样 / 排序 / 金额**没有任何影响**（inert）—— 与三类软降权
# 同一模式。选号只由 波浪配额(balanced) + 点阵概率分布 + 期号种子随机 决定。
#
# 仍然保留、仍然产出的是**展示字段**：``trend_distributions``（波动×角色频次带）、
# ``trend_count`` / ``trend_note`` / ``number_frequency`` / ``trend_bias_label``。
# ``trend_sampling_weight`` 恒返回 1.0；``order_pool`` 不再吃走势前缀；
# 取号路径恒走「池首 / 配额内加权抽样」的 neutral 口径；
# ``effective_trend_bias`` / ``resolve_trend_bias`` 恒回退 ``neutral``，
# 写任何 ``trend_bias_explicit=True`` 都无法悄悄恢复加权。
#
# 口径提醒：近窗频次是本池经验频率，不是真实概率；notes / UI 禁止写「提高命中率」。
# --------------------------------------------------------------------------- #
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
    TREND_BIAS_NEUTRAL: "不加权（已停用）",
    TREND_BIAS_HOT: "热号偏好（已停用，仅展示）",
    TREND_BIAS_COLD: "冷号偏好（已停用，仅展示）",
    TREND_BIAS_MID: "中频优先（已停用，仅展示）",
}
# 角色带方向的白话说明（notes / 推送用）；必须与 _band_rank_prefix 的实际排序一致
# （展示切片仍按偏好方向切；选号不再消费这些带）
_BAND_ORDER_NOTE = {
    TREND_BIAS_HOT: "主推=最热段、次选=中段、防守=最冷段",
    TREND_BIAS_COLD: "主推=最冷段、次选=中段、防守=最热段",
    TREND_BIAS_MID: "主推=最接近中频段、次选=次接近段、防守=离中频最远段",
}
TREND_BIAS_PATTERN = "^(" + "|".join(TREND_BIASES) + ")$"
# 0 = 用全部样本；默认 20（仅影响走势分布参考展示，不影响选号）
DEFAULT_TREND_WINDOW = 20
TREND_WINDOW_MIN = 0
TREND_WINDOW_MAX = 500
# 「是否由用户**手动设置过**走势加权」的内部元数据键（落在 settings 表里）。
# 「走势加权去掉」后：``effective_trend_bias`` 恒回退 neutral，该标记不再能恢复加权；
# 键仍被接受 / 写入（幂等兼容），不进入对外设置契约（SettingsOut / 前端类型）。
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

# --------------------------------------------------------------------------- #
# 波动桶注数分配（`wave_alloc`）
#
# 背景：三类波动桶是**以最新一期特码为圆心**的连续区段：
#   small  = |n − latest| ≤ small_max ；normal = small_max < |n − latest| ≤ normal_max ；
#   big    = |n − latest| > normal_max 。
# 旧口径「drain」按 WAVE_ORDER（小→常→大）逐桶取满：小波动桶先取 1 注，
# 再在补齐轮里被**一次性抽干**，于是 10 注里恒有 8~9 注落在同一个连续区段
# （最新=10 时即 1..20），形成「一坨 + 两个离群点」。这是下注形状问题，
# 与期望值无关：任意 10 个不同号的命中概率恒为 10/49。
#
# 「balanced」（默认）改为：按非空桶均分注数（最大余额法，10 注 → 4/3/3），
# 轮转取号 —— 它是**形状骨架**，点阵概率分布只负责「配额内偏向哪些号」。
# 结果仍是 10 个不同号、确定性可复现（配合 pick_sampling=ranked 时逐字节可复现），
# P(hit) / EV 不变。旧行为保留在 drain 值下，仅供历史审计对照。
# --------------------------------------------------------------------------- #
WAVE_ALLOC_DRAIN = "drain"
WAVE_ALLOC_BALANCED = "balanced"
WAVE_ALLOCS = [WAVE_ALLOC_DRAIN, WAVE_ALLOC_BALANCED]
WAVE_ALLOC_LABELS = {
    WAVE_ALLOC_DRAIN: "逐桶取满（旧：号码挤在一段）",
    WAVE_ALLOC_BALANCED: "均衡分散（推荐：每个波动桶均分）",
}
WAVE_ALLOC_PATTERN = "^(" + "|".join(WAVE_ALLOCS) + ")$"
DEFAULT_WAVE_ALLOC = WAVE_ALLOC_BALANCED

# --------------------------------------------------------------------------- #
# 桶内取号方式（`pick_sampling`）——「随机分布选号」
#
# seeded_random（默认）：在 balanced 配额内，按**点阵概率分布**做**期号种子随机**
#   加权抽样（不放回）。同一期 + 同一组生效设置 → 逐字节一致；换期 → 换样本。
#   随机源 = ``random.Random(sha256(种子材料))``，不碰全局 ``random``，
#   不依赖 dict/set 迭代顺序，因此 ``PYTHONHASHSEED`` 不影响输出。
# ranked：旧口径，按池内确定性名次（点阵 → 走势 → 遗漏）逐位取号，供历史快照复现。
#
# 两者都不改变 P(hit) = pick_count/49 与 EV = total×(odds/49 − 1)。
# --------------------------------------------------------------------------- #
PICK_SAMPLING_SEEDED_RANDOM = "seeded_random"
PICK_SAMPLING_RANKED = "ranked"
PICK_SAMPLINGS = [PICK_SAMPLING_SEEDED_RANDOM, PICK_SAMPLING_RANKED]
PICK_SAMPLING_LABELS = {
    PICK_SAMPLING_SEEDED_RANDOM: "种子随机（推荐：期号确定性抽样）",
    PICK_SAMPLING_RANKED: "按名次（旧：确定性排序取号）",
}
PICK_SAMPLING_PATTERN = "^(" + "|".join(PICK_SAMPLINGS) + ")$"
DEFAULT_PICK_SAMPLING = PICK_SAMPLING_SEEDED_RANDOM

# 种子材料域标签（改动它 = 换一整批抽样结果；勿随意变更）
SAMPLING_SEED_DOMAIN = "lottery.pick_sampling.v1"
# 进入种子材料的「生效设置」白名单。
#
# 口径（务必保持）：白名单只放**决定候选池与配额结构**的键 —— 它们一变，
# 「抽哪些桶、每个桶抽几个、哪些号在池里」就变了，抽样理应重新摇一次。
# 反过来，加权类旋钮（trend_* / avoid_cold_* / lattice_* / score_* / role_*）
# 刻意**不进种子**：它们通过 ``trend_sampling_weight`` / ``bucket_sampling_weight``
# 直接改变每个号的抽样概率，已经忠实地体现在结果里；若再把它们塞进种子，
# 会出现「权重其实没变、只因为窗口参数被重摇一遍」的假敏感性 ——
# 等于把真正的惰性开关伪装成敏感开关。金额类（total_amount / amount_unit）
# 同样不入种子：它们不影响号码，只影响注数预算。
SAMPLING_SEED_SETTING_KEYS = (
    "pick_strategy",
    "pick_sampling",
    "wave_alloc",
    "pick_count",
    "mode",
    "small_max",
    "normal_max",
    "exclude_repeat_zodiac",
    "include_repeat_number",
)

# --------------------------------------------------------------------------- #
# 出票状态 / 机器可读原因码（一律英文枚举；汉字只出现在 *_message / notes）
#
# 预算连 **1 个注码单位** 都覆盖不了时（``total_amount // amount_unit == 0``），
# 推荐**不抛异常**：返回零注的 ``status = "no_ticket"`` 结果，附 reason_code +
# 中文 message。下游（``build_copy_text`` / API 层 / 出票单层）一律按「零注」
# 处理，绝不编造金额把空票伪装成有效票。
# --------------------------------------------------------------------------- #
STATUS_OK = "ok"
STATUS_NO_TICKET = "no_ticket"
# 运行时：预算不足 1 个注码单位（可能来自存储设置 + 本次请求覆盖）
REASON_BUDGET_TOO_SMALL_FOR_ONE_UNIT = "BUDGET_TOO_SMALL_FOR_ONE_UNIT"
# 设置写入校验：金额最小单位 > 最大投注金额（写入边界直接拒绝，不落库）
REASON_AMOUNT_UNIT_EXCEEDS_TOTAL_AMOUNT = "AMOUNT_UNIT_EXCEEDS_TOTAL_AMOUNT"
REASON_TOTAL_AMOUNT_NOT_POSITIVE = "TOTAL_AMOUNT_NOT_POSITIVE"
REASON_AMOUNT_UNIT_NOT_POSITIVE = "AMOUNT_UNIT_NOT_POSITIVE"
REASON_PICK_COUNT_NOT_POSITIVE = "PICK_COUNT_NOT_POSITIVE"
REASON_MESSAGES: dict[str, str] = {
    REASON_BUDGET_TOO_SMALL_FOR_ONE_UNIT: (
        "最大投注金额 {total} 元不足 1 个金额最小单位（{unit} 元），本次不出票："
        "请调高最大投注金额，或调低金额最小单位。"
    ),
    REASON_AMOUNT_UNIT_EXCEEDS_TOTAL_AMOUNT: (
        "金额最小单位 {unit} 元不能大于最大投注金额 {total} 元："
        "预算连 1 注都覆盖不了，请调高最大投注金额或调低金额最小单位。"
    ),
    REASON_TOTAL_AMOUNT_NOT_POSITIVE: "最大投注金额必须大于 0 元（当前 {total} 元）。",
    REASON_AMOUNT_UNIT_NOT_POSITIVE: "金额最小单位必须大于 0 元（当前 {unit} 元）。",
    REASON_PICK_COUNT_NOT_POSITIVE: "注数必须大于 0（当前 {pick_count}）。",
}


def reason_message(reason_code: str | None, **values: Any) -> str | None:
    """原因码 → 中文提示；无原因码 / 未知原因码返回 ``None``（不编造文案）。"""
    if not reason_code:
        return None
    template = REASON_MESSAGES.get(reason_code)
    if template is None:
        return None
    try:
        return template.format(**values)
    except (KeyError, IndexError):
        return template


class SettingsValidationError(ValueError):
    """设置**写入**校验失败：``reason_code``（英文枚举）+ ``message``（中文）。

    ``clamp_settings`` 的读取口径保持宽松（钳制 / 回退，永不抛错），只有
    ``merge_settings_patch``（设置写入的唯一汇合点）会抛这个异常，因此脏配置
    不可能落库，读侧也永远不会因为脏存量数据而崩。
    """

    def __init__(self, reason_code: str, **values: Any) -> None:
        self.reason_code = reason_code
        self.message = reason_message(reason_code, **values) or reason_code
        super().__init__(f"{reason_code}: {self.message}")


def _as_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def validate_settings(cfg: Mapping[str, Any] | None) -> None:
    """校验一组**生效配置**是否自洽；不合法抛 ``SettingsValidationError``。

    规则（边界层，越早拒绝越好）：
    - ``total_amount <= 0`` / ``amount_unit <= 0`` / ``pick_count <= 0``；
    - ``amount_unit > total_amount`` —— 预算连 1 注都覆盖不了，必然出不了票。
    """
    raw: Mapping[str, Any] = cfg if isinstance(cfg, Mapping) else {}
    total = _as_int_or_none(raw.get("total_amount"))
    unit = _as_int_or_none(raw.get("amount_unit"))
    count = _as_int_or_none(raw.get("pick_count"))
    if total is None or total <= 0:
        raise SettingsValidationError(
            REASON_TOTAL_AMOUNT_NOT_POSITIVE, total=raw.get("total_amount")
        )
    if unit is None or unit <= 0:
        raise SettingsValidationError(
            REASON_AMOUNT_UNIT_NOT_POSITIVE, unit=raw.get("amount_unit")
        )
    if count is None or count <= 0:
        raise SettingsValidationError(
            REASON_PICK_COUNT_NOT_POSITIVE, pick_count=raw.get("pick_count")
        )
    if unit > total:
        raise SettingsValidationError(
            REASON_AMOUNT_UNIT_EXCEEDS_TOTAL_AMOUNT, unit=unit, total=total
        )


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
    # 走势加权：已停用为选择权重；默认 neutral，窗口仅影响展示
    "trend_bias": TREND_BIAS_NEUTRAL,
    "trend_window": DEFAULT_TREND_WINDOW,
    # 避冷加权（按**自然日**，会把冷号排到队尾 + 金额归 0）：默认关闭 ——
    # 与「按期数软降权」不同，本项仍然**生效**（不在『降权去除』范围内）。
    # 两者同时开启会对冷号双重处理，需要旧行为时可在设置页单独打开。
    "avoid_cold_enabled": DEFAULT_AVOID_COLD_ENABLED,
    "avoid_cold_days": DEFAULT_AVOID_COLD_DAYS,
    # 选号策略与打分权重（score_top 才读权重；wave_round 忽略）
    "pick_strategy": DEFAULT_PICK_STRATEGY,
    # 波动桶注数分配：balanced=非空桶均分（默认，防「一坨」）；
    # drain=旧行为（按小→常→大逐桶取满，号码会挤在同一连续区段）——仅历史审计复用
    "wave_alloc": DEFAULT_WAVE_ALLOC,
    # 桶内取号方式：seeded_random=期号种子随机加权抽样（默认）；ranked=旧确定性按名次
    "pick_sampling": DEFAULT_PICK_SAMPLING,
    "score_w_focus": DEFAULT_SCORE_W_FOCUS,
    "score_w_mid": DEFAULT_SCORE_W_MID,
    "score_w_omit": DEFAULT_SCORE_W_OMIT,
    "score_w_diff": DEFAULT_SCORE_W_DIFF,
    # 显式标记：False = 用户从未手动设置过走势加权（含存量旧默认 hot 遗留行）
    TREND_BIAS_EXPLICIT_KEY: False,
    # ---- 三类软降权：**已停用为选择权重，仅保留信息标签**（见上方常量注释）----
    # 三个权重键与 stale_periods 继续被接受 / clamp（向后兼容旧配置与审计快照），
    # 但**不再影响选号与金额**；前端徽章照旧读 is_repeat_number / is_repeat_zodiac /
    # is_stale / soft_reasons。
    # 上期出过的号（重号）是否保留在候选池内：True = 不避开（用户口径）
    "include_repeat_number": True,
    # 重号（上期特码本身）：inert（历史值 0.5，不再使用）
    "repeat_number_weight": DEFAULT_REPEAT_NUMBER_WEIGHT,
    # 同肖（与上期同肖、非重号）：inert（历史值 0.8，不再使用）
    "repeat_zodiac_weight": DEFAULT_REPEAT_ZODIAC_WEIGHT,
    # 冷号按**期数**阈值：仅用于计算 is_stale 标签（不再降权）
    "stale_periods": DEFAULT_STALE_PERIODS,
    # : inert（历史值 0.3，不再使用）
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
    # 波动桶注数分配：缺失 / 脏值一律回退为默认（均衡分散）
    alloc = str(merged.get("wave_alloc") or "").strip().lower()
    merged["wave_alloc"] = alloc if alloc in WAVE_ALLOCS else DEFAULT_WAVE_ALLOC
    # 桶内取号方式：缺失 / 脏值一律回退为默认（期号种子随机）
    sampling = str(merged.get("pick_sampling") or "").strip().lower()
    merged["pick_sampling"] = (
        sampling if sampling in PICK_SAMPLINGS else DEFAULT_PICK_SAMPLING
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
    """**读取口径**：走势加权已停用 → 恒返回 ``neutral``。

    「近期走势加权去掉」后，不论库里存的是 hot/cold/mid、也不论
    ``trend_bias_explicit`` 是否为 True，读取侧一律回退 ``neutral``。
    参数被有意忽略 —— 结构上无法通过写库悄悄恢复加权（与
    ``soft_penalty_weight`` 恒返回 1.0 同模式）。
    """
    _ = cfg  # 显式忽略：任何存值 / 标记都无法恢复加权
    return TREND_BIAS_NEUTRAL


def resolve_trend_bias(cfg: dict[str, Any]) -> dict[str, Any]:
    """对配置应用读取口径（只改 ``trend_bias`` → 恒 ``neutral``，其余字段原样）。"""
    return {**cfg, "trend_bias": effective_trend_bias(cfg)}


def merge_settings_patch(
    current: dict[str, Any], patch: dict[str, Any]
) -> dict[str, Any]:
    """把可写 patch 叠加到当前配置，并维护走势加权兼容标记。

    - 只接受 ``DEFAULT_SETTINGS`` 里的键（``big_min`` 等派生字段永远写不进去）；
      避冷加权两项（``avoid_cold_enabled`` / ``avoid_cold_days``）同在
      ``DEFAULT_SETTINGS`` 内，因此由下面的通用循环原样接收（无需特殊分支）；
    - 本次**显式提交** ``trend_bias``（非 None）→ 仍打上
      ``trend_bias_explicit=True``（兼容旧审计），但读取侧
      ``effective_trend_bias`` 恒回退 neutral，**无法恢复加权**；
    - 未提交则沿用当前值：``current`` 已按读取口径解析过，恒为 neutral。
    """
    merged = dict(current)
    for key, value in (patch or {}).items():
        if value is None or key not in DEFAULT_SETTINGS:
            continue
        merged[key] = value
        if key == "trend_bias":
            merged[TREND_BIAS_EXPLICIT_KEY] = True
    validated = clamp_settings(merged)
    # 写入边界：预算 / 注码 / 注数必须自洽（amount_unit > total_amount 直接拒绝）。
    # 这里抛 SettingsValidationError，由 API 层转 4xx，脏配置永远不会落库。
    validate_settings(validated)
    return validated


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


def soft_flags(
    number: int,
    *,
    latest: int,
    periods_since_last: int | None,
    sample_size: int,
    stale_periods: int = DEFAULT_STALE_PERIODS,
) -> list[str]:
    """三类「软降权」的**信息标签**（只报告事实，不产生任何权重）。

    - ``repeat_number``：与上期特码完全相同的号（重号）
    - ``repeat_zodiac``：与上期同肖但不等于上期特码的号（**与重号互斥**）
    - ``stale``：本池样本内连续 ``stale_periods`` 期以上未出现（从未出现也算）

    **冷号判定的样本门槛**：只有 ``sample_size >= stale_periods`` 时才可能判为
    ``stale``。样本不足 60 期时无法证明「60 期未出现」，一律不打标签。

    「降权去除」后这三个标签不再折算成排序权重或金额折扣 —— 前端徽章照旧渲染，
    选号与金额完全不受影响（见上方「已停用为选择权重」说明）。
    """
    reasons: list[str] = []
    if int(number) == int(latest):
        reasons.append("repeat_number")
    elif zodiac_group(int(number)) == zodiac_group(int(latest)):
        reasons.append("repeat_zodiac")
    stale = int(sample_size) >= int(stale_periods) and (
        periods_since_last is None or int(periods_since_last) >= int(stale_periods)
    )
    if stale:
        reasons.append("stale")
    return reasons


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
    """**已停用**：恒返回 ``(1.0, [标签…])`` —— 权重参数被有意忽略。

    「降权去除」变更：重号 ``repeat_number_weight``（0.5）/ 同肖
    ``repeat_zodiac_weight``（0.8）/ 冷号 ``stale_weight``（0.3）**不再**作用于
    选号与金额。函数与签名保留下来只是为了兼容旧调用方与旧审计脚本，
    **四个权重参数被显式忽略** —— 因此任何设置写入都无法悄悄恢复降权行为
    （这是「不能只把 DB 值改成 1.0」的落地点）。

    仍然如实返回 ``soft_flags`` 的标签，供前端徽章 / 报表使用。
    """
    return 1.0, soft_flags(
        number,
        latest=latest,
        periods_since_last=periods_since_last,
        sample_size=sample_size,
        stale_periods=stale_periods,
    )


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
    """按权重压低各注金额的**纯工具函数**（只降不升；省下的预算**不补给**其它注）。

    ⚠️ **「降权去除」后线上引擎不再调用本函数**（选号与金额都不再依赖软降权）。
    保留它是为了：① 旧审计快照 / 外部脚本可继续复算历史金额；② 出票单镜像模块
    等仍传全 1.0 权重的调用方无论如何都是严格 no-op。因此它不会给当前推荐结果
    带来任何折扣 —— 想恢复打折必须显式传 ``< 1.0`` 的权重，而那已无任何线上入口。

    - ``target = 原金额 × 权重``，向下取整到 ``amount_unit`` 的整数倍；
    - 结果不低于「每注最低金额」= ``max(min_bet_amount, amount_unit)``
      （金额必须是单位倍数，故单位大于最低金额时以单位为准）；
    - **只降不升**：被上游压到 0 的注（如避冷封顶）不会被抬回下限；
    - 权重 ``>= 1.0`` 的注原样保留，因此权重全为 1.0 时严格 no-op。

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
) -> dict[str, list[dict[str, Any]]]:
    """同一波动桶内按**该偏好**的近窗频次切三角色带。

    规则（前后端一致、可解释）：
    1. 桶内按「偏好排序键」排序，再按「与最新号差值升序 → 号码升序」回落；
    2. 连续切成三段，尺寸 ``(n+2)//3, (n+1)//3, n//3``；
    3. **主推** = 该偏好最靠前的一段，**次选** = 中段，**防守** = 最靠后的一段。

    ``bias`` 决定「最靠前」的含义，三种方向互不相同：
    - ``hot``：近窗次数**降序** → 主推=最热段，防守=最冷段；
    - ``cold``：近窗次数**升序** → 主推=最冷段，防守=最热段；
    - ``mid``：按 ``|次数 − 近窗均值|`` **升序** → 主推=最接近中频段，
      防守=离中频最远段（两端极值）；
    - ``neutral``：不加权，保持旧的「最热在前」展示口径（仅供对照）。

    **变更（降权去除 + 点阵分布化）**：角色带排序不再吃「号码点阵」
    （``lattice_scores``）与「三类软降权」（``penalties``）两个前缀 —— 点阵只作
    抽样概率分布、软降权只作信息标签，两者都不再参与任何确定性排序键。

    次数与占比相对「近窗样本期数」计算，是经验频率不是真实概率。
    近窗 count=0 的号额外带 ``days_since_last``（样本内距上次出现自然日；
    从未出现为 null）。
    """

    def rank_key(item: dict[str, int]) -> tuple:
        number = item["number"]
        return _band_rank_prefix(
            number,
            bias=bias,
            trend_counts=trend_counts,
            mid_target=mid_target,
        )

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
) -> dict[str, Any]:
    """结构化「波动 × 角色」走势分布参考（供 UI 分块展示）。

    角色带按 ``bias`` 切分，与 ``wave_bands`` 同源。**变更**：不再接收点阵 /
    软降权前缀（点阵只作抽样概率分布、软降权只作信息标签，均不参与排序）。
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
) -> list[dict[str, int]]:
    """池内排序。

    排序键由前到后（越靠前越优先被取到）：

    1. **避冷加权**（``avoid_cold_enabled``）：把 ``days_since_last`` 超过
       ``avoid_cold_days``（含从未出现 = ``None``）的号整体排到队列末尾。
    2. 回落键：``(出现次数, 差值, 号码)``（全历史遗漏优先）。

    **变更（走势加权去掉 + 降权去除 + 点阵分布化）**：
    - ``trend_bias`` / ``trend_counts`` / ``mid_target`` **被有意忽略**——
      走势前缀不再进入排序键（与 ``trend_sampling_weight`` 恒 1.0 同模式）；
      参数保留只为兼容旧调用方签名；
    - 不再接收 ``lattice_scores``：点阵已降级为**抽样概率分布**，不再当排序键；
    - 不再接收 ``penalties``：三类软降权已降级为**信息标签**，
      且 ``soft_penalty_weight`` 恒返回 1.0 —— 结构与数值双重保证无法恢复降权。
    """
    _ = (trend_bias, trend_counts, mid_target)  # 走势加权已停用：显式忽略
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
        baseline = (
            history_counts.get(number, 0),
            item["diff"],
            item["number"],
        )
        return (*cold_prefix, *baseline)

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


def balanced_wave_quotas(
    pick_count: int, available: Mapping[str, int]
) -> dict[str, int]:
    """把 ``pick_count`` 注按**非空波动桶均分**（最大余额法），返回各桶配额。

    - 只有候选数 > 0 的桶参与分配；不足由其余桶继续摊（不会留下分不完的注数）；
    - 并列时按 ``WAVE_ORDER``（小→常→大）取靠前者 → 结果确定、可复现；
    - 配额之和恒等于 ``min(pick_count, 候选总数)``，因此不会凭空多出 / 少掉注数。

    例：10 注 / 三桶非空 → 4 / 3 / 3（不是旧口径的 8 / 1 / 1）。
    """
    total = max(0, int(pick_count))
    waves = list(WAVE_ORDER)
    quota = {wave: 0 for wave in waves}
    caps = {wave: max(0, int(available.get(wave, 0))) for wave in waves}
    remaining = total
    active = [wave for wave in waves if caps[wave] > 0]
    while remaining > 0 and active:
        base, extra = divmod(remaining, len(active))
        progressed = False
        for index, wave in enumerate(active):
            want = base + (1 if index < extra else 0)
            give = min(want, caps[wave] - quota[wave])
            if give > 0:
                quota[wave] += give
                remaining -= give
                progressed = True
        active = [wave for wave in active if quota[wave] < caps[wave]]
        if not progressed:
            break
    return quota


def sampling_seed_key(
    *,
    period: Any,
    latest: Any,
    previous: Any,
    settings: Mapping[str, Any] | None,
) -> str:
    """**桶内随机抽样的确定性种子**（SHA-256 十六进制串）。

    种子材料是 ``json.dumps`` 的固定结构（``sort_keys=True``、紧凑分隔符、
    ``ensure_ascii=True``），**逐字节可复现**：

    .. code-block:: text

        {"domain":"lottery.pick_sampling.v1",
         "latest":<最新特码>,
         "period":<期号 or null>,
         "period_fallback":"latest=<..>|previous=<..>"  # 仅 period 缺失时出现
         "previous":<上期特码 or null>,
         "settings":{"<白名单键>":<生效值>, ...}}       # 键按字典序

    设计要点（每一条都是为了「跨进程逐字节一致」）：

    1. **主键是期号** ``period``：同期刷新 / 重算 → 完全相同；换期 → 换样本。
    2. ``period`` 缺失（``None``）时回退 ``latest`` / ``previous`` ——
       仍然**与时间无关**（绝不使用 ``time`` / ``os.urandom`` / 全局 ``random``）。
    3. ``settings`` 只取 :data:`SAMPLING_SEED_SETTING_KEYS` 白名单里**决定候选池 /
       配额结构**的生效值（抽样模式、配额口径、池规模阈值、重复号/重复生肖开关、
       注数…）。**刻意排除**两类键：
       金额类（改预算不该改号）与**所有加权类旋钮**（点阵 / 走势 / 避冷 / ``score_w_*``
       / ``role_w_*``：它们通过 ``*_sampling_weight`` 直接改变每个号的抽样概率，
       已经体现在结果里；再进种子就会把「权重其实没变」的惰性参数伪装成敏感参数，
       等于骗人。
    4. 材料里没有 dict/set 迭代顺序、没有对象地址、没有时间 → ``PYTHONHASHSEED``
       不影响输出；``json`` 的键序由 ``sort_keys`` 固定。

    随机源用法：``random.Random(<本函数返回值>)``。CPython 对 ``str`` 种子走
    sha512 展开（``version=2``，跨进程 / 跨平台稳定），且**不使用**全局 ``random``。
    """
    source: Mapping[str, Any] = settings if isinstance(settings, Mapping) else {}
    payload: dict[str, Any] = {
        "domain": SAMPLING_SEED_DOMAIN,
        "latest": int(latest),
        "period": None if period is None else int(period),
        "previous": None if previous is None else int(previous),
        "settings": {},
    }
    if period is None:
        payload["period_fallback"] = f"latest={int(latest)}|previous={previous}"
    for key in SAMPLING_SEED_SETTING_KEYS:
        if key in source:
            payload["settings"][key] = source[key]
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def trend_sampling_weight(
    number: int,
    *,
    bias: str,
    trend_counts: Mapping[int, Any] | None,
    mid_target: float = 0.0,
) -> float:
    """**已停用**：恒返回 ``1.0`` —— 走势偏好参数被有意忽略。

    「近期走势加权去掉」后，``hot`` / ``cold`` / ``mid`` 不再改变桶内抽样概率。
    函数与签名保留只为兼容旧调用方与旧审计脚本；``bias`` / ``trend_counts`` /
    ``mid_target`` / ``number`` 全部被显式忽略，因此写任何设置都无法悄悄恢复
    走势加权（与 ``soft_penalty_weight`` 同模式）。
    """
    _ = (number, bias, trend_counts, mid_target)
    return 1.0


def bucket_sampling_weight(
    number: int,
    *,
    lattice_scores: Mapping[int, float] | None = None,
    cold_weight: float = 1.0,
    trend_weight: float = 1.0,
) -> float:
    """桶内抽样的**相对权重**（只有比值有意义，不是概率）。

    = ``点阵权重 × 避冷权重 × 走势偏好倍数``（三项缺省均为 1.0 → 均匀抽样）。

    - 点阵（``lattice_scores``）：带内 1.0、带外按 ``1/(1+d/6)`` 衰减 ——
      **只决定抽样概率高低**，不再决定取号顺序；
    - 避冷（``cold_weight``）：沿用 ``avoid_cold_weight`` 的天数衰减 ——
      冷号概率被压低但**不排除**（与旧的「排到队尾」同向、更温和）；
    - 走势（``trend_weight``）：``trend_sampling_weight`` 的倍数。

    返回值恒 ``>= 0``；全 0 时调用方回退为均匀抽样（不会无号可取）。
    """
    weight = 1.0
    if lattice_scores:
        weight *= float(lattice_scores.get(int(number), 1.0))
    weight *= max(0.0, float(cold_weight))
    weight *= max(0.0, float(trend_weight))
    return max(0.0, weight)


def weighted_sample_distinct(
    rng: random.Random,
    weighted: Sequence[tuple[int, float]],
    count: int,
) -> list[int]:
    """按权重**不放回**抽取 ``count`` 个互不相同的号（确定性：同一 rng 状态 → 同一结果）。

    实现是「轮盘赌 + 抽走」：每轮按权重区间取一个号，再从候选表移除。
    复杂度 ``O(k·n)``，``n ≤ 49`` 时可忽略。

    - 只依赖入参序列顺序与浮点求和（IEEE 双精度、跨平台一致）与 ``rng`` ——
      **不依赖 dict / set 迭代顺序**，因此与 ``PYTHONHASHSEED`` 无关；
    - 权重全为 0（或负数被钳 0）时回退 ``rng.randrange`` 均匀抽取，绝不返回空；
    - ``count`` 超过候选数时最多返回全部候选（调用方配额已按候选数封顶）。
    """
    pool: list[tuple[int, float]] = [
        (int(number), max(0.0, float(weight))) for number, weight in weighted
    ]
    take = max(0, min(int(count), len(pool)))
    picked: list[int] = []
    for _ in range(take):
        total = 0.0
        for _number, weight in pool:
            total += weight
        if total <= 0.0:
            index = rng.randrange(len(pool))
        else:
            threshold = rng.random() * total
            index = len(pool) - 1
            acc = 0.0
            for position, (_number, weight) in enumerate(pool):
                acc += weight
                if threshold < acc:
                    index = position
                    break
        picked.append(pool.pop(index)[0])
    return picked


def pick_most_spread(numbers: Sequence[int], used: Iterable[int]) -> int:
    """在候选序列里挑「离已选号码最远」的那个 → 返回下标（并列取最靠前的候选）。

    距离定义为「到最近一个已选号码的绝对差」；``used`` 为空时返回 0，
    即保持候选取号池既有的排序（走势偏好 → 遗漏），不引入任何随机性。
    纯函数：同一组入参 → 同一结果，满足前向台账的确定性要求。
    """
    pool = [int(number) for number in numbers]
    if not pool:
        return 0
    anchors = [int(number) for number in used]
    if not anchors:
        return 0
    best_index = 0
    best_distance = min(abs(pool[0] - anchor) for anchor in anchors)
    for index in range(1, len(pool)):
        distance = min(abs(pool[index] - anchor) for anchor in anchors)
        if distance > best_distance:
            best_distance = distance
            best_index = index
    return best_index


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


def plan_budget(
    mode: str,
    total: int,
    pick_count: int,
    amount_unit: int,
    *,
    seed: Any = None,
) -> dict[str, Any]:
    """**唯一**的预算规范化入口：把「模式 → 可覆盖注数 / 可分配金额」统一算一次。

    返回 ``allocated_total`` / ``pick_count`` / ``notes`` / ``reason_code`` /
    ``reason_message``。它把 ``random`` 与其余模式归一到同一形状：

    - ``random``：走 ``random_allocation``（隔板法 + 同样的降级说明）；
    - 其余：走 ``prepare_budget``（取整 + 覆盖不了时降级注数）。

    ``pick_count == 0`` ⇔ 预算连 1 个注码单位都覆盖不了：此时 ``reason_code`` 非空，
    调用方必须返回零注的 ``no_ticket`` 结果 —— **不得**用 ``max(1, ...)`` 把注数
    抬回 1，否则「计划注数 / 实际选号 / 分配金额」三处会重新打架。
    """
    if mode == MODE_RANDOM:
        plan = random_allocation(total, pick_count, amount_unit, seed)
        allocated_total = int(plan["allocated_total"])
        count = len(plan["amounts"])
        notes = list(plan["notes"])
    else:
        plan = prepare_budget(mode, total, pick_count, amount_unit)
        allocated_total = int(plan["allocated_total"])
        count = int(plan["pick_count"])
        notes = list(plan["notes"])

    reason_code: str | None = None
    if count <= 0:
        reason_code = REASON_BUDGET_TOO_SMALL_FOR_ONE_UNIT
    return {
        "allocated_total": allocated_total,
        "pick_count": count,
        "notes": notes,
        "reason_code": reason_code,
        "reason_message": reason_message(
            reason_code, total=int(total), unit=int(amount_unit)
        ),
    }


def format_number(number: Any) -> str:
    """把号码格式化为定宽两位数字串（1-49 → ``01``…``49``）。

    仅用于展示 / 复制文案：个位数补前导 0（``9`` → ``09``），两位数原样返回
    （``13`` → ``13``，``49`` → ``49``）。金额等其它数字不使用本函数。
    """
    return f"{int(number):02d}"


def build_copy_text(mode: str, picks: list[dict[str, Any]]) -> str:
    """生成一键复制的竞猜投注串（金额文案必须跟各注实际金额一致）。

    空 ``picks``（例如预算不足 1 个注码单位 → ``status = "no_ticket"``）返回空串，
    绝不编造金额；非空时每一注都必须带 ``amount``（由 ``recommend`` 按构造保证）。

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
) -> list[dict[str, Any]]:
    """从三类波动池打分，取分数最高的 ``pick_count`` 个（稳定排序）。

    ``pick_count <= 0`` 时返回空列表 —— **不**用 ``max(1, ...)`` 抬回 1 注：
    注数为 0 只可能来自「预算连 1 个注码单位都覆盖不了」，此时上游要的是
    零注的 ``no_ticket`` 结果，硬凑 1 注会让「计划注数 / 选号 / 金额」三处不一致。

    **变更（降权去除 + 点阵分布化）**：本函数是 ``score_top`` 遗留策略的
    **确定性打分 Top-N**（按定义就是按名次取号，不随机）。载荷里不再有
    ``lattice_scores`` / ``penalties`` 乘数：
    - 软降权已停用（且 ``soft_penalty_weight`` 恒返回 1.0），结构上无法再降权；
    - 号码点阵已降级为**抽样概率分布**，只服务 ``wave_round`` 路径的种子随机抽样，
      不再作为 ``score_top`` 的确定性加分 —— 否则点阵又会变成排序键。
    """
    limit = max(0, int(pick_count))
    if limit <= 0:
        return []
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
        if len(selected) >= limit:
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

    走势加权（``trend_bias`` / ``trend_window``）—— **已停用为选择权重**：
    - 设置键仍接受 / 回显；``trend_distributions`` / ``trend_count`` 等仍产出供对照；
    - 选号 / 抽样 / 排序**恒按不加权口径**（``trend_sampling_weight`` 恒 1.0，
      ``order_pool`` 不吃走势前缀，取号不走角色带偏好）；
    - ``trend_window`` 只影响走势分布参考展示，不影响选号。

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

    # 预算规范化（唯一入口，random / 其余模式同口径）：可覆盖注数 / 可分配金额 /
    # 降级说明 / 原因码。下面「选号、角色、金额」三处全部以这里的 ``pick_count``
    # 为准 —— 预算不够 1 个注码单位时它是 0，三处就都描述 0 注，不会再打架。
    budget_plan = plan_budget(mode, total, pick_count, unit, seed=seed_key)
    # 降级信息（不可满足 / 非整数倍）如实进入 notes，绝不静默
    allocation_notes.extend(budget_plan["notes"])
    total = budget_plan["allocated_total"]
    pick_count = budget_plan["pick_count"]
    no_ticket_reason: str | None = budget_plan["reason_code"]
    no_ticket_message: str | None = budget_plan["reason_message"]

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
    # 三类软降权 → 只保留**信息标签**（「降权去除」：不再产生任何权重）
    periods_since_last = compute_periods_since_last(history)
    stale_periods = int(cfg["stale_periods"])

    soft_reasons: dict[int, list[str]] = {
        number: soft_flags(
            number,
            latest=int(latest),
            periods_since_last=periods_since_last.get(number),
            sample_size=len(history),
            stale_periods=stale_periods,
        )
        for number in range(NUMBER_MIN, NUMBER_MAX + 1)
    }

    # 预测波动线 + 号码点阵：开启后**只定义抽样概率分布**（带内概率更高），
    # 不再参与任何确定性排序，也不再「先在预测波动桶取满」。
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
    # **展示用**（不再参与选号）：预测带中心所在的波动桶，仅用于前端标注
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

    # 中频目标：近窗内「出现过的号码」的平均次数；全空则 0
    # （仅供走势分布参考 / score_top 遗留打分；seeded_random 默认路径不消费）
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
        # 上期出过的号不避开（只打「重号」信息标签），所以候选池保留重号
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

    # 走势分布参考：按**同一偏好**切频次三段（供 UI 分块展示；
    # 变更后与选号不再共用「点阵 / 软降权」前缀，点阵只作抽样概率分布）
    # 走势分布参考（**仅展示**）：按设置里的偏好切频次三段，供 UI 分块对照；
    # 「近期走势加权去掉」后不再参与取号（选号恒走 ordered_pools / 种子抽样）。
    trend_distributions = build_trend_distributions(
        pools,
        trend_counts,
        window=trend_window,
        used_window=used_window,
        bias=trend_bias,
        days_since_last=days_since_last,
        mid_target=mid_target,
    )

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
    pick_strategy = str(cfg.get("pick_strategy") or DEFAULT_PICK_STRATEGY)
    if pick_strategy not in PICK_STRATEGIES:
        pick_strategy = DEFAULT_PICK_STRATEGY
    wave_alloc = str(cfg.get("wave_alloc") or DEFAULT_WAVE_ALLOC)
    if wave_alloc not in WAVE_ALLOCS:
        wave_alloc = DEFAULT_WAVE_ALLOC
    # 桶内取号方式（英文枚举）：seeded_random=期号种子随机（默认）；ranked=确定性按名次
    pick_sampling = str(cfg.get("pick_sampling") or DEFAULT_PICK_SAMPLING)
    if pick_sampling not in PICK_SAMPLINGS:
        pick_sampling = DEFAULT_PICK_SAMPLING
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

            「近期走势加权去掉」后恒取池内第一个（池已按「避冷 → 遗漏」排好）；
            ``preferred_role`` / 角色带不再参与选号（参数保留仅兼容旧调用形）。
            """
            _ = preferred_role  # 走势加权已停用：角色带偏好不再取号
            if not ordered_pools[wave]:
                return False
            _annex_pick(wave, ordered_pools[wave][0])
            return True

        def _take_spread_one(wave: str, preferred_role: str | None = None) -> bool:
            """均衡分配 + ranked 专用：从该波动桶取**离已选号码最远**的一个号。

            「近期走势加权去掉」后候选恒为池内既有排序；只把「取池首」换成
            「取最分散的一个」；并列时取池内靠前者，因此仍然确定、可复现。
            ``used_numbers`` 为空时等价于取池首。``preferred_role`` 被忽略。
            """
            _ = preferred_role  # 走势加权已停用：角色带偏好不再取号
            if not ordered_pools[wave]:
                return False
            candidates = list(ordered_pools[wave])
            index = pick_most_spread(
                [int(item["number"]) for item in candidates], used_numbers
            )
            _annex_pick(wave, candidates[index])
            return True

        # 取号分派（「随机分布选号」）：
        #   balanced + seeded_random（默认）= 配额内按点阵概率的**期号种子随机**抽样；
        #   balanced + ranked               = 配额内**确定性**取最分散的号（旧口径）；
        #   drain                           = 旧行为（逐桶取满，仅历史审计复用）。
        # 点阵不再「先在预测波动桶取满」：它只改抽样概率，不改取号顺序 / 桶配额。
        if wave_alloc == WAVE_ALLOC_BALANCED:
            # 均衡分散（形状骨架）：配额按非空桶均分（最大余额法，10 注 → 4/3/3），
            # 因此「某个连续区段被抽干」的结构被彻底消除；注数 / P(hit) / EV 不变。
            quotas = balanced_wave_quotas(
                pick_count, {wave: len(ordered_pools[wave]) for wave in WAVE_ORDER}
            )
            if pick_sampling == PICK_SAMPLING_SEEDED_RANDOM:
                rng = _random_source(
                    sampling_seed_key(
                        period=period,
                        latest=latest,
                        previous=previous,
                        settings=cfg,
                    )
                )
                for wave in WAVE_ORDER:
                    quota = int(quotas[wave])
                    if quota <= 0:
                        continue
                    weighted: list[tuple[int, float]] = []
                    for item in ordered_pools[wave]:
                        number = int(item["number"])
                        weighted.append(
                            (
                                number,
                                bucket_sampling_weight(
                                    number,
                                    lattice_scores=lattice_scores,
                                    # 避冷仍然生效：冷号概率被压低（不排除）
                                    cold_weight=avoid_cold_weight(
                                        days_since_last.get(number),
                                        enabled=cold_enabled,
                                        threshold=cold_days,
                                    ),
                                    trend_weight=trend_sampling_weight(
                                        number,
                                        bias=trend_bias,
                                        trend_counts=trend_counts,
                                        mid_target=mid_target,
                                    ),
                                ),
                            )
                        )
                    for number in weighted_sample_distinct(rng, weighted, quota):
                        _annex_pick(
                            wave,
                            {"number": number, "diff": abs(number - int(latest))},
                        )
            else:
                # ranked（旧：确定性按名次）：轮转取号 + 桶内取「离已选号码最远」的号
                remaining = dict(quotas)
                while len(picks) < pick_count:
                    progressed = False
                    for wave in WAVE_ORDER:
                        if len(picks) >= pick_count:
                            break
                        if remaining[wave] <= 0:
                            continue
                        # 角色顺位与旧路径一致（只影响角色带映射，不影响金额口径）
                        role = assign_roles(pick_count)[len(picks)]
                        if not _take_spread_one(wave, role):
                            remaining[wave] = 0
                            continue
                        remaining[wave] -= 1
                        progressed = True
                    if not progressed:
                        break
        else:
            # 旧口径（wave_alloc=drain）：候选选取固定按 WAVE_ORDER
            # （小波动 → 常规波动 → 大跳）逐类取一个，与侧重/回补顺序解耦；
            # 侧重顺序只用于角色分配，以及加权时的角色带映射。
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
    # 惩罚前金额快照（唯一作用：逐注判断 `amount_reduced`；今天只可能来自避冷封顶）
    budget_before_penalty = list(amounts)
    cold_reduced_count = 0
    if cold_enabled:
        amounts, cold_reduced_count = apply_avoid_cold_amounts(
            amounts, cold_weights, amount_unit=cfg["amount_unit"]
        )
    # 三类软降权已**完全停用**：金额只由「角色配额 + 均注/余数」决定，
    # 因此这里不再调用 ``apply_soft_weights``（结构上不可能再出现降权折扣）。
    # ``soft_weight`` 恒为 1.0、``soft_penalized`` 恒为 False（见下），
    # 只报告「是否命中重号 / 同肖 / 冷号标签」这一事实。
    cold_staked_total = sum(int(amount) for amount in amounts)
    soft_reduced_count = 0
    staked_total = sum(int(amount) for amount in amounts)

    # 内部一致性（按构造保证）：picks 数量 ≤ 计划注数 ≤ 可覆盖的注码单位数，
    # 因此 ``allocate_amounts`` 必然返回与 picks 等长的金额列表。若这里不成立，
    # 说明三处口径发生了漂移 —— 宁可显式失败，也不产出「有号无金额」的注。
    assert len(amounts) == len(picks), (
        f"预算/选号/金额口径漂移：picks={len(picks)} amounts={len(amounts)}"
    )

    for index, (pick, role, amount) in enumerate(zip(picks, roles, amounts)):
        cold_weight = cold_weights[index] if index < len(cold_weights) else 1.0
        number = int(pick["number"])
        reasons = soft_reasons.get(number, [])
        # 「降权去除」后权重恒为 1.0（不再随设置变化）
        soft_weight_value = 1.0
        periods = periods_since_last.get(number)
        pick["role"] = role
        pick["role_label"] = ROLE_LABELS[role]
        pick["amount"] = amount
        pick["wave_label"] = WAVE_LABELS[pick["wave_type"]]
        # 三类软降权标记（用户要求：不排除，且**信息必须诚实**，
        # 但「降权去除」后 soft_weight 恒 1.0 / soft_penalized 恒 False）
        pick["is_repeat_number"] = number == int(latest)
        pick["is_repeat_zodiac"] = bool(
            number != int(latest) and zodiac_group(number) == zodiac_group(latest)
        )
        pick["is_stale"] = "stale" in reasons
        pick["periods_since_last"] = periods
        pick["soft_weight"] = round(float(soft_weight_value), 4)
        pick["soft_reasons"] = list(reasons)
        # soft_penalized = 是否命中降权规则并**真的被降权**。
        # 「降权去除」后恒为 False（权重恒 1.0）—— 命中标签只进 soft_reasons，
        # 不再产生任何权重效果；amount_reduced 仍如实反映「金额是否被压过」
        # （今天只可能来自避冷封顶，不再是软降权）。
        pick["soft_penalized"] = bool(soft_weight_value < 1.0)
        pick["amount_reduced"] = bool(
            amount
            < (
                budget_before_penalty[index]
                if index < len(budget_before_penalty)
                else amount
            )
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
    if no_ticket_reason is not None:
        # 预算连 1 个注码单位都覆盖不了：明确说明「本次不出票」，不静默给空 picks
        notes.append(
            no_ticket_message
            or "预算不足 1 个金额最小单位，本次不出票。"
        )
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

    # 近期走势加权：**已去除**，如实说明「只保留展示、不产生权重」
    notes.append(
        "近期走势加权已停用：``trend_bias`` / ``trend_window`` 不再改变选号、抽样或金额"
        "（写入热号 / 冷号 / 中频偏好也不会恢复加权）；"
        "池内排序仍按本池全历史遗漏优先（可叠加避冷排后）；"
        "下方「走势分布参考」与每注 ``trend_count`` 仅供对照阅读。"
        "这是样本内经验频率展示，不是真实概率，也不承诺提高命中率。"
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

    # 三类软降权：**已去除**，如实说明「只保留标签、不产生权重」
    notes.append(
        f"三类软降权（重号/同肖/冷号）已去除：上期特码 {latest} 本身、与上期同肖的号、"
        f"以及本池样本内连续 {stale_periods} 期以上未出现的号，"
        "都**不再降权、不再打折**——它们照常参与选号，金额也只按角色配额分配。"
        "若被选中仍会在号码上标注「重号」「同肖」「冷号」（信息标签，仅供阅读）。"
        "（历史权重值 0.5 / 0.8 / 0.3 已停用；写入这些设置也不会改变选号或金额。）"
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

    # 预测波动线 + 号码点阵：**只定义抽样概率分布**（样本内经验分布，不是概率）
    if lattice_enabled and lattice_band:
        notes.append(
            f"预测波动线已开启（最近 {lattice_band['used_window']} 对相邻差值）："
            f"中心 {lattice_band['center']}（{lattice_band['wave_label']}）、"
            f"带宽 {lattice_band['low']}~{lattice_band['high']}。"
            "号码点阵**只定义抽样概率分布**：与最新特码的差值落在带内的号"
            "抽样概率更高，带外按距离衰减（1/(1+距离/6)）。"
            "它不再参与任何确定性排序 —— 出号仍由「波动桶均分配额 + 期号种子随机」决定，"
            "因此不会出现「10 注全挤进同一个波动桶」。"
            "这是样本内经验分布，不是真实概率，也不承诺提高命中率。"
        )
    elif lattice_enabled:
        notes.append(
            "数据不足：样本不足两对相邻差值，本期无预测波动线，"
            "抽样概率退化为均匀（号码池与配额不受影响）。"
        )
    else:
        notes.append(
            "预测波动线与号码点阵已关闭：桶内抽样概率为均匀（号码池与配额不受影响）。"
        )

    if pick_strategy == PICK_STRATEGY_SCORE_TOP:
        notes.append(
            "选号策略为打分 Top-N：在候选池内按侧重波段、中频接近度、遗漏与差值综合打分后取前 N 注；"
            "这是样本内排序对照（用于相对随机命中差），不是真实概率，也不承诺提高命中率。"
        )
    elif wave_alloc == WAVE_ALLOC_BALANCED and pick_sampling == PICK_SAMPLING_SEEDED_RANDOM:
        notes.append(
            "选号策略为波动轮取（均衡分散 + 种子随机）：先按非空波动桶均分注数"
            f"（{pick_count} 注 → 各桶尽量均分，最多相差 1 注），"
            "再在每个桶的配额内按「点阵概率分布」用**期号种子随机**不放回抽样。"
            "形状由配额保证（不会挤在同一个连续区段），具体号码期期不同；"
            "同一期 + 同一组设置重算恒得同一组号（可复现）。"
            "命中概率与期望值完全不变（任意"
            f" {pick_count} 个不同号命中概率恒为 {pick_count}/49），"
            "也不承诺提高命中率。"
        )
    elif wave_alloc == WAVE_ALLOC_BALANCED:
        notes.append(
            "选号策略为波动轮取（均衡分散 + 按名次）：三个波动桶按非空桶均分注数"
            f"（{pick_count} 注 → 各桶尽量均分，最多相差 1 注），轮转取号，"
            "桶内优先挑「离已选号码最远」的号，避免号码挤在同一个连续区段。"
            "该口径为**确定性**（不随机），供历史审计快照复现。"
            "命中概率与期望值完全不变（任意"
            f" {pick_count} 个不同号命中概率恒为 {pick_count}/49）。"
        )
    else:
        notes.append(
            "选号策略为波动轮取（逐桶取满，旧口径）："
            "「小/常/大跳各取一注，不足再按波动桶补齐」——"
            "该口径会把小波动桶一次性抽干，号码容易挤在同一连续区段；"
            "仅供历史审计复现，不建议线上使用。"
        )

    # 派生展示值（deprecated）：均分后再向下对齐到 amount_unit
    effective_notes = max(1, len(picks))
    no_ticket = len(picks) == 0
    return {
        # 出票状态（英文枚举）：ok = 正常出票；no_ticket = 零注（预算不足 1 个注码单位）
        "status": STATUS_NO_TICKET if no_ticket else STATUS_OK,
        # 机器可读原因码（无异常时 None）：BUDGET_TOO_SMALL_FOR_ONE_UNIT
        "reason_code": no_ticket_reason,
        # 中文人读说明（无异常时 None）；前端可直接展示
        "reason_message": no_ticket_message,
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
        # 三类软降权摘要（**已停用**：权重恒 1.0、无折扣；只保留标签计数）
        "soft_weights": {
            # 三个历史权重值原样回显（inert：写入它们不会改变选号 / 金额）
            "repeat_number_weight": float(cfg["repeat_number_weight"]),
            "repeat_zodiac_weight": float(cfg["repeat_zodiac_weight"]),
            "stale_periods": stale_periods,
            "stale_weight": float(cfg["stale_weight"]),
            "min_bet_amount": MIN_BET_AMOUNT,
            "applied": False,
            # 两个折扣计数恒为 0（结构上不再打折）
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
            # **展示用**：预测带中心落在哪个波动桶（不再决定取号顺序 / 桶配额）
            "primary_wave": lattice_primary,
            "primary_wave_label": (
                WAVE_LABELS[lattice_primary] if lattice_primary else None
            ),
            "numbers": lattice_rows,
            # 说明点阵的新语义：只定义抽样概率分布，不参与排序
            "role": "sampling_distribution",
        },
        "focus_order": [{"type": w, "label": WAVE_LABELS[w]} for w in focus],
        # 桶内取号方式（英文枚举 + 中文标签）：seeded_random / ranked
        "pick_sampling": pick_sampling,
        "pick_sampling_label": PICK_SAMPLING_LABELS.get(pick_sampling, pick_sampling),
        "wave_alloc": wave_alloc,
        "wave_alloc_label": WAVE_ALLOC_LABELS.get(wave_alloc, wave_alloc),
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
