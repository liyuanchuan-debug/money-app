r"""设置敏感度审计：逐项扰动设置，测量它**到底**改变了什么（离线纯函数）。

用途：回答「波浪买入法为什么还是这么低 / 这些旋钮到底有没有用」。

这个脚本做什么
--------------
对 ``GET /api/settings`` 暴露的每一个设置项，取一组「合理的备选值」逐一代入
walk-forward 回测，只统计三件事：

1. **号码集合**：208 个回测期里，有多少期的 10 注号码集合与基准不同；
2. **命中数**：备选值下的命中数与基准差多少；
3. **金额 / 兑付**：各注金额、按赔率折算的兑付是否变化（用于区分
   「只改筹码分配」和「真的改了号码」）。

然后按实测结果分类（英文枚举，落库 / 输出禁止汉字）：

- ``ACTIVE``：改变号码集合或有效注数；
- ``COSMETIC``：号码不变，只改金额 / 兑付（筹码分配、赔率）；
- ``INERT_UNDER_CURRENT_CONFIG``：当前配置下什么都不改，但换一组配置会改
  （报告里指明是哪一组配置、被哪个开关挡住）；
- ``DEAD``：在**所有**被测配置与代码路径里都无法影响输出（例如只读派生字段，
  或根本不在引擎输入里的键）。

口径铁律（与项目规则一致）
--------------------------
- 全部结论只针对 **本池已导入的 N 期样本内**，不得升格为全量 / 市场结论；
- 命中率与随机基线只有「样本内对照」意义；**命中数变化不等于命中概率变化**，
  任何一项权重都无法改变单期命中概率 ``注数 / 49``；
- 期望值恒为 ``赔率 / 49 - 1``，与选号方法、权重、注数、注码无关；
- 输出里不得出现「已优化命中率」「提高中奖率」这类表述。

运行（离线，不碰数据库、不改设置）：

    cd backend
    .\.venv\Scripts\python.exe scripts\settings_sensitivity.py `
        --draws-json data\draws_70_279.json --out ..\.tmp-wave\settings_sensitivity.json

测试可只跑一小段：

    .\.venv\Scripts\python.exe scripts\settings_sensitivity.py `
        --draws-json data\draws_70_279.json --keys lattice_enabled,big_min `
        --configs live,snapshot --no-power
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.analytics import (  # noqa: E402
    MIN_PRIOR_DRAWS,
    backtest_stats,
    binomial_tail_p,
    minimum_detectable_delta,
    normalize_draws,
)
from services.lottery import (  # noqa: E402
    DEFAULT_SETTINGS,
    MODES,
    NUMBER_MAX,
    NUMBER_MIN,
    PICK_SAMPLINGS,
    PICK_SAMPLING_SEEDED_RANDOM,
    PICK_STRATEGIES,
    TREND_BIAS_EXPLICIT_KEY,
    TREND_BIASES,
    WAVE_ALLOC_BALANCED,
    WAVE_ALLOCS,
    clamp_settings,
    resolve_trend_bias,
)

# --------------------------------------------------------------------------- #
# 分类码（英文枚举；汉字只出现在 *label / 文案）
# --------------------------------------------------------------------------- #
CLASS_ACTIVE = "ACTIVE"
CLASS_COSMETIC = "COSMETIC"
CLASS_INERT = "INERT_UNDER_CURRENT_CONFIG"
CLASS_DEAD = "DEAD"
CLASS_LABELS = {
    CLASS_ACTIVE: "有效：扰动即改变号码或有效注数",
    CLASS_COSMETIC: "仅展示/筹码：号码不变，只改金额或兑付",
    CLASS_INERT: "当前配置下失效：被开关挡住，换配置才会生效",
    CLASS_DEAD: "死键：任何配置都无法影响输出",
}

# 审计口径：只统计本池样本内
DEFAULT_DRAWS = str(BACKEND_ROOT / "data" / "draws_70_279.json")


# --------------------------------------------------------------------------- #
# 配置快照（全部值来自 ``GET /api/settings`` 与规则文档，禁止编造）
# --------------------------------------------------------------------------- #
# 现场配置：``GET /api/settings`` 的实测值（含内部门闩 trend_bias_explicit=True，
# 否则 Store.get_settings 会把 mid 回退成 neutral —— 见 lottery.effective_trend_bias）
LIVE_SETTINGS: dict[str, Any] = {
    "small_max": 15,
    "normal_max": 20,
    "total_amount": 50,
    "amount_unit": 5,
    "mode": "even",
    "pick_count": 10,
    "odds": 47.0,
    "exclude_repeat_zodiac": True,
    "include_repeat_number": True,
    # 三项软降权（2026-10-09「降权去除」后**保留键、不再生效**：只留信息标签）
    "repeat_number_weight": 0.5,
    "repeat_zodiac_weight": 0.8,
    "stale_periods": 60,
    "stale_weight": 0.3,
    "lattice_enabled": True,
    "lattice_window": 30,
    "wave_alloc": WAVE_ALLOC_BALANCED,
    "pick_sampling": PICK_SAMPLING_SEEDED_RANDOM,
    "role_w_primary": 3.0,
    "role_w_secondary": 2.0,
    "role_w_defense": 1.0,
    "trend_bias": "mid",
    "trend_window": 20,
    "avoid_cold_enabled": False,
    "avoid_cold_days": 60,
    "pick_strategy": "wave_round",
    "score_w_focus": 1.0,
    "score_w_mid": 2.0,
    "score_w_omit": 0.0,
    "score_w_diff": 0.5,
    # 内部门闩：True 才让上面的 trend_bias=mid 生效（Store 读取口径）
    TREND_BIAS_EXPLICIT_KEY: True,
}


def _settings_snapshot() -> dict[str, Any]:
    """对照快照：与现场配置同源，只改审计要求的四项（用来暴露「配置巧合失效」）。"""
    return {
        **LIVE_SETTINGS,
        "small_max": 10,
        "normal_max": 30,
        "trend_bias": "neutral",
        "exclude_repeat_zodiac": False,
    }


def _settings_naked() -> dict[str, Any]:
    """「裸配置」：关点阵 / 关走势加权 / 不排同肖，用来让候选池开关可见。

    目的是让 ``include_repeat_number`` 在池内真正可见（现场配置里它被
    「排同肖 + 点阵加权」盖住）。

    注意（2026-10-09「降权去除」后）：三项软降权 ``repeat_number_weight`` /
    ``repeat_zodiac_weight`` / ``stale_weight`` 已**完全不再参与选号与金额**，
    因此本配置里把它们设成 1.0 只是历史留痕，不再有任何「解除降权」的作用。
    """
    return {
        **LIVE_SETTINGS,
        "lattice_enabled": False,
        "trend_bias": "neutral",
        "exclude_repeat_zodiac": False,
        "repeat_number_weight": 1.0,
        "repeat_zodiac_weight": 1.0,
        "stale_weight": 1.0,
    }


def _settings_score_top() -> dict[str, Any]:
    """打分 Top-N 配置：让 ``score_w_*`` 四项从「被 gate 掉」变成可达。"""
    return {**LIVE_SETTINGS, "pick_strategy": "score_top"}


def _settings_avoid_cold_on() -> dict[str, Any]:
    """开启避冷加权：让 ``avoid_cold_days`` 从被 gate 掉变成可达。"""
    return {**LIVE_SETTINGS, "avoid_cold_enabled": True}


def _settings_legacy_no_total() -> dict[str, Any]:
    """存量旧行：没有 ``total_amount``，预算只能由 ``bet_unit × pick_count`` 回退。"""
    cfg = {k: v for k, v in LIVE_SETTINGS.items() if k != "total_amount"}
    cfg["bet_unit"] = 5
    return cfg


def _settings_slack_budget() -> dict[str, Any]:
    """预算有余量的配置：用来暴露「角色金额配额」这类只在有闲钱时才生效的键。

    现场 50 元 / 5 元 / 10 注 = 10 个单位，刚好等于每注保底 1 个单位，
    余量为 0 → ``role_w_*`` 无论怎么改都不会改变金额（看起来像 DEAD）。
    把预算提到 100 元（20 个单位）就出现 10 个单位的余量，配额才有意义。
    """
    return {**LIVE_SETTINGS, "total_amount": 100}


AUDIT_CONFIGS: dict[str, dict[str, Any]] = {
    "live": LIVE_SETTINGS,
    "snapshot": _settings_snapshot(),
    "naked": _settings_naked(),
    "score_top": _settings_score_top(),
    "avoid_cold_on": _settings_avoid_cold_on(),
    "legacy_no_total": _settings_legacy_no_total(),
    "slack_budget": _settings_slack_budget(),
}

# --------------------------------------------------------------------------- #
# 每个设置的备选值（「合理扫描」；不含等于现场值的项）
# --------------------------------------------------------------------------- #
SETTING_SWEEPS: dict[str, tuple[Any, ...]] = {
    "small_max": (5, 8, 12, 20, 25),
    "normal_max": (10, 12, 25, 30, 40),
    "total_amount": (5, 25, 45, 75, 100),
    "amount_unit": (10, 25, 100),
    "bet_unit": (1, 3, 10, 20),
    "mode": ("weighted", "random", "single"),
    "pick_count": (5, 6, 8, 9),
    "odds": (40.0, 49.0, 60.0),
    "exclude_repeat_zodiac": (False,),
    "include_repeat_number": (False,),
    "repeat_number_weight": (0.0, 1.0),
    "repeat_zodiac_weight": (0.0, 1.0),
    "stale_periods": (10, 30, 120),
    "stale_weight": (0.0, 0.7, 1.0),
    "lattice_enabled": (False,),
    "lattice_window": (0, 10, 20, 60, 100),
    "role_w_primary": (1.0, 5.0),
    "role_w_secondary": (1.0, 5.0),
    "role_w_defense": (1.0, 5.0),
    "trend_bias": ("neutral", "hot", "cold"),
    "trend_window": (0, 5, 60, 100),
    "avoid_cold_enabled": (True,),
    "avoid_cold_days": (10, 30, 120),
    "pick_strategy": ("score_top",),
    "wave_alloc": tuple(WAVE_ALLOCS),
    "pick_sampling": tuple(PICK_SAMPLINGS),
    "score_w_focus": (0.0, 3.0),
    "score_w_mid": (0.0, 4.0),
    "score_w_omit": (-1.0, 1.0),
    "score_w_diff": (0.0, 3.0),
    TREND_BIAS_EXPLICIT_KEY: (False,),
    # 只读派生字段：``SettingsOut`` 会返回，但不在 DEFAULT_SETTINGS 里
    "big_min": (10, 40),
}

# 布尔开关：备选值只能取「另一档」，否则在开关已关的配置下会扫出空集
BOOL_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "exclude_repeat_zodiac",
        "include_repeat_number",
        "lattice_enabled",
        "avoid_cold_enabled",
        TREND_BIAS_EXPLICIT_KEY,
    }
)
# 枚举开关：备选值 = 同枚举的其它取值
ENUM_SETTING_VALUES: dict[str, tuple[Any, ...]] = {
    "mode": tuple(MODES),
    "pick_strategy": tuple(PICK_STRATEGIES),
    "wave_alloc": tuple(WAVE_ALLOCS),
    "pick_sampling": tuple(PICK_SAMPLINGS),
    "trend_bias": tuple(TREND_BIASES),
}


def sweep_for(key: str, current: Any) -> tuple[Any, ...]:
    """该设置在当前取值下的扫描备选（永远不会等于当前值）。

    布尔取反、枚举取其它档、数值取声明列表里不等于当前值的项；若全被过滤掉
    （例如当前值就等于列表里唯一项）则回退为原列表。
    """
    if key in BOOL_SETTING_KEYS:
        return (not bool(current),)
    if key in ENUM_SETTING_VALUES:
        return tuple(
            value for value in ENUM_SETTING_VALUES[key] if value != current
        )
    values = tuple(SETTING_SWEEPS[key])
    filtered = tuple(value for value in values if value != current)
    return filtered or values


# 声明：该键是否是**引擎输入**（``DEFAULT_SETTINGS`` 的成员，外加旧字段 bet_unit）。
# 不在其中的键会被 clamp_settings 整条忽略 → 无法影响输出（DEAD 的判据之一）。
ENGINE_INPUT_KEYS: frozenset[str] = frozenset(
    key for key in (*DEFAULT_SETTINGS.keys(), "bet_unit")
)
NOT_ENGINE_INPUT_KEYS: frozenset[str] = frozenset({"big_min"})

# 代码依据：每个键被谁消费 / 被谁挡住（写进报告，便于人工复核）
SETTING_GATES: dict[str, str] = {
    "small_max": "lottery.classify_wave:335 分桶阈值（同时经 clamp 约束 normal_max >= small_max + 1）",
    "normal_max": "lottery.classify_wave:335 分桶阈值；big_min = normal_max + 1（derive_big_min:535）",
    "total_amount": "唯一预算真值（recommend:1894 → prepare_budget:1627）；不足 10 注时降级注数",
    "amount_unit": "注码粒度（allocate_amounts:1474）；total/unit < 注数 时降级注数",
    "bet_unit": "已降级为派生展示值：只在「缺 total_amount」的旧行回退（clamp_settings:374-382）",
    "mode": "MODE_SINGLE 会把有效注数压到 1（effective_pick_count:540）；其余模式只改金额",
    "pick_count": "选号数量（recommend:1896）",
    "odds": "只进 pnl / 兑付展示；recommend 不读赔率 → 不改变号码与金额",
    "exclude_repeat_zodiac": "候选池整组剔除最新同肖（build_candidate_pools:607）",
    "include_repeat_number": "候选池是否保留上期特码；exclude_repeat_zodiac=True 时该号已被剔除",
    "repeat_number_weight": "**已停用（2026-10-09 降权去除）**：值仍被 clamp 接受并回显，但不参与选号 / 金额",
    "repeat_zodiac_weight": "**已停用（2026-10-09 降权去除）**：同上；同肖仍照常打标签",
    "stale_periods": "冷号判定阈值：只决定 is_stale 标签与冷号展示，**不再参与任何降权**",
    "stale_weight": "**已停用（2026-10-09 降权去除）**：同上",
    "lattice_enabled": "点阵总开关：predict_wave_band → bucket_sampling_weight（**只定义抽样概率分布**，不再是排序键）",
    "lattice_window": "仅在 lattice_enabled=True 时被 predict_wave_band:766 读取",
    "role_w_primary": "只进 allocate_amounts 的角色配额（distribute_units_by_role:1403）；无余量时不生效",
    "role_w_secondary": "只进 allocate_amounts 的角色配额（distribute_units_by_role:1403）；无余量时不生效",
    "role_w_defense": "只进 allocate_amounts 的角色配额（distribute_units_by_role:1403）；无余量时不生效",
    "trend_bias": "已停用：trend_sampling_weight 恒 1.0；order_pool 不吃走势前缀；读取侧恒 neutral",
    "trend_window": "已停用为选号权重；仅影响 trend_distributions / number_frequency 展示",
    "avoid_cold_enabled": "避冷总开关（order_pool:1154 / take_from_role_band:1208 / 金额封顶:2238）",
    "avoid_cold_days": "仅在 avoid_cold_enabled=True 时读取",
    "pick_strategy": "wave_round / score_top 分支（recommend:2135）",
    "wave_alloc": "drain / balanced 配额口径（recommend 选号分支）；balanced = 非空桶按最大余数均分",
    "pick_sampling": "seeded_random / ranked 抽样口径；seeded_random 走 sampling_seed_key:1484 期号种子",
    "score_w_focus": "只在 pick_strategy=score_top 时被 score_candidate:1743 读取",
    "score_w_mid": "只在 pick_strategy=score_top 时被 score_candidate:1743 读取",
    "score_w_omit": "只在 pick_strategy=score_top 时读取；且现场值 0.0 → 该项恒为 0（乘以零）",
    "score_w_diff": "只在 pick_strategy=score_top 时被 score_candidate:1743 读取",
    TREND_BIAS_EXPLICIT_KEY: "已停用：effective_trend_bias 恒回退 neutral（explicit 也无法恢复加权）",
    "big_min": "只读派生 = normal_max + 1；clamp_settings 忽略未知键，SettingsPatch 未声明",
}


# --------------------------------------------------------------------------- #
# 配置解析（与 Store.get_settings 同口径）
# --------------------------------------------------------------------------- #
def resolve_effective(raw: dict[str, Any]) -> dict[str, Any]:
    """``clamp_settings`` + ``resolve_trend_bias`` —— 与 ``Store.get_settings`` 一致。

    ``backtest_stats`` / ``recommend`` 收到的就是这层解析后的配置；审计必须同样解析，
    否则 ``trend_bias_explicit`` 这个门闩会被误判成无效。
    """
    return resolve_trend_bias(clamp_settings(dict(raw)))


# --------------------------------------------------------------------------- #
# 走步回测（与 backtest_stats 同口径，但额外保留每注金额）
# --------------------------------------------------------------------------- #
def walk_forward(
    series: Sequence[dict[str, Any]],
    raw_settings: dict[str, Any],
    *,
    min_prior_draws: int = MIN_PRIOR_DRAWS,
) -> list[dict[str, Any]]:
    """逐期走步：每期只用该期之前的数据 ``recommend()``，再与该期实际特码比对。

    与 ``services.analytics.backtest_stats`` 的循环逐行同口径（升序序列、history
    必须「最新在前」因此要 ``reversed``）；区别只在于本函数额外保留每注金额，
    用来区分「改了号码」与「只改了筹码分配」。
    """
    from services.lottery import recommend  # 局部导入，避免模块级循环依赖观感

    cfg = resolve_effective(raw_settings)
    specials = [int(row["special_number"]) for row in series]
    periods = [row["period"] for row in series]
    dates = [row["draw_date"] for row in series]
    mode = cfg["mode"]

    rows: list[dict[str, Any]] = []
    for index in range(min_prior_draws, len(series)):
        latest = specials[index - 1]
        previous = specials[index - 2] if index - 2 >= 0 else None
        history = list(reversed(specials[:index]))
        history_dates = list(reversed(dates[:index]))
        outcome = recommend(
            latest=latest,
            previous=previous,
            history_numbers=history,
            history_dates=history_dates,
            settings=cfg,
            mode=mode,
            period=periods[index],
        )
        picks = outcome["picks"]
        numbers = tuple(sorted(int(pick["number"]) for pick in picks))
        amounts = tuple(
            sorted((int(pick["number"]), int(pick["amount"])) for pick in picks)
        )
        actual = specials[index]
        rows.append(
            {
                "period": periods[index],
                "latest_used": latest,
                "predicted": numbers,
                "amounts": amounts,
                "hit": actual in set(numbers),
                "hit_amount": next(
                    (int(p["amount"]) for p in picks if int(p["number"]) == actual), 0
                ),
            }
        )
    return rows


def payout_of(row: dict[str, Any], odds: float) -> float:
    """该期兑付 = 命中那一注的金额 × 赔率（未命中为 0）。"""
    return float(row["hit_amount"]) * float(odds)


# --------------------------------------------------------------------------- #
# 敏感度测量
# --------------------------------------------------------------------------- #
_CONFIG_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = {}


def _cache_key(raw: dict[str, Any]) -> str:
    return json.dumps(resolve_effective(raw), sort_keys=True, ensure_ascii=False)


def _series_fingerprint(series: Sequence[dict[str, Any]]) -> str:
    """开奖序列指纹：缓存必须同时按「配置 + 序列」分区。

    只按配置缓存会在同一进程内混用两份序列（例如测试里连续跑合成序列与真实池）
    而静默返回错的回测结果 —— 缓存键必须带上序列。
    """
    payload = "|".join(
        f"{row['period']}:{int(row['special_number'])}:{row['draw_date']}"
        for row in series
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def rows_for(raw: dict[str, Any], series: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """带缓存的走步回测（同一份「有效配置 + 同一份序列」只跑一次）。"""
    key = (_cache_key(raw), _series_fingerprint(series))
    cached = _CONFIG_CACHE.get(key)
    if cached is None:
        cached = walk_forward(series, raw)
        _CONFIG_CACHE[key] = cached
    return cached


def _compare(
    base_rows: Sequence[dict[str, Any]],
    alt_rows: Sequence[dict[str, Any]],
    base_odds: float,
    alt_odds: float,
) -> dict[str, Any]:
    """逐期比较备选值与基准：号码集合 / 有效注数 / 金额 / 兑付。"""
    by_period = {row["period"]: row for row in base_rows}
    picks_changed = pick_count_changed = amounts_changed = payout_changed = 0
    for row in alt_rows:
        base = by_period.get(row["period"])
        if base is None:
            continue
        if base["predicted"] != row["predicted"]:
            picks_changed += 1
        if len(base["predicted"]) != len(row["predicted"]):
            pick_count_changed += 1
        if base["amounts"] != row["amounts"]:
            amounts_changed += 1
        if payout_of(base, base_odds) != payout_of(row, alt_odds):
            payout_changed += 1
    base_hits = sum(1 for row in base_rows if row["hit"])
    alt_hits = sum(1 for row in alt_rows if row["hit"])
    return {
        "periods_picks_changed": picks_changed,
        "periods_pick_count_changed": pick_count_changed,
        "periods_amounts_changed": amounts_changed,
        "periods_payout_changed": payout_changed,
        "hits": alt_hits,
        "hits_delta": alt_hits - base_hits,
        "evaluated": len(alt_rows),
    }


def evaluate_setting(
    key: str,
    config_name: str,
    raw_settings: dict[str, Any],
    series: Sequence[dict[str, Any]],
    values: Iterable[Any],
) -> dict[str, Any]:
    """对单个设置在单组配置下做完整扫描，返回逐备选值的实测差异。"""
    base_cfg = resolve_effective(raw_settings)
    base_rows = rows_for(raw_settings, series)
    base_odds = float(base_cfg["odds"])
    current = base_cfg.get(key, raw_settings.get(key))

    alternatives: list[dict[str, Any]] = []
    numbers_hits: list[Any] = []
    for value in values:
        alt_raw = dict(raw_settings)
        alt_raw[key] = value
        alt_cfg = resolve_effective(alt_raw)
        alt_value = alt_cfg.get(key, value)
        if alt_value == current:
            alternatives.append(
                {
                    "value": value,
                    "skipped": "clamped_back_to_current_value",
                    "effective_value": alt_value,
                }
            )
            continue
        try:
            alt_rows = rows_for(alt_raw, series)
        except Exception as exc:  # noqa: BLE001
            # 引擎在极端参数组合下会自己崩（例如 score_top + 预算不足 1 个注码单位：
            # prepare_budget 把注数降为 0，但 select_score_top_candidates 用 max(1, 0)
            # 仍返回 1 注，随后 allocate_amounts 返回空金额 → build_copy_text 取不到
            # pick["amount"]）。这里如实记录，不吞掉、不修改 services/lottery.py。
            alternatives.append(
                {
                    "value": value,
                    "effective_value": alt_value,
                    "engine_error": f"{type(exc).__name__}: {exc}",
                    "skipped": "engine_error",
                }
            )
            continue
        diff = _compare(base_rows, alt_rows, base_odds, float(alt_cfg["odds"]))
        diff.update({"value": value, "effective_value": alt_value})
        alternatives.append(diff)
        if diff["periods_picks_changed"] or diff["periods_pick_count_changed"]:
            numbers_hits.append(value)

    counted = [alt for alt in alternatives if "skipped" not in alt]
    errors = [alt for alt in alternatives if alt.get("skipped") == "engine_error"]
    numbers_changed = bool(numbers_hits)
    pick_count_changed = any(
        alt["periods_pick_count_changed"] for alt in counted
    )
    amounts_changed = any(alt["periods_amounts_changed"] for alt in counted)
    payout_changed = any(alt["periods_payout_changed"] for alt in counted)
    hits_values = sorted({alt["hits"] for alt in counted})
    base_hits = sum(1 for row in base_rows if row["hit"])

    return {
        "key": key,
        "config": config_name,
        "current_value": current,
        "alternatives_evaluated": len(counted),
        "numbers_changed": numbers_changed,
        "numbers_changed_values": numbers_hits,
        "pick_count_changed": pick_count_changed,
        "amounts_changed": amounts_changed,
        "payout_changed": payout_changed,
        "max_periods_picks_changed": max(
            (alt["periods_picks_changed"] for alt in counted), default=0
        ),
        "evaluated_periods": len(base_rows),
        "hits_base": base_hits,
        "hits_values": hits_values,
        "hits_delta_range": (
            [min(hits_values) - base_hits, max(hits_values) - base_hits]
            if hits_values
            else [0, 0]
        ),
        "engine_error_values": [
            {"value": alt["value"], "error": alt["engine_error"]} for alt in errors
        ],
        "alternatives": alternatives,
    }


def classify(
    effect: dict[str, Any],
    other_effects: dict[str, dict[str, Any]],
    key: str,
) -> tuple[str, str]:
    """按实测结果分类；返回 ``(class, 依据说明)``。

    - ``ACTIVE``：号码或有效注数变了；
    - ``COSMETIC``：号码没变，但金额 / 兑付变了；
    - ``INERT_UNDER_CURRENT_CONFIG``：本配置下无变化，但另一组配置下有变化（点名）；
    - ``DEAD``：所有被测配置与代码路径都无法影响输出。
    """
    if effect["numbers_changed"]:
        via = "pick_count_degradation" if (
            effect["pick_count_changed"] and not effect["numbers_changed_values"]
        ) else "number_selection"
        return CLASS_ACTIVE, f"号码/注数改变（经 {via}）"
    if effect["amounts_changed"] or effect["payout_changed"]:
        via = "amount_allocation" if effect["amounts_changed"] else "payout_only"
        return CLASS_COSMETIC, f"号码不变（{via}）"
    for name, other in other_effects.items():
        if other["numbers_changed"]:
            return CLASS_INERT, (
                f"当前配置下无效；在配置 {name} 下改变号码"
                f"（{other['max_periods_picks_changed']} 期）"
            )
    # 本条自身连金额都不变，但换个配置能改金额 → 属于「被配置挡住」而不是死键
    # （典型：role_w_* 在现场「预算刚好等于最低注」时没有余量可分配）。
    for name, other in other_effects.items():
        if other["amounts_changed"] or other["payout_changed"]:
            return CLASS_INERT, (
                f"当前配置下金额也不变（无余量 / 无预算差异）；"
                f"在配置 {name} 下改变金额（不影响号码）"
            )
    if key in NOT_ENGINE_INPUT_KEYS:
        return CLASS_DEAD, "不是引擎输入：clamp_settings 忽略未知键 / SettingsPatch 未声明"
    return CLASS_DEAD, "所有被测配置与代码路径下均无任何变化"


# --------------------------------------------------------------------------- #
# 命中率 / 功效（deliverable 3 的数字来源）
# --------------------------------------------------------------------------- #
def power_report(series: Sequence[dict[str, Any]], raw_settings: dict[str, Any]) -> dict[str, Any]:
    """本池样本内的命中率对照与统计功效（全部为实测 / 精确二项）。"""
    draws_asc = [
        {
            "period": row["period"],
            "draw_date": row["draw_date"].isoformat()
            if hasattr(row["draw_date"], "isoformat")
            else str(row["draw_date"]),
            "special_number": int(row["special_number"]),
        }
        for row in series
    ]
    cfg = resolve_effective(raw_settings)
    outcome = backtest_stats(
        draws_asc,
        base_settings=cfg,
        include_results=False,
        include_wave_breakdown=False,
    )
    evaluated = int(outcome["evaluated"])
    hits = int(outcome["hits"])
    baseline = float(outcome["random_baseline_hit_rate"])
    picks = int(outcome["settings"]["effective_pick_count"])
    rate = hits / evaluated if evaluated else None
    mdd = minimum_detectable_delta(evaluated, baseline)
    odds = float(cfg["odds"])
    number_count = NUMBER_MAX - NUMBER_MIN + 1

    def required(delta: float) -> int | None:
        if delta <= 0:
            return None
        return math.ceil(((mdd["z_alpha"] + mdd["z_power"]) ** 2) * baseline * (1 - baseline) / (delta * delta))

    break_even_delta = 50 / 235 - baseline
    return {
        "scope": f"本池已导入 {len(series)} 期数据内",
        "evaluated": evaluated,
        "picks_per_period": picks,
        "hits": hits,
        "hit_rate": rate,
        "random_baseline_hit_rate": baseline,
        "hit_rate_minus_baseline_pp": (rate - baseline) * 100 if rate is not None else None,
        "verdict": outcome["verdict"]["kind"],
        "within_noise": outcome["verdict"]["within_noise"],
        "binomial_p_two_sided": binomial_tail_p(hits, evaluated, baseline),
        "binomial_p_greater": binomial_tail_p(hits, evaluated, baseline, alternative="greater"),
        "binomial_p_less": binomial_tail_p(hits, evaluated, baseline, alternative="less"),
        "standard_error": mdd["standard_error"],
        "two_sigma_delta": mdd["two_sigma_delta"],
        "minimum_detectable_delta": mdd["minimum_detectable_delta"],
        "power_at_current_n": mdd["power_at_current_n"],
        "required_draws": {
            **mdd["required_draws"],
            "break_even_50_of_235": required(break_even_delta),
        },
        "break_even_delta_pp": break_even_delta * 100,
        "ev_per_100": (odds / number_count - 1.0) * 100.0,
        "ev_formula": f"ev_per_100 = 100 * ({odds:g} / {number_count} - 1) = "
        f"{(odds / number_count - 1.0) * 100.0:+.4f}",
        "actual_in_pool_rate": outcome.get("actual_in_pool_rate"),
        "average_available_numbers": outcome.get("average_available_numbers"),
        "ceiling_note": (
            "命中数上界 = 实际特码落在候选池内的期数：池外期数引擎不可能命中。"
            "这改变的是可达上限，不改变单期命中概率 注数/49。"
        ),
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def load_series(path: str) -> list[dict[str, Any]]:
    """读开奖数据并规整成 ``(draw_date, period)`` 升序、带生肖的规范序列。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"开奖数据顶层必须是数组：{path}")
    return normalize_draws(raw)


