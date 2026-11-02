"""信息流数据源层：把异构内容实体归一为统一 FeedItem（原 timeline/feed.py）。

每个源实现 ``fetch_items(db, author_ids, board_ids, before_time, before_id, limit)``，
按可见时间降序返回当页候选（已按 cursor 下滤）。排序合并/游标推进在 service 统一做。

* 排序锚点：各源当前统一用 ``created_at``（含游标与 ``FeedItem.created_at``，fanout 水位
  也取自它）。**已知与早期设计不一致**：本文档原写「优先 published/published_at，否则
  created_at」的可见时间锚，实际未实现——先存草稿后发布的内容会按创建时间排序、且可能落在
  fanout 水位之前而不被物化（只能靠实时兜底看到）。是否改为 coalesce(published_at,
  created_at) 属排序/水位语义的产品决定，待定。
* 可见性过滤（各源 SQL WHERE）：Article 仅 published、Column 仅 PUBLISHED、
  QA 仅 open/accepted、Project 仅 active、Discussion 仅 status=PUBLISHED。
* 作者名：逐源批查 ``User.profile.nickname`` 兜底 ``username``；Article 无作者外键，
  ``author_id=None``、``author_name=publisher``。
"""

from __future__ import annotations

import math
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.feed.schemas import FeedItem
from core.ports import columns as columns_port
from core.ports import content as content_port
from core.ports import projects as projects_port
from core.ports import qa as qa_port
from core.ports.snapshot import get_user_display_names as _author_map

_PREVIEW_LEN = 150


def _preview_of(text: str | None, limit: int = _PREVIEW_LEN) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "..."


async def _fetch_discussion(
    db: AsyncSession,
    author_ids: set[uuid.UUID] | None,
    board_ids: set[uuid.UUID] | None,
    before_time: datetime | None,
    before_id: uuid.UUID | None,
    limit: int,
    after_time: datetime | None = None,
    after_id: uuid.UUID | None = None,
) -> list[FeedItem]:
    stmt = stmt = content_port.select_feed_source(
        "discussion",
        author_ids=author_ids,
        board_ids=board_ids,
        before_time=before_time,
        before_id=before_id,
        after_time=after_time,
        after_id=after_id,
        limit=limit,
    )

    rows = (await db.execute(stmt)).scalars().all()
    return [
        FeedItem(
            item_type="discussion",
            id=r.id,
            author_id=r.author_id,
            author_name="",
            title=r.title,
            content_preview=_preview_of(r.excerpt or r.content),
            created_at=r.created_at,
            sort_score=_discussion_heat(r),
            board_id=r.board_id,
            url=f"/content/posts/{r.id}",
        )
        for r in rows
    ]


def _discussion_heat(r: Any) -> float:
    """讨论热度：读写阅赞评藏加权。"""
    return math.log1p(
        r.view_count + r.like_count * 2 + r.comment_count * 3 + r.bookmark_count * 4
    )


# ---------------------------------------------------------------------------
# Article（无作者外键：仅 hot，follow 时不参与）
# ---------------------------------------------------------------------------


async def _fetch_article(
    db: AsyncSession,
    author_ids: set[uuid.UUID] | None,
    board_ids: set[uuid.UUID] | None,
    before_time: datetime | None,
    before_id: uuid.UUID | None,
    limit: int,
    after_time: datetime | None = None,
    after_id: uuid.UUID | None = None,
) -> list[FeedItem]:
    stmt = stmt = content_port.select_feed_source(
        "article",
        author_ids=author_ids,
        board_ids=board_ids,
        before_time=before_time,
        before_id=before_id,
        after_time=after_time,
        after_id=after_id,
        limit=limit,
    )

    rows = (await db.execute(stmt)).scalars().all()
    return [
        FeedItem(
            item_type="article",
            id=r.id,
            author_id=None,
            author_name=r.publisher or "",
            title=r.title,
            content_preview=_preview_of(r.description or r.content),
            created_at=r.created_at,
            sort_score=_article_heat(r),
            board_id=None,
            url=f"/articles/{r.slug}",
        )
        for r in rows
    ]


def _article_heat(r: Any) -> float:
    return math.log1p(r.views + r.likes * 2 + r.comments * 3 + r.bookmarks * 4)


# ---------------------------------------------------------------------------
# Column
# ---------------------------------------------------------------------------


async def _fetch_column(
    db: AsyncSession,
    author_ids: set[uuid.UUID] | None,
    board_ids: set[uuid.UUID] | None,
    before_time: datetime | None,
    before_id: uuid.UUID | None,
    limit: int,
    after_time: datetime | None = None,
    after_id: uuid.UUID | None = None,
) -> list[FeedItem]:
    stmt = stmt = columns_port.select_feed_source(
        author_ids=author_ids,
        board_ids=board_ids,
        before_time=before_time,
        before_id=before_id,
        after_time=after_time,
        after_id=after_id,
        limit=limit,
    )

    rows = (await db.execute(stmt)).scalars().all()
    return [
        FeedItem(
            item_type="column",
            id=r.id,
            author_id=r.author_id,
            author_name="",  # service 合流统一填充
            title=r.title,
            content_preview=_preview_of(r.summary or r.content),
            created_at=r.created_at,
            sort_score=_column_heat(r),
            board_id=None,
            url=f"/columns/{r.column_id}/posts/{r.id}",
        )
        for r in rows
    ]


