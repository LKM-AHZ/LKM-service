"""comment 域端口：评论摘要、评论数投影与评论点赞对账。

步骤 9 切片 2 把评论拆成独立域后，content 侧原先直接 join/读 ``content_comments`` 的三处
都改走这里：通知解析的评论摘要、``reconcile_counts`` 的 ``comment_count`` 标量子查询
（SQL 逐字不变，软删口径仍由属主域维护）、举报队列的评论正文。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.ports.registry import get


async def comment_brief(db: AsyncSession, comment_id: uuid.UUID) -> Any:
    """未软删的评论摘要（``id`` / ``content_id`` / ``user_id`` / ``parent_id``）。"""
    return await get("comment_api").comment_brief(db, comment_id)


async def comment_body(db: AsyncSession, comment_id: uuid.UUID) -> str:
    """评论正文；不存在即抛 ``COMMENT_NOT_FOUND``（由属主域抛出，保证错误码同源）。"""
    return await get("comment_api").comment_body(db, comment_id)


def comment_count_subquery(outer_column: Any) -> Any:
    """「该内容项的未软删评论数」标量子查询（供 content 的对照 UPDATE 使用）。"""
    return get("comment_api").comment_count_subquery(outer_column)


async def reconcile_comment_likes(db: AsyncSession, item_ids: list[uuid.UUID]) -> int:
    """把这批内容项下所有评论的 ``like_count`` 按明细重算，返回被修正的行数。"""
    return await get("comment_api").reconcile_comment_likes(db, item_ids)


async def list_comments(
    db: AsyncSession,
    item_id: uuid.UUID,
    *,
    page: int = 1,
    limit: int = 20,
    viewer_id: uuid.UUID | None = None,
) -> Any:
    """评论分页（楼层升序），带 viewer 时回填 ``liked``。"""
    return await get("comment_api").list_comments(
        db, item_id, page=page, limit=limit, viewer_id=viewer_id
    )


async def list_all_comments(
    db: AsyncSession, item_id: uuid.UUID, viewer_id: uuid.UUID | None = None
) -> Any:
    """一次取回某内容的全部评论（供 content 的 GraphQL 组装评论树）。"""
    return await get("comment_api").list_all_comments(db, item_id, viewer_id)
