"""开奖总表导入 API（整期批量导入）与特码记录查询。

与 ``services.mark_six`` 的分工：
- 解析 / 校验 / 补中文标签全在领域模块里完成，路由只负责串流程与返回文案；
- 解析失败只记入 ``errors``（带行号与原文），合法行照常入库；
- 数据模型只保留特码：波色 / 五行 / 河合码 的旧装饰在解析阶段被静默忽略。

权限（见 ``dependencies``，受 ``AUTH_ENFORCED`` 灰度开关控制）：

| 接口 | 要求 |
|------|------|
| ``GET /api/draws`` / ``latest`` | **匿名可读**（只读浏览） |
| ``GET /api/draws/next-period`` | 任意已审批用户（录入预填） |
| ``POST /api/draws/quick`` / ``correct`` / ``import``、``DELETE /api/draws*`` | **仅 ADMIN** |
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from dependencies import actor_user_id, require_actor, require_admin
from repository import DRAW_ACTION_CORRECT, get_store
from services.mark_six import (
    APP_TIMEZONE_NAME,
    decorate_draw,
    parse_draw_lines,
    resolve_draws,
    today_local,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["draws"])


# --------------------------------------------------------------------------- #
# 请求 / 响应模型
# --------------------------------------------------------------------------- #
class DrawImportIn(BaseModel):
    text: str = Field(..., description="粘贴的多行开奖文本，每行 = 日期 + 期号 + … + 特码")
    replace_existing: bool = Field(
        default=True, description="同一日期已存在时是否覆盖（false 则跳过）"
    )


class DrawQuickIn(BaseModel):
    """快捷录入一期（自动补期号 / 日期，命中同一条则覆盖）。"""

    special_number: int = Field(..., ge=1, le=49, description="特码 1-49（必填）")
    period: int | None = Field(
        default=None, ge=1, description="期号；留空自动取 max(period)+1"
    )
    draw_date: date | None = Field(
        default=None, description="开奖日期；留空取今天（UTC+8）"
    )


class DrawCorrectIn(BaseModel):
    """纠正已保存开奖的特码（可顺带改日期）。"""

    special_number: int = Field(..., ge=1, le=49, description="纠正后的特码 1-49")
    draw_date: date | None = Field(
        default=None, description="可选：同时改开奖日期；留空保留原日期"
    )


class DrawOut(BaseModel):
    id: int
    draw_date: date
    period: int
    special_number: int
    zodiac: str | None = None
    # 展示用中文标签（落库仍是英文码）
    zodiac_label: str | None = None


class DrawCorrectOut(BaseModel):
    """纠正开奖回执：含新旧特码与重算条数。"""

    draw: DrawOut
    action: str = DRAW_ACTION_CORRECT
    action_label: str
    period: int
    old_special_number: int
    new_special_number: int
    old_draw_date: date
    new_draw_date: date
    resettled_rounds: int
    correction_id: int | None = None


class NextPeriodOut(BaseModel):
    """快捷录入表单的预填值（与 add_draw 的缺省口径完全一致）。"""

    suggested_period: int
    suggested_draw_date: date
    timezone: str
    has_draws: bool
    latest_draw: DrawOut | None = None


class DrawImportErrorOut(BaseModel):
    line: int
    text: str
    message: str


class DrawImportResult(BaseModel):
    imported: int
    skipped: int
    warnings: list[str] = Field(default_factory=list)
    errors: list[DrawImportErrorOut] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 开奖总表
# --------------------------------------------------------------------------- #
@router.post("/draws/import", response_model=DrawImportResult)
async def import_draws(payload: DrawImportIn, _admin=Depends(require_admin)):
    """批量导入开奖总表：逐行解析，单行失败只记入 errors，不中断整批。"""
    store = await get_store()

    parsed_draws, parse_errors = parse_draw_lines(payload.text)
    draw_payloads, warnings = resolve_draws(parsed_draws)

    summary = await store.import_draws(
        draw_payloads, replace_existing=payload.replace_existing
    )
    return {
        "imported": summary["imported"] + summary["updated"],
        "skipped": summary["skipped"],
        "warnings": warnings,
        "errors": [
            {"line": item.line, "text": item.text, "message": item.message}
            for item in parse_errors
        ],
    }


@router.get("/draws", response_model=list[DrawOut])
async def list_draws(
    limit: int | None = Query(default=None, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """开奖列表只读浏览；匿名可访问（写操作另有 ADMIN 门禁）。"""
    store = await get_store()
    draws = await store.list_draws(limit, offset)
    return [decorate_draw(draw) for draw in draws]


@router.get("/draws/latest", response_model=DrawOut | None)
async def get_latest_draw():
    """最新一期只读；匿名可访问。"""
    store = await get_store()
    draw = await store.get_latest_draw()
    return decorate_draw(draw)


@router.get("/draws/next-period", response_model=NextPeriodOut)
async def get_next_period(_actor=Depends(require_actor)):
    """快捷录入表单的预填值：建议期号 + 今天（UTC+8）+ 最新一期。

    期号 / 日期的推导口径与 ``add_draw`` 的缺省值同源，前端直接用即可，
    不必自己在浏览器里算日期，避免时区口径不一致。
    """
    store = await get_store()
    latest = await store.get_latest_draw()
    return {
        "suggested_period": await store.suggest_next_period(),
        "suggested_draw_date": today_local(),
        "timezone": APP_TIMEZONE_NAME,
        "has_draws": latest is not None,
        "latest_draw": decorate_draw(latest),
    }


@router.post("/draws/quick", response_model=DrawOut)
async def quick_add_draw(payload: DrawQuickIn, _admin=Depends(require_admin)):
    """快捷录入一期特码，直接写进唯一事实来源 ``draws``。

    - 期号 / 日期留空则自动补（期号 = max(period)+1；日期 = 今天 UTC+8）；
    - 同一日期（或显式给出的同一期号）已存在 → 覆盖修正，不产生重复行；
    - 返回补好生肖的一期，前端可直接展示「最新特码 / 生肖」。

    返回 200（而非 201）：本接口是 upsert，可能只是更新既有的一期。
    显式纠正已有期请优先用 ``POST /api/draws/{id}/correct``（带新旧号回执与审计）。
    """
    store = await get_store()
    try:
        draw = await store.add_draw(
            special_number=payload.special_number,
            period=payload.period,
            draw_date=payload.draw_date,
        )
    except ValueError as exc:  # 特码越界 / 期号或日期冲突
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return decorate_draw(draw)


@router.post("/draws/{draw_id}/correct", response_model=DrawCorrectOut)
async def correct_draw(
    draw_id: int,
    payload: DrawCorrectIn,
    admin=Depends(require_admin),
):
    """纠正已保存开奖的特码（可改日期），并重算该期采用快照的命中 / 盈亏。

    - 必须指定已存在的 ``draw_id``；
    - 覆盖原特码，写入 ``draw_corrections`` 审计；
    - 对该期所有用户的 ``recommend_rounds`` 重新结算。
    """
    store = await get_store()
    try:
        result = await store.correct_draw(
            draw_id,
            payload.special_number,
            draw_date=payload.draw_date,
            actor_user_id=actor_user_id(admin),
        )
    except ValueError as exc:
        detail = str(exc)
        status = 404 if "不存在" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc

    logger.info(
        "draw_corrected action=%s draw_id=%s period=%s old=%s new=%s "
        "actor=%s resettled=%s correction_id=%s",
        result.get("action"),
        draw_id,
        result.get("period"),
        result.get("old_special_number"),
        result.get("new_special_number"),
        actor_user_id(admin),
        result.get("resettled_rounds"),
        result.get("correction_id"),
    )
    return {
        **result,
        "draw": decorate_draw(result["draw"]),
    }


@router.delete("/draws/{draw_id}")
async def delete_draw(draw_id: int, _admin=Depends(require_admin)):
    store = await get_store()
    deleted = await store.delete_draw(draw_id)
    if deleted == 0:
        raise HTTPException(status_code=404, detail="开奖记录不存在")
    return {"deleted": deleted}


@router.delete("/draws")
async def clear_draws(_admin=Depends(require_admin)):
    store = await get_store()
    deleted = await store.clear_draws()
    return {"deleted": deleted}
