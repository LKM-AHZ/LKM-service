"""信息流(feed)域只读 GraphQL：时间线合流。

**关注关系的 GraphQL 面已迁出**：``FollowQuery`` + ``GraphFollowUser``/``GraphFollowBoard``
现由 ``app.modules.interaction.graphql`` 提供（字段名 ``myFollowingUsers`` /
``myFollowingBoards`` 不变，api 层 ``merge_types`` 仍会并进单一 GraphQL Query）。
本文件只剩时间线 ``TimelineQuery``。
"""

import uuid

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types.info import Info

from app.modules.feed.service import get_timeline


@strawberry.type
class GraphFeedItem:
    itemType: str
    id: strawberry.ID
    authorId: strawberry.ID | None
    authorName: str
    title: str
    contentPreview: str
    createdAt: str
    boardId: strawberry.ID | None = None
    url: str


@strawberry.type
class GraphFeedResponse:
    items: list[GraphFeedItem]
    nextCursor: str | None


def _get_db(info: Info) -> AsyncSession:
    return info.context.db


def _get_user_id(info: Info) -> uuid.UUID | None:
    return info.context.user_id


@strawberry.type
class TimelineQuery:
    @strawberry.field
    async def timeline(
        self,
        info: Info,
        mode: str = "follow",
        cursor: str | None = None,
        limit: int = 20,
    ) -> GraphFeedResponse:
        db = _get_db(info)
        user_id = _get_user_id(info)
        # GraphQL 没有 FastAPI Query(ge=1, le=100) 那层约束，limit 是纯客户端可控：不夹紧的话
        # timeline(limit: 10_000_000) 会让服务端拉/排序任意大集合，limit<=0 更会被 PG 直接
        # 拒绝 LIMIT -1（500）。与 REST 端点同口径夹到 [1, 100]。
        limit = max(1, min(limit, 100))
        feed = await get_timeline(
            db, user_id=user_id, mode=mode, cursor=cursor, limit=limit
        )
        return GraphFeedResponse(
            items=[
                GraphFeedItem(
                    itemType=it.item_type,
                    id=it.id,
                    authorId=it.author_id,
                    authorName=it.author_name,
                    title=it.title,
                    contentPreview=it.content_preview,
                    createdAt=it.created_at.isoformat(),
                    boardId=it.board_id,
                    url=it.url,
                )
                for it in feed.items
            ],
            nextCursor=feed.next_cursor,
        )
