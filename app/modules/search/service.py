"""站内检索（M6.9 搜索 P1）：PG FTS + pg_trgm 双路，只读聚合。

两条路互补（口径见 §8 登记）：

- **tsvector 路**：``content_items.search_vector`` 生成列（``simple`` 分词）配 GIN，
  对**英文/数字**词做词法匹配，命中者带 ``ts_rank`` 相关度参与排序。
- **pg_trgm 路**：``ILIKE '%词%'`` 子串匹配，由 ``gin_trgm_ops`` 索引承担。中文在
  ``simple`` 分词下连续整段只成一个 lexeme（FTS 仅能前缀命中），故**中文子串检索
  实际靠这一路**——这也是与 articles 既有 ``_fts_search_stmt`` 一致的取舍。

仅暴露已发布内容（``status=published``），匿名可读，与内容列表口径一致。

**已知限制（真 PG 实测，登记 §8）**：pg_trgm 的 trigram 需 ≥3 字符，故 **2 字中文词**
（如「学习」）无法用上 trgm 索引，退化为全表扫描（结果仍正确）；≥3 字中文与英文词
分别命中 trgm / tsvector 索引（EXPLAIN 已验证为 Bitmap Index Scan）。

跨模块只读内容表：与 interaction/feed 同款取法（auth.snapshot 读缝留在本层；
content.models 的 import 缝随查询表达式下沉到 ``search/repository.py``），已在 pyproject
的 import-linter 契约中精确豁免。
"""

from __future__ import annotations

import uuid

from app.core.common import PageData, paginate_pages
from app.core.err import BizError
from app.db.repository import DbSession
from app.modules.auth.snapshot import get_user_snapshot_batch
from app.modules.search.errors import SearchErr
from app.modules.search.repository import SearchRepository
from app.modules.search.schemas import SearchHit


async def _author_map(db: DbSession, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    ids = {i for i in user_ids if i}
    if not ids:
        return {}
    snaps = await get_user_snapshot_batch(db, user_ids=list(ids))
    return {uid: s.display_name for uid, s in snaps.items()}


async def search_items(
    db: DbSession,
    q: str,
    page: int = 1,
    limit: int = 20,
    content_type: str | None = None,
) -> PageData[SearchHit]:
    """检索已发布内容。空白词抛 422（Query(min_length=1) 拦不住纯空格）。"""
    term = q.strip()
    if not term:
        raise BizError(SearchErr.EMPTY_QUERY)

    repo = SearchRepository(db)
    total = await repo.count_matching(term=term, content_type=content_type)
    items = await repo.list_matching(
        term=term,
        content_type=content_type,
        offset=(page - 1) * limit,
        limit=limit,
    )

    names = await _author_map(db, [i.author_id for i in items if i.author_id])
    hits = [
        SearchHit(
            id=i.id,
            content_type=i.content_type,
            board_id=i.board_id,
            title=i.title,
            excerpt=i.excerpt or "",
            slug=i.slug,
            author_id=i.author_id,
            author_name=(
                names.get(i.author_id, "") if i.author_id else (i.publisher or "")
            ),
            like_count=i.like_count,
            comment_count=i.comment_count,
            view_count=i.view_count,
            published_at=i.published_at,
            created_at=i.created_at,
        )
        for i in items
    ]
    return PageData(
        items=hits, total=total, page=page, pages=paginate_pages(total, limit)
    )