def run_audit(
    series: Sequence[dict[str, Any]],
    *,
    config_names: Sequence[str] = tuple(AUDIT_CONFIGS),
    keys: Sequence[str] | None = None,
    include_power: bool = True,
) -> dict[str, Any]:
    """跑完整审计：逐配置 × 逐设置扫描，并给出跨配置分类。"""
    selected_keys = list(keys) if keys else list(SETTING_SWEEPS)
    for key in selected_keys:
        if key not in SETTING_SWEEPS:
            raise KeyError(f"未声明扫描范围的设置项：{key}")

    # config × key → effect
    matrix: dict[str, dict[str, dict[str, Any]]] = {}
    sweeps_used: dict[str, dict[str, list[Any]]] = {}
    for name in config_names:
        settings = AUDIT_CONFIGS[name]
        current_cfg = resolve_effective(settings)
        matrix[name] = {}
        sweeps_used[name] = {}
        for key in selected_keys:
            values = sweep_for(key, current_cfg.get(key, settings.get(key)))
            sweeps_used[name][key] = list(values)
            matrix[name][key] = evaluate_setting(
                key, name, settings, series, values
            )

    report: dict[str, Any] = {}
    for name in config_names:
        effects = matrix[name]
        rows: list[dict[str, Any]] = []
        for key in selected_keys:
            others = {
                other: matrix[other][key]
                for other in config_names
                if other != name
            }
            cls, reason = classify(effects[key], others, key)
            row = {
                "key": key,
                "class": cls,
                "class_label": CLASS_LABELS[cls],
                "reason": reason,
                "current_value": effects[key]["current_value"],
                "periods_picks_changed": effects[key]["max_periods_picks_changed"],
                "evaluated_periods": effects[key]["evaluated_periods"],
                "hits_base": effects[key]["hits_base"],
                "hits_values": effects[key]["hits_values"],
                "hits_delta_range": effects[key]["hits_delta_range"],
                "numbers_changed_values": effects[key]["numbers_changed_values"],
                "amounts_changed": effects[key]["amounts_changed"],
                "payout_changed": effects[key]["payout_changed"],
                "pick_count_changed": effects[key]["pick_count_changed"],
                "gate": SETTING_GATES.get(key, ""),
            }
            rows.append(row)
        rows.sort(key=lambda row: (row["class"] != CLASS_ACTIVE, row["key"]))
        report[name] = rows

    payload: dict[str, Any] = {
        "scope": f"本池已导入 {len(series)} 期数据内（样本仅 210 期，禁止外推）",
        "sample_size": len(series),
        "evaluated_periods": len(series) - MIN_PRIOR_DRAWS,
        "configs": {
            name: {
                "settings": {
                    k: v for k, v in AUDIT_CONFIGS[name].items()
                },
                "effective": resolve_effective(AUDIT_CONFIGS[name]),
            }
            for name in config_names
        },
        "sweeps": {key: list(SETTING_SWEEPS[key]) for key in selected_keys},
        "sweeps_effective": sweeps_used,
        "gates": {key: SETTING_GATES.get(key, "") for key in selected_keys},
        "engine_input_keys": sorted(ENGINE_INPUT_KEYS),
        "not_engine_input_keys": sorted(NOT_ENGINE_INPUT_KEYS),
        "tables": report,
        "notes": [
            "口径：只统计本池已导入的样本内走步回测；命中数差异不代表命中概率差异。",
            "任何权重都改不动单期命中概率 = 有效注数 / 49；期望值恒为 赔率 / 49 - 1。",
            "ACTIVE = 改变号码或有效注数；COSMETIC = 只改金额 / 兑付；"
            "INERT_UNDER_CURRENT_CONFIG = 被开关挡住，换配置才生效；DEAD = 任何配置都改不动。",
            "无效不代表无用：金额配额与降权是「筹码分配 + 号码卫生」偏好，不是命中率手段。",
        ],
    }
    if include_power:
        payload["power"] = power_report(series, LIVE_SETTINGS)
    payload["consistency"] = consistency_check(series, config_names)
    return payload


