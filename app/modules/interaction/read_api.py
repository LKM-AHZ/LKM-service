"""interaction 域对外只读 API（``core.ports.interaction`` 的实现去处）。

只提供判定与聚合谓词：调用方传入自己的外键列做关联，interaction 域只用自己的表。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.interaction.models import InteractionFavorite
from app.modules.interaction.repository import (
    ContentSubscriptionRepository,
    PostWatchRepository,
    UserRelationshipRepository,
    normalize_target,
)


async def get_blocked_ids(
    db: AsyncSession, user_id: uuid.UUID
) -> list[uuid.UUID]:
    """与 ``user_id`` 存在 block 的对方 id（**双向**合并；block 语义互斥）。"""
    return await UserRelationshipRepository(db).list_blocked_either_direction(user_id)


async def get_muted_ids(
    db: AsyncSession, user_id: uuid.UUID
) -> list[uuid.UUID]:
    """``user_id`` 主动静音的目标 id（单向：对方不受影响）。"""
    return await UserRelationshipRepository(db).list_target_ids(user_id, "mute")


async def get_hidden_author_ids(
    db: AsyncSession, user_id: uuid.UUID
) -> list[uuid.UUID]:
    """时间线/检索/通知过滤用的隐藏作者集合：block（双向）∪ mute（单向）。"""
    blocked = set(await get_blocked_ids(db, user_id))
    blocked.update(await get_muted_ids(db, user_id))
    return list(blocked)


async def is_blocked_either_direction(
    db: AsyncSession, user_id: uuid.UUID, other_id: uuid.UUID
) -> bool:
    """两用户之间任一方向存在 block 即为 ``True``。"""
    return await UserRelationshipRepository(db).is_blocked_either_direction(
        user_id, other_id
    )


async def is_muted(
    db: AsyncSession, user_id: uuid.UUID, target_id: uuid.UUID
) -> bool:
    """``user_id`` 是否静音了 ``target_id``（单向判定）。"""
    return await UserRelationshipRepository(db).relation_exists(
        user_id, target_id, "mute"
    )


async def list_subscription_user_ids(
    db: AsyncSession, targets: list[tuple[str, str]]
) -> list[uuid.UUID]:
    """命中任一目标的订阅者 id（``notify_level='none'`` 排除；tag 目标读侧也归一）。"""
    normalized = [normalize_target(t, i) for t, i in targets if i]
    return await ContentSubscriptionRepository(db).list_user_ids(
        normalized, require_notify=True
    )


async def list_watch_user_ids(
    db: AsyncSession, content_id: uuid.UUID
) -> list[uuid.UUID]:
    """该内容的 watcher id（``notify_level='none'`` 排除）。"""
    return await PostWatchRepository(db).list_watch_user_ids(content_id)


async def watch_post(
    db: AsyncSession, user_id: uuid.UUID, content_id: uuid.UUID
) -> None:
    """跨域写入口：评论发表后自动 watch 该帖（``notify_level=None`` 保留既有偏好）。"""
    await PostWatchRepository(db).watch(user_id, content_id)


def favorite_count_subquery(outer_column: Any) -> Any:
    """「该内容被收藏次数」的标量子查询（``outer_column`` 由调用方传入）。

    返回的是**未 label** 的标量表达式，由调用方按需命名——SQL 与原先内联写法逐字相同。
    """
    return (
        select(func.count())
        .select_from(InteractionFavorite)
        .where(InteractionFavorite.content_id == outer_column)
        .scalar_subquery()
    )
