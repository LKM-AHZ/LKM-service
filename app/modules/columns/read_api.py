"""专栏（columns）域对外只读 API（``core.ports.columns`` 的实现去处）。

三件事：后台待审队列的计数/最早时间、内容项展示用的专栏标题批查、以及专栏存在性判定
（content 的 ``create_item`` 校验 column_post 的挂载点）。
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.columns.models import Column, ColumnApplication


async def pending_column_applications(
    db: AsyncSession,
) -> tuple[int, datetime.datetime | None]:
    """待审专栏申请：``(数量, 最早提交时间)``。"""
    row = (
        await db.execute(
            select(
                func.count(ColumnApplication.id), func.min(ColumnApplication.created_at)
            ).where(ColumnApplication.status == "pending")
        )
    ).one()
    return int(row[0]), row[1]


async def column_titles(
    db: AsyncSession, column_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """批量取专栏标题（内容项列表/详情展示 ``column_title`` 用）。"""
    if not column_ids:
        return {}
    rows = await db.execute(
        select(Column.id, Column.title).where(Column.id.in_(column_ids))
    )
    return {cid: title for cid, title in rows.all()}


async def column_exists(db: AsyncSession, column_id: uuid.UUID) -> bool:
    """专栏是否存在（content 建 column_post 前的前置校验）。"""
    return await db.scalar(select(Column.id).where(Column.id == column_id)) is not None


async def column_owner_id(db: AsyncSession, column_id: uuid.UUID) -> uuid.UUID | None:
    """专栏属主 id（不存在返回 None）；供 content 做对象级权限判定。"""
    return await db.scalar(select(Column.owner_id).where(Column.id == column_id))
