"""出票单 API 的请求模型（枚举一律英文 / 数字；汉字只进 ``description`` 文案）。

设计口径：与 ``scripts/pick_ticket.py`` 的 CLI 完全同源 —— 请求体只描述
「出票形状 + 号码卫生开关」，具体排序与金额分配一律交给 ``services.pick_ticket``
（它再复用 ``services.lottery`` 的生产引擎），本层不做任何算术。

所有开关都是**三态**：``None``（缺省）= 沿用当前用户存储设置；``True`` / ``False``
= 只对本次请求覆盖，**不落库**。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from services.lottery import (
    MODE_PATTERN,
    PICK_COUNT_MAX,
    PICK_COUNT_MIN,
    SOFT_WEIGHT_MAX,
    SOFT_WEIGHT_MIN,
    TOTAL_AMOUNT_MAX,
    TOTAL_AMOUNT_MIN,
)

# 种子长度上限：只作批号派生，给足空间但不接受无限长字符串（防脏请求撑爆哈希输入）
SEED_MAX_LENGTH = 64


class PickTicketRequest(BaseModel):
    """``POST /api/pick/ticket`` 请求体：一次出票的全部可变输入。"""

    budget: int | None = Field(
        default=None,
        ge=TOTAL_AMOUNT_MIN,
        le=TOTAL_AMOUNT_MAX,
        description="本次预算（元）；缺省 = 用当前用户存储设置里的最大投注金额",
    )
    pick_count: int | None = Field(
        default=None,
        ge=PICK_COUNT_MIN,
        le=PICK_COUNT_MAX,
        description="注数；缺省 = 用当前用户存储设置",
    )
    mode: str | None = Field(
        default=None,
        pattern=MODE_PATTERN,
        description="筹码模式；缺省 = 用当前用户存储设置",
    )
    seed: int | str | None = Field(
        default=None,
        description=(
            "种子。缺省 = 生产引擎既定名次；给了 = 在引擎候选排序里滑动一个窗口"
            "（前端「换一批」）。种子只决定取哪一段，不改变中奖概率与期望值。"
        ),
    )
    include_repeat_number: bool | None = Field(
        default=None,
        description="上期特码是否留在候选池（True=保留、只降权；缺省 = 用存储设置）",
    )
    exclude_repeat_zodiac: bool | None = Field(
        default=None, description="是否避开最新一期同肖整组（缺省 = 用存储设置）"
    )
    stale_weight: float | None = Field(
        default=None,
        ge=SOFT_WEIGHT_MIN,
        le=SOFT_WEIGHT_MAX,
        description="冷号降权系数；1.0 = 不降权（缺省 = 用存储设置）",
    )
    lattice_enabled: bool | None = Field(
        default=None, description="预测波动线点阵开关（缺省 = 用存储设置）"
    )

    @field_validator("seed")
    @classmethod
    def _validate_seed(cls, value: int | str | None) -> int | str | None:
        """种子只作批号派生，限制长度但**不做归一化**（归一化在纯核心 ``seed_key``）。"""
        if value is None:
            return None
        if len(str(value)) > SEED_MAX_LENGTH:
            raise ValueError(f"seed 最长 {SEED_MAX_LENGTH} 个字符")
        return value

    def toggle_overrides(self) -> dict[str, Any]:
        """只取「号码卫生」类覆盖项（形状类 budget / pick_count / mode / seed 另走参数）。"""
        candidates: dict[str, Any] = {
            "include_repeat_number": self.include_repeat_number,
            "exclude_repeat_zodiac": self.exclude_repeat_zodiac,
            "stale_weight": self.stale_weight,
            "lattice_enabled": self.lattice_enabled,
        }
        return {key: value for key, value in candidates.items() if value is not None}


class PickTicketSimulateRequest(BaseModel):
    """``POST /api/pick/simulate`` 请求体：按一张票据的**形状**给诚实的结果分布。"""

    ticket: dict[str, Any] = Field(description="POST /api/pick/ticket 返回的票据对象")
    periods: int = Field(
        default=208, ge=1, le=10000, description="模拟期数（默认 208 = 本池已评估期数）"
    )


class PickTicketFreezeRequest(BaseModel):
    """``POST /api/pick/freeze`` 请求体：把票据冻结进前瞻验证账本（append-only）。"""

    ticket: dict[str, Any] = Field(description="POST /api/pick/ticket 返回的票据对象")
    include_fit_demo: bool = Field(
        default=False,
        description="是否连「仅拟合演示」对照行一起冻结（默认 False，只冻线上引擎 + 均匀对照）",
    )


__all__ = [
    "SEED_MAX_LENGTH",
    "PickTicketFreezeRequest",
    "PickTicketRequest",
    "PickTicketSimulateRequest",
]
