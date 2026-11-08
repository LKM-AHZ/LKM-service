"""评论（comment）域对外只读 API（``core.ports.comment`` 的实现去处）。

三件事，都只读或只算：

- ``comment_brief``：通知解析「评论/回复」事件要回查评论归属与父评论；
- ``comment_count_subquery``：content 的 ``reconcile_counts`` 用它算 ``comment_count``，
  软删口径与在线计数同处维护，SQL 与拆分前逐字相同；
- ``comment_body``：举报队列要回填被举报评论的正文。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.comment.errors import CommentErr
from app.modules.comment.models import ContentComment
from core.err import BizError


@dataclass(frozen=True)
class CommentBrief:
    id: uuid.UUID
    content_id: uuid.UUID
    user_id: uuid.UUID
    parent_id: uuid.UUID | None


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


async def comment_body(db: AsyncSession, comment_id: uuid.UUID) -> str:
    """被举报评论的正文；不存在即抛 ``COMMENT_NOT_FOUND``（envelope 会整体回滚）。"""
    row = await db.scalar(select(ContentComment).where(ContentComment.id == comment_id))
    if row is None:
        raise BizError(CommentErr.COMMENT_NOT_FOUND)
    return row.content


def comment_count_subquery(item_id: Any) -> Any:
    """指向某条内容项的「未软删评论数」标量子查询。

    口径与在线计数一致：**不含已软删评论**。若此处不滤，软删评论会被对账反复算回来，
    与「软删时 comment_count 减一」互相覆盖、永久震荡。
    """
    return (
        select(func.count())
        .select_from(ContentComment)
        .where(
            ContentComment.content_id == item_id,
            ContentComment.deleted_at.is_(None),
        )
        .scalar_subquery()
    )
