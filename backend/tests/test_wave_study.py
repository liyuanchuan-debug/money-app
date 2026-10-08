r"""``services.wave_study`` / ``scripts.wave_dedicated`` 的单元与回归测试。

只测**纯逻辑**：不连数据库、不碰线上服务；重活（整张网格 × 置换）用**小合成序列 +
小子网格**跑，真实 210 期只用于少量「锚点」回归（``backend/data/*`` 被 .gitignore
忽略，缺文件时整组 skip，不假装通过）。

守卫要点：

- 预登记家族大小必须**先声明后看数**，且 Holm 校正复用
  ``services.analytics.holm_adjusted_p``（不重写、不缩小家族）；
- 「同网格 max 零分布」必须**严格不宽于**任何单变体检验（这是合法性的核心）；
- 严格 walk-forward：``wave_step`` 只能看见 ``numbers[:position]``，改动未来值
    30|  不得改变当期预测；
- 对半 / 三分 / 滚动窗稳定性算术可独立重算；符号一致性按定义重算；
- 功效算术用 ``statistics.NormalDist`` **独立重算**两比例公式（不引用被测实现）；
- 生产路径（``services/lottery.py`` / ``routers/*`` / ``main.py``）不得引入本模块，
  且线上 38/208 锚点与点阵消融（38 vs 56）不得漂移；
- 判定码 / 变体 id 一律英文枚举，源码 UTF-8 无 BOM、无替换字符。

（口径：所有断言只针对测试内合成的样本或本池已导入的 210 期，不涉及全市场数据。）
"""

from __future__ import annotations

import json
import math
import random
import re
from datetime import date, timedelta
from pathlib import Path
from statistics import NormalDist

import pytest

from scripts import wave_dedicated as CLI
from services import analytics as A
from services import dist_audit as DA
from services import wave_study as WS
from services.analytics import DATA_STATUS_INSUFFICIENT, DATA_STATUS_OK
from services.max_fit import K_DEFAULT, NUM_STATES, ODDS_DEFAULT

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REAL_DRAWS_FILE = BACKEND_ROOT / "data" / "draws_70_279.json"
WAVE_SOURCES = ("services/wave_study.py", "scripts/wave_dedicated.py")

# 线上生效配置快照（只读 GET /api/settings 得到的同一份；用于「生产行为不许漂移」锚点）
LIVE_SETTINGS: dict = {
    "small_max": 15,
    "normal_max": 20,
    "mode": "even",
    "pick_count": 10,
    "odds": 47.0,
    "exclude_repeat_zodiac": True,
    "include_repeat_number": True,
    "repeat_number_weight": 0.5,
    "repeat_zodiac_weight": 0.8,
    "stale_periods": 60,
    "stale_weight": 0.3,
    "lattice_enabled": True,
    "lattice_window": 30,
    "trend_bias": "mid",
    "trend_window": 20,
    "avoid_cold_enabled": False,
    "avoid_cold_days": 60,
    "pick_strategy": "wave_round",
    # 冻结锚点：2026-10-07 起 wave_alloc 默认 balanced（点阵关闭时按非空桶均分注数），
    # 会换掉点阵关闭路径的号码集合、命中数随之漂移。本快照是历史线上配置，
    # 为保住「生产行为不许漂移」对照的历史数字，这里显式钉住当时的旧口径 drain。
    "wave_alloc": "drain",
    "role_w_primary": 3.0,
    "role_w_secondary": 2.0,
    "role_w_defense": 1.0,
}


# --------------------------------------------------------------------------- #
# 合成样本
# --------------------------------------------------------------------------- #
def series_of(count: int = 90, seed: int = 20261007) -> list[int]:
    rng = random.Random(seed)
    return [rng.randrange(1, NUM_STATES + 1) for _ in range(count)]


def draws_of(series: list[int]) -> list[dict[str, object]]:
    start = date(2026, 1, 1)
    return [
        {
            "period": 70 + index,
            "draw_date": (start + timedelta(days=index)).isoformat(),
            "special_number": number,
        }
        for index, number in enumerate(series)
    ]


def real_draws() -> list[dict[str, object]]:
    if not REAL_DRAWS_FILE.exists():  # pragma: no cover - 依赖本地 data/*
        pytest.skip(f"缺少本地真实开奖文件：{REAL_DRAWS_FILE}")
    return json.loads(REAL_DRAWS_FILE.read_text(encoding="utf-8"))


def tiny_grid() -> list[dict[str, object]]:
    """小子网格：引擎组件一个 + 两个纯波动定义（够测编排，且跑得快）。"""
    wanted = {
        "engine_component|w30|neutral",
        "production_band|w30|neutral",
        "halfwidth_band|w30|neutral|p5",
    }
    return [row for row in WS.variant_grid() if row["variant_id"] in wanted]

# --------------------------------------------------------------------------- #
# 1. 预登记与家族计数
# --------------------------------------------------------------------------- #
def test_preregistration_declares_one_hypothesis_and_the_whole_family() -> None:
    block = WS.preregistration_block()
    assert block["declared_before_looking"] is True
    assert block["direction"] == "greater"
    assert block["family_size"] == WS.variant_family_size() == len(WS.variant_grid())
    assert block["family_size"] == 87
    assert block["core_subfamily_size"] == len(block["core_subfamily_ids"]) == 9
    assert block["alpha"] == 0.05
    assert block["k"] == K_DEFAULT
    assert block["baseline_rate"] == pytest.approx(K_DEFAULT / NUM_STATES)
    assert block["primary_variant"] in {row["variant_id"] for row in WS.variant_grid()}
    assert block["band_thresholds"]["small_max"] == 10
    assert block["band_thresholds"]["normal_max"] == 30
    assert "max 零分布" in block["legitimate_test"]


