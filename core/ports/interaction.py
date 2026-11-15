"""interaction 域的只读端口：关注关系与「大 V」判定。

feed 读路径要按「我关注了谁 / 我关注了哪些版块」过滤，物化回填要判定大 V；
这些数据的归属与缓存口径都在 interaction 域，原先直接 import ``interaction.service``。
feed ↔ interaction 本就是双向依赖，走端口后双方都只依赖 core，环自然消失。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.ports.registry import get


async def get_following_ids(db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """我关注的所有用户 id（带缓存）。"""
    return await get("interaction_read").get_following_ids(db, user_id)


async def get_followed_board_ids(
    db: AsyncSession, user_id: uuid.UUID
) -> list[uuid.UUID]:
    """我关注的所有版块 id（带缓存）。"""
    return await get("interaction_read").get_followed_board_ids(db, user_id)


async def list_bigv_author_ids(db: AsyncSession, *, threshold: int) -> list[uuid.UUID]:
    """关注者数超过 ``threshold`` 的作者 id。"""
    return await get("interaction_read").list_bigv_author_ids(db, threshold=threshold)


async def list_follower_ids(
    db: AsyncSession, author_id: uuid.UUID, *, limit: int
) -> list[uuid.UUID]:
    """某作者的关注者 id（最多 ``limit`` 条，用于物化扇出受众面）。"""
    return await get("interaction_read").list_follower_ids(db, author_id, limit=limit)


async def list_board_follower_ids(
    db: AsyncSession, board_id: uuid.UUID, *, limit: int
) -> list[uuid.UUID]:
    """某版块的关注者 id（最多 ``limit`` 条）。"""
    return await get("interaction_read").list_board_follower_ids(
        db, board_id, limit=limit
    )


async def get_blocked_ids(
    db: AsyncSession, user_id: uuid.UUID
) -> list[uuid.UUID]:
    """与 ``user_id`` 存在 block 的对方 id（双向合并）。"""
    return await get("interaction_read").get_blocked_ids(db, user_id)


async def get_muted_ids(db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """``user_id`` 主动静音的目标 id（单向）。"""
    return await get("interaction_read").get_muted_ids(db, user_id)


async def get_hidden_author_ids(
    db: AsyncSession, user_id: uuid.UUID
) -> list[uuid.UUID]:
    """隐藏作者集合：block（双向）∪ mute（单向）。"""
    return await get("interaction_read").get_hidden_author_ids(db, user_id)


async def is_blocked_either_direction(
    db: AsyncSession, user_id: uuid.UUID, other_id: uuid.UUID
) -> bool:
    """任一方向存在 block 即为 ``True``。"""
    return await get("interaction_read").is_blocked_either_direction(
        db, user_id, other_id
    )


async def is_muted(
    db: AsyncSession, user_id: uuid.UUID, target_id: uuid.UUID
) -> bool:
    """``user_id`` 是否静音了 ``target_id``。"""
    return await get("interaction_read").is_muted(db, user_id, target_id)


async def list_subscription_user_ids(
    db: AsyncSession, targets: list[tuple[str, str]]
) -> list[uuid.UUID]:
    """命中任一内容订阅目标（board/tag/column）的用户 id（``notify_level='none'`` 排除）。"""
    return await get("interaction_read").list_subscription_user_ids(db, targets)


async def list_watch_user_ids(
    db: AsyncSession, content_id: uuid.UUID
) -> list[uuid.UUID]:
    """某内容的 watcher 用户 id（``notify_level='none'`` 排除）。"""
    return await get("interaction_read").list_watch_user_ids(db, content_id)


async def watch_post(
    db: AsyncSession, user_id: uuid.UUID, content_id: uuid.UUID
) -> None:
    """评论等互动后自动 watch 内容（跨域写入口，保持 interaction 单方写）。"""
    await get("interaction_read").watch_post(db, user_id, content_id)


def favorite_count_subquery(outer_column: Any) -> Any:
    """「该内容被收藏次数」的标量子查询（``outer_column`` 为调用方的关联列）。"""
    return get("interaction_read").favorite_count_subquery(outer_column)
