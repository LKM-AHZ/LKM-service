"""同一内容的互动请求在独立数据库连接上保持幂等和楼层唯一。"""

import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.modules.content import service as content_service
from app.modules.content.models import Board, ContentComment, ContentItem, ContentLike
from app.modules.content.schemas import ContentCommentCreate


async def _item(db: AsyncSession) -> uuid.UUID:
    board = Board(slug=f"concurrent-{uuid.uuid4()}", title="并发测试", description="")
    db.add(board)
    await db.flush()
    item = ContentItem(
        content_type="discussion",
        board_id=board.id,
        title="并发测试",
        content="正文",
        status="published",
    )
    db.add(item)
    await db.commit()
    return item.id


async def test_concurrent_duplicate_like_counts_once(db: AsyncSession) -> None:
    item_id = await _item(db)
    user_id = uuid.uuid4()
    assert isinstance(db.bind, AsyncEngine)
    engine = create_async_engine(db.bind.url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    first_done = asyncio.Event()
    release_first = asyncio.Event()
    second_ready = asyncio.Event()

    async def first() -> int:
        async with maker() as session:
            count = await content_service.like_item(session, item_id, user_id)
            first_done.set()
            await release_first.wait()
            await session.commit()
            return count

    async def second() -> int:
        async with maker() as session:
            await session.connection()
            second_ready.set()
            count = await content_service.like_item(session, item_id, user_id)
            await session.commit()
            return count

    first_task = asyncio.create_task(first())
    try:
        await asyncio.wait_for(first_done.wait(), 5)
        second_task = asyncio.create_task(second())
        await asyncio.wait_for(second_ready.wait(), 5)
        await asyncio.sleep(0.05)
        assert not second_task.done(), "第二个请求应等待首个事务提交"
        release_first.set()
        assert await asyncio.wait_for(asyncio.gather(first_task, second_task), 5) == [
            1,
            1,
        ]
        async with maker() as session:
            item_count = await session.scalar(
                select(ContentItem.like_count).where(ContentItem.id == item_id)
            )
            likes = await session.scalar(
                select(func.count())
                .select_from(ContentLike)
                .where(ContentLike.content_id == item_id)
            )
        assert item_count == likes == 1
    finally:
        release_first.set()
        if not first_task.done():
            await first_task
        await engine.dispose()


async def test_concurrent_comments_get_distinct_floors(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    item_id = await _item(db)
    assert isinstance(db.bind, AsyncEngine)
    engine = create_async_engine(db.bind.url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    first_done = asyncio.Event()
    release_first = asyncio.Event()
    second_ready = asyncio.Event()

    async def no_names(_db: AsyncSession, _ids: list[uuid.UUID]) -> dict:
        return {}

    monkeypatch.setattr(content_service, "_author_map", no_names)

    async def first() -> int:
        async with maker() as session:
            comment = await content_service.create_comment(
                session, item_id, uuid.uuid4(), ContentCommentCreate(content="一楼")
            )
            first_done.set()
            await release_first.wait()
            await session.commit()
            return comment.floor_number

    async def second() -> int:
        async with maker() as session:
            await session.connection()
            second_ready.set()
            comment = await content_service.create_comment(
                session, item_id, uuid.uuid4(), ContentCommentCreate(content="二楼")
            )
            await session.commit()
            return comment.floor_number

    first_task = asyncio.create_task(first())
    try:
        await asyncio.wait_for(first_done.wait(), 5)
        second_task = asyncio.create_task(second())
        await asyncio.wait_for(second_ready.wait(), 5)
        await asyncio.sleep(0.05)
        assert not second_task.done(), "第二个请求应等待首个事务提交"
        release_first.set()
        assert await asyncio.wait_for(asyncio.gather(first_task, second_task), 5) == [
            1,
            2,
        ]
        async with maker() as session:
            floors = (
                await session.scalars(
                    select(ContentComment.floor_number)
                    .where(ContentComment.content_id == item_id)
                    .order_by(ContentComment.floor_number)
                )
            ).all()
            count = await session.scalar(
                select(ContentItem.comment_count).where(ContentItem.id == item_id)
            )
        assert floors == [1, 2]
        assert count == 2
    finally:
        release_first.set()
        if not first_task.done():
            await first_task
        await engine.dispose()
