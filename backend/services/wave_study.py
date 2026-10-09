r"""波动法（wave）**专项**评估 —— 纯模块，不访问数据库 / 不碰线上推荐路径。

背景（本模块存在的理由）
--------------------------------------------------------------------
此前 ``services/dist_audit.py`` 在**同一个 Holm 家族内**审计了 23 条假设，结果
``survivor_count = 0``。但家族里最极端的一行是 ``wave_lattice``：
180 期逐期命中 48 次（26.67%），置换零分布均值 36.6885、sd 5.3349、z = 2.1203、
原始单侧 p = 0.0285，Holm 校正后 0.6552。

「23 条假设里最好的一条」这个说法是**统计论证**，不是对波动法本身的测量。
本模块因此做一次**专门的、预先登记的、做过稳健性检查的**波动法评估：

- **不继承**那 23 条假设的惩罚（家族不同、口径不同）；
- 但**如实计入**波动法自己可以试多少种变体（波段半宽 / 覆盖分位 / 走势窗口 /
  走势偏好 / 波段定义本身），把变体家族大小算清楚，再用 ``holm_adjusted_p``
  校正；—— 校正函数**复用** ``services.analytics``，不重写。
- 最关键的检验是**同一变体网格上的 max 零分布**：每次置换都在**同一网格**上取
  最大值，得到「max」的零分布，再看观测 max 落在哪里。单变体的 p 只作透明度展示，
  合法性说明写在 ``legitimate_test_note()`` 里。

口径铁律（违反即为缺陷）
--------------------------------------------------------------------
- 所有结论只针对「本池已导入的 N 期样本」；**禁止升格为全市场 / 全量结论**。
- 落库 / 传输的枚举一律英文码（变体 id、波段定义、判定码）；汉字只出现在
  ``*_label`` 与提示文案 / 文档里。
- 样本不足时显式给出 ``data_status = INSUFFICIENT``（复用 ``analytics`` 的口径），
  绝不用 0 值冒充结论。
- 走势 / 命中都是**样本内经验频率**，不是概率，也不承诺提高命中率。

严格 walk-forward 契约
--------------------------------------------------------------------
``wave_step(numbers, position, ...)`` 只能看见 ``numbers[:position]``（严格早于目标期）。
本模块不做参数拟合（变体是预先声明的一整张网格，全部一次性汇报），因此不需要
训练 / 验证切分来防过拟合 —— 防过拟合靠的是「max 零分布 + 家族诚实计数」。
"""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter
from datetime import date
from typing import Any, Mapping, Sequence

from services import dist_audit as dist_audit
from services import dist_engine as dist_engine
from services.analytics import (
    DATA_STATUS_INSUFFICIENT,
    DATA_STATUS_LABELS,
    DATA_STATUS_OK,
    binomial_tail_p,
    holm_adjusted_p,
    minimum_detectable_delta,
)
from services.lottery import (
    DEFAULT_SETTINGS,
    TREND_BIASES,
    TREND_BIAS_COLD,
    TREND_BIAS_HOT,
    TREND_BIAS_MID,
    TREND_BIAS_NEUTRAL,
    WAVE_ALLOC_DRAIN,
    classify_wave,
    clamp_settings,
    lattice_weight,
    predict_wave_band,
    recent_number_frequency,
    zodiac_numbers,
)
from services.max_fit import (
    K_DEFAULT,
    NUMBER_MAX,
    NUMBER_MIN,
    NUMBERS,
    NUM_STATES,
    ODDS_DEFAULT,
    UNIFORM_BRIER,
    UNIFORM_LOG_LOSS,
    UNIFORM_MEAN_RANK,
    brier_score,
    log_loss,
    mean_rank,
)

# --------------------------------------------------------------------------- #
# 常量与英文枚举
# --------------------------------------------------------------------------- #
CLAIM_NO_EDGE = dist_engine.CLAIM_NO_EDGE
CLAIM_LABELS = dict(dist_engine.CLAIM_LABELS)

WAVE_TOOL_LABELS: dict[str, str] = {
    "scope": "本池已导入样本内（不涉及任何全市场数据）",
}

# 预登记口径：与审计脚本的置换种子基数同源，便于横向对照
WAVE_SEED_BASE = 20261007
WAVE_PERMS_DEFAULT = 200
WAVE_ALPHA = 0.05
# 预登记评估窗：与 dist_audit 的 wave_lattice 行（warmup=30 → 180 期）逐位可比
WAVE_WARMUP = 30
WAVE_K = K_DEFAULT
WAVE_ROLLING_BLOCK = 30

WAVE_BASELINE_RATE = K_DEFAULT / NUM_STATES  # 10/49 = 0.2040816...
WAVE_ODDS = float(ODDS_DEFAULT)
WAVE_BREAK_EVEN_RATE = K_DEFAULT / WAVE_ODDS  # 10/47 = 0.2127660
WAVE_BREAK_EVEN_EDGE = WAVE_BREAK_EVEN_RATE - WAVE_BASELINE_RATE

# 波段阈值：固定为**审计快照**（small_max=10 / normal_max=30），
# 这样 ``engine_component`` 变体与本池既有 48/180 那一行逐位同源；
# 阈值本身不作为变体轴（避免把「配置差异」混进「波动变体家族」）。
WAVE_SMALL_MAX = int(DEFAULT_SETTINGS["small_max"])
WAVE_NORMAL_MAX = int(DEFAULT_SETTINGS["normal_max"])

# 波段定义（英文枚举；中文只进 label）
VARIANT_ENGINE_COMPONENT = "engine_component"
VARIANT_PRODUCTION_BAND = "production_band"
VARIANT_DENSITY_BAND = "density_band"
VARIANT_HALFWIDTH_BAND = "halfwidth_band"
VARIANT_VOLATILITY_BAND = "volatility_band"
WAVE_DEFINITIONS: tuple[str, ...] = (
    VARIANT_ENGINE_COMPONENT,
    VARIANT_PRODUCTION_BAND,
    VARIANT_DENSITY_BAND,
    VARIANT_HALFWIDTH_BAND,
    VARIANT_VOLATILITY_BAND,
)
WAVE_DEFINITION_LABELS: dict[str, str] = {
    VARIANT_ENGINE_COMPONENT: "生产引擎 WAVE 组件（点阵带 × 近窗差值密度，含均匀回落）",
    VARIANT_PRODUCTION_BAND: "生产点阵带（predict_wave_band 的 P25~P75 + 距离衰减）",
    VARIANT_DENSITY_BAND: "生产点阵带 × 近窗差值密度（带内权重再乘出现密度）",
    VARIANT_HALFWIDTH_BAND: "中位数 ± 半宽 h（固定半宽，不看分位）",
    VARIANT_VOLATILITY_BAND: "均值 ± k×标准差（滚动波动带）",
}

# 变体轴
WAVE_WINDOWS: tuple[int, ...] = (10, 20, 30)
WAVE_BIAS_AXIS: tuple[str, ...] = tuple(TREND_BIASES)  # neutral / hot / cold / mid
WAVE_HALFWIDTHS: tuple[float, ...] = (3.0, 5.0, 8.0)
WAVE_VOLATILITY_SCALES: tuple[float, ...] = (0.5, 1.0)
# 保守核（次级敏感性分析用）：只留「结构定义 × 窗口」，不加偏好 / 参数轴
WAVE_CORE_DEFINITIONS: tuple[str, ...] = (
    VARIANT_ENGINE_COMPONENT,
    VARIANT_PRODUCTION_BAND,
    VARIANT_DENSITY_BAND,
)

BIAS_LABELS: dict[str, str] = {
    TREND_BIAS_NEUTRAL: "不加权（仅波段 + 差值）",
    TREND_BIAS_HOT: "同波段内偏热（近窗次数降序）",
    TREND_BIAS_COLD: "同波段内偏冷（近窗次数升序）",
    TREND_BIAS_MID: "同波段内偏中频（|次数 − 均值| 升序）",
}

