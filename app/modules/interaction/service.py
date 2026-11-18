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

from app.modules.interaction.errors import FollowErr, InteractionErr
from app.modules.interaction.repository import (
    NOTIFY_LEVELS,
    TARGET_TYPES,
    BoardReadRepository,
    ContentSubscriptionRepository,
    InteractionContentItemRepository,
    InteractionFavoriteRepository,
    InteractionViewLogRepository,
    PostWatchRepository,
    UserFollowRepository,
    UserRelationshipRepository,
    normalize_target,
)
from app.modules.interaction.schemas import (
    FavoriteItem,
    FavoriteState,
    HistoryItem,
    SubscriptionItem,
    ViewState,
    WatchItem,
)
from core import counters
from core.cache import (
    TTL_ITEM_S,
    cache_invalidate,
    cached_read,
    make_key,
)
from core.common import PageData, paginate_offset, paginate_pages
from core.config import settings
from core.db.repository import DbSession
from core.err import BizError
from core.ports import columns as columns_port
from core.ports import content as content_port
from core.ports import feed as feed_port
from core.ports.snapshot import get_user_snapshot, get_user_snapshot_batch


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
    # 收藏即自动 watch（后续评论/点赞会通知该用户）；已有 watch 的提醒偏好保持不变。
    await PostWatchRepository(db).watch(user_id, content_id)
    if await _is_favorited(db, user_id, content_id):
        return FavoriteState(
            content_id=content_id, favorited=True, bookmark_count=count
        )

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


async def get_content_viewer_state(
    db: DbSession, user_id: uuid.UUID | None, content_id: uuid.UUID
) -> tuple[bool, bool, int, int]:
    """互动态 ``(liked, favorited, like_count, bookmark_count)``；``user_id`` 可空。

    详情页按钮的初值用它回填：SSR 阶段拿不到登录态（后端 ``get_optional_user`` 只认
    ``Authorization`` 头，而 SSR 只转发 Cookie），故由组件挂载后在客户端补拉。
    匿名（``user_id is None``）时回计数、点赞/收藏恒 false。
    内容不存在（含已软删）→ 404，避免读到一个「空状态」把按钮画成未赞/未藏。
    """
    state = await InteractionContentItemRepository(db).get_viewer_state(
        content_id=content_id, user_id=user_id
    )
    if state is None:
        raise BizError(InteractionErr.CONTENT_NOT_FOUND)
    return state


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


