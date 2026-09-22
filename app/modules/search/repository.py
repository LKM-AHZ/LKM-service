"""search 域的仓储子类：PG FTS + pg_trgm 双路只读检索。

两条路互补（口径见 service 模块 docstring）：

- **tsvector 路**：``content_items.search_vector`` 生成列 + ``ts_rank`` 相关度；
- **pg_trgm 路**：``ILIKE '%词%'`` 子串匹配（中文实际靠这一路）。

跨模块只读内容表的 import 缝由 ``search.repository -> content.models`` 承接
（原豁免在 ``search.service -> content.models``）；LIKE 通配符转义也随之收在此处，
避免用户输入的 ``%`` / ``_`` 被当通配符。
"""

from __future__ import annotations

from sqlalchemy import func, or_

from app.db.repository import AsyncRepository
from app.modules.content.models import ContentItem, ContentStatus

# LIKE 通配符转义：用户输入里的 % / _ / \ 必须按字面处理，否则 ``%`` 会命中全表。
_LIKE_ESCAPE = "\\"


def _like_pattern(term: str) -> str:
    escaped = (
        term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"


class SearchRepository(AsyncRepository[ContentItem]):
    """``content_items`` 的只读检索（仅已发布内容，匿名口径与内容列表一致）。"""

    model = ContentItem

    @staticmethod
    def _match(term: str, content_type: str | None) -> tuple[object, list[object]]:
        """返回 ``(tsquery, 谓词)``：tsvector 命中或 ILIKE 子串命中，可叠加内容类型过滤。"""
        query = func.websearch_to_tsquery("simple", term)
        fts = ContentItem.search_vector.bool_op("@@")(query)
        pattern = _like_pattern(term)
        # 列集合与 SEARCH_VECTOR_SQL（title/excerpt/content/summary/keywords/tags）对齐：
        # 中文子串只走这一路，少了 summary/keywords/tags 会出现「tsvector 有词、ILIKE 却
        # 匹配不到」的整类漏检（中文命中只落在这三列时彻底搜不到）
        contains = or_(
            ContentItem.title.ilike(pattern, escape=_LIKE_ESCAPE),
            ContentItem.excerpt.ilike(pattern, escape=_LIKE_ESCAPE),
            ContentItem.content.ilike(pattern, escape=_LIKE_ESCAPE),
            ContentItem.summary.ilike(pattern, escape=_LIKE_ESCAPE),
            ContentItem.keywords.ilike(pattern, escape=_LIKE_ESCAPE),
            ContentItem.tags.ilike(pattern, escape=_LIKE_ESCAPE),
        )
        conditions: list[object] = [
            ContentItem.status == ContentStatus.PUBLISHED,
            or_(fts, contains),
        ]
        if content_type:
            conditions.append(ContentItem.content_type == content_type)
        return query, conditions

    async def count_matching(
        self, *, term: str, content_type: str | None = None
    ) -> int:
        _, conditions = self._match(term, content_type)
        return await self.count(*conditions)

    async def list_matching(
        self,
        *,
        term: str,
        content_type: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[ContentItem]:
        """命中项按 ``ts_rank`` 倒序，再按 id 倒序。

        纯 ILIKE 命中（tsvector 不匹配）的 ``ts_rank`` 是 **0.0 而非 NULL**，
        故排在所有正 rank 之后靠的是 0 < 正分的取值关系、不是 ``nulls_last()``；
        该修饰符只对「向量为 NULL」起防御作用（DESC 下 PG 默认 NULL 排前）。
        """
        query, conditions = self._match(term, content_type)
        rank = func.ts_rank(ContentItem.search_vector, query)
        return await self.get_many(
            *conditions,
            order_by=(rank.desc().nulls_last(), ContentItem.id.desc()),
            offset=offset,
            limit=limit,
        )


__all__ = ["SearchRepository"]
