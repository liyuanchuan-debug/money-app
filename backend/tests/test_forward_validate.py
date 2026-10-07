"""``services.forward_ledger`` / ``scripts.forward_validate`` 的前瞻验证协议测试。

只测**纯逻辑**：不连数据库、不读 ``backend/data/*``（该目录被 .gitignore 忽略），
所有开奖序列都在测试内确定性合成（固定 seed），账本一律写进 ``tmp_path``。

覆盖的协议要点：

- **append-only**：同一期已有记录 → 追加第二条必须硬报错；已开奖期 → 禁止事后补冻；
- **哈希链**：改写任何一条记录（选号 / 金额 / 配置 / prev_hash），``verify`` 必须失败；
- **无前视**：``available_length == period_index``，可用前缀绝不含目标期，
  且**换掉目标期的开奖结果**不影响已冻结记录（不依赖答案）；
- **pending 不计分**：未开奖的期只报 pending，命中率 / 收支一律不参与统计；
- **计分口径**：与手工算例逐项对齐，且二项检验复用 ``services.analytics``；
- **导入守卫**：线上路径（``services/lottery.py`` / ``routers/*`` / ``main.py``）
  不得引用本账本模块；
- **编码 / 口径守卫**：枚举英文码、源码无 BOM、无 U+FFFD。
"""

from __future__ import annotations

import copy
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from scripts import forward_validate as FV
from services import analytics as A
from services import forward_ledger as FL
from services.lottery import DEFAULT_SETTINGS

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# 测试配置：仓库默认设置 + 10 注（与线上回测口径一致 → 均匀基线 10/49）
TEST_SETTINGS: dict[str, Any] = {**DEFAULT_SETTINGS, "pick_count": 10}
FROZEN_AT = "2026-10-07T00:00:00+00:00"


def synthetic_draws(
    count: int = 40, *, start_period: int = 1, seed: int = 20261007
) -> list[dict[str, Any]]:
    """确定性合成开奖（期号连续、日期连续、特码 1..49）。"""
    rng = random.Random(seed)
    start = date(2026, 1, 1)
    return [
        {
            "period": start_period + index,
            "draw_date": (start + timedelta(days=index)).isoformat(),
            "special_number": rng.randrange(1, 50),
        }
        for index in range(count)
    ]


def freeze(
    draws: Sequence[dict[str, Any]],
    periods: Sequence[int],
    *,
    ledger: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict:
    base = ledger or {
        "version": FL.LEDGER_VERSION,
        "genesis_hash": FL.GENESIS_HASH,
        "records": [],
    }
    return FL.freeze_periods(
        base,
        draws,
        periods,
        settings=kwargs.pop("settings", TEST_SETTINGS),
        frozen_at=kwargs.pop("frozen_at", FROZEN_AT),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# append-only / 禁止事后补冻
# --------------------------------------------------------------------------- #
def test_freeze_refuses_a_second_record_for_the_same_period() -> None:
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31])
    assert [record["period"] for record in ledger["records"]] == [31]

    with pytest.raises(FL.ImmutabilityError):
        freeze(draws, [31], ledger=ledger)

    # 已有记录 + 新期号混在一起时，也不能只写一半（整体拒绝）
    with pytest.raises(FL.ImmutabilityError):
        FL.freeze_periods(
            ledger,
            draws,
            [32, 31],
            settings=TEST_SETTINGS,
            frozen_at=FROZEN_AT,
        )
    assert [record["period"] for record in ledger["records"]] == [31]


def test_freeze_refuses_a_period_that_has_already_been_drawn() -> None:
    draws = synthetic_draws(30)  # 最新已开奖第 30 期
    with pytest.raises(FL.LookAheadError):
        freeze(draws, [30])
    with pytest.raises(FL.LookAheadError):
        freeze(draws, [29, 31])  # 混入一个已开奖期 → 整体拒绝


def test_freeze_accepts_only_undrawn_periods_and_chains_them() -> None:
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31, 32, 33])
    records = ledger["records"]
    assert [record["period"] for record in records] == [31, 32, 33]
    assert records[0]["prev_hash"] == FL.GENESIS_HASH
    assert records[1]["prev_hash"] == records[0]["record_hash"]
    assert records[2]["prev_hash"] == records[1]["record_hash"]


# --------------------------------------------------------------------------- #
# 哈希链 / 数据摘要篡改检测
# --------------------------------------------------------------------------- #
def test_verify_passes_on_a_freshly_frozen_ledger() -> None:
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31, 32])
    result = FL.verify_ledger(ledger, draws)
    assert result["ok"], result["problems"]
    assert result["records"] == 2
    assert result["chain_root"] == ledger["records"][-1]["record_hash"]
    assert result["problems"] == []


def test_verify_detects_a_mutated_pick() -> None:
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31, 32])
    mutated = copy.deepcopy(ledger)
    entry = mutated["records"][0]["strategies"][0]
    entry["picks"][0] = (int(entry["picks"][0]) % 49) + 1  # 保证与原值不同

    result = FL.verify_ledger(mutated, draws)
    assert not result["ok"]
    assert any("record_hash" in problem for problem in result["problems"])
    # 链本身没断：错的只有被改写那条
    assert result["details"][1]["ok"], result["details"][1]["problems"]


def test_verify_detects_a_mutated_setting_via_data_digest() -> None:
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31, 32])
    mutated = copy.deepcopy(ledger)
    mutated["records"][1]["settings"]["pick_count"] = 3

    result = FL.verify_ledger(mutated, draws)
    assert not result["ok"]
    joined = " ".join(result["problems"])
    assert "record_hash" in joined
    assert "data_digest" in joined