# 预先登记的**唯一**假设 + 预先登记的**主变体**
PRIMARY_VARIANT_ID = "engine_component|w30|neutral"
PREREGISTERED_HYPOTHESIS = (
    "波动 / 点阵（wave / lattice）家族在 10/49 基线之上带有**正**提升："
    "按波动带选出的 top-10 的逐期命中率高于 10/49。"
)
PREREGISTERED_DIRECTION = "greater"

# 判定码（英文；中文只进 label）
VERDICT_SURVIVES = "survives"
VERDICT_NO_SIGNAL = "no_signal"
VERDICT_INSUFFICIENT = "insufficient_data"
VERDICT_LABELS: dict[str, str] = {
    VERDICT_SURVIVES: "校正 / max 零分布下仍显著（需独立样本复核）",
    VERDICT_NO_SIGNAL: "落在抽样噪声内（不能证明优于随机）",
    VERDICT_INSUFFICIENT: "数据不足",
}

# 引擎特征消融与点阵角色的机制说明（代码位置随仓库源码，这里只做引用）
#
# 2026-10-09 变更（「降权去除 + 点阵分布化 + 期号种子随机」）后，点阵**不再是硬门控**：
# 它只定义 1..49 上的抽样概率分布；取号形状由 balanced 配额决定，具体号码由期号种子
# PRNG 抽出。因此下面的引用描述的是**新机制**，旧门控路径（wave_pass_order 以
# lattice_primary 开头）已从 recommend 中移除。
LATTICE_CODE_PATH: dict[str, str] = {
    "band": "services/lottery.py:993-1039  predict_wave_band：近窗相邻差值的 P25/P50/P75 → "
            "预测波动带（样本内经验分布，不是真实概率）",
    "lattice_weight": "services/lottery.py:1041-1055  lattice_weight：带内 1.0、带外 1/(1+距离/6)"
                      "（**只当抽样概率**，不再当排序键）",
    "primary_wave": "services/lottery.py:1057-1079  lattice_primary_wave = 预测带中心所在的波动桶"
                    "（**仅展示**：前端徽章 / 文案，不参与取号）",
    "pass_order": "services/lottery.py:2687-2758  取号分派（**旧门控已移除**）：不再有以 lattice_primary "
                  "开头的 wave_pass_order；balanced 走配额内抽样（2699-2743 期号种子随机 / "
                  "2740-2758 确定性名次），drain 走旧逐桶取满",
    "round_robin": "services/lottery.py:2740-2758  ranked 口径：按配额逐桶轮转，桶内取「离已选号码最远」的号",
    "drain": "services/lottery.py:2759-2772  旧 drain 口径：固定按 WAVE_ORDER 逐桶取满（仅历史审计复现）",
    "neutral_take": "services/lottery.py:2624-2628  neutral 直接取 ordered_pools[wave][0]",
    "role_take": "services/lottery.py:2634-2641  非 neutral 走 take_from_role_band（同桶内角色频次带）",
    "spread_take": "services/lottery.py:2643-2685  _take_spread_one（ranked 专用：取最分散的号）",
    "sort_prefix": "services/lottery.py:1330-1346   _trend_sort_prefix **已移除** lattice / penalty 前缀"
                   "（只剩走势偏好）；services/lottery.py:1348-1405 order_pool 排序键 = 避冷 → 走势 → 遗漏 → 回落",
    "band_rank": "services/lottery.py:1167-1186   _band_rank_prefix **已移除**点阵前缀（只按走势偏好）；"
                 "services/lottery.py:1188-1282 split_pool_into_role_bands 据此切主推 / 次选 / 防守三段",
    "sampling": "services/lottery.py:1484-1540  sampling_seed_key：期号 + 结构设置白名单 → SHA-256 种子 →"
                " random.Random；services/lottery.py:1572-1597 bucket_sampling_weight = 点阵 × 避冷 × 走势；"
                "services/lottery.py:1599-1636 weighted_sample_distinct（不放回轮盘赌）",
    "quotas": "services/lottery.py:1451-1482  balanced_wave_quotas 按非空桶均分注数（最大余额法，10 注 → 4/3/3）",
    "pool": "services/lottery.py:791-820  build_candidate_pools 只按重号 / 重肖剔除，"
            "normal_max 仅决定 classify_wave 的分桶（不缩小并集）",
    "classify": "services/lottery.py:528-536  classify_wave(diff, small_max, normal_max)",
    "soft_flags": "services/lottery.py:916-947  soft_flags 只产出信息标签；"
                  "services/lottery.py:949-977 soft_penalty_weight 恒返回 1.0（权重参数被忽略）；"
                  "services/lottery.py:1108-1146 apply_soft_weights 保留为纯工具（线上引擎不再调用）",
    "ticket_mirror": "services/pick_ticket.py:356-390  出票单镜像同一排序口径"
                     "（点阵不再参与排序；不在回测路径内）",
}


# --------------------------------------------------------------------------- #
# 预登记与变体网格
# --------------------------------------------------------------------------- #
def _variant_id(
    definition: str, window: int, bias: str, param: float | None = None
) -> str:
    """变体英文 id（确定性、可排序；参数用 ``%g`` 格式化）。"""
    base = f"{definition}|w{int(window)}|{bias}"
    return base if param is None else f"{base}|p{float(param):g}"


def _variant(
    definition: str, window: int, bias: str, param: float | None = None
) -> dict[str, Any]:
    return {
        "variant_id": _variant_id(definition, window, bias, param),
        "definition": definition,
        "definition_label": WAVE_DEFINITION_LABELS[definition],
        "window": int(window),
        "bias": bias,
        "bias_label": BIAS_LABELS.get(bias, bias),
        "param": None if param is None else float(param),
        "is_primary": _variant_id(definition, window, bias, param) == PRIMARY_VARIANT_ID,
    }


def variant_grid() -> list[dict[str, Any]]:
    """**预先声明**的波动变体家族（一次性全部汇报，不做二次挑选）。

    家族 = 波段定义 × 走势窗口 × 走势偏好 × 定义特有参数：

    - ``engine_component``  × 窗口 3（偏好固定 neutral；该组件本身不含偏好）
    - ``production_band``   × 窗口 3 × 偏好 4
    - ``density_band``      × 窗口 3 × 偏好 4
    - ``halfwidth_band``    × 窗口 3 × 偏好 4 × 半宽 3
    - ``volatility_band``   × 窗口 3 × 偏好 4 × k 2
    """
    rows: list[dict[str, Any]] = []
    for window in WAVE_WINDOWS:
        rows.append(_variant(VARIANT_ENGINE_COMPONENT, window, TREND_BIAS_NEUTRAL))
    for window in WAVE_WINDOWS:
        for bias in WAVE_BIAS_AXIS:
            rows.append(_variant(VARIANT_PRODUCTION_BAND, window, bias))
            rows.append(_variant(VARIANT_DENSITY_BAND, window, bias))
            for half in WAVE_HALFWIDTHS:
                rows.append(_variant(VARIANT_HALFWIDTH_BAND, window, bias, half))
            for scale in WAVE_VOLATILITY_SCALES:
                rows.append(_variant(VARIANT_VOLATILITY_BAND, window, bias, scale))
    return rows


def variant_family_size() -> int:
    return len(variant_grid())


def variant_model_key(variant: Mapping[str, Any]) -> str:
    """变体的**概率模型**身份（忽略偏好轴 / 平手打破）。

    ``bias`` 在本模块里**只**进入 ``_pure_ranking`` 的次级排序键，不进 ``wave_scores``，
    因此同窗口下四种偏好得到的是**逐位相同**的概率向量，只有并列时的次序不同。
    另外 ``density_band`` 与 ``engine_component`` 的公式相同
    （都是 ``lattice_weight × 近窗差值密度``；softmax 抵消了 ``_centered_log`` 的平移），
    所以两者的概率向量也逐位相同。
    """
    definition = str(variant["definition"])
    if definition == VARIANT_DENSITY_BAND:
        definition = VARIANT_ENGINE_COMPONENT
    parts = [definition, f"w{int(variant['window'])}"]
    if variant.get("param") is not None:
        parts.append(f"p{float(variant['param']):g}")
    return "|".join(parts)


