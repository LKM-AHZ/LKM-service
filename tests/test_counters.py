"""互动计数：写穿主路径（B3）+ Redis write-behind 回退通道 + 对账（M6.10）。

覆盖两套语义：

- **写穿（默认，``LKM_COUNTERS_WRITE_THROUGH=true``）**：点赞/收藏/评论与明细同事务原子改
  计数列，读数为真值、无 pending 残留；事务回滚时明细与计数一起回滚。
- **回退（开关关闭）**：M6.10 的 Redis 增量链路——写路径只记增量、``flush_counters`` 落库。

两套共用的：对账以明细为真相源修正偏差且**可证伪**（二次 ``affected == 0``）；
cron 注册随模式变化（写穿下 flush 无事可做故不注册）。
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select
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


async def _db_count(db: AsyncSession, item_id: uuid.UUID, field: str) -> int:
    col = getattr(ContentItem, field)
    value = await db.scalar(select(col).where(ContentItem.id == item_id))
    assert value is not None
    return int(value)


# ---- 写穿主路径（默认）----


async def test_like_commits_with_detail_same_tx(db: AsyncSession) -> None:
    """写穿：计数列与明细同事务立即可见，且不产生 Redis pending。"""
    item = await _make_item(db, "cnt-w1")
    item_id = item.id

    assert await like_item(db, item_id, uuid.uuid4()) == 1

    assert await _db_count(db, item_id, "like_count") == 1
    likes = await db.scalar(
        select(func.count()).select_from(ContentLike).where(ContentLike.content_id == item_id)
    )
    assert likes == 1
    assert await counters.pending_delta("like_count", item_id) == 0


async def test_rollback_reverts_detail_and_count(db: AsyncSession) -> None:
    """写穿的同事务性：回滚后明细与计数列一起消失（不会「有计数无明细」）。"""
    item = await _make_item(db, "cnt-w2")
    item_id = item.id
    await db.commit()  # 基线落库，后续点赞单独成事务

    await like_item(db, item_id, uuid.uuid4())
    assert await _db_count(db, item_id, "like_count") == 1

    await db.rollback()
    assert await _db_count(db, item_id, "like_count") == 0
    likes = await db.scalar(
        select(func.count()).select_from(ContentLike).where(ContentLike.content_id == item_id)
    )
    assert likes == 0


async def test_duplicate_like_counts_once(db: AsyncSession) -> None:
    """同一用户重复点赞：幂等早退，计数只 +1。"""
    item = await _make_item(db, "cnt-w3")
    user_id = uuid.uuid4()

    assert await like_item(db, item.id, user_id) == 1
    assert await like_item(db, item.id, user_id) == 1
    assert await _db_count(db, item.id, "like_count") == 1


async def test_unlike_floors_at_zero(db: AsyncSession) -> None:
    item = await _make_item(db, "cnt-w4")
    user_id = uuid.uuid4()

    await like_item(db, item.id, user_id)
    assert await unlike_item(db, item.id, user_id) == 0
    # 未点赞者取消 → 幂等且不压负
    assert await unlike_item(db, item.id, uuid.uuid4()) == 0
    assert await _db_count(db, item.id, "like_count") == 0


async def test_bookmark_and_comment_write_through(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """收藏与评论同样写穿（DB 计数列即时为真值）。"""
    item = await _make_item(db, "cnt-w5")
    commenter = await auth_user_uid(
        auth_db,
        username="wt-commenter",
        email="wt-commenter@example.com",
        nickname="wt-commenter",
        account_level="normal",
    )
    fav_user = uuid.uuid4()

    state = await add_favorite(db, user_id=fav_user, content_id=item.id)
    assert state.bookmark_count == 1
    assert await _db_count(db, item.id, "bookmark_count") == 1

    await create_comment(
        db,
        item_id=item.id,
        user_id=commenter.id,
        info=ContentCommentCreate(content="hello"),
    )
    assert await _db_count(db, item.id, "comment_count") == 1

    state = await remove_favorite(db, user_id=fav_user, content_id=item.id)
    assert state.bookmark_count == 0
    assert await _db_count(db, item.id, "bookmark_count") == 0


# ---- 回退通道：M6.10 的 Redis write-behind ----


async def test_write_behind_buffers_in_redis_then_flush(
    db: AsyncSession, monkeypatch: Any
) -> None:
    monkeypatch.setattr(settings, "counters_write_through", False)
    await _enable_fake_redis(monkeypatch)
    item = await _make_item(db, "cnt-b")

    new_count = await like_item(db, item.id, uuid.uuid4())
    assert new_count == 1  # 即时读数 = DB 0 + 未落库 1
    assert await _db_count(db, item.id, "like_count") == 0  # DB 计数列未被写
    assert await counters.pending_delta("like_count", item.id) == 1

    assert await flush_counters(db) == 1
    assert await _db_count(db, item.id, "like_count") == 1
    assert await counters.pending_delta("like_count", item.id) == 0
    # 二次 flush 无待落库差值 → 0 行（幂等）
    assert await flush_counters(db) == 0


async def test_write_behind_bookmark_and_comment(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None, monkeypatch: Any
) -> None:
    """回退模式下收藏/评论也走 Redis 增量（写路径不直改计数列）。"""
    monkeypatch.setattr(settings, "counters_write_through", False)
    await _enable_fake_redis(monkeypatch)
    item = await _make_item(db, "cnt-b7")
    commenter = await auth_user_uid(
        auth_db,
        username="commenter",
        email="commenter@example.com",
        nickname="commenter",
        account_level="normal",
    )
    fav_user = uuid.uuid4()

    state = await add_favorite(db, user_id=fav_user, content_id=item.id)
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

    state = await remove_favorite(db, user_id=fav_user, content_id=item.id)
    assert state.bookmark_count == 0
    await flush_counters(db)
    assert await _db_count(db, item.id, "bookmark_count") == 0


async def test_flush_negative_delta_floor(db: AsyncSession, monkeypatch: Any) -> None:
    """回退模式：净负差值不得把计数压到 0 以下。"""
    await _enable_fake_redis(monkeypatch)
    item = await _make_item(db, "cnt-b4")

    await counters.bump_counter("like_count", item.id, -5)
    await flush_counters(db)
    assert await _db_count(db, item.id, "like_count") == 0


# ---- 两套共用：对账与注册 ----


async def test_reconcile_fixes_drift_and_is_falsifiable(db: AsyncSession) -> None:
    """对账以明细 COUNT 为真相源；连续两次第二次 affected == 0（收敛可证伪）。"""
    item = await _make_item(db, "cnt-b5")
    db.add_all(
        [
            ContentLike(content_id=item.id, user_id=uuid.uuid4()),
            ContentLike(content_id=item.id, user_id=uuid.uuid4()),
            ContentComment(
                content_id=item.id, user_id=uuid.uuid4(), content="c1", floor_number=1
            ),
        ]
    )
    db.add(InteractionFavorite(content_id=item.id, user_id=uuid.uuid4()))
    # 人为制造偏差（模拟历史脏值/异常路径漏计）
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


async def test_bump_rejects_unknown_field(db: AsyncSession) -> None:
    item = await _make_item(db, "cnt-b8")
    with pytest.raises(ValueError):
        await bump_content_counter(db, item.id, "view_count", 1)


def test_counts_cron_matches_mode() -> None:
    """cron 注册随模式变化：写穿下 flush 无事可做（不注册），对账始终注册。

    断言按**当前模式**而非写死清单——否则把默认值改回 write-behind 时这条会误红。
    """
    ensure_tasks_registered()
    job_ids = {j["id"] for j in cron_jobs()}
    assert "reconcile_content_counts" in job_ids
    assert ("flush_content_counters" in job_ids) is (
        not settings.counters_write_through
    )
    # handler 两种模式都在（回退运行时 flush 仍可被手工触发）
    handlers = handlers_for(SUB_JOBS.name)
    assert "flush_content_counters" in handlers
    assert "reconcile_content_counts" in handlers
