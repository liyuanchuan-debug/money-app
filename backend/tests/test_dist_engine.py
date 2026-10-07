"""``services.dist_engine`` 的纯逻辑 / 口径 / 守卫测试。

只测**纯逻辑**：不连数据库、不读 ``backend/data/*``（该目录被 .gitignore 忽略），
所有开奖序列都在测试内确定性合成（固定 seed），保证换机器也是同一批断言。

守卫要点：

- 每个组件都必须吐出 1..49 的**完整概率分布**（处处为正、和恰为 1），
  而不是只有一个 top-k 排序 —— 没有完整分布就谈不上「号码分布预测」；
- **确定性的字节级复现**：同数据 + 同设置 + 同种子 => ``json.dumps`` 逐字节相同；
- **严格 walk-forward 无未来函数**：改动目标期及其之后的数据，
  不得改动该期的预测；只改评估窗，内层选出的权重必须逐位相同；
- **权重只来自训练窗**：``fit_weights`` 的 split_index 之后的数据不参与选权；
- 权重为 0（或某个组件被关掉）=> 该组件对结果没有影响（可单独消融）；
- ``claim`` 一律 ``NO_EDGE`` + 中文免责声明；文案里出现「全市场」必须身处禁令句；
- **导入守卫**：线上推荐路径（``services/lottery.py``、``routers/*``、``main.py``）
  一律不得引用 ``dist_engine`` / ``dist_audit``。
"""

from __future__ import annotations

import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

import pytest

from services import dist_engine as DE
from services.lottery import DEFAULT_SETTINGS, clamp_settings
from services.max_fit import K_DEFAULT, NUM_STATES, ODDS_DEFAULT

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALL_COMPONENTS = list(DE.COMPONENT_IDS)
ENGINE_SOURCES = ("services/dist_engine.py", "services/dist_audit.py")


# --------------------------------------------------------------------------- #
# 合成样本
# --------------------------------------------------------------------------- #
def series_of(count: int = 120, seed: int = 20261007) -> list[int]:
    """固定 seed 的确定性伪随机开奖序列（1..49）；同一 seed => 同一序列。"""
    rng = random.Random(seed)
    return [rng.randrange(1, NUM_STATES + 1) for _ in range(count)]


def draws_of(series: list[int]) -> list[dict[str, object]]:
    start = date(2025, 1, 1)
    return [
        {
            "period": 100 + index,
            "draw_date": (start + timedelta(days=index)).isoformat(),
            "special_number": number,
        }
        for index, number in enumerate(series)
    ]


# --------------------------------------------------------------------------- #
# 1. 完整 49 路分布
# --------------------------------------------------------------------------- #
def test_uniform_default_is_a_valid_49_way_distribution() -> None:
    prediction = DE.predict(series_of(60), 50)
    probabilities = prediction["probabilities"]

    assert sorted(probabilities) == list(range(1, NUM_STATES + 1))
    assert sum(probabilities.values()) == pytest.approx(1.0, abs=1e-12)
    assert all(value > 0.0 for value in probabilities.values())
    # 全 0 权重 = 严格均匀
    for value in probabilities.values():
        assert value == pytest.approx(1.0 / NUM_STATES, abs=1e-12)
    assert prediction["components_used"] == []


@pytest.mark.parametrize("component", ALL_COMPONENTS)
def test_every_component_alone_yields_a_valid_distribution(component: str) -> None:
    prediction = DE.predict(series_of(60), 55, weights={component: 1.0})
    probabilities = prediction["probabilities"]

    assert prediction["components_used"] == [component]
    assert sorted(probabilities) == list(range(1, NUM_STATES + 1))
    assert sum(probabilities.values()) == pytest.approx(1.0, abs=1e-12)
    assert all(value > 0.0 for value in probabilities.values())
    # 排序必须与概率同调（不是另算的一套排序）；并列时由 uniform_ranking 决定次序
    ranking = prediction["ranking"]
    assert sorted(ranking) == list(range(1, NUM_STATES + 1))
    for position in range(len(ranking) - 1):
        assert probabilities[ranking[position]] >= probabilities[ranking[position + 1]] - 1e-15
    assert prediction["picks"] == ranking[:K_DEFAULT]