def distinct_probability_models() -> dict[str, Any]:
    """结构性清点：87 行变体实际对应多少个**不同**的概率模型。"""
    grid = variant_grid()
    groups: dict[str, list[str]] = {}
    for row in grid:
        groups.setdefault(variant_model_key(row), []).append(str(row["variant_id"]))
    return {
        "row_count": len(grid),
        "distinct_probability_models": len(groups),
        "bias_is_tiebreak_only": True,
        "duplicate_formula_note": (
            "density_band 与 engine_component 的概率向量逐位相同"
            "（同一公式 lattice_weight × 近窗差值密度；softmax 抵消居中对数平移）。"
        ),
        "groups": {
            key: {"rows": members, "size": len(members)}
            for key, members in sorted(groups.items())
        },
    }


def model_family_note() -> str:
    """把「87 行 ≠ 87 个模型」说清楚（防止把重复计数的行当成独立证据）。"""
    block = distinct_probability_models()
    return (
        f"预登记家族是 {block['row_count']} 行变体，但**偏好轴只影响并列时的次序**，"
        f"且 density_band 与 engine_component 是同一公式，因此实际不同的概率模型只有 "
        f"{block['distinct_probability_models']} 个。max 零分布用的是「行」的网格（更宽的集合），"
        "所以更宽只会更保守，不会把 p 值做小；但报告里不能把这 87 行当成 87 份独立证据 —— "
        "四把尺子中 log-loss / Brier 只在**不同模型**之间才有区别。"
    )


def core_variant_ids() -> list[str]:
    """保守核子家族 id（次级敏感性分析）：引擎组件 + 生产带 × 窗口，偏好 neutral。"""
    return [
        _variant_id(definition, window, TREND_BIAS_NEUTRAL)
        for window in WAVE_WINDOWS
        for definition in WAVE_CORE_DEFINITIONS
    ]


def preregistration_block() -> dict[str, Any]:
    """预先登记：唯一假设、方向、家族大小、主变体、评估窗与种子。"""
    grid = variant_grid()
    return {
        "hypothesis": PREREGISTERED_HYPOTHESIS,
        "direction": PREREGISTERED_DIRECTION,
        "family_size": len(grid),
        "family_axes": {
            "definition": list(WAVE_DEFINITIONS),
            "window": list(WAVE_WINDOWS),
            "bias": list(WAVE_BIAS_AXIS),
            "halfwidth": list(WAVE_HALFWIDTHS),
            "volatility_scale": list(WAVE_VOLATILITY_SCALES),
        },
        "core_subfamily_ids": core_variant_ids(),
        "core_subfamily_size": len(core_variant_ids()),
        "primary_variant": PRIMARY_VARIANT_ID,
        "primary_variant_label": WAVE_DEFINITION_LABELS[VARIANT_ENGINE_COMPONENT],
        "warmup": WAVE_WARMUP,
        "k": WAVE_K,
        "baseline_rate": WAVE_BASELINE_RATE,
        "alpha": WAVE_ALPHA,
        "seed_base": WAVE_SEED_BASE,
        "band_thresholds": {
            "small_max": WAVE_SMALL_MAX,
            "normal_max": WAVE_NORMAL_MAX,
            "note": "波段阈值固定为审计快照（10/30），不作为变体轴。",
        },
        "declared_before_looking": True,
        "legitimate_test": legitimate_test_note(),
    }


def legitimate_test_note() -> str:
    """说明哪一个是合法检验（单变体 p 不合法，max 零分布才是）。"""
    return (
        "合法检验 = 同一变体网格上的 max 零分布：每次置换都在**同一网格**上取最大值，"
        "再看观测 max 落在该零分布的哪里。单变体的原始 p 只在「事先指定且只测这一条」"
        "时才合法；我们看了整张网格再挑最高的那条，就必须用 max 零分布。"
        "Holm 校正是另一个（更保守的）合法尺度，两者都汇报。"
    )


def reference_block() -> dict[str, float]:
    """均匀参考值（与仓库其它分析同口径，复用 max_fit 的常量）。"""
    return {
        "hit_rate": WAVE_BASELINE_RATE,
        "mean_rank": UNIFORM_MEAN_RANK,
        "log_loss": UNIFORM_LOG_LOSS,
        "brier": UNIFORM_BRIER,
    }


# --------------------------------------------------------------------------- #
# 单期：波段 → 打分 → 排名 / 概率（严格 walk-forward）
# --------------------------------------------------------------------------- #
def window_diffs(numbers: Sequence[int], position: int, window: int) -> list[int]:
    """最近 ``window`` 期内的相邻差值绝对值（只看 ``numbers[:position]``）。"""
    history = [int(value) for value in numbers[: int(position)]]
    limit = int(window or 0)
    if limit > 0:
        history = history[-limit:]
    return [abs(history[index] - history[index - 1]) for index in range(1, len(history))]


def wave_band(
    numbers: Sequence[int],
    position: int,
    *,
    definition: str,
    window: int,
    param: float | None = None,
) -> dict[str, Any] | None:
    """按变体定义算出一条波动带 ``{low, high, center, ...}``（数据不足返回 ``None``）。"""
    history_asc = [int(value) for value in numbers[: int(position)]]
    if len(history_asc) < 2:
        return None
    latest_first = list(reversed(history_asc))
    if definition in (VARIANT_PRODUCTION_BAND, VARIANT_DENSITY_BAND, VARIANT_ENGINE_COMPONENT):
        # 复用生产函数：P25~P75 线性插值分位，与 services.lottery 逐位一致
        band = predict_wave_band(
            latest_first,
            window=int(window),
            small_max=WAVE_SMALL_MAX,
            normal_max=WAVE_NORMAL_MAX,
        )
        return band
    diffs = window_diffs(numbers, position, window)
    if not diffs:
        return None
    if definition == VARIANT_HALFWIDTH_BAND:
        half = float(param if param is not None else WAVE_HALFWIDTHS[0])
        center = float(statistics.median(diffs))
        low, high = center - half, center + half
    elif definition == VARIANT_VOLATILITY_BAND:
        scale = float(param if param is not None else WAVE_VOLATILITY_SCALES[0])
        center = float(statistics.fmean(diffs))
        spread = float(statistics.pstdev(diffs)) if len(diffs) > 1 else 0.0
        low, high = center - scale * spread, center + scale * spread
    else:
        raise ValueError(f"未知波动定义：{definition}")
    return {
        "window": int(window),
        "used_window": len(diffs) + 1,
        "samples": len(diffs),
        "center": round(center, 2),
        "low": round(low, 2),
        "high": round(high, 2),
        "definition": definition,
    }


def _bias_key(
    counts: Counter[int], number: int, bias: str, mid_target: float
) -> float:
    """同波段内的走势偏好次级排序键（越小越优先被取到）。"""
    count = float(counts.get(int(number), 0))
    if bias == TREND_BIAS_HOT:
        return -count
    if bias == TREND_BIAS_COLD:
        return count
    if bias == TREND_BIAS_MID:
        return abs(count - float(mid_target))
    return 0.0


def wave_scores(
    numbers: Sequence[int], position: int, variant: Mapping[str, Any]
) -> tuple[dict[int, float], dict[str, Any] | None, Counter[int], float]:
    """1..49 的「波动分」（越大越优）+ 波段 + 近窗频次 + 中频目标。

    只使用 ``numbers[:position]``；不含任何软降权（重号 / 同肖 / 冷号）与避冷加权，
    因此它衡量的是**纯波动**信息，而不是生产引擎的复合排序。
    """
    definition = str(variant["definition"])
    window = int(variant["window"])
    band = wave_band(
        numbers, position, definition=definition, window=window, param=variant.get("param")
    )
    history_asc = [int(value) for value in numbers[: int(position)]]
    latest = history_asc[-1]
    counts, _, _ = recent_number_frequency(list(reversed(history_asc)), window)
    mid_target = (
        sum(counts.values()) / max(1, len(set(counts))) if counts else 0.0
    )
    diffs = window_diffs(numbers, position, window)
    if definition == VARIANT_DENSITY_BAND and band is not None:
        diff_counts = Counter(diffs)
        total = len(diffs) + 1
        scores = {
            number: lattice_weight(abs(number - latest), band)
            * ((diff_counts.get(abs(number - latest), 0) + 1) / total)
            for number in NUMBERS
        }
    else:
        scores = {
            number: lattice_weight(abs(number - latest), band) for number in NUMBERS
        }
    return scores, band, counts, mid_target


