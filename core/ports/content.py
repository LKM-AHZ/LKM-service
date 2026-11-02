"""content 域端口：内容摘要（读）与问答条目落库（写）。

notification 解析行为事件时要回查内容的标题与作者，原先直接读 ``content.models``。
摘要形态由 content 域定义（``read_api`` 的数据类），调用方只读字段，不持有 ORM 实例。

``publish_qa_item`` 是唯一一项写入：步骤 9 把问答拆成独立域后，「提问同步为论坛可见条目」
不再能在 qa 里直接写 ``content_items``，改为由 content 提供同事务写入口（``content_write``）。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.ports.registry import get


async def item_brief(db: AsyncSession, item_id: uuid.UUID) -> Any:
    """未软删的内容项摘要（``id`` / ``title`` / ``author_id``）；不存在返回 None。"""
    return await get("content_read").item_brief(db, item_id)


async def qa_answer_brief(db: AsyncSession, answer_id: uuid.UUID) -> Any:
    """问答回答摘要（``id`` / ``question_id`` / ``author_id``）。"""
    return await get("content_read").qa_answer_brief(db, answer_id)


async def qa_question_brief(db: AsyncSession, question_id: uuid.UUID) -> Any:
    """问答问题摘要（``id`` / ``title`` / ``bounty_per_person``）。"""
    return await get("content_read").qa_question_brief(db, question_id)


async def count_discussions(db: AsyncSession) -> int:
    """讨论帖总数（不含软删）。"""
    return await get("content_read").count_discussions(db)


async def daily_discussion_counts(
    db: AsyncSession, start: Any, days: int
) -> dict[Any, int]:
    """讨论帖按日增量（UTC 日期分桶）。"""
    return await get("content_read").daily_discussion_counts(db, start, days)


async def pending_board_applications(
    db: AsyncSession,
) -> tuple[int, Any]:
    """待审版块申请：``(数量, 最早提交时间)``。"""
    return await get("content_read").pending_board_applications(db)


def search_select(term: str, content_type: str | None) -> Any:
    """内容侧检索分支：``(id, source, rank, created_at)``，供 search 做 UNION。"""
    return get("content_read").search_select(term, content_type)


async def search_count(db: AsyncSession, term: str, content_type: str | None) -> int:
    """内容命中数（PG 路径分页总数）。"""
    return await get("content_read").search_count(db, term, content_type)


def select_matching(
    term: str, content_type: str | None, *, offset: int, limit: int
) -> Any:
    """PG 路径的命中行（未执行）。"""
    return get("content_read").select_matching(
        term, content_type, offset=offset, limit=limit
    )


def select_published_by_ids(ids: list[uuid.UUID]) -> Any:
    """按 id 批量取已发布行（未执行）。"""
    return get("content_read").select_published_by_ids(ids)


async def index_doc(db: AsyncSession, item_id: uuid.UUID) -> Any:
    """可索引则返回索引文档，否则 None。"""
    return await get("content_read").index_doc(db, item_id)


async def index_docs_batch(db: AsyncSession, *, after_id: uuid.UUID, limit: int) -> Any:
    """按 id 升序取一窗已发布文档（全量重建索引）。"""
    return await get("content_read").index_docs_batch(
        db, after_id=after_id, limit=limit
    )


def content_brief_view() -> Any:
    """内容摘要的可 join 子查询（``id``/``content_type``/``title``/``slug``/``board_id``）。"""
    return get("content_read").content_brief_view()


def board_brief_view() -> Any:
    """板块摘要的可 join 子查询（``id``/``title``）。"""
    return get("content_read").board_brief_view()


async def bookmark_count(db: AsyncSession, content_id: uuid.UUID) -> Any:
    """当前收藏计数；行不存在（含软删）返回 None。"""
    return await get("content_read").bookmark_count(db, content_id)


async def bump_bookmark_count(
    db: AsyncSession, content_id: uuid.UUID, delta: int
) -> Any:
    """原子增减 ``bookmark_count`` 并返回即时读数。"""
    return await get("content_read").bump_bookmark_count(db, content_id, delta)


async def exists_content(db: AsyncSession, content_id: uuid.UUID) -> bool:
    """内容行是否存在。"""
    return await get("content_read").exists_content(db, content_id)


async def item_counts(
    db: AsyncSession, content_id: uuid.UUID
) -> tuple[int, int] | None:
    """``(like_count, bookmark_count)``。"""
    return await get("content_read").item_counts(db, content_id)


async def liked_by(db: AsyncSession, content_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """该用户是否点赞过该内容。"""
    return await get("content_read").liked_by(db, content_id, user_id)


def select_feed_source(
    kind: str,
    *,
    author_ids: Any,
    board_ids: Any,
    before_time: Any,
    before_id: Any,
    after_time: Any,
    after_id: Any,
    limit: int,
) -> Any:
    """按 kind（discussion/article/column/qa/blog）取一窗时间线源行（未执行）。"""
    return get("content_read").select_feed_source(
        kind,
        author_ids=author_ids,
        board_ids=board_ids,
        before_time=before_time,
        before_id=before_id,
        after_time=after_time,
        after_id=after_id,
        limit=limit,
    )


async def publish_qa_item(
    db: AsyncSession,
    *,
    author_id: uuid.UUID,
    question_id: uuid.UUID,
    title: str,
    content: str,
    excerpt: str,
) -> Any:
    """把一条提问同步为论坛可见条目（``content_type='qa'``，同事务）。"""
    return await get("content_write").publish_qa_item(
        db,
        author_id=author_id,
        question_id=question_id,
        title=title,
        content=content,
        excerpt=excerpt,
    )


async def require_content_item(db: AsyncSession, item_id: uuid.UUID) -> Any:
    """断言内容项存在且未软删（不存在抛 content 域的 CONTENT_NOT_FOUND）。"""
    return await get("content_write").require_content_item(db, item_id)


async def lock_content_item(db: AsyncSession, item_id: uuid.UUID) -> Any:
    """锁住内容项行（同事务串行化同帖互动），已软删/不存在抛 CONTENT_NOT_FOUND。"""
    return await get("content_write").lock_content_item(db, item_id)


async def bump_comment_count(db: AsyncSession, item_id: uuid.UUID, delta: int) -> int:
    """把帖子的 ``comment_count`` 写穿加减（下限 0），返回新值。"""
    return await get("content_write").bump_comment_count(db, item_id, delta)


async def get_item_info(db: AsyncSession, item_id: uuid.UUID) -> Any:
    """内容项完整只读视图（``app.contracts.content.ContentItemInfo``）。"""
    return await get("content_read").get_item_info(db, item_id)


async def ensure_board(db: AsyncSession, slug: str) -> uuid.UUID:
    """按 slug 取板块 id，不存在则原子创建（博客发布用）。"""
    return await get("content_write").ensure_board(db, slug)


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
    """把博客发布产物落成一条 ``content_items``（同事务，幂等）。"""
    return await get("content_write").publish_blog_item(
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


async def find_board_id(db: AsyncSession, slug: str) -> uuid.UUID | None:
    """按 slug 查板块 id（只读，不存在返回 None）；不自动建。"""
    return await get("content_read").find_board_id(db, slug)
