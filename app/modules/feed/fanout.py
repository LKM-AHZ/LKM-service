"""时间线写扩散（M6.11）：把新内容 fanout 给关注者，写入物化读模型。

驱动方式：**cron 水位扫描**（每源一条 ``(created_at, id)`` 水位，见
``FeedFanoutState``），而不是给 6 个内容源都挂事件钩子——这样新增源只需进
``feed.SOURCES``/``FOLLOW_SOURCES`` 即可被覆盖，且不引入新的 topic / worker 容器。

- 水位单调推进到「已完整 fanout 的最大条目」；中途失败不推进 → 下轮重放，
  重复由 ``feed_items`` 的 ``(user_id, item_type, source_id)`` 唯一约束吸收。
- **大 V 封顶**：作者关注者数（含关注该内容版块者）超过 ``feed_fanout_max_followers``
  时整条跳过、只把作者记入 Redis 大 V 集合；读路径对这些作者走实时合流补齐
  （见 ``service.get_timeline`` 的物化分支），既不写放大也不丢内容。
- **Article 不参与**：它无作者外键（follow 源里也不参与个性化），无法按作者扩散。

关注者变更时的回填见 :func:`backfill_author`（新关注一位作者时补其最近 N 条）。
"""

from __future__ import annotations

import datetime
import logging
import uuid

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_client
from app.core.cache import make_key
from app.core.config import settings
from app.modules.feed import feed as feed_src
from app.modules.feed.models import (
    BoardFollow,
    FeedFanoutState,
    FeedItemMaterialized,
    UserFollow,
)
from app.modules.feed.schemas import FeedItem

logger = logging.getLogger(__name__)


def _bigv_key() -> str:
    return make_key("feed", "bigv")


async def bigv_authors() -> set[uuid.UUID]:
    """当前被标记为「大 V」的作者 id 集合（Redis 不可用 → 空集，读路径退化为纯物化）。"""
    client = await redis_client.get_redis()
    if client is None:
        return set()
    try:
        members = await client.smembers(_bigv_key())
    except Exception:
        return set()
    out: set[uuid.UUID] = set()
    for m in members or ():
        try:
            out.add(uuid.UUID(m))
        except (TypeError, ValueError):
            continue
    return out


async def _mark_bigv(author_id: uuid.UUID) -> None:
    client = await redis_client.get_redis()
    if client is None:
        return
    try:
        await client.sadd(_bigv_key(), str(author_id))
    except Exception:
        logger.warning("mark bigv failed for author %s", author_id, exc_info=True)


async def _follower_ids(
    db: AsyncSession, author_id: uuid.UUID | None, board_id: uuid.UUID | None
) -> set[uuid.UUID]:
    """该条目的受众：关注作者的人 ∪ 关注该内容版块的人（均过滤软删）。"""
    followers: set[uuid.UUID] = set()
    if author_id is not None:
        rows = await db.execute(
            select(UserFollow.follower_id).where(
                UserFollow.following_id == author_id,
                UserFollow.deleted_at.is_(None),
            )
        )
        followers |= set(rows.scalars().all())
    if board_id is not None:
        rows = await db.execute(
            select(BoardFollow.follower_id).where(
                BoardFollow.board_id == board_id,
                BoardFollow.deleted_at.is_(None),
            )
        )
        followers |= set(rows.scalars().all())
    return followers


async def _fanout_item(db: AsyncSession, item: FeedItem) -> int:
    """把一条内容写入其受众的物化 feed；返回写入的受众数（大 V 跳过时 0）。"""
    if item.author_id is None and item.board_id is None:
        return 0

    followers = await _follower_ids(db, item.author_id, item.board_id)
    if not followers:
        return 0

    if len(followers) > settings.feed_fanout_max_followers:
        # 大 V：跳过写扩散，标记后由读路径实时补齐（避免 O(关注者) 写入突刺）
        if item.author_id is not None:
            await _mark_bigv(item.author_id)
        logger.info(
            "skip fanout for %s#%s: %d followers exceed cap",
            item.item_type,
            item.id,
            len(followers),
        )
        return 0

    values = [
        {
            "user_id": uid,
            "item_type": item.item_type,
            "source_id": item.id,
            "author_id": item.author_id,
            "board_id": item.board_id,
            "sort_score": item.sort_score,
            "title": item.title,
            "content_preview": item.content_preview,
            "url": item.url,
            "created_at": item.created_at,
        }
        for uid in sorted(followers)
    ]
    stmt = pg_insert(FeedItemMaterialized).values(values)
    stmt = stmt.on_conflict_do_nothing(constraint="uq_feed_item")
    await db.execute(stmt)
    return len(values)


async def _load_state(db: AsyncSession, source: str) -> FeedFanoutState:
    """取源水位；首次出现时以「现在」为起点（存量内容不回填，由实时合流兜底读）。"""
    state = await db.get(FeedFanoutState, source)
    if state is None:
        state = FeedFanoutState(
            source=source,
            last_created_at=datetime.datetime.now(datetime.UTC),
            last_id=None,
        )
        db.add(state)
        await db.flush()
    return state