def _column_heat(r: Any) -> float:
    return math.log1p(r.view_count + r.like_count * 2 + r.comment_count * 3)


# ---------------------------------------------------------------------------
# QA
# ---------------------------------------------------------------------------


async def _fetch_qa(
    db: AsyncSession,
    author_ids: set[uuid.UUID] | None,
    board_ids: set[uuid.UUID] | None,
    before_time: datetime | None,
    before_id: uuid.UUID | None,
    limit: int,
    after_time: datetime | None = None,
    after_id: uuid.UUID | None = None,
) -> list[FeedItem]:
    stmt = stmt = qa_port.select_feed_source(
        author_ids=author_ids,
        board_ids=board_ids,
        before_time=before_time,
        before_id=before_id,
        after_time=after_time,
        after_id=after_id,
        limit=limit,
    )

    rows = (await db.execute(stmt)).scalars().all()
    return [
        FeedItem(
            item_type="qa",
            id=r.id,
            author_id=r.author_id,
            author_name="",  # service 合流统一填充
            title=r.title,
            content_preview=_preview_of(r.content or r.situation),
            created_at=r.created_at,
            sort_score=0.0,
            board_id=None,
            url=f"/qa/{r.id}",
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


async def _fetch_project(
    db: AsyncSession,
    author_ids: set[uuid.UUID] | None,
    board_ids: set[uuid.UUID] | None,
    before_time: datetime | None,
    before_id: uuid.UUID | None,
    limit: int,
    after_time: datetime | None = None,
    after_id: uuid.UUID | None = None,
) -> list[FeedItem]:
    stmt = stmt = projects_port.select_feed_source(
        author_ids=author_ids,
        board_ids=board_ids,
        before_time=before_time,
        before_id=before_id,
        after_time=after_time,
        after_id=after_id,
        limit=limit,
    )

    rows = (await db.execute(stmt)).scalars().all()
    return [
        FeedItem(
            item_type="project",
            id=r.id,
            author_id=r.applicant_id,
            author_name="",  # service 合流统一填充
            title=r.title,
            content_preview=_preview_of(r.summary or r.description),
            created_at=r.created_at,
            sort_score=0.0,
            board_id=None,
            url=f"/projects/{r.id}",
        )
        for r in rows
    ]


async def _fetch_blog(
    db: AsyncSession,
    author_ids: set[uuid.UUID] | None,
    board_ids: set[uuid.UUID] | None,
    before_time: datetime | None,
    before_id: uuid.UUID | None,
    limit: int,
    after_time: datetime | None = None,
    after_id: uuid.UUID | None = None,
) -> list[FeedItem]:
    """博客发布产物（统一内容表中 content_type==blog_post）。

    blog_post 只落 content_items、无独立分表源（forum/article/column/qa 走各自旧表），
    故单独从 content_items 补源，避免与其他源重复。
    """
    stmt = stmt = content_port.select_feed_source(
        "blog",
        author_ids=author_ids,
        board_ids=board_ids,
        before_time=before_time,
        before_id=before_id,
        after_time=after_time,
        after_id=after_id,
        limit=limit,
    )

    rows = (await db.execute(stmt)).scalars().all()
    names = await _author_map(db, {r.author_id for r in rows if r.author_id})
    return [
        FeedItem(
            item_type="blog",
            id=r.id,
            author_id=r.author_id,
            author_name=(
                names.get(r.author_id, r.publisher or "")
                if r.author_id is not None
                else (r.publisher or "")
            ),
            title=r.title,
            content_preview=_preview_of(r.excerpt or r.content),
            created_at=r.created_at,
            sort_score=0.0,
            board_id=r.board_id,
            url=f"/blog/posts/{r.slug}" if r.slug else f"/content/boards/{r.board_id}",
        )
        for r in rows
    ]


SOURCES: dict[str, Any] = {
    "discussion": _fetch_discussion,
    "article": _fetch_article,
    "column": _fetch_column,
    "qa": _fetch_qa,
    "project": _fetch_project,
    "blog": _fetch_blog,
}

# follow 模式参与的源（按关注作者过滤；discussion 额外按关注版块）
FOLLOW_SOURCES: list[str] = ["discussion", "column", "qa", "project", "blog"]
# hot 模式参与的源（全站、不按关注过滤、包含无作者外键的 Article）
HOT_SOURCES: list[str] = ["discussion", "article", "column", "qa", "project", "blog"]