def test_verify_detects_a_broken_chain() -> None:
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31, 32])
    mutated = copy.deepcopy(ledger)
    mutated["records"][1]["prev_hash"] = "0" * 64

    result = FL.verify_ledger(mutated, draws)
    assert not result["ok"]
    assert any("链断裂" in problem for problem in result["problems"])


def test_verify_detects_a_shortened_available_prefix() -> None:
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31])
    mutated = copy.deepcopy(ledger)
    mutated["records"][0]["available_last_period"] = 25  # 偷改可用前缀

    result = FL.verify_ledger(mutated, draws)
    assert not result["ok"]
    assert any("期数不符" in problem or "摘要" in problem for problem in result["problems"])


# --------------------------------------------------------------------------- #
# 无前视（no look-ahead）
# --------------------------------------------------------------------------- #
def test_available_length_equals_period_index_and_excludes_the_target() -> None:
    draws = synthetic_draws(20)  # 第 1..20 期
    ledger = freeze(draws, [21, 22])
    for record in ledger["records"]:
        target = int(record["period"])
        assert record["available_length"] == record["period_index"] == 20
        assert record["available_last_period"] == 20
        available = FL.available_rows(record["available_last_period"], draws)
        assert len(available) == record["available_length"]
        assert all(int(row["period"]) < target for row in available)
        for entry in record["strategies"]:
            assert entry["available_length"] == record["available_length"]
            assert entry["pick_count"] == 10
            assert len(set(entry["picks"])) == len(entry["picks"])  # 不重复选号
            assert min(entry["picks"]) >= 1 and max(entry["picks"]) <= 49
    # 第 22 期冻结时看不到第 21 期（当时还没开奖）—— 记录里必须如实反映
    assert ledger["records"][1]["available_last_period"] == 20


def test_frozen_record_does_not_depend_on_the_target_outcome() -> None:
    """把目标期的开奖结果换掉：已冻结记录仍然自洽（它从未读过答案）。"""
    draws = synthetic_draws(20)
    ledger = freeze(draws, [21])

    for outcome in (1, 7, 49):
        extended = [*draws, {"period": 21, "draw_date": "2026-01-21", "special_number": outcome}]
        result = FL.verify_ledger(ledger, extended)
        assert result["ok"], (outcome, result["problems"])


def test_digest_changes_when_the_available_history_changes() -> None:
    """可用前缀里改一个号 → data_digest / available_digest 必须变（摘要真的覆盖了输入）。"""
    draws = synthetic_draws(20)
    base = FL.build_record(
        period=21, draws=draws, settings=TEST_SETTINGS, pick_count=10, frozen_at=FROZEN_AT
    )
    tampered_history = copy.deepcopy(draws)
    tampered_history[0]["special_number"] = (int(tampered_history[0]["special_number"]) % 49) + 1
    changed = FL.build_record(
        period=21,
        draws=tampered_history,
        settings=TEST_SETTINGS,
        pick_count=10,
        frozen_at=FROZEN_AT,
    )
    assert changed["available_digest"] != base["available_digest"]
    assert changed["data_digest"] != base["data_digest"]
    assert changed["record_hash"] != base["record_hash"]


def test_records_are_deterministic_for_a_fixed_freeze_time() -> None:
    draws = synthetic_draws(20)
    first = freeze(draws, [21, 22])
    second = freeze(draws, [21, 22])
    assert first == second


# --------------------------------------------------------------------------- #
# 独立性 / 有效独立样本量（prediction_digest）
# --------------------------------------------------------------------------- #
COMMITTED_LEDGER_PATH = BACKEND_ROOT / "forward_ledger" / "ledger.json"
# 已提交账本前三条（第 280/281/282 期）的链根：append-only 意味着它永远不该变
COMMITTED_THIRD_RECORD_ROOT = "91af45bfea0e2ad0b13809cc5dc84efbf836342a54b074c8de27c14ae7063216"


def _entry_of(record: Mapping[str, Any], strategy: str) -> dict[str, Any]:
    return next(item for item in record["strategies"] if item["strategy"] == strategy)


def test_prediction_digest_is_stable_across_disk_round_trip_and_tracks_picks(
    tmp_path: Path,
) -> None:
    """prediction_digest 必须「同一预测恒同值、任一组成变化即变值」。"""
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31])
    record = ledger["records"][0]
    entry = _entry_of(record, FL.STRATEGY_PRODUCTION)
    stored = str(entry["prediction_digest"])

    # 现算（老记录路径）必须与冻结时写死的值一致
    assert FL.entry_prediction_digest(entry, record["settings"]) == stored

    # 真正写盘再读回：摘要一字不差（int 键 / 浮点配置都要稳定）
    path = tmp_path / "ledger.json"
    FL.dump_ledger(ledger, path)
    reloaded = FL.load_ledger(path)["records"][0]
    assert reloaded["strategies"][0]["prediction_digest"] == stored
    assert FL.entry_prediction_digest(reloaded["strategies"][0], reloaded["settings"]) == stored
    assert (
        FL.independence_report({"records": [reloaded]})["by_period"][31][
            FL.STRATEGY_PRODUCTION
        ]["prediction_digest"]
        == stored
    )

    # 换选号 / 换注码 / 换配置 / 换策略 —— 任一项变化，摘要必须变
    other_picks = copy.deepcopy(entry["picks"])
    other_picks[0] = (int(other_picks[0]) % 49) + 1
    assert (
        FL.prediction_digest(
            FL.STRATEGY_PRODUCTION, other_picks, entry["amounts"], record["settings"]
        )
        != stored
    )
    assert (
        FL.prediction_digest(
            FL.STRATEGY_PRODUCTION,
            entry["picks"],
            [int(value) + 5 for value in entry["amounts"]],
            record["settings"],
        )
        != stored
    )
    other_settings = {
        **record["settings"],
        "total_amount": int(record["settings"]["total_amount"]) + 5,
    }
    assert (
        FL.prediction_digest(
            FL.STRATEGY_PRODUCTION, entry["picks"], entry["amounts"], other_settings
        )
        != stored
    )
    assert (
        FL.prediction_digest(
            FL.STRATEGY_UNIFORM, entry["picks"], entry["amounts"], record["settings"]
        )
        != stored
    )