def _pure_ranking(
    numbers: Sequence[int], position: int, variant: Mapping[str, Any]
) -> list[int]:
    """纯波动排序（含偏好的次级键；点阵权重相同时按 偏好 → 差值 → 号码 定序）。"""
    scores, _band, counts, mid_target = wave_scores(numbers, position, variant)
    history_asc = [int(value) for value in numbers[: int(position)]]
    latest = history_asc[-1]
    bias = str(variant["bias"])
    return sorted(
        NUMBERS,
        key=lambda number: (
            -round(float(scores[number]), 12),
            _bias_key(counts, number, bias, mid_target),
            abs(int(number) - latest),
            int(number),
        ),
    )


def _engine_component_ranking_and_probabilities(
    numbers: Sequence[int], position: int, variant: Mapping[str, Any]
) -> tuple[list[int], dict[int, float]]:
    """生产引擎 WAVE 组件（复用 dist_engine，权重 one-hot = 1.0）。"""
    params = {
        "lattice_window": int(variant["window"]),
        "small_max": WAVE_SMALL_MAX,
        "normal_max": WAVE_NORMAL_MAX,
    }
    logweights = dist_engine.component_logweights_at(
        numbers, int(position), (dist_engine.COMPONENT_WAVE,), params
    )
    probabilities = dist_engine.pool_probabilities(
        logweights, {dist_engine.COMPONENT_WAVE: 1.0}
    )
    ranking = dist_engine.order_from_probabilities(probabilities, int(position))
    return ranking, probabilities


def wave_step(
    numbers: Sequence[int], position: int, variant: Mapping[str, Any], *, k: int = WAVE_K
) -> dict[str, Any]:
    """单期一步：返回 picks / 完整排序 / 概率（只读 ``numbers[:position]``）。"""
    index = int(position)
    if index < 1 or index >= len(numbers):
        raise ValueError(f"position 越界：{index}（序列长度 {len(numbers)}）")
    if variant["definition"] == VARIANT_ENGINE_COMPONENT:
        ranking, probabilities = _engine_component_ranking_and_probabilities(
            numbers, index, variant
        )
    else:
        ranking = _pure_ranking(numbers, index, variant)
        scores, _band, _counts, _mid = wave_scores(numbers, index, variant)
        logweights = {
            str(variant["variant_id"]): {
                number: math.log(float(scores[number]) + 1e-12) for number in NUMBERS
            }
        }
        probabilities = dist_engine.pool_probabilities(logweights, {
            str(variant["variant_id"]): 1.0
        })
    return {
        "variant_id": variant["variant_id"],
        "position": index,
        "visible_length": index,
        "picks": ranking[: max(1, int(k))],
        "ranking": ranking,
        "probabilities": probabilities,
    }


def _ranking_only(
    numbers: Sequence[int], position: int, variant: Mapping[str, Any]
) -> list[int]:
    """只要排序的轻量路径（置换循环用）。"""
    if variant["definition"] == VARIANT_ENGINE_COMPONENT:
        return dist_engine.component_topk(
            numbers,
            int(position),
            dist_engine.COMPONENT_WAVE,
            k=NUM_STATES,
            params={
                "lattice_window": int(variant["window"]),
                "small_max": WAVE_SMALL_MAX,
                "normal_max": WAVE_NORMAL_MAX,
            },
        )
    return _pure_ranking(numbers, position, variant)


# --------------------------------------------------------------------------- #
# 变体评估（严格 walk-forward）
# --------------------------------------------------------------------------- #
def variant_walk_forward(
    numbers: Sequence[int],
    variant: Mapping[str, Any],
    *,
    warmup: int = WAVE_WARMUP,
    k: int = WAVE_K,
) -> dict[str, Any]:
    """单个变体的逐期 walk-forward 命中与四个尺子（命中率 / 平均排名 / log-loss / Brier）。"""
    series = [int(value) for value in numbers]
    start = max(1, int(warmup))
    ranks: list[int] = []
    hits_series: list[bool] = []
    loss_sum = 0.0
    brier_sum = 0.0
    positions: list[int] = []
    for position in range(start, len(series)):
        step = wave_step(series, position, variant, k=k)
        actual = series[position]
        rank = step["ranking"].index(actual) + 1
        probabilities = step["probabilities"]
        ranks.append(rank)
        hits_series.append(rank <= int(k))
        loss_sum += log_loss(probabilities, actual)
        brier_sum += brier_score(probabilities, actual)
        positions.append(position)
    evaluated = len(hits_series)
    hits = sum(1 for value in hits_series if value)
    hit_rate = (hits / evaluated) if evaluated else None
    available = evaluated > 0
    return {
        "variant_id": variant["variant_id"],
        "definition": variant["definition"],
        "definition_label": variant["definition_label"],
        "window": variant["window"],
        "bias": variant["bias"],
        "param": variant["param"],
        "is_primary": bool(variant.get("is_primary")),
        "evaluated": evaluated,
        "hits": hits,
        "hit_rate": hit_rate,
        "lift": (hit_rate - WAVE_BASELINE_RATE) if hit_rate is not None else None,
        "mean_rank": mean_rank(ranks),
        "log_loss": (loss_sum / evaluated) if available else None,
        "brier": (brier_sum / evaluated) if available else None,
        "p_binomial": (
            binomial_tail_p(hits, evaluated, WAVE_BASELINE_RATE, alternative="greater")
            if available
            else None
        ),
        "positions": positions,
        "ranks": ranks,
        "hits_series": hits_series,
        "reference": reference_block(),
        "data_status": DATA_STATUS_OK if available else DATA_STATUS_INSUFFICIENT,
        "data_status_label": DATA_STATUS_LABELS[
            DATA_STATUS_OK if available else DATA_STATUS_INSUFFICIENT
        ],
    }


def variant_hits(
    numbers: Sequence[int],
    variant: Mapping[str, Any],
    *,
    warmup: int = WAVE_WARMUP,
    k: int = WAVE_K,
) -> int:
    """只要命中数（置换循环用，避免算概率）。"""
    series = [int(value) for value in numbers]
    total = 0
    for position in range(max(1, int(warmup)), len(series)):
        ranking = _ranking_only(series, position, variant)
        if series[position] in set(ranking[: max(1, int(k))]):
            total += 1
    return total


def grid_walk_forward(
    numbers: Sequence[int],
    variants: Sequence[Mapping[str, Any]] | None = None,
    *,
    warmup: int = WAVE_WARMUP,
    k: int = WAVE_K,
) -> dict[str, dict[str, Any]]:
    """整张网格的 walk-forward 结果（id → block）。"""
    rows = list(variants if variants is not None else variant_grid())
    return {
        str(variant["variant_id"]): variant_walk_forward(
            numbers, variant, warmup=warmup, k=k
        )
        for variant in rows
    }


def grid_hits(
    numbers: Sequence[int],
    variants: Sequence[Mapping[str, Any]] | None = None,
    *,
    warmup: int = WAVE_WARMUP,
    k: int = WAVE_K,
) -> dict[str, int]:
    rows = list(variants if variants is not None else variant_grid())
    return {
        str(variant["variant_id"]): variant_hits(numbers, variant, warmup=warmup, k=k)
        for variant in rows
    }


# --------------------------------------------------------------------------- #
# 置换：同一网格上的 max 零分布
# --------------------------------------------------------------------------- #
def shuffled_values(numbers: Sequence[int], seed: int) -> list[int]:
    """把特码的出现位置洗牌（多重集合不变、时间结构摧毁）；确定性 seed。"""
    values = [int(value) for value in numbers]
    random.Random(int(seed)).shuffle(values)
    return values


