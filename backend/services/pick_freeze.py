"""出票单 → 前瞻验证账本的**可选**适配层（缺失即降级，绝不阻断出票）。

## 为什么要有这一层

前瞻验证账本（开奖前冻结、开奖后诚实计分）是唯一能证明「策略到底有没有用」的
口径，``backend/forward_ledger/`` 就是它的落盘位置。出票单（``services.pick_ticket``）
与账本是两件事，但把它们接起来才有审计价值：**先把票冻住，再等开奖对账**。

依赖方向必须单向、可断、且**尽量晚**：

    services/pick_ticket  →  （无依赖）                  纯出票核心
    services/pick_freeze  →  账本模块（**懒加载**，可缺失）  本层：唯一知道账本存在的地方
    routers/*             →  services/pick_ticket        出票 / 模拟，永不触碰账本
                           →  services.pick_freeze       只有「冻结 / 查账本」两个接口

本层用 ``importlib`` **按需**加载账本模块，并把「找不到 / 导入期报错 / 接口不兼容」
统一转成 :class:`FreezeUnavailable`。因此：

- 应用启动时**不会**加载账本模块（线上推荐路径零感知）；
- 账本模块被删掉 / 改名 / 还没写好时，出票与模拟**完全不受影响**；
- 前端拿到的只是 ``available=false`` + 一句原因，按钮自然降级。

## 铁律

- **只搬运，不计算**：冻结用的选号 / 金额 / 统计全部由账本模块自己的公开函数产出；
  本层只做「取票据里的期号与设置 → 调冻结 → 落盘」。
- **不新增字段、不翻译枚举**：账本里落的是 ``PRODUCTION_ENGINE`` / ``UNIFORM`` 这类英文码。
- **append-only 不绕过**：目标期已开奖（禁止事后补冻）或已有记录时，账本会抛错，
  本层原样转成 :class:`FreezeRejected`（HTTP 409），**绝不**覆盖或追加第二条。
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Mapping, Sequence

# 账本模块的导入路径（本层是唯一知道它的地方；路由层只按函数名调用）
LEDGER_MODULE_NAME = "services.forward_ledger"

# 冻结 / 读账本依赖的最小接口集合：缺任意一个就视为「接口不兼容」，整体降级
REQUIRED_LEDGER_API = (
    "freeze_periods",
    "load_ledger",
    "dump_ledger",
    "status_payload",
    "LedgerError",
)

# 懒加载缓存：None = 还没试过；模块对象 = 可用；(None, 原因) 表示已确认不可用
_cached_module: Any = None
_load_attempted = False
_load_error: str | None = None


class FreezeUnavailable(RuntimeError):
    """账本模块不可用（未安装 / 导入失败 / 接口不兼容）→ 调用方降级为「暂不可用」。"""


class FreezeRejected(ValueError):
    """账本拒绝了本次冻结（目标期已开奖 / 已有冻结记录 / 数据不足）→ 调用方回 409。"""


def _load() -> Any:
    """懒加载账本模块（只尝试一次，失败结果也缓存，避免每个请求都重试导入）。"""
    global _cached_module, _load_attempted, _load_error
    if _load_attempted:
        return _cached_module
    _load_attempted = True
    try:
        module = importlib.import_module(LEDGER_MODULE_NAME)
    except Exception as exc:  # 模块不存在 / 导入期报错 / 依赖缺失
        _load_error = f"{type(exc).__name__}: {exc}"
        return None
    missing = [name for name in REQUIRED_LEDGER_API if not hasattr(module, name)]
    if missing:
        _load_error = f"账本模块缺少接口：{', '.join(missing)}"
        return None
    _cached_module = module
    return module


def is_available() -> bool:
    """账本模块是否可用（导入成功且关键接口齐全）。"""
    return _load() is not None


def unavailable_reason() -> str | None:
    """不可用原因（可用时为 ``None``），用于接口降级文案。"""
    if _load() is not None:
        return None
    return _load_error or "前瞻验证账本模块未安装"


def _require() -> Any:
    module = _load()
    if module is None:
        raise FreezeUnavailable(unavailable_reason() or "前瞻验证账本模块不可用")
    return module


def ledger_path() -> str | None:
    """账本文件的绝对路径（可用时），供前端提示「冻结到哪个文件」。"""
    module = _load()
    if module is None:
        return None
    path = getattr(module, "DEFAULT_LEDGER_PATH", None)
    return str(Path(path)) if path is not None else None


def _normalize_draws(draws: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把存储层的开奖行整成账本要的形状（period / draw_date / special_number）。"""
    rows: list[dict[str, Any]] = []
    for draw in draws:
        if draw.get("special_number") is None or draw.get("period") is None:
            continue
        rows.append(
            {
                "period": int(draw["period"]),
                "draw_date": str(draw["draw_date"]),
                "special_number": int(draw["special_number"]),
            }
        )
    return rows