def test_identical_picks_are_marked_non_independent_at_freeze_time() -> None:
    """同一前缀冻结两期 → 线上引擎同一注：第二条必须 independent=false + 指回第一期。"""
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31, 32])  # 故意一次冻结两期（会产生重复登记）
    first, second = ledger["records"]

    prod_first = _entry_of(first, FL.STRATEGY_PRODUCTION)
    prod_second = _entry_of(second, FL.STRATEGY_PRODUCTION)
    assert prod_first["picks"] == prod_second["picks"], "同一前缀下线上引擎应当给出同一注"
    assert prod_first["independent"] is True
    assert prod_first["duplicate_of_period"] is None
    assert prod_second["independent"] is False
    assert prod_second["duplicate_of_period"] == 31
    assert prod_second["prediction_digest"] == prod_first["prediction_digest"]

    # FIT_ONLY 演示行同样确定性 → 也是重复登记
    fit_second = _entry_of(second, FL.STRATEGY_FIT_DEMO)
    assert fit_second["independent"] is False
    assert fit_second["duplicate_of_period"] == 31

    # UNIFORM 对照按期号播种 → 每期选号都不同，必须仍是独立预测
    uniform_second = _entry_of(second, FL.STRATEGY_UNIFORM)
    assert uniform_second["picks"] != _entry_of(first, FL.STRATEGY_UNIFORM)["picks"]
    assert uniform_second["independent"] is True
    assert uniform_second["duplicate_of_period"] is None

    # 推导口径必须与冻结时写死的口径一致（verify 靠它抓「谎报独立性」）
    derived = FL.independence_report(ledger)["by_period"]
    assert derived[32][FL.STRATEGY_PRODUCTION]["independent"] is False
    assert derived[32][FL.STRATEGY_PRODUCTION]["duplicate_of_period"] == 31

    # 老记录（没有这些字段）也必须能被现算出来
    legacy = {"records": [{k: v for k, v in first.items()}]}
    legacy["records"][0]["strategies"] = [
        {k: v for k, v in entry.items() if k not in ("independent", "duplicate_of_period", "prediction_digest")}
        for entry in first["strategies"]
    ]
    legacy_flags = FL.independence_report(legacy)["by_period"][31][FL.STRATEGY_PRODUCTION]
    assert legacy_flags["digest_source"] == FL.DIGEST_SOURCE_DERIVED
    assert legacy_flags["prediction_digest"] == str(prod_first["prediction_digest"])


def test_one_freeze_per_draw_cycle_keeps_every_row_independent() -> None:
    """规范工作流：每期开奖后再冻结下一期 → 新数据带来新预测，全部是独立预测。"""
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31])
    extended = [*draws, {"period": 31, "draw_date": "2026-01-31", "special_number": 5}]
    ledger = freeze(extended, [32], ledger=ledger)

    first, second = ledger["records"]
    assert second["available_length"] == 31 > first["available_length"] == 30
    for record in ledger["records"]:
        for entry in record["strategies"]:
            assert entry["independent"] is True, (record["period"], entry["strategy"])
            assert entry["duplicate_of_period"] is None
    # 前缀变了 → 线上引擎的选号（因而摘要）必须变
    assert (
        _entry_of(second, FL.STRATEGY_PRODUCTION)["prediction_digest"]
        != _entry_of(first, FL.STRATEGY_PRODUCTION)["prediction_digest"]
    )


def test_next_undrawn_periods_skips_periods_already_frozen() -> None:
    """一次冻结多期留下的「已冻结未开奖」期号，不能再把规范流程堵死。"""
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31, 32])
    extended = [*draws, {"period": 31, "draw_date": "2026-01-31", "special_number": 5}]

    # 不跳过时（老行为）会算出 32 —— 而 32 已经冻结，下一次 freeze 必定报错
    assert FL.next_undrawn_periods(extended, 1) == [32]
    # 跳过账本已有期号 → 顺延到 33，规范流程可以继续
    skip = [int(record["period"]) for record in ledger["records"]]
    assert FL.next_undrawn_periods(extended, 1, skip_periods=skip) == [33]
    assert FL.next_undrawn_periods(extended, 2, skip_periods=skip) == [33, 34]

    ledger = freeze(extended, [33], ledger=ledger)
    assert [int(record["period"]) for record in ledger["records"]] == [31, 32, 33]
    # 第 33 期看得到第 31 期开奖 → 是新预测（独立），而不是重复登记
    assert _entry_of(ledger["records"][2], FL.STRATEGY_PRODUCTION)["independent"] is True