def test_variant_grid_ids_are_unique_english_codes_and_exactly_one_primary() -> None:
    grid = WS.variant_grid()
    ids = [row["variant_id"] for row in grid]
    assert len(ids) == len(set(ids)) == 87
    for row in grid:
        assert re.fullmatch(r"[a-z0-9_|.\-]+", row["variant_id"]), row["variant_id"]
        assert row["definition"] in WS.WAVE_DEFINITIONS
        assert row["bias"] in WS.WAVE_BIAS_AXIS
        assert row["definition_label"].strip()
    assert [row["variant_id"] for row in grid if row["is_primary"]] == [
        WS.PRIMARY_VARIANT_ID
    ]
    assert WS.PRIMARY_VARIANT_ID == "engine_component|w30|neutral"
    assert WS.variant_grid() == grid  # 确定性


def test_bias_axis_only_moves_tie_breaks_and_density_matches_the_engine_formula() -> None:
    """87 行 ≠ 87 个概率模型：偏好轴只改并列次序，density 与引擎同一公式。"""
    block = WS.distinct_probability_models()
    assert block["row_count"] == 87
    assert block["distinct_probability_models"] == 21
    assert block["bias_is_tiebreak_only"] is True
    keys = {row["variant_id"]: WS.variant_model_key(row) for row in WS.variant_grid()}
    assert keys["density_band|w30|hot"] == keys["engine_component|w30|neutral"]
    assert keys["density_band|w10|cold"] == keys["engine_component|w10|neutral"]
    assert keys["production_band|w30|hot"] == keys["production_band|w30|cold"]
    assert keys["production_band|w30|hot"] != keys["halfwidth_band|w30|hot|p5"]
    assert block["groups"][keys["engine_component|w30|neutral"]]["size"] == 5  # 引擎 1 + density 4
    assert str(block["distinct_probability_models"]) in WS.model_family_note()


def test_variant_grid_axes_match_the_declared_cartesian_product() -> None:
    grid = WS.variant_grid()
    by_definition: dict[str, int] = {}
    for row in grid:
        by_definition[row["definition"]] = by_definition.get(row["definition"], 0) + 1
    windows = len(WS.WAVE_WINDOWS)
    biases = len(WS.WAVE_BIAS_AXIS)
    assert by_definition[WS.VARIANT_ENGINE_COMPONENT] == windows
    assert by_definition[WS.VARIANT_PRODUCTION_BAND] == windows * biases
    assert by_definition[WS.VARIANT_DENSITY_BAND] == windows * biases
    assert by_definition[WS.VARIANT_HALFWIDTH_BAND] == windows * biases * len(
        WS.WAVE_HALFWIDTHS
    )
    assert by_definition[WS.VARIANT_VOLATILITY_BAND] == windows * biases * len(
        WS.WAVE_VOLATILITY_SCALES
    )
    assert sum(by_definition.values()) == WS.variant_family_size()


# --------------------------------------------------------------------------- #
# 2. max 零分布严格不宽于单变体检验
# --------------------------------------------------------------------------- #
def test_max_over_grid_is_strictly_more_conservative_than_any_single_variant() -> None:
    observed = {"a": 11, "b": 11}
    null = [
        {"a": 11, "b": 2},
        {"a": 2, "b": 11},
        {"a": 3, "b": 3},
        {"a": 10, "b": 4},
        {"a": 4, "b": 10},
    ]
    single = WS.per_variant_pvalues(observed, null)
    block = WS.max_over_grid(observed, null)
    assert block["observed_max"] == 11
    assert block["best_variant"] in ("a", "b")
    assert single["a"] == pytest.approx((1 + 1) / 6)  # 只有一行 a >= 11
    assert block["p_value"] == pytest.approx((1 + 2) / 6)  # 两行的 max >= 11
    assert block["p_value"] > min(single.values())
    assert block["family_size"] == 2
    assert block["null_max"]["mean"] == pytest.approx((11 + 11 + 3 + 10 + 10) / 5)
    assert block["null_max"]["max"] == 11
    assert block["null_max"]["values"] == [11, 11, 3, 10, 10]
    assert block["data_status"] == DATA_STATUS_OK


def test_max_over_grid_null_is_never_below_any_per_variant_null() -> None:
    rng = random.Random(7)
    keys = ["a", "b", "c", "d"]
    null = [{key: rng.randrange(0, 20) for key in keys} for _ in range(40)]
    observed = {key: rng.randrange(0, 20) for key in keys}
    block = WS.max_over_grid(observed, null)
    single = WS.per_variant_pvalues(observed, null)
    # 结构性质：{max >= obs_max} ⊇ {X_best >= obs_best} ⇒ p_max 不会比「观测最好的那条」更小
    assert block["p_value"] >= single[block["best_variant"]]
    assert block["p_value"] == pytest.approx(
        (1 + sum(1 for row in null if max(row.values()) >= block["observed_max"]))
        / (1 + len(null))
    )
    for key in keys:
        values = [row[key] for row in null]
        assert block["null_max"]["mean"] >= sum(values) / len(values)
        assert block["null_max"]["values"] == [
            max(row.values()) for row in null
        ]


def test_max_over_grid_subfamily_restricts_the_keys_and_the_null_max() -> None:
    observed = {"a": 11, "b": 3}
    null = [{"a": 4, "b": 12}, {"a": 5, "b": 2}]
    subset = WS.max_over_grid(observed, null, keys=["a"])
    assert subset["family_size"] == 1
    assert subset["observed_max"] == 11
    assert subset["null_max"]["values"] == [4, 5]  # 只看 a，不看 b 的 12
    assert subset["p_value"] == pytest.approx(1 / 3)


def test_max_over_grid_without_samples_reports_insufficient_data() -> None:
    block = WS.max_over_grid({"a": 5}, [])
    assert block["observed_max"] == 5
    assert block["p_value"] is None
    assert block["data_status"] == DATA_STATUS_INSUFFICIENT
    assert WS.max_over_grid({}, [{"a": 1}])["data_status"] == DATA_STATUS_INSUFFICIENT


