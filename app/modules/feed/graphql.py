"""信息流(feed)域只读 GraphQL：关注(follow) + 时间线合流(timeline) 两 Query 聚合。

M2.3 汇集原 follow/graphql（FollowQuery + GraphFollowUser/Board，按登录态返回「我关注」
用户/版块）与原 timeline/graphql（TimelineQuery + GraphFeedItem/Response，按当前用户可选
个性化 read 合流）。两者类型与字段名互不重名、语义独立，各保留 Query 类；api 层
``merge_types`` 会后并进单一 GraphQL Query（字段仍为 myFollowingUsers/myFollowingBoards/
timeline，REST/前端字段名契约不破）。
"""

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types.info import Info

from app.modules.feed import service as feed_service
from app.modules.feed.service import get_timeline

# -- 关注关系（原 follow/graphql）--


@strawberry.type
class GraphFollowUser:
    userId: strawberry.ID
    displayName: str
    avatar: str | None


@strawberry.type
class GraphFollowBoard:
    boardId: strawberry.ID
    title: str


# -- 时间线合流（原 timeline/graphql）--


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


def _get_user_id(info: Info) -> int | None:
    return info.context.user_id


@strawberry.type
class FollowQuery:
    @strawberry.field
    async def myFollowingUsers(self, info: Info) -> list[GraphFollowUser]:
        user_id = _get_user_id(info)
        db = _get_db(info)
        if user_id is None:
            return []
        rows = await feed_service.list_following_users(db, user_id)
        return [
            GraphFollowUser(userId=uid, displayName=name, avatar=avatar)
            for uid, name, avatar in rows
        ]

    @strawberry.field
    async def myFollowingBoards(self, info: Info) -> list[GraphFollowBoard]:
        user_id = _get_user_id(info)
        db = _get_db(info)
        if user_id is None:
            return []
        rows = await feed_service.list_followed_boards(db, user_id)
        return [GraphFollowBoard(boardId=bid, title=title) for bid, title in rows]


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