def test_score_uses_effective_independent_rows_not_scored_rows() -> None:
    """3 条已计分（2 条同一注 + 1 条另一注）→ 统计必须按有效独立 2 条算，不是 3 条。"""
    picks_a = [7, 1, 2, 3, 4, 5, 6, 8, 9, 10]  # 含 7 → 命中
    picks_b = [11, 12, 13, 14, 15, 16, 17, 18, 19, 21]  # 不含 7/20 → 未命中
    ranking_hit = [11, 12, 7, *[n for n in range(1, 50) if n not in (11, 12, 7)]]
    ranking_miss = [20, *[n for n in range(1, 50) if n != 20]]
    ledger = {
        "version": FL.LEDGER_VERSION,
        "genesis_hash": FL.GENESIS_HASH,
        "records": [
            {"period": 91, "strategies": [_hand_entry(picks_a, ranking_hit)]},
            {"period": 92, "strategies": [_hand_entry(picks_a, ranking_hit)]},  # 与 91 逐字相同
            {"period": 93, "strategies": [_hand_entry(picks_b, ranking_miss)]},
        ],
    }
    draws = [
        *synthetic_draws(90),
        {"period": 91, "draw_date": "2026-04-01", "special_number": 7},
        {"period": 92, "draw_date": "2026-04-02", "special_number": 7},
        {"period": 93, "draw_date": "2026-04-03", "special_number": 20},
    ]
    block = FL.score_ledger(ledger, draws)["strategies"][FL.STRATEGY_UNIFORM]

    assert block["scored_rows"] == 3
    assert block["effective_independent_rows"] == 2  # 92 被排除
    assert block["inference_sample_size"] == 2
    assert len(block["excluded_duplicates"]) == 1
    excluded = block["excluded_duplicates"][0]
    assert excluded["period"] == 92
    assert excluded["duplicate_of_period"] == 91
    assert excluded["reason"] == FL.EXCLUDE_REASON_DUPLICATE
    assert excluded["hit"] is True  # 重复行确实命中了，但仍然不参与统计

    # 统计口径：有效独立 2 条（91 命中 + 93 未命中）→ 1/2，而不是全行的 2/3
    assert block["hits"] == 1
    assert block["hit_rate"] == pytest.approx(0.5)
    assert block["all_rows"]["scored_rows"] == 3
    assert block["all_rows"]["hits"] == 2
    assert block["all_rows"]["hit_rate"] == pytest.approx(2.0 / 3.0)

    # 二项检验 / 最小可检测 delta 必须用 n=2（有效独立），不是 n=3（已计分行）
    assert block["binomial_p_greater"] == pytest.approx(
        A.binomial_tail_p(1, 2, 10.0 / 49.0, alternative="greater")
    )
    assert block["binomial_p_greater"] != pytest.approx(
        A.binomial_tail_p(2, 3, 10.0 / 49.0, alternative="greater")
    )
    assert block["power"]["evaluated"] == 2
    assert block["power"]["minimum_detectable_delta"] == pytest.approx(
        A.minimum_detectable_delta(2, 10.0 / 49.0)["minimum_detectable_delta"]
    )
    assert block["power"]["minimum_detectable_delta"] != pytest.approx(
        A.minimum_detectable_delta(3, 10.0 / 49.0)["minimum_detectable_delta"]
    )

    # 收支记全部已计分行（真金白银下了 3 注）；去重口径另给 independent_*
    assert block["total_spend"] == pytest.approx(150.0)
    assert block["total_return"] == pytest.approx(470.0)
    assert block["net_pnl"] == pytest.approx(320.0)
    assert block["independent_spend"] == pytest.approx(100.0)
    assert block["independent_return"] == pytest.approx(235.0)
    assert block["independent_net_pnl"] == pytest.approx(135.0)
    # 被排除这件事必须写进 notes（可见、有理由），不是静默折叠
    assert any("DUPLICATE_PREDICTION" in note for note in block["notes"])


def test_zero_independent_rows_emit_no_p_value() -> None:
    """没有已开奖的冻结期 → 有效独立 0 条：只报 pending，绝不给 p 值 / 判定。"""
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31, 32])  # 两期都还没开奖
    block = FL.score_ledger(ledger, draws)["strategies"][FL.STRATEGY_PRODUCTION]

    assert block["frozen_rows"] == 2
    assert block["scored_rows"] == 0
    assert block["effective_independent_rows"] == 0
    assert block["evidence_status"] == FL.EVIDENCE_STATUS_NO_SCORED_ROWS
    assert block["binomial_p_greater"] is None
    assert block["binomial_p_two_sided"] is None
    assert block["power"] is None
    assert block["verdict"] is None
    assert block["hit_rate"] is None


def test_all_identical_scored_rows_collapse_to_one_observation() -> None:
    """所有已计分行都是同一注 → 有效独立 1 条：报观测值，但明确「不给显著性」。"""
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31, 32])  # 同前缀 → 线上引擎两期同一注
    extended = [
        *draws,
        {"period": 31, "draw_date": "2026-01-31", "special_number": 5},
        {"period": 32, "draw_date": "2026-02-01", "special_number": 9},
    ]
    block = FL.score_ledger(ledger, extended)["strategies"][FL.STRATEGY_PRODUCTION]

    assert block["scored_rows"] == 2
    assert block["effective_independent_rows"] == 1
    assert len(block["excluded_duplicates"]) == 1
    assert block["evidence_status"] == FL.EVIDENCE_STATUS_SINGLE_ROW
    # 观测值照报（这一注的命中率 / 排名 / 收支）
    assert block["hit_rate"] in (0.0, 1.0)
    assert block["total_spend"] == pytest.approx(100.0)
    # 但绝不给 p 值 / 判定：单条观测无法区分随机与优势
    assert block["binomial_p_greater"] is None
    assert block["binomial_p_two_sided"] is None
    assert block["power"] is None
    assert block["verdict"] is None
    assert any("1 条有效独立预测" in note for note in block["notes"])


