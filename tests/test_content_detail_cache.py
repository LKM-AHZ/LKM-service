"""帖详情缓存（蓝图 §5.6 `cache:content:{id}`）：稳定快照缓存 + 计数实时叠加 + 事件失效。

可证伪点：
- 稳定字段（标题）确实**命中缓存**：绕过 service 直改库后，未失效前仍读到旧值。
- 互动计数**不入缓存**：同一时刻直改 like_count，下一次读立即看到新值。
- 失效（clear_item_detail_cache / delete_item）后回源读到新值 / 报不存在。
- get_item_by_slug 与 get_item 共用同一键（按 slug 打开也命中同一份缓存）。
"""

from collections.abc import AsyncIterator
from typing import Any

import fakeredis.aioredis
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.redis as redis_mod
from app.core.cache import cache_get
from app.core.config import settings
from app.core.err import BizError
from app.modules.content.boards.schemas import BoardCreate
from app.modules.content.boards.service import create_board_ex
from app.modules.content.errors import ContentErr
from app.modules.content.models import ContentItem
from app.modules.content.schemas import ContentItemCreate
from app.modules.content.service import (
    _detail_cache_key,
    clear_item_detail_cache,
    create_item,
    delete_item,
    get_item,
    get_item_by_slug,
)
from tests.conftest import AuthUser, auth_user_uid


@pytest.fixture(autouse=True)
async def _fake_redis(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """开 fakeredis，使 cached_read 真正走 L2 缓存（否则 fail-open 直读库，测不出命中）。"""
    fake = fakeredis.aioredis.FakeRedis()
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")

    def _from_url(cls: Any, url: str, **kwargs: Any) -> Any:
        return fake

    monkeypatch.setattr(redis_mod.Redis, "from_url", classmethod(_from_url))
    yield
    await redis_mod.close_redis()
    redis_mod._client = None
    redis_mod._client_pool = None


async def _au(auth_db: AsyncSession, username: str) -> AuthUser:
    return await auth_user_uid(
        auth_db,
        username=username,
        email=f"{username}@example.com",
        nickname=username,
        account_level="normal",
    )


async def _make_board(db: AsyncSession, slug: str, owner_id: Any = None) -> Any:
    return (
        await create_board_ex(db, BoardCreate(slug=slug, title=slug), owner_id)
    ).id


async def _make_item(
    db: AsyncSession, uid: Any, title: str = "原标题", *, slug: str | None = None
) -> ContentItem:
    bid = await _make_board(db, f"b-{slug or uid}")
    info = await create_item(
        db,
        uid,
        ContentItemCreate(
            content_type="article" if slug else "discussion",
            board_id=bid,
            title=title,
            content="正文",
            slug=slug,
        ),
    )
    return await db.get(ContentItem, info.id)


async def _direct_edit(db: AsyncSession, item_id: Any, **values: Any) -> None:
    """绕过 service 直接改库（模拟「不经失效通道」的外部写入，用于证明缓存命中）。"""
    await db.execute(
        sa.update(ContentItem).where(ContentItem.id == item_id).values(**values)
    )
    await db.flush()


async def test_stable_fields_cached_counts_live(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """标题命中缓存（旧值），计数不入缓存（立即新值）——两种口径同时可证伪。"""
    uid = (await _au(auth_db, "c1")).id
    item = await _make_item(db, uid)
    first = await get_item(db, item.id)
    assert first.title == "原标题" and first.like_count == 0
    assert await cache_get(_detail_cache_key(item.id)) is not None  # 已回填

    # 直改库：标题 + like_count 同时变，且不失效缓存
    await _direct_edit(db, item.id, title="直改标题", like_count=7)

    second = await get_item(db, item.id)
    assert second.title == "原标题"  # 稳定字段来自缓存 → 仍旧值
    assert second.like_count == 7  # 计数来自窄列实时 SELECT → 立即新值


async def test_explicit_invalidation_reads_through(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    uid = (await _au(auth_db, "c2")).id
    item = await _make_item(db, uid)
    await get_item(db, item.id)
    await _direct_edit(db, item.id, title="新标题")
    assert (await get_item(db, item.id)).title == "原标题"  # 未失效 → 缓存旧值

    await clear_item_detail_cache(item.id)
    assert await cache_get(_detail_cache_key(item.id)) is None
    assert (await get_item(db, item.id)).title == "新标题"  # 失效后回源


async def test_delete_invalidates_and_hides(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    uid = (await _au(auth_db, "c3")).id
    item = await _make_item(db, uid)
    await get_item(db, item.id)
    assert await cache_get(_detail_cache_key(item.id)) is not None

    await delete_item(db, item.id, uid)
    # 软删路径必须删键（否则 TTL 内缓存会把已删帖继续吐出来）
    assert await cache_get(_detail_cache_key(item.id)) is None
    with pytest.raises(BizError) as e:
        await get_item(db, item.id)
    assert e.value.errcode == ContentErr.CONTENT_NOT_FOUND


async def test_get_by_slug_reuses_same_key(
    db: AsyncSession, auth_db: AsyncSession, auth_seam_realm: None
) -> None:
    """按 slug 打开与按 id 打开共用一份缓存：slug 路径也命中 id 键。"""
    uid = (await _au(auth_db, "c4")).id
    item = await _make_item(db, uid, slug="s-cache")
    await get_item(db, item.id)
    await _direct_edit(db, item.id, title="直改标题")

    # 未失效：slug 路径读到的仍是 id 键里缓存的旧标题 → 证明复用了同一份缓存
    slug_view = await get_item_by_slug(db, "s-cache")
    assert slug_view.id == item.id
    assert slug_view.title == "原标题"
