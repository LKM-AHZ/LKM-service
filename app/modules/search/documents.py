"""内容行 → 索引文档（蓝图 §6.5.3 的字段集）。

单独一层的理由：**只有它需要读 ``content.models``**（import-linter 已豁免 search 只读内容
表），引擎层与同步层拿到的都是纯 dict，单测可用假引擎替换、无需起真容器。
"""

from __future__ import annotations

from typing import Any

from app.core.common import parse_tags
from app.modules.content.models import ContentItem
from app.modules.search.engines.base import IndexDoc


def _tag_list(raw: str | None) -> list[str]:
    return parse_tags(raw) if raw else []


def build_doc(item: ContentItem, author_name: str = "") -> IndexDoc:
    """把一条 ``content_items`` 行映射成索引文档。

    可选字段（summary/keywords/published_at/created_at）缺失时**不落 key**：Meilisearch 的
    filter/sort 与 OpenSearch 的 date mapping 对 ``null`` 的处理都比「缺字段」更易出意外，
    缺字段在两边的默认语义都是「不参与该维度」。
    """
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