def test_status_reports_raw_and_effective_sample_sizes() -> None:
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31, 32, 33])  # 1 期后每期都是重复登记（共 9 条 = 5 独立 + 4 重复）
    payload = FL.status_payload(ledger, draws)

    assert payload["frozen_rows_total"] == 9
    assert payload["independent_rows_frozen"] == 5
    assert payload["duplicate_rows_frozen"] == 4
    assert payload["effective_independent_rows_total"] == 0  # 都还没开奖
    assert payload["frozen_rows_by_strategy"][FL.STRATEGY_PRODUCTION] == 3
    assert payload["effective_independent_rows_by_strategy"][FL.STRATEGY_PRODUCTION] == 0
    assert payload["integrity_ok"] is True


def test_verify_catches_a_lied_about_independence_flag() -> None:
    """即使把整条链重新签名，谎报 independent=true 也必须被推导口径抓住。"""
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31, 32])
    lied = copy.deepcopy(ledger)
    entry = _entry_of(lied["records"][1], FL.STRATEGY_PRODUCTION)
    entry["independent"] = True
    entry["duplicate_of_period"] = None
    # 重新签名整条链（攻击者能改内容、也能重算哈希）
    previous = FL.GENESIS_HASH
    for record in lied["records"]:
        record["prev_hash"] = previous
        record["record_hash"] = FL.compute_record_hash(record, previous)
        previous = record["record_hash"]

    result = FL.verify_ledger(lied, draws)
    assert not result["ok"]
    assert any("独立性标记被改写" in problem for problem in result["problems"])
    # 原账本不受影响
    assert FL.verify_ledger(ledger, draws)["ok"] is True


def test_committed_three_record_chain_still_verifies() -> None:
    """回归守卫：改动本模块后，已提交的三条记录（280/281/282）与链根必须原样可验。

    哈希链部分不依赖开奖数据文件（`backend/data/*` 被 gitignore）；若本地存在该文件，
    再跑一次完整的 `verify_ledger`（含 available_digest / data_digest 重算）。
    """
    ledger = FL.load_ledger(COMMITTED_LEDGER_PATH)
    records = ledger["records"]
    assert len(records) >= 3
    assert [int(record["period"]) for record in records[:3]] == [280, 281, 282]

    previous = FL.GENESIS_HASH
    for record in records:
        assert record["prev_hash"] == previous
        assert FL.compute_record_hash(record, previous) == record["record_hash"]
        previous = record["record_hash"]
    assert records[2]["record_hash"] == COMMITTED_THIRD_RECORD_ROOT

    # 三条历史记录应该是「老格式」（没有 prediction_digest 字段）→ 等价于未知
    assert all(
        "prediction_digest" not in entry
        for record in records[:3]
        for entry in record["strategies"]
    )

    draws_path = BACKEND_ROOT / "data" / "draws_70_279.json"
    if draws_path.exists():
        result = FL.verify_ledger(ledger, FL.load_draws(draws_path))
        assert result["ok"], result["problems"]
        assert result["chain_root"] == previous
        assert result["details"][2]["record_hash"] == COMMITTED_THIRD_RECORD_ROOT

    # 老记录也能被现算独立性：线上引擎 280/281/282 是同一注 → 后两条是重复登记
    flags = FL.independence_report(ledger)["by_period"]
    assert flags[280][FL.STRATEGY_PRODUCTION]["independent"] is True
    assert flags[281][FL.STRATEGY_PRODUCTION]["independent"] is False
    assert flags[281][FL.STRATEGY_PRODUCTION]["duplicate_of_period"] == 280
    assert flags[281][FL.STRATEGY_PRODUCTION]["digest_source"] == FL.DIGEST_SOURCE_DERIVED
    assert flags[282][FL.STRATEGY_UNIFORM]["independent"] is True  # 均匀对照按期播种


# --------------------------------------------------------------------------- #
# pending 绝不计分
# --------------------------------------------------------------------------- #
def test_pending_records_are_never_scored() -> None:
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31, 32])
    score = FL.score_ledger(ledger, draws)

    assert score["scored_periods"] == []
    assert score["pending_periods"] == [31, 32]
    assert set(score["strategies"]) == set(FL.ALL_STRATEGIES)
    for sid, block in score["strategies"].items():
        assert block["evaluated"] == 0, sid
        assert block["pending"] == 2, sid
        assert block["hit_rate"] is None
        assert block["mean_rank"] is None
        assert block["log_loss"] is None
        assert block["brier"] is None
        assert block["net_pnl"] == 0.0
        assert block["total_spend"] == 0.0
        assert block["binomial_p_greater"] is None
        assert block["power"] is None
        assert block["verdict"] is None


def test_pending_periods_do_not_enter_the_scored_metrics() -> None:
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31, 32, 33])
    # 只让 31 开奖
    extended = [
        *draws,
        {"period": 31, "draw_date": "2026-01-31", "special_number": 11},
    ]
    score = FL.score_ledger(ledger, extended)
    assert score["scored_periods"] == [31]
    assert score["pending_periods"] == [32, 33]
    for block in score["strategies"].values():
        assert block["evaluated"] == 1
        assert block["pending"] == 2


