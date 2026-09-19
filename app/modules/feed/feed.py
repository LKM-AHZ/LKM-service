"""信息流数据源层：把异构内容实体归一为统一 FeedItem（原 timeline/feed.py）。

每个源实现 ``fetch_items(db, author_ids, board_ids, before_time, before_id, limit)``，
按可见时间降序返回当页候选（已按 cursor 下滤）。排序合并/游标推进在 service 统一做。

* 可见时间（feed_time）：优先 ``published``/``published_at``，否则 ``created_at``——
  即"内容对外可见的时间"，作为跨源排序锚点。
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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.articles.models import Article
from app.modules.auth.snapshot import get_user_snapshot_batch
from app.modules.content.models import (
    ColumnPost,
    ColumnPostStatus,
    ContentItem,
    ContentStatus,
    QAQuestion,
)
from app.modules.feed.schemas import FeedItem
from app.modules.projects.models import Project

_PREVIEW_LEN = 150


def _preview_of(text: str | None, limit: int = _PREVIEW_LEN) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "..."


async def _author_map(
    db: AsyncSession, user_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not user_ids:
        return {}
    snaps = await get_user_snapshot_batch(db, user_ids=list(user_ids))
    return {uid: s.display_name for uid, s in snaps.items()}


def _before_conds(
    col_time: Any,
    model_id: Any,
    before_time: datetime | None,
    before_id: uuid.UUID | None,
) -> list[Any]:
    """(created_at, id) 游标下滤条件。before_time 为 None 时返回空（首页）。"""
    if before_time is None:
        return []
    return [
        # created_at < before_time OR (created_at == before_time AND id < before_id)
        (col_time < before_time) | ((col_time == before_time) & (model_id < before_id))
    ]


def _after_conds(
    col_time: Any,
    model_id: Any,
    after_time: datetime,
    after_id: uuid.UUID | None,
) -> list[Any]:
    """(created_at, id) 水位上滤条件（M6.11 fanout 增量扫描用，升序配套）。

    与 :func:`_before_conds` 严格互补：``> after`` 或 ``== after 且 id >``。

    首次运行无水位（``after_id is None``）时退化为纯 ``col_time > after_time``——
    不能写成 ``model_id > NULL``：SQL 里该式恒为 NULL，会让「同一时刻的条目」被全部漏掉。
    """
    if after_id is None:
        return [col_time > after_time]
    return [
        (col_time > after_time) | ((col_time == after_time) & (model_id > after_id))
    ]


def _cursor_order(
    col_time: Any,
    model_id: Any,
    conditions: list[Any],
    before_time: datetime | None,
    before_id: uuid.UUID | None,
    after_time: datetime | None,
    after_id: uuid.UUID | None,
) -> tuple[Any, ...]:
    """按游标方向**就地**给 ``conditions`` 追加过滤，返回 ``order_by`` 元组。

    - 给 ``after_time`` → 升序增量扫描（M6.11 fanout 按源水位取新内容）；
    - 否则按 ``before_time`` 降序（读路径分页）。
    两方向互斥（调用方只传一个）；各源共用本函数以保证「同源两路条件永不漂移」。
    """
    if after_time is not None:
        conditions.extend(_after_conds(col_time, model_id, after_time, after_id))
        return (col_time.asc(), model_id.asc())
    if before_time is not None:
        conditions.extend(_before_conds(col_time, model_id, before_time, before_id))
    return (col_time.desc(), model_id.desc())


# ---------------------------------------------------------------------------
# Discussion（讨论帖，content_items 中 content_type==discussion）
# ---------------------------------------------------------------------------


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
    conditions: list[Any] = [
        ContentItem.content_type == "discussion",
        ContentItem.status == ContentStatus.PUBLISHED,
        # 内容软删（批 4）：实时合流兜底路径同样不得返回已删内容
        ContentItem.deleted_at.is_(None),
    ]
    # follow 模式：关注作者 或 关注版块；hot 模式不限制
    if author_ids is not None and board_ids is not None:
        conditions.append(
            ContentItem.author_id.in_(author_ids) | ContentItem.board_id.in_(board_ids)
        )
    order_by = _cursor_order(
        ContentItem.created_at,
        ContentItem.id,
        conditions,
        before_time,
        before_id,
        after_time,
        after_id,
    )
    stmt = select(ContentItem).where(*conditions).order_by(*order_by).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    # author_name 由 service 合流后统一批量填充（见 service.get_timeline 的 _fill_authors），
    # 避免同一作者在多源各查一次；此源只返回 author_id。
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
    conditions: list[Any] = [Article.status == "published"]
    order_by = _cursor_order(
        Article.created_at,
        Article.id,
        conditions,
        before_time,
        before_id,
        after_time,
        after_id,
    )
    stmt = select(Article).where(*conditions).order_by(*order_by).limit(limit)
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


def _article_heat(r: Article) -> float:
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
    conditions: list[Any] = [ColumnPost.status == ColumnPostStatus.PUBLISHED]
    if author_ids is not None:
        conditions.append(ColumnPost.author_id.in_(author_ids))
    order_by = _cursor_order(
        ColumnPost.created_at,
        ColumnPost.id,
        conditions,
        before_time,
        before_id,
        after_time,
        after_id,
    )
    stmt = select(ColumnPost).where(*conditions).order_by(*order_by).limit(limit)
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


def _column_heat(r: ColumnPost) -> float:
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
    conditions: list[Any] = [QAQuestion.status.in_(["open", "accepted"])]
    if author_ids is not None:
        conditions.append(QAQuestion.author_id.in_(author_ids))
    order_by = _cursor_order(
        QAQuestion.created_at,
        QAQuestion.id,
        conditions,
        before_time,
        before_id,
        after_time,
        after_id,
    )
    stmt = select(QAQuestion).where(*conditions).order_by(*order_by).limit(limit)
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
    conditions: list[Any] = [Project.status == "active"]
    if author_ids is not None:
        conditions.append(Project.applicant_id.in_(author_ids))
    order_by = _cursor_order(
        Project.created_at,
        Project.id,
        conditions,
        before_time,
        before_id,
        after_time,
        after_id,
    )
    stmt = select(Project).where(*conditions).order_by(*order_by).limit(limit)
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
    conditions: list[Any] = [
        ContentItem.content_type == "blog_post",
        ContentItem.status == ContentStatus.PUBLISHED,
        ContentItem.deleted_at.is_(None),  # 软删（批 4）
    ]
    if author_ids is not None:
        conditions.append(ContentItem.author_id.in_(author_ids))
    order_by = _cursor_order(
        ContentItem.created_at,
        ContentItem.id,
        conditions,
        before_time,
        before_id,
        after_time,
        after_id,
    )
    stmt = select(ContentItem).where(*conditions).order_by(*order_by).limit(limit)
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
