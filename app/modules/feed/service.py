"""信息流(feed)域服务：时间线(read-time) 合流读。

**关注关系不在此域**：``UserFollow``/``BoardFollow`` 与其写/查服务已按蓝图 §7.2 目标形态
迁入 interaction（信息流域只保留「时间线生成」）。本域只**消费**关注关系——经
``interaction.service`` 的公开读口取「我关注了谁 / 我关注了哪些版块」用于过滤，不直接触达
那两张表，也不缓存它们（缓存归 interaction 域所有）。

follow 流优先合并物化行、关注版块和大 V 的实时结果；hot 流实时合并内容源。
两路都按 (created_at, id) 游标扫描，审校隐藏和重复项过滤后再切页。
审校：命中 hide 的条目在合流前剔除；命中 derank 的压低 ``sort_score`` 字段值
（v1 主序仍为时间倒序，derank 反映到排序分供后续热度排序使用，且 hide 即时生效）。
"""

from __future__ import annotations

import base64
import datetime
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

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
from core.db.repository import DbSession
from core.ports.snapshot import get_user_display_names


async def _fill_authors(db: DbSession, items: list[FeedItem]) -> None:
    """把各源返回的 ``author_id`` 去重后批量查询一次并回填 ``author_name``。

    feed 各源不再各自查作者（除 blog 保留 publisher 兜底），避免同一作者在多源被
    重复 IN 查询。仅回填 ``author_name`` 仍为空者（blog 已填充/兜底的不触碰）。
    解析语义与 feed 一致：优先 profile.nickname，否则 username。
    """
    author_ids = {it.author_id for it in items if it.author_id and not it.author_name}
    if not author_ids:
        return
    name_of = await get_user_display_names(db, author_ids)
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
    物化页合并 ``feed_items``、大 V 作者及所关注版块的实时讨论帖；首页完全没有候选时
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


async def _collect_page(
    fetch: Callable[
        [datetime.datetime | None, uuid.UUID | None, int], Awaitable[list[FeedItem]]
    ],
    *,
    before_time: datetime.datetime | None,
    before_id: uuid.UUID | None,
    limit: int,
    rules: list[Any],
) -> tuple[
    list[FeedItem],
    dict[tuple[str, uuid.UUID], ModerationResult],
    bool,
    tuple[datetime.datetime, uuid.UUID] | None,
]:
    """按原始排序键逐批扫描，审校过滤和跨来源去重后再分页。

    每轮只消费请求量范围内的原始候选；不能把本轮所有候选的最后一条用作下一轮
    游标，否则某个来源仅取到本轮上限时，尚未取出的条目可能被跨源合并跳过。
    隐藏项过多时限制单请求扫描量，并用原始扫描位置续页。
    """
    batch_size = limit + 1
    max_scanned = max(200, limit * 10)
    scanned = 0
    visible: list[FeedItem] = []
    mods: dict[tuple[str, uuid.UUID], ModerationResult] = {}
    seen: set[tuple[str, uuid.UUID]] = set()
    has_candidates = False
    scan_time, scan_id = before_time, before_id
    last_scanned: FeedItem | None = None
    while len(visible) <= limit and scanned < max_scanned:
        size = min(batch_size, max_scanned - scanned)
        candidates = await fetch(scan_time, scan_id, size)
        if not candidates:
            break
        has_candidates = True
        candidates.sort(key=lambda it: (it.created_at, it.id), reverse=True)
        batch = candidates[:size]
        scanned += len(batch)
        kept, batch_mods = _filter_hidden(batch, rules)
        for it in kept:
            key = (it.item_type, it.id)
            if key not in seen:
                seen.add(key)
                visible.append(it)
                mods[key] = batch_mods[key]
                if len(visible) > limit:
                    break
        last_scanned = batch[-1]
        scan_time, scan_id = last_scanned.created_at, last_scanned.id
        if len(candidates) < size:
            break
        batch_size = min(batch_size * 2, 100)
    continuation = None
    if scanned >= max_scanned and len(visible) <= limit and last_scanned is not None:
        continuation = (last_scanned.created_at, last_scanned.id)
    return visible, mods, has_candidates, continuation