async def list_likes(
    db: DbSession, user_id: uuid.UUID, page: int = 1, limit: int = 20
) -> PageData[FavoriteItem]:
    """我赞过的内容：复用收藏列表的形状，``created_at`` 是点赞时间。"""
    rows, total = await content_port.list_liked_items(
        db, user_id, offset=paginate_offset(page, limit), limit=limit
    )
    return PageData(
        items=[FavoriteItem(**row) for row in rows],
        total=total,
        page=page,
        pages=paginate_pages(total, limit),
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
    if await is_blocked_either_direction(db, follower_id, following_id):
        raise BizError(InteractionErr.BLOCKED)

    created = await UserFollowRepository(db).follow(follower_id, following_id)
    if created and settings.feed_backfill_limit > 0:
        await feed_port.backfill_author(
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
        keep = set(await get_followed_board_ids(db, follower_id))
        await feed_port.remove_author_items(db, follower_id, following_id, keep)
        await _invalidate_follow_cache(follower_id)


async def _validate_subscription_target(
    db: DbSession, target_type: str, raw_target_id: str
) -> str:
    """校验订阅目标并返回归一后的 ``target_id``；非法/不存在分别抛 400/404。"""
    if target_type not in TARGET_TYPES:
        raise BizError(InteractionErr.INVALID_TARGET_TYPE)
    target_type, target_id = normalize_target(target_type, raw_target_id)
    if not target_id or len(target_id) > 64:
        raise BizError(InteractionErr.INVALID_TARGET_TYPE)

    if target_type in ("board", "column"):
        try:
            parsed = uuid.UUID(target_id)
        except ValueError:
            raise BizError(InteractionErr.INVALID_TARGET_TYPE) from None
        if target_type == "board":
            exists = await BoardReadRepository(db).get(parsed) is not None
        else:
            exists = await columns_port.column_exists(db, parsed)
        if not exists:
            raise BizError(InteractionErr.SUBSCRIPTION_TARGET_NOT_FOUND)
    return target_id


async def subscribe_content(
    db: DbSession,
    user_id: uuid.UUID,
    target_type: str,
    target_id: str,
    notify_level: str = "all",
) -> bool:
    """订阅内容目标（板块/标签/专栏，幂等）：返回是否「新增/复活」。

    - tag 目标 trim + 小写归一后落库；
    - 板块订阅与板内关注同源：新订阅仍回填最近帖子到物化 feed。
    """
    if notify_level not in NOTIFY_LEVELS:
        raise BizError(InteractionErr.INVALID_TARGET_TYPE, "非法提醒等级")
    normalized = await _validate_subscription_target(db, target_type, target_id)
    created = await ContentSubscriptionRepository(db).subscribe(
        user_id, target_type, normalized, notify_level
    )
    if created and target_type == "board" and settings.feed_backfill_limit > 0:
        await feed_port.backfill_board(
            db, user_id, uuid.UUID(normalized), settings.feed_backfill_limit
        )
    if target_type == "board":
        await _invalidate_follow_cache(user_id)
    return created


async def unsubscribe_content(
    db: DbSession, user_id: uuid.UUID, target_type: str, target_id: str
) -> bool:
    """取消订阅（幂等）：返回是否真的发生变更；取消立即从 fanout 受众面消失。"""
    if target_type not in TARGET_TYPES:
        raise BizError(InteractionErr.INVALID_TARGET_TYPE)
    _, normalized = normalize_target(target_type, target_id)
    changed = await ContentSubscriptionRepository(db).unsubscribe(
        user_id, target_type, normalized
    )
    if changed and target_type == "board":
        keep = set(await get_following_ids(db, user_id))
        await feed_port.remove_board_items(
            db, user_id, uuid.UUID(normalized), keep
        )
        await _invalidate_follow_cache(user_id)
    return changed


async def list_subscriptions(
    db: DbSession, user_id: uuid.UUID
) -> list[SubscriptionItem]:
    """我的活动订阅列表（新订阅在前）。"""
    rows = await ContentSubscriptionRepository(db).list_active(user_id)
    return [
        SubscriptionItem(
            target_type=r.target_type,
            target_id=r.target_id,
            notify_level=r.notify_level,
            created_at=r.created_at,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 单帖 watch（收藏/评论自动跟进；后续评论、点赞通知 watcher）
# ---------------------------------------------------------------------------


async def watch_post(
    db: DbSession,
    user_id: uuid.UUID,
    content_id: uuid.UUID,
    notify_level: str | None = None,
) -> None:
    """watch 内容（幂等）：内容不存在 → 404。``notify_level=None`` 保留既有偏好。"""
    if notify_level is not None and notify_level not in NOTIFY_LEVELS:
        raise BizError(InteractionErr.INVALID_TARGET_TYPE, "非法提醒等级")
    if not await InteractionContentItemRepository(db).exists_content(content_id):
        raise BizError(InteractionErr.CONTENT_NOT_FOUND)
    await PostWatchRepository(db).watch(user_id, content_id, notify_level)


async def unwatch_post(
    db: DbSession, user_id: uuid.UUID, content_id: uuid.UUID
) -> None:
    """取消 watch（幂等；内容已删也允许清理）。"""
    await PostWatchRepository(db).unwatch(user_id, content_id)


async def list_watches(
    db: DbSession, user_id: uuid.UUID, page: int = 1, limit: int = 20
) -> PageData[WatchItem]:
    """「我的 watch」分页（join 内容摘要）。"""
    repo = PostWatchRepository(db)
    total = await repo.count_for_user(user_id)
    rows = await repo.list_page(
        user_id, offset=paginate_offset(page, limit), limit=limit
    )
    items = [
        WatchItem(
            content_id=r.content_id,
            content_type=r.content_type,
            title=r.title,
            slug=r.slug,
            board_id=r.board_id,
            notify_level=r.notify_level,
            created_at=r.created_at,
        )
        for r in rows
    ]
    return PageData(
        items=items, total=total, page=page, pages=paginate_pages(total, limit)
    )


async def follow_board(
    db: DbSession, follower_id: uuid.UUID, board_id: uuid.UUID
) -> None:
    """follower 关注版块（幂等）——URL 保留，实现为统一订阅 ``target_type='board'``。"""
    await subscribe_content(db, follower_id, "board", str(board_id))


async def unfollow_board(
    db: DbSession, follower_id: uuid.UUID, board_id: uuid.UUID
) -> None:
    """follower 取消关注版块（幂等）。"""
    await unsubscribe_content(db, follower_id, "board", str(board_id))


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
    """我订阅的所有版块 id（缓存，供时间线过滤）。

    版块关注已统一进 ``content_subscriptions``；缓存里存 str、读回转 UUID 的口径同
    :func:`get_following_ids`。
    """

    async def load() -> list[str]:
        ids = await ContentSubscriptionRepository(db).list_target_ids(user_id, "board")
        return list(ids)

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


async def list_bigv_author_ids(db: DbSession, *, threshold: int) -> list[uuid.UUID]:
    """关注者数超过 ``threshold`` 的作者 id（feed fanout 的大 V 判定，读时现算）。

    供 feed 读路径的实时补拉使用 —— 原先读的是一份 Redis SET 标记，关闭 Redis 持久化后
    该标记不可靠（重启即空且不会自动重建），故改由本表重放判据。
    """
    return await UserFollowRepository(db).list_bigv_author_ids(threshold)


async def list_board_follower_ids(
    db: DbSession, board_id: uuid.UUID, *, limit: int | None = None
) -> list[uuid.UUID]:
    """订阅了某版块的用户 id（fanout 受众面，不经缓存）。"""
    return await ContentSubscriptionRepository(db).list_user_ids(
        [("board", str(board_id))], limit=limit
    )


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


# ---------------------------------------------------------------------------
# Block / Mute（账号级用户关系）
#
# block 存**单向行**但语义**双向**：任一方向存在 block，双方都互相屏蔽；判定一律走
# ``is_blocked_either_direction`` / ``get_blocked_ids``（后者合并两个方向）。
# mute 是单向可见性隐藏：只有发起方看不到对方，对方仍可正常互动。
#
# **树洞不接入账号 block**：树洞内容是匿名身份（``treehole_*`` 表用匿名代号），没有可
# 对应的账号作者，故本节的 block/mute 过滤不下沉到树洞域。
# ---------------------------------------------------------------------------


async def _assert_relation_target(
    db: DbSession, user_id: uuid.UUID, target_id: uuid.UUID
) -> None:
    """校验 block/mute 目标：不能是自己，且目标用户必须存在。"""
    if user_id == target_id:
        raise BizError(InteractionErr.CANNOT_RELATE_SELF)
    if await get_user_snapshot(db, user_id=target_id) is None:
        raise BizError(InteractionErr.RELATION_TARGET_NOT_FOUND)


async def block_user(
    db: DbSession, user_id: uuid.UUID, target_id: uuid.UUID
) -> None:
    """屏蔽用户（幂等）：落 block 行，并**双向**软删双方关注关系。

    - 任一方向的 block 都让对方互相不可见（语义双向、存储单向）；
    - block 不删除历史点赞/评论（与参考语义一致），只阻止新的互动；
    - unblock 不自动恢复关注。
    """
    await _assert_relation_target(db, user_id, target_id)
    await UserRelationshipRepository(db).add_relation(user_id, target_id, "block")

    follows = UserFollowRepository(db)
    await follows.unfollow(user_id, target_id)
    await follows.unfollow(target_id, user_id)
    await _invalidate_follow_cache(user_id)
    await _invalidate_follow_cache(target_id)


async def unblock_user(
    db: DbSession, user_id: uuid.UUID, target_id: uuid.UUID
) -> None:
    """取消屏蔽（幂等）：只删本方向的 block 行，不恢复关注。"""
    await UserRelationshipRepository(db).remove(user_id, target_id, "block")


async def mute_user(
    db: DbSession, user_id: uuid.UUID, target_id: uuid.UUID
) -> None:
    """静音用户（幂等，单向）；不限制对方互动。"""
    await _assert_relation_target(db, user_id, target_id)
    await UserRelationshipRepository(db).add_relation(user_id, target_id, "mute")


async def unmute_user(
    db: DbSession, user_id: uuid.UUID, target_id: uuid.UUID
) -> None:
    """取消静音（幂等）。"""
    await UserRelationshipRepository(db).remove(user_id, target_id, "mute")


async def list_blocked_ids(db: DbSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """「我屏蔽了谁」——单向列表（不含屏蔽了我的人）。"""
    return await UserRelationshipRepository(db).list_target_ids(user_id, "block")


async def list_muted_ids(db: DbSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """「我静音了谁」——单向列表。"""
    return await UserRelationshipRepository(db).list_target_ids(user_id, "mute")


async def get_blocked_ids(db: DbSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """与 ``user_id`` 存在 block 的全部对方 id（**双向合并**，用于过滤）。"""
    return await UserRelationshipRepository(db).list_blocked_either_direction(user_id)


async def get_muted_ids(db: DbSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """``user_id`` 静音的目标 id（单向，用于过滤）。"""
    return await list_muted_ids(db, user_id)


async def get_hidden_author_ids(
    db: DbSession, user_id: uuid.UUID
) -> list[uuid.UUID]:
    """时间线/检索/通知过滤用的隐藏作者集合：block（双向）∪ mute（单向）。"""
    blocked = set(await get_blocked_ids(db, user_id))
    blocked.update(await get_muted_ids(db, user_id))
    return list(blocked)


async def is_blocked_either_direction(
    db: DbSession, user_id: uuid.UUID, other_id: uuid.UUID
) -> bool:
    """任一方向存在 block 即为 ``True``。"""
    return await UserRelationshipRepository(db).is_blocked_either_direction(
        user_id, other_id
    )


async def relationship_state(
    db: DbSession, user_id: uuid.UUID, target_id: uuid.UUID
) -> tuple[bool, bool, bool]:
    """``(blocked, blocked_by, muted)``：我屏蔽对方 / 对方屏蔽我 / 我静音对方。"""
    repo = UserRelationshipRepository(db)
    blocked = await repo.relation_exists(user_id, target_id, "block")
    blocked_by = await repo.relation_exists(target_id, user_id, "block")
    muted = await repo.relation_exists(user_id, target_id, "mute")
    return blocked, blocked_by, muted


async def _list_relationship_users(
    db: DbSession, user_id: uuid.UUID, kind: str
) -> list[tuple[uuid.UUID, str, str | None]]:
    ids = await UserRelationshipRepository(db).list_target_ids(user_id, kind)
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


async def list_blocked_users(
    db: DbSession, user_id: uuid.UUID
) -> list[tuple[uuid.UUID, str, str | None]]:
    """我屏蔽的用户列表：(user_id, display_name, avatar)。"""
    return await _list_relationship_users(db, user_id, "block")


async def list_muted_users(
    db: DbSession, user_id: uuid.UUID
) -> list[tuple[uuid.UUID, str, str | None]]:
    """我静音的用户列表：(user_id, display_name, avatar)。"""
    return await _list_relationship_users(db, user_id, "mute")
