"""站内检索（M6.9 P1 = PG FTS + pg_trgm；B2 起可切外部引擎）。

**读路径引擎择一**（``settings.search_engine``）：

- ``pg``（默认）：两条 PG 路互补——**tsvector 路**走 ``content_items.search_vector`` 生成列
  （``simple`` 分词）配 GIN，对英文/数字词做词法匹配并给 ``ts_rank`` 相关度；**pg_trgm 路**
  走 ``ILIKE '%词%'`` 子串匹配（中文在 ``simple`` 分词下只成一个 lexeme，中文子串实际靠
  这一路）。已知限制：trigram 需 ≥3 字符，**2 字中文词**退化为全表扫描（结果仍正确）。
- ``meilisearch`` / ``opensearch``：内容与文件统一交给引擎检索（含 ``content_type`` 为空的
  默认检索），命中 id 后**一律回查 DB 权威回填**——索引可能滞后（行已软删/下架/被新版本
  取代），故出参与 PG 路径同源口径。引擎调用失败 **fail-open 回落 PG**（记
  ``search_engine_fallback_total``），保证检索面不因外部组件抖动而不可用。

仅暴露已发布内容（``status=published``），匿名可读，与内容列表口径一致。

跨模块只读内容表：与 interaction/feed 同款取法（auth.snapshot 读缝留在本层；content.models
的 import 缝随查询表达式下沉到 ``search/repository.py``），已在 pyproject 的 import-linter
契约中精确豁免——**本文件不得 import content.models**（否则破坏该契约）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.modules.search.engines.base import SearchEngine
from app.modules.search.engines.factory import get_engine
from app.modules.search.errors import SearchErr
from app.modules.search.repository import SearchRepository
from app.modules.search.schemas import SearchHit
from core.common import PageData, paginate_offset, paginate_pages
from core.db.repository import DbSession
from core.err import BizError
from core.metrics import search_engine_fallback_total
from core.ports import interaction as interaction_port
from core.ports.snapshot import get_user_display_names as _author_map

logger = logging.getLogger(__name__)


def _to_hit(item: Any, names: dict[uuid.UUID, str]) -> SearchHit:
    """内容行 → 检索命中项（PG 路径与引擎回填路径共用，口径只有这一处）。

    参数不注解 ``ContentItem``：本文件受 import-linter 契约约束不得 import content.models
    （见模块 docstring），行对象按属性鸭子类型使用。
    """
    return SearchHit(
        id=item.id,
        content_type=item.content_type,
        board_id=item.board_id,
        title=item.title,
        excerpt=item.excerpt or "",
        slug=item.slug,
        author_id=item.author_id,
        author_name=(
            names.get(item.author_id, "") if item.author_id else (item.publisher or "")
        ),
        like_count=item.like_count,
        comment_count=item.comment_count,
        view_count=item.view_count,
        published_at=item.published_at,
        created_at=item.created_at,
    )


def _file_hit(item: Any, names: dict[uuid.UUID, str]) -> SearchHit:
    return SearchHit(
        id=item.id,
        content_type="library_file",
        board_id=None,
        title=item.original_name,
        excerpt=item.description,
        author_id=item.uploader_id,
        author_name=names.get(item.uploader_id, ""),
        like_count=0,
        comment_count=0,
        view_count=item.view_count,
        created_at=item.created_at,
        document_code=item.document_code,
        version=item.version,
        project_id=item.project_id,
    )


async def _pg_hits(
    db: DbSession,
    term: str,
    *,
    page: int,
    limit: int,
    content_type: str | None,
) -> PageData[SearchHit]:
    repo = SearchRepository(db)
    if content_type is None or content_type == "library_file":
        total, ordered_ids = await repo.search_with_files(
            term=term,
            offset=paginate_offset(page, limit),
            limit=limit,
            content_type=content_type,
        )
        content_ids = [i for i, source in ordered_ids if source == "content"]
        file_ids = [i for i, source in ordered_ids if source == "library_file"]
        content_rows = await repo.list_published_by_ids(content_ids)
        file_rows = await repo.public_files_by_ids(file_ids)
        content_map = {row.id: row for row in content_rows}
        file_map = {row.id: row for row in file_rows}
        names = await _author_map(
            db,
            [row.author_id for row in content_rows if row.author_id]
            + [row.uploader_id for row in file_rows],
        )
        hits = [
            _to_hit(content_map[i], names)
            if source == "content"
            else _file_hit(file_map[i], names)
            for i, source in ordered_ids
            if (i in content_map if source == "content" else i in file_map)
        ]
        return PageData(
            items=hits, total=total, page=page, pages=paginate_pages(total, limit)
        )
    total = await repo.count_matching(term=term, content_type=content_type)
    items = await repo.list_matching(
        term=term,
        content_type=content_type,
        offset=paginate_offset(page, limit),
        limit=limit,
    )
    names = await _author_map(db, [i.author_id for i in items if i.author_id])
    return PageData(
        items=[_to_hit(i, names) for i in items],
        total=total,
        page=page,
        pages=paginate_pages(total, limit),
    )


async def _engine_hits(
    db: DbSession,
    engine: SearchEngine,
    term: str,
    *,
    page: int,
    limit: int,
    content_type: str | None,
) -> PageData[SearchHit]:
    ids, total = await engine.search_ids(
        term,
        content_type=content_type,
        offset=paginate_offset(page, limit),
        limit=limit,
    )
    parsed: list[uuid.UUID] = []
    for raw in ids:
        try:
            parsed.append(uuid.UUID(str(raw)))
        except ValueError:
            logger.warning("引擎返回非 uuid 命中，跳过 id=%r", raw)

    repo = SearchRepository(db)
    content_rows = await repo.list_published_by_ids(parsed)
    file_rows = await repo.public_files_by_ids(parsed)
    content_map = {r.id: r for r in content_rows}
    file_map = {r.id: r for r in file_rows}
    names = await _author_map(
        db,
        [r.author_id for r in content_rows if r.author_id]
        + [r.uploader_id for r in file_rows],
    )
    return PageData(
        items=[
            # 保持引擎的相关度顺序；被 DB 口径过滤掉的（已软删/下架/草稿/旧版本/机密
            # 文件）自然缺席，索引滞后不会绕过可见性判定。
            _to_hit(content_map[i], names)
            if i in content_map
            else _file_hit(file_map[i], names)
            for i in parsed
            if i in content_map or i in file_map
        ],
        total=total,
        page=page,
        pages=paginate_pages(total, limit),
    )


def _filter_hidden(
    page: PageData[SearchHit], hidden: set[uuid.UUID]
) -> PageData[SearchHit]:
    """剔除被 block/mute 作者的命中。

    ``total`` 仍是引擎/DB 的原始计数（近似值）——跨页补位需要重扫，收益不抵复杂度；
    约束是「不返回被屏蔽项」，列表本身准确。
    """
    if not hidden:
        return page
    items = [hit for hit in page.items if hit.author_id not in hidden]
    if len(items) == len(page.items):
        return page
    return PageData(
        items=items, total=page.total, page=page.page, pages=page.pages
    )


async def search_items(
    db: DbSession,
    q: str,
    page: int = 1,
    limit: int = 20,
    content_type: str | None = None,
    viewer_id: uuid.UUID | None = None,
) -> PageData[SearchHit]:
    """检索已发布内容。空白词抛 422（Query(min_length=1) 拦不住纯空格）。

    ``viewer_id`` 非空时过滤其 block（双向）/ mute（单向）作者的命中；匿名不过滤。
    """
    term = q.strip()
    if not term:
        raise BizError(SearchErr.EMPTY_QUERY)

    hidden: set[uuid.UUID] = set()
    if viewer_id is not None:
        hidden = set(await interaction_port.get_hidden_author_ids(db, viewer_id))

    engine = get_engine()
    if engine is not None:
        try:
            # 内容与文件都走引擎；回填时按 DB 审核/密级/版本口径实时过滤，
            # 索引滞后不能绕过可见性判定。
            return _filter_hidden(
                await _engine_hits(
                    db, engine, term, page=page, limit=limit, content_type=content_type
                ),
                hidden,
            )
        except Exception:
            search_engine_fallback_total.labels(engine.name).inc()
            logger.exception("外部检索失败，回落 PG 检索 engine=%s", engine.name)

    return _filter_hidden(
        await _pg_hits(db, term, page=page, limit=limit, content_type=content_type),
        hidden,
    )