# --------------------------------------------------------------------------- #
# 3. Holm 家族：家族大小真的喂进去了，且复用 analytics 的实现
# --------------------------------------------------------------------------- #
def test_holm_family_counts_every_variant_and_reuses_analytics() -> None:
    series = series_of(40)
    variants = WS.variant_grid()
    observed = WS.grid_hits(series, variants, warmup=15, k=3)
    null = [
        WS.grid_hits(WS.shuffled_values(series, 900 + index), variants, warmup=15, k=3)
        for index in range(3)
    ]

    holm = WS.holm_family(observed, null)
    assert holm["family_size"] == 87
    assert holm["tested_size"] == 87
    assert holm["correction"] == "holm"
    assert holm["reused_function"] == "services.analytics.holm_adjusted_p"
    assert holm["bonferroni_threshold"] == pytest.approx(0.05 / 87)

    pvalues = [row["p_value"] for row in holm["rows"]]
    expected = A.holm_adjusted_p(pvalues, alpha=0.05)
    assert [row["p_adjusted"] for row in holm["rows"]] == expected["adjusted"]
    assert holm["min_p_raw"] == pytest.approx(min(pvalues))
    assert holm["min_p_adjusted"] == pytest.approx(min(expected["adjusted"]))
    assert holm["survivor_count"] == len(
        [row for row in holm["rows"] if row["p_adjusted"] <= 0.05]
    )
    assert len(holm["rows"]) == 87
    assert [row["variant_id"] for row in holm["rows"]] == list(observed)


def test_holm_family_insufficient_when_no_p_values_exist() -> None:
    holm = WS.holm_family({}, [])
    assert holm["family_size"] == 0
    assert holm["tested_size"] == 0
    assert holm["survivors"] == []
    assert holm["data_status"] == DATA_STATUS_INSUFFICIENT
    assert holm["min_p_raw"] is None


def test_permutation_p_values_are_conservative_and_never_zero() -> None:
    observed = {"a": 5}
    null = [{"a": 1}, {"a": 2}, {"a": 5}]
    pvalues = WS.per_variant_pvalues(observed, null)
    assert pvalues["a"] == pytest.approx((1 + 1) / 4)  # 永不为 0（含观测自身）
    assert WS.per_variant_pvalues({"a": 9}, null)["a"] == pytest.approx(1 / 4)
    assert WS.per_variant_pvalues({"a": 1}, [])["a"] is None


# --------------------------------------------------------------------------- #
# 4. 严格 walk-forward：未来值不许进当期预测
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "variant_id",
    ["engine_component|w30|neutral", "halfwidth_band|w30|neutral|p5"],
)
def test_wave_step_ignores_future_values(variant_id: str) -> None:
    variant = next(
        row for row in WS.variant_grid() if row["variant_id"] == variant_id
    )
    base = series_of(60)
    position = len(base) - 1
    left = base + [1, 2, 3]
    right = base + [40, 41, 42]

    first = WS.wave_step(left, position, variant, k=5)
    second = WS.wave_step(right, position, variant, k=5)
    assert first["picks"] == second["picks"]
    assert first["ranking"] == second["ranking"]
    assert first["probabilities"] == second["probabilities"]
    assert first["visible_length"] == position


def test_wave_step_rejects_out_of_range_positions() -> None:
    variant = WS.variant_grid()[0]
    series = series_of(20)
    with pytest.raises(ValueError):
        WS.wave_step(series, 0, variant)
    with pytest.raises(ValueError):
        WS.wave_step(series, len(series), variant)
    with pytest.raises(ValueError):
        WS.wave_step(series, -3, variant)


def test_variant_walk_forward_ranks_before_the_edited_tail_are_unchanged() -> None:
    variant = next(
        row
        for row in WS.variant_grid()
        if row["variant_id"] == "production_band|w30|neutral"
    )
    base = series_of(40)
    left = base + [11, 22, 33]
    right = base + [44, 45, 46]
    warmup = 10

    first = WS.variant_walk_forward(left, variant, warmup=warmup, k=3)
    second = WS.variant_walk_forward(right, variant, warmup=warmup, k=3)
    assert first["positions"] == second["positions"] == list(range(warmup, len(left)))
    assert first["ranks"][:-3] == second["ranks"][:-3]
    assert first["ranks"] != second["ranks"]  # 尾巴确实参与（当期实际值）
    assert first["evaluated"] == len(left) - warmup
    assert first["hits"] == sum(1 for value in first["hits_series"] if value)
    assert first["hit_rate"] == pytest.approx(first["hits"] / first["evaluated"])


def test_variant_hits_agrees_with_the_full_walk_forward_path() -> None:
    """置换用的轻量路径必须与四把尺子的路径给出同一命中数（否则零分布是假的）。"""
    series = series_of(50)
    for variant in tiny_grid():
        light = WS.variant_hits(series, variant, warmup=12, k=3)
        full = WS.variant_walk_forward(series, variant, warmup=12, k=3)
        assert light == full["hits"]
        assert light == sum(1 for value in full["hits_series"] if value)


def test_grid_walk_forward_and_grid_hits_cover_the_whole_grid() -> None:
    series = series_of(45)
    variants = tiny_grid()
    walk = WS.grid_walk_forward(series, variants, warmup=10, k=3)
    hits = WS.grid_hits(series, variants, warmup=10, k=3)
    assert set(walk) == set(hits) == {row["variant_id"] for row in variants}
    for variant_id, block in walk.items():
        assert hits[variant_id] == block["hits"]
        assert block["reference"]["hit_rate"] == pytest.approx(K_DEFAULT / NUM_STATES)
        assert block["reference"]["mean_rank"] == pytest.approx(25.0)
        assert block["reference"]["log_loss"] == pytest.approx(math.log(NUM_STATES))
        assert block["reference"]["brier"] == pytest.approx(
            (1 / NUM_STATES) * (1 - 1 / NUM_STATES)
        )


def test_engine_component_variant_matches_dist_engine_wave_component() -> None:
    """``engine_component`` 变体必须就是 ``dist_engine`` 的 WAVE 组件（不另写一套）。"""
    from services import dist_engine as DE

    series = series_of(40)
    variant = next(
        row
        for row in WS.variant_grid()
        if row["variant_id"] == "engine_component|w20|neutral"
    )
    step = WS.wave_step(series, 30, variant, k=10)
    expected = DE.component_topk(
        series,
        30,
        DE.COMPONENT_WAVE,
        k=NUM_STATES,
        params={"lattice_window": 20, "small_max": WS.WAVE_SMALL_MAX,
                "normal_max": WS.WAVE_NORMAL_MAX},
    )
    assert step["ranking"] == expected
    assert step["picks"] == expected[:10]


