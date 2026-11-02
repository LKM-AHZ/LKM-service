"""评论（comment）域仓储：评论区查询与点赞明细。

步骤 9 切片 2 自 ``content.repository`` 迁出，SQL 逐字保留。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.modules.comment.models import ContentComment, ContentCommentLike
from core.db.repository import AsyncRepository


class ContentCommentRepository(AsyncRepository[ContentComment]):
    model = ContentComment

    async def count_in_content(self, item_id: uuid.UUID) -> int:
        return await self.count(ContentComment.content_id == item_id)

    async def list_in_content(
        self, item_id: uuid.UUID, *, offset: int = 0, limit: int = 20
    ) -> list[ContentComment]:
        return await self.get_many(
            ContentComment.content_id == item_id,
            order_by=ContentComment.floor_number.asc(),
            offset=offset,
            limit=limit,
        )

    async def list_all_in_content(self, item_id: uuid.UUID) -> list[ContentComment]:
        return await self.get_many(
            ContentComment.content_id == item_id,
            order_by=ContentComment.floor_number.asc(),
        )

    async def max_floor_number(self, item_id: uuid.UUID) -> int | None:
        """当前最大楼层号；**含已软删**——否则软删后新评论会与可见楼层重号。"""
        return await self.db.scalar(
            select(func.max(ContentComment.floor_number)).where(
                ContentComment.content_id == item_id
            )
        )


class ContentCommentLikeRepository(AsyncRepository[ContentCommentLike]):
    model = ContentCommentLike

    async def get_one_like(
        self, *, comment_id: uuid.UUID, user_id: uuid.UUID
    ) -> ContentCommentLike | None:
        return await self.get_one(
            ContentCommentLike.comment_id == comment_id,
            ContentCommentLike.user_id == user_id,
        )

    async def liked_comment_ids(
        self, *, comment_ids: list[uuid.UUID], user_id: uuid.UUID
    ) -> set[uuid.UUID]:
        """批量取「该用户点过赞的评论 id」。

        评论列表逐条查会退化成 N+1（一页 20 条评论 = 20 次往返），故按页一次查完。
        """
        if not comment_ids:
            return set()
        rows = await self.db.scalars(
            select(ContentCommentLike.comment_id).where(
                ContentCommentLike.comment_id.in_(comment_ids),
                ContentCommentLike.user_id == user_id,
            )
        )
        return set(rows)
