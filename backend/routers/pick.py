"""出票单（选号工具）API：生成 / 模拟 / 冻结到前瞻验证账本。

## 这个接口交付什么（以及**不**交付什么）

已独立验证的事实：本游戏每个号码等概率（1/49），赔率 47 时单注期望收益率恒为
``47 / 49 − 1 = −4.0816%``，**与选号方法、权重、注数、注码无关**；本池样本内走步
回测命中率落在抽样噪声内（``verdict = noise``）。所以本接口**绝不**声称、暗示或
包装「更容易中奖」—— 返回体里带机器可读的 ``claim = "NO_EDGE"``，
任何下游层都不得把它当预测结果展示。

它真正交付的五件事：纪律与花费控制 / 号码卫生 / 覆盖透明 / 可复现可审计 /
诚实风险披露（期望值、随机基线、结果分布、历史最久连续未中期数）。

## 三层分工（本文件只做编排，不做算术）

| 层 | 职责 |
|----|------|
| ``routers/pick.py``（本文件） | 取数与鉴权、把请求合并进设置、错误码映射 |
| ``services/pick_ticket.py`` | 纯核心：出票 / 覆盖报告 / 诚实页脚 / 逐字节复现 |
| ``services/lottery.py`` | **唯一**的选号排序与金额分配来源（本层不重写、不改变它） |

## 枚举口径

返回体里写进任何字段的枚举一律**英文或数字**（``claim`` / ``selection`` / ``mode`` /
``role`` / ``wave_type`` / ``soft_reasons`` / ``verdict`` / ``data_status`` …），
汉字只出现在 ``*_label``、``*_note``、``notes`` 之类的展示文案里。

## 冻结到账本是**可选**能力

``POST /api/pick/freeze`` 走可选的适配层 ``services.pick_freeze``（导入守卫在那里），
账本模块缺失 / 接口不兼容时返回 ``501``，出票与模拟**完全不受影响**。
本文件不直接依赖账本模块，线上推荐路径（``services/lottery``）更不依赖它。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from dependencies import actor_user_id, require_vip
from models.pick_ticket import (
    PickTicketFreezeRequest,
    PickTicketRequest,
    PickTicketSimulateRequest,
)
from repository import get_store
from services import pick_freeze
from services.pick_ticket import (
    TicketDataError,
    build_ticket,
    simulate_ticket,
)

router = APIRouter(prefix="/api/pick", tags=["pick"])


async def _draws_and_settings(actor) -> tuple[list[dict], dict]:
    """取本池开奖（最新在前）与**当前用户**的生效设置（匿名落地全局模板）。"""
    store = await get_store()
    draws = await store.list_draws()
    settings = dict(await store.get_settings(actor_user_id(actor)))
    return draws, settings


@router.post("/ticket")
async def post_pick_ticket(
    payload: PickTicketRequest | None = None, actor=Depends(require_vip)
):
    """生成一张出票单（不落库、不写设置；同 seed + 同数据 + 同设置逐字节可复现）。

    请求里的 ``budget`` / ``pick_count`` / ``mode`` / 四个号码卫生开关都是**本次覆盖**，
    与 ``POST /api/recommend`` 的临场试算口径一致：只影响这一次返回，不改存储设置。
    """
    payload = payload or PickTicketRequest()
    draws, settings = await _draws_and_settings(actor)
    # 号码卫生类开关：先合并进设置，再交给纯核心（它内部会 clamp）
    settings.update(payload.toggle_overrides())
    try:
        return build_ticket(
            draws,
            settings=settings,
            budget=payload.budget,
            pick_count=payload.pick_count,
            seed=payload.seed,
            mode=payload.mode,
        )
    except TicketDataError as exc:
        # 数据不足这类问题一律 400 + 中文说明（不是 500，也不是用 0 值冒充）
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/simulate")
async def post_pick_simulate(payload: PickTicketSimulateRequest):
    """按一张票据的**形状**（注数 / 金额 / 赔率）给出诚实的结果分布与期望亏损。

    等概率假设下的数学期望与波动，不是历史预测，也不承诺任何命中能力。
    """
    try:
        return simulate_ticket(payload.ticket, periods=payload.periods)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=f"票据结构不完整，无法模拟：{exc}"
        ) from exc


@router.get("/ledger")
async def get_pick_ledger(actor=Depends(require_vip)):
    """前瞻验证账本的进度 / 完整性 / 当前判定（账本模块缺失时 ``available=false``）。"""
    if not pick_freeze.is_available():
        return {
            "available": False,
            "reason": pick_freeze.unavailable_reason(),
            "note": "账本模块当前不可用；出票与模拟不受影响。",
        }
    store = await get_store()
    draws = await store.list_draws()
    try:
        return pick_freeze.status(draws)
    except Exception as exc:  # pragma: no cover - 账本文件损坏等罕见情况
        raise HTTPException(
            status_code=502, detail=f"账本读取失败：{type(exc).__name__}: {exc}"
        ) from exc


@router.post("/freeze")
async def post_pick_freeze(
    payload: PickTicketFreezeRequest, actor=Depends(require_vip)
):
    """把一张出票单冻结进前瞻验证账本（append-only，冻结后不可改写）。

    拒绝而不是编造：目标期已开奖（禁止事后补冻）或已有冻结记录时返回 ``409``；
    账本模块不可用时返回 ``501``（前端据此降级，出票流程照常可用）。
    """
    if not pick_freeze.is_available():
        raise HTTPException(
            status_code=501,
            detail=(
                "前瞻验证账本当前不可用："
                f"{pick_freeze.unavailable_reason()}；"
                "出票单仍可正常生成与导出，冻结请改用 CLI。"
            ),
        )
    store = await get_store()
    draws = await store.list_draws()
    try:
        return pick_freeze.freeze_ticket(
            ticket=payload.ticket,
            draws=draws,
            include_fit_demo=payload.include_fit_demo,
        )
    except pick_freeze.FreezeRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except pick_freeze.FreezeUnavailable as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
