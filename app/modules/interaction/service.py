"""interaction 业务逻辑：收藏 + 浏览记录 + 关注关系（蓝图 §7.2 目标形态）。

- 收藏写两处：``interaction_favorites`` 明细行 + ``content_items.bookmark_count`` 计数，
  计数用原子 ``UPDATE ... RETURNING`` 而非读改写，避免并发丢计数；行不存在即 404。
- 浏览上报走 ``INSERT ... ON CONFLICT DO UPDATE``：同一 (user_id, content_id) 只保留
  最近一次 ``viewed_at``，重复上报天然幂等，行数上界 = 用户数 × 内容数。
- 列表接口 join ``content_items`` 内联标题等摘要，避免前端逐条回拉（N+1）。

**关注关系**（原 feed 域）：幂等实现走「软删墓碑」——follow 时将已有行 ``deleted_at`` 置
NULL（若存在；否则新插入）；unfollow 仅置 ``deleted_at``，不删行。配合唯一约束保证不产生
第二行活动关注。「我关注了谁」的 id 集合被时间线高频读取 → 短 TTL 缓存；follow/unfollow
写路径显式失效。新关注/取关还会驱动 feed 的物化条目**回填/清理**（信息流是消费方，
经 ``feed.fanout`` 的既有入口，方向 interaction → feed 单向）。

跨模块只读内容/板块表：经 ``interaction.repository`` 收口，已在 pyproject 的
import-linter 契约中精确豁免；不建跨模块 ORM relationship，保持边界单向。
"""

from __future__ import annotations

import datetime
import uuid

from app.core import counters
from app.core.cache import (
    TTL_ITEM_S,
    cache_invalidate,
    cached_read,
    make_key,
)
from app.core.common import PageData, paginate_offset, paginate_pages
from app.core.config import settings
from app.core.err import BizError
from app.db.repository import DbSession
from app.modules.feed import fanout
from app.modules.interaction.errors import FollowErr, InteractionErr
from app.modules.interaction.repository import (
    BoardFollowRepository,
    BoardReadRepository,
    InteractionContentItemRepository,
    InteractionFavoriteRepository,
    InteractionViewLogRepository,
    UserFollowRepository,
)
from app.modules.interaction.schemas import (
    FavoriteItem,
    FavoriteState,
    HistoryItem,
    ViewState,
)
from auth.snapshot import get_user_snapshot, get_user_snapshot_batch


async def _bookmark_count(db: DbSession, content_id: uuid.UUID) -> int:
    """内容不存在 → 404；存在则返回当前收藏计数。"""
    count = await InteractionContentItemRepository(db).get_bookmark_count(content_id)
    if count is None:
        raise BizError(InteractionErr.CONTENT_NOT_FOUND)
    return int(count)


async def _current_bookmark_count(db: DbSession, content_id: uuid.UUID) -> int:
    """即时收藏读数（内容不存在则 404）。

    写穿模式（默认）：DB 计数列即真值。回退模式：DB 值 + 未落库 Redis 增量，是**近似值**
    （读 DB 与读 pending 之间若 flush 恰好 drain 并落库，会取到「旧 base + 已清零 pending」
    而偏小）；两种模式下都夹紧到 >= 0——不夹紧会在增量丢失时给前端负计数。
    """
    base = await _bookmark_count(db, content_id)
    if settings.counters_write_through:
        return max(0, base)
    pending = await counters.pending_delta("bookmark_count", content_id)
    return max(0, base + pending)


async def _bump_bookmark(db: DbSession, content_id: uuid.UUID, delta: int) -> int:
    """增减 ``bookmark_count`` 并返回权威读数（下限 0，行不存在/已软删则 404）。

    写穿（默认）：原子 ``UPDATE ... RETURNING``，与收藏明细同事务。回退：先试 M6.10 的
    Redis 增量链路（收藏明细是真相源、计数由 flush 收敛），Redis 未启用/不可达时落到同一
    原子 UPDATE。
    """
    if not settings.counters_write_through and await counters.bump_counter(
        "bookmark_count", content_id, delta
    ):
        return await _current_bookmark_count(db, content_id)

    new_count = await InteractionContentItemRepository(db).bump_bookmark_count(
        content_id, delta
    )
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


# ---------------------------------------------------------------------------
# 关注关系（原 feed 域的 follow 部分，按蓝图 §7.2 目标形态归入 interaction）
# ---------------------------------------------------------------------------


def _following_key(user_id: uuid.UUID) -> str:
    return make_key("follow", "following", user_id)


def _board_ids_key(user_id: uuid.UUID) -> str:
    return make_key("follow", "boards", user_id)


async def _invalidate_follow_cache(user_id: uuid.UUID) -> None:
    """关注集合缓存显式失效（follow/unfollow 低频但需即时）。"""
    await cache_invalidate(_following_key(user_id), _board_ids_key(user_id))


async def follow_user(
    db: DbSession, follower_id: uuid.UUID, following_id: uuid.UUID
) -> None:
    """follower 关注 following（幂等：重复关注静默成功）。"""
    if follower_id == following_id:
        raise BizError(FollowErr.CANNOT_FOLLOW_SELF, "不能关注自己")
    # 关注目标身份存在性走 auth 快照缝（business 不直读 auth.users）。
    target_snap = await get_user_snapshot(db, user_id=following_id)
    if target_snap is None:
        raise BizError(FollowErr.TARGET_NOT_FOUND, "关注目标用户不存在")

    created = await UserFollowRepository(db).follow(follower_id, following_id)
    if created and settings.feed_backfill_limit > 0:
        # M6.11：新关注即回填该作者最近内容，令物化 feed 当场可用（否则要等新内容 fanout）
        await fanout.backfill_author(
            db, follower_id, following_id, settings.feed_backfill_limit
        )
    await _invalidate_follow_cache(follower_id)