# --------------------------------------------------------------------------- #
# 5. 稳定性：对半 / 三分 / 滚动窗 + 符号一致性
# --------------------------------------------------------------------------- #
def test_stability_block_halves_thirds_and_rolling_window_arithmetic() -> None:
    hits = [True] * 50 + [False] * 50
    block = WS.stability_block(hits, rolling=30)
    assert block["evaluated"] == 100
    assert block["baseline_rate"] == pytest.approx(K_DEFAULT / NUM_STATES)
    halves = block["halves"]
    assert [(row["start"], row["stop"], row["hits"], row["evaluated"]) for row in halves] == [
        (0, 50, 50, 50),
        (50, 100, 0, 50),
    ]
    assert halves[0]["lift"] == pytest.approx(1.0 - K_DEFAULT / NUM_STATES)
    assert halves[1]["lift"] == pytest.approx(-K_DEFAULT / NUM_STATES)
    thirds = block["thirds"]
    assert [row["hits"] for row in thirds] == [33, 17, 0]
    assert [row["evaluated"] for row in thirds] == [33, 33, 34]
    assert block["rolling_window"] == 30
    assert len(block["rolling"]) == 4  # ceil(100 / 30)
    assert [row["hits"] for row in block["rolling"]] == [30, 20, 0, 0]
    # 对半 2 段（+ / −）+ 三分 3 段（+ / + / −）：#正 / #非零 = 3 / 5 = 0.6
    assert block["sign_consistency"] == pytest.approx(0.6)
    assert block["data_status"] == DATA_STATUS_OK


def test_stability_block_flags_a_single_interval_spike() -> None:
    """所有命中都挤在第一个三分段：符号一致性低，正是「噪声而非信号」的形态。"""
    hits = [True] * 20 + [False] * 40
    block = WS.stability_block(hits, rolling=20)
    assert block["thirds"][0]["lift"] > 0
    assert block["thirds"][1]["lift"] < 0
    assert block["thirds"][2]["lift"] < 0
    assert block["sign_consistency"] < 1.0
    assert block["halves"][1]["hits"] == 0


def test_stability_block_on_empty_series_says_insufficient() -> None:
    block = WS.stability_block([])
    assert block["evaluated"] == 0
    assert block["halves"] == [] and block["thirds"] == [] and block["rolling"] == []
    assert block["sign_consistency"] is None
    assert block["data_status"] == DATA_STATUS_INSUFFICIENT


# --------------------------------------------------------------------------- #
# 6. 功效：两比例公式的独立重算 + 回归预测
# --------------------------------------------------------------------------- #
def test_power_arithmetic_recomputed_independently() -> None:
    block = WS.power_and_verdict(180, 48)
    normal = NormalDist()
    z_alpha = normal.inv_cdf(1.0 - WS.WAVE_ALPHA / 2.0)
    z_power = normal.inv_cdf(0.8)
    p0 = K_DEFAULT / NUM_STATES

    assert block["observed_delta"] == pytest.approx(48 / 180 - p0)
    assert block["break_even_rate"] == pytest.approx(K_DEFAULT / ODDS_DEFAULT)
    assert block["break_even_edge"] == pytest.approx(
        K_DEFAULT / ODDS_DEFAULT - K_DEFAULT / NUM_STATES
    )
    assert block["minimum_detectable_delta"] > block["observed_delta"]

    assert [row["required_draws_80_power"] for row in block["targets"]] == [
        345,
        535,
        17063,
    ]
    for target in block["targets"]:
        p1 = target["p1"]
        required = (
            z_alpha * math.sqrt(p0 * (1 - p0)) + z_power * math.sqrt(p1 * (1 - p1))
        ) ** 2 / (target["delta"] ** 2)
        assert target["required_draws_80_power"] == math.ceil(required)
        assert target["additional_draws_needed"] == target["required_draws_80_power"] - 180
        assert target["months_at_one_draw_per_day"] == pytest.approx(
            target["additional_draws_needed"] / 30.4375
        )
        assert f"= {required:.2f}" in target["arithmetic"]


def test_power_rejects_zero_or_negative_shift_and_empty_samples() -> None:
    assert WS.power_and_verdict(0, 0)["data_status"] == DATA_STATUS_INSUFFICIENT
    block = WS.power_and_verdict(200, 20)  # 观测率低于基线 → 只留「正 Δ」目标
    assert block["observed_delta"] < 0
    assert [row["delta"] for row in block["targets"]] == pytest.approx(
        [0.05, block["break_even_edge"]]
    )
    assert all(row["delta"] > 0 for row in block["targets"])


def test_regression_path_dilutes_a_noise_spike_back_to_baseline() -> None:
    block = WS.regression_path(180, 48, expected_best_z=1.817078184395247)
    p0 = K_DEFAULT / NUM_STATES
    rows = block["rows"]
    assert rows[0]["cumulative_rate_if_noise"] == pytest.approx(48 / 180)
    assert rows[0]["expected_best_of_grid_rate"] == pytest.approx(
        p0 + 1.817078184395247 * math.sqrt(p0 * (1 - p0) / 180)
    )
    rates = [row["cumulative_rate_if_noise"] for row in rows]
    best = [row["expected_best_of_grid_rate"] for row in rows]
    assert rates == sorted(rates, reverse=True)
    assert best == sorted(best, reverse=True)
    assert rates[-1] == pytest.approx((48 + 5000 * p0) / 5180)
    assert abs(rates[-1] - p0) < abs(rates[0] - p0)
    assert rows[-1]["total_evaluated"] == 5180
    assert block["observed_rate"] == pytest.approx(48 / 180)


