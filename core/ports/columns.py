"""columns 域端口：待审队列、专栏标题投影、存在性判定与时间线源。

步骤 9 切片 3 把专栏拆成独立域后，content 侧原先直接读 ``columns`` / ``column_posts``
的四处置都改走这里。读口返回**投影**（标量、字典、未执行的 Select），不把 ORM 实例交给
调用方——这里没有跨域可变行。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.ports.registry import get


async def pending_column_applications(db: AsyncSession) -> tuple[int, Any]:
    """待审专栏申请：``(数量, 最早提交时间)``。"""
    return await get("columns_api").pending_column_applications(db)


async def column_titles(
    db: AsyncSession, column_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """批量取专栏标题（内容项展示 ``column_title`` 用）。"""
    return await get("columns_api").column_titles(db, column_ids)


async def column_exists(db: AsyncSession, column_id: uuid.UUID) -> bool:
    """专栏是否存在（content 建 column_post 前的前置校验）。"""
    return await get("columns_api").column_exists(db, column_id)


async def column_owner_id(db: AsyncSession, column_id: uuid.UUID) -> uuid.UUID | None:
    """专栏属主 id（不存在返回 None）；供调用方做对象级权限判定。"""
    return await get("columns_api").column_owner_id(db, column_id)


def select_feed_source(
    *,
    author_ids: Any,
    board_ids: Any,
    before_time: Any,
    before_id: Any,
    after_time: Any,
    after_id: Any,
    limit: int,
) -> Any:
    """已发布专栏连载的按游标一窗（未执行）。"""
    return get("columns_api").select_feed_source(
        author_ids=author_ids,
        board_ids=board_ids,
        before_time=before_time,
        before_id=before_id,
        after_time=after_time,
        after_id=after_id,
        limit=limit,
    )