async def unfollow_user(
    db: DbSession, follower_id: uuid.UUID, following_id: uuid.UUID
) -> None:
    """follower 取消关注 following（幂等：末关注时静默成功）。"""
    if follower_id == following_id:
        raise BizError(FollowErr.CANNOT_FOLLOW_SELF, "不能操作自己的关注")
    changed = await UserFollowRepository(db).unfollow(follower_id, following_id)
    if changed:
        # M6.11：取关即清掉该作者的物化条目（否则已取关内容仍留在 feed 里）；
        # 仍在关注的版块所覆盖的行保留（与 unfollow_board 的 keep 语义对称）。
        keep = set(await get_followed_board_ids(db, follower_id))
        await fanout.remove_author_items(db, follower_id, following_id, keep)
        await _invalidate_follow_cache(follower_id)


async def follow_board(
    db: DbSession, follower_id: uuid.UUID, board_id: uuid.UUID
) -> None:
    """follower 关注版块（幂等）。"""
    target = await BoardReadRepository(db).get(board_id)
    if target is None:
        raise BizError(FollowErr.TARGET_NOT_FOUND, "关注版块不存在")

    created = await BoardFollowRepository(db).follow(follower_id, board_id)
    if created and settings.feed_backfill_limit > 0:
        # M6.11：新关注版块即回填该版块最近讨论帖
        await fanout.backfill_board(
            db, follower_id, board_id, settings.feed_backfill_limit
        )
    await _invalidate_follow_cache(follower_id)


async def unfollow_board(
    db: DbSession, follower_id: uuid.UUID, board_id: uuid.UUID
) -> None:
    """follower 取消关注版块（幂等）。"""
    changed = await BoardFollowRepository(db).unfollow(follower_id, board_id)
    if changed:
        # M6.11：取关版块即清理其物化条目（保留仍因作者关注而可见的行）
        keep = set(await get_following_ids(db, follower_id))
        await fanout.remove_board_items(db, follower_id, board_id, keep)
        await _invalidate_follow_cache(follower_id)


async def get_following_ids(db: DbSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """我关注的所有用户 id（缓存，供时间线过滤）。

    缓存层是 ``json.dumps``/``json.loads``：UUID 不可序列化，直接缓存 list[UUID] 会被
    fail-open 静默丢弃（缓存永不生效、每请求回库），命中时还会拿回 list[str]。故缓存里
    存 str、读回再转 UUID（与物化页 model_dump(mode="json") 同思路）。
    """

    async def load() -> list[str]:
        ids = await UserFollowRepository(db).list_following_ids(user_id)
        return [str(i) for i in ids]

    raw = await cached_read(_following_key(user_id), TTL_ITEM_S, load)
    return [uuid.UUID(x) for x in (raw or [])]


async def get_followed_board_ids(db: DbSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """我关注的所有版块 id（缓存，供时间线过滤）。序列化口径同 get_following_ids。"""

    async def load() -> list[str]:
        ids = await BoardFollowRepository(db).list_board_ids(user_id)
        return [str(i) for i in ids]

    raw = await cached_read(_board_ids_key(user_id), TTL_ITEM_S, load)
    return [uuid.UUID(x) for x in (raw or [])]


async def list_follower_ids(
    db: DbSession, following_id: uuid.UUID, *, limit: int | None = None
) -> list[uuid.UUID]:
    """关注了某用户的 follower id（fanout 受众面，**不经缓存**）。

    与 ``get_following_ids`` 方向相反（那是「我关注了谁」，供时间线过滤）；这里是
    「谁关注了我」，供 feed 扩散时枚举受众。刻意不缓存：fanout 只对**新内容**调用一次，
    且写入方刚改过关注关系、缓存反而是陈旧源。
    """
    return await UserFollowRepository(db).list_follower_ids(following_id, limit=limit)


async def list_board_follower_ids(
    db: DbSession, board_id: uuid.UUID, *, limit: int | None = None
) -> list[uuid.UUID]:
    """关注了某版块的 follower id（fanout 受众面，不经缓存）。"""
    return await BoardFollowRepository(db).list_follower_ids(board_id, limit=limit)


async def is_following_user(
    db: DbSession, follower_id: uuid.UUID, following_id: uuid.UUID
) -> bool:
    """follower 当前是否关注 following（软删过滤）。"""
    return await UserFollowRepository(db).is_following(follower_id, following_id)


async def list_following_users(
    db: DbSession, user_id: uuid.UUID
) -> list[tuple[uuid.UUID, str, str | None]]:
    """我关注的用户列表：(user_id, display_name, avatar)。

    display_name 取 ``nickname or username``（沿用 points 榜惯例；缝的 display_name
    同口径），avatar 取快照 avatar。走 id 集合 + 读缝一次批量，避免逐条/跨域 join。
    """
    ids = await get_following_ids(db, user_id)
    if not ids:
        return []
    snaps = await get_user_snapshot_batch(db, user_ids=ids)
    return [
        (
            uid,
            snaps[uid].display_name if uid in snaps else str(uid),
            snaps[uid].avatar if uid in snaps else None,
        )
        for uid in ids
    ]


async def list_followed_boards(
    db: DbSession, user_id: uuid.UUID
) -> list[tuple[uuid.UUID, str]]:
    """我关注的版块列表：(board_id, title)。"""
    ids = await get_followed_board_ids(db, user_id)
    if not ids:
        return []
    title_by_id = await BoardReadRepository(db).title_map(ids)
    return [(bid, title_by_id.get(bid, "")) for bid in ids]
