"""feed 域物化时间线的写入口（端口）。

content 软删、interaction 关注/取关都会触发物化 feed 的回填或清理，原先分别 import
``feed.fanout``。物化表的写入口径（快照语义、大 V 跳过、版块保留规则）属 feed 域，
收进端口后调用方不再持有这些细节。
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from core.ports.registry import get


async def backfill_author(
    db: AsyncSession, follower_id: uuid.UUID, author_id: uuid.UUID, limit: int
) -> int:
    """把作者最近 ``limit`` 条内容补进该关注者的物化 feed（新关注时调用，幂等）。"""
    return await get("feed_fanout").backfill_author(db, follower_id, author_id, limit)


async def backfill_board(
    db: AsyncSession, follower_id: uuid.UUID, board_id: uuid.UUID, limit: int
) -> int:
    """把版块最近 ``limit`` 条讨论帖补进该关注者的物化 feed（新关注版块时调用）。"""
    return await get("feed_fanout").backfill_board(db, follower_id, board_id, limit)


async def remove_author_items(
    db: AsyncSession,
    follower_id: uuid.UUID,
    author_id: uuid.UUID,
    keep_boards: set[uuid.UUID],
) -> int:
    """取消关注某作者时清理其条目（保留 ``keep_boards`` 内的版块来源）。"""
    return await get("feed_fanout").remove_author_items(
        db, follower_id, author_id, keep_boards
    )


async def remove_source_item(db: AsyncSession, source_id: uuid.UUID) -> int:
    """按源条目 id 清理物化行，返回删除行数（内容软删后必须调用）。"""
    return await get("feed_fanout").remove_source_item(db, source_id)


async def remove_board_items(
    db: AsyncSession,
    follower_id: uuid.UUID,
    board_id: uuid.UUID,
    keep_authors: set[uuid.UUID],
) -> int:
    """取消关注版块时清理该版块条目（保留 ``keep_authors`` 内的作者来源）。"""
    return await get("feed_fanout").remove_board_items(
        db, follower_id, board_id, keep_authors
    )