# --------------------------------------------------------------------------- #
# 计分：手工算例
# --------------------------------------------------------------------------- #
def _hand_entry(
    picks: Sequence[int],
    ranking: Sequence[int],
    *,
    amounts: Sequence[int] | None = None,
    odds: float = 47.0,
) -> dict[str, Any]:
    amounts = list(amounts) if amounts is not None else [5] * len(picks)
    return {
        "strategy": FL.STRATEGY_UNIFORM,
        "label": "hand",
        "fit_role": FL.FIT_ROLE_LIVE,
        "available_length": 10,
        "pick_count": len(picks),
        "picks": list(picks),
        "amounts": amounts,
        "staked_total": int(sum(amounts)),
        "odds": float(odds),
        "budget": 50,
        "mode": "even",
        "ranking": list(ranking),
        "probabilities": {n: 1.0 / 49.0 for n in range(1, 50)},
    }


def _hand_ledger(draws: Sequence[dict[str, Any]]) -> dict[str, Any]:
    # 两条已计分行必须是**不同的预测**（picks 不同）—— 否则会被判为重复登记，
    # 有效独立样本只剩 1 条，就不再是「手工算例」了（见独立性专门测试）。
    picks_hit = [7, 1, 2, 3, 4, 5, 6, 8, 9, 10]
    picks_miss = [11, 12, 13, 14, 15, 16, 17, 18, 19, 21]
    ranking_hit = [11, 12, 7, *[n for n in range(1, 50) if n not in (11, 12, 7)]]
    ranking_miss = [20, *[n for n in range(1, 50) if n != 20]]
    return {
        "version": FL.LEDGER_VERSION,
        "genesis_hash": FL.GENESIS_HASH,
        "records": [
            {"period": 91, "strategies": [_hand_entry(picks_hit, ranking_hit)]},
            {"period": 92, "strategies": [_hand_entry(picks_miss, ranking_miss)]},
            {"period": 93, "strategies": [_hand_entry(picks_hit, ranking_hit)]},  # 未开奖
        ],
    }


def test_scoring_matches_a_hand_computed_case() -> None:
    draws = [
        *synthetic_draws(90),
        {"period": 91, "draw_date": "2026-04-01", "special_number": 7},  # 命中
        {"period": 92, "draw_date": "2026-04-02", "special_number": 20},  # 未命中
    ]
    score = FL.score_ledger(_hand_ledger(draws), draws)
    block = score["strategies"][FL.STRATEGY_UNIFORM]

    assert score["scored_periods"] == [91, 92]
    assert score["pending_periods"] == [93]
    assert block["evaluated"] == 2
    # 两条预测不同 → 有效独立样本 = 已计分行数 = 2（推断样本量就是它）
    assert block["scored_rows"] == 2
    assert block["effective_independent_rows"] == 2
    assert block["inference_sample_size"] == 2
    assert block["frozen_rows"] == 3
    assert block["excluded_duplicates"] == []
    assert block["evidence_status"] == FL.EVIDENCE_STATUS_OK
    assert block["hits"] == 1
    assert block["hit_rate"] == pytest.approx(0.5)
    # 排名：第 91 期实际号 7 排第 3；第 92 期实际号 20 排第 1 → 平均 2.0
    assert block["mean_rank"] == pytest.approx(2.0)
    # 概率向量均匀 → 与均匀参考逐项相等
    assert block["log_loss"] == pytest.approx(math.log(49))
    assert block["brier"] == pytest.approx((1.0 / 49.0) * (1.0 - 1.0 / 49.0))
    # 10 注 × 5 元 × 2 期 = 100 元；命中那注 5 元 × 47 = 235 元
    assert block["total_spend"] == pytest.approx(100.0)
    assert block["total_return"] == pytest.approx(235.0)
    assert block["net_pnl"] == pytest.approx(135.0)
    assert block["baseline_hit_rate"] == pytest.approx(10.0 / 49.0)
    assert block["uniform_reference"]["mean_rank"] == 25.0
    assert block["uniform_reference"]["log_loss"] == pytest.approx(math.log(49))
    # 二项检验必须复用 services.analytics（同一函数、同一口径）
    assert block["binomial_p_greater"] == pytest.approx(
        A.binomial_tail_p(1, 2, 10.0 / 49.0, alternative="greater")
    )
    assert block["binomial_p_two_sided"] == pytest.approx(
        A.binomial_tail_p(1, 2, 10.0 / 49.0, alternative="two-sided")
    )
    power = block["power"]
    assert power["evaluated"] == 2
    assert power["minimum_detectable_delta"] == pytest.approx(
        A.minimum_detectable_delta(2, 10.0 / 49.0)["minimum_detectable_delta"]
    )
    # 2 期远不足以定论 → 只能报 noise
    assert block["verdict"]["kind"] == A.VERDICT_NOISE


def test_production_engine_row_has_no_fabricated_density_metrics() -> None:
    """线上引擎只输出 top-k：mean_rank / log_loss / brier 必须是 null，不是 0。"""
    draws = synthetic_draws(20)
    ledger = freeze(draws, [21])
    entry = next(
        item for item in ledger["records"][0]["strategies"]
        if item["strategy"] == FL.STRATEGY_PRODUCTION
    )
    assert entry["ranking"] is None
    assert entry["probabilities"] is None

    extended = [*draws, {"period": 21, "draw_date": "2026-01-21", "special_number": 9}]
    block = FL.score_ledger(ledger, extended)["strategies"][FL.STRATEGY_PRODUCTION]
    assert block["evaluated"] == 1
    assert block["hit_rate"] is not None
    assert block["mean_rank"] is None
    assert block["log_loss"] is None
    assert block["brier"] is None


