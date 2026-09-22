"""interaction 业务逻辑：收藏（幂等添加/取消）与浏览记录（upsert 幂等）。

- 收藏写两处：``interaction_favorites`` 明细行 + ``content_items.bookmark_count`` 计数，
  计数用原子 ``UPDATE ... RETURNING`` 而非读改写，避免并发丢计数；行不存在即 404。
- 浏览上报走 ``INSERT ... ON CONFLICT DO UPDATE``：同一 (user_id, content_id) 只保留
  最近一次 ``viewed_at``，重复上报天然幂等，行数上界 = 用户数 × 内容数。
- 列表接口 join ``content_items`` 内联标题等摘要，避免前端逐条回拉（N+1）。

跨模块只读内容表：与 feed 同款取法（现经 ``interaction.repository`` 收口），已在
pyproject 的 import-linter 契约中精确豁免；不建跨模块 ORM relationship，保持边界单向。
"""

from __future__ import annotations

import datetime
import uuid

from app.core import counters
from app.core.common import PageData, paginate_offset, paginate_pages
from app.core.err import BizError
from app.db.repository import DbSession
from app.modules.interaction.errors import InteractionErr
from app.modules.interaction.repository import (
    InteractionContentItemRepository,
    InteractionFavoriteRepository,
    InteractionViewLogRepository,
)
from app.modules.interaction.schemas import (
    FavoriteItem,
    FavoriteState,
    HistoryItem,
    ViewState,
)


async def _bookmark_count(db: DbSession, content_id: uuid.UUID) -> int:
    """内容不存在 → 404；存在则返回当前收藏计数。"""
    count = await InteractionContentItemRepository(db).get_bookmark_count(content_id)
    if count is None:
        raise BizError(InteractionErr.CONTENT_NOT_FOUND)
    return int(count)


async def _current_bookmark_count(db: DbSession, content_id: uuid.UUID) -> int:
    """即时收藏读数 = DB 计数列 + 未落库 Redis 增量，夹紧到 >= 0（不存在则 404）。

    该读数是**近似值**：读 DB 与读 pending 之间若 flush 恰好 drain 并落库，会取到
    「旧 base + 已清零 pending」而偏小。但下限必须夹紧——DB 回退路径用 greatest(...,0)，
    这里不夹紧会在 flush 窗口/增量丢失时给前端负计数。
    """
    base = await _bookmark_count(db, content_id)
    pending = await counters.pending_delta("bookmark_count", content_id)
    return max(0, base + pending)


async def _bump_bookmark(db: DbSession, content_id: uuid.UUID, delta: int) -> int:
    """增减 ``bookmark_count`` 并返回即时读数（下限 0）。

    M6.10：优先走 Redis 增量链路（收藏明细行是真相源，计数由 flush 收敛）；Redis
    未启用/不可达时回退到原有原子 UPDATE，语义不变。
    """
    repo = InteractionContentItemRepository(db)
    if await counters.bump_counter("bookmark_count", content_id, delta):
        return await _current_bookmark_count(db, content_id)

    new_count = await repo.bump_bookmark_count(content_id, delta)
    if new_count is None:
        raise BizError(InteractionErr.CONTENT_NOT_FOUND)
    return new_count


async def _is_favorited(
    db: DbSession, user_id: uuid.UUID, content_id: uuid.UUID
) -> bool:
    return await InteractionFavoriteRepository(db).is_favorited(
        user_id=user_id, content_id=content_id
    )


async def add_favorite(
    db: DbSession, user_id: uuid.UUID, content_id: uuid.UUID
) -> FavoriteState:
    """收藏：重复调用不报错也不重复计数（复合主键兜并发）。"""
    # 幂等早退也要给「即时读数」：用纯 DB 快照会与真正变更分支返回的口径不一致
    count = await _current_bookmark_count(db, content_id)
    if await _is_favorited(db, user_id, content_id):
        return FavoriteState(
            content_id=content_id, favorited=True, bookmark_count=count
        )

    # 并发下同一 (content_id, user_id) 撞复合主键由 ON CONFLICT DO NOTHING 吸收
    # （不产生异常，无需 savepoint）：未真正插入即视为「已收藏」幂等返回——此时计数
    # 未增，故直接返回原值而非再 bump。
    inserted = await InteractionFavoriteRepository(db).add_if_absent(
        user_id=user_id, content_id=content_id
    )
    if not inserted:
        return FavoriteState(
            content_id=content_id, favorited=True, bookmark_count=count
        )

    new_count = await _bump_bookmark(db, content_id, 1)
    return FavoriteState(
        content_id=content_id, favorited=True, bookmark_count=new_count
    )


async def remove_favorite(
    db: DbSession, user_id: uuid.UUID, content_id: uuid.UUID
) -> FavoriteState:
    """取消收藏：未收藏时幂等返回当前计数，不递减。"""
    count = await _current_bookmark_count(db, content_id)
    removed = await InteractionFavoriteRepository(db).remove(
        user_id=user_id, content_id=content_id
    )
    if removed == 0:
        return FavoriteState(
            content_id=content_id, favorited=False, bookmark_count=count
        )
    new_count = await _bump_bookmark(db, content_id, -1)
    return FavoriteState(
        content_id=content_id, favorited=False, bookmark_count=new_count
    )


async def list_favorites(
    db: DbSession, user_id: uuid.UUID, page: int = 1, limit: int = 20
) -> PageData[FavoriteItem]:
    repo = InteractionFavoriteRepository(db)
    total = await repo.count_for_user(user_id)
    rows = await repo.list_page(
        user_id, offset=paginate_offset(page, limit), limit=limit
    )
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


async def record_view(
    db: DbSession, user_id: uuid.UUID, content_id: uuid.UUID
) -> ViewState:
    """浏览上报：同内容重复调用幂等（只刷新 viewed_at）。"""
    if not await InteractionContentItemRepository(db).exists_content(content_id):
        raise BizError(InteractionErr.CONTENT_NOT_FOUND)

    viewed_at = datetime.datetime.now(datetime.UTC)
    await InteractionViewLogRepository(db).upsert_view(
        user_id=user_id, content_id=content_id, viewed_at=viewed_at
    )
    return ViewState(content_id=content_id, viewed_at=viewed_at)


async def list_history(
    db: DbSession, user_id: uuid.UUID, page: int = 1, limit: int = 20
) -> PageData[HistoryItem]:
    repo = InteractionViewLogRepository(db)
    total = await repo.count_for_user(user_id)
    rows = await repo.list_page(
        user_id, offset=paginate_offset(page, limit), limit=limit
    )
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


async def purge_stale_view_logs(db: DbSession, retention_days: int) -> int:
    """保留策略：删除超过保留期的浏览记录，返回删除行数（cron 调用）。"""
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        days=retention_days
    )
    return await InteractionViewLogRepository(db).purge_before(cutoff)
