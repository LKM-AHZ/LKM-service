"""content 域的时间线源查询（供 feed 读取）。

六个源里有五个由 content 提供；查询条件与游标语义留在属主域，feed 只做
``FeedItem`` 映射与热度计算。这样 feed 不再 import 任何内容/博客模型。
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.sql import Select

from app.contracts.feed import cursor_order
from app.modules.content.articles.models import Article
from app.modules.content.models import (
    ContentItem,
    ContentStatus,
)


def _discussion_conditions(
    author_ids: set[uuid.UUID] | None, board_ids: set[uuid.UUID] | None
) -> list[Any]:
    conditions: list[Any] = [
        ContentItem.content_type == "discussion",
        ContentItem.status == ContentStatus.PUBLISHED,
        ContentItem.deleted_at.is_(None),
    ]
    if author_ids is not None:
        author_cond = ContentItem.author_id.in_(author_ids)
        if board_ids:
            author_cond = author_cond | ContentItem.board_id.in_(board_ids)
        conditions.append(author_cond)
    return conditions


def _blog_conditions(
    author_ids: set[uuid.UUID] | None, board_ids: set[uuid.UUID] | None
) -> list[Any]:
    conditions: list[Any] = [
        ContentItem.content_type == "blog_post",
        ContentItem.status == ContentStatus.PUBLISHED,
        ContentItem.deleted_at.is_(None),
    ]
    if author_ids is not None:
        conditions.append(ContentItem.author_id.in_(author_ids))
    return conditions


def _article_conditions(
    author_ids: set[uuid.UUID] | None, board_ids: set[uuid.UUID] | None
) -> list[Any]:
    return [Article.status == "published"]


#: kind → (模型, 条件构造函数)。feed 的 SOURCES 键与此一一对应。
_SOURCES: dict[str, tuple[Any, Any]] = {
    "discussion": (ContentItem, _discussion_conditions),
    "blog": (ContentItem, _blog_conditions),
    "article": (Article, _article_conditions),
}


def select_source(
    kind: str,
    *,
    author_ids: set[uuid.UUID] | None,
    board_ids: set[uuid.UUID] | None,
    before_time: datetime.datetime | None,
    before_id: uuid.UUID | None,
    after_time: datetime.datetime | None,
    after_id: uuid.UUID | None,
    limit: int,
) -> Select[Any]:
    """按 kind 取一窗源行（未执行）。"""
    model, build_conditions = _SOURCES[kind]
    conditions = build_conditions(author_ids, board_ids)
    order_by = cursor_order(
        model.created_at,
        model.id,
        conditions,
        before_time,
        before_id,
        after_time,
        after_id,
    )
    return select(model).where(*conditions).order_by(*order_by).limit(limit)