def test_uniform_control_matches_the_textbook_reference() -> None:
    """均匀对照的概率向量是严格均匀 → 密度指标恒等于教科书参考值。"""
    draws = synthetic_draws(20)
    ledger = freeze(draws, [21])
    extended = [*draws, {"period": 21, "draw_date": "2026-01-21", "special_number": 42}]
    block = FL.score_ledger(ledger, extended)["strategies"][FL.STRATEGY_UNIFORM]
    assert block["log_loss"] == pytest.approx(math.log(49))
    assert block["brier"] == pytest.approx((1.0 / 49.0) * (1.0 - 1.0 / 49.0))
    assert 1.0 <= float(block["mean_rank"]) <= 49.0
    assert block["baseline_hit_rate"] == pytest.approx(10.0 / 49.0)


def test_required_sample_size_uses_the_analytics_helper() -> None:
    """要检出 +5% 绝对优势（20.41% → 25.41%）在 80% 功效下约需 510 期。"""
    block = A.minimum_detectable_delta(1, 10.0 / 49.0, alpha=0.05, power=0.8)
    assert block["required_draws"]["+5%"] == 510
    assert block["required_draws"]["+2%"] == 3188
    assert block["required_draws"]["+10%"] == 128
    # MDD 处的功效略高于目标（双侧公式在 |Δ|=MDD 时已越过临界值）
    assert 0.8 <= block["power_at_mdd"] < 0.81


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #
def test_status_reports_counts_and_integrity() -> None:
    draws = synthetic_draws(30)
    ledger = freeze(draws, [31, 32])
    payload = FL.status_payload(ledger, draws)
    assert payload["integrity_ok"] is True
    assert payload["records"] == 2
    assert payload["pending_periods"] == [31, 32]
    assert payload["chain_root"] == ledger["records"][-1]["record_hash"]
    assert set(payload["strategies"]) == set(FL.ALL_STRATEGIES)
    assert payload["scored_periods"] == []


# --------------------------------------------------------------------------- #
# CLId
# --------------------------------------------------------------------------- #
def _write_fixtures(tmp_path: Path, draws: Sequence[dict[str, Any]]) -> tuple[Path, Path]:
    draws_path = tmp_path / "draws.json"
    draws_path.write_text(json.dumps(list(draws), ensure_ascii=False), encoding="utf-8")
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"settings": TEST_SETTINGS}, ensure_ascii=False), encoding="utf-8"
    )
    return draws_path, settings_path


def test_cli_end_to_end(tmp_path: Path) -> None:
    draws = synthetic_draws(30)
    draws_path, settings_path = _write_fixtures(tmp_path, draws)
    ledger_path = tmp_path / "ledger.json"

    FV.main(
        [
            "freeze", "--next", "2", "--ledger", str(ledger_path),
            "--draws-json", str(draws_path), "--settings-json", str(settings_path),
            "--out", str(tmp_path / "freeze.json"),
        ]
    )
    ledger = FL.load_ledger(ledger_path)
    assert [record["period"] for record in ledger["records"]] == [31, 32]

    FV.main(
        [
            "status", "--ledger", str(ledger_path), "--draws-json", str(draws_path),
            "--out", str(tmp_path / "status.json"),
        ]
    )
    FV.main(
        [
            "verify", "--ledger", str(ledger_path), "--draws-json", str(draws_path),
            "--out", str(tmp_path / "verify.json"),
        ]
    )

    # 开奖后计分
    after_dir = tmp_path / "after"
    after_dir.mkdir()
    extends_path, _ = _write_fixtures(
        after_dir,
        [*draws, {"period": 31, "draw_date": "2026-02-01", "special_number": 5}],
    )
    FV.main(
        [
            "score", "--ledger", str(ledger_path), "--draws-json", str(extends_path),
            "--out", str(tmp_path / "score.json"),
        ]
    )
    artifact = json.loads((tmp_path / "score.json").read_text(encoding="utf-8"))
    assert artifact["scored_periods"] == [31]
    assert artifact["pending_periods"] == [32]
    for block in artifact["strategies"].values():
        assert block["evaluated"] == 1
        assert block["rows"][0]["period"] == 31


def test_cli_freeze_next_skips_already_frozen_periods(tmp_path: Path) -> None:
    """CLI 层：开奖后 `freeze --next 1` 必须继续可用，而不是撞上已冻结的期号报错。"""
    draws = synthetic_draws(30)
    draws_path, settings_path = _write_fixtures(tmp_path, draws)
    ledger_path = tmp_path / "ledger.json"
    common = [
        "--ledger", str(ledger_path), "--settings-json", str(settings_path),
        "--out", str(tmp_path / "x.json"),
    ]
    FV.main(["freeze", "--next", "2", "--draws-json", str(draws_path), *common])
    assert [int(r["period"]) for r in FL.load_ledger(ledger_path)["records"]] == [31, 32]

    # 第 31 期开奖 → 规范流程再冻结一期：应顺延到 33（跳过已冻结的 32）
    after_dir = tmp_path / "after_next"
    after_dir.mkdir()
    extended_path, _ = _write_fixtures(
        after_dir,
        [*draws, {"period": 31, "draw_date": "2026-02-01", "special_number": 5}],
    )
    FV.main(["freeze", "--next", "1", "--draws-json", str(extended_path), *common])
    records = FL.load_ledger(ledger_path)["records"]
    assert [int(r["period"]) for r in records] == [31, 32, 33]
    # 新增的 33 期看得到第 31 期开奖 → 独立预测
    assert _entry_of(records[2], FL.STRATEGY_PRODUCTION)["independent"] is True