def test_null_max_z_converts_the_null_max_mean_into_a_z_scale() -> None:
    p0 = K_DEFAULT / NUM_STATES
    expected = (46.56 - 180 * p0) / math.sqrt(180 * p0 * (1 - p0))
    assert WS.null_max_z(180, 46.56) == pytest.approx(expected)
    assert WS.null_max_z(0, 46.56) is None


# --------------------------------------------------------------------------- #
# 7. 确定性
# --------------------------------------------------------------------------- #
def test_shuffled_values_is_deterministic_and_preserves_the_multiset() -> None:
    series = series_of(120)
    first = WS.shuffled_values(series, WS.WAVE_SEED_BASE)
    assert first == WS.shuffled_values(series, WS.WAVE_SEED_BASE)
    assert sorted(first) == sorted(series)
    assert first != series
    assert first != WS.shuffled_values(series, WS.WAVE_SEED_BASE + 1)


def test_grid_hits_is_deterministic() -> None:
    series = series_of(60)
    variants = tiny_grid()
    assert WS.grid_hits(series, variants, warmup=15, k=5) == WS.grid_hits(
        series, variants, warmup=15, k=5
    )


def test_permutation_matrix_is_deterministic_and_self_describing() -> None:
    series = series_of(50)
    variants = tiny_grid()
    first = WS.permutation_matrix(series, variants, perms=3, warmup=12, k=3)
    second = WS.permutation_matrix(series, variants, perms=3, warmup=12, k=3)
    assert first == second
    assert first["perms"] == 3
    assert len(first["null"]) == 3
    assert first["seed_base"] == WS.WAVE_SEED_BASE
    assert first["warmup"] == 12 and first["k"] == 3
    assert first["variant_ids"] == [row["variant_id"] for row in variants]
    for row in first["null"]:
        assert set(row) == set(first["observed"])


def test_cli_permutation_worker_matches_the_single_process_path() -> None:
    series = series_of(50)
    variants = tiny_grid()
    CLI._init_grid(series, variants, 12, 3)
    worker = CLI._grid_worker(0)
    expected = WS.grid_hits(
        WS.shuffled_values(series, WS.WAVE_SEED_BASE + 0), variants, warmup=12, k=3
    )
    assert worker["index"] == 0
    assert worker["hits"] == expected
    pooled = CLI.collect_grid_null(series, variants, perms=2, warmup=12, k=3, jobs=1)
    assert [row for row in pooled["null"]] == [CLI._grid_worker(0)["hits"], CLI._grid_worker(1)["hits"]]
    assert pooled["meta"]["perms"] == 2
    assert pooled["meta"]["permutation_scheme"] == (
        "shuffle_special_number_positions_keep_period_and_date"
    )
    assert pooled["meta"]["seed_base"] == WS.WAVE_SEED_BASE
    assert CLI.collect_grid_null(series, variants, perms=0, warmup=12, k=3, jobs=1)["null"] == []


# --------------------------------------------------------------------------- #
# 8. 消融并排 + 机制说明（纯离线）
# --------------------------------------------------------------------------- #
def test_toggle_ablation_reports_both_sides_and_the_pick_churn() -> None:
    series = series_of(60)
    draws = draws_of(series)
    cfg = WS.audit_config()
    row = WS.toggle_ablation(
        draws,
        cfg,
        toggle={
            "handle": "engine_lattice",
            "on": {"lattice_enabled": True},
            "off": {"lattice_enabled": False},
        },
        label="audit_snapshot",
    )
    assert row["handle"] == "engine_lattice"
    assert row["on"]["evaluated"] == row["off"]["evaluated"] > 0
    assert row["positions"] == row["on"]["evaluated"]
    assert row["delta_hits"] == row["on"]["hits"] - row["off"]["hits"]
    assert row["settings_on"]["lattice_enabled"] is True
    assert row["settings_off"]["lattice_enabled"] is False
    assert 0 <= row["pick_set_identical_positions"] <= row["positions"]
    assert 0 <= row["hit_outcome_flips"] <= row["positions"]
    assert row["inert"] == (row["pick_set_identical_positions"] == row["positions"])
    assert sum(row["pick_wave_mix_on"].values()) == row["positions"] * K_DEFAULT
    assert "本池已导入" in row["scope"]


def test_ablation_side_by_side_runs_two_configs_and_keeps_the_code_path() -> None:
    series = series_of(60)
    draws = draws_of(series)
    payload = WS.ablation_side_by_side(
        draws,
        audit_settings=WS.audit_config(),
        live_settings=WS.config_from_settings(LIVE_SETTINGS),
    )
    assert set(payload["configs"]) == {WS.AUDIT_CONFIG_LABEL, WS.LIVE_CONFIG_LABEL}
    audit = payload["configs"][WS.AUDIT_CONFIG_LABEL]["settings"]
    live = payload["configs"][WS.LIVE_CONFIG_LABEL]["settings"]
    assert (audit["small_max"], audit["normal_max"]) == (10, 30)
    assert (live["small_max"], live["normal_max"]) == (15, 20)
    assert live["trend_bias"] == "mid"
    assert live["exclude_repeat_zodiac"] is True
    assert live["pick_count"] == K_DEFAULT and live["mode"] == "even"
    for label, entry in payload["configs"].items():
        assert set(entry["engine_feature_hits"]) == set(DA.ENGINE_FEATURE_HANDLES)
        for name in ("lattice", "repeat_zodiac"):
            assert entry[name]["label"] == label
    assert payload["code_path"]["pass_order"].startswith("services/lottery.py:")
    assert "硬门控" in payload["mechanism_note"]
    assert set(payload["toggles"]) == {"lattice", "repeat_zodiac"}


def test_config_from_settings_pins_k_and_mode_and_ignores_foreign_keys() -> None:
    cfg = WS.config_from_settings({**LIVE_SETTINGS, "pick_count": 3, "big_min": 21})
    assert cfg["pick_count"] == K_DEFAULT
    assert cfg["mode"] == "even"
    assert "big_min" not in cfg
    assert WS.config_from_settings(None)["small_max"] == 10


