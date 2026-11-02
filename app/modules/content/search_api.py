"""content 域的检索投影：索引文档、FTS 查询分支。

search 模块不再读 ``content.models``：文档形状与可见性口径（已发布、未软删）由 content
自己给出，search 只负责跨域合并、排序与引擎交互。
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.contracts.search import IndexDoc
from app.modules.content.models import ContentItem, ContentStatus
from core.common import parse_tags
from core.ports.snapshot import get_user_display_names, get_user_snapshot_batch

#: ILIKE 子串匹配的转义字符（与 search 侧一致）。
_LIKE_ESCAPE = "\\"


def _like_pattern(term: str) -> str:
    escaped = (
        term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"


def _tag_list(raw: str | None) -> list[str]:
    return parse_tags(raw) if raw else []


def build_doc(item: ContentItem, author_name: str = "") -> IndexDoc:
    """把一条 ``content_items`` 行映射成索引文档（可选字段缺失时不落 key）。"""
    doc: dict[str, Any] = {
        "id": str(item.id),
        "content_type": item.content_type,
        "board_id": str(item.board_id),
        "title": item.title or "",
        "excerpt": item.excerpt or "",
        "content": item.content or "",
        "tags": _tag_list(item.tags),
        "author_id": str(item.author_id) if item.author_id else "",
        "author_name": author_name,
    }
    if item.summary:
        doc["summary"] = item.summary
    if item.keywords:
        doc["keywords"] = _tag_list(item.keywords)
    if item.published_at is not None:
        doc["published_at"] = item.published_at.isoformat()
    if item.created_at is not None:
        doc["created_at"] = item.created_at.isoformat()
    return doc


def _match(term: str, content_type: str | None) -> tuple[Any, list[Any]]:
    """``(tsquery, 谓词)``：tsvector 命中或 ILIKE 子串命中，可叠加内容类型过滤。"""
    query = func.websearch_to_tsquery("simple", term)
    fts = ContentItem.search_vector.bool_op("@@")(query)
    pattern = _like_pattern(term)
    contains = or_(
        ContentItem.title.ilike(pattern, escape=_LIKE_ESCAPE),
        ContentItem.excerpt.ilike(pattern, escape=_LIKE_ESCAPE),
        ContentItem.content.ilike(pattern, escape=_LIKE_ESCAPE),
        ContentItem.summary.ilike(pattern, escape=_LIKE_ESCAPE),
        ContentItem.keywords.ilike(pattern, escape=_LIKE_ESCAPE),
        ContentItem.tags.ilike(pattern, escape=_LIKE_ESCAPE),
    )
    conditions: list[Any] = [
        ContentItem.status == ContentStatus.PUBLISHED,
        or_(fts, contains),
    ]
    if content_type:
        conditions.append(ContentItem.content_type == content_type)
    return query, conditions


def search_select(term: str, content_type: str | None) -> Select[Any]:
    """内容侧的检索分支：``(id, source, rank, created_at)``，供 search 做 UNION。"""
    query, conditions = _match(term, content_type)
    return select(
        ContentItem.id.label("id"),
        literal("content").label("source"),
        func.ts_rank(ContentItem.search_vector, query).label("rank"),
        ContentItem.created_at.label("created_at"),
    ).where(*cast(Any, conditions))


async def count_matching(
    db: AsyncSession, *, term: str, content_type: str | None = None
) -> int:
    _, conditions = _match(term, content_type)
    return int(
        await db.scalar(
            select(func.count())
            .select_from(ContentItem)
            .where(*cast(Any, conditions), ContentItem.deleted_at.is_(None))
        )
        or 0
    )


async def _display_name(db: AsyncSession, author_id: uuid.UUID | None) -> str:
    if author_id is None:
        return ""
    snaps = await get_user_snapshot_batch(db, user_ids=[author_id])
    snap = snaps.get(author_id)
    return snap.display_name if snap else ""


async def index_doc(db: AsyncSession, item_id: uuid.UUID) -> IndexDoc | None:
    """可索引则返回文档，否则 ``None``（已软删 / 未发布）。"""
    item = await db.get(ContentItem, item_id)
    if (
        item is None
        or item.deleted_at is not None
        or item.status != ContentStatus.PUBLISHED
    ):
        return None
    return build_doc(item, await _display_name(db, item.author_id))


async def index_docs_batch(
    db: AsyncSession, *, after_id: uuid.UUID, limit: int
) -> tuple[list[IndexDoc], uuid.UUID]:
    """按 id 升序取一窗已发布行并映射成文档（全量重建索引）。"""
    rows = (
        (
            await db.execute(
                select(ContentItem)
                .where(
                    ContentItem.id > after_id,
                    ContentItem.status == ContentStatus.PUBLISHED,
                    ContentItem.deleted_at.is_(None),
                )
                .order_by(ContentItem.id)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return [], after_id
    names = await get_user_display_names(
        db, {r.author_id for r in rows if r.author_id}
    )
    docs = [
        build_doc(r, names.get(r.author_id, "") if r.author_id else "") for r in rows
    ]
    return docs, rows[-1].id


def select_published_by_ids(ids: list[uuid.UUID]) -> Select[Any]:
    """按 id 批量取**已发布**行（引擎命中后的权威回填）。"""
    return select(ContentItem).where(
        ContentItem.id.in_(ids),
        ContentItem.status == ContentStatus.PUBLISHED,
        ContentItem.deleted_at.is_(None),
    )


def select_matching(
    term: str,
    content_type: str | None,
    *,
    offset: int = 0,
    limit: int = 20,
) -> Select[Any]:
    """PG 路径的命中行：按 ``ts_rank`` 倒序、再按 id 倒序分页。"""
    query, conditions = _match(term, content_type)
    rank = func.ts_rank(ContentItem.search_vector, query)
    return (
        select(ContentItem)
        .where(*cast(Any, conditions), ContentItem.deleted_at.is_(None))
        .order_by(rank.desc().nulls_last(), ContentItem.id.desc())
        .offset(offset)
        .limit(limit)
    )
