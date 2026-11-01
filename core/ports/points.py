"""
积分域写入口（端口）：跨模块触发积分事件的唯一通道。

content / exam / files 在**自己的业务事务内**触发积分——发帖、点赞、审核通过、考试通过——
原先直接 ``from app.modules.points.rules import enqueue_points_event`` / ``points.service``。
这类调用是同步且必须同事务的（L2），因此**不能**改成消息总线上的异步事件：那会把
「内容发布成功」与「积分入账」拆成两次独立提交，回滚时只剩半条链。

改为端口后：

- 消费方只依赖本模块的函数签名，不再 import points 的任何实现模块；
- ``db`` 由调用方传入并与业务写共用，保证同事务；
- 单测可 ``ports.reset()`` + ``ports.install("points_writer", 假实现)`` 替换。

实现由 ``app.bootstrap`` 在装配期绑定。未绑定即
:class:`~core.ports.registry.PortNotBound`——宁可在第一个积分事件上炸，也不静默丢账。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.ports.registry import get


async def enqueue_event(
    db: AsyncSession, user_id: uuid.UUID, event: str, ref_id: str
) -> None:
    """把一次用户行为事件排进 outbox（与业务同事务落库，relay 稍后投给积分 worker）。"""
    await get("points_writer").enqueue_event(db, user_id, event, ref_id)


async def reward(
    db: AsyncSession,
    user_id: uuid.UUID,
    delta: int,
    reason: str,
    ref_type: str,
    ref_id: str,
    *,
    allow_negative: bool = False,
) -> Any:
    """发放/扣减积分（原子、幂等）；返回本次或既有的流水。"""
    return await get("points_writer").reward(
        db, user_id, delta, reason, ref_type, ref_id, allow_negative=allow_negative
    )


async def spend(
    db: AsyncSession,
    user_id: uuid.UUID,
    amount: int,
    reason: str,
    ref_type: str,
    ref_id: str,
) -> Any:
    """消费积分（余额不足拒）；返回本次流水。"""
    return await get("points_writer").spend(
        db, user_id, amount, reason, ref_type, ref_id
    )
