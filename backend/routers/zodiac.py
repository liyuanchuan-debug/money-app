"""生肖表 API（农历年口径的 49 号码关联表，只读）。

口径说明（**禁止**升格为全市场结论）：
- 本表是本项目自己的 49 号码 → 生肖 映射，只随农历年（春节）轮转；
- 同一号码在不同农历年属于不同生肖：01 在 2026 丙午马年是「马」，在 2025 乙巳蛇年是「蛇」；
- 接口只输出本项目这份映射，不做任何全市场统计或结论。

权限：**匿名可读**（只读映射表，无写操作）。
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Path, Query

from repository import get_store
from services.mark_six import (
    NUMBER_MAX,
    NUMBER_MIN,
    ZODIAC_LABELS,
    ZODIAC_ORDER,
    animal_of_01,
    known_zodiac_years,
    lunar_year_for,
    year_starts_on,
    zodiac_label,
    zodiac_of,
    zodiac_table,
    zodiac_table_for_year,
)

router = APIRouter(prefix="/api/zodiac", tags=["zodiac"])


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


@router.get("/table")
async def get_zodiac_table(
    date_param: str | None = Query(default=None, alias="date", description="YYYY-MM-DD"),
) -> dict:
    """整张 49 号码关联表；默认取最新一期开奖日（无数据则今天）。"""
    store = await get_store()
    if date_param:
        try:
            target = date.fromisoformat(date_param.strip())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="date 需为 YYYY-MM-DD 格式") from exc
    else:
        latest = await store.get_latest_draw()
        target = _as_date(latest["draw_date"]) if latest else date.today()

    lunar_year = lunar_year_for(target)
    table = zodiac_table(target)
    if lunar_year is None or table is None:
        raise HTTPException(
            status_code=404,
            detail=f"{target} 不在已知农历年表内，无法生成生肖表",
        )

    animal = animal_of_01(lunar_year)
    return {
        "date": target,
        "lunar_year": lunar_year,
        "starts_on": year_starts_on(lunar_year),
        "animal_of_01": animal,
        "animal_of_01_label": zodiac_label(animal),
        "zodiacs": [
            {"code": code, "label": ZODIAC_LABELS.get(code), "numbers": table[code]}
            for code in ZODIAC_ORDER
        ],
        "numbers": [
            {
                "number": number,
                "zodiac": zodiac_of(number, target),
                "zodiac_label": zodiac_label(zodiac_of(number, target)),
            }
            for number in range(NUMBER_MIN, NUMBER_MAX + 1)
        ],
    }


@router.get("/years")
async def list_zodiac_years() -> list[dict]:
    """已知农历年边界列表（含 01 号所属生肖）。"""
    return known_zodiac_years()


@router.get("/years/{lunar_year}/numbers")
async def list_zodiac_year_numbers(
    lunar_year: int = Path(..., ge=1900, le=2200, description="农历年，如 2026"),
) -> list[dict]:
    """某个农历年的 49 行号码关联记录（来自 zodiac_numbers 表）。"""
    if zodiac_table_for_year(lunar_year) is None:
        raise HTTPException(status_code=404, detail=f"未知农历年: {lunar_year}")

    store = await get_store()
    rows = await store.list_zodiac_numbers(lunar_year)
    return [
        {**row, "zodiac_label": zodiac_label(row.get("zodiac"))} for row in rows
    ]
