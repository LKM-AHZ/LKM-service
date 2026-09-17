"""interaction 域（M6.6）：收藏幂等/计数、浏览记录 upsert 幂等与保留期清理。

service 直测注入业务 ``db`` 与 auth ``auth_db``（业务行只写 auth realm 的裸 int id）。
"""

import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.err import BizError
from app.modules.content.models import Board, ContentItem, ContentStatus, ContentType
from app.modules.interaction.errors import InteractionErr
from app.modules.interaction.models import InteractionFavorite, InteractionViewLog
from app.modules.interaction.service import (
    add_favorite,
    list_favorites,
    list_history,
    purge_stale_view_logs,
    record_view,
    remove_favorite,
)
from tests.conftest import auth_user_uid


async def _mk_user(auth_db: AsyncSession, username: str = "alice") -> int:
    return int(
        (
            await auth_user_uid(
                auth_db,
                username=username,
                email=f"{username}@x.test",
                nickname=username,
                account_level="normal",
                with_token=False,
            )
        ).id
    )


async def _mk_item(db: AsyncSession, author_id: int | None, title: str = "帖子") -> int:
    board = Board(slug=f"b-{title}", title="B", description="", status="active")
    db.add(board)
    await db.flush()
    item = ContentItem(
        content_type=ContentType.DISCUSSION,
        board_id=board.id,
        author_id=author_id,
        title=title,
        content="正文",
        status=ContentStatus.PUBLISHED,
    )
    db.add(item)
    await db.flush()
    return int(item.id)


async def _fav_count(db: AsyncSession, item_id: int) -> int:
    return int(
        await db.scalar(
            select(ContentItem.bookmark_count).where(ContentItem.id == item_id)
        )
    )


async def _view_rows(db: AsyncSession, user_id: int, item_id: int) -> list:
    return list(
        (
            await db.execute(
                select(InteractionViewLog).where(
                    InteractionViewLog.user_id == user_id,
                    InteractionViewLog.content_id == item_id,
                )
            )
        )
        .scalars()
        .all()
    )


class TestFavorite:
    async def test_add_idempotent(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        uid = await _mk_user(auth_db)
        item_id = await _mk_item(db, uid)

        first = await add_favorite(db, uid, item_id)
        second = await add_favorite(db, uid, item_id)  # 重复收藏：不报错

        assert (first.favorited, second.favorited) == (True, True)
        assert first.bookmark_count == second.bookmark_count == 1
        assert await _fav_count(db, item_id) == 1
        rows = (
            await db.execute(
                select(func.count())
                .select_from(InteractionFavorite)
                .where(InteractionFavorite.content_id == item_id)
            )
        ).scalar()
        assert rows == 1

    async def test_remove_favorite_decrements_once(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        uid = await _mk_user(auth_db)
        item_id = await _mk_item(db, uid)
        await add_favorite(db, uid, item_id)

        removed = await remove_favorite(db, uid, item_id)
        again = await remove_favorite(db, uid, item_id)  # 未收藏时幂等

        assert removed.favorited is False
        assert removed.bookmark_count == 0
        assert again.bookmark_count == 0
        assert await _fav_count(db, item_id) == 0

    async def test_missing_content_raises(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        uid = await _mk_user(auth_db)
        with pytest.raises(BizError) as err:
            await add_favorite(db, uid, 999999)
        assert err.value.errcode == InteractionErr.CONTENT_NOT_FOUND

        with pytest.raises(BizError) as err2:
            await remove_favorite(db, uid, 999999)
        assert err2.value.errcode == InteractionErr.CONTENT_NOT_FOUND

    async def test_list_favorites_inlines_content(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        uid = await _mk_user(auth_db)
        other = await _mk_user(auth_db, "bob")
        a = await _mk_item(db, uid, "甲")
        b = await _mk_item(db, uid, "乙")
        await add_favorite(db, uid, a)
        await add_favorite(db, uid, b)
        await add_favorite(db, other, a)  # 别人的收藏不进我的列表

        page = await list_favorites(db, uid, page=1, limit=10)

        assert page.total == 2
        assert {i.content_id for i in page.items} == {a, b}
        assert {i.title for i in page.items} == {"甲", "乙"}
        assert all(i.content_type == ContentType.DISCUSSION for i in page.items)

    async def test_list_favorites_paginates(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        uid = await _mk_user(auth_db)
        for i in range(3):
            await add_favorite(db, uid, await _mk_item(db, uid, f"帖{i}"))

        first = await list_favorites(db, uid, page=1, limit=2)
        second = await list_favorites(db, uid, page=2, limit=2)

        assert (first.total, first.pages) == (3, 2)
        assert len(first.items) == 2 and len(second.items) == 1


class TestViewLog:
    async def test_record_view_is_idempotent(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        uid = await _mk_user(auth_db)
        item_id = await _mk_item(db, uid)

        first = await record_view(db, uid, item_id)
        second = await record_view(db, uid, item_id)

        rows = await _view_rows(db, uid, item_id)
        assert len(rows) == 1, "重复上报同内容只保留一行"
        assert rows[0].viewed_at >= first.viewed_at
        assert second.viewed_at >= first.viewed_at

    async def test_record_view_missing_content(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        uid = await _mk_user(auth_db)
        with pytest.raises(BizError) as err:
            await record_view(db, uid, 999999)
        assert err.value.errcode == InteractionErr.CONTENT_NOT_FOUND

    async def test_list_history_orders_by_viewed_at(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        uid = await _mk_user(auth_db)
        a = await _mk_item(db, uid, "甲")
        b = await _mk_item(db, uid, "乙")
        await record_view(db, uid, a)
        await record_view(db, uid, b)
        # 把 a 的 viewed_at 推早，验证排序取 viewed_at desc
        await db.execute(
            sa_update(InteractionViewLog)
            .where(InteractionViewLog.content_id == a)
            .values(
                viewed_at=datetime.datetime.now(datetime.UTC)
                - datetime.timedelta(days=1)
            )
        )

        page = await list_history(db, uid, page=1, limit=10)

        assert page.total == 2
        assert [i.content_id for i in page.items] == [b, a]
        assert page.items[0].title == "乙"

    async def test_purge_respects_retention(
        self, db: AsyncSession, auth_db: AsyncSession
    ) -> None:
        uid = await _mk_user(auth_db)
        fresh = await _mk_item(db, uid, "新")
        stale = await _mk_item(db, uid, "旧")
        await record_view(db, uid, fresh)
        await record_view(db, uid, stale)
        await db.execute(
            sa_update(InteractionViewLog)
            .where(InteractionViewLog.content_id == stale)
            .values(
                viewed_at=datetime.datetime.now(datetime.UTC)
                - datetime.timedelta(days=200)
            )
        )

        removed = await purge_stale_view_logs(db, retention_days=90)

        assert removed == 1
        assert await _view_rows(db, uid, stale) == []
        assert len(await _view_rows(db, uid, fresh)) == 1