def permutation_matrix(
    numbers: Sequence[int],
    variants: Sequence[Mapping[str, Any]] | None = None,
    *,
    perms: int = WAVE_PERMS_DEFAULT,
    seed_base: int = WAVE_SEED_BASE,
    warmup: int = WAVE_WARMUP,
    k: int = WAVE_K,
) -> dict[str, Any]:
    """观测网格命中 + ``perms`` 次置换的网格命中矩阵（单进程；大规模请走 CLI 多进程）。"""
    rows = list(variants if variants is not None else variant_grid())
    observed = grid_hits(numbers, rows, warmup=warmup, k=k)
    null: list[dict[str, int]] = []
    for index in range(max(0, int(perms))):
        shuffled = shuffled_values(numbers, int(seed_base) + index)
        null.append(grid_hits(shuffled, rows, warmup=warmup, k=k))
    return {
        "observed": observed,
        "null": null,
        "perms": max(0, int(perms)),
        "seed_base": int(seed_base),
        "warmup": int(warmup),
        "k": int(k),
        "variant_ids": [str(row["variant_id"]) for row in rows],
    }


def per_variant_pvalues(
    observed: Mapping[str, int], null: Sequence[Mapping[str, int]]
) -> dict[str, float | None]:
    """逐变体置换 p（同一网格的置换矩阵里顺带得到，不需要额外置换）。"""
    output: dict[str, float | None] = {}
    for key, value in observed.items():
        samples = [int(row[key]) for row in null if key in row]
        if not samples:
            output[key] = None
            continue
        exceed = sum(1 for sample in samples if sample >= int(value))
        output[key] = (1 + exceed) / (1 + len(samples))
    return output


