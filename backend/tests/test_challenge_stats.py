"""``services.analytics`` 新增的小样本功效纯函数 + ``scripts/challenge_210_draws``
核心换算的单元测试。

只测纯逻辑：不连数据库、不依赖 ``backend/data/*``（该目录被 .gitignore 忽略），
所有开奖序列都在测试内合成。
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from services import analytics as A
from scripts import challenge_210_draws as C


# --------------------------------------------------------------------------- #
# 合成样本
# --------------------------------------------------------------------------- #
def synthetic_draws(count: int = 120) -> list[dict]:
    """递增日期 + 与 49 互质的步长，保证号码铺满 1..49（确定性、可复现）。"""
    start = date(2025, 1, 1)
    return [
        {
            "period": index + 1,
            "draw_date": (start + timedelta(days=index)).isoformat(),
            "special_number": ((index * 17 + 5) % 49) + 1,
        }
        for index in range(count)
    ]


# --------------------------------------------------------------------------- #
# binomial_log_pmf / binomial_tail_p
# --------------------------------------------------------------------------- #
def test_binomial_log_pmf_matches_float_pmf() -> None:
    for k, n, p in [(0, 10, 0.5), (3, 12, 0.25), (20, 40, 0.20408163265306123)]:
        expected = math.log(math.comb(n, k) * p**k * (1.0 - p) ** (n - k))
        assert A.binomial_log_pmf(k, n, p) == pytest.approx(expected, rel=1e-12)


def test_binomial_log_pmf_degenerate_and_out_of_range() -> None:
    assert A.binomial_log_pmf(0, 5, 0.0) == 0.0
    assert A.binomial_log_pmf(1, 5, 0.0) == -math.inf
    assert A.binomial_log_pmf(5, 5, 1.0) == 0.0
    assert A.binomial_log_pmf(4, 5, 1.0) == -math.inf
    assert A.binomial_log_pmf(6, 5, 0.5) == -math.inf
    assert A.binomial_log_pmf(-1, 5, 0.5) == -math.inf


def test_binomial_tail_p_exact_fractions() -> None:
    # 5/10 命中在 p=0.5 下：P(X>=5) = (252+210+120+45+10+1)/1024
    assert A.binomial_tail_p(5, 10, 0.5, alternative="greater") == pytest.approx(
        638 / 1024
    )
    assert A.binomial_tail_p(10, 10, 0.5, alternative="greater") == pytest.approx(1 / 1024)
    assert A.binomial_tail_p(0, 10, 0.5, alternative="less") == pytest.approx(1 / 1024)
    assert A.binomial_tail_p(10, 10, 0.5, alternative="less") == pytest.approx(1.0)
    # 概率排序双尾：只有 k=0 与 k=10 的质量不超过观测质量
    assert A.binomial_tail_p(0, 10, 0.5, alternative="two-sided") == pytest.approx(
        2 / 1024
    )
    # 中位数处双尾 p 必然为 1
    assert A.binomial_tail_p(5, 10, 0.5, alternative="two-sided") == pytest.approx(1.0)


def test_binomial_tail_p_matches_naive_recomputation() -> None:
    n, p = 208, 10 / 49

    def pmf(k: int) -> float:
        return math.comb(n, k) * p**k * (1.0 - p) ** (n - k)

    assert A.binomial_tail_p(58, n, p, alternative="greater") == pytest.approx(
        sum(pmf(k) for k in range(58, n + 1)), rel=1e-9
    )
    assert A.binomial_tail_p(38, n, p, alternative="less") == pytest.approx(
        sum(pmf(k) for k in range(0, 39)), rel=1e-9
    )
    threshold = pmf(58) * (1 + 1e-9)
    expected_two_sided = sum(pmf(k) for k in range(n + 1) if pmf(k) <= threshold)
    assert A.binomial_tail_p(58, n, p, alternative="two-sided") == pytest.approx(
        min(1.0, expected_two_sided), rel=1e-9
    )


def test_binomial_tail_p_insufficient_and_bad_alternative() -> None:
    assert A.binomial_tail_p(0, 0, 0.5) is None
    assert A.binomial_tail_p(11, 10, 0.5) is None
    assert A.binomial_tail_p(-1, 10, 0.5) is None
    with pytest.raises(ValueError):
        A.binomial_tail_p(5, 10, 0.5, alternative="both")


# --------------------------------------------------------------------------- #
# holm_adjusted_p
# --------------------------------------------------------------------------- #
def test_holm_adjusted_p_matches_step_down() -> None:
    result = A.holm_adjusted_p([0.001, 0.02, 0.5], alpha=0.05)
    assert result["adjusted"] == pytest.approx([0.003, 0.04, 0.5])
    assert result["significant_count"] == 2
    assert result["significant_indices"] == [0, 1]
    assert result["bonferroni_threshold"] == pytest.approx(0.05 / 3)


def test_holm_adjusted_p_is_monotone_in_rank_and_order_independent() -> None:
    shuffled = A.holm_adjusted_p([0.02, 0.001, 0.5], alpha=0.05)
    assert shuffled["adjusted"][1] == pytest.approx(0.003)
    assert shuffled["adjusted"][0] == pytest.approx(0.04)
    assert shuffled["adjusted"][2] == pytest.approx(0.5)
    # 单个假设时 Holm 等价于不校正
    assert A.holm_adjusted_p([0.03], alpha=0.05)["adjusted"] == pytest.approx([0.03])


def test_holm_adjusted_p_empty() -> None:
    result = A.holm_adjusted_p([], alpha=0.05)
    assert result["adjusted"] == []
    assert result["significant_count"] == 0
    assert result["bonferroni_threshold"] == 0.05


# --------------------------------------------------------------------------- #
# minimum_detectable_delta
# --------------------------------------------------------------------------- #
def test_minimum_detectable_delta_matches_prior_power_numbers() -> None:
    block = A.minimum_detectable_delta(208, 10 / 49)
    assert block["standard_error"] == pytest.approx(0.02794502844414113)
    assert block["two_sigma_delta"] == pytest.approx(0.05589005688828226)
    assert block["minimum_detectable_delta"] == pytest.approx(0.07829037860885223)
    assert block["power_at_mdd"] == pytest.approx(0.8, abs=1e-6)
    # 与 scripts/lottery_power_analysis.py 的口径一致（+1% 需约 12750 期）
    assert block["required_draws"]["+1%"] == 12750
    assert block["required_draws"]["+2%"] == 3188
    assert block["power_at_current_n"]["+5%"] == pytest.approx(0.432, abs=5e-3)


def test_minimum_detectable_delta_shrinking_window_raises_threshold() -> None:
    wide = A.minimum_detectable_delta(208, 10 / 49)
    narrow = A.minimum_detectable_delta(10, 10 / 49)
    # 窗口缩小 ⇒ 标准误 ~1/sqrt(n) 变大 ⇒ MDD 变大（能排除的优势变少）
    assert narrow["minimum_detectable_delta"] > wide["minimum_detectable_delta"]
    ratio = narrow["standard_error"] / wide["standard_error"]
    assert ratio == pytest.approx(math.sqrt(208 / 10), rel=1e-9)


def test_minimum_detectable_delta_insufficient() -> None:
    block = A.minimum_detectable_delta(0, 10 / 49)
    assert block["standard_error"] is None
    assert block["minimum_detectable_delta"] is None
    assert block["data_status"] == A.DATA_STATUS_INSUFFICIENT
    assert block["data_status_label"] == "数据不足"
    # 基线取 0 / 1 时同样不给结论
    assert A.minimum_detectable_delta(100, 0.0)["minimum_detectable_delta"] is None


# --------------------------------------------------------------------------- #
# window_delta_from_hits
# --------------------------------------------------------------------------- #
def test_window_delta_from_hits_splits_prefix_and_suffix() -> None:
    hits = [True, True, False, False]
    block = A.window_delta_from_hits(hits, 2, 0.5)
    assert block["train_eval"] == 2 and block["valid_eval"] == 2
    assert block["train"]["hits"] == 2 and block["train"]["delta"] == pytest.approx(0.5)
    assert block["valid"]["hits"] == 0 and block["valid"]["delta"] == pytest.approx(-0.5)


def test_window_delta_from_hits_clamps_offset() -> None:
    hits = [True, False, True]
    assert A.window_delta_from_hits(hits, -5, 0.2)["train_eval"] == 0
    assert A.window_delta_from_hits(hits, 99, 0.2)["valid_eval"] == 0
    empty = A.window_delta_from_hits([], 3, 0.2)
    assert empty["train"]["delta"] is None and empty["valid"]["delta"] is None


def test_window_delta_from_hits_train_matches_harness_train() -> None:
    """训练窗快捷换算必须与 ``walk_forward_eval_config`` 逐位一致。

    这是本分析的核心依赖：整池回测的**前缀**就是训练窗，所以训练窗 Δ 可以白拿。
    """
    draws = synthetic_draws(120)
    split = A.walk_forward_split(draws, train_ratio=0.7)
    series = split["series"]
    config = dict(A.DEFAULT_SETTINGS)
    reference = A.walk_forward_eval_config(
        series, config, split_index=split["split_index"]
    )
    out = A.backtest_stats(
        series,
        base_settings=config,
        include_results=True,
        include_wave_breakdown=False,
    )
    hits = [bool(row["hit"]) for row in out["results"]]
    derived = A.window_delta_from_hits(
        hits, split["split_index"] - split["eval_start"], out["random_baseline_hit_rate"]
    )
    assert derived["train"]["hits"] == reference["train_hits"]
    assert derived["train"]["delta"] == pytest.approx(reference["train_delta"])
    assert derived["train_eval"] == reference["train_evaluated"]
    assert derived["valid_eval"] == reference["valid_evaluated"]


# --------------------------------------------------------------------------- #
# scripts/challenge_210_draws 核心换算
# --------------------------------------------------------------------------- #
def test_harness_valid_segment_reproduces_walk_forward_valid() -> None:
    draws = synthetic_draws(120)
    split = A.walk_forward_split(draws, train_ratio=0.7)
    config = dict(A.DEFAULT_SETTINGS)
    reference = A.walk_forward_eval_config(
        split["series"], config, split_index=split["split_index"]
    )
    segment = C.harness_valid_segment(
        split["series"], config, split["split_index"], A.MIN_PRIOR_DRAWS
    )
    assert segment["hits"] == reference["valid_hits"]
    assert segment["evaluated"] == reference["valid_evaluated"]
    assert segment["delta"] == pytest.approx(reference["valid_delta"])


def test_evaluate_all_selects_champion_by_train_delta() -> None:
    draws = synthetic_draws(120)
    series = A.walk_forward_split(draws)["series"]
    configs = [
        dict(A.DEFAULT_SETTINGS),
        {**A.DEFAULT_SETTINGS, "pick_strategy": "score_top"},
    ]
    block = C.evaluate_all(
        series,
        configs,
        exp3_windows=(20,),
        exp4_windows=(120,),
        min_prior_draws=A.MIN_PRIOR_DRAWS,
    )
    window = block["exp3"]["20"]
    selected = window["selected"]
    # 冠军必须同时在 harness 口径与 full-history 口径下等于「训练窗 Δ 最高」的那组
    assert 0 <= selected["config_index"] < len(configs)
    assert window["valid_eval"] == 20
    assert window["train_eval"] == block["eval_count"] - 20
    # 整池窗口的 exp4 统计量应当与 exp1 完全一致（同一批回测）
    assert block["exp4"]["120"]["max_full_delta"] == pytest.approx(
        block["exp1"]["max_full_delta"]
    )
    assert block["exp4"]["120"]["argmax_index"] == block["exp1"]["argmax_index"]


def test_prespecified_table_reports_exact_binomial_p() -> None:
    draws = synthetic_draws(120)
    series = A.walk_forward_split(draws)["series"]
    rows = C.prespecified_table(
        series,
        [{"name": "默认", "config": dict(A.DEFAULT_SETTINGS), "pre_specified": True}],
        baseline_rate=0.20408163265306123,
        min_prior_draws=A.MIN_PRIOR_DRAWS,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["evaluated"] == 118
    assert 0.0 <= row["p_greater"] <= 1.0
    assert 0.0 <= row["p_two_sided"] <= 1.0
    assert row["hit_rate"] == pytest.approx(row["hits"] / row["evaluated"])
    assert row["delta"] == pytest.approx(row["hit_rate"] - 0.20408163265306123)


def test_apply_permutation_preserves_window_multiset_and_dates() -> None:
    draws = synthetic_draws(60)
    series = A.walk_forward_split(draws)["series"]
    permuted = C.apply_permutation(series, 3, 40)
    assert len(permuted) == 40
    # 窗口内的号码多集不变，只换位置；期号与日期原样
    assert sorted(row["special_number"] for row in permuted) == sorted(
        row["special_number"] for row in series[-40:]
    )
    assert [row["period"] for row in permuted] == [
        row["period"] for row in series[-40:]
    ]
    # 同 seed ⇒ 可复现
    again = C.apply_permutation(series, 3, 40)
    assert [row["special_number"] for row in again] == [
        row["special_number"] for row in permuted
    ]


def test_adversarial_findings_flags_only_small_pvalues() -> None:
    null = {
        "exp1": {"max_full_delta": {"p_one_sided": 0.20, "observed": 0.07, "mean": 0.06, "n": 100}},
        "exp3": {
            "30": {
                "selected_valid_delta_harness": {
                    "p_one_sided": 0.02,
                    "observed": 0.15,
                    "mean": 0.01,
                    "n": 100,
                }
            }
        },
        "exp4": {"60": {"max_full_delta": {"p_one_sided": 0.5, "observed": 0.1, "mean": 0.12, "n": 100}}},
    }
    findings = C.adversarial_findings(
        null=null,
        prespecified=[{"name": "默认", "hit_rate": 0.18, "p_greater": 0.8}],
        grid_significance={"tested": 256, "best_p_greater": {"index": 5, "p_greater": 0.014, "hit_rate": 0.27}},
    )
    families = {finding["family"] for finding in findings}
    assert "exp3 验证窗 30 期" in families
    assert "exp2 整池 256 组里的最小原始 p（未校正）" in families
    assert all(finding["p_one_sided"] < 0.05 for finding in findings)
    assert len(findings) == 2


def test_adversarial_findings_empty_when_nothing_significant() -> None:
    null = {
        "exp1": {"max_full_delta": {"p_one_sided": 0.3}},
        "exp3": {},
        "exp4": {},
    }
    assert (
        C.adversarial_findings(
            null=null,
            prespecified=[{"name": "默认", "p_greater": 0.9}],
            grid_significance={"tested": 256, "best_p_greater": {"p_greater": 0.4, "index": 1}},
        )
        == []
    )