async def fanout_batch(db: AsyncSession, per_source_limit: int = 200) -> int:
    """扫描各源水位之后的新内容并 fanout，返回本轮处理条目数。

    每个源每轮最多处理 ``per_source_limit`` 条（升序），水位推进到本批最大条目；
    还有积压时下一轮继续（不会跳过）。
    """
    processed = 0
    for name in feed_src.FOLLOW_SOURCES:
        state = await _load_state(db, name)
        fetch = feed_src.SOURCES[name]
        items: list[FeedItem] = await fetch(
            db,
            None,
            None,
            None,
            None,
            per_source_limit,
            after_time=state.last_created_at,
            after_id=state.last_id,
        )
        if not items:
            continue
        for item in items:
            await _fanout_item(db, item)
        state.last_created_at = items[-1].created_at
        state.last_id = items[-1].id
        processed += len(items)
        await db.flush()
    return processed


async def backfill_author(
    db: AsyncSession, follower_id: uuid.UUID, author_id: uuid.UUID, limit: int
) -> int:
    """把作者最近 ``limit`` 条内容补进该关注者的物化 feed（新关注时调用，幂等）。"""
    values: list[dict[str, object]] = []
    for name in feed_src.FOLLOW_SOURCES:
        fetch = feed_src.SOURCES[name]
        # discussion 源要求 author/board 两个集合都给才按作者过滤，故传空版块集
        items: list[FeedItem] = await fetch(db, {author_id}, set(), None, None, limit)
        values.extend(
            {
                "user_id": follower_id,
                "item_type": item.item_type,
                "source_id": item.id,
                "author_id": item.author_id or author_id,
                "board_id": item.board_id,
                "sort_score": item.sort_score,
                "title": item.title,
                "content_preview": item.content_preview,
                "url": item.url,
                "created_at": item.created_at,
            }
            for item in items
        )
    if not values:
        return 0
    stmt = pg_insert(FeedItemMaterialized).values(values)
    stmt = stmt.on_conflict_do_nothing(constraint="uq_feed_item")
    await db.execute(stmt)
    await db.flush()
    return len(values)


async def backfill_board(
    db: AsyncSession, follower_id: uuid.UUID, board_id: uuid.UUID, limit: int
) -> int:
    """把版块最近 ``limit`` 条讨论帖补进该关注者的物化 feed（新关注版块时调用）。"""
    items: list[FeedItem] = await feed_src.SOURCES["discussion"](
        db, set(), {board_id}, None, None, limit
    )
    if not items:
        return 0
    values = [
        {
            "user_id": follower_id,
            "item_type": item.item_type,
            "source_id": item.id,
            "author_id": item.author_id,
            "board_id": item.board_id,
            "sort_score": item.sort_score,
            "title": item.title,
            "content_preview": item.content_preview,
            "url": item.url,
            "created_at": item.created_at,
        }
        for item in items
    ]
    stmt = pg_insert(FeedItemMaterialized).values(values)
    stmt = stmt.on_conflict_do_nothing(constraint="uq_feed_item")
    await db.execute(stmt)
    await db.flush()
    return len(values)


async def remove_author_items(
    db: AsyncSession, follower_id: uuid.UUID, author_id: uuid.UUID
) -> int:
    """取消关注某作者时清理其条目（否则物化 feed 会残留已取关的内容）。"""
    result = await db.execute(
        sa_delete(FeedItemMaterialized).where(
            FeedItemMaterialized.user_id == follower_id,
            FeedItemMaterialized.author_id == author_id,
        )
    )
    await db.flush()
    return int(result.rowcount or 0)


async def remove_board_items(
    db: AsyncSession,
    follower_id: uuid.UUID,
    board_id: uuid.UUID,
    keep_authors: set[uuid.UUID],
) -> int:
    """取消关注版块时清理该版块条目。

    仅删「不再因作者关注而保留」的行——``board_id`` 匹配但作者仍在关注列表里的条目
    保留（同一内容可能同时因作者与版块进入 feed）。
    """
    conds = [
        FeedItemMaterialized.user_id == follower_id,
        FeedItemMaterialized.board_id == board_id,
    ]
    if keep_authors:
        conds.append(
            FeedItemMaterialized.author_id.is_(None)
            | FeedItemMaterialized.author_id.notin_(keep_authors)
        )
    result = await db.execute(sa_delete(FeedItemMaterialized).where(*conds))
    await db.flush()
    return int(result.rowcount or 0)


async def count_materialized(db: AsyncSession, user_id: uuid.UUID) -> int:
    """某用户物化 feed 的条目数（读路径判「物化是否可用」，以及测试断言用）。"""
    return (
        await db.scalar(
            select(func.count())
            .select_from(FeedItemMaterialized)
            .where(FeedItemMaterialized.user_id == user_id)
        )
    ) or 0
