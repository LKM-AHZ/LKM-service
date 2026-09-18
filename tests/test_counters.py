"""M6.10 互动计数 Redis 链路 + 对账。

覆盖：
- 写路径只记 Redis 增量（DB 计数列不动），flush 后落库并清空增量
- Redis 不可用 fail-open 直改 DB（与引入本链路前语义一致）
- 负增量下限 0（取消赞不会把计数压负）
- 对账以明细为真相源修正偏差，且**可证伪**（二次 affected == 0）
- 对账覆盖 like/comment/bookmark 三项
- cron/任务注册存在（防回退：任务被摘掉即红）
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.redis as redis_mod
from app.core import counters
from app.core.config import settings
from app.core.messaging import SUB_JOBS
from app.core.task_registry import cron_jobs, ensure_tasks_registered, handlers_for
from app.modules.content.boards.schemas import BoardCreate
from app.modules.content.boards.service import create_board_ex
from app.modules.content.counters import (
    bump_content_counter,
    flush_counters,
    reconcile_counts,
)
from app.modules.content.models import ContentComment, ContentItem, ContentLike
from app.modules.content.schemas import ContentCommentCreate
from app.modules.content.service import create_comment, like_item, unlike_item
from app.modules.interaction.models import InteractionFavorite
from app.modules.interaction.service import add_favorite, remove_favorite
from tests.conftest import auth_user_uid


@pytest.fixture(autouse=True)
async def reset_redis_globals() -> AsyncIterator[None]:
    """每个用例前后彻底复位 Redis 单例（照 tests/test_cache.py 范式）。"""
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None
    yield
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None


async def _enable_fake_redis(monkeypatch: Any) -> Any:
    import fakeredis.aioredis

    # decode_responses=True 与生产 get_redis() 的客户端一致（scan_iter 返回 str 键）
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)

    def _from_url(cls: Any, url: str, **kwargs: Any) -> Any:
        return fake

    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(_from_url))
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
    return fake


async def _make_item(db: AsyncSession, slug: str = "cnt-b") -> ContentItem:
    board_id = (
        await create_board_ex(
            db, BoardCreate(slug=slug, title=slug, description="d"), None
        )
    ).id
    item = ContentItem(
        content_type="discussion",
        board_id=board_id,
        title="计数目标",
        excerpt="",
        content="正文",
        tags="[]",
    )
    db.add(item)
    await db.flush()
    return item


async def _db_count(db: AsyncSession, item_id: int, field: str) -> int:
    col = getattr(ContentItem, field)
    value = await db.scalar(select(col).where(ContentItem.id == item_id))
    assert value is not None
    return int(value)


async def test_like_buffers_in_redis_then_flush(
    db: AsyncSession, monkeypatch: Any
) -> None:
    await _enable_fake_redis(monkeypatch)
    item = await _make_item(db)

    new_count = await like_item(db, item.id, 42)
    assert new_count == 1  # 即时读数 = DB 0 + 未落库 1
    assert await _db_count(db, item.id, "like_count") == 0  # DB 计数列未被写
    assert await counters.pending_delta("like_count", item.id) == 1

    assert await flush_counters(db) == 1
    assert await _db_count(db, item.id, "like_count") == 1
    assert await counters.pending_delta("like_count", item.id) == 0
    # 二次 flush 无待落库差值 → 0 行（幂等）
    assert await flush_counters(db) == 0


async def test_unlike_floors_at_zero_and_flushes(
    db: AsyncSession, monkeypatch: Any
) -> None:
    await _enable_fake_redis(monkeypatch)
    item = await _make_item(db, "cnt-b2")

    await like_item(db, item.id, 7)
    await flush_counters(db)
    assert await _db_count(db, item.id, "like_count") == 1

    assert await unlike_item(db, item.id, 7) == 0
    await flush_counters(db)
    assert await _db_count(db, item.id, "like_count") == 0

    # 未点赞者取消 → 幂等，且不会把计数压负
    assert await unlike_item(db, item.id, 999) == 0
    await flush_counters(db)
    assert await _db_count(db, item.id, "like_count") == 0


async def test_fail_open_direct_db_when_redis_absent(db: AsyncSession) -> None:
    """Redis 未启用：回退原子 UPDATE，计数立刻可见（语义与引入链路前一致）。"""
    item = await _make_item(db, "cnt-b3")

    assert await like_item(db, item.id, 5) == 1
    assert await _db_count(db, item.id, "like_count") == 1
    assert await unlike_item(db, item.id, 5) == 0
    assert await _db_count(db, item.id, "like_count") == 0


async def test_flush_negative_delta_floor(db: AsyncSession, monkeypatch: Any) -> None:
    """净负差值不得把计数压到 0 以下。"""
    await _enable_fake_redis(monkeypatch)
    item = await _make_item(db, "cnt-b4")

    await counters.bump_counter("like_count", item.id, -5)
    await flush_counters(db)
    assert await _db_count(db, item.id, "like_count") == 0


async def test_reconcile_fixes_drift_and_is_falsifiable(db: AsyncSession) -> None:
    """对账以明细 COUNT 为真相源；连续两次第二次 affected == 0（收敛可证伪）。"""
    item = await _make_item(db, "cnt-b5")
    db.add_all(
        [
            ContentLike(content_id=item.id, user_id=1),
            ContentLike(content_id=item.id, user_id=2),
            ContentComment(
                content_id=item.id, user_id=3, content="c1", floor_number=1
            ),
        ]
    )
    db.add(InteractionFavorite(content_id=item.id, user_id=9))
    # 人为制造偏差（模拟链路漏落库/漏计）
    item.like_count = 99
    item.comment_count = 0
    item.bookmark_count = 7
    await db.flush()

    scanned, affected = await reconcile_counts(db)
    assert scanned >= 1
    assert affected == 1
    assert await _db_count(db, item.id, "like_count") == 2
    assert await _db_count(db, item.id, "comment_count") == 1
    assert await _db_count(db, item.id, "bookmark_count") == 1

    _, affected2 = await reconcile_counts(db)
    assert affected2 == 0  # 已收敛，二次对账不再改动任何行


async def test_reconcile_writes_reconciled_marker(db: AsyncSession) -> None:
    """有偏差的行被打上 counts_reconciled_at；无偏差的行不被触碰。"""
    item = await _make_item(db, "cnt-b6")
    item.like_count = 5
    await db.flush()

    await reconcile_counts(db)
    marker = await db.scalar(
        select(ContentItem.counts_reconciled_at).where(ContentItem.id == item.id)
    )
    assert marker is not None

    _, affected = await reconcile_counts(db)
    assert affected == 0


async def test_bookmark_and_comment_go_through_link(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None, monkeypatch: Any
) -> None:
    """收藏与评论计数也走链路（写路径不直改 DB 计数列）。"""
    await _enable_fake_redis(monkeypatch)
    item = await _make_item(db, "cnt-b7")
    # 评论 service 会经 auth.snapshot 回填显示名 → 需本测 seam 里确有该用户
    commenter = await auth_user_uid(
        auth_db,
        username="commenter",
        email="commenter@example.com",
        nickname="commenter",
        account_level="normal",
    )

    state = await add_favorite(db, user_id=3, content_id=item.id)
    assert state.bookmark_count == 1
    await create_comment(
        db,
        item_id=item.id,
        user_id=commenter.id,
        info=ContentCommentCreate(content="hello"),
    )
    assert await _db_count(db, item.id, "bookmark_count") == 0
    assert await _db_count(db, item.id, "comment_count") == 0

    await flush_counters(db)
    assert await _db_count(db, item.id, "bookmark_count") == 1
    assert await _db_count(db, item.id, "comment_count") == 1

    state = await remove_favorite(db, user_id=3, content_id=item.id)
    assert state.bookmark_count == 0
    await flush_counters(db)
    assert await _db_count(db, item.id, "bookmark_count") == 0


async def test_bump_rejects_unknown_field(db: AsyncSession) -> None:
    item = await _make_item(db, "cnt-b8")
    with pytest.raises(ValueError):
        await bump_content_counter(db, item.id, "view_count", 1)


async def test_counts_cron_registered() -> None:
    """防回退：落库与对账 cron 必须已登记（引用了不存在的模块即红）。"""
    ensure_tasks_registered()
    job_ids = {j["id"] for j in cron_jobs()}
    assert {"flush_content_counters", "reconcile_content_counts"} <= job_ids
    handlers = handlers_for(SUB_JOBS.name)
    assert "flush_content_counters" in handlers
    assert "reconcile_content_counts" in handlers
