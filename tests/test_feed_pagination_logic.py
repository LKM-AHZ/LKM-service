"""Feed 合流分页的纯逻辑回归测试，无数据库依赖。"""

import datetime
import uuid
from unittest.mock import AsyncMock

from sqlalchemy import Column, DateTime, MetaData, Table, Uuid

from app.modules.admin.moderation.engine import Rule
from app.modules.feed import feed as feed_src
from app.modules.feed import service
from app.modules.feed.schemas import FeedItem


def _item(n: int, *, item_type: str = "discussion", title: str = "visible") -> FeedItem:
    return FeedItem(
        item_type=item_type,
        id=uuid.UUID(int=n),
        author_id=uuid.UUID(int=100),
        author_name="author",
        title=title,
        content_preview="",
        created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        + datetime.timedelta(seconds=n),
        sort_score=0.0,
        url=f"/posts/{n}",
    )


async def test_merge_deduplicates_and_fills_page_after_hidden_items() -> None:
    # 两路都含同一内容；每路单次最多给 limit+1 条。
    streams = [
        [_item(9), _item(7), _item(5)],
        [_item(9), _item(8, title="hide"), _item(6)],
    ]

    async def fetch(before_time, before_id, size):
        return [
            item
            for stream in streams
            for item in [
                x
                for x in stream
                if before_time is None
                or (x.created_at, x.id) < (before_time, before_id)
            ][:size]
        ]

    rules = [Rule(pattern="hide", action="hide")]
    first, _, _, _ = await service._collect_page(
        fetch, before_time=None, before_id=None, limit=2, rules=rules
    )
    assert [item.id.int for item in first] == [9, 7, 6]

    second, _, _, _ = await service._collect_page(
        fetch,
        before_time=first[1].created_at,
        before_id=first[1].id,
        limit=2,
        rules=rules,
    )
    assert [item.id.int for item in second] == [6, 5]


async def test_hidden_backlog_has_bounded_scan_and_continuation() -> None:
    calls = 0
    rows = [_item(n, title="hide") for n in range(1, 301)]

    async def fetch(before_time, before_id, size):
        nonlocal calls
        calls += 1
        return [
            item
            for item in reversed(rows)
            if before_time is None
            or (item.created_at, item.id) < (before_time, before_id)
        ][:size]

    visible, _, _, continuation = await service._collect_page(
        fetch,
        before_time=None,
        before_id=None,
        limit=1,
        rules=[Rule(pattern="hide", action="hide")],
    )
    assert visible == []
    assert continuation is not None
    assert calls <= 10
    page = await service._finish_page(None, visible, None, {}, 1, continuation)
    assert page.items == []
    assert service._decode_cursor(page.next_cursor) == continuation


async def test_materialized_hidden_only_does_not_fall_back(monkeypatch) -> None:
    user_id = uuid.UUID(int=1)
    monkeypatch.setattr(
        service, "get_following_ids", AsyncMock(return_value=[uuid.UUID(int=100)])
    )
    monkeypatch.setattr(service, "get_followed_board_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(service.fanout, "bigv_authors", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        service,
        "load_active_rules",
        AsyncMock(return_value=[Rule(pattern="hide", action="hide")]),
    )

    async def load_page(_db, _user_id, before_time, _before_id, _size):
        return [_item(1, title="hide")] if before_time is None else []

    monkeypatch.setattr(service, "_load_materialized_page", load_page)
    fallback = AsyncMock()
    monkeypatch.setattr(service, "_realtime_timeline", fallback)
    response = await service.get_timeline(
        None, user_id=user_id, mode="follow", cursor=None, limit=2
    )
    assert response.items == []
    fallback.assert_not_awaited()


async def test_materialized_page_reads_current_rows_each_time(monkeypatch) -> None:
    user_id = uuid.UUID(int=1)
    monkeypatch.setattr(
        service, "get_following_ids", AsyncMock(return_value=[uuid.UUID(int=100)])
    )
    monkeypatch.setattr(service, "get_followed_board_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(service.fanout, "bigv_authors", AsyncMock(return_value=set()))
    monkeypatch.setattr(service, "load_active_rules", AsyncMock(return_value=[]))
    rows = [_item(2)]

    async def load_page(_db, _user_id, before_time, _before_id, _size):
        return list(rows) if before_time is None else []

    monkeypatch.setattr(service, "_load_materialized_page", load_page)
    first = await service._materialized_timeline(
        None, user_id=user_id, cursor=None, limit=2
    )
    rows[:] = [_item(3)]
    second = await service._materialized_timeline(
        None, user_id=user_id, cursor=None, limit=2
    )
    assert [it.id.int for it in first.items] == [2]
    assert [it.id.int for it in second.items] == [3]


async def test_board_only_realtime_supplement(monkeypatch) -> None:
    calls = []

    async def fetch(_db, authors, boards, _time, _id, _limit):
        calls.append((authors, boards))
        return [_item(1)]

    monkeypatch.setattr(feed_src, "FOLLOW_SOURCES", ["discussion", "blog"])
    monkeypatch.setattr(feed_src, "SOURCES", {"discussion": fetch, "blog": fetch})
    board = uuid.UUID(int=10)
    items = await service._realtime_supplement(None, set(), {board}, None, None, 3)
    assert [it.id.int for it in items] == [1]
    assert calls == [(set(), {board})]


def test_before_cursor_without_id_only_filters_by_time() -> None:
    table = Table(
        "feed_cursor_test",
        MetaData(),
        Column("created_at", DateTime(timezone=True)),
        Column("id", Uuid),
    )
    condition = feed_src._before_conds(
        table.c.created_at, table.c.id, datetime.datetime.now(datetime.UTC), None
    )
    sql = str(condition[0])
    assert "created_at <" in sql
    assert "id <" not in sql
