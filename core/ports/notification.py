"""
站内信写入口（端口）。

目前唯一跨域消费者是 admin 的每日待办摘要：它要给每个管理员落一条 ``review_digest``
通知。通知类型是**落库值**（写进 DB、被偏好接口白名单消费），因此类型枚举的单一事实源
必须留在 notification 域——admin 只按名字取值，不复制字面量。

为什么不让 admin 直接写 ``notifications`` 表：聚合去重（同 actor+target 窗口内合并）、
偏好开关、负载结构都由 notification 域持有，跨域复制这些规则必然漂移。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.ports.registry import get


def type_value(name: str) -> str:
    """把一个通知类型的**成员名**解析成落库值（如 ``"REVIEW_DIGEST"`` → ``"review_digest"``）。"""
    return get("notification_writer").type_value(name)


async def create(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    type: str,
    payload: dict[str, Any] | None = None,
    actor_id: uuid.UUID | None = None,
    target_id: uuid.UUID | None = None,
) -> Any:
    """落一条通知（不 commit，与调用方事务同生共死）。"""
    return await get("notification_writer").create(
        db,
        user_id=user_id,
        type=type,
        payload=payload,
        actor_id=actor_id,
        target_id=target_id,
    )
