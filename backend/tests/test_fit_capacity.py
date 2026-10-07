"""``scripts.fit_capacity_analysis`` 的纯函数与口径守卫单元测试。

只测**纯逻辑**：不连数据库、不读 ``backend/data/*``（该目录被 .gitignore 忽略），
所有开奖序列都在测试内确定性合成。

守卫要点：
- walk-forward 无前视：改动「未来」不得改动「过去」的逐期命中；
- 容量 → 样本内命中率可升到 100%，但样本外恒为均匀基线；
- 点阵带宽的定义期望必须严格等于 (2w+1)/49；
- 盈利条件 p > m/odds 的算术；
- 对抗性检验的 Holm 多重比较校正。
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from scripts import fit_capacity_analysis as F


# --------------------------------------------------------------------------- #
# 合成样本
# --------------------------------------------------------------------------- #
def synthetic(count: int = 120, step: int = 17, offset: int = 5) -> tuple[list[int], list[date]]:
    """递增日期 + 与 49 互质的步长 17，保证号码铺满 1..49（确定性、可复现）。"""
    start = date(2025, 1, 1)
    series = [((index * step + offset) % F.NUM_STATES) + 1 for index in range(count)]
    dates = [start + timedelta(days=index) for index in range(count)]
    return series, dates


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def test_hit_rate_edge_cases() -> None:
    assert F.hit_rate([]) is None
    assert F.hit_rate([True, True]) == 1.0
    assert F.hit_rate([False, False]) == 0.0
    assert F.hit_rate([True, False, True, False]) == 0.5


def test_rank_top_k_breaks_ties_by_number() -> None:
    counts = {7: 3, 5: 3, 9: 1}
    assert F.rank_top_k(counts, 2) == [5, 7]
    assert F.rank_top_k(counts, 0) == []
    assert F.rank_top_k({}, 3) == []


def test_pad_to_k_is_unique_and_exact_size() -> None:
    picks = F.pad_to_k([4, 4, 7], [1, 2, 3, 3, 8], 5)
    assert len(picks) == 5
    assert len(set(picks)) == 5
    assert picks[:2] == [4, 7]
    assert all(F.NUMBER_MIN <= number <= F.NUMBER_MAX for number in picks)


def test_uniform_picks_has_exactly_k_distinct_numbers_in_range() -> None:
    for index in range(10):
        picks = F._uniform_picks(index, F.K_DEFAULT)
        assert len(picks) == F.K_DEFAULT
        assert picks <= set(F.NUMBERS)


# --------------------------------------------------------------------------- #
# 马尔可夫 / 上下文查表
# --------------------------------------------------------------------------- #
def test_build_transition_counts_counts_successors() -> None:
    table = F.build_transition_counts([1, 2, 2, 3, 2], order=1)
    assert table[(1,)][2] == 1
    assert table[(2,)][2] == 1
    assert table[(2,)][3] == 1


def test_second_order_transition_key_length() -> None:
    table = F.build_transition_counts([1, 2, 3, 4, 1, 2, 3], order=2)
    assert all(len(state) == 2 for state in table)
    assert table[(1, 2)][3] == 2


def test_markov_rank_at_explains_the_observed_target() -> None:
    """样本内口径：表建在含被预测期的整段数据上，必然能解释该观测。"""
    series = [3, 11, 3, 11, 3, 11, 3, 11, 21, 3, 11]
    for index in range(2, len(series)):
        picks = F.markov_rank_at(series, index, 1, F.K_DEFAULT)
        assert series[index] in picks


def test_context_lookup_at_in_sample_hit() -> None:
    series = [5, 9, 5, 9, 5, 9, 33, 5, 9]
    for index in range(2, len(series)):
        picks = F.context_lookup_at(series, index, 2, F.K_DEFAULT)
        assert series[index] in picks


def test_markov_rank_falls_back_when_state_unseen() -> None:
    picks = F.markov_rank([1, 2, 3], order=2, k=10)
    assert len(picks) == 10
    assert len(set(picks)) == 10
    # 回退到全历史频次后仍补足到 k 个（前 3 个是真实出现过的号）
    assert picks[:3] == [1, 2, 3]


def test_markov_rank_uses_only_the_given_prefix() -> None:
    prefix = [7, 7, 7, 7]
    assert F.markov_rank(prefix, 1, 6) == F.markov_rank(list(prefix), 1, 6)
    assert F.markov_rank([], 1, 4) == list(F.NUMBERS[:4])
    assert len(F.markov_rank([5], 1, 10)) == 10  # 单期前缀也要补足 k 个号


def test_markov_rank_always_returns_exactly_k_distinct() -> None:
    for order in (1, 2, 3):
        for length in (1, 2, 3, 5, 20):
            series = [((index * 11 + 3) % 49) + 1 for index in range(length)]
            picks = F.markov_rank(series, order, F.K_DEFAULT)
            assert len(picks) == F.K_DEFAULT, (order, length)
            assert len(set(picks)) == F.K_DEFAULT, (order, length)
            assert set(picks) <= set(F.NUMBERS)


# --------------------------------------------------------------------------- #
# 记忆化 / 热门窗口 / 点阵
# --------------------------------------------------------------------------- #
def test_fit_memorise_index_has_one_parameter_per_observation() -> None:
    series = [1, 49, 7]
    table = F.fit_memorise_index(series)
    assert table == {0: 1, 1: 49, 2: 7}
    assert len(table) == len(series)


def test_hot_picks_window_truncation() -> None:
    series = [1, 1, 1, 2, 2, 3]
    assert F.hot_picks(series, 3, 1) == [2]
    assert F.hot_picks(series, 0, 1) == [1]


def test_fit_hot_window_picks_a_grid_member() -> None:
    series, _ = synthetic(90)
    fitted = F.fit_hot_window(series, F.K_DEFAULT)
    assert fitted["window"] in F.HOT_WINDOW_GRID
    assert 0.0 <= fitted["in_sample_rate"] <= 1.0
    assert fitted["evaluated"] == len(series) - 1


def test_lattice_band_size_matches_definition() -> None:
    for w in (0, 1, 24, 48):
        for latest in (1, 25, 49):
            expected = sum(1 for n in F.NUMBERS if abs(n - latest) <= w)
            assert F.lattice_band_size(latest, w) == expected


def test_fit_lattice_band_on_constant_step() -> None:
    series = [((index * 5) % 49) + 1 for index in range(60)]
    # 恒定步长在环上会回绕，用非回绕段更直观：
    series = [index + 1 for index in range(20)]
    band = F.fit_lattice_band(series)
    assert band["offset"] == 1
    assert band["half_width"] == 0
    assert band["coverage"] == 1.0
    assert band["samples"] == 19


def test_lattice_picks_returns_exactly_k() -> None:
    picks = F.lattice_picks(latest=25, half_width=1, offset=0, k=F.K_DEFAULT)
    assert len(picks) == F.K_DEFAULT
    assert len(set(picks)) == F.K_DEFAULT
    assert picks[0] == 25


def test_lattice_picks_clamps_centre_into_range() -> None:
    picks = F.lattice_picks(latest=49, half_width=0, offset=40, k=3)
    assert picks[0] == 49


# --------------------------------------------------------------------------- #
# 盈利条件算术
# --------------------------------------------------------------------------- #
def test_breakeven_probability_is_m_over_odds() -> None:
    assert F.breakeven_probability(10, 47.0) == pytest.approx(10.0 / 47.0)


def test_required_relative_uplift_is_49_over_odds_minus_one() -> None:
    assert F.required_relative_uplift(47.0) == pytest.approx(49.0 / 47.0 - 1.0)
    assert F.required_relative_uplift(47.0) == pytest.approx(0.042553191489361764)


def test_required_absolute_edge_at_k10_matches_0868pp() -> None:
    edge = F.required_absolute_edge(10, 47.0)
    assert edge == pytest.approx(10.0 / 47.0 - 10.0 / 49.0)
    assert edge * 100.0 == pytest.approx(0.8684, abs=1e-4)


def test_uniform_ev_is_negative_for_every_stake() -> None:
    for m in range(1, F.NUM_STATES + 1):
        assert F.expected_value_per_period(m, 47.0, m / F.NUM_STATES) < 0.0
    assert F.expected_value_per_period(10, 47.0, 10.0 / 49.0) == pytest.approx(10.0 * (47.0 / 49.0 - 1.0))


def test_break_even_ev_is_zero() -> None:
    assert F.expected_value_per_period(10, 47.0, F.breakeven_probability(10, 47.0)) == pytest.approx(0.0)


def test_draws_to_detect_shrinks_as_edge_grows() -> None:
    # 同一 p0 下提升越大，所需期数越少
    p0 = 10.0 / 49.0
    small = F.draws_to_detect(p0, p0 + 0.005)
    large = F.draws_to_detect(p0, p0 + 0.02)
    assert small is not None and large is not None
    assert small > large > 0
    assert F.draws_to_detect(p0, p0) is None


def test_two_sigma_rule_matches_closed_form() -> None:
    p0, p1 = 10.0 / 49.0, 10.0 / 47.0
    delta = p1 - p0
    expected = math.ceil(4.0 * p0 * (1.0 - p0) / (delta * delta))
    assert F.draws_to_detect_two_sigma(p0, p1) == expected
    assert F.draws_to_detect_two_sigma_exact(p0, p1) == pytest.approx(4.0 * p0 * (1.0 - p0) / (delta * delta))
    # 2σ 只是一条「刚好 2 倍标准误」的下限，比 80% 功效的正式样本量更宽松
    assert F.draws_to_detect_two_sigma(p0, p1) < F.draws_to_detect(p0, p1)


def test_sample_size_uses_row_own_variance_not_a_fixed_baseline() -> None:
    """回归：曾把 k=10 的方差（10/49）跨行复用到 m=1/3/5，导致所需期数最高被高估 8 倍。"""
    z = F.sample_size_z_values()
    assert z["z_two_sided_alpha"] == pytest.approx(1.9599639845400545)
    assert z["z_power"] == pytest.approx(0.8416212335729143)

    for m, expected_two_sigma, expected_power in ((1, 106032, 210642), (3, 33872, 67250), (5, 19440, 38572)):
        p0, p1 = m / 49.0, m / 47.0
        two_sigma_exact = F.draws_to_detect_two_sigma_exact(p0, p1)
        power_exact = F.draws_to_detect_exact(p0, p1)
        assert two_sigma_exact is not None and power_exact is not None
        assert F.draws_to_detect_two_sigma(p0, p1) == expected_two_sigma
        assert F.draws_to_detect(p0, p1) == expected_power

        # 跨行复用 k=10 方差会得到明显更大的数 —— 必须与正确值不同
        wrong = 4.0 * F.BASELINE * (1.0 - F.BASELINE) / ((p1 - p0) ** 2)
        assert wrong > two_sigma_exact
        # 高估倍数 = 390 / (m·(49−m))：m=1 → 8.125 倍，m=3 → 2.826 倍，m=5 → 1.773 倍
        assert wrong / two_sigma_exact == pytest.approx(390.0 / (m * (49 - m)))

    # m=10 时 p0 恰好就是 10/49，新旧公式必须给出同一个数（诊断的交叉验证点）
    p0, p1 = 10 / 49.0, 10 / 47.0
    fixed_variance = 4.0 * F.BASELINE * (1.0 - F.BASELINE) / ((p1 - p0) ** 2)
    assert F.draws_to_detect_two_sigma_exact(p0, p1) == pytest.approx(fixed_variance)
    assert F.draws_to_detect_two_sigma_exact(p0, p1) == pytest.approx(8615.10, abs=0.01)
    assert F.draws_to_detect_two_sigma(p0, p1) == 8616  # 向上取整
    assert F.draws_to_detect_exact(p0, p1) == pytest.approx(17062.20, abs=0.01)


def test_power_formula_cannot_be_reduced_to_a_shared_variance_term() -> None:
    """``(z_a+z_b)²·p(1−p)/δ²`` 只有在两侧方差相同且等于该 p 时才成立；本表不成立。"""
    z = F.sample_size_z_values()
    shared = (z["z_two_sided_alpha"] + z["z_power"]) ** 2
    for m in (1, 3, 5, 10):
        p0, p1 = m / 49.0, m / 47.0
        collapsed = shared * p0 * (1.0 - p0) / ((p1 - p0) ** 2)
        correct = F.draws_to_detect_exact(p0, p1)
        assert correct is not None
        assert collapsed < correct  # 简化式系统性低估（p1 > p0 ⇒ 第二项方差更大）


def test_profitability_block_consistency() -> None:
    block = F.profitability_block(10, 47.0, 208)
    assert block["breakeven_hit_rate"] == pytest.approx(10.0 / 47.0)
    assert block["required_relative_uplift"] == pytest.approx(49.0 / 47.0 - 1.0)
    assert block["draws_to_establish_2sigma"] < block["draws_to_establish_80pct_power"]
    assert len(block["ev_table"]) == F.NUM_STATES
    assert all(row["ev_per_period"] < 0 for row in block["ev_table"])

    # 每行必须带自己的 p0/p1 方差；2σ 与功效列都必须由该行自己的 p0/p1 推出
    for row in block["table_by_k"]:
        p0 = row["numbers"] / 49.0
        p1 = row["numbers"] / 47.0
        assert row["uniform_hit_rate"] == pytest.approx(p0)
        assert row["breakeven_hit_rate"] == pytest.approx(p1)
        assert row["required_absolute_edge_pp"] == pytest.approx((p1 - p0) * 100.0)
        assert row["variance_p0_p1"] == pytest.approx(p0 * (1.0 - p0))
        assert row["variance_p1_q1"] == pytest.approx(p1 * (1.0 - p1))
        assert row["draws_2sigma"] == F.draws_to_detect_two_sigma(p0, p1)
        assert row["draws_80pct_power"] == F.draws_to_detect(p0, p1)
        assert row["draws_2sigma_exact"] == pytest.approx(F.draws_to_detect_two_sigma_exact(p0, p1))
        assert row["draws_2sigma"] >= row["draws_2sigma_exact"]  # 向上取整
        assert row["draws_80pct_power"] > row["draws_2sigma"]

    # 提升幅度越大所需期数越少：注数越大，δ 越大
    ordered = [row["draws_2sigma_exact"] for row in block["table_by_k"]]
    assert ordered == sorted(ordered, reverse=True)

    assert "z_values" in block and "sample_size_formulas" in block
    assert block["z_values"]["z_two_sided_alpha"] == pytest.approx(1.9599639845400545)
    assert block["z_values"]["z_power"] == pytest.approx(0.8416212335729143)


# --------------------------------------------------------------------------- #
# 容量阶梯 / 参数数 / 点阵带宽扫描
# --------------------------------------------------------------------------- #
def test_capacity_ladder_climbs_to_100_percent_in_sample_only() -> None:
    series, _ = synthetic(60)
    ladder = F.capacity_ladder(series, F.K_DEFAULT, F.WARMUP, [0, 10, 29, 58, 100])
    rows = ladder["rows"]
    assert [row["free_parameters"] for row in rows] == [0, 10, 29, 58, 58]
    rates = [row["in_sample_hit_rate"] for row in rows]
    assert rates == sorted(rates)  # 每多背一期，样本内只增不减
    assert rates[0] < rates[-1]
    assert rates[-1] == pytest.approx(1.0)
    oos = {row["out_of_sample_hit_rate"] for row in rows}
    assert len(oos) == 1  # 样本外恒为均匀基线，与背下多少期无关
    assert ladder["uniform_rate"] in oos


def test_capacity_ladder_memorised_targets_capped_at_evaluated() -> None:
    series, _ = synthetic(20)
    ladder = F.capacity_ladder(series, F.K_DEFAULT, F.WARMUP, [999])
    row = ladder["rows"][0]
    assert row["memorised_targets"] == ladder["evaluated_targets"] == 18


def test_parameter_count_table_marks_orders_of_magnitude() -> None:
    table = F.parameter_count_table(210, F.K_DEFAULT)
    by_name = {row["name"]: row for row in table["rows"]}
    first = by_name["1 阶马尔可夫转移表"]
    assert first["cells"] == 49 ** 2
    assert first["free_parameters"] == 49 * 48
    assert first["observations_per_parameter"] == pytest.approx(210 / (49 * 48))
    second = by_name["2 阶马尔可夫转移表"]
    assert second["cells"] == 49 ** 3
    assert second["observations_per_parameter"] < first["observations_per_parameter"]
    memorisation = by_name["按期序号记忆（复现每个训练答案）"]
    assert memorisation["free_parameters"] == 208
    # 记忆化行只吃被评估的 208 期（前 2 期没有上一期可用），不能套用池内 210
    assert memorisation["observations_used"] == 208
    assert memorisation["observations"] == 210
    assert memorisation["observations_per_parameter"] == pytest.approx(1.0)
    assert memorisation["mean_count_per_cell"] == pytest.approx(1.0)


def test_parameter_count_table_rows_derive_their_own_denominators() -> None:
    """回归：每一行的两个比率都必须由该行自己的分子分母推出，不得写死或跨行复用。"""
    table = F.parameter_count_table(210, F.K_DEFAULT)
    assert table["evaluated_targets"] == 208
    for row in table["rows"]:
        used = row["observations_used"]
        assert used <= row["observations"]
        if row["cells"]:
            assert row["mean_count_per_cell"] == pytest.approx(used / row["cells"])
        else:
            assert row["mean_count_per_cell"] is None
        assert row["observations_per_parameter"] == pytest.approx(used / row["free_parameters"])



def test_lattice_sweep_definitional_line_and_monotonicity() -> None:
    series, _ = synthetic(120)
    sweep = F.lattice_band_sweep(series, F.K_DEFAULT, [1, 2, 3, 6, 12, 24, 48])
    coverages = []
    for row in sweep["rows"]:
        w = row["half_width"]
        assert row["band_size"] == 2 * w + 1
        assert row["definitional_coverage"] == pytest.approx(min(1.0, (2 * w + 1) / F.NUM_STATES))
        coverages.append(row["in_sample_coverage"])
    assert coverages == sorted(coverages)
    assert sweep["rows"][-1]["in_sample_coverage"] == pytest.approx(1.0)
    assert sweep["rows"][-1]["out_of_sample_coverage"] == pytest.approx(1.0)


def test_lattice_sweep_never_claims_more_than_definitional_structure() -> None:
    """样本外覆盖率不应系统性超过定义期望（这是「点阵没有额外信息」的核心判据）。"""
    series, _ = synthetic(140)
    sweep = F.lattice_band_sweep(series, F.K_DEFAULT, [1, 2, 3, 4, 6, 8, 12])
    for row in sweep["rows"]:
        assert row["out_of_sample_coverage"] <= row["boundary_adjusted_coverage"] + 0.12


# --------------------------------------------------------------------------- #
# walk-forward 无前视（口径硬闸）
# --------------------------------------------------------------------------- #
def test_walk_forward_never_reads_future_draws() -> None:
    series, dates = synthetic(80)
    mutated = list(series)
    for index in range(70, len(mutated)):  # 只改「未来」，不动前 70 期
        mutated[index] = 1 if mutated[index] != 1 else 2

    baseline_hits = F.run_all_rules(series, dates, F.K_DEFAULT, F.WARMUP)
    mutated_hits = F.run_all_rules(mutated, dates, F.K_DEFAULT, F.WARMUP)

    cutoff = 70 - F.WARMUP  # position p 对应目标 index = WARMUP + p
    assert cutoff > 0
    for name, hits in baseline_hits.items():
        assert hits[:cutoff] == mutated_hits[name][:cutoff], f"{name} 读到了未来数据"


def test_walk_forward_length_matches_evaluated_periods() -> None:
    series, dates = synthetic(50)
    hits = F.run_all_rules(series, dates, F.K_DEFAULT, F.WARMUP)
    assert hits  # 非空
    for name, values in hits.items():
        assert len(values) == len(series) - F.WARMUP, name
        assert all(isinstance(value, bool) for value in values), name


# --------------------------------------------------------------------------- #
# 置换零分布与对抗性检验
# --------------------------------------------------------------------------- #
def test_summarize_null_reports_empirical_p() -> None:
    summary = F.summarize_null([0.1, 0.2, 0.2, 0.3], observed=0.2)
    assert summary["n"] == 4
    assert summary["exceed_count"] == 3
    assert summary["p_one_sided"] == pytest.approx((3 + 1) / (4 + 1))
    assert summary["mean"] == pytest.approx(0.2)


def test_summarize_null_handles_empty() -> None:
    assert F.summarize_null([None, None], 0.2)["p_one_sided"] is None


def test_adversarial_findings_applies_holm_step_down() -> None:
    rows = [
        {"rule": "weak", "family": "f", "null_p_one_sided": 0.03, "binomial_p_greater": 0.04,
         "out_of_sample_hit_rate": 0.25, "free_parameters": 1, "null_mean": 0.20},
        {"rule": "strong", "family": "f", "null_p_one_sided": 0.0001, "binomial_p_greater": 0.0002,
         "out_of_sample_hit_rate": 0.30, "free_parameters": 1, "null_mean": 0.20},
        {"rule": "nullish", "family": "f", "null_p_one_sided": 0.9, "binomial_p_greater": 0.8,
         "out_of_sample_hit_rate": 0.20, "free_parameters": 1, "null_mean": 0.20},
    ]
    findings = F.adversarial_findings(rows, alpha=0.05)
    assert [finding["rule"] for finding in findings] == ["strong", "strong", "weak", "weak"]
    assert all(finding["hypotheses_tried"] == 6 for finding in findings)
    strong = [f for f in findings if f["rule"] == "strong"]
    weak = [f for f in findings if f["rule"] == "weak"]
    assert all(f["holm_significant"] for f in strong)
    # 6 个假设里排第 3、4 位的 0.03 / 0.04 过不了 Holm 门槛
    assert not any(f["holm_significant"] for f in weak)


def test_adversarial_findings_empty_when_all_null() -> None:
    rows = [{"rule": "x", "family": "f", "null_p_one_sided": 0.6, "binomial_p_greater": 0.7}]
    assert F.adversarial_findings(rows) == []


# --------------------------------------------------------------------------- #
# 口径守卫
# --------------------------------------------------------------------------- #
def test_module_scope_never_uses_full_market_wording() -> None:
    """铁律：本池口径；任何出现「全市场」的行都必须是明令禁止它的规则说明。"""
    source = (F.BACKEND_ROOT / "scripts" / "fit_capacity_analysis.py").read_text(encoding="utf-8")
    assert "\ufffd" not in source
    for line in source.splitlines():
        if "全市场" in line:
            assert "禁止" in line, f"读者可见的『全市场』措辞：{line.strip()}"


def test_numeric_constants_are_consistent() -> None:
    assert F.NUM_STATES == 49
    assert F.NUMBERS[0] == 1 and F.NUMBERS[-1] == 49
    assert F.K_DEFAULT == 10
    assert F.BASELINE == pytest.approx(10 / 49)
    assert F.WARMUP == 2
