"""样本内统计分析回归测试（``services.analytics`` + ``/api/stats``）。

覆盖：
- 频次：计数 / 出现率 / 期望值 / 偏差，以及「样本不足以支持强结论」的提示；
- 走势：相邻波动分类在边界上的判定（diff == small_max / normal_max / normal_max + 1）；
- 生肖走势：连出（最长 / 当前）与最近窗口覆盖 / 轮转；
- 回测：手工样本的已知答案（走步回测）、无未来函数验证、波动分解；
- 零开奖数据时全部接口返回 200 且带「数据不足」标记；
- 返回体里不出现「全市场」等越界措辞。

全部用例不依赖真实数据库（强制内存存储）。
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.analytics import (
    backtest_stats,
    frequency_stats,
    trend_stats,
    zodiac_trend_stats,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
REAL_DRAWS_FILE = BACKEND_DIR / "data" / "draws_70_269.txt"

STATS_GET_PATHS = (
    "/api/stats/frequency",
    "/api/stats/trend",
    "/api/stats/zodiac-trend",
)


@pytest.fixture(autouse=True)
def memory_store_env(monkeypatch):
    """强制内存存储，避免测试进程连到真实数据库。

    注意用**空串**而不是 delenv：main.py 在 import 时 load_dotenv() 会把
    backend/.env 里的 DATABASE_URL 灌回来（python-dotenv 不覆盖已存在的变量，
    但会补上被删掉的）。详见 tests/conftest.py 的说明。
    """
    monkeypatch.setenv("DATABASE_URL", "")
    yield


@pytest.fixture()
def client():
    from main import app

    with TestClient(app) as test_client:
        yield test_client


def _draws(specials: list[int], start: date = date(2026, 6, 1)) -> list[dict]:
    """构造升序的手工期序列。

    起始日固定在 2026 农历马年内（马年春节 2026-02-17），
    因此 01 = HORSE、02 = SNAKE、03 = DRAGON、07 = RAT 等映射稳定。
    """
    return [
        {
            "period": index + 1,
            "draw_date": start + timedelta(days=index),
            "special_number": number,
        }
        for index, number in enumerate(specials)
    ]


def _by_number(result: dict) -> dict[int, dict]:
    return {row["number"]: row for row in result["numbers"]}


def _by_zodiac(result: dict) -> dict[str, dict]:
    return {row["zodiac"]: row for row in result["zodiacs"]}


# --------------------------------------------------------------------------- #
# 2. 频次
# --------------------------------------------------------------------------- #
def test_frequency_known_series():
    result = frequency_stats(_draws([1, 2, 1, 3]))

    assert result["sample_size"] == 4
    assert result["expected_number_rate"] == pytest.approx(1 / 49)

    numbers = _by_number(result)
    assert numbers[1]["appearances"] == 2
    assert numbers[1]["rate"] == pytest.approx(0.5)
    assert numbers[1]["deviation"] == pytest.approx(0.5 - 1 / 49)
    assert numbers[49]["appearances"] == 0
    assert numbers[49]["rate"] == 0.0
    assert numbers[49]["deviation"] == pytest.approx(-1 / 49)

    zodiacs = _by_zodiac(result)
    assert zodiacs["HORSE"]["appearances"] == 2
    # 2026 马年 HORSE 含 5 个号码（01/13/25/37/49），期望率 = 5/49 而非 1/12
    assert zodiacs["HORSE"]["expected_rate"] == pytest.approx(5 / 49)
    assert zodiacs["RAT"]["expected_rate"] == pytest.approx(4 / 49)
    assert zodiacs["HORSE"]["deviation"] == pytest.approx(0.5 - 5 / 49)
    # 12 生肖期望率之和应为 1
    assert sum(row["expected_rate"] for row in result["zodiacs"]) == pytest.approx(1.0)


def test_frequency_states_sample_cannot_support_strong_conclusion():
    result = frequency_stats(_draws([1, 2, 1, 3]))

    assert "不足以支持强结论" in result["note"]
    assert result["sample_power"]["can_support_strong_conclusion"] is False
    assert result["sample_power"]["expected_appearances_per_number"] == pytest.approx(
        4 / 49
    )
    assert result["sample_power"]["strong_conclusion_min_draws"] == 490


# --------------------------------------------------------------------------- #
# 3. 走势与波动边界
# --------------------------------------------------------------------------- #
def test_trend_wave_boundaries():
    # 差值依次为 10（=small_max）、30（=normal_max）、31（=normal_max + 1）
    result = trend_stats(_draws([1, 11, 41, 10]), small_max=10, normal_max=30)

    assert result["sample_size"] == 4
    pairs = [(row["diff"], row["wave_type"]) for row in result["series"]]
    assert pairs == [(None, None), (10, "small"), (30, "normal"), (31, "big")]
    assert [row["wave_label"] for row in result["series"]] == [
        None,
        "小波动",
        "常规波动",
        "大跳",
    ]

    distribution = {
        item["type"]: item["count"] for item in result["wave_distribution"]["items"]
    }
    assert distribution == {"small": 1, "normal": 1, "big": 1}
    assert result["wave_distribution"]["total_pairs"] == 3
    assert result["settings"] == {"small_max": 10, "normal_max": 30, "big_min": 31}


def test_trend_small_max_plus_one_is_normal():
    result = trend_stats(_draws([1, 12]), small_max=10, normal_max=30)

    row = result["series"][1]
    assert row["diff"] == 11  # small_max + 1
    assert row["wave_type"] == "normal"


def test_trend_series_fields_and_scope_note():
    result = trend_stats(_draws([1, 11, 41, 10]), small_max=10, normal_max=30)

    first = result["series"][0]
    assert set(first) == {
        "period",
        "draw_date",
        "special_number",
        "zodiac",
        "zodiac_label",
        "diff",
        "wave_type",
        "wave_label",
    }
    assert result["series"][0]["zodiac"] == "HORSE"
    assert result["series"][0]["zodiac_label"] == "马"
    assert "本池已导入的 4 期" in result["notes"][0]


# --------------------------------------------------------------------------- #
# 4. 生肖走势 / 连出
# --------------------------------------------------------------------------- #
def test_zodiac_trend_streaks():
    # 2026 马年：01/13/25 同为 HORSE，02 为 SNAKE → HORSE 连出 3 期
    result = zodiac_trend_stats(_draws([1, 13, 25, 2]))

    assert [row["zodiac"] for row in result["series"]] == [
        "HORSE",
        "HORSE",
        "HORSE",
        "SNAKE",
    ]

    zodiacs = _by_zodiac(result)
    assert zodiacs["HORSE"]["appearances"] == 3
    assert zodiacs["HORSE"]["max_streak"] == 3
    assert zodiacs["HORSE"]["max_streak_end_period"] == 3
    assert zodiacs["HORSE"]["current_streak"] == 0  # 最新一期不是 HORSE
    assert zodiacs["SNAKE"]["max_streak"] == 1
    assert zodiacs["SNAKE"]["current_streak"] == 1

    assert result["max_streak_leaders"][0]["zodiac"] == "HORSE"


def test_zodiac_trend_recent_window_and_rotation():
    result = zodiac_trend_stats(_draws([1, 13, 25, 2]), recent_window=12)

    window = result["recent_window"]
    assert window["requested"] == 12
    assert window["used"] == 4  # 样本不足 12 期，按实际期数
    assert window["distinct_count"] == 2
    assert window["present"] == ["SNAKE", "HORSE"]  # 按 ZODIAC_ORDER 排列
    assert window["available"] == 12
    assert window["missing"] == [
        code
        for code in (
            "RAT",
            "OX",
            "TIGER",
            "RABBIT",
            "DRAGON",
            "GOAT",
            "MONKEY",
            "ROOSTER",
            "DOG",
            "PIG",
        )
    ]
    assert result["rotation"]["distinct_in_recent"] == 2
    assert result["rotation"]["available"] == 12
    # 窗口不足 12 期时必须显式声明数据不足
    assert any("数据不足" in note for note in result["notes"])


def test_zodiac_trend_respects_smaller_window():
    result = zodiac_trend_stats(_draws([1, 2, 1, 2]), recent_window=2)

    window = result["recent_window"]
    assert window["requested"] == 2
    assert window["used"] == 2
    assert window["sequence"] == ["HORSE", "SNAKE"]
    assert window["distinct_count"] == 2


def test_zodiac_trend_full_window_has_no_insufficient_note():
    # 窗口 12 期且样本恰好 12 期 → 不再触发「窗口不足」提示
    result = zodiac_trend_stats(_draws([1, 2] * 6), recent_window=12)

    assert result["recent_window"]["used"] == 12
    assert result["recent_window"]["distinct_count"] == 2
    assert not any("数据不足" in note for note in result["notes"])


# --------------------------------------------------------------------------- #
# 6. 回测（走步）
# --------------------------------------------------------------------------- #
def test_backtest_known_answer():
    # 第 3 期：latest=20/prev=10 → 小波动池内「遗漏最久且差值最小」为 19，实际也是 19 → 命中
    # 第 4 期：latest=19/prev=20 → 预测 18，实际 05 → 未命中
    result = backtest_stats(_draws([10, 20, 19, 5]), mode="even", pick_count=1)

    assert result["evaluated"] == 2
    assert result["hits"] == 1
    assert result["hit_rate"] == pytest.approx(0.5)
    assert result["data_status"] == "OK"

    window = result["evaluation_window"]
    assert window["draws_used"] == 4
    assert window["min_prior_draws"] == 2
    assert window["skipped"] == 2
    assert window["evaluated"] == 2
    assert window["first_evaluated_period"] == 3
    assert window["last_evaluated_period"] == 4
    assert "walk-forward" not in window["description"]  # 文案为中文说明
    assert "只用该期之前的数据" in window["description"]

    # 随机参考值口径 = 有效注数 / 49（而非每期可用号码数）
    assert result["random_baseline_hit_rate"] == pytest.approx(1 / 49)
    assert result["hit_rate_minus_baseline"] == pytest.approx(0.5 - 1 / 49)
    assert result["hit_rate_standard_error"] == pytest.approx(
        math.sqrt((1 / 49) * (1 - 1 / 49) / 2)
    )
    # 两期的实际特码都落在候选池内 → 理论上限为 100%
    # 默认不避开重肖：每期可用号码 = 49 - 1（仅排除上期特码本身）= 48
    assert result["actual_in_pool_rate"] == pytest.approx(1.0)
    assert result["average_available_numbers"] == pytest.approx(48.0)
    assert all(row["actual_in_candidate_pool"] for row in result["results"])
    assert result["settings"]["exclude_repeat_zodiac"] is False

    first, second = result["results"]
    assert first["period"] == 3
    assert first["latest_used"] == 20
    assert first["previous_used"] == 10
    assert first["predicted"] == [19]
    assert first["actual"] == 19
    assert first["hit"] is True

    assert second["period"] == 4
    assert second["latest_used"] == 19
    assert second["previous_used"] == 20
    assert second["predicted"] == [18]
    assert second["actual"] == 5
    assert second["hit"] is False


def test_backtest_exclude_repeat_zodiac_shrinks_pool():
    """开启避开重肖时，平均可用号码应从 48 降到约 45（去掉整组同肖）。"""
    draws = _draws([10, 20, 19, 5])
    off = backtest_stats(
        draws, mode="even", pick_count=1, base_settings={"exclude_repeat_zodiac": False}
    )
    on = backtest_stats(
        draws, mode="even", pick_count=1, base_settings={"exclude_repeat_zodiac": True}
    )
    assert off["average_available_numbers"] == pytest.approx(48.0)
    assert on["average_available_numbers"] == pytest.approx(45.0)
    assert off["settings"]["exclude_repeat_zodiac"] is False
    assert on["settings"]["exclude_repeat_zodiac"] is True


def test_backtest_wave_breakdown():
    result = backtest_stats(_draws([10, 20, 19, 5]), mode="even", pick_count=1)

    realized = {item["type"]: item for item in result["wave_breakdown"]["items"]}
    assert realized["small"]["evaluated"] == 1  # |19-20| = 1
    assert realized["small"]["hits"] == 1
    assert realized["small"]["hit_rate"] == pytest.approx(1.0)
    assert realized["normal"]["evaluated"] == 1  # |5-19| = 14
    assert realized["normal"]["hits"] == 0
    assert realized["normal"]["hit_rate"] == pytest.approx(0.0)
    assert realized["big"]["evaluated"] == 0
    assert realized["big"]["hit_rate"] is None

    by_prev = {item["type"]: item for item in result["wave_breakdown_by_prev"]["items"]}
    # 两期的上一期波动都是小波动（|20-10| = 10、|19-20| = 1）
    assert by_prev["small"]["evaluated"] == 2
    assert by_prev["small"]["hits"] == 1
    assert by_prev["small"]["hit_rate"] == pytest.approx(0.5)


def test_backtest_has_no_lookahead():
    base = backtest_stats(_draws([10, 20, 19, 5]), mode="even", pick_count=1)
    # 只改动最后一期（未来）；更早一期的结果必须逐字段完全一致
    modified = backtest_stats(_draws([10, 20, 19, 45]), mode="even", pick_count=1)

    assert base["results"][0] == modified["results"][0]
    # 最后一期的预测只依赖它之前的数据，也必须一致；只有 actual / hit 变了
    assert (
        base["results"][1]["predicted"] == modified["results"][1]["predicted"] == [18]
    )
    assert base["results"][1]["actual"] == 5
    assert modified["results"][1]["actual"] == 45
    assert modified["results"][1]["hit"] is False


def test_backtest_respects_overrides():
    draws = _draws([10, 20, 19, 5])

    single = backtest_stats(draws, mode="single", pick_count=3)
    assert single["settings"]["mode"] == "single"
    assert single["settings"]["effective_pick_count"] == 1
    assert all(len(row["predicted"]) == 1 for row in single["results"])

    wide = backtest_stats(draws, mode="even", pick_count=3)
    assert wide["settings"]["effective_pick_count"] == 3
    assert all(len(row["predicted"]) == 3 for row in wide["results"])

    # 阈值覆盖必须生效（small_max=0 → 差值 1 也算常规波动）
    strict = backtest_stats(draws, mode="even", pick_count=1, small_max=0, normal_max=30)
    assert strict["settings"]["small_max"] == 0
    assert strict["settings"]["big_min"] == 31
    assert strict["results"][0]["realized_wave_type"] == "normal"  # |19-20| = 1


def test_backtest_too_few_draws_is_insufficient():
    result = backtest_stats(_draws([10, 20]))

    assert result["evaluated"] == 0
    assert result["hit_rate"] is None
    assert result["results"] == []
    assert result["data_status"] == "INSUFFICIENT"
    assert result["data_status_label"] == "数据不足"
    assert any("数据不足" in note for note in result["notes"])


# --------------------------------------------------------------------------- #
# 7. 零开奖数据：所有接口都必须安全返回
# --------------------------------------------------------------------------- #
def test_pure_functions_with_zero_draws_do_not_crash():
    assert frequency_stats([])["numbers"] == []
    assert frequency_stats([])["sample_power"]["can_support_strong_conclusion"] is False
    assert "数据不足" in frequency_stats([])["note"]

    assert trend_stats([])["series"] == []
    assert trend_stats([])["wave_distribution"]["total_pairs"] == 0
    assert trend_stats([])["wave_distribution"]["dominant"] is None

    assert zodiac_trend_stats([])["series"] == []
    assert zodiac_trend_stats([])["recent_window"]["used"] == 0

    assert backtest_stats([])["evaluated"] == 0


def test_stats_endpoints_with_zero_draws_return_200(client):
    for path in STATS_GET_PATHS:
        response = client.get(path)
        assert response.status_code == 200, path
        body = response.json()
        assert body["sample_size"] == 0
        assert body["scope"] == "本池已导入 0 期数据内"
        assert body["data_status"] == "INSUFFICIENT"
        assert body["data_status_label"] == "数据不足"

    backtest = client.post("/api/stats/backtest", json={})
    assert backtest.status_code == 200
    body = backtest.json()
    assert body["evaluated"] == 0
    assert body["results"] == []
    assert body["data_status"] == "INSUFFICIENT"
    assert body["data_status_label"] == "数据不足"

    # 空 body 也必须可用（参数全部可选）
    assert client.post("/api/stats/backtest").status_code == 200


# --------------------------------------------------------------------------- #
# 8. 真实 200 期数据（HTTP 端到端） + 口径防越界
# --------------------------------------------------------------------------- #
def _import_real_draws(client: TestClient) -> None:
    text = REAL_DRAWS_FILE.read_text(encoding="utf-8")
    response = client.post("/api/draws/import", json={"text": text})
    assert response.status_code == 200
    assert response.json()["imported"] == 200


def test_stats_endpoints_against_real_200_draws(client):
    _import_real_draws(client)

    frequency = client.get("/api/stats/frequency").json()
    assert frequency["sample_size"] == 200
    assert sum(row["appearances"] for row in frequency["numbers"]) == 200
    assert sum(row["appearances"] for row in frequency["zodiacs"]) == 200
    assert frequency["sample_power"]["can_support_strong_conclusion"] is False

    trend = client.get("/api/stats/trend").json()
    assert trend["sample_size"] == 200
    assert len(trend["series"]) == 200
    assert trend["wave_distribution"]["total_pairs"] == 199
    assert sum(
        item["count"] for item in trend["wave_distribution"]["items"]
    ) == 199
    latest = trend["series"][-1]
    assert latest["period"] == 269
    assert latest["special_number"] == 22
    assert latest["zodiac"] == "ROOSTER"
    assert latest["zodiac_label"] == "鸡"

    limited_trend = client.get("/api/stats/trend", params={"limit": 10}).json()
    assert limited_trend["sample_size"] == 10
    assert limited_trend["wave_distribution"]["total_pairs"] == 9

    zodiac_trend = client.get("/api/stats/zodiac-trend").json()
    assert zodiac_trend["sample_size"] == 200
    assert len(zodiac_trend["series"]) == 200
    assert zodiac_trend["recent_window"]["used"] == 12
    assert zodiac_trend["recent_window"]["distinct_count"] <= 12


def test_backtest_against_real_200_draws(client):
    _import_real_draws(client)

    body = client.post("/api/stats/backtest", json={}).json()

    assert body["sample_size"] == 200
    assert body["evaluated"] == 198  # 200 - min_prior_draws(2)
    assert body["evaluation_window"]["skipped"] == 2
    assert body["evaluation_window"]["first_evaluated_period"] == 72
    assert body["evaluation_window"]["last_evaluated_period"] == 269
    assert body["hits"] <= body["evaluated"]
    assert body["hit_rate"] == pytest.approx(body["hits"] / 198)
    assert len(body["results"]) == 198

    override = client.post(
        "/api/stats/backtest",
        json={"mode": "single", "pick_count": 3, "limit": 50},
    ).json()
    assert override["sample_size"] == 50
    assert override["evaluated"] == 48
    assert override["settings"]["effective_pick_count"] == 1


def test_payloads_never_use_market_wide_wording(client):
    _import_real_draws(client)

    texts = [client.get(path).text for path in STATS_GET_PATHS]
    texts.append(client.post("/api/stats/backtest", json={}).text)

    for text in texts:
        for banned in ("全市场", "全量市场", "市场最"):
            assert banned not in text
        assert "本池已导入" in text


def test_backtest_rejects_invalid_mode(client):
    response = client.post("/api/stats/backtest", json={"mode": "allin"})
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# 9. 回测默认取「当前用户已存配置」（而非钉死 DEFAULT_SETTINGS）
# --------------------------------------------------------------------------- #
def test_backtest_base_settings_not_wiped_by_missing_overrides():
    """未显式覆盖的项必须保留 base_settings —— 不能回退成 DEFAULT_SETTINGS。

    这是 ``/api/stats/trend`` 修过的同类 bug：把 ``None`` 展开进合并字典会
    抹掉已存阈值（``clamp_settings`` 跳过 ``None``，于是回退到默认值）。
    """
    draws = _draws([10, 20, 19, 5])
    result = backtest_stats(
        draws,
        base_settings={
            "small_max": 5,
            "normal_max": 25,
            "mode": "single",
            "pick_count": 3,
            "bet_unit": 10,
        },
    )

    assert result["settings"]["small_max"] == 5
    assert result["settings"]["normal_max"] == 25  # 不是 DEFAULT 的 30
    assert result["settings"]["big_min"] == 26
    assert result["settings"]["mode"] == "single"
    assert result["settings"]["effective_pick_count"] == 1
    assert result["parameter_sources"] == {
        "small_max": "saved_settings",
        "normal_max": "saved_settings",
        "mode": "saved_settings",
        "pick_count": "saved_settings",
    }


def test_backtest_without_base_settings_falls_back_to_defaults():
    result = backtest_stats(_draws([10, 20, 19, 5]))
    assert result["settings"]["normal_max"] == 30
    assert result["settings"]["mode"] == "even"
    assert all(
        source == "default" for source in result["parameter_sources"].values()
    )


def test_backtest_endpoint_uses_saved_settings_by_default(client):
    _import_real_draws(client)
    client.put(
        "/api/settings",
        json={"small_max": 10, "normal_max": 25, "mode": "single", "pick_count": 3},
    )

    body = client.post("/api/stats/backtest", json={}).json()

    assert body["settings"]["normal_max"] == 25
    assert body["settings"]["small_max"] == 10
    assert body["settings"]["mode"] == "single"
    assert body["settings"]["effective_pick_count"] == 1  # single 恒为 1 注
    assert body["settings"]["big_min"] == 26
    assert all(
        source == "saved_settings" for source in body["parameter_sources"].values()
    )
    assert all(len(row["predicted"]) == 1 for row in body["results"])


def test_backtest_endpoint_request_override_beats_saved_settings(client):
    _import_real_draws(client)
    client.put(
        "/api/settings",
        json={"small_max": 10, "normal_max": 25, "mode": "single", "pick_count": 3},
    )

    # 只覆盖 mode：未覆盖的 normal_max / pick_count 必须沿用已存值
    body = client.post("/api/stats/backtest", json={"mode": "even"}).json()

    assert body["settings"]["mode"] == "even"
    assert body["settings"]["normal_max"] == 25  # 未覆盖 → 用已存 25，而非默认 30
    assert body["settings"]["effective_pick_count"] == 3  # 用已存的 pick_count=3
    assert body["parameter_sources"]["mode"] == "request_override"
    assert body["parameter_sources"]["normal_max"] == "saved_settings"
    assert body["parameter_sources"]["pick_count"] == "saved_settings"
    assert all(len(row["predicted"]) == 3 for row in body["results"])


def test_real_database_connection_is_hard_blocked(monkeypatch):
    """即使 DATABASE_URL 被重新灌成真实 DSN，也不允许真的建池（conftest 硬闸）。"""
    import asyncio

    import db

    asyncio.run(db.close_pool())  # 确保 _pool 为空
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/not_a_real_db"
    )
    with pytest.raises(RuntimeError):
        asyncio.run(db.init_pool())