def test_audit_config_matches_the_audit_snapshot() -> None:
    cfg = WS.audit_config()
    assert cfg["small_max"] == 10 and cfg["normal_max"] == 30
    assert cfg["trend_bias"] == "neutral"
    assert cfg["exclude_repeat_zodiac"] is False
    assert cfg["pick_count"] == K_DEFAULT and cfg["mode"] == "even"
    assert WS.audit_config({"small_max": 15})["small_max"] == 15


def test_mechanism_note_points_at_the_real_gating_lines() -> None:
    note = WS.mechanism_note()
    assert "lattice_primary" in note
    assert "2183-2196" in note
    assert "trend_bias" in note
    path = WS.LATTICE_CODE_PATH
    assert set(path) >= {"band", "primary_wave", "pass_order", "round_robin", "pool"}
    for value in path.values():
        assert value.startswith(("services/lottery.py:", "services/pick_ticket.py:"))
    src = (BACKEND_ROOT / "services" / "lottery.py").read_text(encoding="utf-8").splitlines()
    for line in (993, 1166, 2183, 2197):
        assert src[line - 1].strip(), f"机制说明引用的行号 {line} 落空"


# --------------------------------------------------------------------------- #
# 9. 真实 210 期锚点（缺 backend/data/* 时整组 skip）
# --------------------------------------------------------------------------- #
def test_primary_variant_reproduces_the_audit_row() -> None:
    """主变体必须逐位复现审计里的 ``wave_lattice`` 行：180 期命中 48 次。"""
    draws = real_draws()
    numbers = [int(row["special_number"]) for row in draws]
    assert len(numbers) == 210
    variants = WS.variant_grid()
    observed = WS.grid_hits(numbers, variants, warmup=WS.WAVE_WARMUP, k=K_DEFAULT)
    assert observed[WS.PRIMARY_VARIANT_ID] == 48
    assert len(observed) == 87
    assert max(observed.values()) == 51
    assert max(observed, key=lambda key: observed[key]) == "density_band|w30|hot"


def test_grid_champion_is_inside_its_own_max_null_on_real_and_synthetic_data() -> None:
    """观测 max 不得低于同网格零分布的均值 —— 那说明零分布本身有问题。"""
    draws = real_draws()
    numbers = [int(row["special_number"]) for row in draws]
    variants = tiny_grid()
    observed = WS.grid_hits(numbers, variants, warmup=WS.WAVE_WARMUP, k=K_DEFAULT)
    null = [
        WS.grid_hits(WS.shuffled_values(numbers, WS.WAVE_SEED_BASE + index), variants,
                     warmup=WS.WAVE_WARMUP, k=K_DEFAULT)
        for index in range(5)
    ]
    block = WS.max_over_grid(observed, null)
    assert block["observed_max"] == max(observed.values())
    assert block["null_max"]["n"] == 5
    assert block["p_value"] == pytest.approx(
        (1 + sum(1 for row in null if max(row.values()) >= block["observed_max"])) / 6
    )


def test_real_pool_split_half_decays_like_noise() -> None:
    """主变体的超额命中集中在最早一段 —— 这是噪声的形态，不是持续信号。"""
    draws = real_draws()
    numbers = [int(row["special_number"]) for row in draws]
    variant = next(
        row for row in WS.variant_grid() if row["variant_id"] == WS.PRIMARY_VARIANT_ID
    )
    block = WS.variant_walk_forward(numbers, variant, warmup=WS.WAVE_WARMUP, k=K_DEFAULT)
    stability = WS.stability_block(block["hits_series"])
    halves = stability["halves"]
    assert block["hits"] == 48 and block["evaluated"] == 180
    assert halves[0]["hits"] == 29 and halves[1]["hits"] == 19
    assert halves[0]["lift"] > halves[1]["lift"]
    assert halves[1]["lift"] < 2 * stability["baseline_rate"] / 10  # 后半段几乎贴基线


# --------------------------------------------------------------------------- #
# 10. 生产行为不许漂移（真实 210 期 + 线上配置快照）
# --------------------------------------------------------------------------- #
def _real_draws_for_backtest() -> list[dict[str, object]]:
    draws = real_draws()
    return [
        {
            "period": int(row["period"]),
            "draw_date": str(row["draw_date"]),
            "special_number": int(row["special_number"]),
        }
        for row in draws
    ]


def _backtest(draws: list[dict[str, object]], **overrides: object) -> dict:
    cfg = {**WS.config_from_settings(LIVE_SETTINGS), **overrides}
    return A.backtest_stats(
        draws,
        base_settings=cfg,
        include_results=False,
        include_wave_breakdown=False,
    )


def test_live_snapshot_backtest_anchor_is_still_38_of_208() -> None:
    draws = _real_draws_for_backtest()
    body = _backtest(draws)
    assert (body["hits"], body["evaluated"]) == (38, 208)
    assert body["verdict"]["kind"] == "noise"
    assert body["hit_rate"] == pytest.approx(0.18269, abs=5e-5)


def test_lattice_toggle_changes_the_number_in_both_configs() -> None:
    """点阵是**硬门控**：两种配置下开 / 关都不是同一个号码集合，也都不 inert。"""
    draws = _real_draws_for_backtest()
    assert _backtest(draws, lattice_enabled=False)["hits"] == 56  # 线上配置：关掉反而更高
    # 审计快照原先靠代码默认继承 lattice_enabled=True；默认已改为 False（2026-10-07），
    # 为保留「ON vs OFF 同配置对照」，这里对 on 显式钉住 True（off 仍显式 False）。
    on = A.backtest_stats(
        draws,
        base_settings={**WS.audit_config(), "lattice_enabled": True},
        include_results=False,
        include_wave_breakdown=False,
    )
    off = A.backtest_stats(
        draws,
        base_settings={**WS.audit_config(), "lattice_enabled": False},
        include_results=False,
        include_wave_breakdown=False,
    )
    assert (on["hits"], off["hits"]) == (46, 48)  # 审计快照：开比关低 2 → 与审计行一致
    assert on["evaluated"] == off["evaluated"] == 208
    assert WS.audit_config()["pick_count"] == K_DEFAULT


