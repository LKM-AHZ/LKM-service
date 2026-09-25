"""信息流(feed)域服务：时间线(read-time) 合流读。

**关注关系不在此域**：``UserFollow``/``BoardFollow`` 与其写/查服务已按蓝图 §7.2 目标形态
迁入 interaction（信息流域只保留「时间线生成」）。本域只**消费**关注关系——经
``interaction.service`` 的公开读口取「我关注了谁 / 我关注了哪些版块」用于过滤，不直接触达
那两张表，也不缓存它们（缓存归 interaction 域所有）。

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

from app.core.cache import TTL_LIST_S, cached_read, make_key
from app.db.repository import DbSession
from app.modules.admin.moderation.engine import (
    ModerationResult,
    evaluate,
    load_active_rules,
)
from app.modules.feed import fanout
from app.modules.feed import feed as feed_src
from app.modules.feed.repository import FeedItemMaterializedRepository
from app.modules.feed.schemas import FeedItem, FeedResponse
from app.modules.interaction.service import (
    get_followed_board_ids,
    get_following_ids,
)
from auth.snapshot import get_user_snapshot_batch


async def _fill_authors(db: DbSession, items: list[FeedItem]) -> None:
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


def _filter_hidden(
    items: list[FeedItem], rules: list[Any]
) -> tuple[list[FeedItem], dict[tuple[str, uuid.UUID], ModerationResult]]:
    """一次审校：返回 ``(可见项, 每项审校结果)``。

    结果表供打分阶段复用，避免同一条文本在一请求内跑两遍规则匹配（正则/子串都可能很贵）。
    """
    kept: list[FeedItem] = []
    mods: dict[tuple[str, uuid.UUID], ModerationResult] = {}
    for it in items:
        mod = evaluate(f"{it.title} {it.content_preview}", rules)
        mods[(it.item_type, it.id)] = mod
        if not mod.should_hide:
            kept.append(it)
    return kept, mods


async def _compute_scores(
    items: list[FeedItem],
    following_ids: set[uuid.UUID] | None,
    mods: dict[tuple[str, uuid.UUID], ModerationResult],
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
        # 审校：hide 已在上游剔除；这里只取 derank 扣分（结果由 _filter_hidden 一次算好）
        mod = mods.get((it.item_type, it.id))
        penalty = max(0.0, mod.penalty) if mod is not None else 0.0
        # 时间基分(recency*1000)保证 0 热度内容也有>0基分，使 derank 扣分可分辨；
        # 关注权重(follow_bonus)加在前面、不被审校削减。
        base = it.sort_score * 500 + recency * 1000
        it.sort_score = base * (1.0 - penalty) + follow_bonus
    return items


async def get_timeline(
    db: DbSession,
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


def _materialized_key(
    user_id: uuid.UUID,
    before_time: datetime.datetime | None,
    before_id: uuid.UUID | None,
    limit: int,
) -> str:
    """键含 limit 与游标**解码后**的值。

    缓存值是已按 limit 切好的整页（含由该页推出的 ``next_cursor``），故 limit 必须进键，
    否则同一游标下不同 limit 的请求会互相拿到长度不符的页并跳条。游标用解码值而非原始
    字符串：原始游标是客户端可控的任意 base64，进键会为每个畸形串各开一份缓存与 singleflight
    航班（缓存模块按「键基数自然有界」设计），解码值则天然收敛（畸形统一落到首页）。
    """
    return make_key("feed", user_id, before_time or "", before_id or "", limit)


async def _materialized_timeline(
    db: DbSession, *, user_id: uuid.UUID, cursor: str | None, limit: int
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
        kept, mods = _filter_hidden(items, rules)
        if not kept:
            # 本页候选全被审校隐藏：不能缓存空页（客户端会在 TTL_LIST_S 内一直看到空流），
            # 返回 {} 让 get_timeline 回退实时合流去够更老的候选
            return {}
        await _compute_scores(kept, following_ids, mods)
        kept.sort(key=lambda it: (it.created_at, it.id), reverse=True)
        page = kept[:limit]
        next_cursor: str | None = None
        if len(kept) > limit and page:
            last = page[-1]
            next_cursor = _encode_cursor(last.created_at, last.id)
        return FeedResponse(items=page, next_cursor=next_cursor).model_dump(mode="json")

    cached = await cached_read(
        _materialized_key(user_id, before_time, before_id, limit), TTL_LIST_S, _load
    )
    if not cached:
        return None
    return FeedResponse.model_validate(cached)


async def _load_materialized_page(
    db: DbSession,
    user_id: uuid.UUID,
    before_time: datetime.datetime | None,
    before_id: uuid.UUID | None,
    limit: int,
) -> list[FeedItem]:
    """从物化表取一页（(created_at, id) 游标下滤，时间倒序）。"""
    rows = await FeedItemMaterializedRepository(db).list_page(
        user_id, before_time=before_time, before_id=before_id, limit=limit
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
    db: DbSession,
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
    db: DbSession,
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
        # 多取一条：与物化路径同口径，用「是否多出可见项」判断还有没有下一页
        # （否则恰好凑满 limit 时会误判为到底，客户端提前结束）
        return await fetch(db, a_ids, b_ids, before_time, before_id, limit + 1)

    fetched: list[list[FeedItem]] = await asyncio.gather(
        *(_fetch_one(n) for n in source_names)
    )
    candidates: list[FeedItem] = [it for group in fetched for it in group]

    # 合并回填作者名：各源只返回 author_id，此处一次性批量查询（抵消每源各查一次）
    await _fill_authors(db, candidates)

    # 审校隐藏剔除 + 排序分计算（审校只跑一遍，结果传给打分层）
    rules = await load_active_rules(db)
    kept, mods = _filter_hidden(candidates, rules)
    await _compute_scores(kept, following_ids, mods)

    # 主序：时间倒序（稳定性靠 id 倒序兜底）
    kept.sort(key=lambda it: (it.created_at, it.id), reverse=True)
    page = kept[:limit]

    next_cursor: str | None = None
    if page and not (len(kept) <= limit):
        last = page[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)

    return FeedResponse(items=page, next_cursor=next_cursor)
