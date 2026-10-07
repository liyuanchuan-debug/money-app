"""``services.max_fit`` / ``scripts.max_fit_analysis`` 的纯逻辑与口径守卫测试。

只测**纯逻辑**：不连数据库、不读 ``backend/data/*``（该目录被 .gitignore 忽略），
所有开奖序列都在测试内确定性合成（固定 seed 的伪随机 + 少量手工构造）。

守卫要点：
- 每个模型都必须吐出 1..49 的**完整排序**（不只是 top-k），否则 ``mean_rank`` 无法度量；
- **严格 walk-forward 无前视**：改动「目标期及其之后」的数据，不得改动该期的预测；
- **内层选参不碰评估窗**：只改评估段，选出的超参数必须逐位相同；
- 记忆化可把样本内刷到完美（命中 100% / 平均排名 1.0），样本外则**逐位退化成均匀基线**；
- 正则化 → 0 时样本内拟合趋于完美、样本外反而更差；
- ``mean_rank`` / ``log_loss`` / ``Brier`` 对齐均匀参考（25.0 / ln 49 / (1/49)(1−1/49)）；
- **导入守卫**：线上推荐路径（``services/lottery.py``、``routers/*``、``main.py``）
  一律不得引用 ``max_fit``；
- 口径与编码守卫：源码不得出现「全市场」与替换字符 U+FFFD。
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta
from pathlib import Path

import pytest

from scripts import max_fit_analysis as MA
from services import max_fit as MF

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALL_MODELS = list(MF.MODEL_IDS)


# --------------------------------------------------------------------------- #
# 合成样本
# --------------------------------------------------------------------------- #
def series_of(count: int = 120, seed: int = 20261007) -> list[int]:
    """固定 seed 的确定性伪随机开奖序列（1..49）；同一 seed ⇒ 同一序列。"""
    rng = random.Random(seed)
    return [rng.randrange(MF.NUMBER_MIN, MF.NUMBER_MAX + 1) for _ in range(count)]


def dates_of(count: int) -> list[date]:
    start = date(2025, 1, 1)
    return [start + timedelta(days=index) for index in range(count)]


# --------------------------------------------------------------------------- #
# 排序完备性 / 确定性 / 严格 walk-forward
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model", ALL_MODELS)
def test_every_model_emits_a_full_permutation_of_1_to_49(model: str) -> None:
    series = series_of(80)
    index = 40
    prediction = MF.predict_walk_forward(model, series, index, MF.K_DEFAULT)

    ranking = prediction["ranking"]
    assert sorted(ranking) == list(range(MF.NUMBER_MIN, MF.NUMBER_MAX + 1))
    assert len(set(ranking)) == MF.NUM_STATES
    # top-k 必须是排序的前 k 个（不是另算的一套集合）
    assert prediction["picks"] == ranking[: MF.K_DEFAULT]
    # ranks 与 ranking 互为逆映射，取遍 1..49
    assert sorted(prediction["ranks"].values()) == list(range(1, MF.NUM_STATES + 1))
    for position, number in enumerate(ranking, start=1):
        assert prediction["ranks"][number] == position
    # 概率是 1..49 上的合法分布
    probabilities = prediction["probabilities"]
    assert sorted(probabilities) == list(range(MF.NUMBER_MIN, MF.NUMBER_MAX + 1))
    assert all(value > 0.0 for value in probabilities.values())
    assert sum(probabilities.values()) == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize("model", ALL_MODELS)
def test_predictions_are_deterministic_under_fixed_seed(model: str) -> None:
    series = series_of(80)
    first = MF.predict_walk_forward(model, series, 35, MF.K_DEFAULT)
    second = MF.predict_walk_forward(model, series, 35, MF.K_DEFAULT)
    assert first["ranking"] == second["ranking"]
    assert first["picks"] == second["picks"]
    assert first["probabilities"] == second["probabilities"]


@pytest.mark.parametrize("model", ALL_MODELS)
def test_walk_forward_ignores_everything_at_or_after_the_target(model: str) -> None:
    """改动目标期及其之后的数据，目标期的样本外预测必须逐位不变（无未来函数）。"""
    series = series_of(90)
    index = 30
    baseline = MF.predict_walk_forward(model, series, index, MF.K_DEFAULT)

    # 把目标期本身与全部未来期都换成别的号（前缀 series[:index] 保持不变）
    mutated = list(series)
    for position in range(index, len(mutated)):
        mutated[position] = (mutated[position] % MF.NUM_STATES) + 1
    assert mutated[:index] == series[:index]
    assert mutated[index] != series[index]

    changed = MF.predict_walk_forward(model, mutated, index, MF.K_DEFAULT)
    assert changed["ranking"] == baseline["ranking"]
    assert changed["picks"] == baseline["picks"]
    assert changed["probabilities"] == baseline["probabilities"]


def test_walk_forward_evaluate_rows_are_frozen_in_the_past() -> None:
    """逐期评估同样无前视：改未来只影响未来那几行，早期行逐位不变。"""
    series = series_of(90)
    cut = 45
    mutated = list(series)
    for position in range(cut, len(mutated)):
        mutated[position] = (mutated[position] + 7) % MF.NUM_STATES + 1

    before = MF.evaluate(MF.MODEL_MARKOV_1, series, MF.K_DEFAULT, mode="walk_forward")
    after = MF.evaluate(MF.MODEL_MARKOV_1, mutated, MF.K_DEFAULT, mode="walk_forward")
    frozen_before = [row for row in before["rows"] if row["index"] < cut]
    frozen_after = [row for row in after["rows"] if row["index"] < cut]
    assert frozen_before == frozen_after
    # 至少有一行确实变了，否则这个测试是空转
    moved_before = [row for row in before["rows"] if row["index"] >= cut]
    moved_after = [row for row in after["rows"] if row["index"] >= cut]
    assert moved_before != moved_after


# --------------------------------------------------------------------------- #
# 记忆化：样本内完美，样本外退化为均匀基线
# --------------------------------------------------------------------------- #
def test_memoriser_reaches_perfect_in_sample_fit() -> None:
    series = series_of(120)
    evaluation = MF.evaluate(MF.MODEL_MEMORIZE, series, MF.K_DEFAULT, mode="in_sample")
    assert evaluation["evaluated"] == len(series) - MF.WARMUP
    assert evaluation["hit_rate"] == 1.0
    assert evaluation["mean_rank"] == 1.0
    assert evaluation["hits"] == evaluation["evaluated"]
    assert evaluation["rank_buckets"]["top_k"] == 1.0
    # 概率下限 1e-6 ⇒ log-loss 与 Brier 都压到数值零附近，但不是报 0 骗人
    assert 0.0 <= evaluation["log_loss"] < 1e-5
    assert 0.0 <= evaluation["brier"] < 1e-10


def test_memoriser_out_of_sample_is_bit_identical_to_uniform_baseline() -> None:
    """样本外没有可查的表项 ⇒ 退化为均匀排序，与 UNIFORM 模型逐位相同。"""
    series = series_of(120)
    memoriser = MF.evaluate(MF.MODEL_MEMORIZE, series, MF.K_DEFAULT, mode="walk_forward")
    uniform = MF.evaluate(MF.MODEL_UNIFORM, series, MF.K_DEFAULT, mode="walk_forward")
    assert memoriser["rows"] == uniform["rows"]
    assert memoriser["hit_rate"] == uniform["hit_rate"]
    assert memoriser["mean_rank"] == uniform["mean_rank"]
    assert memoriser["log_loss"] == pytest.approx(MF.UNIFORM_LOG_LOSS, abs=1e-12)
    assert memoriser["brier"] == pytest.approx(MF.UNIFORM_BRIER, abs=1e-15)


# --------------------------------------------------------------------------- #
# 内层选参只看训练窗
# --------------------------------------------------------------------------- #
def test_inner_selection_does_not_touch_the_evaluation_window() -> None:
    series = series_of(120)
    fit = MF.fit_hyperparameters(series, MF.K_DEFAULT)
    split = fit["split_index"]
    assert 0 < split < len(series)

    mutated = list(series)
    for position in range(split, len(mutated)):
        mutated[position] = (mutated[position] + 11) % MF.NUM_STATES + 1
    mutated_fit = MF.fit_hyperparameters(mutated, MF.K_DEFAULT)

    assert mutated_fit["split_index"] == split
    for model in ALL_MODELS:
        assert mutated_fit["models"][model].get("params") == fit["models"][model].get("params")
    # 有超参数的模型确实选出了参数（否则上面的相等是空转）
    assert fit["models"][MF.MODEL_NGRAM_SMOOTHED]["params"]
    assert fit["models"][MF.MODEL_REGULARIZED_BEST_FIT]["params"]
    assert fit["models"][MF.MODEL_LATTICE_FIT]["params"]


def test_insufficient_data_is_reported_not_zero_filled() -> None:
    tiny = [7, 11, 13]
    fit = MF.fit_hyperparameters(tiny, MF.K_DEFAULT)
    assert fit["data_status"] == "INSUFFICIENT"
    assert fit["data_status_label"] == "数据不足"

    evaluation = MF.evaluate(MF.MODEL_MEMORIZE, [7, 11], MF.K_DEFAULT, mode="walk_forward")
    assert evaluation["evaluated"] == 0
    assert evaluation["hit_rate"] is None
    assert evaluation["mean_rank"] is None
    assert evaluation["data_status"] == "INSUFFICIENT"
    assert evaluation["data_status_label"] == "数据不足"


# --------------------------------------------------------------------------- #
# 指标：手工核对
# --------------------------------------------------------------------------- #
def test_mean_rank_hand_computed_cases() -> None:
    assert MF.mean_rank([]) is None
    assert MF.mean_rank([1]) == 1.0
    assert MF.mean_rank([25]) == 25.0
    assert MF.mean_rank([1, 2, 3]) == pytest.approx(2.0)
    assert MF.mean_rank([1, 49]) == pytest.approx(MF.UNIFORM_MEAN_RANK)


def test_rank_bucket_shares_boundaries() -> None:
    shares = MF.rank_bucket_shares([1, 10, 11, 25, 26, 49], MF.K_DEFAULT)
    assert shares == pytest.approx({"top_k": 2 / 6, "mid": 2 / 6, "bottom": 2 / 6})
    assert MF.rank_bucket_shares([], MF.K_DEFAULT) is None


def test_rank_bucket_boundary_at_k_and_midpoint() -> None:
    # k=10：1..10 为 top；11..25 为 mid；26..49 为 bottom
    assert MF.rank_bucket_shares([10, 25, 26], 10) == pytest.approx(
        {"top_k": 1 / 3, "mid": 1 / 3, "bottom": 1 / 3}
    )


def test_log_loss_hand_computed_values() -> None:
    uniform = MF.uniform_probabilities()
    assert MF.log_loss(uniform, 7) == pytest.approx(math.log(MF.NUM_STATES))

    assert MF.log_loss({1: 1.0}, 1) == pytest.approx(0.0)
    assert MF.log_loss({1: 0.25, 2: 0.75}, 2) == pytest.approx(-math.log(0.75))
    assert MF.log_loss({1: 0.0}, 1) == math.inf


def test_brier_hand_computed_values() -> None:
    uniform = MF.uniform_probabilities()
    assert MF.brier_score(uniform, 7) == pytest.approx(MF.UNIFORM_BRIER)

    # 点质量分布：缺号按概率 0 计 ⇒ p(actual)=1 时 Brier = 0（不是 48/49）
    assert MF.brier_score({1: 1.0}, 1) == pytest.approx(0.0)
    # {1: 0.25, 2: 0.75}, actual=2 ⇒ (1/49)·(0.25² + 0.25²) = 0.125/49
    assert MF.brier_score({1: 0.25, 2: 0.75}, 2) == pytest.approx(0.125 / 49.0)
    # 均匀分布下把 actual 之外 48 个号各计 (1/49)² ⇒ 48/49² 之和 = (1/49)(1−1/49)
    hand = (1.0 / 49.0) * sum(
        ((1.0 / 49.0) - (1.0 if number == 7 else 0.0)) ** 2 for number in range(1, 50)
    )
    assert MF.brier_score(uniform, 7) == pytest.approx(hand)


def test_uniform_reference_identities() -> None:
    assert MF.UNIFORM_PROB == pytest.approx(1.0 / 49.0)
    assert MF.UNIFORM_MEAN_RANK == 25.0
    assert MF.UNIFORM_LOG_LOSS == pytest.approx(math.log(49))
    assert MF.UNIFORM_BRIER == pytest.approx((1.0 / 49.0) * (1.0 - 1.0 / 49.0))
    assert sum(MF.uniform_probabilities().values()) == pytest.approx(1.0, abs=1e-12)
    evaluation = MF.evaluate(MF.MODEL_UNIFORM, series_of(60), MF.K_DEFAULT, mode="walk_forward")
    # 均匀排序的排名期望是 25；用的是有限样本，留出抽样误差（sd≈1.9）
    assert abs(evaluation["mean_rank"] - MF.UNIFORM_MEAN_RANK) < 8.0
    # 逐期密度指标与期号无关 ⇒ 必须精确等于均匀参考
    assert evaluation["log_loss"] == pytest.approx(MF.UNIFORM_LOG_LOSS, abs=1e-12)
    assert evaluation["brier"] == pytest.approx(MF.UNIFORM_BRIER, abs=1e-15)


def test_uniform_ranking_is_a_permutation_and_shift_stable() -> None:
    for index in range(12):
        ranking = MF.uniform_ranking(index)
        assert sorted(ranking) == list(range(1, MF.NUM_STATES + 1))
    # 步长 10 与 49 互质 ⇒ 相邻期号的 top-k 集合会平移（不是固定 {1..10}）
    assert MF.uniform_ranking(0)[: MF.K_DEFAULT] != MF.uniform_ranking(1)[: MF.K_DEFAULT]
    assert len(set(MF.uniform_ranking(1)[: MF.K_DEFAULT])) == MF.K_DEFAULT


def test_probabilities_from_weights_normalises_and_keeps_floor() -> None:
    probabilities = MF.probabilities_from_weights({3: 2.0, 7: 1.0})
    assert sum(probabilities.values()) == pytest.approx(1.0, abs=1e-12)
    assert probabilities[3] > probabilities[7] > probabilities[11]
    assert min(probabilities.values()) >= MF.PROBABILITY_FLOOR / MF.NUM_STATES

    # 空 / 全零 ⇒ 均匀兜底（不是除零，也不是空分布）
    for empty in ({}, {5: 0.0}, {5: -1.0}):
        assert MF.probabilities_from_weights(empty) == pytest.approx(
            MF.uniform_probabilities()
        )


def test_ranking_from_probabilities_tie_break_is_deterministic() -> None:
    flat = MF.uniform_probabilities()
    assert MF.ranking_from_probabilities(flat, tie_break=MF.uniform_ranking(3)) == (
        MF.uniform_ranking(3)
    )
    assert MF.ranking_from_probabilities(flat) == list(range(1, MF.NUM_STATES + 1))


# --------------------------------------------------------------------------- #
# 模型规格
# --------------------------------------------------------------------------- #
def test_model_spec_free_parameter_counts() -> None:
    assert MF.model_spec(MF.MODEL_UNIFORM)["free_parameters"] == 0
    assert MF.model_spec(MF.MODEL_MARKOV_1)["free_parameters"] == 49 * 48
    assert MF.model_spec(MF.MODEL_MARKOV_2)["free_parameters"] == 49**3 - 49**2
    assert MF.model_spec(MF.MODEL_NGRAM_SMOOTHED)["free_parameters"] == 49**3 - 49**2 + 1
    assert MF.model_spec(MF.MODEL_REGULARIZED_BEST_FIT)["free_parameters"] == 49**3 - 49**2 + 2
    assert MF.model_spec(MF.MODEL_LATTICE_FIT)["free_parameters"] == 2
    memorize = MF.model_spec(MF.MODEL_MEMORIZE)
    assert memorize["dynamic_free_parameters"] is True
    assert memorize["free_parameters"] is None  # 参数数 = 被拟合的期数，运行期才知道
    for model in ALL_MODELS:
        spec = MF.model_spec(model)
        assert spec["label"] and spec["family"] and spec["desc"]
    with pytest.raises(ValueError):
        MF.model_spec("NOT_A_MODEL")


def test_lattice_band_is_fitted_and_coverage_one_covers_everything() -> None:
    series = series_of(120)
    band = MF.fit_lattice_band(series, coverage=1.0)
    diffs = [series[index] - series[index - 1] for index in range(1, len(series))]
    assert band["samples"] == len(diffs)
    assert band["fitted_coverage"] == 1.0
    assert all(abs(diff - band["offset"]) <= band["half_width"] for diff in diffs)

    half = MF.fit_lattice_band(series, coverage=0.5)
    assert 0.4 <= half["fitted_coverage"] <= 0.6
    assert half["half_width"] <= band["half_width"]
    assert MF.fit_lattice_band([], coverage=1.0)["fitted_coverage"] is None


def test_honesty_flag_and_disclaimer_on_every_return() -> None:
    assert MF.FIT_ROLE == "FIT_ONLY"
    prediction = MF.predict_walk_forward(MF.MODEL_MARKOV_1, series_of(40), 20, MF.K_DEFAULT)
    assert prediction["fit_role"] == MF.FIT_ROLE
    assert "不是预测" in prediction["disclaimer"]
    assert any("禁止进入线上推荐路径" in note for note in prediction["notes"])

    evaluation = MF.evaluate(MF.MODEL_MARKOV_1, series_of(40), MF.K_DEFAULT)
    assert evaluation["fit_role"] == MF.FIT_ROLE
    assert evaluation["disclaimer"] == MF.DISCLAIMER

    fit = MF.fit_hyperparameters(series_of(40), MF.K_DEFAULT)
    assert fit["fit_role"] == MF.FIT_ROLE


# --------------------------------------------------------------------------- #
# 分析脚本的纯逻辑
# --------------------------------------------------------------------------- #
def test_analysis_metrics_block_shape() -> None:
    series = series_of(70)
    fit, evaluation = MA.fit_and_evaluate(series, MF.K_DEFAULT, MF.WARMUP)
    for model in ALL_MODELS:
        entry = evaluation[model]
        assert set(entry) == {"params", "in_sample", "walk_forward"}
        for mode in ("in_sample", "walk_forward"):
            metrics = entry[mode]
            assert metrics["evaluated"] == len(series) - MF.WARMUP
            assert 0.0 <= metrics["hit_rate"] <= 1.0
            assert 1.0 <= metrics["mean_rank"] <= float(MF.NUM_STATES)
            assert metrics["log_loss"] >= 0.0
            assert metrics["brier"] >= 0.0
            if mode == "walk_forward":
                # 样本外都该在均匀期望 25 附近（有限样本留出抽样余量）
                assert abs(metrics["mean_rank"] - MF.UNIFORM_MEAN_RANK) < 10.0
    # 记忆化：样本内完美
    assert evaluation[MF.MODEL_MEMORIZE]["in_sample"]["hit_rate"] == 1.0
    # 排序按样本内拟合降序
    rows = []
    for model in ALL_MODELS:
        spec = MF.model_spec(model)
        rows.append(
            {
                "model": model,
                "in_sample": evaluation[model]["in_sample"],
            }
        )
    ordered = sorted(rows, key=MA._sort_key)
    assert ordered[0]["model"] == MF.MODEL_MEMORIZE
    assert fit["split_index"] > 0


def test_regularisation_sweep_zero_beats_fit_and_loses_out_of_sample() -> None:
    series = series_of(120)
    sweep = MA.regularization_sweep(series, MF.K_DEFAULT, MF.WARMUP)
    rows = sweep["rows"]
    assert rows[0]["alpha"] == 0.0
    assert rows[-1]["alpha"] == 100.0
    # α → 0 退化为纯记忆化：样本内 log-loss 压到 0 附近
    assert rows[0]["in_sample"]["log_loss"] < 1e-5
    assert rows[0]["in_sample"]["mean_rank"] == 1.0
    # 但样本外反而比强正则化（α=100，几乎均匀）更差 —— 拟合越强，样本外越糟
    assert rows[0]["walk_forward"]["log_loss"] > rows[-1]["walk_forward"]["log_loss"]
    # 样本内 log-loss 随 α 单调上升（正则化越强，拟合越差）
    in_sample_losses = [row["in_sample"]["log_loss"] for row in rows]
    assert in_sample_losses == sorted(in_sample_losses)


def test_lattice_block_reports_coverage_sweep() -> None:
    series = series_of(120)
    fit = MF.fit_hyperparameters(series, MF.K_DEFAULT)
    block = MA.lattice_block(series, MF.K_DEFAULT, MF.WARMUP, fit)
    assert block["max_fit_band"]["fitted_coverage"] == 1.0
    assert block["train_diffs"] > 0 and block["out_of_sample_diffs"] > 0
    assert len(block["coverage_sweep"]) == len(MF.LATTICE_COVERAGE_GRID)
    coverages = [row["coverage"] for row in block["coverage_sweep"]]
    assert coverages == sorted(coverages)
    for row in block["coverage_sweep"]:
        assert 0.0 <= row["train_coverage"] <= 1.0
        assert 0.0 <= row["out_of_sample_coverage"] <= 1.0
        assert row["walk_forward"]["hit_rate"] is not None
    # 覆盖分位 = 1.0 时样本内覆盖率必然 100%（半宽被撑到盖住所有训练差值）
    full = block["coverage_sweep"][-1]
    assert full["coverage"] == 1.0
    assert full["train_coverage"] == 1.0


def test_summarize_metric_is_directional() -> None:
    # 命中率：越大越好 ⇒ 观测值越大 p 越小
    null_rates = [0.18, 0.20, 0.22]
    weak = MA._summarize_metric(null_rates, 0.19, lower_is_better=False)
    strong = MA._summarize_metric(null_rates, 0.23, lower_is_better=False)
    assert strong["p_one_sided"] < weak["p_one_sided"]
    assert strong["p_one_sided"] == pytest.approx(1 / 4)

    # 平均排名：越小越好 ⇒ 取负号后方向应反转
    null_ranks = [25.0, 26.0, 27.0]
    good = MA._summarize_metric(null_ranks, 24.0, lower_is_better=True)
    bad = MA._summarize_metric(null_ranks, 28.0, lower_is_better=True)
    assert good["p_one_sided"] == pytest.approx(1 / 4)
    assert bad["p_one_sided"] == pytest.approx(1.0)
    assert good["mean"] == pytest.approx(-26.0)  # 取负号后的零分布均值


def test_best_fit_picks_examples_are_concrete_and_complete() -> None:
    series = series_of(80)
    dates = dates_of(len(series))
    periods = [100 + index for index in range(len(series))]
    block = MA.best_fit_picks(
        series, dates, periods, MF.MODEL_MEMORIZE, {}, MF.K_DEFAULT, MF.WARMUP, limit=3
    )
    assert block["model"] == MF.MODEL_MEMORIZE
    assert len(block["examples"]) == 3
    for example in block["examples"]:
        assert example["period"] in periods
        assert example["draw_date"] in {value.isoformat() for value in dates}
        assert example["actual"] == series[periods.index(example["period"])]
        assert len(example["in_sample_top_k"]) == MF.K_DEFAULT
        assert len(set(example["in_sample_top_k"])) == MF.K_DEFAULT
        assert len(example["walk_forward_top_k"]) == MF.K_DEFAULT
        # 记忆化的样本内排名恒为 1：它把答案排在第一位
        assert example["in_sample_rank"] == 1
        assert example["in_sample_top_k"][0] == example["actual"]


def test_band_coverage_helper() -> None:
    band = {"offset": 0, "half_width": 2, "samples": 2}
    assert MA._band_coverage([0, 1, 2], band) == 1.0
    assert MA._band_coverage([0, 3], band) == 0.5
    assert MA._band_coverage([], band) is None
    assert MA._band_coverage([1, 2], {"offset": 0, "half_width": 1, "samples": 0}) is None


# --------------------------------------------------------------------------- #
# 导入守卫 / 口径守卫 / 编码守卫
# --------------------------------------------------------------------------- #
LIVE_PATH_FILES = ["main.py", "services/lottery.py"]


def _live_path_sources() -> list[tuple[Path, str]]:
    sources: list[tuple[Path, str]] = []
    for relative in LIVE_PATH_FILES:
        path = BACKEND_ROOT / relative
        if path.exists():
            sources.append((path, path.read_text(encoding="utf-8")))
    routers = BACKEND_ROOT / "routers"
    if routers.is_dir():
        for path in sorted(routers.rglob("*.py")):
            sources.append((path, path.read_text(encoding="utf-8")))
    return sources


def test_live_recommendation_path_never_references_max_fit() -> None:
    sources = _live_path_sources()
    assert sources, "线上路径源码一个都没读到，守卫会静默失效"
    assert any(path.name == "lottery.py" for path, _ in sources)
    for path, text in sources:
        assert "max_fit" not in text, f"{path.name} 引用了 max_fit，违反「仅拟合、不接入线上」铁律"


def test_max_fit_is_not_wired_into_lottery_module() -> None:
    import services.lottery as lottery  # 局部导入：确保线上模块可正常加载

    assert not hasattr(lottery, "max_fit")
    assert not [name for name in vars(lottery) if "max_fit" in name]


def test_edited_sources_have_no_market_wide_scope_claim() -> None:
    """**输出的文案**禁止把本池样本内统计升格为更大口径的结论（仓库口径铁律）。

    检查的是真正会落到 JSON / 前端 / 推送的可读文案（label、desc、disclaimer、
    notes），而不是源码里那句「禁止写全市场」的口径声明本身。
    """
    artifacts: dict[str, str] = {
        "DISCLAIMER": MF.DISCLAIMER,
        "FIT_ONLY_NOTE": MF._FIT_ONLY_NOTE,
        "FIT_ROLE_LABEL": MF.FIT_ROLE_LABEL,
    }
    for model in ALL_MODELS:
        spec = MF.model_spec(model)
        artifacts[f"{model}.label"] = spec["label"]
        artifacts[f"{model}.family"] = spec["family"]
        artifacts[f"{model}.desc"] = spec["desc"]

    series = series_of(40)
    prediction = MF.predict_walk_forward(MF.MODEL_MARKOV_1, series, 20, MF.K_DEFAULT)
    artifacts["predict.notes"] = " ".join(prediction["notes"])
    evaluation = MF.evaluate(MF.MODEL_MARKOV_1, series, MF.K_DEFAULT)
    artifacts["evaluate.notes"] = " ".join(evaluation["notes"])
    fit = MF.fit_hyperparameters(series, MF.K_DEFAULT)
    artifacts["fit.notes"] = " ".join(fit["notes"])

    for name, text in artifacts.items():
        for banned in ("全市场", "全量", "市场高度"):
            assert banned not in text, f"{name} 文案出现「{banned}」：本池没有该口径的数据"


PROHIBITION_MARKERS = ("禁止", "不得", "没有", "不是", "仅限", "只针对")


def test_source_mentions_of_market_scope_are_only_prohibitions() -> None:
    """源码里凡出现「全市场」，必须身处一句**禁令**，不能是结论。"""
    for relative in ("services/max_fit.py", "scripts/max_fit_analysis.py"):
        text = (BACKEND_ROOT / relative).read_text(encoding="utf-8")
        for sentence in text.split("。"):
            if "全市场" not in sentence:
                continue
            assert any(marker in sentence for marker in PROHIBITION_MARKERS), (
                f"{relative} 疑似把本池统计升格为全市场结论：{sentence.strip()[:120]}"
            )


def test_edited_sources_are_valid_utf8_without_replacement_char() -> None:
    for relative in (
        "services/max_fit.py",
        "scripts/max_fit_analysis.py",
        "tests/test_max_fit.py",
    ):
        raw = (BACKEND_ROOT / relative).read_bytes()
        text = raw.decode("utf-8")  # 非 UTF-8 会在这里抛错
        assert "\ufffd" not in text, f"{relative} 存在 U+FFFD 替换字符（曾因此损坏过 101 行）"


def test_analysis_scope_string_never_generalises_to_market() -> None:
    """脚本生成的 scope 文案必须是「本池已导入 N 期…」口径。"""
    series = series_of(50)
    dates = dates_of(len(series))
    scope = (
        f"本池已导入 {len(series)} 期样本内"
        f"（第 100…{99 + len(series)} 期，{dates[0].isoformat()}…{dates[-1].isoformat()}）"
    )
    assert scope.startswith("本池已导入")
    assert "样本内" in scope
    for banned in ("全市场", "全量", "市场高度"):
        assert banned not in scope

    source = (BACKEND_ROOT / "scripts" / "max_fit_analysis.py").read_text(encoding="utf-8")
    assert "本池已导入" in source
    # 文案模板里不得出现「全市场涨停」这类市场级结论词
    for banned in ("全市场涨停", "全市场最高", "全市场最强"):
        assert banned not in source