def target_period_of(ticket: Mapping[str, Any]) -> int | None:
    """票据里的目标期号（``data.target_period``）；缺失时返回 ``None``，不猜。"""
    data = ticket.get("data") or {}
    period = data.get("target_period")
    try:
        return int(period) if period is not None else None
    except (TypeError, ValueError):
        return None


def freeze_ticket(
    *,
    ticket: Mapping[str, Any],
    draws: Sequence[Mapping[str, Any]],
    include_fit_demo: bool = False,
) -> dict[str, Any]:
    """把一张出票单冻结进账本（append-only），返回冻结回执。

    票据里携带的 ``settings`` 就是本次出票实际生效的设置 —— 冻结沿用同一份，
    保证「冻下来的预测」与「打印出来的那张票」是同一件事。
    """
    module = _require()

    period = target_period_of(ticket)
    if period is None:
        raise FreezeRejected("票据里没有目标期号（data.target_period），拒绝冻结")

    rows = _normalize_draws(draws)
    if not rows:
        raise FreezeRejected("本池没有已导入的开奖记录，无法冻结任何期")

    settings = dict(ticket.get("settings") or {})
    if not settings:
        raise FreezeRejected("票据里没有生效设置（settings），拒绝冻结")

    picks = ticket.get("picks") or []
    pick_count = len(picks) or None

    ledger = module.load_ledger()
    try:
        frozen = module.freeze_periods(
            ledger,
            rows,
            [period],
            settings=settings,
            pick_count=pick_count,
            include_fit_demo=bool(include_fit_demo),
        )
    except module.LedgerError as exc:
        # 已开奖 / 已有记录 / 数据不足：原样转 409，绝不覆盖
        raise FreezeRejected(str(exc)) from exc

    path = module.dump_ledger(frozen)
    record = frozen["records"][-1]
    return {
        "ok": True,
        "period": int(record["period"]),
        "record_hash": str(record["record_hash"]),
        "prev_hash": str(record["prev_hash"]),
        "available_length": int(record["available_length"]),
        "period_index": int(record["period_index"]),
        "strategies": [str(entry["strategy"]) for entry in record["strategies"]],
        "records_total": len(frozen["records"]),
        "ledger_path": str(Path(path)),
        "note": (
            "已按开奖前冻结写入账本：冻结后不可改写，开奖后按同期号自动计分。"
            "账本只做诚实对账，不改变中奖概率与期望值。"
        ),
    }


def status(draws: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """账本进度 + 完整性 + 当前判定（不可用时返回 ``available=False``，不抛错）。"""
    module = _load()
    if module is None:
        return {
            "available": False,
            "reason": unavailable_reason(),
        }
    rows = _normalize_draws(draws)
    ledger = module.load_ledger()
    payload = module.status_payload(ledger, rows)
    default_path = getattr(module, "DEFAULT_LEDGER_PATH", None)
    return {
        "available": True,
        "ledger_path": str(Path(default_path)) if default_path is not None else None,
        **payload,
    }


__all__ = [
    "LEDGER_MODULE_NAME",
    "REQUIRED_LEDGER_API",
    "FreezeRejected",
    "FreezeUnavailable",
    "freeze_ticket",
    "is_available",
    "ledger_path",
    "status",
    "target_period_of",
    "unavailable_reason",
]
