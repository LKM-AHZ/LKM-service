"""身份快照读端口：业务侧经此读取用户展示性身份（auth 提供实现）。

``UserSnapshot``/``UserManagementItem`` 等类型是共享契约，见 ``core.contracts``；
本模块只转发「读」这个动作。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.contracts import UserManagementItem, UserSnapshot
from core.ports.registry import get


async def get_user_snapshot(
    db: AsyncSession, *, user_id: uuid.UUID
) -> UserSnapshot | None:
    """单个用户快照（先缓存、后 DB；无该用户返回 None）。"""
    return await get("snapshot").get_user_snapshot(db, user_id=user_id)


async def get_user_snapshot_batch(
    db: AsyncSession, *, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, UserSnapshot]:
    """批量用户快照，返回 ``{user_id: UserSnapshot}``（缺失的 id 不在结果里）。"""
    return await get("snapshot").get_user_snapshot_batch(db, user_ids=user_ids)


async def list_user_snapshots(
    db: AsyncSession,
    *,
    q: str | None = None,
    offset: int = 0,
    limit: int = 50,
    include_pii: bool = False,
) -> tuple[list[UserManagementItem], int]:
    """管理面分页列表读，返回 ``(rows, total)``。"""
    return await get("snapshot").list_user_snapshots(
        db, q=q, offset=offset, limit=limit, include_pii=include_pii
    )


async def count_active_users(db: AsyncSession) -> int:
    """活跃用户总数（真值只在 auth 库）。"""
    return await get("snapshot").count_active_users(db)


async def user_count_by_day(
    db: AsyncSession, *, start: datetime.date, days: int
) -> dict[datetime.date, int]:
    """按日新增用户数，返回 ``{date: count}``（窗口内无数据的日期不出现）。"""
    return await get("snapshot").user_count_by_day(db, start=start, days=days)
