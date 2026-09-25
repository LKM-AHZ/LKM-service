"""时间线全量回填（B5）：存量补写、幂等可重跑、缺失水位补行、参数校验。

回填复用既有 fanout（水位 + ``uq_feed_item`` 幂等），故「重跑不重复」是主要正确性断言。
"""

import datetime
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.redis as redis_mod
from app.core.config import settings
from app.flows.feed_backfill_body import _EARLIEST, _parse_since, backfill_feed
from app.modules.content.boards.schemas import BoardCreate
from app.modules.content.boards.service import create_board_ex
from app.modules.content.schemas import ContentItemCreate
from app.modules.content.service import create_item
from app.modules.feed import feed as feed_src
from app.modules.feed.models import FeedFanoutState, FeedItemMaterialized
from app.modules.interaction.service import follow_user
from tests.conftest import AuthUser, auth_user_uid


@pytest.fixture(autouse=True)
async def reset_redis_globals() -> AsyncIterator[None]:
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None
    yield
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None


async def _enable_fake_redis(monkeypatch: Any) -> Any:
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)

    def _from_url(cls: Any, url: str, **kwargs: Any) -> Any:
        return fake

    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(_from_url))
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
    return fake


async def _au(auth_db: AsyncSession, name: str) -> AuthUser:
    return await auth_user_uid(
        auth_db,
        username=name,
        email=f"{name}@example.com",
        nickname=name,
        account_level="normal",
    )


async def _board(db: AsyncSession, slug: str) -> Any:
    return (
        await create_board_ex(
            db, BoardCreate(slug=slug, title=slug, description="d"), None
        )
    ).id


async def _post(db: AsyncSession, board_id: Any, uid: Any, title: str) -> Any:
    item = await create_item(
        db, uid, ContentItemCreate(board_id=board_id, title=title, content="正文")
    )
    return item.id


async def _count_rows(db: AsyncSession, user_id: Any) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(FeedItemMaterialized)
            .where(FeedItemMaterialized.user_id == user_id)
        )
    ) or 0


async def _push_watermark_future(db: AsyncSession, source: str) -> None:
    """把某源水位抬到未来：模拟「水位已推进，但物化行缺失」的存量场景。"""
    future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
    state = await db.get(FeedFanoutState, source)
    if state is None:
        db.add(FeedFanoutState(source=source, last_created_at=future, last_id=None))
    else:
        state.last_created_at = future
        state.last_id = None
    await db.flush()


async def test_backfill_restores_missing_rows_and_is_idempotent(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None, monkeypatch: Any
) -> None:
    await _enable_fake_redis(monkeypatch)
    author = await _au(auth_db, "bf-author")
    reader = await _au(auth_db, "bf-reader")
    bid = await _board(db, "bf-b1")
    await _post(db, bid, author.id, "存量帖")
    await follow_user(db, reader.id, author.id)
    assert await _count_rows(db, reader.id) == 1

    # 模拟物化行丢失 + 水位已推进（此时 cron 不会再扫到它）
    await db.execute(
        delete(FeedItemMaterialized).where(FeedItemMaterialized.user_id == reader.id)
    )
    await _push_watermark_future(db, "discussion")
    assert await _count_rows(db, reader.id) == 0

    result = await backfill_feed(db=db, batch_size=50)
    assert result["fanout_items"] >= 1
    assert await _count_rows(db, reader.id) == 1

    # 幂等：重跑不产生重复行（uq_feed_item + on_conflict_do_nothing）
    await backfill_feed(db=db, batch_size=50)
    assert await _count_rows(db, reader.id) == 1


async def test_backfill_creates_missing_watermark_rows(
    db: AsyncSession, monkeypatch: Any
) -> None:
    """缺失的水位行要被补出——否则该源（从未 fanout 过）的存量永远不回填。"""
    await _enable_fake_redis(monkeypatch)
    await db.execute(delete(FeedFanoutState))
    await db.flush()

    await backfill_feed(db=db, batch_size=10)

    rows = (await db.execute(select(FeedFanoutState.source))).scalars().all()
    assert set(rows) == set(feed_src.FOLLOW_SOURCES)


def test_parse_since_empty_means_earliest() -> None:
    assert _parse_since("") == _EARLIEST
    assert _parse_since(None) == _EARLIEST
    assert _parse_since("   ") == _EARLIEST


def test_parse_since_accepts_iso_and_rejects_garbage() -> None:
    parsed = _parse_since("2026-01-01T00:00:00+08:00")
    assert parsed.tzinfo is not None
    with pytest.raises(ValueError):
        _parse_since("not-a-date")


async def test_backfill_rejects_non_positive_batch(
    db: AsyncSession, monkeypatch: Any
) -> None:
    await _enable_fake_redis(monkeypatch)
    with pytest.raises(ValueError):
        await backfill_feed(db=db, batch_size=-1)