def test_extreme_weight_never_produces_zero_or_negative_probability() -> None:
    probabilities = DE.pool_probabilities(
        {DE.COMPONENT_FREQUENCY: {number: float(number) for number in range(1, NUM_STATES + 1)}},
        {DE.COMPONENT_FREQUENCY: 1000.0},
    )
    assert all(value > 0.0 for value in probabilities.values())
    assert sum(probabilities.values()) == pytest.approx(1.0, abs=1e-12)


# --------------------------------------------------------------------------- #
# 2. 组件可单独开关 / 消融
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("component", ALL_COMPONENTS)
def test_zero_weight_component_has_no_effect(component: str) -> None:
    series = series_of(70)
    weights = {name: 1.0 for name in ALL_COMPONENTS}
    weights[component] = 0.0
    without = DE.predict(series, 60, weights=weights)

    assert component not in without["components_used"]
    # 该组件权重置 0 的结果，必须与「从来不把该组件放进池」逐位一致
    other_only = DE.predict(
        series, 60, weights={name: 1.0 for name in ALL_COMPONENTS if name != component}
    )
    assert without["probabilities"] == other_only["probabilities"]
    assert without["ranking"] == other_only["ranking"]


def test_every_component_changes_the_distribution_it_claims_to_model() -> None:
    """每个组件都必须真的能改变分布 —— 否则它不是「可开关组件」而是死代码。"""
    series = series_of(120, seed=20260101)
    uniform = DE.predict(series, 110)["probabilities"]
    for component in ALL_COMPONENTS:
        alone = DE.predict(series, 110, weights={component: 1.0})["probabilities"]
        assert alone != uniform, f"{component} 对分布没有任何影响"


def test_component_catalog_covers_all_components_with_toggles() -> None:
    catalog = DE.component_catalog()
    assert [row["component"] for row in catalog] == ALL_COMPONENTS
    for row in catalog:
        assert row["component_label"]
        assert row["component_note"]
        assert "weights[" in row["toggle"]


def test_evaluate_components_reports_one_row_per_component_plus_uniform() -> None:
    outcome = DE.evaluate_components(series_of(80))
    names = [entry["component"] for entry in outcome["entries"]]
    assert names == ALL_COMPONENTS + ["UNIFORM"]
    for entry in outcome["entries"]:
        assert entry["evaluated"] > 0
        assert entry["hits"] <= entry["evaluated"]
        assert 1.0 <= entry["mean_rank"] <= float(NUM_STATES)


# --------------------------------------------------------------------------- #
# 3. 确定性 / 字节级复现
# --------------------------------------------------------------------------- #
def test_predictions_are_byte_identical_across_calls() -> None:
    series = series_of(90)
    weights = {DE.COMPONENT_ZODIAC: 0.5, DE.COMPONENT_REPEAT: 2.0}
    first = DE.predict(series, 80, weights=weights)
    second = DE.predict(series, 80, weights=weights)
    assert json.dumps(first, sort_keys=True, default=str) == json.dumps(
        second, sort_keys=True, default=str
    )


def test_select_and_evaluate_is_byte_identical_across_calls() -> None:
    series = series_of(120)
    first = DE.select_and_evaluate(series)
    second = DE.select_and_evaluate(series)
    assert json.dumps(first, sort_keys=True, default=str) == json.dumps(
        second, sort_keys=True, default=str
    )


def test_engine_module_has_no_random_source() -> None:
    """引擎必须是纯确定性算术：源码里不允许出现 random / time 之类的熵来源。"""
    source = (BACKEND_ROOT / "services" / "dist_engine.py").read_text(encoding="utf-8")
    for banned in ("import random", "random.", "time.time", "os.urandom", "uuid", "hash("):
        assert banned not in source, f"dist_engine.py 出现随机/非确定来源：{banned}"


