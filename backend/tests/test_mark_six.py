"""开奖总表解析 / 导入回归测试（Mark Six 清理后的数据模型）。

覆盖：
- 用户真实数据的两种头部顺序：「日期在前」与「期号在前」；
- 旧装饰 `(波色)生肖/五行` 一律静默忽略，只保留号码 + 生肖；
- 落库载荷只有 draw_date / period / special_number / zodiac / zodiac_label；
- 生肖推导（农历年口径）与申报值不一致 → 只警告不失败；
- 期号 vs 年积日 软提示 → 只警告不失败；
- 多行导入时单行解析失败不中断整批；MemoryStore 按 draw_date 覆盖；
- 49 号码关联表（zodiac_numbers）与 mark_six 推导永远一致；
- 已删除的 /api/elements、/api/hehe* 路由不再存在。

所有用例均不依赖真实数据库（内存存储）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.mark_six import (
    ZODIAC_YEARS,
    parse_draw_lines,
    resolve_draws,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
REAL_DRAWS_FILE = BACKEND_DIR / "data" / "draws_70_269.txt"

# 用户提供的原始样例行（整期：6 正码 + 1 特码，带旧装饰）
SAMPLE_DATE_FIRST = (
    "2026-03-11\t70期\t"
    "47(蓝)猴/木、17(绿)虎/木、23(红)猴/水、10(蓝)鸡/火、41(蓝)虎/火、07(红)鼠/土"
    "\t+25(蓝)马/木"
)
# 用户真实数据的头部顺序：期号在前 + 全角冒号
SAMPLE_PERIOD_FIRST = (
    "269期 2026-09-26："
    "19(红)鼠/火、40(红)兔/火、36(蓝)羊/土、05(绿)虎/金、38(绿)蛇/木、16(绿)兔/木 "
    "+22(绿)鸡/水"
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


# --------------------------------------------------------------------------- #
# 1. 旧装饰（波色 / 五行）被静默忽略，只留号码 + 生肖
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("cell", "number", "zodiac"),
    [
        ("19(红)鼠/火", 19, "RAT"),
        ("19(红)鼠", 19, "RAT"),
        ("19鼠", 19, "RAT"),
        ("19", 19, None),
        ("19(红)", 19, None),
        ("+22(绿)鸡/水", 22, "ROOSTER"),
    ],
)
def test_decorations_are_ignored(cell: str, number: int, zodiac: str | None):
    parsed, errors = parse_draw_lines(f"269期 2026-09-26：{cell}")

    assert errors == []
    assert len(parsed) == 1
    draw = parsed[0]
    assert draw.special_number == number
    assert draw.numbers[0].number == number
    assert draw.numbers[0].zodiac == zodiac
    # 解析结果里不存在波色 / 五行字段
    assert not hasattr(draw.numbers[0], "color")
    assert not hasattr(draw.numbers[0], "element")


# --------------------------------------------------------------------------- #
# 2. 头部两种顺序
# --------------------------------------------------------------------------- #
def test_header_date_first():
    parsed, errors = parse_draw_lines(SAMPLE_DATE_FIRST)

    assert errors == []
    assert len(parsed) == 1
    assert parsed[0].draw_date.isoformat() == "2026-03-11"
    assert parsed[0].period == 70
    assert parsed[0].special_number == 25
    assert len(parsed[0].numbers) == 7  # 解析保留全部，落库只用特码


def test_header_period_first():
    parsed, errors = parse_draw_lines(SAMPLE_PERIOD_FIRST)

    assert errors == []
    assert len(parsed) == 1
    assert parsed[0].draw_date.isoformat() == "2026-09-26"
    assert parsed[0].period == 269
    assert parsed[0].special_number == 22


@pytest.mark.parametrize("separator", ["：", ":"])
def test_header_accepts_both_colons(separator: str):
    parsed, errors = parse_draw_lines(f"269期 2026-09-26{separator}+22(绿)鸡/水")
    assert errors == []
    assert parsed[0].special_number == 22


@pytest.mark.parametrize("separator", ["、", ",", "，", ";", "；", "\t", " "])
def test_number_separators_all_work(separator: str):
    text = separator.join(["19鼠", "40兔", "+22鸡"])
    parsed, errors = parse_draw_lines(f"269期 2026-09-26：{text}")
    assert errors == []
    assert parsed[0].special_number == 22


def test_period_suffix_optional_in_date_first_order():
    parsed, errors = parse_draw_lines("2026-03-11 70 +25马")
    assert errors == []
    assert parsed[0].period == 70
    assert parsed[0].special_number == 25


# --------------------------------------------------------------------------- #
# 3. 落库载荷形状：只有日期 / 期号 / 特码 / 生肖
# --------------------------------------------------------------------------- #
def test_resolve_payload_has_only_special_number_fields():
    parsed, _ = parse_draw_lines(SAMPLE_PERIOD_FIRST)
    payloads, warnings = resolve_draws(parsed)

    assert warnings == []
    assert payloads == [
        {
            "draw_date": parsed[0].draw_date,
            "period": 269,
            "special_number": 22,
            "zodiac": "ROOSTER",
            "zodiac_label": "鸡",
        }
    ]
    # 没有 7 号码数组，也没有 color / element
    assert "numbers" not in payloads[0]
    assert "color" not in payloads[0]
    assert "element" not in payloads[0]


# --------------------------------------------------------------------------- #
# 4. 警告：生肖不一致 / 期号与年积日不一致 → 只警告不失败
# --------------------------------------------------------------------------- #
def test_zodiac_mismatch_only_warns():
    # 2026 马年，19 号应为鼠；这里故意申报为「马」
    parsed, errors = parse_draw_lines("269期 2026-09-26：+19马")
    assert errors == []

    payloads, warnings = resolve_draws(parsed)

    assert len(payloads) == 1
    assert any("生肖" in warning for warning in warnings)
    # 落库仍按推导值（权威表）
    assert payloads[0]["zodiac"] == "RAT"
    assert payloads[0]["zodiac_label"] == "鼠"


def test_period_day_of_year_mismatch_only_warns():
    # 2026-03-11 的年积日是 70，这里写 71
    parsed, _ = parse_draw_lines("71期 2026-03-11：+25马")
    payloads, warnings = resolve_draws(parsed)

    assert len(payloads) == 1
    assert any("年积日" in warning for warning in warnings)
    assert payloads[0]["special_number"] == 25


def test_consistent_draw_produces_no_warnings():
    parsed, _ = parse_draw_lines("70期 2026-03-11：+25马")
    _payloads, warnings = resolve_draws(parsed)
    assert warnings == []


# --------------------------------------------------------------------------- #
# 5. 真实 200 期数据：解析干净、生肖全部与推导一致
# --------------------------------------------------------------------------- #
def test_real_200_draws_parse_cleanly():
    text = REAL_DRAWS_FILE.read_text(encoding="utf-8")
    parsed, errors = parse_draw_lines(text)

    assert errors == []
    assert len(parsed) == 200

    payloads, warnings = resolve_draws(parsed)
    assert warnings == []  # 生肖与年积日两处软检查都无警告

    assert sorted(payload["period"] for payload in payloads) == list(range(70, 270))

    by_period = {payload["period"]: payload for payload in payloads}
    assert by_period[70]["special_number"] == 25
    assert by_period[150]["special_number"] == 9
    assert by_period[269]["special_number"] == 22
    # 2026 丙午马年：01 = 马
    assert by_period[269]["zodiac"] == "ROOSTER"
    assert by_period[269]["zodiac_label"] == "鸡"


# --------------------------------------------------------------------------- #
# 6. 批量容错：坏行进 errors，好行照常导入
# --------------------------------------------------------------------------- #
def test_import_reports_bad_lines_without_failing_whole_batch(client):
    text = "\n".join(["这不是一行合法的开奖记录", SAMPLE_PERIOD_FIRST])
    response = client.post("/api/draws/import", json={"text": text})

    assert response.status_code == 200
    body = response.json()

    assert body["imported"] == 1
    assert body["skipped"] == 0
    assert body["warnings"] == []
    assert len(body["errors"]) == 1
    error = body["errors"][0]
    assert error["line"] == 1
    assert error["text"] == "这不是一行合法的开奖记录"
    assert error["message"]


def test_import_replace_existing_false_skips_duplicate_date(client):
    assert client.post("/api/draws/import", json={"text": SAMPLE_PERIOD_FIRST}).status_code == 200

    response = client.post(
        "/api/draws/import",
        json={"text": SAMPLE_PERIOD_FIRST, "replace_existing": False},
    )
    assert response.status_code == 200
    assert response.json()["skipped"] == 1
    assert len(client.get("/api/draws").json()) == 1

    # 默认 replace_existing=True 时按 draw_date 覆盖
    replacement = SAMPLE_PERIOD_FIRST.replace("+22(绿)鸡/水", "+09(蓝)狗/木")
    response = client.post("/api/draws/import", json={"text": replacement})
    assert response.status_code == 200
    assert response.json()["imported"] == 1

    draws = client.get("/api/draws").json()
    assert len(draws) == 1
    assert draws[0]["special_number"] == 9
    assert draws[0]["zodiac"] == "DOG"
    assert draws[0]["zodiac_label"] == "狗"


def test_draws_api_shape(client):
    client.post("/api/draws/import", json={"text": SAMPLE_PERIOD_FIRST})

    row = client.get("/api/draws").json()[0]
    assert set(row) == {"id", "draw_date", "period", "special_number", "zodiac", "zodiac_label"}

    latest = client.get("/api/draws/latest").json()
    assert latest["period"] == 269
    assert latest["special_number"] == 22

    assert client.delete(f"/api/draws/{row['id']}").json() == {"deleted": 1}
    assert client.get("/api/draws/latest").json() is None
    assert client.get("/api/draws").json() == []


# --------------------------------------------------------------------------- #
# 7. MemoryStore 覆盖语义（只存特码）
# --------------------------------------------------------------------------- #
def test_memory_store_import_draws_upserts_by_date():
    from repository import MemoryStore

    store = MemoryStore()

    async def scenario():
        inserted = await store.import_draws(
            [{"draw_date": "2026-03-11", "period": 70, "special_number": 25}]
        )
        updated = await store.import_draws(
            [{"draw_date": "2026-03-11", "period": 70, "special_number": 7}]
        )
        skipped = await store.import_draws(
            [{"draw_date": "2026-03-11", "period": 70, "special_number": 7}],
            replace_existing=False,
        )
        draws = await store.list_draws()
        latest = await store.get_latest_draw()
        fetched = await store.get_draw(draws[0]["id"]) if draws else None
        deleted = await store.delete_draw(draws[0]["id"]) if draws else 0
        cleared = await store.clear_draws()
        return inserted, updated, skipped, draws, latest, fetched, deleted, cleared

    inserted, updated, skipped, draws, latest, fetched, deleted, cleared = asyncio.run(
        scenario()
    )

    assert inserted == {"imported": 1, "updated": 0, "skipped": 0}
    assert updated == {"imported": 0, "updated": 1, "skipped": 0}
    assert skipped == {"imported": 0, "updated": 0, "skipped": 1}

    assert len(draws) == 1
    assert draws[0]["special_number"] == 7
    assert draws[0]["period"] == 70
    assert latest is not None and latest["special_number"] == 7
    assert fetched is not None and fetched["special_number"] == 7
    assert deleted == 1
    assert cleared == 0


# --------------------------------------------------------------------------- #
# 8. 49 号码关联表：物化结果必须与推导一致
# --------------------------------------------------------------------------- #
def test_materialized_zodiac_numbers_match_derivation():
    from repository import MemoryStore
    from services.mark_six import zodiac_table_for_year

    store = MemoryStore()
    assert len(ZODIAC_YEARS) == 2

    total = 0
    for lunar_year, _starts_on, _animal in ZODIAC_YEARS:
        rows = asyncio.run(store.list_zodiac_numbers(lunar_year))
        assert len(rows) == 49
        total += len(rows)

        derived = zodiac_table_for_year(lunar_year)
        assert derived is not None
        expected = {
            number: code for code, numbers in derived.items() for number in numbers
        }
        assert {row["number"]: row["zodiac"] for row in rows} == expected

    assert total == 98
    # 未知年份没有数据
    assert asyncio.run(store.list_zodiac_numbers(1999)) == []


def test_zodiac_table_is_lunar_year_dependent():
    from services.mark_six import animal_of_01, zodiac_of
    from datetime import date

    assert animal_of_01(2026) == "HORSE"
    assert animal_of_01(2025) == "SNAKE"
    # 01 号：2026 马年属马，2025 蛇年属蛇
    assert zodiac_of(1, date(2026, 9, 26)) == "HORSE"
    assert zodiac_of(1, date(2025, 9, 26)) == "SNAKE"


# --------------------------------------------------------------------------- #
# 9. 生肖表 API
# --------------------------------------------------------------------------- #
def test_api_zodiac_table_covers_all_49(client):
    body = client.get("/api/zodiac/table").json()

    # 默认取最新一期开奖日，无开奖数据时取今天（两者都落在已知农历年内）
    assert body["lunar_year"] in (2025, 2026)
    assert body["animal_of_01"] in ("SNAKE", "HORSE")
    assert len(body["zodiacs"]) == 12
    assert sum(len(item["numbers"]) for item in body["zodiacs"]) == 49
    assert [item["number"] for item in body["numbers"]] == list(range(1, 50))
    assert all(item["zodiac_label"] for item in body["numbers"])


def test_api_zodiac_table_accepts_date(client):
    body = client.get("/api/zodiac/table", params={"date": "2026-09-26"}).json()
    assert body["lunar_year"] == 2026
    assert body["animal_of_01"] == "HORSE"
    assert body["animal_of_01_label"] == "马"
    assert body["numbers"][0] == {"number": 1, "zodiac": "HORSE", "zodiac_label": "马"}

    snake = client.get("/api/zodiac/table", params={"date": "2025-06-01"}).json()
    assert snake["lunar_year"] == 2025
    assert snake["animal_of_01"] == "SNAKE"
    assert snake["numbers"][0]["zodiac"] == "SNAKE"


def test_api_zodiac_table_rejects_unknown_year(client):
    assert client.get("/api/zodiac/table", params={"date": "1990-01-01"}).status_code == 404


def test_api_zodiac_years(client):
    body = client.get("/api/zodiac/years").json()
    assert [item["lunar_year"] for item in body] == [2025, 2026]
    assert body[0]["animal_of_01"] == "SNAKE"
    assert body[1]["animal_of_01_label"] == "马"


def test_api_zodiac_year_numbers(client):
    rows = client.get("/api/zodiac/years/2026/numbers").json()
    assert len(rows) == 49
    assert {row["number"] for row in rows} == set(range(1, 50))
    assert all(row["lunar_year"] == 2026 for row in rows)
    assert all(row["zodiac_label"] for row in rows)
    assert client.get("/api/zodiac/years/1999/numbers").status_code == 404


# --------------------------------------------------------------------------- #
# 10. 已删除的路由不再存在
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path",
    [
        "/api/elements",
        "/api/hehe",
        "/api/hehe/meta",
        "/api/hehe/number/25",
        "/api/hehe/rows/9",
    ],
)
def test_removed_routes_are_gone(client, path: str):
    assert client.get(path).status_code == 404
    assert client.put(path, json={}).status_code in (404, 405)