def max_over_grid(
    observed: Mapping[str, int],
    null: Sequence[Mapping[str, int]],
    *,
    keys: Sequence[str] | None = None,
) -> dict[str, Any]:
    """「网格最大值」的观测值与零分布（这是唯一合法的选择性推断尺度）。"""
    selected = list(keys if keys is not None else observed.keys())
    if not selected:
        return {
            "family_size": 0,
            "observed_max": None,
            "p_value": None,
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    values = {key: int(observed[key]) for key in selected if key in observed}
    if not values:
        return {
            "family_size": len(selected),
            "observed_max": None,
            "p_value": None,
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    best_key = max(values, key=lambda key: (values[key], key))
    observed_max = values[best_key]
    null_maxima = [
        max(int(row[key]) for key in selected if key in row)
        for row in null
        if any(key in row for key in selected)
    ]
    if not null_maxima:
        return {
            "family_size": len(selected),
            "observed_max": observed_max,
            "best_variant": best_key,
            "p_value": None,
            "null_max": {"n": 0},
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    ordered = sorted(null_maxima)
    mean = sum(ordered) / len(ordered)
    sd = math.sqrt(sum((value - mean) ** 2 for value in ordered) / len(ordered))
    exceed = sum(1 for value in ordered if value >= observed_max)
    return {
        "family_size": len(selected),
        "observed_max": observed_max,
        "best_variant": best_key,
        "best_variant_label": WAVE_DEFINITION_LABELS.get(
            str(best_key).split("|")[0], ""
        ),
        "p_value": (1 + exceed) / (1 + len(ordered)),
        "permitted_alpha_floor": 1.0 / (1 + len(ordered)),
        "null_max": {
            "n": len(ordered),
            "mean": mean,
            "sd": sd,
            "p50": ordered[len(ordered) // 2],
            "p95": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))],
            "max": ordered[-1],
            "values": null_maxima,
        },
        "data_status": DATA_STATUS_OK,
        "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
    }


def holm_family(
    observed: Mapping[str, int],
    null: Sequence[Mapping[str, int]],
    *,
    alpha: float = WAVE_ALPHA,
) -> dict[str, Any]:
    """逐变体置换 p + **复用** ``analytics.holm_adjusted_p`` 的 Holm 校正表。"""
    pvalues = per_variant_pvalues(observed, null)
    keys = [key for key in observed if pvalues.get(key) is not None]
    correction = holm_adjusted_p(
        [float(pvalues[key]) for key in keys], alpha=float(alpha)
    )
    adjusted = correction["adjusted"]
    rows: list[dict[str, Any]] = []
    cursor = 0
    for key in observed:
        p_value = pvalues.get(key)
        if p_value is None:
            rows.append(
                {
                    "variant_id": key,
                    "hits": observed[key],
                    "p_value": None,
                    "p_adjusted": None,
                    "verdict": VERDICT_INSUFFICIENT,
                    "verdict_label": VERDICT_LABELS[VERDICT_INSUFFICIENT],
                }
            )
            continue
        p_adjusted = adjusted[cursor]
        cursor += 1
        verdict = (
            VERDICT_SURVIVES if p_adjusted <= float(alpha) else VERDICT_NO_SIGNAL
        )
        rows.append(
            {
                "variant_id": key,
                "hits": observed[key],
                "p_value": p_value,
                "p_adjusted": p_adjusted,
                "verdict": verdict,
                "verdict_label": VERDICT_LABELS[verdict],
            }
        )
    survivors = [row["variant_id"] for row in rows if row["verdict"] == VERDICT_SURVIVES]
    return {
        "family_size": len(observed),
        "tested_size": len(keys),
        "alpha": float(alpha),
        "correction": "holm",
        "bonferroni_threshold": correction["bonferroni_threshold"],
        "min_p_raw": min((float(pvalues[key]) for key in keys), default=None),
        "min_p_adjusted": min(adjusted, default=None),
        "survivors": survivors,
        "survivor_count": len(survivors),
        "rows": rows,
        "data_status": DATA_STATUS_OK if keys else DATA_STATUS_INSUFFICIENT,
        "data_status_label": DATA_STATUS_LABELS[
            DATA_STATUS_OK if keys else DATA_STATUS_INSUFFICIENT
        ],
        "reused_function": "services.analytics.holm_adjusted_p",
    }

# --------------------------------------------------------------------------- #
# 稳健性：对半 / 三分 / 滚动窗
# --------------------------------------------------------------------------- #
def _segment(hits: Sequence[bool], baseline_rate: float) -> dict[str, Any]:
    evaluated = len(hits)
    count = sum(1 for value in hits if value)
    rate = (count / evaluated) if evaluated else None
    return {
        "hits": count,
        "evaluated": evaluated,
        "hit_rate": rate,
        "lift": (rate - baseline_rate) if rate is not None else None,
        "p_binomial": (
            binomial_tail_p(
                count, evaluated, baseline_rate, alternative="greater"
            )
            if evaluated
            else None
        ),
    }


def stability_block(
    hits_series: Sequence[bool],
    *,
    baseline_rate: float = WAVE_BASELINE_RATE,
    rolling: int = WAVE_ROLLING_BLOCK,
) -> dict[str, Any]:
    """对半 / 三分 / 滚动窗的命中与提升，外加符号一致性。

    真实信号应当**在各子区间都同号**；只集中在一个子区间的偏离按噪声处理。
    """
    hits = [bool(value) for value in hits_series]
    total = len(hits)
    if total == 0:
        return {
            "evaluated": 0,
            "halves": [],
            "thirds": [],
            "rolling": [],
            "sign_consistency": None,
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    half = total // 2
    boundaries = {
        "halves": [(0, half), (half, total)],
        "thirds": [
            (0, total // 3),
            (total // 3, (2 * total) // 3),
            ((2 * total) // 3, total),
        ],
    }
    blocks: dict[str, list[dict[str, Any]]] = {}
    for name, spans in boundaries.items():
        rows = []
        for index, (start, stop) in enumerate(spans):
            row = {"index": index, "start": start, "stop": stop}
            row.update(_segment(hits[start:stop], baseline_rate))
            rows.append(row)
        blocks[name] = rows
    span = max(1, int(rolling))
    rolling_rows = []
    for start in range(0, total, span):
        stop = min(total, start + span)
        row = {"index": len(rolling_rows), "start": start, "stop": stop}
        row.update(_segment(hits[start:stop], baseline_rate))
        rolling_rows.append(row)
    positive = sum(
        1
        for row in blocks["halves"] + blocks["thirds"]
        if row["lift"] is not None and row["lift"] > 0
    )
    counted = sum(
        1
        for row in blocks["halves"] + blocks["thirds"]
        if row["lift"] is not None
    )
    return {
        "evaluated": total,
        "baseline_rate": baseline_rate,
        "halves": blocks["halves"],
        "thirds": blocks["thirds"],
        "rolling_window": span,
        "rolling": rolling_rows,
        "sign_consistency": (positive / counted) if counted else None,
        "sign_consistency_note": (
            "符号一致性 = 「对半 + 三分」各段提升为正的比例；真实信号应当接近 1，"
            "集中在单一子区间的偏离按噪声处理。"
        ),
        "data_status": DATA_STATUS_OK,
        "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
    }


# --------------------------------------------------------------------------- #
# 功效 / 需要多少期才能定论
# --------------------------------------------------------------------------- #
def power_and_verdict(
    evaluated: int,
    hits: int,
    *,
    baseline_rate: float = WAVE_BASELINE_RATE,
    odds: float = WAVE_ODDS,
    extra_per_day: float = 1.0,
) -> dict[str, Any]:
    """目标 Δ 的 80% 功效期数（算术全部展示）+ 追加期数 / 月数 + 噪声下的回归预测。

    所需期数用 ``services.dist_engine.power_block``（两比例公式）算，不重写公式。
    """
    count = int(evaluated)
    observed_hits = int(hits)
    if count <= 0:
        return {
            "evaluated": count,
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    break_even_edge = (K_DEFAULT / float(odds)) - baseline_rate
    claimed_rate = observed_hits / count
    claimed_delta = claimed_rate - baseline_rate
    deltas = [claimed_delta, 0.05, break_even_edge]
    power = dist_engine.power_block(
        count, baseline_rate=baseline_rate, deltas=deltas, alpha=WAVE_ALPHA, power=0.8
    )
    mdd = minimum_detectable_delta(count, baseline_rate, alpha=WAVE_ALPHA, power=0.8)
    targets: list[dict[str, Any]] = []
    for target in power.get("targets", []):
        required = int(target["required_draws_80_power"])
        additional = max(0, required - count)
        targets.append(
            {
                **target,
                "additional_draws_needed": additional,
                "months_at_one_draw_per_day": (
                    additional / (30.4375 * float(extra_per_day))
                    if extra_per_day
                    else None
                ),
            }
        )
    return {
        "evaluated": count,
        "hits": observed_hits,
        "observed_rate": claimed_rate,
        "baseline_rate": baseline_rate,
        "observed_delta": claimed_delta,
        "break_even_rate": K_DEFAULT / float(odds),
        "break_even_edge": break_even_edge,
        "alpha": WAVE_ALPHA,
        "power": 0.8,
        "minimum_detectable_delta": mdd.get("minimum_detectable_delta"),
        "two_sigma_delta": mdd.get("two_sigma_delta"),
        "standard_error": mdd.get("standard_error"),
        "targets": targets,
        "extra_draws_per_day": float(extra_per_day),
        "reused_function": "services.dist_engine.power_block",
        "data_status": DATA_STATUS_OK,
        "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
    }


def regression_path(
    evaluated: int,
    hits: int,
    *,
    baseline_rate: float = WAVE_BASELINE_RATE,
    horizons: Sequence[int] = (0, 165, 355, 1000, 5000),
    expected_best_z: float | None = None,
) -> dict[str, Any]:
    """随期数累积的两种预测：观测值被稀释 / 噪声下「网格最好那条」会怎么走。

    - ``cumulative_rate_if_noise``：如果真实率恰好等于基线，那么累计命中率
      ``(hits + M·p0) / (evaluated + M)`` —— 观测的 26.67% 会被逐步稀释回 20.41%；
    - ``expected_best_of_grid_rate``：用 max 零分布给出的 z 尺度
      ``z_max = (mean(null_max) − n·p0) / sqrt(n·p0(1−p0))`` 外推「网格冠军」的期望命中率
      —— 纯噪声下它也应随 1/√n 回落，因此「冠军保持 26.67%」本身就不可期待。
    """
    count = int(evaluated)
    if count <= 0:
        return {
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    p0 = float(baseline_rate)
    rows: list[dict[str, Any]] = []
    for horizon in horizons:
        extra = max(0, int(horizon))
        total = count + extra
        row: dict[str, Any] = {
            "extra_draws": extra,
            "total_evaluated": total,
            "cumulative_rate_if_noise": (int(hits) + extra * p0) / total,
        }
        if expected_best_z is not None:
            row["expected_best_of_grid_rate"] = p0 + float(expected_best_z) * math.sqrt(
                p0 * (1.0 - p0) / total
            )
        rows.append(row)
    return {
        "evaluated": count,
        "hits": int(hits),
        "observed_rate": int(hits) / count,
        "baseline_rate": p0,
        "expected_best_z": expected_best_z,
        "rows": rows,
        "data_status": DATA_STATUS_OK,
        "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_OK],
    }


def null_max_z(evaluated: int, null_max_mean: float, *, baseline_rate: float = WAVE_BASELINE_RATE) -> float | None:
    """把 max 零分布的均值换算成 z 尺度（用于外推「网格冠军」的期望水平）。"""
    count = int(evaluated)
    if count <= 0:
        return None
    p0 = float(baseline_rate)
    sd = math.sqrt(count * p0 * (1.0 - p0))
    if sd <= 0:
        return None
    return (float(null_max_mean) - count * p0) / sd


# --------------------------------------------------------------------------- #
# 生产引擎特征的消融（两种配置并排）+ 机制诊断
# --------------------------------------------------------------------------- #
AUDIT_CONFIG_LABEL = "audit_snapshot"
LIVE_CONFIG_LABEL = "live_server_snapshot"

# 审计快照的四个旋钮（与 dist_audit.default_audit_settings 同源）
AUDIT_SNAPSHOT: dict[str, Any] = {
    "small_max": 10,
    "normal_max": 30,
    "trend_bias": TREND_BIAS_NEUTRAL,
    "exclude_repeat_zodiac": False,
    # 冻结锚点：本快照是**历史审计基线**，不是「当前代码默认」的同义词。
    # 2026-10-07 起 ``wave_alloc`` 默认改为 balanced（均衡分散），会把点阵关闭时的
    # 号码集合整体换掉、命中数随之漂移；为保留审计快照的历史可比性，这里显式钉住旧口径。
    "wave_alloc": WAVE_ALLOC_DRAIN,
}


def audit_config(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """审计快照配置（pick_count = K_DEFAULT、mode = even）。"""
    merged = {**DEFAULT_SETTINGS, "pick_count": K_DEFAULT, "mode": "even"}
    merged.update(AUDIT_SNAPSHOT)
    if overrides:
        merged.update({key: value for key, value in overrides.items() if value is not None})
    return clamp_settings(merged)


def config_from_settings(
    settings: Mapping[str, Any], overrides: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """把一份（GET /api/settings 形态的）设置变成回测基线：注数 / 模式钉死为 k=10 / even。"""
    merged = {**DEFAULT_SETTINGS}
    merged.update(
        {
            key: value
            for key, value in dict(settings or {}).items()
            if key in DEFAULT_SETTINGS and value is not None
        }
    )
    merged["pick_count"] = K_DEFAULT
    merged["mode"] = "even"
    # 线上读到的 mid/hot/cold 是「已按读取口径解析过」的生效值，这里保持生效
    merged["trend_bias_explicit"] = True
    if overrides:
        merged.update({key: value for key, value in overrides.items() if value is not None})
    return clamp_settings(merged)


def _pick_wave_mix(
    rows: Sequence[Mapping[str, Any]], small_max: int, normal_max: int
) -> dict[str, int]:
    """预测号码落在三类波动桶的计数（判断「这一期到底从哪个桶取号」）。"""
    mix = {wave: 0 for wave in ("small", "normal", "big")}
    for row in rows:
        latest = int(row["latest_used"])
        for number in row["predicted"]:
            mix[classify_wave(abs(int(number) - latest), small_max, normal_max)] += 1
    return mix


def _candidate_pool_sizes(
    rows: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any]
) -> dict[str, int]:
    sizes = set()
    for row in rows:
        latest = int(row["latest_used"])
        pool = set(range(NUMBER_MIN, NUMBER_MAX + 1))
        if not cfg.get("include_repeat_number", True):
            pool -= {latest}
        if cfg.get("exclude_repeat_zodiac"):
            pool -= set(zodiac_numbers(latest))
        sizes.add(len(pool))
    return {"min": min(sizes), "max": max(sizes), "distinct": sorted(sizes)}


def _primary_wave_counts(
    numbers: Sequence[int], cfg: Mapping[str, Any], *, window: int
) -> dict[str, int]:
    from services.lottery import lattice_primary_wave

    counts: Counter[str] = Counter()
    for position in range(2, len(numbers)):
        latest_first = list(reversed([int(value) for value in numbers[:position]]))
        band = predict_wave_band(
            latest_first,
            window=int(window),
            small_max=int(cfg["small_max"]),
            normal_max=int(cfg["normal_max"]),
        )
        if band is None:
            counts["none"] += 1
            continue
        counts[str(lattice_primary_wave(
            band, small_max=int(cfg["small_max"]), normal_max=int(cfg["normal_max"])
        ))] += 1
    return dict(counts)


def toggle_ablation(
    draws: Sequence[Mapping[str, Any]],
    cfg: Mapping[str, Any],
    *,
    toggle: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    """对某一组「开 / 关」配置跑两次 walk-forward，给出 Δ 与选号差异诊断。"""
    from services import analytics as analytics

    results: dict[str, dict[str, Any]] = {}
    for side in ("on", "off"):
        config = {**dict(cfg), **dict(toggle[side])}
        results[side] = analytics.backtest_stats(
            draws,
            base_settings=config,
            include_results=True,
            include_wave_breakdown=False,
        )
    on, off = results["on"], results["off"]
    on_sets = [set(row["predicted"]) for row in on["results"]]
    off_sets = [set(row["predicted"]) for row in off["results"]]
    identical = sum(1 for left, right in zip(on_sets, off_sets) if left == right)
    hit_flips = sum(
        1
        for left, right in zip(on["results"], off["results"])
        if left["hit"] != right["hit"]
    )
    return {
        "label": label,
        "handle": toggle["handle"],
        "on": {"hits": on["hits"], "evaluated": on["evaluated"], "hit_rate": on["hit_rate"]},
        "off": {"hits": off["hits"], "evaluated": off["evaluated"], "hit_rate": off["hit_rate"]},
        "delta_hits": int(on["hits"]) - int(off["hits"]),
        "delta_hit_rate": (
            (on["hit_rate"] - off["hit_rate"])
            if on["hit_rate"] is not None and off["hit_rate"] is not None
            else None
        ),
        "settings_on": {**dict(cfg), **dict(toggle["on"])},
        "settings_off": {**dict(cfg), **dict(toggle["off"])},
        "pick_set_identical_positions": identical,
        "positions": len(on_sets),
        "hit_outcome_flips": hit_flips,
        "pick_wave_mix_on": _pick_wave_mix(
            on["results"], int(cfg["small_max"]), int(cfg["normal_max"])
        ),
        "pick_wave_mix_off": _pick_wave_mix(
            off["results"], int(cfg["small_max"]), int(cfg["normal_max"])
        ),
        "candidate_pool_on": _candidate_pool_sizes(on["results"], {**dict(cfg), **dict(toggle["on"])}),
        "candidate_pool_off": _candidate_pool_sizes(
            off["results"], {**dict(cfg), **dict(toggle["off"])}
        ),
        "inert": bool(identical == len(on_sets)),
        "scope": f"本池已导入 {on['evaluated']} 期可评估样本内",
    }


def ablation_side_by_side(
    draws: Sequence[Mapping[str, Any]],
    *,
    audit_settings: Mapping[str, Any] | None = None,
    live_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """两种配置下、两个开关（lattice / exclude_repeat_zodiac）的开 / 关消融。

    - 「lattice」：``lattice_enabled`` True vs False；
    - 「repeat_zodiac」：``exclude_repeat_zodiac`` True vs False；
    - 另附四个生产特征的整表（复用 ``dist_audit.engine_feature_hits``）。
    """
    audit_cfg = dict(audit_settings) if audit_settings else audit_config()
    toggles = {
        "lattice": {
            "handle": "engine_lattice",
            "on": {"lattice_enabled": True},
            "off": {"lattice_enabled": False},
        },
        "repeat_zodiac": {
            "handle": "engine_repeat_zodiac",
            "on": {"exclude_repeat_zodiac": True},
            "off": {"exclude_repeat_zodiac": False},
        },
    }
    configs: dict[str, dict[str, Any]] = {AUDIT_CONFIG_LABEL: audit_cfg}
    if live_settings:
        configs[LIVE_CONFIG_LABEL] = dict(live_settings)
    output: dict[str, Any] = {"configs": {}, "toggles": toggles}
    for label, cfg in configs.items():
        entry: dict[str, Any] = {
            "settings": {
                key: cfg.get(key)
                for key in (
                    "small_max",
                    "normal_max",
                    "trend_bias",
                    "exclude_repeat_zodiac",
                    "lattice_enabled",
                    "lattice_window",
                    "include_repeat_number",
                    "avoid_cold_enabled",
                    "pick_count",
                    "mode",
                )
            },
            "engine_feature_hits": dist_audit.engine_feature_hits(
                draws, base_settings=cfg
            ),
        }
        for name, toggle in toggles.items():
            entry[name] = toggle_ablation(draws, cfg, toggle=toggle, label=label)
        output["configs"][label] = entry
    if live_settings:
        for label, cfg in configs.items():
            output["configs"][label]["lattice_primary_wave_counts"] = dict(
                _primary_wave_counts(
                    [int(row["special_number"]) for row in draws],
                    cfg,
                    window=int(cfg.get("lattice_window", 30)),
                )
            )
    output["code_path"] = dict(LATTICE_CODE_PATH)
    output["mechanism_note"] = mechanism_note()
    return output


def mechanism_note() -> str:
    """机制结论（白话 + 代码位置）：同一个开关在不同配置下幅度为何不同。

    2026-10-09 起点阵**不再是硬门控**：它只定义 1..49 上的抽样概率分布，
    取号形状由 balanced 配额决定、具体号码由期号种子 PRNG 抽出；旧的
    「以 ``lattice_primary`` 开头把该桶取满」路径已从 ``recommend`` 移除。
    """
    return (
        "点阵已从「硬门控」改为「抽样概率分布」：开启时它只决定带内号码的"
        "**抽样概率更高**（services/lottery.py:1484-1540 的 sampling_seed_key → "
        "services/lottery.py:1572-1597 的 bucket_sampling_weight → "
        "services/lottery.py:1599-1636 的 weighted_sample_distinct），不再决定取号顺序；"
        "旧口径里以 ``lattice_primary`` 开头把该桶一次性取满的 wave_pass_order 循环"
        "已从 recommend 移除（services/lottery.py:2687-2758）。因此「开 / 关」比较的是"
        "同一形状下的两条不同抽样概率：开启时带内号码被抽到的频率更高，关闭时桶内概率均匀。"
        "取号形状改由 balanced 配额定型（services/lottery.py:1451-1482 最大余额法均分注数，"
        "10 注 → 4/3/3），ranked 模式下桶内按确定性名次取号"
        "（services/lottery.py:2740-2758），两种模式都不再让点阵独占某一个波动桶。"
        "``lattice_primary`` 现在只作**展示**：告诉前端预测带中心落在哪个桶"
        "（services/lottery.py:1057-1079）。"
        "``trend_bias`` 仍不与点阵耦合：它只决定「同一个桶里谁更容易被抽到」——"
        "neutral 取池首（services/lottery.py:2624-2628），非 neutral 走频次带优先"
        "（services/lottery.py:2634-2641），两者最终都在 bucket_sampling_weight 里按连续"
        "倍数加权（services/lottery.py:1572-1597）。"
        "``normal_max`` 也**不**把号码踢出候选池（services/lottery.py:791-820 只按重号 / 重肖剔除），"
        "它只改分桶归属（services/lottery.py:528-536）与配额分配。"
        "三类软降权（重号 / 同肖 / 冷号）已**去除权重**，只留信息标签"
        "（services/lottery.py:916-947 soft_flags；services/lottery.py:949-977 "
        "soft_penalty_weight 恒返回 1.0）。"
    )


# --------------------------------------------------------------------------- #
# 顶层汇总（供 CLI / canvas 直接消费）
# --------------------------------------------------------------------------- #
def study_summary(
    variants_table: Mapping[str, Mapping[str, Any]],
    observed_hits: Mapping[str, int],
    holm: Mapping[str, Any],
    max_block: Mapping[str, Any],
    *,
    primary_variant_id: str = PRIMARY_VARIANT_ID,
) -> dict[str, Any]:
    """把逐变体表 / Holm / max 零分布压成一句话结论（英文码 + 白话）。"""
    primary = variants_table.get(primary_variant_id) or {}
    primary_row = next(
        (
            row
            for row in holm.get("rows") or []
            if row.get("variant_id") == primary_variant_id
        ),
        {},
    )
    anything = bool(holm.get("survivors")) or (
        max_block.get("p_value") is not None
        and float(max_block["p_value"]) <= WAVE_ALPHA
    )
    return {
        "primary_variant": primary_variant_id,
        "primary_hits": primary.get("hits"),
        "primary_evaluated": primary.get("evaluated"),
        "primary_hit_rate": primary.get("hit_rate"),
        "primary_lift": primary.get("lift"),
        "primary_p_single": primary_row.get("p_value"),
        "primary_p_holm": primary_row.get("p_adjusted"),
        "primary_verdict": primary_row.get("verdict"),
        "family_size": holm.get("family_size"),
        "holm_survivors": list(holm.get("survivors") or []),
        "observed_max": max_block.get("observed_max"),
        "observed_max_variant": max_block.get("best_variant"),
        "max_over_grid_p": max_block.get("p_value"),
        "anything_survives": anything,
        "statement": (
            "波动法家族在本池样本内未通过合法尺度（同网格 max 零分布 / Holm）："
            "看到的最好一条落在「同网格纯噪声也能造出」的范围内。"
            if not anything
            else "有变体通过合法尺度：必须在独立样本上复核，不能直接当成可用优势。"
        ),
        "claim": CLAIM_NO_EDGE,
        "claim_label": CLAIM_LABELS[CLAIM_NO_EDGE],
        "observed_hits_size": len(observed_hits),
    }


def describe_variant_row(
    block: Mapping[str, Any], holm_row: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """单行变体表（变体 / 命中 / 提升 / 原始 p / Holm p / 判定）。"""
    row = holm_row or {}
    return {
        "variant_id": block.get("variant_id"),
        "definition": block.get("definition"),
        "window": block.get("window"),
        "bias": block.get("bias"),
        "param": block.get("param"),
        "is_primary": block.get("is_primary"),
        "hits": block.get("hits"),
        "evaluated": block.get("evaluated"),
        "hit_rate": block.get("hit_rate"),
        "lift": block.get("lift"),
        "mean_rank": block.get("mean_rank"),
        "log_loss": block.get("log_loss"),
        "brier": block.get("brier"),
        "p_value": row.get("p_value"),
        "p_adjusted": row.get("p_adjusted"),
        "verdict": row.get("verdict"),
        "verdict_label": row.get("verdict_label"),
    }


def variant_table(
    variants_table: Mapping[str, Mapping[str, Any]], holm: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows = {row["variant_id"]: row for row in holm.get("rows", [])}
    table = [
        describe_variant_row(block, rows.get(key))
        for key, block in variants_table.items()
    ]
    table.sort(key=lambda row: (-(row.get("hits") or 0), str(row.get("variant_id"))))
    return table


def scope_note(sample_size: int, evaluated: int) -> str:
    return (
        f"本池已导入 {int(sample_size)} 期；波动专项预登记评估窗为其中 "
        f"{int(evaluated)} 期（跳过最早 {int(sample_size) - int(evaluated)} 期前置历史）。"
        "所有结论只针对这个样本，不涉及任何全市场数据。"
    )


def period_span(draws: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    periods = [int(row["period"]) for row in draws if row.get("period") is not None]
    dates = sorted(str(row["draw_date"])[:10] for row in draws if row.get("draw_date"))
    span_days: int | None = None
    if dates:
        span_days = (
            date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])
        ).days
    return {
        "first_period": periods[0] if periods else None,
        "last_period": periods[-1] if periods else None,
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "span_days": span_days,
        "draws_per_day": (
            (len(draws) / span_days) if span_days else None
        ),
    }


def uniformity_of_variant_hits(hits: Sequence[int]) -> dict[str, Any]:
    """观测网格命中的分布摘要（帮助看清「最好那条」是孤峰还是整体抬升）。"""
    values = sorted(int(value) for value in hits)
    if not values:
        return {
            "n": 0,
            "data_status": DATA_STATUS_INSUFFICIENT,
            "data_status_label": DATA_STATUS_LABELS[DATA_STATUS_INSUFFICIENT],
        }
    mean = sum(values) / len(values)
    sd = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
    return {
        "n": len(values),
        "min": values[0],
        "p25": values[len(values) // 4],
        "mean": mean,
        "p50": values[len(values) // 2],
        "p75": values[(3 * len(values)) // 4],
        "max": values[-1],
        "sd": sd,
    }


__all__ = [
    "AUDIT_CONFIG_LABEL",
    "AUDIT_SNAPSHOT",
    "BIAS_LABELS",
    "CLAIM_LABELS",
    "CLAIM_NO_EDGE",
    "LATTICE_CODE_PATH",
    "LIVE_CONFIG_LABEL",
    "PREREGISTERED_DIRECTION",
    "PREREGISTERED_HYPOTHESIS",
    "PRIMARY_VARIANT_ID",
    "VARIANT_DENSITY_BAND",
    "VARIANT_ENGINE_COMPONENT",
    "VARIANT_HALFWIDTH_BAND",
    "VARIANT_PRODUCTION_BAND",
    "VARIANT_VOLATILITY_BAND",
    "VERDICT_INSUFFICIENT",
    "VERDICT_LABELS",
    "VERDICT_NO_SIGNAL",
    "VERDICT_SURVIVES",
    "WAVE_ALPHA",
    "WAVE_BASELINE_RATE",
    "WAVE_BIAS_AXIS",
    "WAVE_BREAK_EVEN_EDGE",
    "WAVE_BREAK_EVEN_RATE",
    "WAVE_CORE_DEFINITIONS",
    "WAVE_DEFINITION_LABELS",
    "WAVE_DEFINITIONS",
    "WAVE_HALFWIDTHS",
    "WAVE_K",
    "WAVE_NORMAL_MAX",
    "WAVE_ODDS",
    "WAVE_PERMS_DEFAULT",
    "WAVE_ROLLING_BLOCK",
    "WAVE_SEED_BASE",
    "WAVE_SMALL_MAX",
    "WAVE_VOLATILITY_SCALES",
    "WAVE_WARMUP",
    "WAVE_WINDOWS",
    "ablation_side_by_side",
    "audit_config",
    "config_from_settings",
    "core_variant_ids",
    "describe_variant_row",
    "distinct_probability_models",
    "grid_hits",
    "grid_walk_forward",
    "holm_family",
    "legitimate_test_note",
    "max_over_grid",
    "mechanism_note",
    "model_family_note",
    "null_max_z",
    "per_variant_pvalues",
    "period_span",
    "permutation_matrix",
    "power_and_verdict",
    "preregistration_block",
    "reference_block",
    "regression_path",
    "scope_note",
    "shuffled_values",
    "stability_block",
    "study_summary",
    "toggle_ablation",
    "uniformity_of_variant_hits",
    "variant_family_size",
    "variant_grid",
    "variant_hits",
    "variant_model_key",
    "variant_table",
    "variant_walk_forward",
    "wave_band",
    "wave_scores",
    "wave_step",
    "window_diffs",
]