async def _finish_page(
    db: DbSession,
    visible: list[FeedItem],
    following_ids: set[uuid.UUID] | None,
    mods: dict[tuple[str, uuid.UUID], ModerationResult],
    limit: int,
    continuation: tuple[datetime.datetime, uuid.UUID] | None,
) -> FeedResponse:
    page = visible[:limit]
    if not page:
        return FeedResponse(
            items=[],
            next_cursor=_encode_cursor(*continuation) if continuation else None,
        )
    await _fill_authors(db, page)
    await _compute_scores(page, following_ids, mods)
    next_cursor = None
    if len(visible) > limit:
        next_cursor = _encode_cursor(page[-1].created_at, page[-1].id)
    elif continuation is not None:
        next_cursor = _encode_cursor(*continuation)
    return FeedResponse(items=page, next_cursor=next_cursor)


async def _materialized_timeline(
    db: DbSession, *, user_id: uuid.UUID, cursor: str | None, limit: int
) -> FeedResponse | None:
    """物化行与实时补拉合并；首页无候选时返回 ``None`` 以回退。"""
    before_time, before_id = _decode_cursor(cursor)
    following_ids = set(await get_following_ids(db, user_id))
    board_ids = set(await get_followed_board_ids(db, user_id))
    if not following_ids and not board_ids:
        return FeedResponse(items=[], next_cursor=None)

    bigv = (await fanout.bigv_authors(db)) & following_ids
    rules = await load_active_rules(db)

    async def _fetch(
        scan_time: datetime.datetime | None, scan_id: uuid.UUID | None, size: int
    ) -> list[FeedItem]:
        items = await _load_materialized_page(db, user_id, scan_time, scan_id, size)
        if bigv or board_ids:
            items.extend(
                await _realtime_supplement(
                    db, bigv, board_ids, scan_time, scan_id, size
                )
            )
        return items

    visible, mods, has_candidates, continuation = await _collect_page(
        _fetch,
        before_time=before_time,
        before_id=before_id,
        limit=limit,
        rules=rules,
    )
    # 仅首页完全没有候选时才用实时兜底：后续页或「候选全被审校隐藏」
    # 都是物化流中的合法空页，切换读模型会造成分页重复或跳条。
    if not has_candidates and before_time is None:
        return None
    return await _finish_page(db, visible, following_ids, mods, limit, continuation)


async def _load_materialized_page(
    db: DbSession,
    user_id: uuid.UUID,
    before_time: datetime.datetime | None,
    before_id: uuid.UUID | None,
    limit: int,
) -> list[FeedItem]:
    """从物化表取一页（(created_at, source_id) 游标下滤，时间倒序）。"""
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


async def _realtime_supplement(
    db: DbSession,
    author_ids: set[uuid.UUID],
    board_ids: set[uuid.UUID],
    before_time: datetime.datetime | None,
    before_id: uuid.UUID | None,
    limit: int,
) -> list[FeedItem]:
    """补拉大 V 作者及关注版块的讨论帖，供物化页合并去重。"""
    items: list[FeedItem] = []
    for name in feed_src.FOLLOW_SOURCES:
        if name != "discussion" and not author_ids:
            continue
        if name == "discussion" and not author_ids and not board_ids:
            continue
        fetch = feed_src.SOURCES[name]
        items.extend(
            await fetch(
                db,
                author_ids,
                board_ids if name == "discussion" else None,
                before_time,
                before_id,
                limit,
            )
        )
    return items


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

    async def _fetch(
        scan_time: datetime.datetime | None, scan_id: uuid.UUID | None, size: int
    ) -> list[FeedItem]:
        items: list[FeedItem] = []
        # 同一个 AsyncSession 不可并发执行 SQL；按源依次查询，仍只在结果页回填作者。
        for name in source_names:
            fetch = feed_src.SOURCES[name]
            a_ids = following_ids if mode == "follow" else None
            b_ids = board_ids if mode == "follow" and name == "discussion" else None
            items.extend(await fetch(db, a_ids, b_ids, scan_time, scan_id, size))
        return items

    rules = await load_active_rules(db)
    visible, mods, _, continuation = await _collect_page(
        _fetch,
        before_time=before_time,
        before_id=before_id,
        limit=limit,
        rules=rules,
    )
    return await _finish_page(db, visible, following_ids, mods, limit, continuation)
