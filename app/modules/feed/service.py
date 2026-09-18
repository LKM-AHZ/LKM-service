"""信息流(feed)域服务：关注关系(follow) 写/查 + 时间线(read-time) 合流读。

M2.3 合并 incoming follow/service.py（关注写 + 关注集合缓存，feed 过滤的数据源坐标）与
timeline/service.py（时间线合流：follow 过滤 × hot 全站 × 游标分页）。两类函数名不冲突，
语义独立合居此命名空间；关注集合读（get_following_ids/get_followed_board_ids）与时间线
按关注过滤天然同域协作。

--- 关注关系（follow 原 service） ---
幂等实现走「软删墓碑」：follow 时将已有行 ``deleted_at`` 置 NULL（若存在；否则新插入）；
unfollow 仅置 ``deleted_at``，不删行。配合 ``(follower_id, following_id)`` 唯一约束保证不产生
第二行活动关注。「我关注了谁」的 id 集合被时间线高频读取 → 短 TTL 缓存；follow/unfollow 写路径
显式失效。

--- 时间线合流（timeline 原 service） ---
合流策略（对齐 Solar 参考）：**查询时合流**，非写入 fan-out——每次请求实时从各内容源按
(created_at, id) 游标各取一页，合并后过滤审校隐藏项，按（关注加权 + 审校排除后的）时间倒序返回。
审校：命中 hide 的条目在合流前剔除；命中 derank 的压低 ``sort_score`` 字段值
（v1 主序仍为时间倒序，derank 反映到排序分供后续热度排序使用，且 hide 即时生效）。
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import (
    TTL_ITEM_S,
    TTL_LIST_S,
    cache_invalidate,
    cached_read,
    make_key,
)
from app.core.config import settings
from app.core.err import BizError
from app.db.base import now_iso
from app.modules.admin.moderation.engine import evaluate, load_active_rules
from app.modules.auth.snapshot import get_user_snapshot, get_user_snapshot_batch
from app.modules.content.models import Board
from app.modules.feed import fanout
from app.modules.feed import feed as feed_src
from app.modules.feed.errors import FollowErr
from app.modules.feed.models import (
    BoardFollow,
    FeedItemMaterialized,
    UserFollow,
)
from app.modules.feed.schemas import FeedItem, FeedResponse


def _following_key(user_id: uuid.UUID) -> str:
    return make_key("follow", "following", user_id)


def _board_ids_key(user_id: uuid.UUID) -> str:
    return make_key("follow", "boards", user_id)


async def _invalidate_follow_cache(user_id: uuid.UUID) -> None:
    """关注集合缓存显式失效（follow/unfollow 低频但需即时）。"""
    await cache_invalidate(_following_key(user_id), _board_ids_key(user_id))


async def follow_user(
    db: AsyncSession, follower_id: uuid.UUID, following_id: uuid.UUID
) -> None:
    """follower 关注 following（幂等：重复关注静默成功）。"""
    if follower_id == following_id:
        raise BizError(FollowErr.CANNOT_FOLLOW_SELF, "不能关注自己")
    # 关注目标身份存在性走 auth 快照缝（business 不直读 auth.users）。
    target_snap = await get_user_snapshot(db, user_id=following_id)
    if target_snap is None:
        raise BizError(FollowErr.TARGET_NOT_FOUND, "关注目标用户不存在")

    row = await db.scalar(
        select(UserFollow).where(
            UserFollow.follower_id == follower_id,
            UserFollow.following_id == following_id,
        )
    )
    created = row is None or row.deleted_at is not None
    if row is None:
        db.add(UserFollow(follower_id=follower_id, following_id=following_id))
    elif row.deleted_at is not None:
        row.deleted_at = None
    await db.flush()
    if created and settings.feed_backfill_limit > 0:
        # M6.11：新关注即回填该作者最近内容，令物化 feed 当场可用（否则要等新内容 fanout）
        await fanout.backfill_author(
            db, follower_id, following_id, settings.feed_backfill_limit
        )
    await _invalidate_follow_cache(follower_id)


async def unfollow_user(
    db: AsyncSession, follower_id: uuid.UUID, following_id: uuid.UUID
) -> None:
    """follower 取消关注 following（幂等：末关注时静默成功）。"""
    if follower_id == following_id:
        raise BizError(FollowErr.CANNOT_FOLLOW_SELF, "不能操作自己的关注")
    row = await db.scalar(
        select(UserFollow).where(
            UserFollow.follower_id == follower_id,
            UserFollow.following_id == following_id,
        )
    )
    if row is not None and row.deleted_at is None:
        row.deleted_at = now_iso()
        await db.flush()
        # M6.11：取关即清掉该作者的物化条目（否则已取关内容仍留在 feed 里）
        await fanout.remove_author_items(db, follower_id, following_id)
        await _invalidate_follow_cache(follower_id)


async def follow_board(
    db: AsyncSession, follower_id: uuid.UUID, board_id: uuid.UUID
) -> None:
    """follower 关注版块（幂等）。"""
    target = await db.get(Board, board_id)
    if target is None:
        raise BizError(FollowErr.TARGET_NOT_FOUND, "关注版块不存在")

    row = await db.scalar(
        select(BoardFollow).where(
            BoardFollow.follower_id == follower_id,
            BoardFollow.board_id == board_id,
        )
    )
    created = row is None or row.deleted_at is not None
    if row is None:
        db.add(BoardFollow(follower_id=follower_id, board_id=board_id))
    elif row.deleted_at is not None:
        row.deleted_at = None
    await db.flush()
    if created and settings.feed_backfill_limit > 0:
        # M6.11：新关注版块即回填该版块最近讨论帖
        await fanout.backfill_board(
            db, follower_id, board_id, settings.feed_backfill_limit
        )
    await _invalidate_follow_cache(follower_id)


async def unfollow_board(
    db: AsyncSession, follower_id: uuid.UUID, board_id: uuid.UUID
) -> None:
    """follower 取消关注版块（幂等）。"""
    row = await db.scalar(
        select(BoardFollow).where(
            BoardFollow.follower_id == follower_id,
            BoardFollow.board_id == board_id,
        )
    )
    if row is not None and row.deleted_at is None:
        row.deleted_at = now_iso()
        await db.flush()
        # M6.11：取关版块即清理其物化条目（保留仍因作者关注而可见的行）
        keep = set(await get_following_ids(db, follower_id))
        await fanout.remove_board_items(db, follower_id, board_id, keep)
        await _invalidate_follow_cache(follower_id)


async def get_following_ids(db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """我关注的所有用户 id（缓存，供时间线过滤）。"""

    async def load() -> list[uuid.UUID]:
        rows = (
            (
                await db.execute(
                    select(UserFollow.following_id).where(
                        UserFollow.follower_id == user_id,
                        UserFollow.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    return await cached_read(_following_key(user_id), TTL_ITEM_S, load)


async def get_followed_board_ids(
    db: AsyncSession, user_id: uuid.UUID
) -> list[uuid.UUID]:
    """我关注的所有版块 id（缓存，供时间线过滤）。"""

    async def load() -> list[uuid.UUID]:
        rows = (
            (
                await db.execute(
                    select(BoardFollow.board_id).where(
                        BoardFollow.follower_id == user_id,
                        BoardFollow.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    return await cached_read(_board_ids_key(user_id), TTL_ITEM_S, load)


async def is_following_user(
    db: AsyncSession, follower_id: uuid.UUID, following_id: uuid.UUID
) -> bool:
    """follower 当前是否关注 following（软删过滤）。"""
    row = await db.scalar(
        select(UserFollow.id).where(
            UserFollow.follower_id == follower_id,
            UserFollow.following_id == following_id,
            UserFollow.deleted_at.is_(None),
        )
    )
    return row is not None


async def list_following_users(
    db: AsyncSession, user_id: uuid.UUID
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
    db: AsyncSession, user_id: uuid.UUID
) -> list[tuple[uuid.UUID, str]]:
    """我关注的版块列表：(board_id, title)。"""
    ids = await get_followed_board_ids(db, user_id)
    if not ids:
        return []
    rows = (
        await db.execute(select(Board.id, Board.title).where(Board.id.in_(ids)))
    ).all()
    title_by_id = {bid: title for bid, title in rows}
    return [(bid, title_by_id.get(bid, "")) for bid in ids]


async def _fill_authors(db: AsyncSession, items: list[FeedItem]) -> None:
    """把各源返回的 ``author_id`` 去重后批量查询一次并回填 ``author_name``。

    feed 各源不再各自查作者（除 blog 保留 publisher 兜底），避免同一作者在多源被
    重复 IN 查询。仅回填 ``author_name`` 仍为空者（blog 已填充/兜底的不触碰）。
    解析语义与 feed 一致：优先 profile.nickname，否则 username。
    """
    author_ids = {it.author_id for it in items if it.author_id and not it.author_name}
    if not author_ids:
        return
    snaps = await get_user_snapshot_batch(db, user_ids=list(author_ids))
    name_of: dict[uuid.UUID, str] = {uid: s.display_name for uid, s in snaps.items()}
    for it in items:
        if it.author_id is not None and it.author_id in name_of and not it.author_name:
            it.author_name = name_of[it.author_id]


def _encode_cursor(created_at: datetime.datetime, item_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{item_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(
    cursor: str | None,
) -> tuple[datetime.datetime | None, uuid.UUID | None]:
    if not cursor:
        return None, None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        time_s, id_s = raw.rsplit("|", 1)
        return datetime.datetime.fromisoformat(time_s), uuid.UUID(id_s)
    except (ValueError, UnicodeDecodeError):
        return None, None


def _recency_multiplier(created_at: datetime.datetime, now: datetime.datetime) -> float:
    age_hours = max(0.0, (now - created_at).total_seconds() / 3600)
    return (age_hours + 2.0) ** -1.2


async def _compute_scores(
    items: list[FeedItem],
    following_ids: set[uuid.UUID] | None,
    rules: list[Any],
) -> list[FeedItem]:
    now = datetime.datetime.now(datetime.UTC)
    for it in items:
        recency = (
            1.0
            if it.created_at.tzinfo is None
            else _recency_multiplier(it.created_at, now)
        )
        follow_bonus = 0.0
        if following_ids is not None and it.author_id in following_ids:
            follow_bonus = 5.0
        # 审校：hide 已在上游剔除；这里只处理 derank 扣分
        text = f"{it.title} {it.content_preview}"
        mod = evaluate(text, rules)
        penalty = 0.0 if mod.should_hide else mod.penalty
        # 时间基分(recency*1000)保证 0 热度内容也有>0基分，使 derank 扣分可分辨；
        # 关注权重(follow_bonus)加在前面、不被审校削减。
        base = it.sort_score * 500 + recency * 1000
        it.sort_score = base * (1.0 - penalty) + follow_bonus
    return items


async def get_timeline(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    mode: str,
    cursor: str | None,
    limit: int,
) -> FeedResponse:
    """时间线读入口：物化读模型优先，未命中回退实时多源合流（M6.11）。

    只有**登录用户的 follow 流**有物化意义（hot 流是个性化无关的全站榜，沿用实时）。
    物化的两条来源：``feed_items``（fanout 写入）+ 大 V 作者的实时补拉；两者都为空时
    返回 ``None`` → 兜底实时合流（覆盖「刚关注/物化未回填」的用户）。
    """
    if mode == "follow" and user_id is not None:
        materialized = await _materialized_timeline(
            db, user_id=user_id, cursor=cursor, limit=limit
        )
        if materialized is not None:
            return materialized
    return await _realtime_timeline(
        db, user_id=user_id, mode=mode, cursor=cursor, limit=limit
    )


def _materialized_key(user_id: uuid.UUID, cursor: str | None) -> str:
    return make_key("feed", user_id, cursor or "")


async def _materialized_timeline(
    db: AsyncSession, *, user_id: uuid.UUID, cursor: str | None, limit: int
) -> FeedResponse | None:
    """物化读：feed_items + 大 V 实时补拉。返回 ``None`` 表示应回退实时合流。"""
    before_time, before_id = _decode_cursor(cursor)
    following_ids = set(await get_following_ids(db, user_id))
    board_ids = set(await get_followed_board_ids(db, user_id))
    if not following_ids and not board_ids:
        return FeedResponse(items=[], next_cursor=None)

    bigv = (await fanout.bigv_authors()) & following_ids

    async def _load() -> dict[str, Any]:
        # 多取一条以判定「是否还有下一页」（两路各自 +1，合并后仍能判出）
        items = await _load_materialized_page(
            db, user_id, before_time, before_id, limit + 1
        )
        if bigv:
            items += await _realtime_for_authors(
                db, bigv, board_ids, before_time, before_id, limit + 1
            )
        if not items:
            return {}

        await _fill_authors(db, items)
        rules = await load_active_rules(db)
        kept = [
            it
            for it in items
            if not evaluate(f"{it.title} {it.content_preview}", rules).should_hide
        ]
        await _compute_scores(kept, following_ids, rules)
        kept.sort(key=lambda it: (it.created_at, it.id), reverse=True)
        page = kept[:limit]
        next_cursor: str | None = None
        if len(kept) > limit and page:
            last = page[-1]
            next_cursor = _encode_cursor(last.created_at, last.id)
        return FeedResponse(items=page, next_cursor=next_cursor).model_dump(
            mode="json"
        )

    cached = await cached_read(
        _materialized_key(user_id, cursor), TTL_LIST_S, _load
    )
    if not cached:
        return None
    return FeedResponse.model_validate(cached)


async def _load_materialized_page(
    db: AsyncSession,
    user_id: uuid.UUID,
    before_time: datetime.datetime | None,
    before_id: uuid.UUID | None,
    limit: int,
) -> list[FeedItem]:
    """从物化表取一页（(created_at, id) 游标下滤，时间倒序）。"""
    conds: list[Any] = [FeedItemMaterialized.user_id == user_id]
    if before_time is not None:
        conds.extend(
            feed_src._before_conds(
                FeedItemMaterialized.created_at,
                FeedItemMaterialized.id,
                before_time,
                before_id,
            )
        )
    rows = (
        (
            await db.execute(
                select(FeedItemMaterialized)
                .where(*conds)
                .order_by(
                    FeedItemMaterialized.created_at.desc(),
                    FeedItemMaterialized.id.desc(),
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        FeedItem(
            item_type=r.item_type,
            id=r.source_id,
            author_id=r.author_id,
            author_name="",
            title=r.title,
            content_preview=r.content_preview,
            created_at=r.created_at,
            sort_score=r.sort_score,
            board_id=r.board_id,
            url=r.url,
        )
        for r in rows
    ]


async def _realtime_for_authors(
    db: AsyncSession,
    author_ids: set[uuid.UUID],
    board_ids: set[uuid.UUID],
    before_time: datetime.datetime | None,
    before_id: uuid.UUID | None,
    limit: int,
) -> list[FeedItem]:
    """大 V 补拉：只对这些作者走实时源（与 follow 模式同一过滤语义）。"""

    async def _fetch_one(name: str) -> list[FeedItem]:
        fetch = feed_src.SOURCES[name]
        b_ids = board_ids if name == "discussion" else None
        return await fetch(db, author_ids, b_ids, before_time, before_id, limit)

    groups = await asyncio.gather(*(_fetch_one(n) for n in feed_src.FOLLOW_SOURCES))
    return [it for group in groups for it in group]


async def _realtime_timeline(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    mode: str,
    cursor: str | None,
    limit: int,
) -> FeedResponse:
    """实时多源合流（原实现）：物化未命中时的兜底读路径。"""
    before_time, before_id = _decode_cursor(cursor)

    following_ids: set[uuid.UUID] | None = None
    board_ids: set[uuid.UUID] | None = None
    if mode == "follow":
        if user_id is None:
            mode = "hot"  # 匿名只能看全站热门
        else:
            following_ids = set(await get_following_ids(db, user_id))
            board_ids = set(await get_followed_board_ids(db, user_id))
            # 空关注 → 返回空流
            if not following_ids and not board_ids:
                return FeedResponse(items=[], next_cursor=None)

    # 选源：follow 用 FOLLOW_SOURCES（article 无作者外键不进个性化），hot 全含
    source_names = feed_src.FOLLOW_SOURCES if mode == "follow" else feed_src.HOT_SOURCES

    # 各内容源互不依赖，gather 并行拉取，而非串行 await（时间线多源往返叠加）。
    async def _fetch_one(name: str) -> list[FeedItem]:
        fetch = feed_src.SOURCES[name]
        if mode == "follow":
            # discussion 额外按关注版块过滤；其余按关注作者过滤
            b_ids = board_ids if name == "discussion" else None
            a_ids = following_ids
        else:
            a_ids, b_ids = None, None
        return await fetch(db, a_ids, b_ids, before_time, before_id, limit)

    fetched: list[list[FeedItem]] = await asyncio.gather(
        *(_fetch_one(n) for n in source_names)
    )
    candidates: list[FeedItem] = [it for group in fetched for it in group]

    # 合并回填作者名：各源只返回 author_id，此处一次性批量查询（抵消每源各查一次）
    await _fill_authors(db, candidates)

    # 审校隐藏剔除 + 排序分计算
    rules = await load_active_rules(db)
    kept: list[FeedItem] = []
    for it in candidates:
        text = f"{it.title} {it.content_preview}"
        mod = evaluate(text, rules)
        if mod.should_hide:
            continue
        kept.append(it)
    await _compute_scores(kept, following_ids, rules)

    # 主序：时间倒序（稳定性靠 id 倒序兜底）
    kept.sort(key=lambda it: (it.created_at, it.id), reverse=True)
    page = kept[:limit]

    next_cursor: str | None = None
    if page and not (len(kept) <= limit):
        last = page[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)

    return FeedResponse(items=page, next_cursor=next_cursor)
