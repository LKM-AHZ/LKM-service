"""评论点赞计数：写穿列 + 按明细对账（步骤 9 切片 2 自 ``content/counters.py`` 迁出）。

``content_comments.like_count`` 是派生列，真相源是 ``content_comment_likes`` 行数：

- 写路径（点赞/取消）恒为写穿——与明细同事务原子 UPDATE，不存在 Redis 增量窗口；
- 对账由 content 的 ``reconcile_counts`` 在某批 item 被扫到时顺带触发
  （``core.ports.comment.reconcile_comment_likes``），复用 item 的 ``counts_reconciled_at``
  两级拍（15 分钟增量 + 日级全量），不另立收敛标记列。
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.comment.errors import CommentErr
from app.modules.comment.models import ContentComment, ContentCommentLike
from core.err import BizError


async def bump_like_count(db: AsyncSession, comment_id: uuid.UUID, delta: int) -> int:
    """评论点赞计数：写穿原子 UPDATE（下限 0），返回新值。

    不接 Redis write-behind：那套是为 ``content_items`` 三项计数设计的回退通道，评论点赞
    只有写穿一条路径，不存在 flush 窗口。行不存在（含已硬删）抛 ``COMMENT_NOT_FOUND``，
    使调用方事务整体回滚。
    """
    result = await db.execute(
        sa.update(ContentComment)
        .where(ContentComment.id == comment_id)
        .values(like_count=func.greatest(ContentComment.like_count + delta, 0))
        .returning(ContentComment.like_count)
    )
    row = result.first()
    if row is None:
        raise BizError(CommentErr.COMMENT_NOT_FOUND)
    return int(row[0])


async def reconcile_comment_likes(db: AsyncSession, item_ids: list[uuid.UUID]) -> int:
    """把这批 item 下所有评论的 ``like_count`` 按明细重算，返回被修正的行数。

    单条 UPDATE + 相关子查询（不是逐行回写）：一页 500 条 item 下的评论可能上千，逐行发
    UPDATE 会把这拍拖成全表往返。``synchronize_session=False`` 是必须的——WHERE 里的
    相关子查询无法在 Python 侧求值，默认的 evaluate 策略会直接抛错。
    """
    if not item_ids:
        return 0
    real_like = (
        select(func.count())
        .select_from(ContentCommentLike)
        .where(ContentCommentLike.comment_id == ContentComment.id)
        .scalar_subquery()
    )
    result = cast(
        CursorResult[Any],
        await db.execute(
            sa.update(ContentComment)
            .where(
                ContentComment.content_id.in_(item_ids),
                ContentComment.like_count != real_like,
            )
            .values(like_count=real_like)
            .execution_options(synchronize_session=False)
        ),
    )
    return int(result.rowcount or 0)