# --------------------------------------------------------------------------- #
# 4. 严格 walk-forward：无未来函数
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("component", ALL_COMPONENTS)
def test_prediction_ignores_everything_at_or_after_the_target(component: str) -> None:
    series = series_of(80)
    index = 70
    weights = {component: 1.0}
    baseline = DE.predict(series, index, weights=weights)

    corrupted = list(series)
    for position in range(index, len(corrupted)):
        corrupted[position] = (corrupted[position] % NUM_STATES) + 1
    after = DE.predict(corrupted, index, weights=weights)

    assert baseline["probabilities"] == after["probabilities"]
    assert baseline["ranking"] == after["ranking"]
    assert baseline["available_length"] == index


def test_inner_weight_selection_never_reads_the_evaluation_window() -> None:
    series = series_of(150)
    baseline = DE.fit_weights(series)
    split_index = baseline["split_index"]

    corrupted = list(series)
    for position in range(split_index, len(corrupted)):
        corrupted[position] = (corrupted[position] % NUM_STATES) + 1
    after = DE.fit_weights(corrupted)

    assert after["split_index"] == split_index
    assert after["weights"] == baseline["weights"]
    assert after["inner_log_loss"] == pytest.approx(baseline["inner_log_loss"], abs=1e-15)


def test_holdout_scores_only_the_evaluation_window() -> None:
    series = series_of(150)
    selected = DE.select_and_evaluate(series)
    split_index = selected["fit"]["split_index"]
    assert selected["holdout"]["evaluated"] == len(series) - split_index
    assert selected["uniform_holdout"]["evaluated"] == selected["holdout"]["evaluated"]


def test_weights_are_zero_or_inside_the_declared_grid() -> None:
    selected = DE.select_and_evaluate(series_of(140))
    grid = set(float(value) for value in DE.WEIGHT_GRID)
    for name, value in selected["selected_weights"].items():
        assert name in ALL_COMPONENTS
        assert value == 0.0 or float(value) in grid


def test_evaluate_in_sample_mode_is_explicitly_separated() -> None:
    series = series_of(60)
    walk = DE.evaluate(series, weights={DE.COMPONENT_FREQUENCY: 1.0})
    inside = DE.evaluate(series, weights={DE.COMPONENT_FREQUENCY: 1.0}, mode="in_sample")
    assert walk["mode"] == "walk_forward"
    assert inside["mode"] == "in_sample"
    with pytest.raises(ValueError):
        DE.evaluate(series, mode="whatever")


# --------------------------------------------------------------------------- #
# 5. 校准 / 均匀参考口径
# --------------------------------------------------------------------------- #
def test_reference_block_matches_the_shared_uniform_expectations() -> None:
    reference = DE.reference_block()
    assert reference["hit_rate"] == pytest.approx(K_DEFAULT / NUM_STATES, abs=1e-15)
    assert reference["mean_rank"] == pytest.approx((NUM_STATES + 1) / 2.0, abs=1e-12)
    assert reference["log_loss"] == pytest.approx(math.log(NUM_STATES), abs=1e-12)
    assert reference["brier"] == pytest.approx(
        (1.0 / NUM_STATES) * (1.0 - 1.0 / NUM_STATES), abs=1e-15
    )


def test_calibration_block_is_a_49_point_reliability_check() -> None:
    rows = DE.evaluate(series_of(60), weights={DE.COMPONENT_ZODIAC: 1.0})["rows"]
    calibration = DE.calibration_block(rows)
    assert len(calibration["per_number"]) == NUM_STATES
    assert calibration["degrees_of_freedom"] == NUM_STATES - 1
    assert calibration["ece"] is not None and calibration["ece"] >= 0.0
    assert calibration["mean_predicted"] == pytest.approx(
        1.0 / NUM_STATES, abs=0.05
    )
    for row in calibration["per_number"]:
        assert 0.0 < row["mean_predicted_p"] < 1.0
        assert 0.0 <= row["observed_rate"] <= 1.0


