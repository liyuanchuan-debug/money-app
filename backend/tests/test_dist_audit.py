"""``services.dist_audit`` / ``scripts.dist_audit`` 的纯逻辑、统计与口径守卫测试。

只测**纯逻辑**：不连数据库、不读 ``backend/data/*``（该目录被 .gitignore 忽略），
所有序列都在测试内确定性合成（固定 seed），换机器也是同一批断言。

守卫要点：

- 卡方尾概率必须对得上已知数值，且在样本过小时**明说数据不足**；
- 互信息 / 列联表检验必须**有功效**：对确定性依赖链要报出远超置换零分布的互信息，
  对打乱过的独立序列则必须落在零分布内（否则这套检验只是在复读噪声）；
- 均匀性的正确零假设是**多项均匀重抽**，不是序列置换（置换对频次完全无效）；
- 每个引擎组件都必须有一个可审计 handle，且每个 handle 都有中文标签与样本量；
- **Holm 校正必须真的改变判定**：raw p = 0.03 在 23 条假设下不再显著；
- 判定一律英文枚举（``signal`` / ``no_signal`` / ``insufficient_data``）；
- 文案不得把本池统计升格为「全市场」结论；源码 UTF-8 无 BOM、无替换字符。
"""

from __future__ import annotations

import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

import pytest

from scripts import dist_audit as CLI
from services import dist_audit as DA
from services import dist_engine as DE
from services.analytics import DATA_STATUS_INSUFFICIENT, DATA_STATUS_OK
from services.max_fit import K_DEFAULT, NUM_STATES, ODDS_DEFAULT

BACKEND_ROOT = Path(__file__).resolve().parents[1]
AUDIT_SOURCES = ("services/dist_audit.py", "scripts/dist_audit.py")


# --------------------------------------------------------------------------- #
# 合成样本
# --------------------------------------------------------------------------- #
def series_of(count: int = 120, seed: int = 20261007) -> list[int]:
    rng = random.Random(seed)
    return [rng.randrange(1, NUM_STATES + 1) for _ in range(count)]


def draws_of(series: list[int]) -> list[dict[str, object]]:
    start = date(2025, 1, 1)
    return [
        {
            "period": 300 + index,
            "draw_date": (start + timedelta(days=index)).isoformat(),
            "special_number": number,
        }
        for index, number in enumerate(series)
    ]


