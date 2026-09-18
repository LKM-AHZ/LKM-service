"""interaction 业务逻辑：收藏（幂等添加/取消）与浏览记录（upsert 幂等）。

- 收藏写两处：``interaction_favorites`` 明细行 + ``content_items.bookmark_count`` 计数，
  计数用原子 ``UPDATE ... RETURNING`` 而非读改写，避免并发丢计数；行不存在即 404。
- 浏览上报走 ``INSERT ... ON CONFLICT DO UPDATE``：同一 (user_id, content_id) 只保留
  最近一次 ``viewed_at``，重复上报天然幂等，行数上界 = 用户数 × 内容数。
- 列表接口 join ``content_items`` 内联标题等摘要，避免前端逐条回拉（N+1）。

跨模块只读内容表：与 feed 同款取法（直接 import content.models），已在 pyproject 的
import-linter 契约中精确豁免；不建跨模块 ORM relationship，保持边界单向。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import counters
from app.core.common import PageData, paginate_offset, paginate_pages
from app.core.err import BizError
from app.modules.content.models import ContentItem
from app.modules.interaction.errors import InteractionErr
from app.modules.interaction.models import InteractionFavorite, InteractionViewLog
from app.modules.interaction.schemas import (
    FavoriteItem,
    FavoriteState,
    HistoryItem,
    ViewState,
)


async def _bookmark_count(db: AsyncSession, content_id: uuid.UUID) -> int:
    """内容不存在 → 404；存在则返回当前收藏计数。"""
    count = await db.scalar(
        select(ContentItem.bookmark_count).where(ContentItem.id == content_id)
    )
    if count is None:
        raise BizError(InteractionErr.CONTENT_NOT_FOUND)
    return int(count)


async def _bump_bookmark(db: AsyncSession, content_id: uuid.UUID, delta: int) -> int:
    """增减 ``bookmark_count`` 并返回即时读数（下限 0）。

    M6.10：优先走 Redis 增量链路（收藏明细行是真相源，计数由 flush 收敛）；Redis
    未启用/不可达时回退到原有原子 UPDATE，语义不变。
    """
    if await counters.bump_counter("bookmark_count", content_id, delta):
        base = await db.scalar(
            select(ContentItem.bookmark_count).where(ContentItem.id == content_id)
        )
        if base is None:
            raise BizError(InteractionErr.CONTENT_NOT_FOUND)
        pending = await counters.pending_delta("bookmark_count", content_id)
        return int(base) + pending

    result = await db.execute(
        sa_update(ContentItem)
        .where(ContentItem.id == content_id)
        .values(bookmark_count=func.greatest(ContentItem.bookmark_count + delta, 0))
        .returning(ContentItem.bookmark_count)
    )
    row: Any = result.first()
    if row is None:
        raise BizError(InteractionErr.CONTENT_NOT_FOUND)
    return int(row[0])


async def _is_favorited(db: AsyncSession, user_id: uuid.UUID, content_id: uuid.UUID) -> bool:
    found = await db.scalar(
        select(InteractionFavorite.content_id).where(
            InteractionFavorite.user_id == user_id,
            InteractionFavorite.content_id == content_id,
        )
    )
    return found is not None


async def add_favorite(
    db: AsyncSession, user_id: uuid.UUID, content_id: uuid.UUID
) -> FavoriteState:
    """收藏：重复调用不报错也不重复计数（复合主键兜并发）。"""
    count = await _bookmark_count(db, content_id)
    if await _is_favorited(db, user_id, content_id):
        return FavoriteState(
            content_id=content_id, favorited=True, bookmark_count=count
        )

    # 先在 savepoint 里落明细：并发下同一 (content_id, user_id) 撞复合主键只回滚本
    # savepoint（不牵连调用方事务的其它未提交写），视为「已收藏」幂等返回——此时
    # 计数未增，故直接返回原值而非再 bump。
    sp = await db.begin_nested()
    try:
        db.add(InteractionFavorite(content_id=content_id, user_id=user_id))
        await db.flush()
        await sp.commit()
    except IntegrityError:
        await sp.rollback()
        return FavoriteState(
            content_id=content_id, favorited=True, bookmark_count=count
        )

    new_count = await _bump_bookmark(db, content_id, 1)
    return FavoriteState(
        content_id=content_id, favorited=True, bookmark_count=new_count
    )


async def remove_favorite(
    db: AsyncSession, user_id: uuid.UUID, content_id: uuid.UUID
) -> FavoriteState:
    """取消收藏：未收藏时幂等返回当前计数，不递减。"""
    count = await _bookmark_count(db, content_id)
    result = await db.execute(
        delete(InteractionFavorite).where(
            InteractionFavorite.user_id == user_id,
            InteractionFavorite.content_id == content_id,
        )
    )
    if (result.rowcount or 0) == 0:
        return FavoriteState(
            content_id=content_id, favorited=False, bookmark_count=count
        )
    new_count = await _bump_bookmark(db, content_id, -1)
    return FavoriteState(
        content_id=content_id, favorited=False, bookmark_count=new_count
    )


async def list_favorites(
    db: AsyncSession, user_id: uuid.UUID, page: int = 1, limit: int = 20
) -> PageData[FavoriteItem]:
    total = (
        await db.scalar(
            select(func.count())
            .select_from(InteractionFavorite)
            .where(InteractionFavorite.user_id == user_id)
        )
        or 0
    )
    rows = (
        await db.execute(
            select(
                InteractionFavorite.content_id,
                InteractionFavorite.created_at,
                ContentItem.content_type,
                ContentItem.title,
                ContentItem.slug,
                ContentItem.board_id,
            )
            .join(ContentItem, ContentItem.id == InteractionFavorite.content_id)
            .where(InteractionFavorite.user_id == user_id)
            .order_by(
                InteractionFavorite.created_at.desc(),
                InteractionFavorite.content_id.desc(),
            )
            .offset(paginate_offset(page, limit))
            .limit(limit)
        )
    ).all()
    items = [
        FavoriteItem(
            content_id=r.content_id,
            content_type=r.content_type,
            title=r.title,
            slug=r.slug,
            board_id=r.board_id,
            created_at=r.created_at,
        )
        for r in rows
    ]
    return PageData(
        items=items, total=total, page=page, pages=paginate_pages(total, limit)
    )


async def record_view(db: AsyncSession, user_id: uuid.UUID, content_id: uuid.UUID) -> ViewState:
    """浏览上报：同内容重复调用幂等（只刷新 viewed_at）。"""
    exists = await db.scalar(select(ContentItem.id).where(ContentItem.id == content_id))
    if exists is None:
        raise BizError(InteractionErr.CONTENT_NOT_FOUND)

    viewed_at = datetime.datetime.now(datetime.UTC)
    stmt = pg_insert(InteractionViewLog).values(
        user_id=user_id, content_id=content_id, viewed_at=viewed_at
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_interaction_view_user_content",
        set_={"viewed_at": stmt.excluded.viewed_at},
    )
    await db.execute(stmt)
    return ViewState(content_id=content_id, viewed_at=viewed_at)


async def list_history(
    db: AsyncSession, user_id: uuid.UUID, page: int = 1, limit: int = 20
) -> PageData[HistoryItem]:
    total = (
        await db.scalar(
            select(func.count())
            .select_from(InteractionViewLog)
            .where(InteractionViewLog.user_id == user_id)
        )
        or 0
    )
    rows = (
        await db.execute(
            select(
                InteractionViewLog.content_id,
                InteractionViewLog.viewed_at,
                ContentItem.content_type,
                ContentItem.title,
                ContentItem.slug,
                ContentItem.board_id,
            )
            .join(ContentItem, ContentItem.id == InteractionViewLog.content_id)
            .where(InteractionViewLog.user_id == user_id)
            .order_by(InteractionViewLog.viewed_at.desc(), InteractionViewLog.id.desc())
            .offset(paginate_offset(page, limit))
            .limit(limit)
        )
    ).all()
    items = [
        HistoryItem(
            content_id=r.content_id,
            content_type=r.content_type,
            title=r.title,
            slug=r.slug,
            board_id=r.board_id,
            viewed_at=r.viewed_at,
        )
        for r in rows
    ]
    return PageData(
        items=items, total=total, page=page, pages=paginate_pages(total, limit)
    )


async def purge_stale_view_logs(db: AsyncSession, retention_days: int) -> int:
    """保留策略：删除超过保留期的浏览记录，返回删除行数（cron 调用）。"""
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        days=retention_days
    )
    result = await db.execute(
        delete(InteractionViewLog).where(InteractionViewLog.viewed_at < cutoff)
    )
    return int(result.rowcount or 0)
