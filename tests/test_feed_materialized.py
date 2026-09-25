"""M6.11 时间线物化读模型：fanout 写扩散 + 物化读 + 大 V 兜底 + 关注变更一致性。

覆盖：
- cron fanout 按源水位把新内容写进关注者的 feed_items；重复执行幂等（唯一约束/水位）
- 读路径走物化（不再实时合流）；游标分页在物化表上推进
- 受众超阈值 → 跳过写扩散并标记大 V；读路径实时补拉，**内容不丢**
- 新关注回填该作者/版块最近内容；取关清理其物化条目（不残留已取关内容）
- cron 注册存在（防回退）
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.redis as redis_mod
from app.core.config import settings
from app.core.messaging import SUB_JOBS
from app.core.task_registry import cron_jobs, ensure_tasks_registered, handlers_for
from app.modules.content.boards.schemas import BoardCreate
from app.modules.content.boards.service import create_board_ex
from app.modules.content.schemas import ContentItemCreate
from app.modules.content.service import create_item
from app.modules.feed import fanout
from app.modules.feed.models import FeedItemMaterialized
from app.modules.feed.service import get_timeline
from app.modules.interaction.service import (
    follow_board,
    follow_user,
    unfollow_board,
    unfollow_user,
)
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


async def _board(db: AsyncSession, slug: str) -> int:
    return (
        await create_board_ex(
            db, BoardCreate(slug=slug, title=slug, description="d"), None
        )
    ).id


async def _post(db: AsyncSession, board_id: int, uid: int, title: str) -> int:
    item = await create_item(
        db, uid, ContentItemCreate(board_id=board_id, title=title, content="正文")
    )
    return item.id


async def _count_rows(db: AsyncSession, user_id: int) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(FeedItemMaterialized)
            .where(FeedItemMaterialized.user_id == user_id)
        )
    ) or 0


async def test_fanout_and_materialized_read(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    author = await _au(auth_db, "author")
    reader = await _au(auth_db, "reader")
    bid = await _board(db, "fm-b1")
    await _post(db, bid, author.id, "作者的新帖")

    # fanout 水位起点是「启用时刻」，故先建内容再推进水位不覆盖它；
    # 这里直接验证「关注 → 回填 → 读物化」这条主链路。
    await follow_user(db, reader.id, author.id)
    assert await _count_rows(db, reader.id) == 1

    resp = await get_timeline(
        db, user_id=reader.id, mode="follow", cursor=None, limit=20
    )
    assert [i.title for i in resp.items] == ["作者的新帖"]


async def test_fanout_batch_scans_new_items_idempotently(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    author = await _au(auth_db, "author2")
    reader = await _au(auth_db, "reader2")
    bid = await _board(db, "fm-b2")
    await follow_user(db, reader.id, author.id)
    before = await _count_rows(db, reader.id)

    # 首次调用只初始化水位（存量内容不回填，由关注回填/实时兜底覆盖）
    await fanout.fanout_batch(db)
    # 水位之后发布 → 由 cron 扫描写入
    await _post(db, bid, author.id, "水位之后的新帖")
    processed = await fanout.fanout_batch(db)
    assert processed >= 1
    assert await _count_rows(db, reader.id) == before + 1

    # 幂等：水位已推进，重复跑不再重复插入（唯一约束 + 水位双保险）
    again = await fanout.fanout_batch(db)
    assert again == 0
    assert await _count_rows(db, reader.id) == before + 1


async def test_cursor_pagination_on_materialized(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    author = await _au(auth_db, "author3")
    reader = await _au(auth_db, "reader3")
    bid = await _board(db, "fm-b3")
    for n in range(3):
        await _post(db, bid, author.id, f"帖 {n}")
    await follow_user(db, reader.id, author.id)
    assert await _count_rows(db, reader.id) == 3

    page1 = await get_timeline(
        db, user_id=reader.id, mode="follow", cursor=None, limit=2
    )
    assert len(page1.items) == 2
    assert page1.next_cursor is not None

    page2 = await get_timeline(
        db, user_id=reader.id, mode="follow", cursor=page1.next_cursor, limit=2
    )
    ids1 = {i.id for i in page1.items}
    ids2 = {i.id for i in page2.items}
    assert not (ids1 & ids2)  # 游标推进不重不漏
    assert len(ids1 | ids2) == 3


async def test_superfan_skipped_but_reader_still_sees_content(
    db: AsyncSession,
    auth_db: AsyncSession,
    auth_seam_realm: None,
    monkeypatch: Any,
) -> None:
    """受众超阈值 → 不写物化，但读路径实时补拉，内容不丢（大 V 兜底）。"""
    await _enable_fake_redis(monkeypatch)
    monkeypatch.setattr(settings, "feed_fanout_max_followers", 0)

    author = await _au(auth_db, "bigv")
    reader = await _au(auth_db, "follower")
    bid = await _board(db, "fm-b4")
    await follow_user(db, reader.id, author.id)
    await fanout.fanout_batch(db)  # 初始化水位（存量不回填）
    await _post(db, bid, author.id, "大 V 的新帖")

    await fanout.fanout_batch(db)
    assert await _count_rows(db, reader.id) == 0  # 写扩散被跳过
    assert author.id in await fanout.bigv_authors()  # 已标记为大 V

    resp = await get_timeline(
        db, user_id=reader.id, mode="follow", cursor=None, limit=20
    )
    assert [i.title for i in resp.items] == ["大 V 的新帖"]  # 实时补拉兜底


async def test_unfollow_removes_materialized_items(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    author = await _au(auth_db, "author4")
    reader = await _au(auth_db, "reader4")
    bid = await _board(db, "fm-b5")
    await _post(db, bid, author.id, "会被取关的帖")
    await follow_user(db, reader.id, author.id)
    assert await _count_rows(db, reader.id) == 1

    await unfollow_user(db, reader.id, author.id)
    assert await _count_rows(db, reader.id) == 0
    # 取关后读路径不应再出现该作者内容（物化已清，实时兜底也按关注过滤）
    resp = await get_timeline(
        db, user_id=reader.id, mode="follow", cursor=None, limit=20
    )
    assert resp.items == []


async def test_follow_board_backfill_and_unfollow_cleanup(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    author = await _au(auth_db, "author5")
    reader = await _au(auth_db, "reader5")
    bid = await _board(db, "fm-b6")
    await _post(db, bid, author.id, "版块里的帖")

    await follow_board(db, reader.id, bid)
    assert await _count_rows(db, reader.id) == 1

    await unfollow_board(db, reader.id, bid)
    assert await _count_rows(db, reader.id) == 0


async def test_fanout_cron_registered() -> None:
    ensure_tasks_registered()
    job_ids = {j["id"] for j in cron_jobs()}
    assert "fanout_feed_items" in job_ids
    assert "fanout_feed_items" in handlers_for(SUB_JOBS.name)