def uniform_deck(count: int = 196) -> list[int]:
    """每号出现次数完全相同的「公平骰子」序列（长度向下取整到 49 的倍数）。"""
    blocks = max(1, count // NUM_STATES)
    return [(index % NUM_STATES) + 1 for index in range(blocks * NUM_STATES)]


def stacked_deck(count: int = 210) -> list[int]:
    """严重偏斜的序列（只在 3 个号里出）。"""
    return [(index % 3) + 1 for index in range(count)]


def translation_chain(count: int = 210, step: int = 17) -> list[int]:
    """确定性依赖链：下一期 = 上一期平移 step（gcd(step,49)=1 => 双射）。"""
    value = 7
    values = [value]
    for _ in range(count - 1):
        value = (value - 1 + step) % NUM_STATES + 1
        values.append(value)
    return values


def mutual_information_null(series: list[int], *, draws: int = 24, seed: int = 4242) -> list[float]:
    """互信息的置换零分布（本测试自建，规模小、确定性强）。"""
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(draws):
        shuffled = list(series)
        rng.shuffle(shuffled)
        values.append(DA.mutual_information_bits(DA.lag_pairs(shuffled)))
    return values


# --------------------------------------------------------------------------- #
# 1. 卡方尾概率 / 均匀性
# --------------------------------------------------------------------------- #
def test_chi_square_survival_matches_known_values() -> None:
    # 自由度 1 的 95% 分位点 → 尾概率 0.05
    assert DA.chi2_sf(3.841458820694124, 1) == pytest.approx(0.05, abs=1e-9)
    # df=48 的 95% 分位点约 65.17
    assert DA.chi2_sf(65.17077, 48) == pytest.approx(0.05, abs=1e-4)
    assert DA.chi2_sf(0.0, 48) == pytest.approx(1.0, abs=1e-12)
    assert DA.chi2_sf(100.0, 48) < 1e-4
    assert DA.chi2_sf(50.0, 0) is None
    assert DA.chi2_sf(50.0, -1) is None


def test_chi_square_survival_is_monotone_decreasing() -> None:
    previous = 1.0
    for statistic in (0, 10, 30, 48, 60, 90, 150):
        value = DA.chi2_sf(float(statistic), 48)
        assert value is not None and value <= previous + 1e-12
        previous = value


def test_uniformity_test_accepts_a_fair_deck_and_rejects_a_stacked_one() -> None:
    fair = DA.uniformity_test(uniform_deck())
    assert fair["statistic"] == pytest.approx(0.0, abs=1e-9)
    assert fair["p_value"] > 0.5
    assert fair["sample_size"] == 196
    assert fair["min_count"] == fair["max_count"] == 4

    stacked = DA.uniformity_test(stacked_deck())
    assert stacked["statistic"] > 1000.0
    assert stacked["p_value"] is not None and stacked["p_value"] < 1e-3


def test_uniformity_null_is_a_multinomial_resample_not_a_permutation() -> None:
    """置换对频次无效 → 零分布必须来自「均匀重抽」，而不是打乱序列。"""
    fair = DA.uniformity_test(uniform_deck())
    null = fair["null"]
    assert null["simulations"] > 0
    # 49 格卡方在零假设下的期望≈df=48
    assert null["mean"] == pytest.approx(48.0, abs=2.0)
    assert null["p95"] > 48.0
    assert fair["p_asymptotic"] == pytest.approx(fair["p_value"], abs=0.05)


def test_uniformity_test_reports_insufficient_data_below_one_draw_per_number() -> None:
    outcome = DA.uniformity_test([1, 2, 3])
    assert outcome["data_status"] == DATA_STATUS_INSUFFICIENT
    assert outcome["statistic"] is None
    assert outcome["minimum_sample_size"] == NUM_STATES

    empty = DA.uniformity_test([])
    assert empty["data_status"] == DATA_STATUS_INSUFFICIENT
    assert empty["p_value"] is None


def test_uniformity_test_warns_when_the_chi_square_approximation_is_marginal() -> None:
    outcome = DA.uniformity_test(uniform_deck())
    # 每格期望 4.29 < 5 → 必须显式提示以蒙特卡洛 p 为准，不许默默用渐近近似
    assert outcome["expected_count"] < 5.0
    assert outcome["approximation_warning"]


# --------------------------------------------------------------------------- #
# 2. 独立性 / 互信息（最重要的那张表）
# --------------------------------------------------------------------------- #
def test_mutual_information_has_power_on_a_deterministic_chain() -> None:
    chain = translation_chain()
    observed = DA.mutual_information_bits(DA.lag_pairs(chain))
    null = mutual_information_null(chain)

    assert observed > 5.0  # 近乎 log2(49)=5.61：下一期几乎被上一期完全决定
    assert observed > max(null) * 1.4
    assert DA.permutation_p(observed, null) <= 1.0 / (1 + len(null))


def test_mutual_information_of_a_shuffled_sequence_stays_inside_its_null() -> None:
    series = series_of(210)
    observed = DA.mutual_information_bits(DA.lag_pairs(series))
    null = mutual_information_null(series)

    # 209 对样本摊在 2401 个格子里 => 经验互信息必然虚高，
    # 必须与「同多重集合、随机顺序」的零分布对比才谈得上结论。
    assert observed > 3.0
    assert observed <= max(null)
    assert DA.permutation_p(observed, null) > 0.05


def test_independence_chi_square_detects_the_deterministic_chain() -> None:
    chain = DA.independence_test(translation_chain())
    assert chain["statistic"] > 5000.0
    assert chain["degrees_of_freedom"] == (NUM_STATES - 1) ** 2
    assert DA.chi2_sf(chain["statistic"], chain["degrees_of_freedom"]) < 1e-6

    independent = DA.independence_test(series_of(210))
    assert DA.chi2_sf(independent["statistic"], independent["degrees_of_freedom"]) > 0.01


def test_independence_needs_at_least_two_draws() -> None:
    outcome = DA.independence_test([7])
    assert outcome["data_status"] == DATA_STATUS_INSUFFICIENT
    assert outcome["statistic"] is None
    assert outcome["pairs"] == 0


def test_lag_pairs_uses_the_original_order_only() -> None:
    assert DA.lag_pairs([1, 2, 3, 4], 1) == [(1, 2), (2, 3), (3, 4)]
    assert DA.lag_pairs([1, 2, 3, 4], 2) == [(1, 3), (2, 4)]
    assert DA.lag_pairs([1, 2, 3, 4], 9) == []


def test_contingency_matrix_margins_are_the_pair_counts() -> None:
    pairs = DA.lag_pairs(translation_chain(60))
    matrix = DA.contingency_matrix(pairs)
    assert sum(sum(row) for row in matrix) == len(pairs)
    assert len(matrix) == NUM_STATES
    assert all(len(row) == NUM_STATES for row in matrix)


def test_permutation_p_matches_its_definition() -> None:
    null = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert DA.permutation_p(6.0, null) == pytest.approx(1.0 / 6.0)
    assert DA.permutation_p(5.0, null) == pytest.approx(2.0 / 6.0)
    assert DA.permutation_p(0.0, null) == pytest.approx(6.0 / 6.0)
    assert DA.permutation_p(1.0, [], ) is None
    assert DA.permutation_p(None, null) is None
    with pytest.raises(ValueError):
        DA.permutation_p(1.0, null, direction="sideways")


def test_summarize_null_reports_mean_sd_and_percentiles() -> None:
    block = DA.summarize_null([1.0, 2.0, 3.0, 4.0], 3.0)
    assert block["n"] == 4
    assert block["mean"] == pytest.approx(2.5)
    assert block["max"] == 4.0
    assert block["observed"] == 3.0
    assert block["p_one_sided"] is not None
    empty = DA.summarize_null([], 1.0)
    assert empty["n"] == 0 and empty["p_one_sided"] is None


# --------------------------------------------------------------------------- #
# 3. 自相关 / 游程 / 谱扫描 / 熵
# --------------------------------------------------------------------------- #
def test_autocorrelation_scan_covers_all_derived_series() -> None:
    scan = DA.autocorrelation_scan(series_of(120))
    assert scan["max_lag"] == DA.DEFAULT_MAX_LAG
    assert set(scan["series"]) == {"special", "parity", "bigsmall", "tail_digit", "zodiac_group"}
    for name, entry in scan["series"].items():
        assert len(entry["lags"]) == DA.DEFAULT_MAX_LAG
        assert 0.0 <= entry["max_abs_r"] <= 1.0
        assert entry["best_lag"] in range(1, DA.DEFAULT_MAX_LAG + 1)


def test_autocorrelation_is_exact_for_a_shifted_copy() -> None:
    series = [float(value) for value in range(1, 50)] * 2
    assert DA.autocorrelation(series, 49) == pytest.approx(1.0, abs=1e-12)
    assert DA.autocorrelation([1.0, 1.0, 1.0], 1) is None  # 方差为 0


def test_runs_test_is_two_sided_and_needs_both_symbols() -> None:
    alternating = [1, 0] * 50
    outcome = DA.runs_test(alternating)
    assert outcome["runs"] == 100
    assert outcome["z"] > 0 and outcome["abs_z"] > 5.0
    assert DA.runs_test([1] * 10)["data_status"] == DATA_STATUS_INSUFFICIENT


def test_spectral_scan_finds_an_injected_period() -> None:
    injected = [min(NUM_STATES, max(1, 25 + round(20 * math.sin(2 * math.pi * i / 7)))) for i in range(210)]
    outcome = DA.spectral_scan(injected)
    assert outcome["best_period"] == 7
    assert outcome["statistic"] > 1000.0
    assert DA.spectral_scan(injected)["statistic"] == pytest.approx(outcome["statistic"], abs=1e-9)
    assert DA.spectral_scan([1, 2, 3])["data_status"] == DATA_STATUS_INSUFFICIENT


def test_entropy_gap_is_zero_for_a_fair_deck_and_large_for_a_stacked_one() -> None:
    fair = DA.entropy_test(uniform_deck())
    assert fair["statistic"] == pytest.approx(0.0, abs=0.01)
    assert fair["entropy"]["plugin_nats"] == pytest.approx(math.log(NUM_STATES), abs=0.01)

    stacked = DA.entropy_test(stacked_deck())
    assert stacked["statistic"] > 2.0
    assert stacked["p_value"] < 0.01
    assert stacked["entropy"]["observed_categories"] == 3


def test_entropy_null_band_is_the_multinomial_reference() -> None:
    band = DA.entropy_null_band(210, simulations=500, seed=7)
    assert band["mean"] > 0.0
    assert band["p975"] > band["p025"]
    assert len(band["gaps"]) == 500
    assert DA.entropy_null_band(0)["data_status"] == DATA_STATUS_INSUFFICIENT
    assert DA.entropy_test([])["data_status"] == DATA_STATUS_INSUFFICIENT


# --------------------------------------------------------------------------- #
# 4. 组件级样本外检验（热冷 / 遗漏 / 生肖 / 点阵 / 马尔可夫 / 结构 / 重号）
# --------------------------------------------------------------------------- #
def test_every_engine_component_is_auditable_exactly_once() -> None:
    assert set(DA.COMPONENT_HANDLE_MAP) == set(DE.COMPONENT_IDS)
    handles = list(DA.COMPONENT_HANDLE_MAP.values())
    assert len(handles) == len(set(handles)), "两个组件共用一个 handle 会重复计数"
    for handle in handles:
        assert handle in DA.ALL_HANDLES
        assert DA.HANDLE_LABELS[handle]


def test_every_declared_handle_has_a_label_and_a_statistic_definition() -> None:
    assert len(DA.ALL_HANDLES) == len(set(DA.ALL_HANDLES))
    assert set(DA.ALL_HANDLES) == set(DA.PARAMETRIC_HANDLES) | set(DA.PERMUTATION_HANDLES)
    for handle in DA.ALL_HANDLES:
        assert DA.HANDLE_LABELS[handle].strip()
        assert handle in CLI.STATISTIC_LABELS, f"{handle} 缺统计量说明"


def test_standalone_component_tests_report_counts_and_baseline() -> None:
    series = series_of(150)
    for component in DE.COMPONENT_IDS:
        outcome = DA.standalone_component_test(series, component)
        assert outcome["handle"] == DA.COMPONENT_HANDLE_MAP[component]
        assert outcome["data_status"] == DATA_STATUS_OK
        assert outcome["statistic"] is not None
        if outcome.get("hit_rate") is not None:
            assert outcome["baseline_hit_rate"] == pytest.approx(K_DEFAULT / NUM_STATES)
            assert 0.0 <= outcome["hit_rate"] <= 1.0
    with pytest.raises(ValueError):
        DA.standalone_component_test(series, "NOT_A_COMPONENT")


def test_component_topk_is_deterministic_and_leak_free() -> None:
    series = series_of(120)
    position = 100
    for component in DE.COMPONENT_IDS:
        baseline = DE.component_topk(series, position, component)
        assert len(baseline) == K_DEFAULT
        assert len(set(baseline)) == K_DEFAULT
        assert DA.standalone_component_test(series, component)["statistic"] is not None

        corrupted = list(series)
        for index in range(position, len(corrupted)):
            corrupted[index] = (corrupted[index] % NUM_STATES) + 1
        assert DE.component_topk(corrupted, position, component) == baseline


def test_hot_cold_uses_a_strict_train_valid_split() -> None:
    series = series_of(120)
    outcome = DA.hot_cold_test(series)
    assert outcome["train_size"] == 60
    assert outcome["evaluated"] == 60
    assert outcome["hits"] == sum(
        1 for value in series[60:] if value in set(outcome["picks"])
    )


def test_gap_test_evaluates_every_draw_after_the_warmup() -> None:
    series = series_of(120)
    outcome = DA.gap_test(series, warmup=30)
    assert outcome["evaluated"] == 90
    assert 0 <= outcome["hits"] <= 90
    assert outcome["p_binomial"] is not None


def test_gap_component_matches_the_most_overdue_cross_check() -> None:
    series = series_of(120)
    cross = DA.gap_test(series)
    component = DA.component_topk_test(series, DE.COMPONENT_GAP, DA.HANDLE_GAP)
    # 两个实现口径不同（组件排序 vs 最久未出），但命中率都在同一量级
    assert abs(cross["hit_rate"] - component["hit_rate"]) < 0.15


def test_short_series_are_reported_as_insufficient_not_as_zero() -> None:
    short = series_of(20)  # 少于 warmup
    for outcome in (
        DA.hot_cold_test(short),
        DA.gap_test(short),
        DA.zodiac_topk_test(short),
        DA.wave_topk_test(short),
        DA.autocorrelation_scan([1, 2, 3]),
        DA.standalone_component_test(short, DE.COMPONENT_MARKOV),
    ):
        assert outcome["data_status"] == DATA_STATUS_INSUFFICIENT
        assert outcome.get("statistic") is None


# --------------------------------------------------------------------------- #
# 5. 生产引擎自身特征的逐项消融
# --------------------------------------------------------------------------- #
def test_engine_feature_hits_reports_both_sides_and_the_delta() -> None:
    draws = draws_of(series_of(110))
    outcome = DA.engine_feature_hits(draws)
    assert set(outcome) == set(DA.ENGINE_FEATURE_HANDLES)
    for handle, blob in outcome.items():
        assert blob["handle"] == handle
        assert blob["data_status"] in (DATA_STATUS_OK, DATA_STATUS_INSUFFICIENT)
        on, off = blob["on"], blob["off"]
        assert on["evaluated"] == off["evaluated"]
        if on["hit_rate"] is not None:
            assert blob["statistic"] == pytest.approx(on["hit_rate"] - off["hit_rate"])
        assert blob["settings_on"] != blob["settings_off"]


def test_engine_feature_definitions_toggle_exactly_one_knob() -> None:
    for definition in DA.engine_feature_definitions():
        assert definition["handle"] in DA.ENGINE_FEATURE_HANDLES
        changed = {
            key
            for key in set(definition["on"]) | set(definition["off"])
            if definition["on"].get(key) != definition["off"].get(key)
        }
        assert len(changed) == 1, f"{definition['handle']} 一次改了多个旋钮：{changed}"


def test_default_audit_settings_pin_the_ticket_shape() -> None:
    settings = DA.default_audit_settings()
    assert settings["pick_count"] == K_DEFAULT
    assert settings["mode"] == "even"
    assert settings["odds"] == pytest.approx(float(ODDS_DEFAULT))


# --------------------------------------------------------------------------- #
# 6. 多重比较校正（Holm）与英文判定
# --------------------------------------------------------------------------- #
def test_holm_changes_a_borderline_p_into_a_non_signal() -> None:
    """raw p=0.03 在 23 条假设下不该算显著 —— 这正是「先声明族大小」的意义。"""
    rows = [{"handle": f"h{index}", "p_value": 0.03} for index in range(len(DA.ALL_HANDLES))]
    outcome = DA.correct_handles(rows, alpha=0.05)

    assert outcome["family_size"] == len(DA.ALL_HANDLES)
    assert outcome["tested_size"] == len(DA.ALL_HANDLES)
    assert outcome["correction"] == "holm"
    assert outcome["bonferroni_threshold"] == pytest.approx(0.05 / len(DA.ALL_HANDLES))
    assert outcome["rows"][0]["p_adjusted"] > 0.05
    assert outcome["survivors"] == []
    # 未经校正会把 23 条全部误判为「显著」
    assert sum(1 for row in rows if row["p_value"] <= 0.05) == len(DA.ALL_HANDLES)
    for row in outcome["rows"]:
        assert row["verdict"] == DA.VERDICT_NO_SIGNAL


def test_holm_keeps_a_genuinely_tiny_p_significant() -> None:
    rows = [
        {"handle": "real", "p_value": 1e-6},
        {"handle": "noise", "p_value": 0.4},
        {"handle": "noise2", "p_value": 0.9},
    ]
    outcome = DA.correct_handles(rows, alpha=0.05)
    assert outcome["survivors"] == ["real"]
    assert outcome["rows"][0]["verdict"] == DA.VERDICT_SIGNAL
    assert outcome["min_p_raw"] == pytest.approx(1e-6)


def test_missing_p_values_become_insufficient_data_not_zero() -> None:
    rows = [
        {"handle": "a", "p_value": None},
        {"handle": "b", "p_value": 0.5},
    ]
    outcome = DA.correct_handles(rows, alpha=0.05)
    by_handle = {row["handle"]: row for row in outcome["rows"]}
    assert by_handle["a"]["verdict"] == DA.VERDICT_INSUFFICIENT
    assert by_handle["a"]["p_adjusted"] is None
    assert by_handle["b"]["verdict"] == DA.VERDICT_NO_SIGNAL
    assert outcome["tested_size"] == 1


def test_verdict_enums_are_english_and_bilingual_labels_are_chinese_only_in_labels() -> None:
    for verdict in (DA.VERDICT_SIGNAL, DA.VERDICT_NO_SIGNAL, DA.VERDICT_INSUFFICIENT):
        assert verdict.isascii(), f"判定码必须是英文：{verdict}"
        assert DA.VERDICT_LABELS[verdict].strip()
    assert DA.VERDICT_INSUFFICIENT == "insufficient_data"
    assert set(DA.VERDICT_LABELS) == {
        DA.VERDICT_SIGNAL,
        DA.VERDICT_NO_SIGNAL,
        DA.VERDICT_INSUFFICIENT,
    }


def test_correct_handles_output_is_json_serialisable_with_english_enums() -> None:
    outcome = DA.correct_handles([{"handle": "a", "p_value": 0.2}], alpha=0.05)
    payload = json.dumps(outcome, ensure_ascii=False, default=str)
    restored = json.loads(payload)
    assert restored["rows"][0]["verdict"] == "no_signal"


# --------------------------------------------------------------------------- #
# 7. 样本状态 / 保本口径
# --------------------------------------------------------------------------- #
def test_assess_state_reports_the_break_even_margin_in_the_arithmetic() -> None:
    series = series_of(210)
    state = DA.assess_state(draws_of(series), series, k=K_DEFAULT, odds=float(ODDS_DEFAULT))
    assert state["sample_size"] == 210
    assert state["baseline_hit_rate"] == pytest.approx(K_DEFAULT / NUM_STATES)
    assert state["break_even_hit_rate"] == pytest.approx(K_DEFAULT / ODDS_DEFAULT)
    assert state["break_even_edge"] == pytest.approx(
        K_DEFAULT / ODDS_DEFAULT - K_DEFAULT / NUM_STATES, abs=1e-12
    )
    assert state["break_even_edge"] == pytest.approx(0.008684, abs=1e-6)
    assert state["power"]["minimum_detectable_delta"] is not None
    assert "本池已导入" in state["scope"]
    for banned in ("全市场", "全量", "市场高度"):
        assert banned not in state["scope"]


# --------------------------------------------------------------------------- #
# 8. 脚本编排：置换零分布 / CLI
# --------------------------------------------------------------------------- #
def test_collect_nulls_is_deterministic_and_sized_per_handle() -> None:
    series = series_of(100)
    draws = draws_of(series)
    first = CLI.collect_nulls(series, draws, perms=4, perms_engine=0, jobs=1)
    second = CLI.collect_nulls(series, draws, perms=4, perms_engine=0, jobs=1)

    for handle in DA.PERMUTATION_HANDLES:
        assert first["nulls"][handle] == second["nulls"][handle], handle
        if handle in DA.ENGINE_FEATURE_HANDLES:
            assert first["nulls"][handle] == []
    assert first["meta"]["null_sizes"][DA.HANDLE_INDEPENDENCE] == 4
    assert first["meta"]["permutation_scheme"] == "shuffle_special_number_positions_keep_period_and_date"
    assert first["meta"]["seed_base"] == CLI.PERM_SEED_BASE
    assert len(first["meta"]["cheap_handles"]) == len(DA.PERMUTATION_HANDLES) - len(
        DA.ENGINE_FEATURE_HANDLES
    )


def test_shuffling_preserves_the_number_multiset_but_destroys_the_order() -> None:
    series = series_of(120)
    draws = draws_of(series)
    shuffled = CLI._shifted(draws, CLI.PERM_SEED_BASE)
    assert sorted(row["special_number"] for row in shuffled) == sorted(series)
    assert [row["period"] for row in shuffled] == [row["period"] for row in draws]
    assert [row["draw_date"] for row in shuffled] == [row["draw_date"] for row in draws]
    assert [row["special_number"] for row in shuffled] != series
    assert CLI._shifted(draws, CLI.PERM_SEED_BASE) == shuffled
    assert len(CLI._shuffled_numbers(series, 5)) == len(series)


def test_numbers_digest_is_stable_and_input_sensitive() -> None:
    assert CLI.numbers_digest([1, 2, 3]) == CLI.numbers_digest([1, 2, 3])
    assert CLI.numbers_digest([1, 2, 3]) != CLI.numbers_digest([3, 2, 1])
    assert len(CLI.numbers_digest([1, 2, 3])) == 64


def test_cli_help_carries_the_plain_chinese_summary_and_four_subcommands() -> None:
    parser = CLI.build_parser()
    text = parser.format_help()
    for command in ("audit", "evaluate", "explain", "null"):
        assert command in text
    assert "为何做不出这个策略" in text
    assert "互信息" in text
    assert "保本" in text


def test_cli_explain_runs_without_any_draw_file(tmp_path: Path) -> None:
    out = tmp_path / "explain.json"
    code = CLI.main(
        [
            "explain",
            "--draws-json",
            str(tmp_path / "missing.json"),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["claim"] == "NO_EDGE"
    assert payload["state"] is None
    assert "为何做不出这个策略" in payload["text"]
    assert "互信息" in payload["text"]
    assert "只针对本池已导入的样本" in payload["text"]
    for banned in ("全市场涨停", "全市场最高", "必出", "稳赚", "保证中奖"):
        assert banned not in payload["text"]
    assert payload["legitimate_value"]["items"]


def test_cli_audit_writes_a_full_artifact(tmp_path: Path) -> None:
    series = series_of(90)
    draws_path = tmp_path / "draws.json"
    draws_path.write_text(json.dumps(draws_of(series), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "audit.json"

    code = CLI.main(
        [
            "audit",
            "--draws-json",
            str(draws_path),
            "--out",
            str(out),
            "--perms",
            "3",
            "--perms-engine",
            "0",
            "--jobs",
            "1",
        ]
    )
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert payload["claim"] == "NO_EDGE"
    assert payload["hypotheses"]["family_size"] == len(DA.ALL_HANDLES)
    assert payload["hypotheses"]["correction"] == "holm"
    assert len(payload["handles"]) == len(DA.ALL_HANDLES)
    assert [row["handle"] for row in payload["handles"]] == list(DA.ALL_HANDLES)
    assert len(payload["signal_table"]) == len(DA.ALL_HANDLES)
    assert payload["mechanism"]["answer"]
    assert payload["mechanism"]["mutual_information_bits"] is not None
    assert payload["ablation"]["engine_features"]
    assert len(payload["ablation"]["engine_components"]) == len(DE.COMPONENT_IDS)
    assert payload["power"]["targets"]
    assert payload["legitimate_value"]["not_claimed"]
    assert "本池已导入" in payload["scope"]
    assert payload["input"]["sample_size"] == len(series)
    assert len(payload["input"]["numbers_sha256"]) == 64
    for row in payload["handles"]:
        assert row["verdict"] in DA.VERDICT_LABELS
        assert row["handle"].isascii()
        assert row["label"].strip()


def test_cli_default_out_paths_live_under_the_ignored_tmp_dir() -> None:
    resolved = CLI.resolve_out(None, "dist_audit.json")
    assert ".tmp-dist-audit" in resolved
    assert CLI.resolve_out("custom.json", "dist_audit.json") == "custom.json"


# --------------------------------------------------------------------------- #
# 9. 口径 / 编码守卫
# --------------------------------------------------------------------------- #
def test_source_mentions_of_market_scope_are_only_prohibitions() -> None:
    markers = ("禁止", "不得", "没有", "不是", "仅限", "只针对", "不涉及", "不升格")
    for relative in AUDIT_SOURCES:
        text = (BACKEND_ROOT / relative).read_text(encoding="utf-8")
        for sentence in text.split("。"):
            if "全市场" not in sentence:
                continue
            assert any(marker in sentence for marker in markers), (
                f"{relative} 疑似把本池统计升格为全市场结论：{sentence.strip()[:120]}"
            )


def test_audit_handles_and_verdicts_are_english_codes_only() -> None:
    for handle in DA.ALL_HANDLES + DA.COMPONENT_HANDLES:
        assert handle.isascii(), f"handle 必须是英文码：{handle}"
    for handle in DA.ALL_HANDLES:
        assert handle in CLI.STATISTIC_LABELS
    for handle in DA.ALL_HANDLES:
        assert handle == handle.lower()


def test_edited_sources_are_valid_utf8_without_bom_or_replacement_char() -> None:
    for relative in AUDIT_SOURCES + ("tests/test_dist_audit.py",):
        raw = (BACKEND_ROOT / relative).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{relative} 带 BOM"
        text = raw.decode("utf-8")
        assert "\ufffd" not in text, f"{relative} 存在 U+FFFD 替换字符"


def test_audit_module_only_imports_services_primitives() -> None:
    """审计模块必须是纯模块：不许碰数据库 / 路由 / 生产引擎的内部状态。"""
    text = (BACKEND_ROOT / "services" / "dist_audit.py").read_text(encoding="utf-8")
    for banned in ("import sqlalchemy", "from database", "from routers", "from main"):
        assert banned not in text, f"dist_audit.py 出现不该有的依赖：{banned}"
    from services.analytics import holm_adjusted_p, minimum_detectable_delta  # noqa: F401
    from services.analytics import binomial_tail_p  # noqa: F401