def test_cli_refuses_to_overwrite_and_to_backfill(tmp_path: Path) -> None:
    draws = synthetic_draws(30)
    draws_path, settings_path = _write_fixtures(tmp_path, draws)
    ledger_path = tmp_path / "ledger.json"
    common = [
        "--ledger", str(ledger_path), "--draws-json", str(draws_path),
        "--settings-json", str(settings_path), "--out", str(tmp_path / "x.json"),
    ]
    FV.main(["freeze", "--period", "31", *common])

    with pytest.raises(FL.ImmutabilityError):
        FV.main(["freeze", "--period", "31", *common])
    with pytest.raises(FL.LookAheadError):
        FV.main(["freeze", "--period", "30", *common])


def test_cli_verify_exits_nonzero_on_a_tampered_ledger(tmp_path: Path) -> None:
    draws = synthetic_draws(30)
    draws_path, settings_path = _write_fixtures(tmp_path, draws)
    ledger_path = tmp_path / "ledger.json"
    FV.main(
        [
            "freeze", "--period", "31", "--ledger", str(ledger_path),
            "--draws-json", str(draws_path), "--settings-json", str(settings_path),
            "--out", str(tmp_path / "freeze.json"),
        ]
    )
    tampered = json.loads(ledger_path.read_text(encoding="utf-8"))
    tampered["records"][0]["strategies"][0]["amounts"][0] = 999
    ledger_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        FV.main(
            [
                "verify", "--ledger", str(ledger_path), "--draws-json", str(draws_path),
                "--out", str(tmp_path / "verify.json"),
            ]
        )
    assert excinfo.value.code == 2


# --------------------------------------------------------------------------- #
# 导入守卫 / 枚举 / 编码守卫
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


def test_production_path_never_references_the_forward_ledger() -> None:
    sources = _live_path_sources()
    assert sources, "线上路径源码一个都没读到，守卫会静默失效"
    assert any(path.name == "lottery.py" for path, _ in sources)
    for path, text in sources:
        for banned in ("forward_ledger", "forward_validate"):
            assert banned not in text, (
                f"{path.name} 引用了 {banned}：前瞻账本只能读线上引擎，绝不能反向接入线上路径"
            )


def test_forward_ledger_is_not_wired_into_lottery_module() -> None:
    import services.lottery as lottery  # 局部导入：确认线上模块可正常加载

    assert not hasattr(lottery, "forward_ledger")
    assert not [name for name in vars(lottery) if "forward_ledger" in name]


def test_stored_enums_are_english_or_numeric() -> None:
    """落盘字段（期号 / 策略 id / fit_role / 模式 / 选号 / 金额 / 摘要）禁止汉字。"""
    draws = synthetic_draws(20)
    ledger = freeze(draws, [21])
    record = ledger["records"][0]
    stored = {
        "period": record["period"],
        "period_index": record["period_index"],
        "available_length": record["available_length"],
        "available_first_period": record["available_first_period"],
        "available_last_period": record["available_last_period"],
        "available_digest": record["available_digest"],
        "data_digest": record["data_digest"],
        "prev_hash": record["prev_hash"],
        "record_hash": record["record_hash"],
        "settings": record["settings"],
        "strategies": [
            {
                "strategy": entry["strategy"],
                "fit_role": entry["fit_role"],
                "mode": entry["mode"],
                "pick_count": entry["pick_count"],
                "picks": entry["picks"],
                "amounts": entry["amounts"],
                "staked_total": entry["staked_total"],
                "odds": entry["odds"],
                "budget": entry["budget"],
                "ranking": entry["ranking"],
                "probabilities": entry["probabilities"],
                # 独立性标记同样是落盘字段：必须英文 / 数字 / null，禁止汉字
                "prediction_digest": entry["prediction_digest"],
                "independent": entry["independent"],
                "duplicate_of_period": entry["duplicate_of_period"],
            }
            for entry in record["strategies"]
        ],
    }
    text = json.dumps(stored, ensure_ascii=False)
    assert not any("\u4e00" <= char <= "\u9fff" for char in text), text[:400]

    assert record["strategies"][0]["strategy"] == FL.STRATEGY_PRODUCTION
    for entry in record["strategies"]:
        assert entry["strategy"] in FL.ALL_STRATEGIES
        assert entry["fit_role"] in (FL.FIT_ROLE_LIVE, FL.FIT_ROLE_FIT_ONLY)
        assert entry["mode"] in ("even", "weighted", "single", "random")
        assert isinstance(entry["independent"], bool)
        assert entry["duplicate_of_period"] is None or isinstance(
            entry["duplicate_of_period"], int
        )
        assert len(str(entry["prediction_digest"])) == 64


def test_edited_sources_are_valid_utf8_without_bom_and_no_replacement_char() -> None:
    for relative in (
        "services/forward_ledger.py",
        "scripts/forward_validate.py",
        "tests/test_forward_validate.py",
        "forward_ledger/README.md",
        "forward_ledger/production_settings.json",
    ):
        raw = (BACKEND_ROOT / relative).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{relative} 带 BOM"
        text = raw.decode("utf-8")  # 非 UTF-8 会在这里抛错
        assert "\ufffd" not in text, f"{relative} 存在 U+FFFD 替换字符"