def test_calibration_of_the_uniform_distribution_equals_the_fairness_chi_square() -> None:
    """均匀权重下，「校准」与「号码是否均匀」是同一个卡方 —— 不许互相冒充证据。"""
    series = series_of(60)
    rows = DE.evaluate(series, weights={})["rows"]
    calibration = DE.calibration_block(rows)
    counts = [0] * (NUM_STATES + 1)
    for row in rows:
        counts[row["actual"]] += 1
    evaluated = len(rows)
    expected = evaluated / NUM_STATES
    chi_square = sum(
        (counts[number] - expected) ** 2 / expected for number in range(1, NUM_STATES + 1)
    )
    assert calibration["chi_square_statistic"] == pytest.approx(chi_square, rel=1e-9)


# --------------------------------------------------------------------------- #
# 6. 样本量天花板
# --------------------------------------------------------------------------- #
def test_power_block_shows_the_arithmetic_and_the_break_even_edge() -> None:
    block = DE.power_block(208, baseline_rate=K_DEFAULT / NUM_STATES, deltas=(0.05, 0.102))
    assert block["minimum_detectable_delta"] is not None
    assert block["standard_error"] == pytest.approx(
        math.sqrt((K_DEFAULT / NUM_STATES) * (1 - K_DEFAULT / NUM_STATES) / 208.0), abs=1e-12
    )
    deltas = {round(row["delta"], 6) for row in block["targets"]}
    assert deltas == {0.05, 0.102}
    # 需要的期数随目标优势变小而变大（反比关系，算术必须可见）
    required = {round(row["delta"], 6): row["required_draws_80_power"] for row in block["targets"]}
    assert required[0.102] < required[0.05]
    assert required[0.05] > 208
    for row in block["targets"]:
        assert row["required_draws_80_power"] > 0
        assert "n = (" in row["arithmetic"]


def test_break_even_edge_is_the_margin_that_must_be_beaten() -> None:
    block = DE.power_block(210, baseline_rate=K_DEFAULT / NUM_STATES, deltas=(0.0087,))
    assert K_DEFAULT / ODDS_DEFAULT - K_DEFAULT / NUM_STATES == pytest.approx(
        0.008684, abs=1e-6
    )
    assert block["targets"][0]["required_draws_80_power"] > 10000


def test_power_block_reports_insufficient_data_instead_of_zero() -> None:
    block = DE.power_block(0, baseline_rate=K_DEFAULT / NUM_STATES)
    assert block["minimum_detectable_delta"] is None
    assert block["data_status"] == DE.DATA_STATUS_INSUFFICIENT
    assert block["data_status_label"] == DE.DATA_STATUS_LABELS[DE.DATA_STATUS_INSUFFICIENT]


# --------------------------------------------------------------------------- #
# 7. 声明口径 / 免责声明
# --------------------------------------------------------------------------- #
def test_every_returned_object_carries_the_no_edge_claim() -> None:
    series = series_of(100)
    artifacts = {
        "predict": DE.predict(series, 90),
        "evaluate": DE.evaluate(series),
        "fit": DE.fit_weights(series),
        "select": DE.select_and_evaluate(series),
        "bottom_line": DE.bottom_line(),
        "power": DE.power_block(90, baseline_rate=0.2),
    }
    for name, blob in artifacts.items():
        assert blob["claim"] == DE.CLAIM_NO_EDGE, name
        assert blob["claim_label"], name
    for name in ("predict", "evaluate", "fit", "select", "bottom_line"):
        assert artifacts[name]["disclaimer"] == DE.DISCLAIMER, name
    assert DE.CLAIM_NO_EDGE == "NO_EDGE"
    assert "不承诺" in DE.DISCLAIMER or "不可区分" in DE.DISCLAIMER