def test_repeat_zodiac_toggle_is_hit_count_neutral_under_the_live_snapshot() -> None:
    """线上配置下避开重肖的净命中变化为 0（但选号集合并不相同，不是结构性无效）。"""
    draws = _real_draws_for_backtest()
    assert _backtest(draws, exclude_repeat_zodiac=False)["hits"] == 38
    assert _backtest(draws, exclude_repeat_zodiac=True)["hits"] == 38


def test_wave_modules_never_leak_into_production_paths() -> None:
    for relative in ("main.py", "services/lottery.py"):
        text = (BACKEND_ROOT / relative).read_text(encoding="utf-8")
        assert "wave_study" not in text, relative
        assert "wave_dedicated" not in text, relative
    for path in sorted((BACKEND_ROOT / "routers").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "wave_study" not in text, path.name
        assert "wave_dedicated" not in text, path.name


def test_audit_family_and_engine_handles_did_not_change() -> None:
    """23 条假设与 4 个引擎开关是上一轮的锚点：本轮不许改动它们。"""
    assert len(DA.ALL_HANDLES) == 23
    assert len(DA.ENGINE_FEATURE_HANDLES) == 4
    assert [row["handle"] for row in DA.engine_feature_definitions()] == list(
        DA.ENGINE_FEATURE_HANDLES
    )
    assert DA.HANDLE_WAVE_LATTICE in DA.ALL_HANDLES


# --------------------------------------------------------------------------- #
# 11. 口径 / 编码守卫
# --------------------------------------------------------------------------- #
def test_verdict_enums_are_english_and_labels_are_chinese_only_in_labels() -> None:
    for verdict in (WS.VERDICT_SURVIVES, WS.VERDICT_NO_SIGNAL, WS.VERDICT_INSUFFICIENT):
        assert verdict.isascii()
        assert WS.VERDICT_LABELS[verdict].strip()
    assert WS.VERDICT_NO_SIGNAL == "no_signal"
    assert WS.CLAIM_NO_EDGE == "NO_EDGE"
    assert set(WS.VERDICT_LABELS) == {
        WS.VERDICT_SURVIVES,
        WS.VERDICT_NO_SIGNAL,
        WS.VERDICT_INSUFFICIENT,
    }


def test_scope_note_and_labels_never_promote_the_pool_to_the_whole_market() -> None:
    note = WS.scope_note(210, 180)
    assert "本池已导入 210 期" in note
    assert "不涉及任何全市场数据" in note
    for banned in ("全市场涨停", "全市场最高", "必出", "稳赚", "保证中奖"):
        assert banned not in note
    assert "本池" in WS.WAVE_TOOL_LABELS["scope"]


def test_study_summary_is_honest_and_json_serialisable() -> None:
    observed = {"a": 12, "b": 9}
    null = [{"a": 8, "b": 4}, {"a": 7, "b": 6}, {"a": 12, "b": 1}]
    holm = WS.holm_family(observed, null)
    block = WS.max_over_grid(observed, null)
    summary = WS.study_summary(
        {
            "a": {"hits": 12, "evaluated": 10, "hit_rate": 1.2, "lift": 0.99,
                  "variant_id": "a"},
        },
        observed,
        holm,
        block,
        primary_variant_id="a",
    )
    assert summary["primary_variant"] == "a"
    assert summary["family_size"] == 2
    assert summary["claim"] == "NO_EDGE"
    assert isinstance(summary["anything_survives"], bool)
    assert summary["statement"]
    json.dumps(summary, ensure_ascii=False)


def test_variant_table_sorts_by_hits_and_carries_holm_columns() -> None:
    blocks = {
        "a": {"variant_id": "a", "hits": 12, "evaluated": 10, "hit_rate": 1.2,
              "lift": 0.99, "mean_rank": 20.0, "log_loss": 3.9, "brier": 0.02},
        "b": {"variant_id": "b", "hits": 3, "evaluated": 10, "hit_rate": 0.3,
              "lift": 0.1, "mean_rank": 25.0, "log_loss": 3.9, "brier": 0.02},
    }
    observed = {"a": 12, "b": 3}
    null = [{"a": 11, "b": 2}, {"a": 5, "b": 4}]
    table = WS.variant_table(blocks, WS.holm_family(observed, null))
    assert [row["variant_id"] for row in table] == ["a", "b"]
    assert "is_primary" in table[0]
    for row in table:
        assert row["p_value"] is not None and row["p_adjusted"] is not None
        assert row["verdict"] in WS.VERDICT_LABELS


def test_hit_distribution_summary_matches_hand_computed_values() -> None:
    block = WS.uniformity_of_variant_hits([5, 1, 3, 9, 7])
    assert block["n"] == 5
    assert block["min"] == 1 and block["max"] == 9
    assert block["mean"] == pytest.approx(5.0)
    assert block["p25"] == 3 and block["p50"] == 5 and block["p75"] == 7
    assert block["sd"] == pytest.approx(math.sqrt(8.0))
    assert WS.uniformity_of_variant_hits([])["data_status"] == DATA_STATUS_INSUFFICIENT


def test_period_span_reads_the_real_pool_metadata() -> None:
    draws = real_draws()
    span = WS.period_span(draws)
    assert (span["first_period"], span["last_period"]) == (70, 279)
    assert span["first_date"] == "2026-03-11"
    assert span["last_date"] == "2026-10-06"
    assert span["span_days"] == 209
    assert span["draws_per_day"] == pytest.approx(210 / 209)


def test_source_files_are_utf8_without_bom_or_replacement_char() -> None:
    for relative in WAVE_SOURCES + ("tests/test_wave_study.py",):
        raw = (BACKEND_ROOT / relative).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{relative} 带 BOM"
        assert b"\xef\xbf\xbd" not in raw, f"{relative} 存在 U+FFFD 替换字符"
        raw.decode("utf-8")


def test_wave_study_is_a_pure_module_with_no_io_dependencies() -> None:
    text = (BACKEND_ROOT / "services" / "wave_study.py").read_text(encoding="utf-8")
    for banned in (
        "import sqlalchemy",
        "from database",
        "from routers",
        "from main",
        "urllib",
        "open(",
    ):
        assert banned not in text, f"wave_study.py 出现不该有的依赖：{banned}"


# --------------------------------------------------------------------------- #
# 12. CLI 编排
# --------------------------------------------------------------------------- #
def test_cli_help_lists_the_four_subcommands_and_the_plain_question() -> None:
    parser = CLI.build_parser()
    text = parser.format_help()
    for command in ("study", "matrix", "ablation", "explain"):
        assert command in text
    assert "波动法" in text
    assert "也不行吗" in text
    assert "本池" in text
    assert "--live-settings-json" in text  # 离线优先：线上快照走文件


def test_cli_explain_runs_without_any_draw_file(tmp_path: Path) -> None:
    out = tmp_path / "explain.json"
    assert CLI.main(["explain", "--out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["claim"] == "NO_EDGE"
    assert payload["variant_grid_size"] == 87
    assert len(payload["variant_grid"]) == 87
    assert payload["break_even_rate"] == pytest.approx(K_DEFAULT / ODDS_DEFAULT)
    assert "max 零分布" in payload["text"]
    for banned in ("全市场涨停", "全市场最高", "必出", "稳赚", "保证中奖"):
        assert banned not in payload["text"]
    assert "10/47=21.28%" in payload["text"]
    assert "345 期" in payload["text"]
    assert "17,000 期" in payload["text"]


def test_cli_study_writes_the_full_artifact(tmp_path: Path) -> None:
    series = series_of(50)
    draws_path = tmp_path / "draws.json"
    draws_path.write_text(json.dumps(draws_of(series), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "wave_study.json"

    assert CLI.main(
        [
            "study",
            "--draws-json",
            str(draws_path),
            "--out",
            str(out),
            "--perms",
            "2",
            "--jobs",
            "1",
            "--warmup",
            "20",
        ]
    ) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["command"] == "study"
    assert payload["claim"] == "NO_EDGE"
    assert payload["preregistration"]["family_size"] == 87
    assert len(payload["variant_table"]) == 87
    assert len(payload["holm_rows"]) == 87
    assert payload["holm"]["correction"] == "holm"
    assert payload["max_over_grid"]["observed_max"] == max(
        payload["variant_hits_observed"].values()
    )
    assert payload["max_over_grid_null"]["n"] == 2
    assert payload["null_meta"]["perms"] == 2
    assert payload["stability"]["primary"]["evaluated"] == len(series) - 20
    assert payload["walk_forward"]["primary"]["variant_id"] == WS.PRIMARY_VARIANT_ID
    assert payload["walk_forward"]["reference"]["mean_rank"] == pytest.approx(25.0)
    assert payload["power"]["targets"]
    assert payload["regression"]["rows"]
    assert payload["model_family"]["distinct_probability_models"] == 21
    assert payload["input"]["sample_size"] == len(series)
    assert len(payload["input"]["numbers_sha256"]) == 64
    assert "ablation" not in payload  # 默认不跑消融（保持纯离线）
    for row in payload["variant_table"]:
        assert row["variant_id"].isascii()
        assert row["verdict"] in WS.VERDICT_LABELS
    for row in payload["variant_table"]:
        if row["p_adjusted"] is not None:
            assert 0.0 < row["p_value"] <= 1.0
            assert 0.0 < row["p_adjusted"] <= 1.0


def test_cli_ablation_runs_offline_with_a_saved_settings_snapshot(tmp_path: Path) -> None:
    series = series_of(60)
    draws_path = tmp_path / "draws.json"
    draws_path.write_text(json.dumps(draws_of(series), ensure_ascii=False), encoding="utf-8")
    settings_path = tmp_path / "live_settings.json"
    settings_path.write_text(json.dumps(LIVE_SETTINGS, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "wave_ablation.json"

    assert CLI.main(
        [
            "ablation",
            "--draws-json",
            str(draws_path),
            "--out",
            str(out),
            "--live-settings-json",
            str(settings_path),
        ]
    ) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["command"] == "ablation"
    assert payload["ablation"]["live_settings_source"].startswith("file:")
    for label, entry in payload["ablation"]["configs"].items():
        assert entry["lattice"]["positions"] == entry["repeat_zodiac"]["positions"]
    assert "硬门控" in payload["mechanism_note"]


def test_cli_ablation_falls_back_to_the_audit_config_without_a_snapshot(
    tmp_path: Path,
) -> None:
    series = series_of(50)
    draws_path = tmp_path / "draws.json"
    draws_path.write_text(json.dumps(draws_of(series), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "wave_ablation.json"
    assert CLI.main(
        ["ablation", "--draws-json", str(draws_path), "--out", str(out)]
    ) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ablation"]["live_settings_source"] == "none"
    assert set(payload["ablation"]["configs"]) == {WS.AUDIT_CONFIG_LABEL}


def test_cli_settings_snapshot_accepts_flat_and_nested_payloads() -> None:
    assert CLI._settings_from_payload({"small_max": 15})["small_max"] == 15
    assert CLI._settings_from_payload({"settings": {"small_max": 15}})["small_max"] == 15
    assert CLI._settings_from_payload({"data": {}}) == {"data": {}}
    assert CLI._settings_from_payload(None) == {}
    assert CLI._settings_from_payload("nope") == {}


def test_cli_default_out_paths_live_under_the_ignored_tmp_dir() -> None:
    resolved = CLI.resolve_out(None, "wave_study.json")
    assert ".tmp-wave" in resolved
    assert CLI.resolve_out("custom.json", "wave_study.json") == "custom.json"


def test_cli_numbers_digest_is_stable_and_input_sensitive() -> None:
    assert CLI.numbers_digest([1, 2, 3]) == CLI.numbers_digest([1, 2, 3])
    assert CLI.numbers_digest([1, 2, 3]) != CLI.numbers_digest([3, 2, 1])
    assert len(CLI.numbers_digest([1, 2, 3])) == 64
