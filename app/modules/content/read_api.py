"""content 域对外只读 API（``core.ports.content`` 的实现去处）。

只返回**不可变的摘要对象**，不把 ORM 实例交给调用方：跨域持有可变行会让「谁负责 flush」
变得模糊，而调用方（通知、后台）只需要几个字段。
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.content.models import (
    Board,
    BoardApplication,
    ColumnApplication,
    ContentComment,
    ContentItem,
    ContentLike,
    ContentType,
)


@dataclass(frozen=True)
class ItemBrief:
    id: uuid.UUID
    title: str
    author_id: uuid.UUID | None


@dataclass(frozen=True)
class CommentBrief:
    id: uuid.UUID
    content_id: uuid.UUID
    user_id: uuid.UUID
    parent_id: uuid.UUID | None


async def item_brief(db: AsyncSession, item_id: uuid.UUID) -> ItemBrief | None:
    """未软删的内容项摘要。"""
    row = await db.scalar(
        select(ContentItem).where(
            ContentItem.id == item_id, ContentItem.deleted_at.is_(None)
        )
    )
    if row is None:
        return None
    return ItemBrief(id=row.id, title=row.title, author_id=row.author_id)


async def comment_brief(db: AsyncSession, comment_id: uuid.UUID) -> CommentBrief | None:
    """未软删的评论摘要。"""
    row = await db.scalar(
        select(ContentComment).where(
            ContentComment.id == comment_id, ContentComment.deleted_at.is_(None)
        )
    )
    if row is None:
        return None
    return CommentBrief(
        id=row.id,
        content_id=row.content_id,
        user_id=row.user_id,
        parent_id=row.parent_id,
    )


async def count_discussions(db: AsyncSession) -> int:
    """讨论帖总数（不含软删）。"""
    value = await db.scalar(
        select(func.count(ContentItem.id)).where(
            ContentItem.content_type == ContentType.DISCUSSION,
            ContentItem.deleted_at.is_(None),
        )
    )
    return int(value or 0)


async def daily_discussion_counts(
    db: AsyncSession, start: datetime.date, days: int
) -> dict[datetime.date, int]:
    """讨论帖按日增量（UTC 日期分桶）。查询失败返回空 dict（后台统计 fail-open）。"""
    out: dict[datetime.date, int] = {}
    try:
        day_expr = func.date(func.timezone("UTC", ContentItem.created_at))
        start_dt = datetime.datetime.combine(
            start, datetime.datetime.min.time(), tzinfo=datetime.UTC
        )
        end_dt = start_dt + datetime.timedelta(days=days)
        stmt = select(day_expr.label("d"), func.count()).where(
            ContentItem.created_at >= start_dt,
            ContentItem.created_at < end_dt,
            ContentItem.content_type == ContentType.DISCUSSION,
            ContentItem.deleted_at.is_(None),
        )
        rows = (await db.execute(stmt.group_by("d"))).all()
    except Exception:
        return out
    for row in rows:
        raw = row[0]
        if raw is None:
            continue
        out[datetime.date.fromisoformat(str(raw))] = int(row[1])
    return out


async def _pending(
    db: AsyncSession, model: Any
) -> tuple[int, datetime.datetime | None]:
    row = (
        await db.execute(
            select(func.count(model.id), func.min(model.created_at)).where(
                model.status == "pending"
            )
        )
    ).one()
    return int(row[0]), row[1]


async def pending_board_applications(
    db: AsyncSession,
) -> tuple[int, datetime.datetime | None]:
    """待审版块申请：``(数量, 最早提交时间)``。"""
    return await _pending(db, BoardApplication)


async def pending_column_applications(
    db: AsyncSession,
) -> tuple[int, datetime.datetime | None]:
    """待审专栏申请：``(数量, 最早提交时间)``。"""
    return await _pending(db, ColumnApplication)


async def bookmark_count(db: AsyncSession, content_id: uuid.UUID) -> int | None:
    """当前收藏计数；内容行不存在（含软删）返回 None。"""
    value = await db.scalar(
        select(ContentItem.bookmark_count).where(
            ContentItem.id == content_id,
            ContentItem.deleted_at.is_(None),
        )
    )
    return None if value is None else int(value)


async def bump_bookmark_count(
    db: AsyncSession, content_id: uuid.UUID, delta: int
) -> int | None:
    """原子增减 ``bookmark_count`` 并返回即时读数（下限 0）；行不存在返回 None。"""
    result = await db.execute(
        sa_update(ContentItem)
        .where(
            ContentItem.id == content_id,
            ContentItem.deleted_at.is_(None),
        )
        .values(bookmark_count=func.greatest(ContentItem.bookmark_count + delta, 0))
        .returning(ContentItem.bookmark_count)
    )
    row = result.first()
    return None if row is None else int(row[0])


async def exists_content(db: AsyncSession, content_id: uuid.UUID) -> bool:
    """内容行是否存在（浏览上报的 404 前置判定）。"""
    return (
        await db.scalar(
            select(ContentItem.id).where(
                ContentItem.id == content_id,
                ContentItem.deleted_at.is_(None),
            )
        )
    ) is not None


async def item_counts(
    db: AsyncSession, content_id: uuid.UUID
) -> tuple[int, int] | None:
    """``(like_count, bookmark_count)``；内容行不存在（含软删）返回 None。"""
    row = (
        await db.execute(
            select(ContentItem.like_count, ContentItem.bookmark_count).where(
                ContentItem.id == content_id,
                ContentItem.deleted_at.is_(None),
            )
        )
    ).first()
    if row is None:
        return None
    return int(row[0]), int(row[1])


async def liked_by(db: AsyncSession, content_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """该用户是否点赞过该内容。"""
    return (
        await db.scalar(
            select(ContentLike.content_id).where(
                ContentLike.content_id == content_id,
                ContentLike.user_id == user_id,
            )
        )
    ) is not None


def content_brief_view() -> Any:
    """内容摘要的**可 join 子查询**（``id`` / ``content_type`` / ``title`` / ``slug`` / ``board_id``）。

    供其他域在自己的分页 SQL 里 join：既保住单条语句的分页与排序语义，又不必 import
    ``content.models``。已软删的行不进视图（与历史 join 过滤等价）。
    """
    return (
        select(
            ContentItem.id.label("id"),
            ContentItem.content_type.label("content_type"),
            ContentItem.title.label("title"),
            ContentItem.slug.label("slug"),
            ContentItem.board_id.label("board_id"),
        )
        .where(ContentItem.deleted_at.is_(None))
        .subquery("content_brief")
    )


def board_brief_view() -> Any:
    """板块摘要的可 join 子查询（``id`` / ``title``）。"""
    return select(Board.id.label("id"), Board.title.label("title")).subquery(
        "board_brief"
    )
