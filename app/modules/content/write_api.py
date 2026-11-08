"""content 域对外写入 API（``core.ports.content.publish_qa_item`` 的实现去处）。

跨域写入一律经端口，调用方（qa）不持有 content 的 ORM：本文件把「提问同步为论坛可见
条目」整段 SQL 与副作用留在属主域内，语义与拆分前逐字一致（同事务、发内容事件、计入
qa 类产出指标、bump content 集合版本）。
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.content.counters import bump_content_counter
from app.modules.content.errors import ContentErr
from app.modules.content.events import enqueue_content_event
from app.modules.content.models import ContentItem
from app.modules.content.repository import BoardRepository, ContentItemRepository
from core.cache import bump_collection_version
from core.metrics import post_created_total


async def _ensure_qa_board(db: AsyncSession) -> uuid.UUID:
    """确保存在 qa 板块（统一分类轴），QA 提问的论坛条目挂到它。"""
    repo = BoardRepository(db)
    existing = await repo.get_by_slug("qa")
    if existing is not None:
        return existing.id
    board = await repo.create(slug="qa", title="问答", description="用户提问与解答")
    return board.id


async def publish_qa_item(
    db: AsyncSession,
    *,
    author_id: uuid.UUID,
    question_id: uuid.UUID,
    title: str,
    content: str,
    excerpt: str,
) -> None:
    """QA 提问同步为论坛可见条目（content_items，content_type='qa'）。"""
    board_id = await _ensure_qa_board(db)
    item = ContentItem(
        content_type="qa",
        board_id=board_id,
        author_id=author_id,
        qa_question_id=question_id,
        title=title,
        excerpt=excerpt,
        content=content,
        tags="[]",
        status="published",
    )
    await ContentItemRepository(db).add(item)
    await enqueue_content_event(db, item)
    # M0.5.2：QA 提问同步为论坛可见的 content_items 条目，视作一次 qa 类产出。
    post_created_total.labels("qa").inc()
    # QA 提问同步落成 content_items 会增加统一内容列表的计数，需 bump content 集合版本
    await bump_collection_version("content")


async def require_content_item(db: AsyncSession, item_id: uuid.UUID) -> None:
    """断言内容项存在且未软删（评论读写前的前置检查）。"""
    await ContentItemRepository(db).get_or_raise(item_id, ContentErr.CONTENT_NOT_FOUND)


async def lock_content_item(db: AsyncSession, item_id: uuid.UUID) -> None:
    """锁住内容项行并断言存在：同帖并发互动被串行化，软删后不再接受新互动。"""
    await ContentItemRepository(db).lock_active(item_id)


async def bump_comment_count(db: AsyncSession, item_id: uuid.UUID, delta: int) -> int:
    """帖子评论数写穿加减（``reconcile_counts`` 的在线一侧）。"""
    return await bump_content_counter(db, item_id, "comment_count", delta)


async def ensure_board(db: AsyncSession, slug: str) -> uuid.UUID:
    """按 slug 取板块 id，不存在则原子创建（博客发布等跨域路径用）。"""
    return await BoardRepository(db).ensure_by_slug(slug)


async def publish_blog_item(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    board_id: uuid.UUID,
    slug: str | None,
    title: str,
    content: str,
    summary: str | None,
    cover: str | None,
    tags: list[str],
) -> uuid.UUID:
    """把博客发布产物落成一条 ``content_items``（content_type=blog_post）。"""
    from app.modules.content.service import publish_blog_item as _publish_blog_item

    return await _publish_blog_item(
        db,
        owner_id,
        board_id=board_id,
        slug=slug,
        title=title,
        content=content,
        summary=summary,
        cover=cover,
        tags=tags,
    )