def consistency_check(
    series: Sequence[dict[str, Any]], config_names: Sequence[str]
) -> dict[str, Any]:
    """自检：本脚本的走步回测命中数必须与 ``backtest_stats`` 一致（逐配置）。

    不一致说明审计用的走步口径与生产回测口径发生漂移，报告不可信 —— 直接报错。
    """
    draws_asc = [
        {
            "period": row["period"],
            "draw_date": row["draw_date"].isoformat()
            if hasattr(row["draw_date"], "isoformat")
            else str(row["draw_date"]),
            "special_number": int(row["special_number"]),
        }
        for row in series
    ]
    checks: dict[str, Any] = {}
    for name in config_names:
        raw = AUDIT_CONFIGS[name]
        mine = rows_for(raw, series)
        outcome = backtest_stats(
            draws_asc,
            base_settings=resolve_effective(raw),
            include_results=False,
            include_wave_breakdown=False,
        )
        mine_hits = sum(1 for row in mine if row["hit"])
        if mine_hits != int(outcome["hits"]):
            raise AssertionError(
                f"走步口径漂移（配置 {name}）：本脚本 {mine_hits} vs "
                f"backtest_stats {outcome['hits']}"
            )
        checks[name] = {
            "hits": mine_hits,
            "evaluated": len(mine),
            "matches_backtest_stats": True,
            "verdict": outcome["verdict"]["kind"],
        }
    return checks


