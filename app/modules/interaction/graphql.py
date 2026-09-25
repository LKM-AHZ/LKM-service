"""interaction 域只读 GraphQL：关注关系（原 feed 域，按蓝图 §7.2 目标形态归入本域）。

字段名 ``myFollowingUsers`` / ``myFollowingBoards`` **保持不变**：REST URL 与 GraphQL 字段
名在模块归属调整中一律不破（api 层 ``merge_types`` 后并进单一 Query，前端无需改动）。
"""

from __future__ import annotations

import uuid

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types.info import Info

from app.modules.interaction import service as interaction_service


@strawberry.type
class GraphFollowUser:
    userId: strawberry.ID
    displayName: str
    avatar: str | None


@strawberry.type
class GraphFollowBoard:
    boardId: strawberry.ID
    title: str


def _get_db(info: Info) -> AsyncSession:
    return info.context.db


def _get_user_id(info: Info) -> uuid.UUID | None:
    return info.context.user_id


@strawberry.type
class FollowQuery:
    @strawberry.field
    async def myFollowingUsers(self, info: Info) -> list[GraphFollowUser]:
        user_id = _get_user_id(info)
        db = _get_db(info)
        if user_id is None:
            return []
        rows = await interaction_service.list_following_users(db, user_id)
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
        rows = await interaction_service.list_followed_boards(db, user_id)
        return [GraphFollowBoard(boardId=bid, title=title) for bid, title in rows]