def test_source_mentions_of_market_scope_are_only_prohibitions() -> None:
    """源码里凡出现「全市场」，必须身处一句**禁令**，不能是结论。"""
    markers = ("禁止", "不得", "没有", "不是", "仅限", "只针对", "不涉及", "不升格")
    for relative in ENGINE_SOURCES:
        text = (BACKEND_ROOT / relative).read_text(encoding="utf-8")
        for sentence in text.split("。"):
            if "全市场" not in sentence:
                continue
            assert any(marker in sentence for marker in markers), (
                f"{relative} 疑似把本池统计升格为全市场结论：{sentence.strip()[:120]}"
            )


def test_generated_notes_never_escalate_beyond_this_pool() -> None:
    series = series_of(80)
    texts: list[str] = []
    texts += list(DE.predict(series, 70)["notes"])
    texts += list(DE.evaluate(series)["notes"])
    texts += list(DE.select_and_evaluate(series)["notes"])
    texts += [DE.bottom_line()["statement"]]
    for text in texts:
        for banned in ("全市场", "全量", "市场高度", "必出", "稳赚"):
            assert banned not in text, f"文案出现越界/承诺词：{banned} :: {text[:80]}"


def test_bottom_line_states_what_is_and_is_not_claimed() -> None:
    block = DE.bottom_line()
    assert block["claim"] == "NO_EDGE"
    statement = block["statement"]
    assert "不可区分" in statement or "不是" in statement
    assert "分布" in statement


# --------------------------------------------------------------------------- #
# 8. 导入守卫：引擎不得进入线上路径
# --------------------------------------------------------------------------- #
LIVE_PATH_FILES = ("main.py", "services/lottery.py", "services/analytics.py")


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


def test_live_recommendation_path_never_references_the_dist_engine() -> None:
    sources = _live_path_sources()
    assert sources, "线上路径源码一个都没读到，守卫会静默失效"
    assert any(path.name == "lottery.py" for path, _ in sources)
    for path, text in sources:
        for banned in ("dist_engine", "dist_audit"):
            assert banned not in text, f"{path.name} 引用了 {banned}，违反「不接入线上」铁律"


def test_dist_engine_is_not_wired_into_lottery_module() -> None:
    import services.lottery as lottery  # 局部导入：确保线上模块可正常加载

    assert not hasattr(lottery, "dist_engine")
    assert not [name for name in vars(lottery) if "dist_engine" in name]


def test_scripts_do_not_write_to_the_forward_ledger() -> None:
    """审计脚本只能读账本口径的数据加载函数，绝不写 ``forward_ledger/*``。"""
    text = (BACKEND_ROOT / "scripts" / "dist_audit.py").read_text(encoding="utf-8")
    assert "load_draws" in text
    for banned in ("freeze_prediction", "append_ledger", "write_ledger", "ledger.json"):
        assert banned not in text, f"审计脚本疑似写账本：{banned}"


# --------------------------------------------------------------------------- #
# 9. 编码 / 文案守卫
# --------------------------------------------------------------------------- #
def test_edited_sources_are_valid_utf8_without_bom_or_replacement_char() -> None:
    paths = list(ENGINE_SOURCES) + ["scripts/dist_audit.py"]
    for relative in paths:
        raw = (BACKEND_ROOT / relative).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{relative} 带 BOM"
        text = raw.decode("utf-8")  # 非 UTF-8 会在这里抛错
        assert "\ufffd" not in text, f"{relative} 存在 U+FFFD 替换字符"


def test_settings_snapshot_helper_still_uses_the_production_shape() -> None:
    settings = clamp_settings({**DEFAULT_SETTINGS, "pick_count": K_DEFAULT, "mode": "even"})
    assert settings["pick_count"] == K_DEFAULT
    assert settings["mode"] == "even"
    assert settings["odds"] == pytest.approx(float(ODDS_DEFAULT))
