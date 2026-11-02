"""content 域对外写入 API（``core.ports.content.publish_qa_item`` 的实现去处）。

跨域写入一律经端口，调用方（qa）不持有 content 的 ORM：本文件把「提问同步为论坛可见
条目」整段 SQL 与副作用留在属主域内，语义与拆分前逐字一致（同事务、发内容事件、计入
qa 类产出指标、bump content 集合版本）。
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

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