def render_table(rows: Sequence[dict[str, Any]]) -> str:
    """把一张分类表渲染成定宽文本。"""
    head = (
        f"{'setting':24s} {'class':27s} {'picks_chg':>9s} {'hits(base→alts)':>17s} "
        f"{'gate/依据'}"
    )
    lines = [head, "-" * len(head)]
    for row in rows:
        hits = (
            f"{row['hits_base']}→{row['hits_values']}"
            if row["hits_values"] != [row["hits_base"]]
            else f"{row['hits_base']}(不变)"
        )
        lines.append(
            f"{row['key']:24s} {row['class']:27s} {row['periods_picks_changed']:9d} "
            f"{hits:>17s} {row['reason']}"
        )
    return "\n".join(lines)


def render_markdown(payload: dict[str, Any]) -> str:
    """把审计结果渲染成 Markdown 报告（UTF-8，无 BOM；供 ``docs/`` 存档）。"""
    lines: list[str] = []
    lines.append("# 设置敏感度审计：这 30 个旋钮到底改变了什么")
    lines.append("")
    lines.append(f"口径：{payload['scope']}。本报告只统计**样本内**走步回测，"
                 f"禁止升格为全量 / 市场结论。")
    lines.append("")
    lines.append(f"- 样本期数：{payload['sample_size']}；可评估期数：{payload['evaluated_periods']}")
    lines.append("- 命中数变化 **不等于** 命中概率变化；任何权重都改不动 `有效注数 / 49`。")
    lines.append("- 期望值恒为 `赔率 / 49 - 1`，与选号方法、权重、注数、注码无关。")
    lines.append("")

    power = payload.get("power")
    if power:
        lines.append("## 现场配置的样本内命中率与统计功效")
        lines.append("")
        lines.append(f"- 命中 **{power['hits']} / {power['evaluated']}** = "
                     f"`{power['hit_rate']:.4%}`；随机基线 `{power['random_baseline_hit_rate']:.4%}`"
                     f"（{power['picks_per_period']} 注 / 49）")
        lines.append(f"- 差 `{power['hit_rate_minus_baseline_pp']:+.2f}pp`；引擎判定 `{power['verdict']}`"
                     f"（within_noise = {power['within_noise']}）")
        lines.append(f"- 精确二项双侧 p = `{power['binomial_p_two_sided']:.4f}`；"
                     f"单侧(greater) p = `{power['binomial_p_greater']:.4f}`")
        lines.append(f"- 标准误 `{power['standard_error']:.4f}`（2SE = `{power['two_sigma_delta']:.4f}`）；"
                     f"MDD(80% 功效) = `{power['minimum_detectable_delta']:.4f}`")
        req = power["required_draws"]
        lines.append(f"- 80% 功效所需期数：+1% → {req.get('+1%')}，+2% → {req.get('+2%')}，"
                     f"+5% → {req.get('+5%')}，+10% → {req.get('+10%')}，"
                     f"盈亏平衡(50/235, {power['break_even_delta_pp']:+.2f}pp) → "
                     f"{req.get('break_even_50_of_235')}")
        lines.append(f"- 期望值：`{power['ev_formula']}`")
        lines.append("")

    lines.append("## 各配置下的逐项分类")
    lines.append("")
    for name, rows in payload["tables"].items():
        lines.append(f"### 配置 `{name}`")
        lines.append("")
        lines.append("| 设置 | 分类 | 号码变化期数 | 命中(基准→备选) | 依据 / 门闩 |")
        lines.append("|---|---|---:|---|---|")
        for row in rows:
            hits = (
                "、".join(str(value) for value in row["hits_values"])
                if row["hits_values"] != [row["hits_base"]]
                else f"{row['hits_base']}（不变）"
            )
            lines.append(
                f"| `{row['key']}` | {row['class']} | {row['periods_picks_changed']} "
                f"| {row['hits_base']}→{hits} | {row['reason']} |"
            )
        lines.append("")

    lines.append("## 分类口径")
    lines.append("")
    for code, label in CLASS_LABELS.items():
        lines.append(f"- `{code}`：{label}")
    lines.append("")
    lines.append("## 复现")
    lines.append("")
    lines.append("```powershell")
    lines.append("cd backend")
    lines.append(".\\\\.venv\\\\Scripts\\\\python.exe scripts\\\\settings_sensitivity.py `")
    lines.append("    --draws-json data\\\\draws_70_279.json --out ..\\\\.tmp-wave\\\\settings_sensitivity.json")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    try:  # 中文输出统一按 UTF-8 写，避免控制台代码页把汉字写成乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - 少数环境不支持
        pass
    parser = argparse.ArgumentParser(
        description="逐项扰动波浪买入法设置，实测它改变了什么（离线，不碰数据库）",
    )
    parser.add_argument("--draws-json", default=DEFAULT_DRAWS)
    parser.add_argument("--out", default="", help="把完整 JSON 结果写到该路径")
    parser.add_argument("--markdown", default="", help="把表格报告写成 Markdown 到该路径")
    parser.add_argument(
        "--configs",
        default=",".join(AUDIT_CONFIGS),
        help="要审计的配置名（逗号分隔）：" + ",".join(AUDIT_CONFIGS),
    )
    parser.add_argument(
        "--keys",
        default="",
        help="只审计这些设置项（逗号分隔）；默认全部",
    )
    parser.add_argument("--no-power", action="store_true", help="跳过命中率 / 功效段")
    parser.add_argument("--json", action="store_true", help="只打印 JSON")
    args = parser.parse_args(argv)

    config_names = [name.strip() for name in args.configs.split(",") if name.strip()]
    unknown = [name for name in config_names if name not in AUDIT_CONFIGS]
    if unknown:
        parser.error(f"未知配置名：{unknown}（可选：{list(AUDIT_CONFIGS)}）")
    keys = [key.strip() for key in args.keys.split(",") if key.strip()] or None

    series = load_series(args.draws_json)
    payload = run_audit(
        series,
        config_names=config_names,
        keys=keys,
        include_power=not args.no_power,
    )

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if args.markdown:
        md_path = Path(args.markdown)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_markdown(payload), encoding="utf-8")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"口径：{payload['scope']}")
    print(f"可评估期数：{payload['evaluated_periods']}")
    for name in config_names:
        print()
        print(f"=== 配置 {name} ===")
        print(render_table(payload["tables"][name]))
    if "power" in payload:
        power = payload["power"]
        print()
        print("=== 命中率 / 功效（现场配置，样本内） ===")
        print(
            f"命中 {power['hits']}/{power['evaluated']} = {power['hit_rate']:.4%}；"
            f"随机基线 {power['random_baseline_hit_rate']:.4%}；"
            f"差 {power['hit_rate_minus_baseline_pp']:+.2f}pp；"
            f"判定 {power['verdict']}"
        )
        print(
            f"二项双侧 p = {power['binomial_p_two_sided']:.4f}；"
            f"单侧(greater) p = {power['binomial_p_greater']:.4f}；"
            f"标准误 {power['standard_error']:.4f}，2SE = {power['two_sigma_delta']:.4f}"
        )
        print(
            f"MDD(80% 功效) = {power['minimum_detectable_delta']:.4f}；"
            f"所需期数 {power['required_draws']}"
        )
        print(f"期望值：{power['ev_formula']}")
    if args.out:
        print()
        print(f"完整结果已写入 {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
